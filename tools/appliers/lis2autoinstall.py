#!/usr/bin/env python3
"""lis2autoinstall — translate a LIS document into Ubuntu autoinstall seed files.

Usage: lis2autoinstall.py FILE.lis.{json,yaml} [--out DIR] [--lenient] [--apply]

Writes a cloud-init NoCloud seed into DIR (default '.'):
  user-data   — `#cloud-config` with the `autoinstall:` section (subiquity)
  meta-data   — minimal NoCloud metadata

Put both files on a volume labeled CIDATA (or build one with
`cloud-localds seed.img user-data meta-data`) and boot the Ubuntu Server
installer with `autoinstall` on the kernel command line. Subiquity then runs
its own native pipeline — this applier never partitions a disk itself.

Fail-closed by default (SPEC §2.3): core intent that autoinstall cannot express
is *refused* with exit status 1. `--lenient` downgrades refusals to warnings.
"""

import argparse
import base64
import json
import pathlib
import re
import sys

from lis_common import (track, check_unread, check_arch, check_script_fields,ALL_SECTIONS, add_common_args, check_firmware,
                        check_unhandled, check_section_fields, sudoers_commands, check_mirror, boot_timeout_commands, driver_packages,
                        check_boot_extras, check_keymap, check_version, enforce,
                        load_doc, refuse, report, role_fs, role_mountpoint, secret_ref, warn)

# Where the installer mounts the LIS seed volume; `seed:` secret references resolve here.
SEED_MOUNT = "/run/lis/seed"


def in_target(script: str) -> list[str]:
    """A late-command that runs inside the installed system.

    Emitted as argv, not a shell string: curtin/subiquity accept either, and a
    list removes an entire layer of nested quoting from generated scripts.
    """
    return ["curtin", "in-target", "--", "sh", "-c", script]


def size_bytes(size: str, what: str) -> int:
    """LIS size → curtin size in bytes (-1 means 'all remaining space')."""
    if size == "rest":
        return -1
    if size.endswith("%"):
        refuse(f"{what}: percent size {size!r} cannot be expressed in curtin "
               "(only absolute sizes and one trailing 'rest' partition)")
        return -1
    units = {"MiB": 1 << 20, "GiB": 1 << 30, "TiB": 1 << 40}
    for unit, factor in units.items():
        if size.endswith(unit):
            return int(size[: -len(unit)]) * factor
    raise ValueError(f"unparseable size: {size}")


# ── storage ──────────────────────────────────────────────────────

