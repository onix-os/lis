#!/usr/bin/env python3
"""lis2agama — translate a LIS document into openSUSE / SLES installer profiles.

Usage: lis2agama.py FILE.lis.{json,yaml} [--out DIR] [--lenient] [--apply]

Writes both openSUSE unattended formats into DIR (default '.'):
  profile.json   — Agama configuration (Tumbleweed / Leap 16 / SLES 16)
  autoyast.xml   — AutoYaST profile (Leap 15.x and older, and the E2E harness)

`--apply` hands profile.json to the Agama CLI; Agama does the installation.
This applier never partitions a disk itself.

Fail-closed by default (SPEC §2.3): core intent neither format can express is
*refused* with exit status 1. `--lenient` downgrades refusals to warnings.
"""

import argparse
import json
import pathlib
import sys
import xml.etree.ElementTree as ET

from lis_common import (track, check_unread, check_raid_consumers, chroot_intents, registration_commands, enrollment_commands, luks_key_path, seed_mount_commands, SEED_MOUNT, resolve_disk_paths, check_snapshots, match_selectors, system_commands, security_packages, file_commands, uid_commands, password_field, shell_packages, check_arch, check_script_fields,ALL_SECTIONS, add_common_args, check_firmware,
                        check_unhandled, check_section_fields, sudoers_commands, kernel_params_commands, check_kernel_variant, check_mirror, boot_timeout_commands, driver_packages,
                        check_boot_extras, check_keymap, check_version, enforce,
                        check_encryption_emitted, resolve_mountpoints,
                        load_doc, refuse, report, role_fs, warn)

NS = "http://www.suse.com/1.0/yast2ns"
CONFIG_NS = "http://www.suse.com/1.0/configns"

# YaST names its keymaps differently from the X layout codes LIS carries.
YAST_KEYMAP = {"us": "english-us", "uk": "english-uk", "gb": "english-uk",
               "de": "german", "fr": "french", "es": "spanish", "it": "italian"}

FS_MAP = {"ext4": "ext4", "xfs": "xfs", "btrfs": "btrfs", "vfat": "vfat", "swap": "swap"}


def size_str(size: str, what: str) -> str | None:
    """LIS size → the string Agama and AutoYaST both accept ('8 GiB', 'rest')."""
    if size == "rest":
        return None
    if size.endswith("%"):
        refuse(f"{what}: percent size {size!r} is not expressible in an openSUSE profile")
        return None
    for unit, suffix in (("TiB", "T"), ("GiB", "G"), ("MiB", "M")):
        if size.endswith(unit):
            return f"{int(size[: -len(unit)])}{suffix}"
    refuse(f"{what}: unparseable size {size!r}")
    return None


def check_unsupported(doc: dict) -> None:
    """Core intent neither openSUSE format expresses from a generated profile."""
    storage = doc.get("storage", {}) or {}
    network = doc.get("network", {}) or {}
    software = doc.get("software", {}) or {}
    if storage.get("raid"):
        names = ", ".join(a["name"] for a in storage["raid"])
        warn(f"storage.raid ({names}): emitted in the AutoYaST profile "
             "(raid_options/raid_type); the Agama JSON profile carries no array")
    for container in storage.get("encryption", []) or []:
        if not luks_key_path(doc, container["id"]):
            refuse(f"storage.encryption ({container['id']}): no key material — declare a "
                   "keys[] entry with a seed: source, or place the passphrase at "
                   f"{SEED_MOUNT}/secrets/luks-{container['id']}.key")
        elif container.get("type") not in (None, "luks1", "luks2"):
            refuse(f"storage.encryption ({container['id']}): type "
                   f"{container['type']!r} is not a LUKS variant AutoYaST can create")
        for method in container.get("unlock", []) or []:
            if method not in ("passphrase", "keyfile"):
                warn(f"storage.encryption ({container['id']}): unlock method "
                     f"{method!r} must be enrolled after installation")
    if storage.get("encryption"):
        # Neither profile carries the secret (delivery.md §6): both hold the
        # placeholder and pick the key up from the seed at apply time — the
        # AutoYaST pre-script rewrites modified.xml, `--apply` rewrites a
        # private copy of profile.json. Stated because handing the generated
        # profile.json straight to `agama config load` would encrypt the disk
        # with the literal placeholder text.
        warn("storage.encryption: both profiles carry the placeholder "
             "'@@LIS_CRYPT_<id>@@' instead of the passphrase; apply profile.json "
             "with this applier's --apply, which substitutes it from the seed")
    if network.get("wifi"):
        refuse("network.wifi is not expressible in a generated openSUSE profile")
    if network.get("firewall"):
        pass   # honored by chroot_intents()
    if software.get("snap"):
        refuse("software.snap is not available on openSUSE")
    if software.get("exclude"):
        pass   # honored by chroot_intents()
    for user in doc.get("users", []) or []:
        password = user.get("password") or {}
        if not password.get("hash") and not password.get("locked"):
            refuse(f"user '{user['name']}': no password hash and not marked locked")
        if user.get("dotfiles"):
            pass   # honored by chroot_intents()
    if (doc.get("scripts", {}) or {}).get("on_error"):
        refuse("scripts.on_error has no openSUSE profile equivalent")


