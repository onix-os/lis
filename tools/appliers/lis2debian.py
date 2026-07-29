#!/usr/bin/env python3
"""lis2debian — translate a LIS document into a Debian installer preseed.

Usage: lis2debian.py FILE.lis.{json,yaml} [--out DIR] [--lenient] [--apply]

Writes preseed.cfg into DIR (default '.'):
  preseed.cfg — debian-installer (d-i) debconf preseed

`--apply` loads the answers with debconf-set-selections so a running
debian-installer picks them up; d-i still does the installation. This applier
never partitions a disk itself.

Fail-closed by default (SPEC §2.3): core intent d-i cannot express is *refused*
with exit status 1. `--lenient` downgrades refusals to warnings.
"""

import argparse
import base64
import json
import pathlib
import sys

from lis_common import (track, check_unread, check_raid_consumers, chroot_intents, registration_commands, enrollment_commands, luks_key_path, seed_mount_commands, SEED_MOUNT, resolve_disk_paths, check_snapshots, match_selectors, system_commands, security_packages, file_commands, uid_commands, password_field, shell_packages, check_arch, check_script_fields,ALL_SECTIONS, add_common_args, check_firmware,
                        check_unhandled, check_section_fields, sudoers_commands, check_mirror, boot_timeout_commands, driver_packages,
                        check_boot_extras, check_keymap, check_version, enforce,
                        load_doc, refuse, report, role_fs, role_mountpoint, warn)

FS_MAP = {"ext4": "ext4", "xfs": "xfs", "btrfs": "btrfs", "vfat": "fat32", "swap": "linux-swap"}