class StorageBuilder:
    """Builds a curtin action list, resolving LIS handles across the stack.

    LIS layers (partition → dm_crypt → raid → lvm) reference each other by
    handle; curtin references by action id. `self.handles` is the bridge.
    """

    def __init__(self, doc: dict):
        self.doc = doc
        self.storage = doc.get("storage", {}) or {}
        self.target = doc.get("target", {}) or {}
        self.firmware = self.target.get("firmware", "uefi")
        self.actions: list[dict] = []
        self.handles: dict[str, str] = {}     # LIS handle → curtin action id
        self.fs_specs: list[dict] = []        # deferred format/mount work
        self.late: list[list[str]] = []        # storage fix-ups for late-commands
        self.late_last: list[list[str]] = []   # fix-ups that must run after everything
        self.disk_paths: dict[str, str] = {}
        self.keys = {k["id"]: k for k in doc.get("keys", []) or []}

    # ── helpers ──
    def consumed_handles(self) -> set[str]:
        """Handles owned by a higher layer, so they must not be formatted directly."""
        used: set[str] = set()
        for c in self.storage.get("encryption", []) or []:
            if c.get("over"):
                used.add(c["over"])
        for group in self.storage.get("lvm", []) or []:
            used.update(group.get("devices", []))
        for array in self.storage.get("raid", []) or []:
            used.update(array.get("devices", []))
            used.update(array.get("spares", []))
        return used

    def defer_fs(self, volume_id: str, spec: dict, what: str) -> None:
        fs = spec.get("fs")
        if fs in (None, "none"):
            return
        if fs == "zfs":
            refuse(f"{what}: fs 'zfs' is not supported by curtin/subiquity")
            return
        self.fs_specs.append({
            "volume": volume_id,
            "fs": fs,
            "mountpoint": spec.get("mountpoint"),
            "mount_options": spec.get("mount_options", []),
            "subvolumes": spec.get("subvolumes", []),
            "label": spec.get("label"),
            "what": what,
        })

    # ── layers ──
    def disk_match(self, handle: str, match: dict) -> dict | None:
        """LIS disk match rules → subiquity's `match` directive on a disk action."""
        out: dict = {}
        for field in ("serial", "model", "wwn"):
            if value := match.get(field):
                out[field] = value
        if kind := match.get("type"):
            if kind == "ssd":
                out["ssd"] = True
            elif kind == "hdd":
                out["ssd"] = False
            else:  # nvme — subiquity has no bus predicate, match the device path glob
                out["path"] = "/dev/nvme*"
        if match.get("largest"):
            out["size"] = "largest"
        if match.get("smallest"):
            out["size"] = "smallest"
        for field in ("min_size", "max_size"):
            if match.get(field):
                refuse(f"disk '{handle}': match.{field} has no subiquity equivalent "
                       "(only largest/smallest ordering is expressible)")
        return out or None

    def build_disks(self) -> None:
        wipe = bool(self.storage.get("wipe", False))
        for disk in self.target.get("disks", []):
            handle = disk["id"]
            match = disk.get("match", {}) or {}
            path = match.get("path")
            action_id = f"disk-{handle}"
            self.handles[handle] = action_id
            action = {
                "type": "disk",
                "id": action_id,
                "ptable": "gpt",
                "wipe": "superblock-recursive" if wipe else "preserve",
                "preserve": not wipe,
            }
            if path:
                self.disk_paths[handle] = path
                action["path"] = path
            elif matcher := self.disk_match(handle, match):
                action["match"] = matcher
            else:
                refuse(f"disk '{handle}': match rules {sorted(match)} cannot be "
                       "expressed as a subiquity disk matcher")
                del self.handles[handle]
                continue
            # BIOS boot needs grub in the MBR gap plus a bios_grub partition on GPT.
            if self.firmware == "bios":
                action["grub_device"] = True
            self.actions.append(action)
            if self.firmware == "bios":
                self.actions.append({
                    "type": "partition", "id": f"{action_id}-biosgrub",
                    "device": action_id, "size": 1 << 20,
                    "flag": "bios_grub", "wipe": "superblock", "preserve": False,
                })

    def build_partitions(self) -> None:
        consumed = self.consumed_handles()
        for i, part in enumerate(self.storage.get("partitions", [])):
            disk_handle = part.get("disk")
            disk_action = self.handles.get(disk_handle)
            if not disk_action:
                if disk_handle not in self.disk_paths:
                    refuse(f"partition {i}: references unknown disk handle {disk_handle!r}")
                continue
            if part.get("existing"):
                refuse(f"partition {i} on disk '{disk_handle}': adopting an existing "
                       "partition ('existing') is not expressible in autoinstall")
                continue
            role = part.get("role")
            handle = part.get("id") or f"auto-{i}"
            action_id = f"part-{handle}"
            self.handles[handle] = action_id
            action = {
                "type": "partition",
                "id": action_id,
                "device": disk_action,
                "size": size_bytes(part.get("size", "rest"), f"partition '{handle}'"),
                "wipe": "superblock",
                "preserve": False,
            }
            if role == "esp":
                action["flag"] = "boot"
                action["grub_device"] = self.firmware != "bios"
            elif role == "swap":
                action["flag"] = "swap"
            self.actions.append(action)

            if handle in consumed:
                continue  # a crypt/raid/lvm layer owns this device
            spec = part.copy()
            spec["fs"] = role_fs(part)
            spec["mountpoint"] = role_mountpoint(part)
            self.defer_fs(action_id, spec, f"partition '{handle}'")

    def build_raid(self) -> None:
        consumed = self.consumed_handles()
        for array in self.storage.get("raid", []) or []:
            name = array["name"]
            devices, missing = self.resolve(array.get("devices", []))
            spares, missing_spares = self.resolve(array.get("spares", []) or [])
            for dev in missing + missing_spares:
                refuse(f"raid '{name}': device handle {dev!r} does not resolve to a volume")
            if not devices:
                continue
            raid_id = f"raid-{name}"
            self.actions.append({
                "type": "raid", "id": raid_id, "name": name,
                "raidlevel": array["level"], "devices": devices,
                "spare_devices": spares, "preserve": False,
            })
            self.handles[name] = raid_id
            if name not in consumed:
                self.defer_fs(raid_id, array, f"raid '{name}'")

    def build_encryption(self) -> None:
        consumed = self.consumed_handles()
        parts_by_handle = {p.get("id"): p for p in self.storage.get("partitions", [])
                           if p.get("id")}
        for container in self.storage.get("encryption", []) or []:
            cid = container["id"]
            over = container["over"]
            volume = self.handles.get(over)
            if not volume:
                refuse(f"encryption '{cid}': device {over!r} does not resolve to a volume")
                continue
            if container.get("type") == "luks1":
                warn(f"encryption '{cid}': curtin creates LUKS2; luks1 was requested")
            crypt_id = f"crypt-{cid}"
            action = {
                "type": "dm_crypt",
                "id": crypt_id,
                "volume": volume,
                "dm_name": cid,
                "preserve": False,
            }
            keyfile = self.luks_keyfile(container)
            if keyfile:
                # Secrets stay on the seed; only the *path* enters the document (SPEC §2.4).
                action["keyfile"] = keyfile
            else:
                refuse(f"encryption '{cid}': no key material available — declare a "
                       f"keys[] entry with a seed: source, or place the passphrase at "
                       f"{SEED_MOUNT}/secrets/luks-{cid}.key; curtin cannot prompt "
                       "during an unattended autoinstall")
            self.actions.append(action)
            self.handles[cid] = crypt_id

            self.enroll_unlock(container)
            if cid in consumed:
                continue
            # The container has no fs of its own in LIS; it inherits the covered
            # partition's filesystem intent.
            covered = parts_by_handle.get(over, {})
            spec = dict(covered)
            spec["fs"] = covered.get("fs") or ("btrfs" if covered.get("role") == "root" else None)
            spec["mountpoint"] = covered.get("mountpoint") or (
                "/" if covered.get("role") == "root" else None)
            if spec["fs"] in (None, "none"):
                refuse(f"encryption '{cid}': nothing consumes the container and the "
                       f"covered volume '{over}' declares no filesystem — the LUKS "
                       "device would be created and then left unused")
                continue
            self.defer_fs(crypt_id, spec, f"encryption '{cid}'")

    def luks_keyfile(self, container: dict) -> str | None:
        """Resolve LUKS key material to a path on the live system, never a literal."""
        cid = container["id"]
        key = container.get("key", {}) or {}
        if kf := key.get("keyfile"):
            return kf if kf.startswith("/") else f"{SEED_MOUNT}/{kf.lstrip('/')}"
        for entry in self.doc.get("keys", []) or []:
            purposes = entry.get("purpose", []) or []
            if "disk_encryption" not in purposes:
                continue
            if entry.get("type") in ("keyfile", "gpg", "age"):
                if path := secret_ref(entry.get("source")):
                    return path
        if key.get("passphrase"):
            # `passphrase: true` declares *how* it unlocks, not the secret itself.
            return f"{SEED_MOUNT}/secrets/luks-{cid}.key"
        return None

    def enroll_unlock(self, container: dict) -> None:
        """`unlock` methods beyond passphrase/keyfile need systemd-cryptenroll in-target."""
        cid = container["id"]
        for method in container.get("unlock", []) or []:
            if method in ("passphrase", "keyfile"):
                continue
            if method == "tpm2":
                self.late.append(in_target(
                    f"systemd-cryptenroll --tpm2-device=auto "
                    f"$(cryptsetup status {cid} | awk '/device:/{{print $2}}')"))
                warn(f"encryption '{cid}': tpm2 unlock enrolled via late-command "
                     "systemd-cryptenroll (not a native autoinstall concept)")
            else:
                refuse(f"encryption '{cid}': unlock method {method!r} requires the token "
                       "to be present during an interactive enrollment step; "
                       "autoinstall cannot perform it unattended")

    def build_lvm(self) -> None:
        consumed = self.consumed_handles()
        for group in self.storage.get("lvm", []) or []:
            name = group["name"]
            devices, missing = self.resolve(group.get("devices", []))
            for dev in missing:
                refuse(f"lvm '{name}': device handle {dev!r} does not resolve to a volume")
            if not devices:
                continue
            vg_id = f"vg-{name}"
            self.actions.append({"type": "lvm_volgroup", "id": vg_id, "name": name,
                                 "devices": devices, "preserve": False})
            self.handles[name] = vg_id
            for vol in group.get("volumes", []):
                lv_handle = vol["name"]
                lv_id = f"lv-{name}-{lv_handle}"
                self.actions.append({
                    "type": "lvm_partition", "id": lv_id, "name": lv_handle,
                    "volgroup": vg_id,
                    "size": size_bytes(vol.get("size", "rest"),
                                       f"lvm '{name}' volume '{lv_handle}'"),
                    "preserve": False,
                })
                self.handles[lv_handle] = lv_id
                if lv_handle not in consumed:
                    self.defer_fs(lv_id, vol, f"lvm '{name}' volume '{lv_handle}'")

    def resolve(self, handles: list[str]) -> tuple[list[str], list[str]]:
        found = [self.handles[h] for h in handles if h in self.handles]
        missing = [h for h in handles if h not in self.handles]
        return found, missing

    # ── formats & mounts ──
    def build_formats(self) -> None:
        mounts: list[dict] = []
        for n, spec in enumerate(self.fs_specs, start=1):
            fstype = {"vfat": "fat32"}.get(spec["fs"], spec["fs"])
            fmt = {"type": "format", "id": f"fmt-{n}", "volume": spec["volume"],
                   "fstype": fstype, "preserve": False}
            if spec.get("label"):
                fmt["label"] = spec["label"]
            self.actions.append(fmt)
            if spec["fs"] == "swap":
                continue
            if not spec["mountpoint"]:
                continue
            mount = {"type": "mount", "id": f"mnt-{n}", "device": f"fmt-{n}",
                     "path": spec["mountpoint"]}
            if spec.get("mount_options"):
                mount["options"] = ",".join(spec["mount_options"])
            mounts.append(mount)
            if spec.get("subvolumes"):
                if spec["fs"] != "btrfs":
                    refuse(f"{spec['what']}: subvolumes declared on a {spec['fs']} filesystem")
                else:
                    early, last = btrfs_subvolume_commands(
                        spec, list(self.disk_paths.values()), self.firmware)
                    self.late.extend(early)
                    self.late_last.extend(last)
        # curtin mounts in list order; parents must precede children.
        mounts.sort(key=lambda m: m["path"].count("/") if m["path"] != "/" else 0)
        self.actions.extend(mounts)

    def build(self) -> list[dict]:
        self.build_disks()
        self.build_partitions()
        self.build_raid()
        self.build_encryption()
        self.build_lvm()
        self.build_formats()
        return self.actions