def disk_paths(doc: dict) -> dict[str, str]:
    out = {}
    for disk in (doc.get("target", {}) or {}).get("disks", []):
        match_selectors(disk)
        path = (disk.get("match", {}) or {}).get("path")
        if not path:
            refuse(f"disk '{disk['id']}': openSUSE profiles need an explicit match.path")
            continue
        out[disk["id"]] = path
    return out


def collect_scripts(doc: dict) -> tuple[list[str], list[str], list[str]]:
    """(pre, post/chroot, first-boot) script bodies in the order they must run."""
    scripts = doc.get("scripts", {}) or {}
    pre = [s["content"] for stage in ("pre_install", "pre")
           for s in scripts.get(stage, []) if s.get("content")]
    post = [s["content"] for stage in ("post_storage", "post_install", "post",
                                       "pre_reboot", "on_success")
            for s in scripts.get(stage, []) if s.get("content")]
    for user in doc.get("users", []) or []:
        for s in (user.get("scripts", {}) or {}).get("post_install", []):
            if c := s.get("content"):
                post.append(f"su - {user['name']} -c {json.dumps(c)}")
    post += sudoers_commands(doc)
    post += uid_commands(doc)
    post += enrollment_commands(doc)
    post += registration_commands(doc, "suse")
    post += chroot_intents(doc, "suse")
    post += system_commands(doc, "suse")
    post += boot_timeout_commands(doc, "suse", (doc.get("boot") or {}).get("loader", "grub"))
    post += kernel_params_commands(doc, "suse")

    # files[] and proxy go through the chroot scripts that both outputs already
    # carry, rather than through AutoYaST-only elements: the same list reaches
    # profile.json and autoyast.xml, so one implementation covers both engines.
    import base64 as _b64
    for entry in doc.get("files", []) or []:
        post += file_commands(entry)

    if mirror_url := (doc.get("mirror", {}) or {}).get("url"):
        post.append("zypper --non-interactive ar --refresh --priority 50 "
                    f"{json.dumps(mirror_url)} lis-mirror")

    proxy = doc.get("proxy", {}) or {}
    if proxy:
        # /etc/sysconfig/proxy is where SUSE reads it from.
        settings = [("PROXY_ENABLED", "yes")]
        if proxy.get("http"):
            settings.append(("HTTP_PROXY", proxy["http"]))
        if proxy.get("https"):
            settings.append(("HTTPS_PROXY", proxy["https"]))
        if proxy.get("no_proxy"):
            settings.append(("NO_PROXY", ",".join(proxy["no_proxy"])))
        body = "".join(f'{k}="{v}"\n' for k, v in settings)
        payload = _b64.b64encode(body.encode()).decode()
        post.append(f"echo {payload} | base64 -d > /etc/sysconfig/proxy")
    firstboot = [s["content"] for s in scripts.get("firstboot", []) if s.get("content")]
    for user in doc.get("users", []) or []:
        for s in (user.get("scripts", {}) or {}).get("firstboot", []):
            if c := s.get("content"):
                firstboot.append(f"su - {user['name']} -c {json.dumps(c)}")
    for app in (doc.get("software", {}) or {}).get("flatpak", []) or []:
        firstboot.append(f"flatpak install -y --noninteractive flathub {app}")

    # Birth certificate (delivery.md §8) — written in the target root.
    import base64
    birth = base64.b64encode(json.dumps(doc, separators=(",", ":")).encode()).decode()
    post.append(f"install -d -m755 /var/lib/lis && echo {birth} | base64 -d "
                "> /var/lib/lis/system.lis.json && chmod 600 /var/lib/lis/system.lis.json")
    return pre, post, firstboot


def packages_of(doc: dict) -> tuple[list[str], list[str]]:
    software = doc.get("software", {}) or {}
    desktop = doc.get("desktop", {}) or {}
    pkgs = list(software.get("packages", []))
    pkgs += driver_packages(doc, "suse")
    pkgs += shell_packages(doc)
    pkgs += security_packages(doc, "suse")
    snapshots = (doc.get("storage", {}) or {}).get("snapshots") or {}
    if snapshots.get("enabled"):
        pkgs.append("snapper")
        if snapshots.get("boot_menu"):
            # The snapshot submenu in GRUB comes from this plugin, not from
            # snapper itself; claiming boot_menu support without it would be a
            # capability declared and not delivered.
            pkgs.append("grub2-snapper-plugin")
    if kernel_pkg := check_kernel_variant(
            doc, {"lts": "kernel-longterm", "realtime": "kernel-rt"}, "openSUSE"):
        pkgs.append(kernel_pkg)
    for app in software.get("apps", []):
        if isinstance(app, str):
            pkgs.append(app)
        elif isinstance(app, dict):
            if name := (app.get("package") or app.get("name")):
                pkgs.append(name)
            if app.get("flatpak"):
                pkgs.append("flatpak")
    if software.get("flatpak"):
        pkgs.append("flatpak")
    if desktop.get("printing"):
        pkgs.append("cups")
    if desktop.get("bluetooth"):
        pkgs.append("bluez")
    if (doc.get("storage", {}) or {}).get("swap", {}).get("zram"):
        pkgs.append("systemd-zram-service")

    role = software.get("role", "")
    patterns = {"desktop:gnome": ["gnome", "gnome_basis"],
                "desktop:kde": ["kde", "kde_plasma"],
                "desktop:xfce": ["xfce", "xfce_basis"],
                "server": ["base", "minimal_base"],
                "minimal": ["base"], "": []}.get(role)
    if patterns is None:
        refuse(f"software.role {role!r} has no openSUSE pattern")
        patterns = []
    return pkgs, patterns


