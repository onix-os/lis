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
                        load_doc, refuse, report, role_fs, role_mountpoint, warn)

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


def agama_drives(doc: dict) -> list[dict]:
    storage = doc.get("storage", {}) or {}
    paths = disk_paths(doc)
    consumed = {d for g in storage.get("lvm", []) or [] for d in g.get("devices", [])}
    if storage.get("lvm"):
        warn("storage.lvm: emitted in the AutoYaST profile (is_lvm_vg/lv_name); "
             "the Agama JSON profile carries no volume group")
    drives = []
    for handle, path in paths.items():
        partitions: list[dict] = []
        if storage.get("wipe"):
            partitions.append({"search": "*", "delete": True})
        for i, part in enumerate(storage.get("partitions", [])):
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
            if name in consumed:
                entry["id"] = "lvm"
                partitions.append(entry)
                continue
            if fs in (None, "none"):
                partitions.append(entry)
                continue
            mountpoint = role_mountpoint(part)
            filesystem: dict = {"type": FS_MAP.get(fs, fs)}
            if fs == "swap":
                filesystem["path"] = "swap"
            elif mountpoint:
                filesystem["path"] = mountpoint
            if subs := part.get("subvolumes"):
                if fs != "btrfs":
                    refuse(f"partition '{name}': subvolumes on a {fs} filesystem")
                else:
                    filesystem["type"] = {"btrfs": {
                        "snapshots": bool((storage.get("snapshots", {}) or {}).get("enabled")),
                        "subvolumes": [{"path": s["name"].lstrip("@") or "@",
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
    partitioning = ET.SubElement(profile, "partitioning")
    partitioning.set(f"{{{CONFIG_NS}}}type", "list")
    for handle, path in disk_paths(doc).items():
        drive = ET.SubElement(partitioning, "drive")
        ET.SubElement(drive, "device").text = path
        boolean(drive, "initialize", bool(storage.get("wipe")))
        ET.SubElement(drive, "use").text = "all"
        plist = ET.SubElement(drive, "partitions")
        plist.set(f"{{{CONFIG_NS}}}type", "list")
        for i, part in enumerate(storage.get("partitions", [])):
            if part.get("disk") != handle:
                continue
            role = part.get("role")
            fs = role_fs(part)
            node = ET.SubElement(plist, "partition")
            if size := size_str(part.get("size", "rest"), f"partition '{part.get('id', i)}'"):
                ET.SubElement(node, "size").text = size
            else:
                ET.SubElement(node, "size").text = "max"
            if fs not in (None, "none"):
                filesystem = ET.SubElement(node, "filesystem")
                filesystem.set(f"{{{CONFIG_NS}}}type", "symbol")
                filesystem.text = FS_MAP.get(fs, fs)
            mountpoint = role_mountpoint(part)
            if fs == "swap":
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
            if group := lvm_member.get(part.get("id")):
                ET.SubElement(node, "lvm_group").text = group["name"]
            if container := crypt_over.get(part.get("id")):
                boolean(node, "crypt_fs", True)
                method = ET.SubElement(node, "crypt_method")
                method.set(f"{{{CONFIG_NS}}}type", "symbol")
                method.text = "luks2" if container.get("type") != "luks1" else "luks1"
                ET.SubElement(node, "crypt_key").text = crypt_placeholder(container["id"])
            if subs := part.get("subvolumes"):
                sub_list = ET.SubElement(node, "subvolumes")
                sub_list.set(f"{{{CONFIG_NS}}}type", "list")
                for sub in subs:
                    entry = ET.SubElement(sub_list, "subvolume")
                    ET.SubElement(entry, "path").text = sub["name"].lstrip("@") or "@"

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
        ET.SubElement(drive, "use").text = "all"
        plist = ET.SubElement(drive, "partitions")
        plist.set(f"{{{CONFIG_NS}}}type", "list")
        node = ET.SubElement(plist, "partition")
        target = next((q for q in storage.get("partitions", [])
                       if q.get("id") == array["name"]), {})
        opts = ET.SubElement(node, "raid_options")
        ET.SubElement(opts, "raid_type").text = f"raid{array['level']}"
        # If a volume group consumes the array, the array *is* its physical
        # volume — without lvm_group the group has no PV and AutoYaST stops at
        # "Partitioning issues" naming the group.
        owner = next((g for g in (storage.get("lvm", []) or [])
                      if array["name"] in (g.get("devices") or [])), None)
        if owner:
            ET.SubElement(node, "lvm_group").text = owner["name"]
            pid = ET.SubElement(node, "partition_id")
            pid.set(f"{{{CONFIG_NS}}}type", "integer")
            pid.text = "142"
        if mount := role_mountpoint(target):
            ET.SubElement(node, "mount").text = mount
        if fs := role_fs(target):
            filesystem = ET.SubElement(node, "filesystem")
            filesystem.set(f"{{{CONFIG_NS}}}type", "symbol")
            filesystem.text = FS_MAP.get(fs, fs)

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
        print(f"applying profile to Agama: {profile_file}")
        res = subprocess.run(["agama", "config", "load", str(profile_file)])
        if res.returncode != 0:
            return res.returncode
        print("starting Agama installation...")
        return subprocess.run(["agama", "install"]).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