ROOT_SUBVOL_SCRIPT = """set -eu
base={base}
name={name}
trace=$(mktemp)

# This step relocates an already-installed root filesystem, so if any part of it
# fails the trace goes to the console rather than into the installer journal
# where nobody will find it.
trap 'rc=$?; [ "$rc" = 0 ] || {{ echo "LIS: root subvolume conversion failed (exit $rc)"
    cat "$trace"; }} > /dev/console 2>&1' EXIT
exec >"$trace" 2>&1
set -x

dev=$(findmnt -no SOURCE --target "$base" | sed 's/\\[.*//')

# curtin leaves nested filesystems mounted inside the target (a separate /boot
# above all) and a mount point cannot be moved, so record them and unmount them
# deepest-first; they are restored once the root has moved.
findmnt -rno TARGET,SOURCE,FSTYPE,OPTIONS --submounts "$base" \\
    | awk -v b="$base" '$1 != b' | sort -r > /run/lis-submounts
cut -d' ' -f1 /run/lis-submounts \\
    | xargs -r -n1 sh -c 'umount "$0" 2>/dev/null || umount -l "$0"'

btrfs subvolume create "$base/$name"
find "$base" -mindepth 1 -maxdepth 1 ! -name '@*' -exec mv -t "$base/$name" -- {{}} +

# Re-point the target mount at the subvolume. Everything downstream — the
# chroot below, and the installer's own log copy and teardown afterwards —
# expects the installed root to be reachable at the target path, and grub-probe
# resolves / through the mount table, so this has to be a real mount and not a
# chroot into a plain directory.
umount "$base" 2>/dev/null || umount -l "$base"
mount -o subvol="$name" "$dev" "$base"

while read -r tgt src fstype opts; do
    mkdir -p "$tgt"
    mount -t "$fstype" -o "$opts" "${{src%%[*}}" "$tgt" || true
done < /run/lis-submounts

awk -v mp={mountpoint} -v o="subvol=$name" \\
    '$2==mp && $3=="btrfs" {{$4=o","$4}} {{print}}' \\
    "$base/etc/fstab" > /run/lis-fstab
cat /run/lis-fstab > "$base/etc/fstab"

# GRUB was configured against the top-level subvolume; regenerate it now that
# the target mount resolves to the subvolume.
for d in dev proc sys; do mount --bind "/$d" "$base/$d"; done
chroot "$base" update-grub{grub_install}
for d in sys proc dev; do umount "$base/$d" || true; done
"""