# ── Agama profile.json ───────────────────────────────────────────

def render_agama(doc: dict) -> dict:
    system = doc.get("system", {}) or {}
    storage = doc.get("storage", {}) or {}
    network = doc.get("network", {}) or {}
    users = doc.get("users", []) or []
    km = system.get("keymap", {}) or {}
    keymap = km.get("layout") or km.get("console", "us")
    if km.get("variant"):
        keymap = f"{keymap}({km['variant']})"
    pkgs, patterns = packages_of(doc)
    pre, post, firstboot = collect_scripts(doc)

    profile: dict = {
        "product": {"id": (doc.get("x-suse", {}) or {}).get("product", "Tumbleweed")},
        "localization": {
            "language": system.get("locale", "en_US.UTF-8"),
            "keyboard": keymap,
            "timezone": system.get("timezone", "UTC"),
        },
        "software": {},
        "scripts": {},
    }
    if patterns:
        profile["software"]["patterns"] = patterns
    if pkgs:
        profile["software"]["packages"] = pkgs

    if hostname := system.get("hostname"):
        profile["hostname"] = {"static": hostname}
    if manager := network.get("manager"):
        if manager not in ("auto", "networkmanager"):
            refuse(f"network.manager {manager!r} is not selectable in an Agama profile")

    root = next((u for u in users if u["name"] == "root"), None)
    if root:
        entry = {}
        if field := password_field(root):
            entry["hashedPassword"] = field
        if keys := root.get("ssh_authorized_keys"):
            entry["sshPublicKey"] = keys[0]
        if entry:
            profile["root"] = entry
    normal = [u for u in users if u["name"] != "root"]
    if normal:
        primary = normal[0]
        profile["user"] = {
            "userName": primary["name"],
            "fullName": primary.get("comment", primary["name"]),
            "hashedPassword": password_field(primary) or "!",
        }
        for extra in normal[1:]:
            groups = ",".join(extra.get("groups", []))
            post.insert(0, f"useradd -m -p {json.dumps(password_field(extra) or '!')}"
                        + (f" -u {extra['uid']}" if extra.get("uid") is not None else "")
                        + (f" -G {groups}" if groups else "") + f" {extra['name']}")

    if drives := agama_drives(doc):
        profile["storage"] = {"drives": drives}
    elif storage:
        refuse("storage section could not be translated into Agama drives")

    # profile.json and autoyast.xml are alternative *complete* profiles: only
    # one of them installs the machine, so encryption reaching the AutoYaST
    # side says nothing about this one. Checked against the storage section
    # alone, so nothing outside it can vouch for a container.
    check_encryption_emitted(doc, profile.get("storage") or {},
                             marker=lambda c: crypt_placeholder(c["id"]),
                             label="Agama profile.json storage section")

    if pre:
        profile["scripts"]["pre"] = [{"name": f"lis-pre-{i}", "body": body}
                                     for i, body in enumerate(pre)]
    if post:
        profile["scripts"]["chroot"] = [{"name": f"lis-post-{i}", "body": body}
                                        for i, body in enumerate(post)]
    if firstboot:
        profile["scripts"]["init"] = [{"name": f"lis-firstboot-{i}", "body": body}
                                      for i, body in enumerate(firstboot)]
    return profile


def agama_encryption(container: dict) -> dict:
    """The Agama `encryption` block for one LUKS container.

    Agama's storage schema names the variant as the key (`luks1`/`luks2`) and
    takes the passphrase inline. LIS never writes a secret into a generated
    file, so the placeholder goes here and `--apply` substitutes it from the
    seed into a private copy that never touches --out.
    """
    variant = "luks1" if container.get("type") == "luks1" else "luks2"
    return {variant: {"password": crypt_placeholder(container["id"])}}