def shquote(value: str) -> str:
    """Single-quote a fragment of `preseed/late_command`.

    d-i hands the whole late_command to a shell, so a double-quoted argument
    would have its ``$var`` and backticks expanded by *that* shell before the
    inner ``sh -c`` ever sees them. Single quotes are the only safe wrapper.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def b64(text: str) -> str:
    """Payloads reach late_command base64-encoded — no shell escaping to get wrong."""
    return base64.b64encode(text.encode()).decode()


def size_mb(size: str, what: str) -> int:
    if size == "rest":
        return -1
    if size.endswith("%"):
        refuse(f"{what}: percent size {size!r} is not expressible in a partman recipe")
        return -1
    for unit, factor in (("TiB", 1024 * 1024), ("GiB", 1024), ("MiB", 1)):
        if size.endswith(unit):
            return int(size[: -len(unit)]) * factor
    refuse(f"{what}: unparseable size {size!r}")
    return 1024


def recipe_entry(name: str, size: str, fs: str | None, mountpoint: str | None,
                 what: str, *, bootable=False, lvmok=False, vg=None, lv=None,
                 crypto=False, raid=False) -> str:
    """One `partman-auto/expert_recipe` stanza."""
    mb = size_mb(size, what)
    minimum = 128 if mb < 0 else mb
    maximum = "1000000000" if mb < 0 else str(mb)
    priority = 1000 if mb < 0 else max(mb, 128)
    parts = [f"{minimum} {priority} {maximum} {FS_MAP.get(fs, fs) or 'ext4'}"]
    if vg:
        # A crypto partition is the PV: partman opens it and adds
        # /dev/mapper/<part>_crypt to the pool itself.
        method = "crypto" if crypto else "lvm"
        parts.append(f"$defaultignore{{ }} $primary{{ }} method{{ {method} }} "
                     f"vg_name{{ {vg} }}")
        return " ".join(parts) + " ."
    if raid:
        # The documented RAID recipe names `raid` as the filesystem field:
        #   1000 5000 4000 raid $primary{ } method{ raid } .
        parts[0] = f"{minimum} {priority} {maximum} raid"
        parts.append("$primary{ } method{ raid }")
        return " ".join(parts) + " ."
    if lvmok:
        parts.append("$lvmok{ }")
        if lv:
            parts.append(f"lv_name{{ {lv} }}")
    else:
        parts.append("$primary{ }")
    if bootable:
        parts.append("$bootable{ }")
    if fs == "swap":
        parts.append("method{ swap } format{ }")
    elif fs in (None, "none"):
        parts.append("method{ keep }")
    else:
        parts.append("method{ format } format{ } use_filesystem{ } "
                     f"filesystem{{ {FS_MAP.get(fs, fs)} }}")
        if mountpoint:
            parts.append(f"mountpoint{{ {mountpoint} }}")
    return " ".join(parts) + " ."


def part_device(handle, storage, disks):
    """The /dev node a declared partition will occupy, for a RAID recipe."""
    order = {}
    for part in storage.get("partitions", []) or []:
        disk = part.get("disk")
        order[disk] = order.get(disk, 0) + 1
        if part.get("id") == handle:
            return f"{disks.get(disk, '')}{order[disk]}"
    return ""


def consumed_by(handle, lvm_groups, crypt_over):
    """The LVM group this partition feeds, and whether through a LUKS container.

    A document may put the partition into the pool directly, or wrap it in an
    encryption container that the pool then consumes; partman expresses both as
    one recipe entry, differing only in `method{ lvm }` vs `method{ crypto }`.
    """
    for group in lvm_groups:
        devices = group.get("devices", [])
        if handle in devices:
            return group, False
        container = crypt_over.get(handle)
        if container and container["id"] in devices:
            return group, True
    return None


def render_storage(doc: dict, lines: list[str]) -> tuple[list[str], list[str]]:
    """LIS storage → partman-auto directives.

    Returns (late_command fix-ups, fix-ups that must run after every other
    in-target command).
    """
    storage = doc.get("storage", {}) or {}
    target = doc.get("target", {}) or {}
    late: list[str] = []
    late_last: list[str] = []
    if not storage:
        return late, late_last

    raid_arrays = storage.get("raid", []) or []
    if storage.get("encryption"):
        ids = ", ".join(c["id"] for c in storage["encryption"])
        # partman-auto's own recipe parser (lib/partman/lib/auto-shared.sh):
        #     elif echo "$*" | grep -q "method{ crypto }"; then
        #         pv_devices="$pv_devices /dev/mapper/${path##*/}_crypt"
        # so a crypto partition becomes an LVM physical volume — Debian's
        # supported shape is LUKS with LVM on top, not a bare filesystem.
        lvm_devices = {d for g in (storage.get("lvm", []) or [])
                       for d in g.get("devices", [])}
        for container in storage.get("encryption", []) or []:
            if not luks_key_path(doc, container["id"]):
                refuse(f"storage.encryption ({container['id']}): no key material — "
                       "declare a keys[] entry with a seed: source, or place the "
                       f"passphrase at {SEED_MOUNT}/secrets/luks-{container['id']}.key")
            elif container["id"] not in lvm_devices:
                refuse(f"storage.encryption ({container['id']}): partman puts a "
                       "crypto partition into the LVM pool, so the container must "
                       "be consumed by a storage.lvm group — a filesystem directly "
                       "on LUKS is not expressible in an expert recipe")
            for method in container.get("unlock", []) or []:
                if method not in ("passphrase", "keyfile"):
                    warn(f"storage.encryption ({container['id']}): unlock method "
                         f"{method!r} must be enrolled after installation")

    disks = {}
    for disk in target.get("disks", []):
        match_selectors(disk)
        path = (disk.get("match", {}) or {}).get("path")
        if not path:
            refuse(f"disk '{disk['id']}': d-i needs an explicit match.path — partman "
                   "cannot evaluate LIS match rules")
            continue
        disks[disk["id"]] = path
    if len(disks) > 1 and not storage.get("raid") and not storage.get("lvm"):
        # Several disks with nothing spanning them: partman would install to
        # the first and silently leave the rest untouched.
        refuse(f"{len(disks)} disks declared but no storage.raid or storage.lvm "
               "spans them — partman-auto would use only the first")
    if not disks:
        return late, late_last

    lvm_groups = storage.get("lvm", []) or []
    crypt_over = {c["over"]: c for c in (storage.get("encryption", []) or [])}
    raid_members = {d for a in (storage.get("raid", []) or [])
                    for d in a.get("devices", [])}
    consumed = {d for g in lvm_groups for d in g.get("devices", [])}
    # partman-auto/disk takes a space-separated list; an array needs every
    # member disk named, not just the first.
    lines.append("d-i partman-auto/disk string " + " ".join(disks.values()))
    method = "raid" if raid_arrays else ("lvm" if lvm_groups else "regular")
    lines.append(f"d-i partman-auto/method string {method}")
    if raid_arrays:
        # partman-auto applies a single expert_recipe to every disk in
        # partman-auto/disk, so a layout that differs per disk cannot be
        # expressed — it tries to create every partition on each disk and stops
        # with "Failed to partition the selected disk".
        per_disk = {}
        for part in storage.get("partitions", []) or []:
            per_disk.setdefault(part.get("disk"), []).append(
                (part.get("size"), part.get("id") in raid_members))
        shapes = {tuple(v) for v in per_disk.values()}
        if len(shapes) > 1:
            refuse("storage.raid: partman applies one recipe to every disk, so "
                   "each disk must declare the same partitions — put /boot on a "
                   "mirror too, or install without RAID")
        # Format read from partman-auto-raid's own bin/auto-raidcfg:
        #   read raidtype devcount sparecount fstype mountpoint devs sparedevs args
        # recipes separated by '.', device lists by '#'.
        recipes = []
        for array in raid_arrays:
            members = [part_device(d, storage, disks) for d in array.get("devices", [])]
            spares = [part_device(d, storage, disks) for d in array.get("spares", []) or []]
            target = next((p for p in storage.get("partitions", [])
                           if p.get("id") == array["name"]), {})
            fs = role_fs(target) or "lvm"
            mount = role_mountpoint(target) or "-"
            recipes.append(f"{array['level']} {len(members)} {len(spares)} {fs} {mount} "
                           + "#".join(m for m in members if m)
                           + (" " + "#".join(s for s in spares if s) if spares else ""))
        lines.append("d-i partman-auto-raid/recipe string " + " . ".join(recipes) + " .")
        lines.append("d-i mdadm/boot_degraded boolean true")
        # d-i stops for confirmation before it writes an array ("Before RAID
        # can be configured, the changes have to be written to the storage
        # devices"), which in an unattended install is a hang, not a prompt.
        # partman-md/init.d/25md-devices does:
        #     db_register partman-md/confirm partman-md/confirm_nooverwrite
        # so the question actually asked is the registered copy; preseeding
        # only partman-md/confirm leaves the install waiting at the dialog.
        lines.append("d-i partman-md/confirm boolean true")
        lines.append("d-i partman-md/confirm_nooverwrite boolean true")
        lines.append("d-i partman-md/confirm_nochanges boolean true")
        lines.append("d-i partman-md/device_remove_md boolean true")
    if lvm_groups:
        lines.append("d-i partman-auto-lvm/new_vg_name string " + lvm_groups[0]["name"])
        lines.append("d-i partman-lvm/device_remove_lvm boolean true")
        lines.append("d-i partman-lvm/confirm boolean true")
        lines.append("d-i partman-lvm/confirm_nooverwrite boolean true")
    if not storage.get("wipe"):
        refuse("storage.wipe: false — partman-auto always rewrites the target disk")

    entries: list[str] = []
    for i, part in enumerate(storage.get("partitions", [])):
        if part.get("disk") not in disks:
            refuse(f"partition {i}: references unknown disk handle {part.get('disk')!r}")
            continue
        if part.get("existing"):
            refuse(f"partition {i}: adopting an existing partition is not expressible "
                   "in a partman-auto recipe")
            continue
        handle = part.get("id") or f"auto-{i}"
        role = part.get("role")
        fs = role_fs(part)
        mountpoint = role_mountpoint(part)
        if handle in raid_members:
            entries.append(recipe_entry(handle, part.get("size", "rest"), None, None,
                                        f"partition '{handle}'", raid=True))
            continue
        owner = consumed_by(handle, lvm_groups, crypt_over)
        if owner:
            group, is_crypto = owner
            entries.append(recipe_entry(handle, part.get("size", "rest"), None, None,
                                        f"partition '{handle}'", vg=group["name"],
                                        crypto=is_crypto))
            continue
        entries.append(recipe_entry(handle, part.get("size", "rest"), fs, mountpoint,
                                    f"partition '{handle}'",
                                    bootable=role in ("esp", "boot")))
        if subs := part.get("subvolumes"):
            early, final = btrfs_subvolume_commands(mountpoint, fs, subs,
                                                    f"partition '{handle}'")
            late += early
            late_last += final

    for group in lvm_groups:
        for vol in group.get("volumes", []):
            entries.append(recipe_entry(vol["name"], vol.get("size", "rest"), vol.get("fs"),
                                        vol.get("mountpoint"),
                                        f"lvm '{group['name']}' volume '{vol['name']}'",
                                        lvmok=True, lv=vol["name"]))
            if subs := vol.get("subvolumes"):
                early, final = btrfs_subvolume_commands(
                    vol.get("mountpoint"), vol.get("fs"), subs,
                    f"lvm volume '{vol['name']}'")
                late += early
                late_last += final

    if entries:
        lines.append("d-i partman-auto/expert_recipe string lis :: "
                     + " ".join(entries))
        lines.append("d-i partman-auto/choose_recipe select lis")
    lines += [
        "d-i partman-partitioning/confirm_write_new_label boolean true",
        "d-i partman/choose_partition select finish",
        "d-i partman/confirm boolean true",
        "d-i partman/confirm_nooverwrite boolean true",
    ]
    if (storage.get("snapshots", {}) or {}).get("enabled"):
        pass   # honored by chroot_intents()
    if swapfile := (storage.get("swap", {}) or {}).get("file"):
        late.append(f"in-target fallocate -l {swapfile['size'].replace('iB', '')} "
                    f"{swapfile['path']}")
        late.append(f"in-target chmod 600 {swapfile['path']}")
        late.append(f"in-target mkswap {swapfile['path']}")
        late.append("in-target sh -c "
                    + shquote(f"echo '{swapfile['path']} none swap sw 0 0' >> /etc/fstab"))
    if (storage.get("swap", {}) or {}).get("zram"):
        warn("storage.swap.zram honored by installing zram-tools")
    return late, late_last


def subvol_script(root: dict, subvolumes: list[dict]) -> str:
    """The conversion, as a single line.

    partman already installs Debian into a btrfs subvolume of its own —
    `@rootfs` — so unlike Ubuntu the root does not need relocating, only
    renaming to whatever the document calls it. Nested subvolumes are carved at
    the filesystem top level and their content lifted across.

    Three constraints shape the shape of this:

    * A preseed value cannot span lines, hence the `;` joins and the absence of
      `#` comments, which would swallow the rest of the command.
    * d-i's busybox has neither btrfs-progs nor a `mount` that understands
      btrfs options, so the work runs through `in-target` against the installed
      system's own tools.
    * Every `in-target` call gets its own mount namespace, so a mount made by
      one is gone by the next. The whole conversion therefore happens inside a
      *single* invocation, and avoids single quotes so it can survive being
      nested in the preseed's own quoting.
    """
    nested = [sub for sub in subvolumes if sub is not root]
    top = "/mnt/lis-top"
    name = root["name"]

    # Runs in the chroot. $dev/$uuid/$current are interpolated by the outer
    # shell before in-target starts, so nothing here needs to survive quoting.
    inner = [f"mkdir -p {top}",
             f'mount -o subvolid=5 "$dev" {top}']
    inner += [f"btrfs subvolume create {top}/{sub['name']}" for sub in nested]
    inner.append(f'if [ "$current" != {name} ]; then mv {top}/"$current" {top}/{name}; fi')
    for sub in nested:
        src = f"{top}/{name}{sub['mountpoint']}"
        inner.append(f"if [ -d {src} ]; then find {src} -mindepth 1 -maxdepth 1 "
                     f"-exec mv {{}} {top}/{sub['name']}/ \\; ; fi")
    inner += [
        # Make the root subvolume the filesystem default so a plain mount — and
        # therefore the bootloader's root=UUID= — lands inside it.
        f"btrfs subvolume set-default {top}/{name}",
        f'sed -i s/subvol="$current"/subvol={name}/ {top}/{name}/etc/fstab',
    ]
    for sub in nested:
        options = ",".join([f"subvol={sub['name']}"] + list(sub.get("mount_options", [])))
        # Single quotes for the format: this sits inside the double-quoted
        # in-target argument, and a nested double quote would close it and let
        # the outer shell strip the backslashes out of \t and \n.
        inner.append(f"printf 'UUID=%s\\t%s\\tbtrfs\\t%s\\t0\\t0\\n' "
                     f'"$uuid" {sub["mountpoint"]} {options} '
                     f">> {top}/{name}/etc/fstab")
    inner.append(f"umount {top}")

    steps = [
        "set -eu",
        "trace=$(mktemp)",
        'trap \'rc=$?; [ "$rc" = 0 ] || { echo "LIS: btrfs subvolume setup failed'
        ' (exit $rc)"; cat "$trace"; } > /dev/console 2>&1\' EXIT',
        'exec >"$trace" 2>&1',
        "set -x",
        "base=/target",
        """dev=$(awk -v b="$base" '$2==b {print $1}' /proc/mounts | head -n1)""",
        'uuid=""',
        'for l in /dev/disk/by-uuid/*; do [ "$(readlink -f "$l")" = "$dev" ]'
        " && uuid=${l##*/} && break; done",
        # Whatever partman called its root subvolume is recorded in the fstab it
        # generated, so read it back rather than assuming the name.
        """current=$(awk '$2=="/" {print $4}' "$base/etc/fstab" | tr ',' '\\n'"""
        """ | sed -n 's/^subvol=//p' | head -n1)""",
        'in-target sh -c "set -eux; ' + "; ".join(inner) + '"',
        "in-target update-grub",
    ]
    return "; ".join(steps)


def btrfs_subvolume_commands(mountpoint, fs, subvolumes, what) -> tuple[list[str], list[str]]:
    """Translate btrfs subvolumes into a preseed late_command conversion.

    partman has no subvolume vocabulary, so the subvolumes are carved after it
    finishes: content is *moved* in — nothing is discarded — and fstab gains the
    matching `subvol=` entries.
    """
    if fs != "btrfs":
        refuse(f"{what}: subvolumes declared on a {fs} filesystem")
        return [], []
    if mountpoint != "/":
        refuse(f"{what}: subvolumes are only translated for the root filesystem; "
               f"this one is mounted at {mountpoint!r}")
        return [], []
    root = next((s for s in subvolumes if s["mountpoint"] == mountpoint), None)
    if not root:
        refuse(f"{what}: partman installs into the top-level subvolume, so the "
               "layout needs a subvolume claiming '/' for the others to hang off")
        return [], []
    return [], ["sh -c " + shquote(subvol_script(root, subvolumes))]


def render_preseed(doc: dict) -> str:
    system = doc.get("system", {}) or {}
    boot = doc.get("boot", {}) or {}
    software = doc.get("software", {}) or {}
    network = doc.get("network", {}) or {}
    desktop = doc.get("desktop", {}) or {}
    scripts = doc.get("scripts", {}) or {}
    installer = doc.get("installer", {}) or {}
    storage = doc.get("storage", {}) or {}
    users = doc.get("users", []) or []

    lines: list[str] = ["# Generated by lis2debian (Linux Installation Specification)", ""]

    lines.append(f"d-i debian-installer/locale string {system.get('locale', 'en_US.UTF-8')}")
    km = system.get("keymap", {}) or {}
    console = km.get("console")
    layout = km.get("layout") or console or "us"
    if console and km.get("layout") and console != km["layout"]:
        warn(f"system.keymap.console {console!r} is not applied — the preseed "
             f"takes one xkb layout, and layout {km['layout']!r} was declared")
    lines.append(f"d-i keyboard-configuration/xkb-keymap select {layout}")
    if km.get("variant"):
        lines.append("d-i keyboard-configuration/variant select " + km["variant"])
    lines.append("d-i clock-setup/utc boolean "
                 + ("false" if system.get("hwclock") == "localtime" else "true"))
    lines.append(f"d-i time/zone string {system.get('timezone', 'UTC')}")
    time_cfg = system.get("time", {}) or {}
    lines.append(f"d-i clock-setup/ntp boolean {str(time_cfg.get('ntp', True)).lower()}")
    if servers := time_cfg.get("servers"):
        lines.append(f"d-i clock-setup/ntp-server string {servers[0]}")

    if host := system.get("hostname"):
        lines.append(f"d-i netcfg/get_hostname string {host}")
        lines.append("d-i netcfg/hostname string " + host)
    if domain := system.get("domain"):
        lines.append(f"d-i netcfg/get_domain string {domain}")
    if network.get("wifi"):
        refuse("network.wifi is not expressible in a preseed")
    if manager := network.get("manager"):
        if manager not in ("auto", "networkmanager"):
            pass   # honored by chroot_intents()
    if network.get("interfaces"):
        refuse("network.interfaces: static addressing needs netcfg/get_ipaddress and "
               "friends per interface; this applier emits DHCP only")
    if network.get("firewall"):
        pass   # honored by chroot_intents()

    root = next((u for u in users if u["name"] == "root"), None)
    if root and (h := (root.get("password") or {}).get("hash")):
        lines.append("d-i passwd/root-login boolean true")
        lines.append(f"d-i passwd/root-password-crypted password {h}")
    else:
        lines.append("d-i passwd/root-login boolean false")

    normal = [u for u in users if u["name"] != "root"]
    if normal:
        primary = normal[0]
        lines.append("d-i passwd/make-user boolean true")
        lines.append(f"d-i passwd/user-fullname string "
                     f"{primary.get('comment') or primary['name']}")
        lines.append(f"d-i passwd/username string {primary['name']}")
        password = primary.get("password") or {}
        if field := password_field(primary):
            lines.append(f"d-i passwd/user-password-crypted password {field}")
        else:
            refuse(f"user '{primary['name']}': no password hash and not marked locked")
        groups = list(primary.get("groups", []))
        if primary.get("admin") and "sudo" not in groups:
            groups.append("sudo")
        if groups:
            lines.append(f"d-i passwd/user-default-groups string {' '.join(groups)}")
    else:
        lines.append("d-i passwd/make-user boolean false")

    late, late_last = render_storage(doc, lines)

    if hostname := system.get("hostname"):
        # netcfg asks DHCP first and a lease that carries a host-name wins over
        # the preseeded answer, so the document's hostname is written directly.
        late.append("in-target sh -c " + shquote(
            f"echo {hostname} > /etc/hostname; hostname {hostname}; "
            f"sed -i \"s/^127.0.1.1.*/127.0.1.1\\t{hostname}/\" /etc/hosts || "
            f"echo '127.0.1.1\t{hostname}' >> /etc/hosts"))

    if loader := boot.get("loader"):
        if loader not in ("auto", "grub"):
            refuse(f"boot.loader {loader!r}: d-i installs GRUB")
    lines.append("d-i grub-installer/only_debian boolean true")
    lines.append(f"d-i grub-installer/with_other_os boolean "
                 f"{str(bool(boot.get('os_prober', True))).lower()}")
    disks = [(d.get("match", {}) or {}).get("path")
             for d in (doc.get("target", {}) or {}).get("disks", [])]
    if disks and disks[0]:
        lines.append(f"d-i grub-installer/bootdev string {disks[0]}")
    if params := (boot.get("kernel", {}) or {}).get("params"):
        lines.append(f"d-i debian-installer/add-kernel-opts string {' '.join(params)}")
    variant = (boot.get("kernel", {}) or {}).get("variant", "default")
    if variant not in ("default", None):
        refuse(f"boot.kernel.variant {variant!r} has no Debian kernel package mapping")

    pkgs = list(software.get("packages", []))
    driver_pkgs = driver_packages(doc, "debian")
    pkgs += shell_packages(doc)
    pkgs += security_packages(doc, "debian")
    if driver_pkgs:
        # intel-microcode, firmware-linux and the graphics firmware all sit in
        # contrib/non-free; without these the packages simply do not resolve.
        lines += ["d-i apt-setup/contrib boolean true",
                  "d-i apt-setup/non-free boolean true",
                  "d-i apt-setup/non-free-firmware boolean true"]
    pkgs += driver_pkgs
    flatpaks = list(software.get("flatpak", []))
    for app in software.get("apps", []):
        if isinstance(app, str):
            pkgs.append(app)
        elif isinstance(app, dict):
            if name := (app.get("package") or app.get("name")):
                pkgs.append(name)
            if fp := app.get("flatpak"):
                flatpaks.append(fp)
    if flatpaks:
        pkgs.append("flatpak")
    if (storage.get("swap", {}) or {}).get("zram"):
        pkgs.append("zram-tools")
    if desktop.get("printing"):
        pkgs.append("cups")
    if desktop.get("bluetooth"):
        pkgs.append("bluez")

    role = software.get("role", "")
    tasks = {"desktop:gnome": "gnome-desktop", "desktop:kde": "kde-desktop",
             "desktop:xfce": "xfce-desktop", "desktop:mate": "mate-desktop",
             "desktop:cinnamon": "cinnamon-desktop", "server": "ssh-server",
             "minimal": "", "": ""}
    if role in tasks:
        selected = ["standard"] + ([tasks[role]] if tasks[role] else [])
        lines.append(f"tasksel tasksel/first multiselect {', '.join(selected)}")
    else:
        refuse(f"software.role {role!r} has no Debian task")
    if pkgs:
        lines.append(f"d-i pkgsel/include string {' '.join(pkgs)}")
    if software.get("exclude"):
        pass   # honored by chroot_intents()
    if software.get("snap"):
        refuse("software.snap is not available on Debian")
    if telemetry := system.get("telemetry"):
        lines.append("popularity-contest popularity-contest/participate boolean "
                     + ("true" if telemetry == "on" else "false"))

    services = software.get("services", {}) or {}
    for unit in services.get("enable", []) or []:
        late.append(f"in-target systemctl enable {unit}")
    for unit in services.get("disable", []) or []:
        late.append(f"in-target systemctl disable {unit}")
    ssh = network.get("ssh", {}) or {}
    if ssh.get("enabled"):
        late.append("in-target systemctl enable ssh")
        if "password_auth" in ssh:
            value = "yes" if ssh["password_auth"] else "no"
            late.append("in-target sh -c " + shquote(
                f"echo 'PasswordAuthentication {value}' >> /etc/ssh/sshd_config"))
        if permit := ssh.get("permit_root"):
            late.append("in-target sh -c " + shquote(
                f"echo 'PermitRootLogin {permit}' >> /etc/ssh/sshd_config"))

    early = [s["content"] for stage in ("pre_install", "pre")
             for s in scripts.get(stage, []) if s.get("content")]
    # partman-crypto asks for the passphrase interactively; early_command runs
    # before partitioning, so the answer is pre-seeded into debconf from the
    # seed volume and never appears in this file (delivery.md §6).
    crypt_keys = [luks_key_path(doc, c["id"])
                  for c in (storage.get("encryption", []) or [])]
    crypt_keys = [k for k in crypt_keys if k]
    if crypt_keys:
        inject = seed_mount_commands() + [
            f'pass=$(cat {crypt_keys[0]})',
            'echo "partman-crypto partman-crypto/passphrase password $pass" '
            '| debconf-set-selections',
            'echo "partman-crypto partman-crypto/passphrase-again password $pass" '
            '| debconf-set-selections',
        ]
        early.insert(0, "; ".join(inject))
    if early:
        lines.append(f"d-i preseed/early_command string {'; '.join(early)}")

    for cmd in sudoers_commands(doc):
        late.append(f"in-target sh -c {shquote(cmd)}")
    for cmd in uid_commands(doc):
        late.append(f"in-target sh -c {shquote(cmd)}")
    for cmd in enrollment_commands(doc):
        late.append(f"in-target sh -c {shquote(cmd)}")
    for cmd in registration_commands(doc, "debian"):
        late.append(f"in-target sh -c {shquote(cmd)}")
    for cmd in chroot_intents(doc, "debian"):
        late.append(f"in-target sh -c {shquote(cmd)}")
    for cmd in system_commands(doc, "debian"):
        late.append(f"in-target sh -c {shquote(cmd)}")
    for cmd in boot_timeout_commands(doc, "debian", (doc.get("boot") or {}).get("loader", "grub")):
        late.append(f"in-target sh -c {shquote(cmd)}")

    for stage in ("post_storage", "post_install", "post", "pre_reboot", "on_success"):
        for s in scripts.get(stage, []):
            if c := s.get("content"):
                late.append(f"in-target sh -c {shquote(c)}")
    for user in users:
        for s in (user.get("scripts", {}) or {}).get("post_install", []):
            if c := s.get("content"):
                late.append(f"in-target su - {user['name']} -c {shquote(c)}")
        for key in user.get("ssh_authorized_keys", []) or []:
            late.append("in-target sh -c " + shquote(
                f"install -d -m700 -o {user['name']} /home/{user['name']}/.ssh && "
                f"echo {shquote(key)} >> /home/{user['name']}/.ssh/authorized_keys"))
        if user.get("dotfiles"):
            pass   # honored by chroot_intents()
    for user in normal[1:]:
        password = user.get("password") or {}
        field = password_field(user)
        if not field:
            refuse(f"user '{user['name']}': no password hash and not marked locked")
        groups = ",".join(user.get("groups", []) + (["sudo"] if user.get("admin") else []))
        late.append("in-target useradd -m -p " + shquote(field or "!")
                    + (f" -u {user['uid']}" if user.get("uid") is not None else "")
                    + (f" -G {groups}" if groups else "")
                    + (f" -s {user['shell']}" if user.get("shell", "").startswith("/") else "")
                    + f" {user['name']}")

    for user in users:
        # d-i creates the first account from its own questions, which have no
        # vocabulary for a login shell.
        shell = user.get("shell")
        if not shell:
            continue
        if shell.startswith("/"):
            late.append(f"in-target chsh -s {shquote(shell)} {user['name']}")
        else:
            # An intent name obliges the applier to install the shell too.
            late.append(f"in-target apt-get install -y {shell}")
            late.append(f"in-target sh -c "
                        + shquote(f"chsh -s $(command -v {shell}) {user['name']}"))

    for entry in doc.get("files", []) or []:
        for cmd in file_commands(entry):
            late.append("in-target sh -c " + shquote(cmd))

    firstboot = [s["content"] for s in scripts.get("firstboot", []) if s.get("content")]
    for user in users:
        for s in (user.get("scripts", {}) or {}).get("firstboot", []):
            if c := s.get("content"):
                firstboot.append(f"su - {user['name']} -c {shquote(c)}")
    for app in flatpaks:
        firstboot.append(f"flatpak install -y --noninteractive flathub {app}")
    if firstboot:
        script = ("#!/bin/sh\n" + "\n".join(firstboot)
                  + "\ntouch /var/lib/lis/.firstboot-done\n")
        unit = ("[Unit]\nDescription=LIS first boot\nAfter=multi-user.target\n"
                "ConditionPathExists=!/var/lib/lis/.firstboot-done\n\n"
                "[Service]\nType=oneshot\nExecStart=/usr/libexec/lis-firstboot\n\n"
                "[Install]\nWantedBy=multi-user.target\n")
        late.append("in-target sh -c " + shquote(
            f"install -d /usr/libexec && echo {b64(script)} | base64 -d "
            "> /usr/libexec/lis-firstboot && chmod 755 /usr/libexec/lis-firstboot"))
        late.append("in-target sh -c " + shquote(
            f"echo {b64(unit)} | base64 -d > /etc/systemd/system/lis-firstboot.service"))
        late.append("in-target systemctl enable lis-firstboot.service")

    # Birth certificate (delivery.md §8).
    birth = b64(json.dumps(doc, separators=(",", ":")))
    late.append("in-target sh -c " + shquote(
        f"install -d -m755 /var/lib/lis && echo {birth} | base64 -d "
        "> /var/lib/lis/system.lis.json && chmod 600 /var/lib/lis/system.lis.json"))

    late += late_last
    if late:
        lines.append(f"d-i preseed/late_command string {'; '.join(late)}")

    if scripts.get("on_error"):
        refuse("scripts.on_error has no preseed equivalent")
    if desktop.get("autologin"):
        refuse("desktop.autologin is not expressible in a preseed")
    if desktop.get("display_manager") not in (None, "auto"):
        pass   # installed and enabled by chroot_intents()
    if country := (doc.get("mirror", {}) or {}).get("country"):
        lines.append(f"d-i mirror/country string {country}")
    if mirror := (doc.get("mirror", {}) or {}).get("url"):
        host = mirror.split("://", 1)[-1].split("/", 1)
        lines.append(f"d-i mirror/http/hostname string {host[0]}")
        lines.append(f"d-i mirror/http/directory string /{host[1] if len(host) > 1 else ''}")
    if proxy := (doc.get("proxy", {}) or {}).get("http"):
        lines.append(f"d-i mirror/http/proxy string {proxy}")

    fin = installer.get("on_finish", "reboot")
    if fin == "reboot":
        lines.append("d-i finish-install/reboot_in_progress note")
    elif fin == "poweroff":
        lines.append("d-i debian-installer/exit/poweroff boolean true")
    else:
        refuse(f"installer.on_finish {fin!r} has no preseed equivalent")

    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Translate a LIS document into a debian-installer preseed.")
    add_common_args(ap)
    ap.add_argument("--apply", "-a", action="store_true",
                    help="load the answers into a running debian-installer's debconf")
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
    check_boot_extras(doc, {"kernel", "loader", "params", "timeout", "variant"})
    check_mirror(doc, {"url", "country"})
    check_section_fields(doc, "desktop", {"autologin", "bluetooth", "display_manager", "printing"})
    check_section_fields(doc, "installer", {"on_finish"})
    check_keymap(doc, {"console", "layout", "variant"})

    cfg = render_preseed(doc)
    # d-i rejects the whole file if any value spans a line ("failed to process
    # the preconfiguration file"), which costs a 40-minute install to discover.
    for number, line in enumerate(cfg.splitlines(), 1):
        if line and not line.startswith(("d-i ", "#", "popularity-contest", "tasksel")):
            refuse(f"generated preseed line {number} is not a preseed directive — "
                   "a value with an embedded newline would be rejected by d-i: "
                   f"{line[:60]!r}")
    args.out.mkdir(parents=True, exist_ok=True)
    cfg_file = args.out / "preseed.cfg"
    cfg_file.write_text(cfg)
    report(cfg_file)

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
        if not shutil.which("debconf-set-selections"):
            sys.exit("error: --apply requested, but 'debconf-set-selections' is not on "
                     "PATH.\nThe supported path is to boot the Debian installer with "
                     "`auto=true priority=critical url=<preseed>` on the kernel command "
                     "line; d-i then runs its own native install. This applier will not "
                     "partition disks itself.")
        print(f"loading preseed answers from {cfg_file}")
        return subprocess.run(["debconf-set-selections", str(cfg_file)]).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