def btrfs_subvolume_commands(spec: dict, disks: list[str],
                             firmware: str) -> tuple[list[list[str]], list[list[str]]]:
    """Translate `subvolumes` into curtin late-commands — created, not flattened.

    curtin's `format` action has no subvolume vocabulary (curtin bug #2017893 is
    still open), so subvolumes are carved after curtin lays the filesystem down:
    content is *moved* into each subvolume — nothing is discarded — and fstab
    gains a matching `subvol=` entry.

    A subvolume claiming the filesystem's own mountpoint (the conventional `@`
    at `/`) relocates the installed root, so it runs last and has to unmount the
    nested filesystems first, then restore them inside the new root and
    regenerate GRUB there.
    """
    mp = spec["mountpoint"]
    base = "/target" + ("" if mp == "/" else mp)
    cmds: list[list[str]] = []
    last: list[list[str]] = []
    for sub in spec["subvolumes"]:
        name = sub["name"]
        sub_mp = sub["mountpoint"]
        sub_opts = ",".join(sub.get("mount_options", []) or spec.get("mount_options", []))
        options = f"subvol={name}" + (f",{sub_opts}" if sub_opts else "")
        if sub_mp == mp:
            grub_install = ""
            if firmware == "bios" and disks:
                grub_install = f'\nchroot "$base" grub-install {disks[0]}'
            last.append(["sh", "-c", ROOT_SUBVOL_SCRIPT.format(
                base=shquote(base), name=shquote(name),
                mountpoint=shquote(mp), grub_install=grub_install)])
            continue
        script = (
            f'set -e; base="{base}"; dest="/target{sub_mp}"; '
            f'btrfs subvolume create "$base/{name}"; '
            f'if [ -d "$dest" ]; then '
            f'find "$dest" -mindepth 1 -maxdepth 1 -exec mv -t "$base/{name}" -- {{}} + ; '
            f'fi; '
            f'dev=$(findmnt -no SOURCE --target "$base" | sed "s/\\[.*//"); '
            f'uuid=$(blkid -s UUID -o value "$dev"); '
            f'mkdir -p "$dest"; '
            f'printf "UUID=%s\\t%s\\tbtrfs\\t%s\\t0\\t0\\n" '
            f'"$uuid" "{sub_mp}" "{options}" >> /target/etc/fstab; '
            f'mount -o {options} "$dev" "$dest"'
        )
        cmds.append(["sh", "-c", script])
    return cmds, last