def agama_drives(doc: dict) -> list[dict]:
    storage = doc.get("storage", {}) or {}
    paths = disk_paths(doc)
    consumed = {d for g in storage.get("lvm", []) or [] for d in g.get("devices", [])}
    # A group may name the LUKS container rather than the partition under it;
    # the partition is still what the group consumes.
    consumed |= {c["over"] for c in (storage.get("encryption", []) or [])
                 if c["id"] in consumed}
    crypt_over = {c["over"]: c for c in (storage.get("encryption", []) or [])}
    if storage.get("lvm"):
        warn("storage.lvm: emitted in the AutoYaST profile (is_lvm_vg/lv_name); "
             "the Agama JSON profile carries no volume group")
    # One arbitration over the whole list, not one per disk: a mirrored boot
    # partition carrying `role: boot` on the second disk must not be handed
    # /boot behind the back of the partition that declared it.
    parts = list(storage.get("partitions", []) or [])
    claimed = {v["mountpoint"] for g in storage.get("lvm", []) or []
               for v in (g.get("volumes") or []) if v.get("mountpoint")}
    mounts = resolve_mountpoints(parts, claimed=claimed)
    drives = []
    for handle, path in paths.items():
        partitions: list[dict] = []
        if storage.get("wipe"):
            partitions.append({"search": "*", "delete": True})
        for i, part in enumerate(parts):
            if part.get("disk") != handle:
                continue
            if part.get("existing"):
                refuse(f"partition {i}: adopting an existing partition is not "
                       "expressible in a generated Agama profile")
                continue
            name = part.get("id") or f"auto-{i}"
            role = part.get("role")
            fs = role_fs(part)
            entry: dict = {}
            if size := size_str(part.get("size", "rest"), f"partition '{name}'"):
                entry["size"] = size
            # Before every early `continue`: a LUKS container over a partition
            # that is also an LVM physical volume, or that carries no
            # filesystem of its own, is still encryption the document declared.
            if container := crypt_over.get(part.get("id")):
                entry["encryption"] = agama_encryption(container)
            if name in consumed:
                entry["id"] = "lvm"
                partitions.append(entry)
                continue
            if fs in (None, "none"):
                partitions.append(entry)
                continue
            mountpoint = mounts[i]
            filesystem: dict = {"type": FS_MAP.get(fs, fs)}
            if fs == "swap":
                filesystem["path"] = "swap"
            elif mountpoint:
                filesystem["path"] = mountpoint
            if subs := part.get("subvolumes"):
                if fs != "btrfs":
                    refuse(f"partition '{name}': subvolumes on a {fs} filesystem")
                else:
                    # The declared name, verbatim: Agama carries a mount path
                    # per subvolume, so "@home" needs no rewriting to land at
                    # /home and rewriting it would create a subvolume the
                    # document never asked for.
                    filesystem["type"] = {"btrfs": {
                        "snapshots": bool((storage.get("snapshots", {}) or {}).get("enabled")),
                        "subvolumes": [{"path": s["name"],
                                        "mountPath": s["mountpoint"]} for s in subs],
                    }}
            entry["filesystem"] = filesystem
            if role == "esp":
                entry["id"] = "esp"
            partitions.append(entry)
        drives.append({"search": path, "partitions": partitions})
    return drives


# ── AutoYaST profile ─────────────────────────────────────────────

def crypt_placeholder(cid: str) -> str:
    """Marker a pre-script replaces with seed key material inside the installer."""
    return f"@@LIS_CRYPT_{cid.replace('-', '_')}@@"


def norm_path(path: str) -> str:
    """'/', '/home', '/home/' → '/', '/home', '/home'."""
    return "/" + (path or "/").strip("/")


def autoyast_subvolumes(base: str | None, subs: list[dict],
                        what: str) -> tuple[str, list[str]]:
    """Map declared subvolumes onto AutoYaST's prefix model.

    storage-ng AutoYaST does not take a subvolume name and a mount point. It
    takes one *prefix* subvolume — the one the filesystem itself is mounted
    from, `@` by default — and nests every listed `<path>` under it, deriving
    that subvolume's mount point from the same path: `<path>home</path>`
    creates `@/home` and mounts it at `<fs mount>/home`. There is no element
    for a subvolume's mount point (which is why the old `<path>home</path>`
    happened to work) and no way to declare a flat sibling of the prefix.

    So the path is derived from the declared *mountpoint*, and the prefix from
    whichever subvolume claims the filesystem's own mount point. `@home` at
    /home is therefore created as `@/home`, and that is warned about rather
    than passed off as the declared name — the previous `lstrip("@")` produced
    the same on-disk layout while silently claiming the document had been
    honored, and additionally emitted the prefix subvolume as a child of
    itself (`@` → `<path>@</path>` → `@/@`).

    Returns (prefix, [path, …]).
    """
    base_n = norm_path(base or "/")
    root = base_n if base_n == "/" else base_n + "/"
    prefix = ""
    rest = []
    for sub in subs:
        if sub.get("mountpoint") and norm_path(sub["mountpoint"]) == base_n:
            if prefix:
                refuse(f"{what}: subvolumes {prefix!r} and {sub['name']!r} both claim "
                       f"the filesystem's own mount point {base_n} — only one "
                       "subvolume can carry it")
                continue
            prefix = sub["name"]
        else:
            rest.append(sub)

    paths = []
    for sub in rest:
        name = sub["name"]
        if mountpoint := sub.get("mountpoint"):
            target = norm_path(mountpoint)
            if not target.startswith(root):
                refuse(f"{what}: subvolume {name!r} declares mountpoint "
                       f"{mountpoint!r}, which is outside the filesystem mounted at "
                       f"{base_n} — AutoYaST derives a subvolume's mount point from "
                       "its path and cannot mount it anywhere else")
                continue
            path = target[len(root):]
        else:
            # Nothing to derive from, so nothing is invented: the name is used
            # as written and AutoYaST will mount it at <fs mount>/<name>.
            path = name
        created = f"{prefix}/{path}" if prefix else path
        if created != name:
            warn(f"{what}: subvolume {name!r} is created as {created!r} — AutoYaST "
                 f"nests every subvolume under the prefix {prefix or '(none)'!r} and "
                 "has no element for a subvolume mount point, so a flat sibling of "
                 "the prefix is not expressible in this profile")
        paths.append(path)
    return prefix, paths