def shquote(value: str) -> str:
    """Single-quote a value for the generated shell scripts."""
    return "'" + value.replace("'", "'\\''") + "'"


def storage_config(doc: dict) -> tuple[list | None, list[str]]:
    """Returns (curtin actions, late-commands, late-commands that must run last)."""
    storage = doc.get("storage", {}) or {}
    if not storage:
        return None, [], []
    builder = StorageBuilder(doc)
    actions = builder.build()
    if swapfile := (storage.get("swap", {}) or {}).get("file"):
        builder.late.append(in_target(
            f"fallocate -l {swapfile['size'].replace('iB', '')} {swapfile['path']} && "
            f"chmod 600 {swapfile['path']} && mkswap {swapfile['path']} && "
            f"echo '{swapfile['path']} none swap sw 0 0' >> /etc/fstab"))
        warn("storage.swap.file created by a late-command (curtin has no swapfile action)")
    if (storage.get("snapshots", {}) or {}).get("enabled"):
        refuse("storage.snapshots is not expressible in autoinstall "
               "(no snapper/timeshift integration in subiquity)")
    return (actions or None), builder.late, builder.late_last


# ── document → cloud-config ──────────────────────────────────────

def _kb_layout(keymap: dict) -> str:
    """autoinstall carries one xkb layout; report a console map it cannot keep."""
    console, layout = keymap.get("console"), keymap.get("layout")
    if console and layout and console != layout:
        warn(f"system.keymap.console {console!r} is not applied — autoinstall "
             f"takes one keyboard layout, and layout {layout!r} was declared")
    return layout or console or "us"


def translate(doc: dict) -> dict:
    system = doc.get("system", {}) or {}
    boot = doc.get("boot", {}) or {}
    software = doc.get("software", {}) or {}
    network = doc.get("network", {}) or {}
    scripts = doc.get("scripts", {}) or {}
    installer = doc.get("installer", {}) or {}
    drivers = doc.get("drivers", {}) or {}
    storage = doc.get("storage", {}) or {}
    keymap = system.get("keymap", {}) or {}

    users = [u for u in doc.get("users", []) if u["name"] != "root"]
    primary = users[0] if users else None

    auto: dict = {
        "version": 1,
        "locale": system.get("locale", "en_US.UTF-8"),
        "timezone": system.get("timezone", "UTC"),
        "keyboard": {"layout": _kb_layout(keymap),
                     "variant": keymap.get("variant", "")},
    }

    if primary:
        if primary.get("admin") is False:
            warn(f"users['{primary['name']}'].admin false is not applied — "
                 "the autoinstall identity user is always an administrator")
        identity = {
            "hostname": system.get("hostname", "ubuntu"),
            "username": primary["name"],
            "realname": primary.get("comment", primary["name"]),
        }
        # autoinstall takes crypt(3) hashes directly — no plaintext needed.
        if hash_ := (primary.get("password") or {}).get("hash"):
            identity["password"] = hash_
        elif (primary.get("password") or {}).get("locked"):
            identity["password"] = "!"
        else:
            refuse(f"user '{primary['name']}': no password hash and not marked locked — "
                   "autoinstall cannot create a usable account (SPEC §2.4 forbids "
                   "inlining a plaintext password)")
            identity["password"] = "!"
        auto["identity"] = identity
    else:
        refuse("document declares no non-root user; autoinstall requires an identity")
        auto["identity"] = {"hostname": system.get("hostname", "ubuntu"),
                            "username": "ubuntu", "password": "!"}

    ssh = network.get("ssh", {}) or {}
    auto["ssh"] = {
        "install-server": bool(ssh.get("enabled", False)),
        "allow-pw": bool(ssh.get("password_auth", False)),
        "authorized-keys": (primary or {}).get("ssh_authorized_keys", []),
    }
    if ssh.get("permit_root"):
        warn("network.ssh.permit_root applied via late-command sshd_config edit")

    storage_actions, storage_late, storage_late_last = storage_config(doc)
    if storage_actions:
        auto["storage"] = {"config": storage_actions}
    elif storage:
        refuse("storage section could not be translated into any curtin action")

    # Packages + software.apps
    packages = list(software.get("packages", []))
    flatpaks = list(software.get("flatpak", []))
    for app in software.get("apps", []):
        if isinstance(app, str):
            packages.append(app)
        elif isinstance(app, dict):
            if name := (app.get("package") or app.get("name")):
                packages.append(name)
            if fp := app.get("flatpak"):
                flatpaks.append(fp)

    role = software.get("role", "")
    role_packages = {"desktop:gnome": "ubuntu-desktop", "desktop:kde": "kubuntu-desktop",
                     "desktop:xfce": "xubuntu-desktop", "desktop:mate": "ubuntu-mate-desktop",
                     "desktop:budgie": "ubuntu-budgie-desktop"}
    if role in role_packages:
        packages.append(role_packages[role])
    elif role.startswith("desktop:"):
        refuse(f"software.role {role!r} has no Ubuntu meta-package")
    if flatpaks:
        packages.append("flatpak")
    if (storage.get("swap", {}) or {}).get("zram"):
        packages.append("zram-config")
        warn("storage.swap.zram honored by installing the zram-config package")
    # Assigned once every contributor below (drivers, desktop) has run: keying
    # off an empty list here would drop everything they add.
    if software.get("exclude"):
        refuse("software.exclude has no autoinstall equivalent (packages are additive)")
    if snaps := software.get("snap"):
        auto["snaps"] = [{"name": s["name"],
                          **({"channel": s["channel"]} if s.get("channel") else {}),
                          **({"classic": True} if s.get("classic") else {})}
                         for s in snaps]

    variant = (boot.get("kernel", {}) or {}).get("variant", "default")
    if variant not in ("default", None):
        kernel_map = {"lts": "linux-generic", "hardened": None, "realtime": "linux-realtime",
                      "zen": None}
        pkg = kernel_map.get(variant)
        if pkg:
            auto["kernel"] = {"package": pkg}
        else:
            refuse(f"boot.kernel.variant {variant!r} has no Ubuntu kernel package")
    if boot.get("loader") == "systemd-boot":
        refuse("boot.loader 'systemd-boot': Ubuntu Server autoinstall installs GRUB")
    if params := (boot.get("kernel", {}) or {}).get("params"):
        warn("boot.kernel.params applied via late-command GRUB_CMDLINE_LINUX edit")

    if drivers.get("gpu") in ("nvidia", "nvidia-open"):
        # autoinstall has a native switch for third-party (restricted) drivers.
        auto["drivers"] = {"install": True}
        packages += driver_packages(doc, "ubuntu", skip=frozenset({"gpu"}))
    else:
        packages += driver_packages(doc, "ubuntu")

    if proxy := (doc.get("proxy", {}) or {}).get("http"):
        auto["proxy"] = proxy
    if mirror_url := (doc.get("mirror", {}) or {}).get("url"):
        auto["apt"] = {"primary": [{"arches": ["default"], "uri": mirror_url}]}

    # 1. Early / pre-storage commands
    early = []
    for stage in ("pre_install", "pre"):
        for s in scripts.get(stage, []):
            if c := s.get("content"):
                early.append(c)
    if early:
        auto["early-commands"] = early
    if scripts.get("post_storage"):
        warn("scripts.post_storage runs as an early late-command "
             "(curtin has no post-partition hook of its own)")

    # 2. Late / post-install commands. Storage fix-ups run first.
    late: list[str] = list(storage_late)
    for s in scripts.get("post_storage", []):
        if c := s.get("content"):
            late.append(c)

    if params:
        joined = " ".join(params)
        late.append(in_target(
            f'sed -i \'s|^GRUB_CMDLINE_LINUX=.*|GRUB_CMDLINE_LINUX="{joined}"|\' '
            "/etc/default/grub && update-grub"))
    if ssh.get("permit_root"):
        late.append(in_target(
            f'echo "PermitRootLogin {ssh["permit_root"]}" >> /etc/ssh/sshd_config'))

    for cmd in sudoers_commands(doc):
        late.append(in_target(cmd))
    for cmd in boot_timeout_commands(doc, "ubuntu", (doc.get("boot") or {}).get("loader", "grub")):
        late.append(in_target(cmd))

    for stage in ("post_install", "post", "pre_reboot", "on_success"):
        for s in scripts.get(stage, []):
            if c := s.get("content"):
                if s.get("chroot"):
                    late.append(in_target(c))
                else:
                    late.append(["sh", "-c", c])
    if scripts.get("on_error"):
        refuse("scripts.on_error has no autoinstall equivalent (no failure hook)")

    # subiquity builds the primary account from `identity`, which carries no
    # group or shell vocabulary — so anything the document declares there has to
    # be applied in-target or it would be dropped without a word.
    if primary:
        primary_groups = list(primary.get("groups", []))
        if primary_groups:
            late.append(in_target(
                f"usermod -aG {','.join(primary_groups)} {primary['name']}"))
        if shell := primary.get("shell"):
            late.append(in_target(
                f"chsh -s {shell_path(shell)} {primary['name']}"))

    # Extra users beyond the primary: created in-target.
    for user in users[1:]:
        password = user.get("password") or {}
        hash_ = password.get("hash")
        if not hash_ and not password.get("locked"):
            refuse(f"user '{user['name']}': no password hash and not marked locked")
        groups = ",".join(user.get("groups", []) + (["sudo"] if user.get("admin") else []))
        cmd = (f"curtin in-target -- useradd -m -p '{hash_ or '!'}'"
               + (f" -G '{groups}'" if groups else "")
               + (f" -c '{user['comment']}'" if user.get("comment") else "")
               + (" -s " + shell_path(user["shell"]) if user.get("shell") else "")
               + f" {user['name']}")
        late.append(cmd)
        for key in user.get("ssh_authorized_keys", []) or []:
            late.append(in_target(
                f"install -d -m700 -o {user['name']} /home/{user['name']}/.ssh && "
                f"echo {json.dumps(key)} >> /home/{user['name']}/.ssh/authorized_keys"))

    for user in users:
        if user.get("dotfiles"):
            refuse(f"users['{user['name']}'].dotfiles is not applied by this applier")
        if user_scripts := user.get("scripts", {}):
            for s in user_scripts.get("post_install", []):
                if c := s.get("content"):
                    late.append(["curtin", "in-target", "--", "su", "-", user["name"],
                                 "-c", c])

    # Birth certificate (delivery.md §8): the document as applied, secret references
    # left unresolved. Base64 keeps the payload out of shell-quoting hazards.
    birth = base64.b64encode(json.dumps(doc, separators=(",", ":")).encode()).decode()
    late.append(in_target(
        "install -d -m755 /var/lib/lis && "
        f"echo {birth} | base64 -d > /var/lib/lis/system.lis.json && "
        "chmod 600 /var/lib/lis/system.lis.json"))

    late += storage_late_last
    if late:
        auto["late-commands"] = late

    if fin := installer.get("on_finish"):
        if fin in ("reboot", "poweroff"):
            auto["shutdown"] = fin
        else:
            refuse(f"installer.on_finish {fin!r} has no autoinstall equivalent")
    if interactive := installer.get("interactive"):
        auto["interactive-sections"] = interactive

    # Network — subiquity consumes a netplan fragment verbatim.
    if netplan := network_config(network):
        auto["network"] = netplan

    # First-boot work rides on cloud-init proper, next to autoinstall.
    cloud_config: dict = {"autoinstall": auto}
    runcmd = [s["content"] for s in scripts.get("firstboot", []) if s.get("content")]
    for user in users:
        if user_scripts := user.get("scripts", {}):
            for s in user_scripts.get("firstboot", []):
                if c := s.get("content"):
                    runcmd.append(f"su - {user['name']} -c {json.dumps(c)}")
    for app in flatpaks:
        runcmd.append(f"flatpak install -y flathub {app}")
    if runcmd:
        auto["user-data"] = {"runcmd": runcmd}

    for entry in doc.get("files", []) or []:
        auto.setdefault("user-data", {}).setdefault("write_files", []).append({
            "path": entry["path"], "content": entry["content"],
            **({"permissions": entry["mode"]} if entry.get("mode") else {}),
        })

    if desktop := doc.get("desktop"):
        translate_desktop(desktop, auto, packages)

    if packages:
        auto["packages"] = packages

    if doc.get("registration"):
        refuse("registration (Ubuntu Pro attach) is not expressible in autoinstall; "
               "attach via a firstboot script with a seed: token reference")

    return cloud_config