def add_subvolumes(node, base: str | None, subs: list[dict], what: str) -> None:
    """<subvolumes_prefix> + <subvolumes> under one partition/volume element."""
    prefix, paths = autoyast_subvolumes(base, subs, what)
    # Stated rather than left to the product default, which decides whether the
    # paths below land under `@` or at the top level of the filesystem.
    ET.SubElement(node, "subvolumes_prefix").text = prefix
    sub_list = ET.SubElement(node, "subvolumes")
    sub_list.set(f"{{{CONFIG_NS}}}type", "list")
    for path in paths:
        ET.SubElement(ET.SubElement(sub_list, "subvolume"), "path").text = path


def render_autoyast(doc: dict) -> str:
    system = doc.get("system", {}) or {}
    storage = doc.get("storage", {}) or {}
    installer = doc.get("installer", {}) or {}
    users = doc.get("users", []) or []
    pkgs, patterns = packages_of(doc)
    pre, post, firstboot = collect_scripts(doc)

    ET.register_namespace("", NS)
    ET.register_namespace("config", CONFIG_NS)
    profile = ET.Element(f"{{{NS}}}profile")

    general = ET.SubElement(profile, "general")
    mode = ET.SubElement(general, "mode")
    boolean(mode, "confirm", False)
    boolean(mode, "final_reboot", installer.get("on_finish", "reboot") == "reboot")
    if installer.get("on_finish") not in (None, "reboot", "poweroff"):
        refuse(f"installer.on_finish {installer['on_finish']!r} has no AutoYaST equivalent")

    lang = ET.SubElement(profile, "language")
    ET.SubElement(lang, "language").text = system.get("locale", "en_US.UTF-8").split(".")[0]
    ET.SubElement(profile, "timezone").append(text_node("timezone",
                                                        system.get("timezone", "UTC")))
    # AutoYaST configures the bootloader after chroot-scripts run, so editing
    # /etc/default/grub from a script is overwritten. bootloader/global carries
    # both the menu timeout and the kernel command line natively
    # (schema: bl_global → bl_timeout INTEGER, append STRING).
    boot_cfg = doc.get("boot", {}) or {}
    bl_timeout = boot_cfg.get("timeout")
    bl_append = (boot_cfg.get("kernel", {}) or {}).get("params")
    if bl_timeout is not None or bl_append:
        bl_global = ET.SubElement(ET.SubElement(profile, "bootloader"), "global")
        if bl_timeout is not None:
            node = ET.SubElement(bl_global, "timeout")
            node.set(f"{{{CONFIG_NS}}}type", "integer")
            node.text = str(int(bl_timeout))
        if bl_append:
            ET.SubElement(bl_global, "append").text = " ".join(bl_append)

    keyboard = ET.SubElement(profile, "keyboard")
    _km = system.get("keymap", {}) or {}
    console_map = _km.get("console") or _km.get("layout") or "us"
    ET.SubElement(keyboard, "keymap").text = YAST_KEYMAP.get(console_map, console_map)
    if hostname := system.get("hostname"):
        networking = ET.SubElement(profile, "networking")
        dns = ET.SubElement(networking, "dns")
        ET.SubElement(dns, "hostname").text = hostname

    crypt_over = {c["over"]: c for c in (storage.get("encryption", []) or [])}
    raid_member = {d: a for a in (storage.get("raid", []) or [])
                   for d in a.get("devices", [])}
    lvm_member = {d: g for g in (storage.get("lvm", []) or [])
                  for d in g.get("devices", [])}
    # One arbitration over every partition on every disk, seeded with the paths
    # the logical volumes already claim: role_mountpoint() alone synthesises
    # /boot for the *mirror* of a boot partition as readily as for the original,
    # and AutoYaST stops on the duplicate fstab entry.
    parts = list(storage.get("partitions", []) or [])
    claimed = {v["mountpoint"] for g in storage.get("lvm", []) or []
               for v in (g.get("volumes") or []) if v.get("mountpoint")}
    mounts = resolve_mountpoints(parts, claimed=claimed)
    partitioning = ET.SubElement(profile, "partitioning")
    partitioning.set(f"{{{CONFIG_NS}}}type", "list")
    for handle, path in disk_paths(doc).items():
        drive = ET.SubElement(partitioning, "drive")
        ET.SubElement(drive, "device").text = path
        boolean(drive, "initialize", bool(storage.get("wipe")))
        ET.SubElement(drive, "use").text = "all"
        plist = ET.SubElement(drive, "partitions")
        plist.set(f"{{{CONFIG_NS}}}type", "list")
        for i, part in enumerate(parts):
            if part.get("disk") != handle:
                continue
            role = part.get("role")
            fs = role_fs(part)
            node = ET.SubElement(plist, "partition")
            if size := size_str(part.get("size", "rest"), f"partition '{part.get('id', i)}'"):
                ET.SubElement(node, "size").text = size
            else:
                ET.SubElement(node, "size").text = "max"
            container = crypt_over.get(part.get("id"))
            # A volume group may name either the partition itself or the LUKS
            # container wrapping it (LVM inside LUKS). Either way the partition
            # is the group's physical volume, so it carries lvm_group and *no*
            # filesystem/mount: a PV holds no filesystem, and role "root" would
            # otherwise synthesise btrfs on / here and collide with the logical
            # volume that declares the same mountpoint — AutoYaST then finds no
            # suitable physical volume for the group and stops.
            group = lvm_member.get(part.get("id"))
            if group is None and container:
                group = lvm_member.get(container["id"])
            if group is None and fs not in (None, "none"):
                filesystem = ET.SubElement(node, "filesystem")
                filesystem.set(f"{{{CONFIG_NS}}}type", "symbol")
                filesystem.text = FS_MAP.get(fs, fs)
            # A physical volume holds no filesystem, so it is never mounted;
            # everything else takes whatever the arbitration above gave it.
            mountpoint = None if group else mounts[i]
            if group:
                pass
            elif fs == "swap":
                ET.SubElement(node, "mount").text = "swap"
            elif mountpoint:
                ET.SubElement(node, "mount").text = mountpoint
            # LUKS on this partition: AutoYaST needs the key in the profile
            # (schema: crypt_fs BOOLEAN, crypt_method SYMBOL, crypt_key STRING),
            # so a placeholder goes here and a pre-script substitutes the real
            # value from the seed before the profile is parsed.
            if array := raid_member.get(part.get("id")):
                # 253 is the partition id AutoYaST uses for a RAID member.
                pid = ET.SubElement(node, "partition_id")
                pid.set(f"{{{CONFIG_NS}}}type", "integer")
                pid.text = "253"
                ET.SubElement(node, "raid_name").text = f"/dev/md/{array['name']}"
            if group:
                ET.SubElement(node, "lvm_group").text = group["name"]
            if container:
                boolean(node, "crypt_fs", True)
                method = ET.SubElement(node, "crypt_method")
                method.set(f"{{{CONFIG_NS}}}type", "symbol")
                method.text = "luks2" if container.get("type") != "luks1" else "luks1"
                ET.SubElement(node, "crypt_key").text = crypt_placeholder(container["id"])
            if subs := part.get("subvolumes"):
                add_subvolumes(node, mountpoint, subs,
                               f"partition '{part.get('id', i)}'")

    software = ET.SubElement(profile, "software")
    # AutoYaST needs a base product or it stops on "None or wrong base product"
    # and waits for someone to pick one. LIS has no field for it, so this is
    # stated rather than guessed silently.
    product = (doc.get("x-suse", {}) or {}).get("product")
    if not product:
        product = "Leap"
        warn("no x-suse.product declared; the AutoYaST profile targets "
             f"{product!r} — set x-suse.product for other media")
    products = ET.SubElement(software, "products")
    products.set(f"{{{CONFIG_NS}}}type", "list")
    ET.SubElement(products, "product").text = product
    if patterns:
        node = ET.SubElement(software, "patterns")
        node.set(f"{{{CONFIG_NS}}}type", "list")
        for pattern in patterns:
            ET.SubElement(node, "pattern").text = pattern
    if pkgs:
        node = ET.SubElement(software, "packages")
        node.set(f"{{{CONFIG_NS}}}type", "list")
        for pkg in pkgs:
            ET.SubElement(node, "package").text = pkg

    group_members: dict[str, list[str]] = {}
    user_list = ET.SubElement(profile, "users")
    user_list.set(f"{{{CONFIG_NS}}}type", "list")
    for user in users:
        node = ET.SubElement(user_list, "user")
        ET.SubElement(node, "username").text = user["name"]
        password = user.get("password") or {}
        ET.SubElement(node, "user_password").text = password_field(user) or "!"
        boolean(node, "encrypted", True)
        if comment := user.get("comment"):
            ET.SubElement(node, "fullname").text = comment
        if shell := user.get("shell"):
            ET.SubElement(node, "shell").text = (
                shell if shell.startswith("/") else f"/usr/bin/{shell}")
        for group in list(user.get("groups", [])) + (
                ["wheel"] if user.get("admin") else []):
            group_members.setdefault(group, []).append(user["name"])

    if group_members:
        group_list = ET.SubElement(profile, "groups")
        group_list.set(f"{{{CONFIG_NS}}}type", "list")
        for group, members in group_members.items():
            node = ET.SubElement(group_list, "group")
            ET.SubElement(node, "groupname").text = group
            ET.SubElement(node, "userlist").text = ",".join(dict.fromkeys(members))

    # AutoYaST re-reads /tmp/profile/modified.xml if a pre-script writes one,
    # which is the documented way to inject a value that must not travel in the
    # served profile. The passphrase exists only in installer memory.
    crypt_ids = [c["id"] for c in (storage.get("encryption", []) or [])
                 if luks_key_path(doc, c["id"])]
    if crypt_ids:
        inject = ["set -e", *seed_mount_commands(),
                  "cp /tmp/profile/autoinst.xml /tmp/profile/modified.xml"]
        for cid in crypt_ids:
            key_path = luks_key_path(doc, cid)
            inject.append(f'key=$(cat {key_path})')
            inject.append(f'sed -i "s|{crypt_placeholder(cid)}|$key|" '
                          "/tmp/profile/modified.xml")
        pre = list(pre) + ["\n".join(inject)]

    # The array and the volume group are drives in their own right; without
    # these entries the members are assembled and then nothing is put on them.
    for array in storage.get("raid", []) or []:
        drive = ET.SubElement(partitioning, "drive")
        ET.SubElement(drive, "device").text = f"/dev/md/{array['name']}"
        # Without the type symbol AutoYaST treats the drive as a
        # physical disk and aborts with "Disk '…' was not found".
        dtype = ET.SubElement(drive, "type")
        dtype.set(f"{{{CONFIG_NS}}}type", "symbol")
        dtype.text = "CT_MD"
        # An array is used whole: without disklabel=none AutoYaST expects a
        # partition table on /dev/md/<name> and the commit step fails.
        ET.SubElement(drive, "disklabel").text = "none"
        ET.SubElement(drive, "use").text = "all"
        plist = ET.SubElement(drive, "partitions")
        plist.set(f"{{{CONFIG_NS}}}type", "list")
        node = ET.SubElement(plist, "partition")
        # The array's own filesystem/mount is declared by a partitions[] entry
        # whose id is the array name; it took part in the arbitration above, so
        # its mountpoint comes from there rather than from a second lookup.
        target_at = next((j for j, q in enumerate(parts)
                          if q.get("id") == array["name"]), None)
        target = parts[target_at] if target_at is not None else {}
        opts = ET.SubElement(node, "raid_options")
        ET.SubElement(opts, "raid_type").text = f"raid{array['level']}"
        # If a volume group consumes the array, the array *is* its physical
        # volume — without lvm_group the group has no PV and AutoYaST stops at
        # "Partitioning issues" naming the group.
        owner = next((g for g in (storage.get("lvm", []) or [])
                      if array["name"] in (g.get("devices") or [])), None)
        if owner:
            # No partition_id here: 0x8E is a partition-table type byte and the
            # array is used whole, not partitioned. Setting it segfaults YaST in
            # "Preparing disks".
            ET.SubElement(node, "lvm_group").text = owner["name"]
        if mount := (mounts[target_at] if target_at is not None else None):
            ET.SubElement(node, "mount").text = mount
        if fs := role_fs(target):
            filesystem = ET.SubElement(node, "filesystem")
            filesystem.set(f"{{{CONFIG_NS}}}type", "symbol")
            filesystem.text = FS_MAP.get(fs, fs)
        if subs := target.get("subvolumes"):
            add_subvolumes(node, mount, subs, f"array '{array['name']}'")

    for group in storage.get("lvm", []) or []:
        drive = ET.SubElement(partitioning, "drive")
        ET.SubElement(drive, "device").text = f"/dev/{group['name']}"
        # Without the type symbol AutoYaST treats the drive as a
        # physical disk and aborts with "Disk '…' was not found".
        dtype = ET.SubElement(drive, "type")
        dtype.set(f"{{{CONFIG_NS}}}type", "symbol")
        dtype.text = "CT_LVM"
        boolean(drive, "is_lvm_vg", True)
        plist = ET.SubElement(drive, "partitions")
        plist.set(f"{{{CONFIG_NS}}}type", "list")
        for volume in group.get("volumes", []) or []:
            node = ET.SubElement(plist, "partition")
            ET.SubElement(node, "lv_name").text = volume["name"]
            ET.SubElement(node, "size").text = size_str(
                volume.get("size", "rest"), f"volume '{volume['name']}'") or "max"
            if volume.get("mountpoint"):
                ET.SubElement(node, "mount").text = volume["mountpoint"]
            if fs := volume.get("fs"):
                filesystem = ET.SubElement(node, "filesystem")
                filesystem.set(f"{{{CONFIG_NS}}}type", "symbol")
                filesystem.text = FS_MAP.get(fs, fs)
            # Subvolumes on a logical volume were read and then dropped on the
            # floor here; AutoYaST's partition section is the same schema for a
            # logical volume, so they belong in the profile like any other.
            if subs := volume.get("subvolumes"):
                if fs != "btrfs":
                    refuse(f"volume '{volume['name']}': subvolumes on a {fs} filesystem")
                else:
                    add_subvolumes(node, volume.get("mountpoint"), subs,
                                   f"volume '{volume['name']}'")

    # X1: a document that declares LUKS and renders to a profile with no trace
    # of it installs a plaintext disk and reports success. Only <partitioning>
    # can express encryption, so only <partitioning> is allowed to vouch for
    # it — the pre-script below carries the same placeholder and would
    # otherwise answer for a container that no partition ever encrypted. The
    # two output files are alternative complete profiles, so each is checked
    # alone; encryption in the sibling is no defence.
    check_encryption_emitted(doc, ET.tostring(partitioning, encoding="unicode"),
                             marker=lambda c: crypt_placeholder(c["id"]),
                             label="AutoYaST <partitioning> section")

    scripts = ET.SubElement(profile, "scripts")
    add_script_list(scripts, "pre-scripts", pre)
    add_script_list(scripts, "chroot-scripts", post, chrooted=True)
    add_script_list(scripts, "init-scripts", firstboot, interpreter=False)

    ET.indent(profile, space="  ")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE profile>\n'
            + ET.tostring(profile, encoding="unicode") + "\n")


def boolean(parent, name: str, value: bool) -> None:
    node = ET.SubElement(parent, name)
    node.set(f"{{{CONFIG_NS}}}type", "boolean")
    node.text = "true" if value else "false"


def text_node(name: str, value: str):
    node = ET.Element(name)
    node.text = value
    return node


def add_script_list(parent, kind: str, bodies: list[str], chrooted=False,
                    interpreter=True) -> None:
    """One <pre-scripts>/<chroot-scripts>/<init-scripts> block.

    The three differ in what a <script> may contain, and the schema is strict:
    `chrooted` is only legal under chroot-scripts, and init-scripts accept no
    `interpreter` at all (they run at first boot from a generated init file).
    The LIST reference is mandatory, hence config:type="list".
    """
    if not bodies:
        return
    node = ET.SubElement(parent, kind)
    node.set(f"{{{CONFIG_NS}}}type", "list")
    for i, body in enumerate(bodies):
        # Element order matches the documented example exactly — interpreter,
        # filename, source. The schema is not an interleave here, so a different
        # order makes the whole <scripts> section fail to match.
        script = ET.SubElement(node, "script")
        if interpreter:
            ET.SubElement(script, "interpreter").text = "shell"
        ET.SubElement(script, "filename").text = f"lis-{kind}-{i}.sh"
        if chrooted:
            boolean(script, "chrooted", True)
        ET.SubElement(script, "source").text = "\n" + body + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Translate a LIS document into openSUSE Agama and AutoYaST profiles.")
    add_common_args(ap)
    ap.add_argument("--apply", "-a", action="store_true",
                    help="apply profile.json with the Agama CLI and start the install")
    args = ap.parse_args()

    raw = load_doc(args.file)
    if args.apply:
        # Rules like {type: nvme, smallest: true} can only be evaluated with the
        # machine in front of us. Resolved before tracking: the tracker hands out
        # copies, so a mutation through it would never reach the document.
        resolve_disk_paths(raw)
    doc = track(raw)
    check_version(doc, args.file)
    check_firmware(doc)
    check_unhandled(doc, ALL_SECTIONS)
    check_boot_extras(doc, {"loader", "timeout", "kernel", "variant", "params"})
    check_mirror(doc, {"url"})
    check_section_fields(doc, "desktop", {"bluetooth", "printing"})
    check_section_fields(doc, "installer", {"on_finish"})
    check_keymap(doc, {"console", "layout", "variant"})

    check_unsupported(doc)
    profile = render_agama(doc)
    autoyast = render_autoyast(doc)

    args.out.mkdir(parents=True, exist_ok=True)
    profile_file = args.out / "profile.json"
    autoyast_file = args.out / "autoyast.xml"
    profile_file.write_text(json.dumps(profile, indent=2) + "\n")
    autoyast_file.write_text(autoyast)
    report(profile_file, autoyast_file)

    # Fail closed *before* touching the machine, not after.
    check_arch(doc, {"x86_64"})
    check_raid_consumers(doc)
    check_snapshots(doc, tools={"snapper"}, boot_menu=True)
    check_script_fields(doc)
    check_unread(doc)

    if status := enforce(args.strict):
        return status

    if args.apply:
        import shutil
        import subprocess
        if not shutil.which("agama"):
            sys.exit("error: --apply requested, but 'agama' is not on PATH.\n"
                     "For AutoYaST media, boot the installer with "
                     "`autoyast=<url-to-autoyast.xml>` instead; this applier will not "
                     "partition disks itself.")
        load_file, scratch = keyed_profile(doc, profile_file)
        print(f"applying profile to Agama: {profile_file}")
        try:
            res = subprocess.run(["agama", "config", "load", str(load_file)])
        finally:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
        if res.returncode != 0:
            return res.returncode
        print("starting Agama installation...")
        return subprocess.run(["agama", "install"]).returncode
    return 0