def shell_path(shell: str) -> str:
    return shell if shell.startswith("/") else f"/usr/bin/{shell}"


def translate_desktop(desktop: dict, auto: dict, packages: list) -> None:
    dm = desktop.get("display_manager")
    if dm and dm not in ("auto", "gdm", "gdm3"):
        refuse(f"desktop.display_manager {dm!r} is not selectable from autoinstall")
    if desktop.get("autologin"):
        refuse("desktop.autologin is not expressible in autoinstall")
    if desktop.get("audio") not in (None, "auto", "pipewire"):
        refuse(f"desktop.audio {desktop['audio']!r} is not expressible in autoinstall")


def network_config(network: dict) -> dict | None:
    """LIS network → netplan v2, which subiquity accepts under `network:`."""
    interfaces = network.get("interfaces", []) or []
    if not interfaces:
        if network.get("manager") not in (None, "auto", "networkmanager"):
            refuse(f"network.manager {network['manager']!r} is not selectable in "
                   "Ubuntu Server autoinstall (subiquity installs systemd-networkd)")
        if network.get("wifi"):
            refuse("network.wifi is not expressible in Ubuntu Server autoinstall")
        for entry in network.get("hosts", []) or []:
            warn(f"network.hosts entry {entry.get('ip')} not translated")
        if network.get("firewall"):
            refuse("network.firewall is not expressible in autoinstall "
                   "(ufw must be configured in a late-command)")
        return None
    ethernets: dict = {}
    for iface in interfaces:
        name = iface.get("name") or iface.get("match", {}).get("name")
        if not name:
            refuse("network.interfaces entry has no resolvable interface name")
            continue
        cfg: dict = {}
        if iface.get("dhcp4", True) and not iface.get("addresses"):
            cfg["dhcp4"] = True
        if addrs := iface.get("addresses"):
            cfg["addresses"] = addrs
            cfg["dhcp4"] = False
        if gw := iface.get("gateway4") or iface.get("gateway"):
            cfg["routes"] = [{"to": "default", "via": gw}]
        if ns := iface.get("nameservers"):
            cfg["nameservers"] = {"addresses": ns}
        ethernets[name] = cfg
    if network.get("wifi"):
        refuse("network.wifi is not expressible in Ubuntu Server autoinstall")
    return {"version": 2, "ethernets": ethernets} if ethernets else None