def keyed_profile(doc: dict, profile_file: pathlib.Path):
    """(profile to hand Agama, scratch dir to delete afterwards).

    The generated profile.json holds `@@LIS_CRYPT_<id>@@` where the passphrase
    goes, because delivery.md §6 keeps the secret on the seed and out of any
    file this applier writes. Agama's `luks2.password` takes a value and
    nothing else, so the substitution happens here, into a 0700 scratch
    directory that exists for the length of `agama config load` — the same
    trick the AutoYaST side plays with modified.xml.
    """
    import tempfile
    containers = [c["id"] for c in
                  ((doc.get("storage", {}) or {}).get("encryption") or [])]
    if not containers:
        return profile_file, None
    body = profile_file.read_text()
    for cid in containers:
        key_path = luks_key_path(doc, cid)
        if not key_path:
            # Only reachable under --lenient, which downgraded the refusal
            # check_unsupported() already raised for this container.
            sys.exit(f"error: LUKS container {cid!r} has no key material to "
                     "substitute; --lenient cannot conjure a passphrase.")
        try:
            secret = pathlib.Path(key_path).read_text().rstrip("\n")
        except OSError as err:
            sys.exit(f"error: no key material for LUKS container {cid!r} at "
                     f"{key_path}: {err}\nThe LIS seed must be mounted at "
                     f"{SEED_MOUNT} before --apply; this applier will not install "
                     "an unencrypted disk in place of an encrypted one.")
        # json.dumps of the value, minus its quotes: the placeholder sits
        # inside a JSON string, so a quote or backslash in the passphrase has
        # to be escaped or the profile stops parsing.
        body = body.replace(crypt_placeholder(cid), json.dumps(secret)[1:-1])
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="lis-agama-"))
    keyed = scratch / "profile.json"
    keyed.touch(mode=0o600)
    keyed.write_text(body)
    return keyed, scratch


if __name__ == "__main__":
    sys.exit(main())