# ── YAML emitter ─────────────────────────────────────────────────

def to_yaml(value, indent=0) -> str:
    """Tiny YAML emitter (avoids a pyyaml hard dependency for JSON input)."""
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return pad + "{}\n"
        out = ""
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                out += f"{pad}{k}:\n" + to_yaml(v, indent + 1)
            else:
                out += f"{pad}{k}: {scalar(v)}\n"
        return out
    if isinstance(value, list):
        out = ""
        for item in value:
            if isinstance(item, (dict, list)) and item:
                body = to_yaml(item, indent + 1)
                first, _, rest = body.partition("\n")
                out += f"{pad}- {first.strip()}\n"
                if rest:
                    out += rest
            else:
                out += f"{pad}- {scalar(item)}\n"
        return out
    return pad + scalar(value) + "\n"


# Identifier-ish strings emit bare; anything else is JSON-quoted (valid YAML).
PLAIN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
YAML_KEYWORDS = {"true", "false", "null", "yes", "no", "on", "off", "y", "n"}


def scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        return "{}"
    if isinstance(v, list):
        return "[]"
    if PLAIN.match(v) and v.lower() not in YAML_KEYWORDS:
        return v
    return json.dumps(v)  # JSON string quoting is valid YAML


# ── apply ────────────────────────────────────────────────────────

def apply_seed(out_dir: pathlib.Path) -> int:
    """Hand the seed to subiquity and let *it* install (SPEC principle 1).

    This applier never partitions, formats or copies a root filesystem itself:
    a translator that shells out to sfdisk/rsync is no longer running the
    distro's native installer, and whatever it produces is unrelated to the
    autoinstall document it just wrote.
    """
    import shutil
    import subprocess

    seed_dir = pathlib.Path("/var/lib/cloud/seed/nocloud")
    try:
        seed_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(out_dir / "user-data", seed_dir / "user-data")
        shutil.copy(out_dir / "meta-data", seed_dir / "meta-data")
    except OSError as err:
        sys.exit(f"error: cannot install NoCloud seed into {seed_dir}: {err}")
    print(f"installed NoCloud seed into {seed_dir}")

    subiquity = shutil.which("subiquity") or shutil.which("subiquity-server")
    if not subiquity:
        snap = pathlib.Path("/snap/bin/subiquity")
        subiquity = str(snap) if snap.exists() else None
    if not subiquity:
        sys.exit("error: --apply requested, but subiquity is not present on this system.\n"
                 "The supported path is to boot the Ubuntu Server ISO with 'autoinstall' on\n"
                 "the kernel command line and this seed on a CIDATA-labelled volume; subiquity\n"
                 "then runs its own native install. This applier will not partition disks itself.")
    cmd = [subiquity, "--autoinstall", str(seed_dir / "user-data")]
    print(f"executing native installer: {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Translate a LIS document into an Ubuntu autoinstall NoCloud seed.")
    add_common_args(ap)
    ap.add_argument("--apply", "-a", action="store_true",
                    help="install the seed for subiquity and hand off to the native installer")
    args = ap.parse_args()

    doc = track(load_doc(args.file))
    check_version(doc, args.file)
    check_firmware(doc)
    check_unhandled(doc, ALL_SECTIONS)
    check_boot_extras(doc, {"kernel", "loader", "params", "timeout", "variant"})
    check_mirror(doc, {"url"})
    check_section_fields(doc, "desktop", {"audio", "autologin", "display_manager"})
    check_section_fields(doc, "installer", {"interactive", "on_finish"})
    check_keymap(doc, {"console", "layout", "variant"})

    cloud_config = translate(doc)
    args.out.mkdir(parents=True, exist_ok=True)
    user_data_file = args.out / "user-data"
    meta_data_file = args.out / "meta-data"

    user_data_file.write_text("#cloud-config\n" + to_yaml(cloud_config))
    hostname = (doc.get("system", {}) or {}).get("hostname", "ubuntu")
    meta_data_file.write_text(f"instance-id: lis-{hostname}\nlocal-hostname: {hostname}\n")
    report(user_data_file, meta_data_file)

    # Fail closed *before* touching the machine, not after.
    check_arch(doc, {"x86_64"})
    check_script_fields(doc, honors_chroot=True)
    check_unread(doc)

    if status := enforce(args.strict):
        return status

    if args.apply:
        return apply_seed(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
