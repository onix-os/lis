#!/usr/bin/env python3
"""lis2nixos — the default LIS → NixOS translator.

Usage: lis2nixos.py FILE.lis.json [--out DIR] [--strict]

Writes the classic NixOS trio into DIR (default '.'):
  disko.nix           — declarative partitioning for the disko module
  hardware.nix        — hardware-configuration-style module
  configuration.nix   — plain NixOS options only, no third-party modules

This is the *default acting as default*: opinionated flakes are expected to
ship their own translators. Core intent plain NixOS cannot express is
reported as a warning; with --strict any dropped intent exits non-zero
(SPEC §2.3).
"""

import argparse
import base64
import difflib
import hashlib
import ipaddress
import os
import shlex
import json
import pathlib
import re
import sys

from lis_common import (track, check_unread, luks_key_path, check_raid_consumers, registration_commands, enrollment_commands, resolve_disk_paths, check_snapshots, match_selectors, consume, password_field, secret_ref, APPLY_TIME_PATHS,ALL_SECTIONS, add_common_args, check_firmware,
                        check_encryption_emitted, resolve_mountpoints,
                        check_unhandled, check_extensions, check_section_fields, check_mirror, check_kernel_variant, check_user_sudo,
                        ROLE_FS, role_fs,
                        check_boot_extras, check_keymap, check_version, enforce,
                        check_script_fields,
                        load_doc, refuse, report, warn)


def nix_str(s: str) -> str:
    """A Nix double-quoted string literal holding exactly `s`.

    `${` has to be escaped as well as `\\` and `"`: inside a double-quoted Nix
    string `${...}` is antiquotation, so a files[] entry whose content is a
    shell snippet (`${HOME}`) or a systemd unit (`${prefix}`) either fails to
    evaluate — after disko has already wiped the disks — or, worse, evaluates
    an expression the document never wrote. `\\${` is the documented literal
    (Nix manual, "String literals").
    """
    return ('"' + s.replace("\\", "\\\\").replace('"', '\\"')
            .replace("${", "\\${") + '"')


def nix_list(items: list[str]) -> str:
    if not items:
        return "[ ]"
    return "[ " + " ".join(nix_str(i) for i in items) + " ]"


def nix_bool(value, where: str) -> str | None:
    """A Nix boolean literal, or None once the value has been refused.

    `str(value).lower()` is not a converter: a document that writes "yes"
    becomes the bare token `yes`, which does not even *parse* — and under
    --apply the parse happens inside nixos-install, after disko has wiped the
    disks. JSON's own `true`/`false` are the only values that survive.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    refuse(f"{where} {value!r} is not a boolean (true or false)")
    return None


def disko_size(size, where: str, *, percent: bool = False) -> str:
    """A LIS size as disko spells it, or a refusal saying why it cannot.

    Two shapes the schema allows do not survive the trip and used to leave by
    the back door. A malformed size raised ValueError and aborted with a Python
    traceback instead of a refusal (SPEC §2.3 wants a stated reason). A
    percentage other than 100% was passed straight through, and disko's GPT
    partition size is `either (enum [ "100%" ]) (strMatching "[0-9]+[KMGTP]?")`
    (disko lib/types/gpt.nix) — so `size: "50%"` produced a config that fails to
    evaluate at apply time. LVM is the exception: disko rewrites a trailing '%'
    to '%FREE' for lvcreate (lib/types/lvm_vg.nix:130-135), so percentages are
    real there and `percent=True` lets them through.
    """
    if not isinstance(size, str):
        refuse(f"{where}: size {size!r} is not a string")
        return "100%"
    if size == "rest":
        return "100%"
    for unit, letter in (("MiB", "M"), ("GiB", "G"), ("TiB", "T")):
        if size.endswith(unit) and size[: -len(unit)].isdigit():
            return size[: -len(unit)] + letter
    if re.fullmatch(r"[0-9]{1,3}%", size):
        if percent or size == "100%":
            return size
        refuse(f"{where}: size {size!r} — a GPT partition cannot be sized as a "
               "share of the disk: disko's size is `either (enum [ \"100%\" ]) "
               "(strMatching \"[0-9]+[KMGTP]?\")` (lib/types/gpt.nix) and sgdisk "
               "has no percentage form; use an absolute size or \"rest\"")
        return "100%"
    refuse(f"{where}: size {size!r} is not a LIS size — expected <n>MiB, <n>GiB, "
           "<n>TiB, <n>% or \"rest\" (schema.md §6.1)")
    return "100%"


def size_mib(size, where: str) -> int | None:
    """A LIS size in whole MiB, or None with a refusal recorded.

    swapDevices[].size is measured in MiB (nixos/modules/config/swap.nix), and
    the previous reader was `int(size[:-3]) if size.endswith("GiB") else 4` —
    so every MiB and TiB spelling silently became a 4 GiB swap file.
    """
    if not isinstance(size, str):
        refuse(f"{where}: size {size!r} is not a string")
        return None
    for unit, factor in (("MiB", 1), ("GiB", 1024), ("TiB", 1024 * 1024)):
        if size.endswith(unit) and size[: -len(unit)].isdigit():
            return int(size[: -len(unit)]) * factor
    refuse(f"{where}: size {size!r} is not a LIS size — expected <n>MiB, <n>GiB "
           "or <n>TiB (schema.md §6.5)")
    return None


# ── disko.nix ──────────────────────────────────────

DEFAULT_ZPOOL = "rpool"

# ZFS gives three characters a structural meaning inside a dataset name — '@'
# separates a snapshot, '#' a bookmark, '/' a child dataset — and reserves '%'
# for its own internal datasets (zfs(8), "Naming Requirements"). A btrfs
# subvolume called '@home' is perfectly legal, so a document that declares one
# and asks for zfs is asking for something ZFS cannot name.
ZFS_RESERVED = re.compile(r"[@#/%]")

_ZFS_REFUSED: set[str] = set()


def zfs_child(base: str, name: str) -> str:
    """The dataset a `subvolumes[]` entry becomes under a zfs filesystem.

    The declared name is used verbatim. It used to be run through
    `lstrip("@")`, which put '@home' on disk as 'home' — the same document
    creates '@home' on every applier that does not mangle it, so one
    declaration landed under two conventions depending on the target. Since
    ZFS genuinely cannot carry the character, the honest answer is to refuse
    rather than to rename: the operator asked for a name, and quietly
    installing a different one is the silent drift the spec forbids (SPEC
    §2.3).
    """
    if ZFS_RESERVED.search(name):
        if name not in _ZFS_REFUSED:
            _ZFS_REFUSED.add(name)
            refuse(f"subvolume {name!r} on a zfs filesystem: ZFS reserves @ (snapshot), "
                   "# (bookmark), / (child dataset) and % in dataset names "
                   "(zfs(8), Naming Requirements), so this name cannot be created "
                   "as written — rename the subvolume, or declare fs: btrfs where "
                   "the name is legal")
    return f"{base}-{name}"


# The mountpoint arbitration below is per-storage-section, not per-partition:
# resolve_mountpoints() has to see the whole layout to notice that two
# partitions are asking for one path. This translator walks the layout three
# times (disko, the mount table, the initrd luks list) and refuse() is not
# idempotent, so the verdict is reached once and reused. The storage object
# itself is kept in the cache, which stops its id() from being recycled by a
# second document translated in the same process.
_ARBITRATED: dict[int, tuple[dict, dict[int, str | None]]] = {}

_NAMED: dict[int, tuple[dict, dict[int, tuple[str, str]]]] = {}


def partition_names(storage: dict) -> dict[int, tuple[str, str]]:
    """id(partition) -> (disk id, the attribute name disko will give it).

    One table, computed once, consumed by every pass. It used to be recomputed
    three times from three slightly different loops — render_disko counted the
    per-disk index *after* skipping adopted partitions while mount_table and
    luks_initrd_devices counted every one — so a document with an `existing`
    entry made disko.nix and hardware.nix disagree about which partition is
    which, and hardware.nix named devices that were never created.
    """
    cached = _NAMED.get(id(storage))
    if cached is not None and cached[0] is storage:
        return cached[1]
    index: dict[str, int] = {}
    out: dict[int, tuple[str, str]] = {}
    for position, part in enumerate(storage.get("partitions", []) or [], 1):
        disk_id = part.get("disk")
        if not disk_id:
            refuse(f"partition {part.get('id') or position!r}: no 'disk' handle — "
                   "storage.partitions[].disk is required (schema.md §6.1), and "
                   "without it there is no disk to create the partition on")
            disk_id = "main"
        index[disk_id] = index.get(disk_id, 0) + 1
        out[id(part)] = (disk_id,
                         part.get("id") or f"{part.get('role', 'part')}{index[disk_id]}")
    _NAMED[id(storage)] = (storage, out)
    return out


def spec_where(spec: dict, handle: str = "") -> str:
    """How a diagnostic names the partition, array or volume it is about."""
    if "level" in spec and "name" in spec:
        return f"raid array {spec['name']!r}"
    if "id" in spec or "role" in spec:
        return f"partition {spec.get('id') or spec.get('role')!r}"
    return f"volume {spec.get('name') or handle!r}"


def partition_label(disk_id: str, name: str) -> str:
    """The GPT name disko writes for a partition, character for character.

    disko's default is `${parent.type}-${parent.name}-${partition.name}`, but a
    GPT name is 72 bytes of UTF-16 so it caps at 36 characters and falls back to
    `substring 0 36 (hashString "sha256" label)` past that (lib/types/gpt.nix:
    139-151). hardware.nix derives /dev/disk/by-partlabel/… from this, so
    reimplementing the truncation is the difference between a mountable root and
    a device node that does not exist — for any document whose disk and
    partition handles together run long.
    """
    label = f"disk-{disk_id}-{name}"
    if len(label) > 36:
        label = hashlib.sha256(label.encode()).hexdigest()[:36]
    return label


def partition_device(disk_id: str, name: str) -> str:
    return f"/dev/disk/by-partlabel/{partition_label(disk_id, name)}"


def adopted_device(part: dict) -> str | None:
    """The device node an adopted partition answers to, or None if not adopted.

    An adopted partition keeps the GPT name it already had, so the label this
    translator would have written is not on the disk and by-partlabel would name
    nothing. Its partition GUID is pinned instead, which is also what disko
    itself resolves the partition to once `uuid` is set
    (lib/types/gpt.nix:81-90).
    """
    info = ADOPTED.get(id(part))
    if info and GUID.fullmatch(info.get("uuid") or ""):
        return f"/dev/disk/by-partuuid/{info['uuid']}"
    return None


def partition_mountpoints(storage: dict) -> dict[int, str | None]:
    """Every declared partition's mountpoint, arbitrated, keyed by identity.

    Role defaults are shorthand, not a second declaration: test-raid's 'boot2'
    carries `role: boot` for the mirroring and deliberately omits the
    mountpoint, because only one of the pair can be mounted at /boot. Handing
    it the default too would put two `mountpoint = "/boot"` entries in one
    disko config. Explicit mountpoints win wherever they sit in the list, and a
    role default whose path is already spoken for is dropped.
    """
    cached = _ARBITRATED.get(id(storage))
    if cached is not None and cached[0] is storage:
        return cached[1]

    parts = list(storage.get("partitions", []) or [])
    # A logical volume or an array at / is as much a claimant as a partition
    # is, and it is rendered by a different pass, so seed it here.
    claimed = {vol.get("mountpoint") for group in (storage.get("lvm") or [])
               for vol in (group.get("volumes") or []) if vol.get("mountpoint")}
    claimed |= {array.get("mountpoint") for array in (storage.get("raid") or [])
                if array.get("mountpoint")}

    # NixOS puts the ESP at /boot, not at the shared ROLE_MOUNT default of
    # /boot/efi: boot.loader.efi.efiSysMountPoint defaults to "/boot" and both
    # loaders this translator emits write their entries beneath it. Since it
    # never sets efiSysMountPoint, an ESP arbitrated onto /boot/efi would leave
    # the bootloader with no ESP under it. Only the copy handed to the arbiter
    # is relabelled — it reads nothing but mountpoint, role and id.
    view = [dict(part, role="boot")
            if part.get("role") == "esp" and not part.get("mountpoint") else part
            for part in parts]
    resolved = resolve_mountpoints(view, claimed=claimed)

    for i, part in enumerate(parts):
        if part.get("role") == "esp" and resolved[i] is None and not part.get("mountpoint"):
            refuse(f"partition '{part.get('id') or i}' declares role 'esp' but /boot is "
                   "already claimed by another partition — NixOS mounts the EFI system "
                   "partition at boot.loader.efi.efiSysMountPoint, whose default is "
                   "/boot, and this translator does not emit that option; leaving the "
                   "ESP unmounted would install a system with no bootloader on it")

    out = {id(part): resolved[i] for i, part in enumerate(parts)}
    _ARBITRATED[id(storage)] = (storage, out)
    return out


# mkfs spells "give this filesystem a name" differently per family, and each
# one truncates or rejects past its own on-disk limit — a label mkfs refuses is
# an install that dies after disko has already wiped the disk, so the length is
# checked here rather than discovered there.
LABEL_FLAG = {"ext2": "-L", "ext3": "-L", "ext4": "-L", "xfs": "-L",
              "btrfs": "-L", "f2fs": "-l", "vfat": "-n", "swap": "-L"}
LABEL_LIMIT = {"ext2": 16, "ext3": 16, "ext4": 16, "xfs": 12, "btrfs": 255,
               "f2fs": 512, "vfat": 11, "swap": 15}
LABEL_SAFE = re.compile(r"[A-Za-z0-9._+:-]+")


def label_args(fs: str, label, where: str) -> list[str]:
    """The mkfs arguments that name a filesystem, or [] with a refusal.

    disko has no `label` of its own; it hands `content.extraArgs` to mkfs
    (lib/types/filesystem.nix:23,55-60, btrfs :83,155, swap :40), so the flag is
    per-filesystem. btrfs interpolates them unquoted (`mkfs.btrfs "$dev"
    ${toString extraArgs}`), which is why the character set is checked too.
    """
    if not isinstance(label, str) or not label:
        refuse(f"{where}: label {label!r} is not a non-empty string")
        return []
    flag = LABEL_FLAG.get(fs)
    if not flag:
        refuse(f"{where}: label {label!r} on filesystem {fs!r} — mkfs.{fs} has no "
               "label option this translator knows, so the name would not reach "
               "the disk")
        return []
    if not LABEL_SAFE.fullmatch(label):
        refuse(f"{where}: label {label!r} — disko interpolates mkfs arguments into "
               "its create script unquoted (lib/types/btrfs.nix:155), so a label "
               "is restricted to letters, digits and ._+:-")
        return []
    limit = LABEL_LIMIT[fs]
    if len(label) > limit:
        refuse(f"{where}: label {label!r} is {len(label)} characters but a {fs} "
               f"label holds {limit} — mkfs would reject it after disko had "
               "already partitioned the disk")
        return []
    return [flag, label]


def fs_content(lines, pad, fs, mountpoint, mount_options, subvolumes,
               label=None, where="filesystem", extra_subvolumes=(),
               pre_create=""):
    """Emit the `content = { … }` block for a plain filesystem or swap area.

    `pre_create` is shell run in the same subshell immediately before this
    content's own create step (lib/default.nix:470-473). It carries
    `existing.format: true` and nothing else: disko will not re-make a
    filesystem that is already there, so removing the signature first is the
    only way the request reaches mkfs.
    """
    if fs in (None, "none"):
        return
    extra = label_args(fs, label, where) if label is not None else []
    hook = [f"{pad}  preCreateHook = {nix_str(pre_create)};"] if pre_create else []
    if fs == "swap":
        lines += [f"{pad}content = {{", f"{pad}  type = \"swap\";"] + hook
        if extra:
            lines.append(f"{pad}  extraArgs = {nix_list(extra)};")
        lines.append(f"{pad}}};")
        return
    if fs == "btrfs" and (subvolumes or extra_subvolumes):
        lines += [f"{pad}content = {{",
                  f"{pad}  type = \"btrfs\";"] + hook + [
                  f"{pad}  extraArgs = {nix_list(['-f'] + extra)};",
                  f"{pad}  subvolumes = {{"]
        for name in extra_subvolumes:
            # mountpoint is omitted deliberately: disko creates the subvolume at
            # format time and leaves it unmounted (lib/types/btrfs.nix:120,193).
            lines.append(f"{pad}    {nix_str(name)} = {{ }};")
        covered = any(s["mountpoint"] == mountpoint for s in subvolumes)
        if mountpoint and not covered:
            # Not a rename: nothing in the document covers this mount, and a
            # btrfs mount needs some subvolume, so one is invented for it.
            lines.append(f"{pad}    \"@\" = {{ mountpoint = {nix_str(mountpoint)}; }};")
        for sub in subvolumes:
            # Verbatim. Prefixing an absent '@' created 'home' as '@home' while
            # debian, ubuntu, arch and alpine created it as 'home' — one
            # declaration, two conventions, decided by the target.
            name = sub["name"]
            entry = f"{pad}    {nix_str(name)} = {{ mountpoint = {nix_str(sub['mountpoint'])};"
            if sub.get("mount_options"):
                entry += f" mountOptions = {nix_list(sub['mount_options'])};"
            lines.append(entry + " };")
        lines += [f"{pad}  }};", f"{pad}}};"]
        return
    lines += [f"{pad}content = {{",
              f"{pad}  type = \"filesystem\";"] + hook + [
              f"{pad}  format = {nix_str('vfat' if fs == 'vfat' else fs)};"]
    if extra:
        lines.append(f"{pad}  extraArgs = {nix_list(extra)};")
    if mountpoint:
        lines.append(f"{pad}  mountpoint = {nix_str(mountpoint)};")
    if mount_options:
        lines.append(f"{pad}  mountOptions = {nix_list(mount_options)};")
    lines.append(f"{pad}}};")


class Topology:
    """Resolves which LIS layer consumes which handle, so disko nests correctly.

    disko expresses the stack by nesting `content` blocks: a partition that
    backs a RAID array carries `type = "mdraid"`, one that backs a volume group
    carries `type = "lvm_pv"`, one that backs a pool carries `type = "zfs"`.
    Everything else gets a real filesystem.
    """

    def __init__(self, storage: dict, doc: dict | None = None):
        self.storage = storage
        # Only needed to resolve where a container's key material lives, which
        # `keys[]` can override at the document level.
        self.doc = doc if doc is not None else {"storage": storage}
        self.encryption = storage.get("encryption", []) or []
        self.lvm = storage.get("lvm", []) or []
        self.raid = storage.get("raid", []) or []
        self.luks_over = {c["over"]: c for c in self.encryption}
        self.consumer: dict[str, tuple[str, str]] = {}
        self.zpools: dict[str, dict] = {}
        self.specs: dict[str, dict] = {}   # handle -> the LIS object declaring it
        self.part_mounts = partition_mountpoints(storage)

        # Spares are deliberately not consumers. disko's mdadm create passes
        # `--raid-devices="$(wc -l "$disko_devices_dir"/raid_<name>)"`
        # (lib/types/mdadm.nix:65-68) — it counts every member file — so folding
        # the spares in turned a 2-disk RAID1 plus a hot spare into a 3-way
        # RAID1. They are added after the array exists instead; see
        # render_mdadm's postCreateHook.
        self.raid_spares: dict[str, list[str]] = {}
        for group in self.lvm:
            for dev in group.get("devices", []):
                self.consumer[dev] = ("lvm_pv", group["name"])
        for array in self.raid:
            for dev in array.get("devices", []):
                self.consumer[dev] = ("mdraid", array["name"])
            if spares := (array.get("spares") or []):
                self.raid_spares[array["name"]] = list(spares)
        self.spare_handles = {h for spares in self.raid_spares.values() for h in spares}

        for part in storage.get("partitions", []) or []:
            if handle := part.get("id"):
                self.specs[handle] = part
        for array in self.raid:
            self.specs[array["name"]] = array

        # ZFS: LIS names the filesystem, not the pool. Every `fs: zfs` volume
        # joins one pool whose vdevs are those volumes.
        for handle, spec in list(self.specs.items()):
            if spec.get("fs") == "zfs":
                self.consumer[handle] = ("zfs", DEFAULT_ZPOOL)
                pool = self.zpools.setdefault(DEFAULT_ZPOOL, {"members": [], "datasets": []})
                pool["members"].append(handle)
                pool["datasets"].append(spec)
        for group in self.lvm:
            for vol in group.get("volumes", []):
                if vol.get("fs") == "zfs":
                    warn(f"lvm volume '{vol['name']}': fs zfs on a logical volume is "
                         "unusual; a zpool over the physical volumes is preferred")
        self.check_filesystems()

    def fs_of(self, spec: dict) -> str | None:
        """The filesystem a partition or volume resolves to, role default included.

        The single reader of that rule. There used to be three: emit_layer
        inferred a default for `role: swap` alone, mount_table used its own
        `{esp, swap, root}` table, and the shared ROLE_FS (which also knows
        `boot`) was consulted by neither. `role: boot` therefore reached disko
        with no content block at all and hardware.nix still mounted it, and a
        `role: root` with no `fs` produced an unformatted partition that
        hardware.nix declared to be btrfs.
        """
        return role_fs(spec)

    def check_filesystems(self) -> None:
        """Refuse any mount whose filesystem nothing will create.

        A partition with a mountpoint and no resolvable filesystem is the
        wipe-then-fail case: disko partitions the disk, creates nothing on it,
        and `nixos-install` cannot mount /. Fail-closed here instead (§2.3).
        """
        seen: list[tuple[str, dict, str | None]] = []
        for part in self.storage.get("partitions", []) or []:
            handle = part.get("id") or ""
            if handle and handle in self.spare_handles:
                if part.get("fs") not in (None, "none") or part.get("mountpoint"):
                    refuse(f"{spec_where(part)} is a hot spare of a raid array and "
                           "also declares a filesystem or a mountpoint — `mdadm "
                           "--add` writes the array superblock over whatever is "
                           "there, so one of the two declarations cannot survive")
                continue
            if handle and self.owner_of(handle) is not None:
                continue    # an aggregate or a pool owns it, directly or through luks
            if adopted(part) and id(part) not in ADOPTED:
                continue    # refused where the layout is rendered
            seen.append((spec_where(part), part, self.mountpoint_of(part)))
        for group in self.lvm:
            for vol in group.get("volumes", []):
                if self.owner_of(vol.get("name", "")) is not None:
                    continue
                seen.append((f"lvm volume {group['name']}/{vol.get('name')!r}",
                             vol, vol.get("mountpoint")))
        for array in self.raid:
            if self.owner_of(array["name"]) is not None:
                continue
            seen.append((f"raid array {array['name']!r}", array,
                         self.mountpoint_of(array)))

        for where, spec, mountpoint in seen:
            fs = self.fs_of(spec)
            if fs in (None, "none") and mountpoint:
                refuse(f"{where}: mountpoint {mountpoint} but no filesystem — "
                       f"role {spec.get('role')!r} implies none (schema.md §6.1 "
                       "lists a default only for esp, boot, root and swap) and "
                       "`fs` is not declared, so disko would create the partition "
                       "and format nothing while the installed system was told to "
                       "mount it; declare storage.…fs")
            elif (fs in (None, "none") and spec.get("role") not in ("raw", None)
                    and id(spec) not in ADOPTED):
                warn(f"{where}: role {spec.get('role')!r} with no `fs` — the "
                     "partition is created but never formatted and nothing mounts "
                     "it; declare fs: none to say so deliberately")
            if fs == "vfat" and (spec.get("subvolumes") or []):
                refuse(f"{where}: subvolumes are declared on a {fs} filesystem, "
                       "which has none")
            if spec.get("role") == "esp" and fs != "vfat":
                refuse(f"{where}: role 'esp' with fs {fs!r} — an EFI System "
                       "Partition is vfat by definition (schema.md §6.1: "
                       "'esp → EF00 + vfat'), and firmware will not read "
                       f"{fs} from it")

    def mount_options_of(self, spec: dict) -> list[str]:
        """Mount options for one filesystem, role default included.

        The ESP default lived in render_disko alone, so disko mounted the EFI
        partition with umask=0077 during the install and the installed system
        mounted the same partition with `defaults` — the boot loader's
        configuration and any keys beside it were world-readable on the running
        machine but not in the installer.
        """
        opts = list(spec.get("mount_options") or [])
        if not opts and spec.get("role") == "esp":
            return ["umask=0077"]
        return opts

    def mountpoint_of(self, spec: dict) -> str | None:
        """Where this spec's filesystem goes, after the whole layout arbitrated.

        Partitions get the arbitrated answer; an array or a logical volume is
        not a partition, was not in that list, and keeps what it declares.
        """
        if id(spec) in self.part_mounts:
            return self.part_mounts[id(spec)]
        return spec.get("mountpoint") or ("/" if spec.get("role") == "root" else None)

    def device_of(self, handle: str) -> str | None:
        """The device node a document handle names once disko has run."""
        names = partition_names(self.storage)
        for part in self.storage.get("partitions", []) or []:
            if part.get("id") == handle:
                return adopted_device(part) or partition_device(*names[id(part)])
        for array in self.raid:
            if array["name"] == handle:
                return f"/dev/md/{handle}"
        for crypt in self.encryption:
            if crypt["id"] == handle:
                return f"/dev/mapper/{handle}"
        for group in self.lvm:
            for vol in group.get("volumes", []):
                if vol.get("name") == handle:
                    return lv_device(group["name"], vol["name"])
        return None

    def spare_device(self, handle: str) -> str:
        device = self.device_of(handle)
        if device is None:
            refuse(f"raid spare {handle!r} does not resolve to a partition, array, "
                   "container or logical volume declared in this document")
            return "/dev/null"
        return device

    def owner_of(self, handle: str) -> tuple[str, str] | None:
        """The consumer of a handle, following an encryption container if present."""
        if crypt := self.luks_over.get(handle):
            return self.consumer.get(crypt["id"])
        return self.consumer.get(handle)

    def emit_content(self, lines: list, pad: str, handle: str, spec: dict,
                     pre_create: str = "") -> None:
        """Emit the content block for `handle`, wrapping in luks when declared."""
        crypt = self.luks_over.get(handle)
        inner_handle = crypt["id"] if crypt else handle
        if crypt:
            lines += [f"{pad}content = {{",
                      f"{pad}  type = \"luks\";",
                      f"{pad}  name = {nix_str(crypt['id'])};",
                      f"{pad}  settings.allowDiscards = true;"]
            fmt = (crypt.get("type") or "luks2").lower()
            lines.append(f"{pad}  extraFormatArgs = [ \"--type\" {nix_str(fmt)} ];")
            if key_path := luks_key_path(self.doc, crypt["id"]):
                # Without one of keyFile/passwordFile/settings.keyFile, disko's
                # askPassword defaults to true and the create script blocks on a
                # console prompt (disko lib/types/luks.nix:118-127, :202-231) —
                # an unattended install then hangs forever. passwordFile is the
                # right half of that pair here: disko reads it only while
                # formatting and opening (luks.nix:19-23) and never copies it
                # into the installed system, whereas settings.keyFile is passed
                # straight through to boot.initrd.luks.devices (luks.nix:338-357)
                # and would point the booted machine at a seed volume that is no
                # longer attached. The passphrase itself stays on the seed.
                lines.append(f"{pad}  passwordFile = {nix_str(key_path)};")
            else:
                warn(f"encryption '{crypt['id']}': no key material declared — disko "
                     "will prompt for the passphrase on the console, which no "
                     "unattended install can answer")
            for method in crypt.get("unlock", []) or []:
                if method not in ("passphrase", "keyfile"):
                    warn(f"encryption '{crypt['id']}': unlock method {method!r} must be "
                         "enrolled with systemd-cryptenroll after installation")
            pad += "  "
        self.emit_layer(lines, pad, inner_handle, spec, pre_create=pre_create)
        if crypt:
            lines.append(f"{pad[:-2]}}};")

    def emit_layer(self, lines: list, pad: str, handle: str, spec: dict,
                   pre_create: str = "") -> None:
        owner = self.consumer.get(handle)
        if owner and owner[0] == "lvm_pv":
            lines += [f"{pad}content = {{",
                      f"{pad}  type = \"lvm_pv\";",
                      f"{pad}  vg = {nix_str(owner[1])};",
                      f"{pad}}};"]
        elif owner and owner[0] == "mdraid":
            lines += [f"{pad}content = {{",
                      f"{pad}  type = \"mdraid\";",
                      f"{pad}  name = {nix_str(owner[1])};",
                      f"{pad}}};"]
        elif owner and owner[0] == "zfs":
            lines += [f"{pad}content = {{",
                      f"{pad}  type = \"zfs\";",
                      f"{pad}  pool = {nix_str(owner[1])};",
                      f"{pad}}};"]
        else:
            mp = self.mountpoint_of(spec)
            fs = self.fs_of(spec)
            fs_content(lines, pad, fs, mp, self.mount_options_of(spec),
                       spec.get("subvolumes", []), label=spec.get("label"),
                       where=spec_where(spec, handle),
                       extra_subvolumes=self.extra_subvolumes(spec, fs, mp),
                       pre_create=pre_create)

    def extra_subvolumes(self, spec: dict, fs, mountpoint) -> tuple:
        """Subvolumes disko must create that no `subvolumes[]` entry declares.

        snapper stores its history in a subvolume named .snapshots under the
        configured path and its NixOS module never creates one (services/misc/
        snapper.nix:50-52). It used to be made by a firstboot shell command, so
        the first timeline tick before that unit ran had nowhere to write;
        disko can create it at format time instead (lib/types/btrfs.nix:120,
        which accepts a subvolume with no mountpoint).
        """
        if fs != "btrfs" or mountpoint != "/":
            return ()
        if not ((self.storage.get("snapshots") or {}).get("enabled")):
            return ()
        subvolumes = spec.get("subvolumes") or []
        # Whatever ends up mounted at / is where snapper looks; fs_content
        # invents '@' when nothing covers the path, so mirror that choice.
        root = next((s["name"] for s in subvolumes if s.get("mountpoint") == "/"), "@")
        wanted = f"{root}/.snapshots"
        declared = {s.get("name") for s in subvolumes}
        return () if wanted in declared else (wanted,)


_TOPOLOGY: dict[int, tuple[dict, "Topology"]] = {}


def topology_for(doc: dict) -> Topology:
    """The one Topology for this document.

    render_disko, mount_table and luks_initrd_devices each used to build their
    own, so every diagnostic Topology raises was printed three times and the
    three passes could reach different conclusions from the same document.
    """
    storage = doc.get("storage", {}) or {}
    cached = _TOPOLOGY.get(id(storage))
    if cached is not None and cached[0] is storage:
        return cached[1]
    built = Topology(storage, doc)
    _TOPOLOGY[id(storage)] = (storage, built)
    return built


# mdadm(8), "Create mode": a level needs at least this many active members, and
# raid10 needs an even count on top of that. mdadm --create rejects the rest,
# and disko runs it from the format script — i.e. with the table already gone.
RAID_MIN_DEVICES = {0: 2, 1: 2, 5: 3, 6: 4, 10: 4}


def check_raid_geometry(name: str, level, count: int) -> None:
    """Refuse an array mdadm would not create."""
    if level not in RAID_MIN_DEVICES:
        refuse(f"raid {name!r}: level {level!r} is not one of 0, 1, 5, 6, 10 "
               "(schema.md §6.4) — disko passes it to `mdadm --create --level=` "
               "verbatim and the array would never be built")
        return
    minimum = RAID_MIN_DEVICES[level]
    if count < minimum:
        refuse(f"raid {name!r}: level {level} needs at least {minimum} active "
               f"devices and {count} are declared (mdadm(8), Create mode); "
               "spares do not count towards the array width")
    elif level == 10 and count % 2:
        refuse(f"raid {name!r}: level 10 needs an even number of active devices "
               f"and {count} are declared")


def render_mdadm(topology: Topology, out: list) -> None:
    """disko.devices.mdadm — one entry per LIS raid array."""
    if not topology.raid:
        return
    out.append("    mdadm = {")
    for array in topology.raid:
        name = array["name"]
        devices = array.get("devices", []) or []
        missing = [d for d in devices if d not in topology.specs
                   and d not in {c["id"] for c in topology.encryption}]
        for dev in missing:
            # Not a warning any more: disko writes each resolved member into
            # $disko_devices_dir/raid_<name> and hands the file to `mdadm
            # --create`, so a handle that resolves to nothing builds a narrower
            # array than the document asked for — silently, and only on the
            # machine being installed.
            refuse(f"raid {name!r}: device handle {dev!r} does not resolve to a "
                   "partition or encryption container declared in this document")
        check_raid_geometry(name, array.get("level"), len(devices))
        out += [f"      {nix_str(name)} = {{",
                "        type = \"mdadm\";",
                f"        level = {array['level']};"]
        if spares := topology.raid_spares.get(name):
            # A spare cannot be declared inline: disko sizes the array with
            # `--raid-devices="$(wc -l "$disko_devices_dir"/raid_<name>)"`
            # (lib/types/mdadm.nix:65-68), which counts every member it was
            # given, so listing the spare there built a wider array instead of
            # a narrower one with a hot spare. `mdadm --add` after creation is
            # the documented way to attach one (mdadm(8), "Grow mode … --add"),
            # and disko exposes postCreateHook on every node
            # (lib/default.nix:438-441) to run it in the right place.
            adds = [f"mdadm --add /dev/md/{name} "
                    f"{topology.spare_device(handle)}" for handle in spares]
            body = "\n".join(f"          {cmd}" for cmd in adds)
            out += ["        postCreateHook = ''",
                    body,
                    "        '';"]
            warn(f"raid '{name}': {len(spares)} hot spare(s) are attached with "
                 "`mdadm --add` from a disko postCreateHook after the array is "
                 "created, not declared as part of it — disko counts every "
                 "declared member as active (lib/types/mdadm.nix:65-68)")
        topology.emit_content(out, "        ", name, array)
        out.append("      };")
    out.append("    };")


def render_zpools(topology: Topology, doc: dict, out: list) -> None:
    """disko.devices.zpool — one pool per set of `fs: zfs` volumes."""
    if not topology.zpools:
        return
    out.append("    zpool = {")
    for pool, info in topology.zpools.items():
        members = info["members"]
        mode = ""
        for array in topology.raid:
            if array["name"] in members:
                mode = ""  # already mirrored/striped by mdadm below the pool
        if len(members) > 1:
            mode = "mirror"
            warn(f"zpool '{pool}': {len(members)} vdevs assembled as a mirror; "
                 "declare storage.raid[] for other layouts")
        out += [f"      {nix_str(pool)} = {{",
                "        type = \"zpool\";",
                f"        mode = {nix_str(mode)};",
                "        rootFsOptions = {",
                "          compression = \"zstd\";",
                "          \"com.sun:auto-snapshot\" = \"false\";",
                "        };",
                "        options.ashift = \"12\";",
                "        datasets = {"]
        for spec in info["datasets"]:
            mp = topology.mountpoint_of(spec)
            base = (spec.get("id") or spec.get("name") or "root")
            if any(s["mountpoint"] == mp for s in spec.get("subvolumes", []) or []):
                # One of the declared children already occupies that path, the
                # same rule fs_content() applies to btrfs. Mounting the parent
                # there too defines fileSystems."/" twice, which is an
                # evaluation error rather than the last one winning.
                mp = None
            if mp:
                out += [f"          {nix_str(base)} = {{",
                        "            type = \"zfs_fs\";",
                        f"            mountpoint = {nix_str(mp)};"]
                if spec.get("mount_options"):
                    out.append(f"            options.mountpoint = \"legacy\";")
                out.append("          };")
            for sub in spec.get("subvolumes", []) or []:
                name = zfs_child(base, sub["name"])
                out += [f"          {nix_str(name)} = {{",
                        "            type = \"zfs_fs\";",
                        f"            mountpoint = {nix_str(sub['mountpoint'])};",
                        "          };"]
        out += ["        };", "      };"]
    out.append("    };")


def _shape(value) -> str:
    """The JSON type name of a value, for a refusal that names a wrong shape.

    The tracker wraps containers, so the bare class name would report
    `TrackedList` at a reader who wrote a JSON array.
    """
    return type(value).__name__.replace("Tracked", "").lower()


def adopted(part: dict) -> bool:
    """Whether a partition entry asks to adopt rather than to create.

    Membership, not truthiness: `"existing": {}` is a malformed adoption, and
    reading it as "creates normally" turned the request to keep a partition
    into an order to format it, with no diagnostic at all.
    """
    return "existing" in part


# resolve_adoptions() fills these two under --apply and leaves them empty
# everywhere else: id(partition entry) -> the live partition it adopts, and disk
# id -> that disk's whole probed table. schema.md §20.8 puts the resolution at
# apply time because a match names a partition on the machine; a translation
# with no machine in front of it has no geometry to pin, which is what makes
# adoption refuse there instead of guessing one.
ADOPTED: dict[int, dict] = {}
PROBED: dict[str, dict] = {}
PROBE_RAN = False
# Set by adoption_plan(): this run has to keep something, so main() must not
# hand disko a mode whose first stage clears the partition table.
PRESERVING = False

# GPT partitions are aligned to 1 MiB by every tool that writes one, and sgdisk
# defaults to it; new partitions placed into free space follow the same rule so
# the table this translator writes looks like the table it found.
ALIGN_BYTES = 1024 * 1024

GUID = re.compile(r"[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")

MATCH_KEYS = ("partition", "label", "uuid", "fs")


def _sysfs_int(path: pathlib.Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def lsblk_identity(path: str) -> dict[str, dict] | str:
    """Each partition's GPT and filesystem identity, or why it could not be read.

    lsblk only reads. That is the point: the probe that exists to preserve a
    foreign layout must not be able to alter it, so nothing here — and nothing
    in probe_disk() — opens the disk for writing or shells a partitioner.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["lsblk", "-P", "-o",
             "NAME,PATH,PARTUUID,PARTLABEL,PARTTYPE,FSTYPE,LABEL,UUID", path],
            capture_output=True, text=True, check=True).stdout
    except Exception as err:  # noqa: BLE001 — no lsblk means no adoption
        return f"lsblk cannot read {path}: {err}"
    table: dict[str, dict] = {}
    for line in out.splitlines():
        fields = dict(re.findall(r'([A-Z]+)="([^"]*)"', line))
        node = fields.get("PATH") or (f"/dev/{fields['NAME']}"
                                      if fields.get("NAME") else "")
        if not node:
            continue
        table[node] = {"uuid": fields.get("PARTUUID", "").lower(),
                       "label": fields.get("PARTLABEL", ""),
                       "typecode": fields.get("PARTTYPE", "").upper(),
                       "fs": fields.get("FSTYPE", "").lower(),
                       "fslabel": fields.get("LABEL", ""),
                       "fsuuid": fields.get("UUID", "").lower(),
                       "attributes": None}
    # A separate call, because an lsblk too old to know PARTFLAGS fails the whole
    # -o list: asked on its own, its absence costs the attribute flags and a
    # warning rather than the entire probe.
    try:
        flags = subprocess.run(["lsblk", "-P", "-o", "PATH,PARTFLAGS", path],
                               capture_output=True, text=True, check=True).stdout
    except Exception:  # noqa: BLE001 — reported by the caller as unknown flags
        return table
    for line in flags.splitlines():
        fields = dict(re.findall(r'([A-Z]+)="([^"]*)"', line))
        entry = table.get(fields.get("PATH", ""))
        if entry is None:
            continue
        raw = fields.get("PARTFLAGS", "")
        try:
            # GPT attributes are a 64-bit field; disko takes the set bit numbers
            # (lib/types/gpt.nix, `attributes`).
            value = int(raw, 0) if raw else 0
        except ValueError:
            continue
        entry["attributes"] = [bit for bit in range(64) if value >> bit & 1]
    return table


def probe_disk(path: str) -> dict | str:
    """One disk's live partition table, read through sysfs and lsblk only."""
    base = os.path.realpath(path).rsplit("/", 1)[-1]
    root = pathlib.Path("/sys/class/block") / base
    if not root.is_dir():
        return f"{path} is not a block device on this machine ({root} is absent)"
    logical = _sysfs_int(root / "queue" / "logical_block_size") or 512
    span = _sysfs_int(root / "size")
    if not span:
        return f"{path} reports no size in sysfs"
    identity = lsblk_identity(path)
    if isinstance(identity, str):
        return identity
    sectors = span * 512 // logical
    parts = []
    for child in sorted(root.iterdir()):
        number = _sysfs_int(child / "partition")
        if not number:
            continue
        start, length = _sysfs_int(child / "start"), _sysfs_int(child / "size")
        if start is None or not length:
            return f"partition {number} of {path} reports no geometry in sysfs"
        # sysfs counts in 512-byte units whatever the device's logical sector
        # is, and sgdisk counts in logical sectors: on a 4Kn disk the two are a
        # factor of eight apart, and a start passed through unconverted would
        # place a new partition inside an existing one.
        start, length = start * 512 // logical, length * 512 // logical
        node = f"/dev/{child.name}"
        parts.append(dict({"number": number, "device": node, "start": start,
                           "end": start + length - 1, "sectors": length},
                          **identity.get(node, {})))
    parts.sort(key=lambda p: p["number"])
    # The secondary GPT sits at the end of the disk — one header plus a 128-entry
    # array — so the last sector a partition may occupy is not the last sector.
    reserve = -(-128 * 128 // logical) + 1
    return {"path": path, "logical": logical, "sectors": sectors,
            "first": 1 + reserve, "last": sectors - 1 - reserve, "parts": parts}


def describe_probe(probe: dict) -> str:
    """The live table as a diagnostic names it back to the operator."""
    return ", ".join(
        f"{p['number']}:{p.get('fs') or 'no filesystem'}"
        f":{p.get('label') or p.get('fslabel') or 'unnamed'}"
        for p in probe["parts"]) or "no partitions"


def match_existing(match: dict, probe: dict, where: str) -> dict | None:
    """The one live partition an `existing.match` names, or None with a refusal.

    schema.md §20.8: a match MUST resolve to exactly one partition. `label` and
    `uuid` are compared against both the GPT entry and the filesystem inside it,
    because the document says only "label" and both readings are in use; where
    the two readings disagree the candidate set has more than one member and the
    ambiguity is refused rather than resolved on the operator's behalf.
    """
    if unknown := sorted(set(match) - set(MATCH_KEYS)):
        refuse(f"{where}: storage.partitions[].existing.match {unknown} — the "
               "schema allows partition, label, uuid and fs only (schema.md §6.2)")
        return None
    if not match:
        refuse(f"{where}: storage.partitions[].existing.match is empty — with no "
               "selector every partition on the disk matches, and schema.md "
               "§20.8 requires exactly one")
        return None
    candidates = list(probe["parts"])
    if "partition" in match:
        number = match["partition"]
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            refuse(f"{where}: existing.match.partition {number!r} is not a "
                   "partition number (a positive integer)")
            return None
        candidates = [p for p in candidates if p["number"] == number]
    if "label" in match:
        want = match["label"]
        candidates = [p for p in candidates
                      if want in (p.get("label"), p.get("fslabel"))]
    if "uuid" in match:
        want = str(match["uuid"]).lower()
        candidates = [p for p in candidates
                      if want in (p.get("uuid"), p.get("fsuuid"))]
    if "fs" in match:
        want = str(match["fs"]).lower()
        candidates = [p for p in candidates if (p.get("fs") or "") == want]
    if len(candidates) == 1:
        return candidates[0]
    asked = ", ".join(f"{key}={match[key]!r}" for key in MATCH_KEYS if key in match)
    refuse(f"{where}: existing.match ({asked}) resolves to {len(candidates)} "
           f"partitions on {probe['path']}, not one (schema.md §20.8). That disk "
           f"carries: {describe_probe(probe)}")
    return None


def resolve_adoptions(doc: dict) -> None:
    """Resolve every `existing.match` against the live disks. --apply only.

    Everything the layout needs — the GPT index, the exact first and last
    sector, the partition GUID and the partition name to keep — comes from here.
    A run that cannot read a disk adopts nothing on it and says so.
    """
    global PROBE_RAN, PRESERVING

    PROBE_RAN = True
    storage = doc.get("storage") or {}
    partitions = storage.get("partitions") or []
    if not any(adopted(part) for part in partitions):
        return
    # Set on the *declaration*, not on a successful resolution. Every way an
    # adoption can fail below ends in a refusal, and a refusal is fatal unless
    # the operator passes --lenient — but a lenient run of a document that asked
    # to keep a partition must still not be the run that clears the table.
    PRESERVING = True
    disks = storage.get("disks", []) or (doc.get("target", {}) or {}).get("disks", [])
    paths = {disk["id"]: (disk.get("match", {}) or {}).get("path")
             for disk in disks if disk.get("id")}
    for disk_id in sorted({part.get("disk") for part in partitions
                           if adopted(part) and part.get("disk")}):
        path = paths.get(disk_id)
        if not path:
            continue        # a disk with no match.path is refused in render_disko
        probe = probe_disk(path)
        if isinstance(probe, str):
            refuse(f"disk '{disk_id}': storage.partitions[].existing needs the "
                   f"live partition table of {path}, and this machine cannot "
                   f"produce it: {probe}")
            continue
        PROBED[disk_id] = probe

    for part in partitions:
        if not adopted(part):
            continue
        where = f"partition {part.get('id') or part.get('role') or '?'!r}"
        existing = part.get("existing")
        if not isinstance(existing, dict) or not isinstance(existing.get("match"), dict):
            continue        # a malformed block is refused in refuse_adoption()
        probe = PROBED.get(part.get("disk"))
        if probe is None:
            continue
        if "resize" in existing:
            # Not registered as an adoption: refuse_adoption() states why a
            # resize cannot be honoured, and leaving the entry unresolved keeps
            # the partition out of the emitted layout entirely, so a --lenient
            # run cannot end up moving a boundary the shrink never reached.
            continue
        found = match_existing(existing["match"], probe, where)
        if found is None:
            continue
        clash = [other for other, taken in ADOPTED.items()
                 if taken["device"] == found["device"]]
        if clash:
            refuse(f"{where}: existing.match resolves to {found['device']}, which "
                   "another partition entry in this document already adopts — two "
                   "entries describing one partition cannot both be honoured")
            continue
        ADOPTED[id(part)] = dict(found, disk=part.get("disk"))
        consume(existing["match"])


def refuse_adoption(part: dict, where: str) -> None:
    """Turn down one `existing` block that adoption could not resolve.

    Three cases reach here, and they fail for different reasons: a malformed
    block, a translation with no machine to read, and `resize` — which no amount
    of probing makes safe.
    """
    existing = part.get("existing") or {}
    if not isinstance(existing, dict):
        # schema.json types `existing` as an object; a scalar or a list is a
        # schema error, and reading it as one would crash here rather than
        # name the partition the document wanted kept.
        refuse(f"{where}: storage.partitions[].existing must be an object, "
               f"not {_shape(existing)} — a malformed adoption cannot be "
               "resolved against the disk, nor reported against its declared "
               "leaves (schema.md §6.2)")
        return
    match = existing.get("match")
    match = match if isinstance(match, dict) else {}
    leaves = [f"match.{key}" for key in sorted(match)]
    leaves += [key for key in ("format", "resize") if key in existing]
    if not PROBE_RAN:
        refuse(f"{where}: storage.partitions[].existing "
               f"({', '.join(leaves) or 'empty'}) — adopting a partition means "
               "describing it to disko exactly as it already is: its GPT index, "
               "its first and last sector, its partition GUID and its partition "
               "name, because disko's create step is `if ! sgdisk "
               "--new=<index>:<start>:<end> …; then sgdisk --change-name=<index> "
               "--typecode --partition-guid fi` (lib/types/gpt.nix:315-318) and "
               "an entry pinned to anything else renames and retypes whatever "
               "occupies that index. None of it is knowable from the document, "
               "so this translator resolves an adoption only under --apply, "
               "where it reads the live table (schema.md §6.2, §20.8: a match "
               "MUST resolve to exactly one partition, at apply time)")
    elif "resize" not in existing:
        # The reason is already on the record — the match resolved to no
        # partition or to several, the probe failed, or the disk's table could
        # not be described with this entry in it. Saying so again here would
        # duplicate it; saying nothing at all would drop the field, so this
        # states the outcome and points at the reason.
        refuse(f"{where}: storage.partitions[].existing "
               f"({', '.join(leaves) or 'empty'}) was not adopted — the step "
               "that declined it named the reason above, and nothing on this "
               "disk is created while an adoption on it is unresolved")
    if existing.get("resize"):
        refuse(f"{where}: storage.partitions[].existing.resize "
               f"{existing['resize']!r} — nothing in disko resizes anything: its "
               "whole lib/ mentions no resize2fs, ntfsresize or xfs_growfs, and "
               "the filesystem must be shrunk before the partition is, in that "
               "order, or the tail of the data is outside the partition that "
               "holds it. A preCreateHook could shell ntfsresize out, but this "
               "translator has no way to verify the shrink succeeded before "
               "sgdisk moves the boundary, and a partial shrink is the one "
               "failure that cannot be undone (schema.md §6.2: an applier that "
               "cannot resize the filesystem MUST fail)")
    # The refusals are the answer for every leaf under `existing`; without
    # this the birth certificate also reports each one as an unnoticed field,
    # which reads as a second, softer verdict.
    consume(part["existing"])


def size_sectors(size, logical: int, where: str) -> int | None:
    """A LIS absolute size in logical sectors. None means "the rest"."""
    if size in (None, "rest", "100%"):
        return None
    if isinstance(size, str) and re.fullmatch(r"[0-9]{1,3}%", size):
        refuse(f"{where}: size {size!r} on a disk that keeps an existing "
               "partition — what is available is the free space, not the disk, "
               "so a percentage of the disk is not what the document asked for; "
               "use an absolute size or \"rest\"")
        return None
    mib = size_mib(size, where)
    return None if mib is None else mib * 1024 * 1024 // logical


def free_regions(probe: dict, align: int) -> list[tuple[int, int]]:
    """The gaps in a probed table, aligned, largest first."""
    cursor = probe["first"]
    gaps = []
    for part in sorted(probe["parts"], key=lambda p: p["start"]):
        if part["start"] > cursor:
            gaps.append((cursor, part["start"] - 1))
        cursor = max(cursor, part["end"] + 1)
    if cursor <= probe["last"]:
        gaps.append((cursor, probe["last"]))
    out = []
    for start, end in gaps:
        start += (-start) % align
        if start <= end:
            out.append((start, end))
    out.sort(key=lambda gap: gap[1] - gap[0], reverse=True)
    return out


def adoption_layer(part: dict, topology: Topology) -> str | None:
    """The disko content type an adopted partition would be built into.

    Only a filesystem, a swap area or nothing can be adopted. Every other
    content type creates unconditionally or nearly so: `mdadm --create … --force`
    runs whenever /dev/md/<name> is absent (lib/types/mdadm.nix:64-71) and
    `zpool create` the same, while luks re-runs `luksFormat` on any device that
    is not already LUKS and lvm_pv skips `pvcreate` on a device that has any
    filesystem at all — which leaves the volume group with no physical volume.
    """
    handle = part.get("id") or ""
    if crypt := topology.luks_over.get(handle):
        return f"the LUKS container '{crypt['id']}' declared over it"
    if owner := topology.consumer.get(handle):
        kind, name = owner
        return {"mdraid": f"the raid array '{name}'",
                "lvm_pv": f"the volume group '{name}'",
                "zfs": f"the zpool '{name}'"}.get(kind, f"{kind} '{name}'")
    if handle in topology.spare_handles:
        return "a raid hot spare, which mdadm --add writes a superblock onto"
    return None


def adopted_format_hook(part: dict, probed: dict, topology: Topology) -> str:
    """Reconcile `existing.format` with what the adopted partition already holds.

    disko guards every mkfs with `if ! (blkid <dev> | grep -q 'TYPE=')`
    (lib/types/filesystem.nix _create; swap and btrfs the same shape), so a
    filesystem that is already there is never re-made — which is precisely
    `format: false`. `format: true` is the other half of that guard: the
    signature is removed first, from the content's own preCreateHook, which runs
    in the same subshell immediately before it (lib/default.nix:470-473).
    """
    existing = part.get("existing") or {}
    where = f"partition {part.get('id') or part.get('role')!r}"
    fs = topology.fs_of(part)
    if existing.get("format"):
        if fs in (None, "none"):
            refuse(f"{where}: existing.format: true asks for the adopted "
                   "partition to be re-made, and the entry declares no `fs` to "
                   "re-make it as (schema.md §6.2)")
            return ""
        warn(f"{where}: existing.format: true — the {probed.get('fs') or 'empty'} "
             f"filesystem on {probed['device']} is replaced with a fresh {fs}. "
             "The partition itself, its GPT name and its partition GUID are kept")
        return 'wipefs --all "$device"'
    if fs in (None, "none"):
        return ""
    if not probed.get("fs"):
        warn(f"{where}: adopted {probed['device']} holds no filesystem and the "
             f"entry declares fs {fs!r} with existing.format false — disko's mkfs "
             "guard passes on a partition with no signature, so one is created "
             "there; nothing is overwritten, because there was nothing to keep")
        return ""
    if probed["fs"] != fs:
        refuse(f"{where}: adopted {probed['device']} holds a {probed['fs']} "
               f"filesystem and the entry declares fs {fs!r} with "
               "existing.format false. disko will not re-make a filesystem that "
               f"is already there, so the mount would be `mount -t {fs}` over "
               f"{probed['fs']} and the install would stop at it — declare "
               f"existing.format: true to replace it, or fs: {probed['fs']!r} to "
               "use it as it stands")
    return ""


def adoption_plan(disk_id: str, disk_parts: list[dict], names: dict,
                  topology: Topology, bios_grub: str | None,
                  wipe: bool) -> list[dict] | None:
    """How one disk's GPT must be described so disko keeps what is on it.

    Every partition on the disk gets an entry, not only the adopted ones. disko
    addresses a partition by its position in the priority-sorted list
    (`_index`, lib/types/gpt.nix:247), and its create step falls back to
    `sgdisk --change-name=<index> --typecode --partition-guid` when `--new`
    cannot have the range — so an entry landing on an index that something else
    occupies renames and retypes *that* partition, and the mkfs and the mount
    that follow are then aimed at it. Declaring the untouched partitions as
    pinned placeholders is what keeps the indices lined up.

    Returns None when this disk adopts nothing, which leaves the ordinary
    create-everything path in charge of it.
    """
    adoptions = {id(part): ADOPTED[id(part)]
                 for part in disk_parts if id(part) in ADOPTED}
    if not adoptions:
        return None
    if any(adopted(part) and id(part) not in adoptions for part in disk_parts):
        # One entry on this disk asked to adopt and could not be resolved, so
        # the indices this plan would pin are not the whole table. Hand the disk
        # back: the ordinary path refuses every adopted entry on it by name.
        return None
    probe = PROBED.get(disk_id)
    if probe is None:
        return None             # refused in resolve_adoptions()
    live, logical = probe["parts"], probe["logical"]
    align = max(ALIGN_BYTES // logical, 1)
    numbers = [p["number"] for p in live]
    if numbers != list(range(1, len(numbers) + 1)):
        refuse(f"disk '{disk_id}': the partition numbers on {probe['path']} are "
               f"{numbers}, which leaves an unused slot. disko addresses a "
               "partition by its position in the priority-sorted list, so a "
               "table with a gap cannot be described without moving everything "
               "after the gap into a different slot (lib/types/gpt.nix:247)")
        return None
    for part in disk_parts:
        if id(part) not in adoptions:
            continue
        if layer := adoption_layer(part, topology):
            refuse(f"partition {part.get('id') or part.get('role')!r}: adopted "
                   f"through `existing`, and {layer} is built on top of it. "
                   "That layer's create step is not held back by an existing "
                   "signature the way a filesystem's mkfs is — `mdadm --create "
                   "… --force` and `zpool create` run whenever the array or pool "
                   "is absent, and luks re-runs luksFormat on any device that is "
                   "not already LUKS — so the partition would be adopted and "
                   "then overwritten. Adopt a partition into a filesystem, a "
                   "swap area or nothing at all")
            return None

    claimed = {info["number"]: part for part in disk_parts
               if (info := adoptions.get(id(part))) is not None}
    taken_names = {names[id(part)][1] for part in disk_parts}
    if bios_grub:
        taken_names.add(bios_grub)

    entries: list[dict] = []
    for probed in live:
        part = claimed.get(probed["number"])
        if not GUID.fullmatch(probed.get("uuid") or ""):
            refuse(f"disk '{disk_id}': partition {probed['number']} of "
                   f"{probe['path']} has no GPT partition GUID "
                   f"({probed.get('uuid') or 'none'!r}) — an MS-DOS table has no "
                   "per-partition GUID and disko writes GPT, so the partition "
                   "cannot be described back to it unchanged")
            return None
        if not (probed.get("typecode") or "") or not GUID.fullmatch(probed["typecode"]):
            refuse(f"disk '{disk_id}': partition {probed['number']} of "
                   f"{probe['path']} reports type code "
                   f"{probed.get('typecode') or 'none'!r}, which is not a GPT "
                   "partition type GUID")
            return None
        if part is None:
            name = f"lis-keep-{probed['number']}"
            while name in taken_names:
                name = "lis_" + name
            taken_names.add(name)
            if probed.get("fs"):
                held = (f"partition {probed['number']} of {probe['path']} holds a "
                        f"{probed['fs']} filesystem and no `existing` entry in "
                        "this document adopts it")
                if wipe:
                    warn(f"disk '{disk_id}': {held}. Because another partition on "
                         "this disk is adopted, disko runs without its destroy "
                         "stage, so it is preserved anyway — storage.wipe: true "
                         "does not reach it")
                else:
                    refuse(f"disk '{disk_id}': {held} — schema.md §6.1 requires "
                           "an applier to fail with storage.wipe: false when an "
                           "owned disk holds data no adoption accounts for. Add "
                           "an `existing` entry naming it (a match with no `fs` "
                           "and no mountpoint keeps it untouched), or set "
                           "storage.wipe: true")
            hook = ""
        else:
            name = names[id(part)][1]
            hook = adopted_format_hook(part, probed, topology)
            if part.get("size"):
                warn(f"partition {part.get('id') or part.get('role')!r}: size "
                     f"{part['size']!r} is not applied to an adopted partition — "
                     f"{probed['device']} keeps the "
                     f"{probed['sectors'] * logical // 1024 ** 2} MiB it already "
                     "has. Changing it is existing.resize, which this translator "
                     "refuses")
        if probed.get("attributes") is None:
            # disko's create resets the attribute field before setting the bits
            # it was given (`--attributes=<index>:=:0`), so a flag it was not
            # told about is cleared: on a Windows recovery partition that is the
            # difference between hidden and given a drive letter.
            warn(f"disk '{disk_id}': the GPT attribute flags of partition "
                 f"{probed['number']} on {probe['path']} could not be read "
                 "(lsblk reported no PARTFLAGS), and disko's create resets the "
                 "attribute field of every partition it names — any flag that "
                 "partition carries is cleared")
        entries.append({"name": name, "priority": probed["number"],
                        "start": str(probed["start"]), "end": str(probed["end"]),
                        "type": probed["typecode"], "label": probed.get("label") or "",
                        "uuid": probed["uuid"], "part": part, "probed": probed,
                        "attributes": probed.get("attributes") or [], "hook": hook})

    new = [part for part in disk_parts if id(part) not in adoptions]
    if bios_grub:
        new.insert(0, None)     # the 1 MiB BIOS boot partition, synthesised
    if new:
        regions = free_regions(probe, align)
        if not regions:
            refuse(f"disk '{disk_id}': {len(new)} partition(s) to create and no "
                   f"free space left on {probe['path']} — its "
                   f"{len(live)} existing partitions fill it")
            return None
        start, limit = regions[0]
        for spare_start, spare_end in regions[1:]:
            if (spare_end - spare_start + 1) * logical >= 1024 ** 3:
                warn(f"disk '{disk_id}': {(spare_end - spare_start + 1) * logical // 1024 ** 2} "
                     f"MiB of free space between the existing partitions is left "
                     "unused; the new partitions are laid out consecutively in "
                     "the largest free region so their GPT indices follow the "
                     "ones already there")
        cursor = start
        for position, part in enumerate(new):
            if part is None:
                name, want, spec_size = bios_grub, ALIGN_BYTES // logical, "1MiB"
            else:
                name = names[id(part)][1]
                where = f"partition {part.get('id') or part.get('role') or '?'!r}"
                spec_size = part.get("size")
                want = size_sectors(spec_size, logical, where)
            if want is None:
                if part is not new[-1]:
                    refuse(f"partition {part.get('id') or part.get('role')!r}: "
                           f"size {spec_size!r} takes what is left of the free "
                           "region, so nothing declared after it on this disk has "
                           "anywhere to go — put it last, or give it a size")
                    return None
                end = limit
            else:
                end = cursor + want - 1
            if end > limit or cursor > limit:
                refuse(f"disk '{disk_id}': partition {name!r} needs "
                       f"{(end - cursor + 1) * logical // 1024 ** 2} MiB at sector "
                       f"{cursor}, and the free region on {probe['path']} ends at "
                       f"{limit} — {(limit - cursor + 1) * logical // 1024 ** 2} "
                       "MiB is left. The existing partitions cannot be moved "
                       "aside without destroying them")
                return None
            entries.append({"name": name, "priority": len(live) + 1 + position,
                            "start": str(cursor), "end": str(end),
                            "type": "EF02" if part is None else None,
                            "label": partition_label(disk_id, name),
                            "uuid": None, "part": part, "probed": None,
                            "attributes": [], "hook": ""})
            cursor = end + 1
            cursor += (-cursor) % align

    return entries


def emit_partition_content(out: list, pad: str, part: dict, topology: Topology,
                           pre_create: str = "") -> None:
    """The `content` block under one partition entry, ESP convention included.

    One function for both layouts — the create-everything one and the adopting
    one — so an ESP is described the same way whether disko is making it or
    finding it.
    """
    where = f"partition {part.get('id') or part.get('role') or '?'!r}"
    if part.get("role") == "esp":
        out += [f"{pad}content = {{",
                f"{pad}  type = \"filesystem\";"]
        if pre_create:
            out.append(f"{pad}  preCreateHook = {nix_str(pre_create)};")
        out.append(f"{pad}  format = \"vfat\";")
        if label := part.get("label"):
            if args := label_args("vfat", label, where):
                out.append(f"{pad}  extraArgs = {nix_list(args)};")
        if mountpoint := topology.mountpoint_of(part):
            # An ESP nothing mounts is a refusal in partition_mountpoints(), not
            # a partial attribute set: disko's filesystem type wants a real path.
            out.append(f"{pad}  mountpoint = {nix_str(mountpoint)};")
        out += [f"{pad}  mountOptions = {nix_list(topology.mount_options_of(part))};",
                f"{pad}}};"]
        return
    topology.emit_content(out, pad, part.get("id", ""), part, pre_create=pre_create)


def render_disko(doc: dict) -> str:
    storage = doc.get("storage")
    if not storage:
        raise SystemExit("error: document has no storage section — nothing to generate")
    partitions = storage.get("partitions", [])
    lvm = storage.get("lvm", []) or []
    topology = topology_for(doc)
    names = partition_names(storage)

    if not storage.get("wipe", False) and not ADOPTED:
        # A document that adopts nothing tells this translator nothing about
        # what is on the disk, so it does not read one: the probe belongs to
        # `existing`, which is what §6.1's "accounted for by an adoption"
        # clause describes. With an adoption resolved, the mode below is
        # format,mount and this refusal does not apply.
        refuse("storage.wipe: false — this translator runs disko in "
               "destroy,format,mount, whose destroy stage clears the "
               "partition table of every declared disk before anything is "
               "created. §6.1 also asks the applier to fail on data not "
               "accounted for by an `existing` adoption, and with no adoption "
               "declared nothing here reads the disk to find that data, so a "
               "preserving install cannot be honoured either way")

    firmware = ((doc.get("target", {}) or {}).get("firmware") or "auto").lower()
    # A 1 MiB BIOS boot partition is only meaningful when GRUB is installed to
    # the MBR; on UEFI it is dead space. Its name must not collide with a
    # partition the document itself declares (LIS ids are free-form).
    declared = {p.get("id") for p in partitions}
    bios_grub = None
    if firmware == "bios":
        bios_grub = "bios_grub"
        while bios_grub in declared:
            bios_grub = "lis_" + bios_grub

    disks = storage.get("disks", []) or (doc.get("target", {}) or {}).get("disks", [])
    disk_paths = {}
    for disk in disks:
        match_selectors(disk)
        path = (disk.get("match", {}) or {}).get("path")
        if not path:
            refuse(f"disk '{disk['id']}': no match.path — disko needs a device path "
                   "(it cannot evaluate LIS match rules)")
            continue
        disk_paths[disk["id"]] = path

    out = ["# Generated from a LIS document by lis2nixos (default translator).",
           "{", "  disko.devices = {", "    disk = {"]
    for disk in disks:
        path = disk_paths.get(disk["id"])
        if not path:
            continue
        out += [f"      {nix_str(disk['id'])} = {{",
                "        type = \"disk\";",
                f"        device = {nix_str(path)};",
                "        content = {", "          type = \"gpt\";",
                "          partitions = {"]
        disk_parts = [p for p in partitions if p.get("disk") == disk["id"]]
        plan = adoption_plan(disk["id"], disk_parts, names, topology,
                             bios_grub, storage.get("wipe", False))
        if plan is not None:
            # Every partition on the disk is described, not only the adopted
            # ones: disko numbers a partition by its position in this list, and
            # an entry landing on an occupied index renames and retypes what is
            # already there. See adoption_plan().
            for entry in plan:
                out += [f"            {nix_str(entry['name'])} = {{",
                        f"              priority = {entry['priority']};",
                        f"              start = {nix_str(entry['start'])};",
                        f"              end = {nix_str(entry['end'])};",
                        f"              label = {nix_str(entry['label'])};"]
                if entry["type"]:
                    out.append(f"              type = {nix_str(entry['type'])};")
                if entry["attributes"]:
                    # Pinned for the same reason as the GUID: disko's create
                    # writes `--attributes=<index>:=:0` before setting the bits
                    # it knows about, so a flag not restated here is cleared.
                    bits = " ".join(str(bit) for bit in entry["attributes"])
                    out.append(f"              attributes = [ {bits} ];")
                if entry["uuid"]:
                    # Pinned so sgdisk's fallback writes back the GUID the
                    # partition already carries: anything referring to this
                    # partition by PARTUUID — a Windows BCD, another fstab —
                    # goes on resolving. It also makes disko address the
                    # partition by /dev/disk/by-partuuid/…, which is what
                    # partition_device() emits for an adopted entry.
                    out.append(f"              uuid = {nix_str(entry['uuid'])};")
                if entry["part"] is None and entry["probed"] is not None:
                    out += ["              # Not declared in the document and not "
                            "adopted: named here only so",
                            "              # the partitions after it keep their "
                            "GPT index. Nothing is",
                            "              # created on it, and nothing mounts it.",
                            "              content = null;"]
                elif entry["part"] is None:
                    out.append("              content = null;")
                else:
                    emit_partition_content(out, "              ", entry["part"],
                                           topology, pre_create=entry["hook"])
                out.append("            };")
            out += ["          };", "        };", "      };"]
            continue
        if bios_grub:
            out += [f"            {nix_str(bios_grub)} = {{",
                    "              size = \"1M\";",
                    "              type = \"EF02\";",
                    "              priority = 1;",
                    "            };"]
        for part in disk_parts:
            where = f"partition {part.get('id') or part.get('role') or '?'!r}"
            if adopted(part):
                # Resolved adoptions never reach here — adoption_plan() has the
                # whole disk. What is left is a block no probe could resolve.
                refuse_adoption(part, where)
                continue
            disk_id, name = names[id(part)]
            out.append(f"            {nix_str(name)} = {{")
            if part.get("size"):
                out.append("              size = "
                           f"{nix_str(disko_size(part['size'], where))};")
            # The GPT name is stated rather than left to disko's default,
            # because hardware.nix derives /dev/disk/by-partlabel/… from it and
            # the two must agree even when the default would have been hashed.
            out.append(f"              label = "
                       f"{nix_str(partition_label(disk_id, name))};")
            if part.get("role") == "esp":
                out.append("              type = \"EF00\";")
            emit_partition_content(out, "              ", part, topology)
            out.append("            };")
        out += ["          };", "        };", "      };"]
    out.append("    };")

    render_mdadm(topology, out)

    if lvm:
        out.append("    lvm_vg = {")
        for group in lvm:
            out += [f"      {nix_str(group['name'])} = {{",
                    "        type = \"lvm_vg\";", "        lvs = {"]
            for vol in group.get("volumes", []):
                where = f"lvm volume {group['name']}/{vol.get('name')!r}"
                out.append(f"          {nix_str(vol['name'])} = {{")
                # percent=True: lvcreate takes a share of the group, which
                # disko spells by appending FREE (lib/types/lvm_vg.nix:130-135).
                out.append("            size = "
                           f"{nix_str(disko_size(vol.get('size', 'rest'), where, percent=True))};")
                vol_fs = vol.get("fs")
                fs_content(out, "            ", vol_fs, vol.get("mountpoint"),
                           vol.get("mount_options", []), vol.get("subvolumes", []),
                           label=vol.get("label"), where=where,
                           extra_subvolumes=topology.extra_subvolumes(
                               vol, vol_fs, vol.get("mountpoint")))
                out.append("          };")
            out += ["        };", "      };"]
        out.append("    };")

    render_zpools(topology, doc, out)
    check_root_filesystem(doc)
    out += ["  };", "}"]
    return "\n".join(out) + "\n"


def check_root_filesystem(doc: dict) -> None:
    """Refuse a layout that never resolves a filesystem to /.

    schema.md §6.1: "Exactly one filesystem in the document must resolve to
    mountpoint / … otherwise the document is invalid." Nothing checked it, and
    the way NixOS finds out is `Failed assertions: The 'fileSystems' option
    does not specify your root file system.` — raised while `nixos-install`
    evaluates the configuration, which is after disko has destroyed the disks.
    """
    mounts, _ = mount_table(doc)
    if any(mountpoint == "/" for mountpoint, _, _, _ in mounts):
        return
    refuse("no filesystem in this document resolves to mountpoint '/' — "
           "schema.md §6.1 requires exactly one, and NixOS raises 'the "
           "fileSystems option does not specify your root file system' while "
           "nixos-install evaluates the configuration, by which time disko has "
           "already partitioned the disks")


# ── hardware.nix ─────────────────────────────────────────────────

def render_hardware(doc: dict) -> str:
    boot = doc.get("boot", {}) or {}
    kernel = boot.get("kernel", {}) or {}
    initramfs = boot.get("initramfs", {}) or {}
    drivers = doc.get("drivers", {}) or {}
    arch = (doc.get("target", {}) or {}).get("arch", "x86_64")

    initrd = ["ahci", "xhci_pci", "nvme", "usb_storage", "sd_mod", "virtio_pci", "virtio_blk", "virtio_scsi", "btrfs", "vfat", "nls_cp437", "nls_iso8859_1"]
    # SPEC §7: include_modules are "always embedded". availableKernelModules is
    # the conditional list — a module in it is carried in the initrd but only
    # loaded if something asks for it, which is exactly the case this field
    # exists for (hardware the auto-detector did not see). kernelModules is the
    # always list, so that is where a named module goes.
    always = ["virtio_pci", "virtio_blk", "btrfs", "vfat"]
    for module in boot_str_list(initramfs.get("include_modules"),
                                "boot.initramfs.include_modules"):
        if module not in always:
            always.append(module)

    out = ["# Generated from a LIS document by lis2nixos (default translator).",
           "{ config, lib, pkgs, modulesPath, ... }:", "", "{",
           "  imports = [ (modulesPath + \"/installer/scan/not-detected.nix\") ];", "",
           f"  boot.initrd.availableKernelModules = {nix_list(initrd)};",
           f"  boot.initrd.kernelModules = {nix_list(always)};",
           "  boot.kernelModules = "
           f"{nix_list(boot_str_list(kernel.get('modules'), 'boot.kernel.modules'))};"]
    if kernel.get("blacklist"):
        out.append("  boot.blacklistedKernelModules = "
                   f"{nix_list(boot_str_list(kernel['blacklist'], 'boot.kernel.blacklist'))};")
    # boot.kernelParams belongs to render_boot alone. Stating it here as well
    # was not a redundancy: list options merge by concatenation, so every
    # declared parameter reached the kernel command line twice.
    if drivers.get("microcode") in ("intel", "amd"):
        out.append(f"  hardware.cpu.{drivers['microcode']}.updateMicrocode = true;")
    firmware_on = drivers.get("firmware") != "none"
    out.append(f"  hardware.enableRedistributableFirmware = {str(firmware_on).lower()};")
    platform = {"x86_64": "x86_64-linux", "aarch64": "aarch64-linux",
                "riscv64": "riscv64-linux"}[arch]
    out.append(f"  nixpkgs.hostPlatform = lib.mkDefault {nix_str(platform)};")

    storage = doc.get("storage", {}) or {}
    mounts, swaps = mount_table(doc)
    for mountpoint, device, fstype, options in mounts:
        entry = (f"  fileSystems.{nix_str(mountpoint)} = {{ device = {nix_str(device)}; "
                 f"fsType = {nix_str(fstype)};")
        if options:
            entry += f" options = {nix_list(options)};"
        out.append(entry + " };")
    if swaps:
        joined = " ".join(f"{{ device = {nix_str(d)}; }}" for d in swaps)
        out.append(f"  swapDevices = [ {joined} ];")
    for name, backing in luks_initrd_devices(doc):
        out.append(f"  boot.initrd.luks.devices.{nix_str(name)} = "
                   f"{{ device = {nix_str(backing)}; allowDiscards = true; }};")
    if any(fstype == "zfs" for _, _, fstype, _ in mounts):
        out.append("  boot.supportedFilesystems = [ \"zfs\" ];")
        out.append(f"  networking.hostId = {nix_str(host_id(doc))};")
    if storage.get("raid"):
        out.append("  boot.swraid.enable = true;")

    out.append("}")
    return "\n".join(out) + "\n"


def luks_initrd_devices(doc: dict) -> list[tuple[str, str]]:
    """Each LUKS container, paired with the device disko will have put it on.

    Stage 1 opens only the containers it was told about: luksroot.nix builds its
    unlock units from `boot.initrd.luks.devices`, and it is also what pulls
    dm_crypt and the cipher modules into the initrd. Without an entry the root
    filesystem — or, with LVM inside the container, the whole volume group —
    never appears and the boot stalls in stage 1. disko would emit these from
    its own NixOS module, but this translator generates plain NixOS options
    only, so it states them itself.

    No key material is named here, for either kind. `unlock: passphrase` means
    the operator types it at boot, and the seed that holds it is not attached
    by then; `key.keyfile` is a seed reference for the same reason. Emitting it
    as boot.initrd.luks.devices.<n>.keyFile pointed stage 1 at a path that does
    not exist on the installed machine — the disk was encrypted correctly and
    the system would not boot — and, had the file been copied in to make that
    work, it would have put the key in the world-readable store (SPEC §2.4).
    """
    storage = doc.get("storage", {}) or {}
    topology = topology_for(doc)
    if not topology.encryption:
        return []

    devices = []
    for crypt in topology.encryption:
        # One naming table for the whole translator; this pass used to count
        # partition indices for itself, so an adopted partition earlier in the
        # list moved the container onto a device that was never created.
        device = topology.device_of(crypt["over"])
        if not device:
            refuse(f"encryption '{crypt['id']}': over {crypt['over']!r} does not "
                   "resolve to a partition, array or logical volume declared in "
                   "this document — the installed system would have no device to "
                   "unlock and stage 1 would stall waiting for it")
            continue
        if ((crypt.get("key", {}) or {}).get("keyfile")
                and not (crypt.get("unlock") or [])):
            warn(f"encryption '{crypt['id']}': key.keyfile is seed material, read "
                 "once while disko formats the container. The installed system "
                 "unlocks it through storage.encryption[].unlock, which this "
                 "container does not declare — stage 1 will prompt for a "
                 "passphrase, and only the one the keyfile holds will open it")
        devices.append((crypt["id"], device))
    return devices


def lv_device(vg: str, lv: str) -> str:
    """The device-mapper node for a logical volume.

    Not `/dev/<vg>/<lv>`: the classic NixOS stage 1 mounts the root filesystem
    with busybox `mount`, which passes the path through verbatim instead of
    canonicalising it the way util-linux does. A system installed with the
    symlink then reports a root device that no longer says device-mapper
    anywhere, which is exactly the evidence that root sits on a LUKS-backed
    volume group. The mapper node is the real thing the symlink points at.
    Device-mapper flattens the two names into one by doubling every literal
    dash, so vg 'a-b' lv 'c' is /dev/mapper/a--b-c.
    """
    return f"/dev/mapper/{vg.replace('-', '--')}-{lv.replace('-', '--')}"


def host_id(doc: dict) -> str:
    """ZFS demands a stable 8-hex-digit host id; derive it from the hostname."""
    import hashlib
    hostname = (doc.get("system", {}) or {}).get("hostname", "nixos")
    return hashlib.sha256(hostname.encode()).hexdigest()[:8]


def mount_table(doc: dict) -> tuple[list[tuple[str, str, str, list[str]]], list[str]]:
    """Every mount the document implies, resolved to the device disko will create.

    disko names GPT partitions `disk-<disk>-<partition>`, LUKS mappings
    `/dev/mapper/<id>`, arrays `/dev/md/<name>` and logical volumes
    `/dev/mapper/<vg>-<lv>` — so the mount table can be derived rather than
    guessed.
    """
    storage = doc.get("storage", {}) or {}
    topology = topology_for(doc)
    names = partition_names(storage)

    mounts: list[tuple[str, str, str, list[str]]] = []
    swaps: list[str] = []

    def add(spec: dict, device: str, mountpoint: str | None) -> None:
        # The mountpoint is decided by the caller, not rediscovered here: the
        # partitions come from one arbitration over the whole layout, so the
        # fstab this builds cannot disagree with the disko config that made it.
        # One role→fs rule, shared with the disko pass through Topology.fs_of;
        # this used to carry a private table that knew nothing about `boot`.
        fs = topology.fs_of(spec)
        if fs == "swap":
            swaps.append(device)
            return
        if fs in (None, "none"):
            return
        options = topology.mount_options_of(spec)
        subvolumes = spec.get("subvolumes", []) or []
        if fs == "btrfs" and subvolumes:
            covered = any(s["mountpoint"] == mountpoint for s in subvolumes)
            if mountpoint and not covered:
                mounts.append((mountpoint, device, fs, options + ["subvol=@"]))
            for sub in subvolumes:
                name = sub["name"]   # verbatim; see fs_content()
                mounts.append((sub["mountpoint"], device, fs,
                               list(sub.get("mount_options", []) or options)
                               + [f"subvol={name}"]))
            return
        if mountpoint:
            mounts.append((mountpoint, device, fs, options))

    for part in storage.get("partitions", []) or []:
        if adopted(part) and id(part) not in ADOPTED:
            continue   # refused in render_disko; nothing here would exist to mount
        disk_id, name = names[id(part)]
        handle = part.get("id") or name
        if handle in topology.spare_handles:
            continue   # a hot spare carries no filesystem of its own
        device = adopted_device(part) or partition_device(disk_id, name)
        if crypt := topology.luks_over.get(handle):
            device = f"/dev/mapper/{crypt['id']}"
            handle = crypt["id"]
        owner = topology.consumer.get(handle)
        if owner and owner[0] != "zfs":
            continue  # a raid or volume group owns it; its mounts come from there
        if owner:
            continue  # zpool datasets are emitted from the pool pass below
        add(part, device, topology.mountpoint_of(part))

    for array in storage.get("raid", []) or []:
        handle = array["name"]
        device = f"/dev/md/{handle}"
        if crypt := topology.luks_over.get(handle):
            device = f"/dev/mapper/{crypt['id']}"
            handle = crypt["id"]
        if topology.consumer.get(handle):
            continue
        add(array, device, topology.mountpoint_of(array))

    for group in storage.get("lvm", []) or []:
        for vol in group.get("volumes", []):
            add(vol, lv_device(group["name"], vol["name"]), vol.get("mountpoint"))

    for pool, info in topology.zpools.items():
        for spec in info["datasets"]:
            base = spec.get("id") or spec.get("name") or "root"
            mountpoint = topology.mountpoint_of(spec)
            if any(s["mountpoint"] == mountpoint
                   for s in spec.get("subvolumes", []) or []):
                mountpoint = None   # a declared child holds it; see render_zpools
            if mountpoint:
                mounts.append((mountpoint, f"{pool}/{base}", "zfs",
                               list(spec.get("mount_options", []))))
            for sub in spec.get("subvolumes", []) or []:
                mounts.append((sub["mountpoint"], f"{pool}/{zfs_child(base, sub['name'])}",
                               "zfs", list(sub.get("mount_options", []))))

    mounts.sort(key=lambda m: (m[0].count("/"), m[0]))
    return mounts, swaps


# ── configuration.nix ────────────────────────────────────────────

def nix_script(body: str) -> str:
    """Wrap a shell body in a Nix indented string, escaping the two magic sequences.

    The escape for a literal `''` inside an indented string is `'''`, not
    `''''`: Nix reads the first two quotes as the escape introducer and the
    third as the single character it yields, so `''''` produces *three*
    quotes. A hook body containing `''` — `x=''` is ordinary shell, and
    `print('''…''')` ordinary Python — therefore gained a stray quote on the
    way into configuration.nix, which in shell is an unterminated string that
    swallows the rest of the hook. Verified both directions with
    `nix-instantiate --eval`: `''a''''b''` → `a'''b`, `''a'''b''` → `a''b`.

    `${` must still be escaped as `''${` so that Nix leaves shell parameter
    expansion for the shell instead of resolving it as antiquotation.
    """
    return "''\n" + body.replace("''", "'''").replace("${", "''${") + "\n    ''"


# Running a hook as its user cannot go through `su`: activation happens inside
# the nixos-install chroot, where PAM has no working stack and su dies with
# "Authentication service cannot retrieve authentication info". setpriv drops
# privilege without consulting PAM at all.
AS_USER_FN = """lis_as_user() {
  _u=$1
  _h=$(getent passwd "$_u" | cut -d: -f6)
  setpriv --reuid="$_u" --regid="$(id -gn "$_u")" --init-groups \\
    env HOME="$_h" USER="$_u" LOGNAME="$_u" sh -c "$2"
}"""


# SPEC §13 gives every script entry an `interpreter` (default /bin/sh) and an
# `on_failure` policy (default `fail`); both used to be warn-and-drop, so a
# Python hook ran under sh and a hook marked `continue` still aborted the
# activation. Running the body from a file is what makes the interpreter real,
# and base64 is what makes the body survive the trip: it travels through a Nix
# indented string, an activation script and a systemd unit without a single
# character of it being re-read as syntax.
#
# The two `if cmd; then :; else _rc=$?; fi` shapes are not style. The
# activation script installs `trap ... ERR` (activation-script.nix:66) and the
# first-boot unit runs under `set -e` (systemd-lib.nix:548); a bare failing
# command in either would mark the whole run failed before `on_failure` was
# ever consulted. A command in an `if` condition triggers neither.
LIS_HOOK_FN = AS_USER_FN + """
lis_hook() {
  # $1 label  $2 interpreter  $3 user (empty for root)  $4 policy  $5 body(base64)
  _lbl=$1; _int=$2; _usr=$3; _pol=$4; _rc=0
  # Not /tmp: NixOS makes /tmp from a later activation snippet, so inside
  # nixos-install's `nixos-enter … activate` on a freshly formatted root
  # `mktemp /tmp/…` is ENOENT and every hook is skipped with the marker still
  # written. mkdir -p brings its own parents, so this needs nothing to exist
  # first; 0755 because a per-user hook has to traverse it after setpriv has
  # dropped privilege. Setup failure is a hook failure, not an early return:
  # it goes through the same policy check below so the marker is withheld.
  _d=/var/lib/lis/hooks
  if mkdir -p "$_d" && chmod 0755 "$_d" && _f=$(mktemp "$_d/hook-XXXXXX"); then
    printf '%s' "$5" | base64 -d > "$_f"
    chmod 0700 "$_f"
    if [ -n "$_usr" ]; then
      chown "$_usr" "$_f"
      _h=$(getent passwd "$_usr" | cut -d: -f6)
      if setpriv --reuid="$_usr" --regid="$(id -gn "$_usr")" --init-groups \\
           env HOME="$_h" USER="$_usr" LOGNAME="$_usr" "$_int" "$_f"; then :;
      else _rc=$?; fi
    else
      if "$_int" "$_f"; then :; else _rc=$?; fi
    fi
    rm -f "$_f"
  else
    echo "lis: $_lbl could not be written under $_d" >&2
    _rc=1
  fi
  if [ "$_rc" -ne 0 ]; then
    echo "lis: $_lbl exited $_rc" >&2
    if [ "$_pol" != continue ]; then LIS_HOOK_FAILED=1; return "$_rc"; fi
  fi
  return 0
}"""


# What an `interpreter` path can mean on a NixOS target. /bin/sh is the one
# absolute path NixOS builds itself (system/build.binsh), so it is honored
# literally; everything else outside the store does not exist there, and the
# only way to run the body under the interpreter the document named is the
# store's copy of it — reached by name through the hook PATH. The second
# element is the package that has to be on that PATH for the name to resolve.
INTERPRETERS = {
    "/bin/sh": ("/bin/sh", None), "sh": ("/bin/sh", None),
    "/bin/bash": ("bash", "pkgs.bash"), "/usr/bin/bash": ("bash", "pkgs.bash"),
    "bash": ("bash", "pkgs.bash"),
    "/bin/dash": ("dash", "pkgs.dash"), "/usr/bin/dash": ("dash", "pkgs.dash"),
    "dash": ("dash", "pkgs.dash"),
    "/bin/zsh": ("zsh", "pkgs.zsh"), "/usr/bin/zsh": ("zsh", "pkgs.zsh"),
    "zsh": ("zsh", "pkgs.zsh"),
    "/bin/fish": ("fish", "pkgs.fish"), "/usr/bin/fish": ("fish", "pkgs.fish"),
    "fish": ("fish", "pkgs.fish"),
    "/bin/python3": ("python3", "pkgs.python3"),
    "/usr/bin/python3": ("python3", "pkgs.python3"),
    "python3": ("python3", "pkgs.python3"),
    "/bin/perl": ("perl", "pkgs.perl"), "/usr/bin/perl": ("perl", "pkgs.perl"),
    "perl": ("perl", "pkgs.perl"),
}


# Absolute shell paths a document may name, mapped to the package NixOS needs.
SHELL_PATHS = {
    "/bin/bash": "bashInteractive", "/usr/bin/bash": "bashInteractive",
    "/bin/zsh": "zsh", "/usr/bin/zsh": "zsh",
    "/bin/fish": "fish", "/usr/bin/fish": "fish",
    "/bin/dash": "dash", "/usr/bin/dash": "dash",
    "/bin/ksh": "ksh", "/usr/bin/ksh": "ksh",
    "/bin/tcsh": "tcsh", "/usr/bin/tcsh": "tcsh",
    "/bin/nu": "nushell", "/usr/bin/nu": "nushell",
    "/bin/elvish": "elvish", "/usr/bin/elvish": "elvish",
    "/bin/xonsh": "xonsh", "/usr/bin/xonsh": "xonsh",
}

# SPEC §9's intent names for a login shell. Every attribute here carries a
# `shellPath`, which is what users.users.<n>.shell's shellPackage type checks
# for and what config/users-groups.nix turns into the /etc/passwd field; the
# package also lands in environment.systemPackages and /etc/shells, which is
# the "intent names oblige the applier to install the shell" half of §9.
# Verified as attributes with a shellPath on nixos-24.11.
SHELL_INTENT = {
    "bash": "bashInteractive", "zsh": "zsh", "fish": "fish", "dash": "dash",
    "ksh": "ksh", "tcsh": "tcsh", "nushell": "nushell", "nu": "nushell",
    "elvish": "elvish", "xonsh": "xonsh",
}

# Shells whose package is not enough: their NixOS module is what writes the
# system-wide rc file the login shell sources. config/users-groups.nix:1113-1136
# asserts programs.<shell>.enable for exactly ["fish" "xonsh" "zsh"] on 24.11,
# so a name missing here is an evaluation error raised inside nixos-install,
# after disko has wiped the disks.
PROGRAMS_MODULE = {"zsh": "zsh", "fish": "fish", "xonsh": "xonsh"}

# "give this account no shell". shadow is in every NixOS system's default
# profile (config/system-path.nix), so the path resolves on the installed
# machine; nologin has no shellPath, so it is a path and not a package here.
NOLOGIN = "/run/current-system/sw/bin/nologin"
NOLOGIN_PATHS = {"nologin", "/sbin/nologin", "/usr/sbin/nologin",
                 "/bin/false", "/usr/bin/false"}

# useradd(8)'s NAME_REGEX. A group name outside it is rejected by groupadd on
# the installed machine, long after this translation reported success.
GROUP_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}\$?")


def nix_attr(name: str) -> str:
    """An attribute-set key, bare where Nix's identifier syntax allows it."""
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'-]*", name) else nix_str(name)

# Packages put on PATH for LIS script hooks, on both activation and first boot.
# pkgs.shadow splits su into its own output, so pkgs.shadow alone yields
# "su: command not found" in a hook that switches user.
HOOK_PATH = ("[ pkgs.bash pkgs.coreutils pkgs.shadow pkgs.shadow.su pkgs.util-linux "
             "pkgs.gnused pkgs.gnugrep pkgs.gawk pkgs.findutils pkgs.systemd ]")


# The binary each dotfiles method needs on the first-boot unit's PATH. A
# systemd unit has no inherited PATH at all, so an unlisted tool is a plain
# "command not found" in the journal and a home directory that never appears.
DOTFILES_TOOLS = {"raw": ["pkgs.git"],
                  "stow": ["pkgs.git", "pkgs.stow"],
                  "chezmoi": ["pkgs.git", "pkgs.chezmoi"]}


def hook_path(doc: dict, extra: list[str] | None = None) -> str:
    """HOOK_PATH plus whatever this document's own first-boot work needs."""
    extra = list(extra or [])
    if snapshots_wanted(doc):
        extra.append("pkgs.btrfs-progs")
    for user in doc.get("users", []) or []:
        method = (user.get("dotfiles") or {}).get("method") or "raw"
        if (user.get("dotfiles") or {}).get("repo"):
            extra += [p for p in DOTFILES_TOOLS.get(method, []) if p not in extra]
    ordered = list(dict.fromkeys(p for p in extra if p not in HOOK_PATH))
    if not ordered:
        return HOOK_PATH
    return HOOK_PATH[:-1] + " ".join(ordered) + " ]"


def as_user(name: str, body: str) -> str:
    """One `lis_as_user` call, single-quoted so the *user's* shell expands it.

    The body used to be emitted with json.dumps, i.e. as a double-quoted shell
    word: `$HOME`, `$USER` and every `$(…)` in a per-user hook were therefore
    expanded by the root shell running the activation script, before setpriv
    dropped privilege. A hook writing to "$HOME/.config" wrote into /root.
    setpriv already exports HOME, USER and LOGNAME for the account, so the body
    only has to survive the trip there intact.
    """
    import shlex
    return f"lis_as_user {name} {shlex.quote(body)}"


def script_payload(item: dict, label: str) -> bytes | None:
    """The bytes of one script entry, from `content` or from `source`.

    `source` used to be refused for every applier in the repository, on the
    grounds that none of them fetch an external body. That is true of `https:`
    and it is true of `env:`/`key:`, which name material rather than a file —
    but it is not true of `seed:` or `file:` here. `--apply` runs from
    /run/lis/seed/appliers with the seed already mounted read-only
    (tools/e2e/installer.py:415-436), so the referenced file is on this
    machine's filesystem while the translation is happening, and the body can
    be read and embedded rather than turned away.

    Read now, not at apply time: `post_install` and `firstboot` bodies are
    baked into the generated configuration, which is evaluated inside the
    target where /run/lis/seed does not exist. A file this run cannot read is
    refused with the path in the message (SPEC §2.3), never emitted as an
    empty hook.
    """
    ref = item.get("source")
    content = item.get("content")
    if ref is None:
        if content is None:
            refuse(f"{label}: neither content nor source — SPEC §13 gives a "
                   "script entry its body through one of the two, and an entry "
                   "with neither is a hook the document asked for and nothing "
                   "would run")
        return None if content is None else content.encode()
    if content is not None:
        # `.from` is named here so the refusal quotes the reference, and so the
        # field tracker records the read: bailing out before secret_ref() left
        # it looking like a leaf this applier never consults.
        named = ref.get("from") if isinstance(ref, dict) else ref
        refuse(f"{label}: content and source {named!r} both name the script "
               "body — SPEC §13 gives an entry one body, and there is no rule "
               "that says which of the two would run")
        return None
    path = secret_ref(ref)
    if path is None:
        named = ref.get("from") if isinstance(ref, dict) else ref
        refuse(f"{label}.source {named!r}: `seed:` and `file:` are the two "
               "schemes that name a file on the installer, and this applier "
               "resolves both. `env:` and `key:` name secret material rather "
               "than a script body (SPEC §2.4; a §17 key object is a token or "
               "a key, and has no body to run), and a fetch is required "
               "nowhere: delivery.md §6 makes `seed:` the only MUST, its §7 "
               "network delivery covers finding the *document*, and an "
               "`https://` source does not even validate — schema.json's "
               "secretRef pattern is ^(file|env|seed|key):.+$, so schema.md "
               "§13's prose example of one cannot appear in a conformant "
               "document (SPEC §20.2)")
        return None
    try:
        return pathlib.Path(path).read_bytes()
    except OSError as err:
        refuse(f"{label}.source resolves to {path}, which this run cannot read "
               f"({err.strerror}) — the body has to be embedded in the "
               "generated configuration now, because the target has no "
               "/run/lis/seed when the activation script runs")
        return None


def script_policy(item: dict, label: str) -> str:
    """SPEC §13's `on_failure`, validated. Default `fail`."""
    policy = item.get("on_failure")
    if policy is None:
        return "fail"
    if policy in ("fail", "continue"):
        return policy
    refuse(f"{label}.on_failure {policy!r} is not one of SPEC §13's values "
           "(fail | continue)")
    return "fail"


def script_interpreter(item: dict, label: str) -> tuple[str, str | None] | None:
    """SPEC §13's `interpreter`, as (command, package needed on PATH)."""
    interpreter = item.get("interpreter")
    if interpreter is None:
        return ("/bin/sh", None)
    if interpreter in INTERPRETERS:
        command, package = INTERPRETERS[interpreter]
        if interpreter.startswith("/") and command != interpreter:
            warn(f"{label}.interpreter {interpreter!r} does not exist on a "
                 f"NixOS target; the body runs under the store's {command} "
                 "instead, which is put on the hook PATH for it")
        return (command, package)
    if interpreter.startswith("/nix/store/"):
        return (interpreter, None)
    refuse(f"{label}.interpreter {interpreter!r} is neither a store path nor "
           "one this applier can resolve to a package on the hook PATH "
           f"({' | '.join(sorted(set(c for c, _ in INTERPRETERS.values())))}) "
           "— on NixOS an unresolved interpreter is 'no such file', not a "
           "fallback to sh")
    return None


def hook_call(item: dict, label: str, user: str = "") -> tuple[str, str | None] | None:
    """One script entry → the `lis_hook` line that runs it, and its package."""
    body = script_payload(item, label)
    resolved = script_interpreter(item, label)
    policy = script_policy(item, label)
    if body is None or resolved is None:
        return None
    command, package = resolved
    blob = base64.b64encode(body).decode()
    # Double quotes, not nix_str and not shlex: the line lands inside a Nix
    # indented string, where a backslash is literal and a bare `''` is the
    # terminator. All four words are ours — a generated label, a table entry
    # or a store path, and a name the schema constrains to [a-z0-9_-] — so a
    # plain quote is exact for them and needs no escaping to survive Nix.
    return (f'lis_hook "{label}" "{command}" "{user}" {policy} {blob}', package)


def dotfiles_commands(doc: dict) -> list[str]:
    """users[].dotfiles → a clone, and an apply, from the first-boot unit.

    NixOS has no option for "put this git repository in that account's home":
    home-manager is out of tree and this translator emits plain NixOS only. The
    first-boot unit does run inside the booted target as root with the accounts
    already created, so the intent is deliverable as ⚙ rather than dropped —
    which is what it was, at `if user.get("dotfiles"): pass`, pointing at a
    `chroot_intents()` this applier never imported.

    Quoted with shlex, not json: the body is passed to `lis_as_user` as one
    shell word, and a double-quoted word would let the *root* shell expand
    `$HOME` before setpriv ever drops privilege — landing every account's
    dotfiles in /root.
    """
    import shlex

    out: list[str] = []
    for user in doc.get("users", []) or []:
        dotfiles = user.get("dotfiles") or {}
        repo = dotfiles.get("repo")
        if not repo:
            if dotfiles:
                # schema.json makes `repo` required under dotfiles. Skipping the
                # entry was silent, and the tracker could not catch it either:
                # hook_path() reads `method` for the PATH it needs, so the leaf
                # counted as read while nothing acted on it.
                refuse(f"users['{user['name']}'].dotfiles declares no repo — "
                       "there is nothing to clone, and `repo` is required by "
                       "the schema")
            continue
        name = user["name"]
        method = dotfiles.get("method") or "raw"
        quoted = shlex.quote(repo)
        # Idempotent: the unit is guarded by ConditionPathExists, but a rerun
        # after a failed first boot must not die on an existing directory.
        clone = ('test -e "$HOME/.dotfiles" || '
                 f'git clone --depth 1 {quoted} "$HOME/.dotfiles"')
        if method == "raw":
            body = clone
        elif method == "stow":
            body = (clone + ' && cd "$HOME/.dotfiles" && for p in */; do '
                    'stow -t "$HOME" -R "${p%/}"; done')
        elif method == "chezmoi":
            body = f'chezmoi init --apply {quoted}'
        else:
            refuse(f"users['{name}'].dotfiles.method {method!r} is not one of "
                   "raw | stow | chezmoi (SPEC §9)")
            continue
        warn(f"users['{name}'].dotfiles: emulated by the lis-firstboot unit "
             f"(method {method}), not by a NixOS option — the repository is "
             "cloned once on the first boot and is not managed by the store "
             "afterwards")
        out.append(f"lis_as_user {name} {shlex.quote(body)}")
    return out


# SPEC §13 gives each lifecycle hook a distinct contract, and four of them name
# the *live installer environment*, not the installed system: `post_storage`
# runs right after the target is formatted and mounted, `pre_reboot` after it is
# unmounted, `on_success` on a clean finish, `on_error` when a step fails. None
# of that is expressible as a NixOS option — activation scripts run inside the
# target during `nixos-install`, which is a different machine state. On this
# applier the installer *is* `--apply`, so these four run there, in order,
# around the disko and nixos-install calls (see main()). A translate-only run
# emits nothing for them and says so, exactly as it already does for `pre`.
#
# Before this, all five of post_storage/post_install/post/pre_reboot/on_success
# were concatenated into one activation script with no diagnostic at all
# (AUDIT X7): a `post_storage` hook expecting /mnt to be an empty formatted
# target instead ran inside the finished system, and an `on_success` hook ran
# whether or not the install went on to succeed.
HOST_STAGES = ("post_storage", "pre_reboot", "on_success", "on_error")

HOST_STAGE_CONTRACT = {
    "post_storage": "on the installer host after disko has formatted and mounted "
                    "the target at /mnt, before nixos-install",
    "pre_reboot": "on the installer host after nixos-install returns, before the "
                  "machine is rebooted",
    "on_success": "on the installer host only when nixos-install exits zero",
    "on_error": "on the installer host only when disko or nixos-install fails",
    "post_install": "on the installer host after nixos-install returns, with "
                    "the target still mounted at /mnt",
    "post": "on the installer host after nixos-install returns, with the "
            "target still mounted at /mnt",
}

# Which sides of the chroot boundary each stage has here. SPEC §13 fixes the
# environment of seven of the nine — pre, pre_install, post_storage, pre_reboot,
# on_success and on_error name the live installer environment, firstboot names
# the booted target — so for those the flag can only agree with the phase or
# contradict it. `post`/`post_install` are the two the spec defines on *both*
# sides ("inside target chroot when chroot: true (default true), or in host
# context when chroot: false"), and NixOS has both: the activation script
# nixos-install runs inside the target, and the installer shell it returns to.
# So the flag is honoured per script there, not per stage.
STAGE_SIDES = {
    "pre": ("host",), "pre_install": ("host",), "post_storage": ("host",),
    "post": ("target", "host"), "post_install": ("target", "host"),
    "pre_reboot": ("host",), "on_success": ("host",), "on_error": ("host",),
    "firstboot": ("target",),
}

# users[].scripts run as the account the document declares, and that account is
# created by the generated configuration — it exists inside the target and
# nowhere else, so a per-user hook has no host-side form.
USER_STAGE_SIDES = {"post": ("target",), "post_install": ("target",),
                    "firstboot": ("target",)}

# Why the stage has no target side, in that stage's own SPEC §13 terms.
NO_TARGET_SIDE = {
    "pre": "before any disk is touched, so no target root exists yet",
    "pre_install": "before any disk is touched, so no target root exists yet",
    "post_storage": "with the target formatted and mounted at /mnt but still "
                    "empty",
    "pre_reboot": "after the target is unmounted",
    "on_success": "in the live installer environment",
    "on_error": "in the live installer environment, where the target may be in "
                "any state, including never formatted",
}


def script_side(stage: str, item: dict, sides: tuple[str, ...]) -> str:
    """Which side of the boundary one script entry runs on (SPEC §13 `chroot`).

    The first entry of `sides` is the stage's default, which is SPEC §13's own:
    `true` for post_install, `false` for the host hooks. A flag naming a side
    the stage does not have is refused by check_stage_chroot; it falls back to
    the default here so that `--lenient` still produces a coherent output.
    """
    flag = item.get("chroot")
    if not isinstance(flag, bool):
        return sides[0]
    wanted = "target" if flag else "host"
    return wanted if wanted in sides else sides[0]


def check_stage_chroot(doc: dict) -> None:
    """Refuse a `chroot` flag naming a side the stage does not have here.

    `check_script_fields` is called with honors_chroot=True precisely so this
    can answer per stage and per entry: the shared helper has one default for
    the whole applier, and this applier straddles the boundary.
    """
    def inspect(stage: str, items, label: str, sides, no_host: str) -> None:
        for index, item in enumerate(items or []):
            flag = item.get("chroot")
            if flag is None:
                continue
            # A shape check, not a truthiness test: schema.json:1307 types this
            # boolean, and the string "false" is truthy, so a document that
            # spelled the flag wrong would run the hook on the side opposite
            # the one it named — the silent inversion SPEC §2.3 forbids.
            if not isinstance(flag, bool):
                refuse(f"{label}[{index}].chroot {flag!r} is not a boolean "
                       "(schema.json types it boolean) — this applier will not "
                       "guess a side from it, and the default for "
                       f"{stage} is {'true' if sides[0] == 'target' else 'false'}")
                continue
            if ("target" if flag else "host") in sides:
                continue
            if flag:
                refuse(f"{label}[{index}].chroot true: SPEC §13 runs {stage} "
                       f"{NO_TARGET_SIDE[stage]} — there is no target root to "
                       "enter")
            else:
                refuse(f"{label}[{index}].chroot false: {no_host}")

    scripts = doc.get("scripts", {}) or {}
    for stage, sides in STAGE_SIDES.items():
        inspect(stage, scripts.get(stage), f"scripts.{stage}", sides,
                "SPEC §13 runs firstboot once on the installed target during "
                "its first boot; the live installer is gone by then, so there "
                "is no host context left to run in")
    for user in doc.get("users", []) or []:
        name = user.get("name")
        user_scripts = user.get("scripts", {}) or {}
        for stage, sides in USER_STAGE_SIDES.items():
            inspect(stage, user_scripts.get(stage),
                    f"users['{name}'].scripts.{stage}", sides,
                    f"a per-user hook runs as {name!r}, an account this "
                    "document creates inside the target — the installer host "
                    "has no such user to run it as")


def _leaf_paths(node, prefix: str = "") -> list[str]:
    """Dotted leaf paths under an *untracked* document fragment.

    Deliberately takes the raw document, not the tracked wrapper: walking the
    wrapper would record a read for every leaf it visits, which is the opposite
    of what the caller wants to establish.
    """
    if isinstance(node, dict) and node:
        return [p for key, value in node.items()
                for p in _leaf_paths(value, f"{prefix}.{key}" if prefix else str(key))]
    return [prefix] if prefix else []


def snapshots_wanted(doc: dict) -> bool:
    """Whether this document asks for snapshots this applier can actually take.

    SPEC §20.9: `storage.snapshots.enabled` needs a filesystem that can take
    them, and the NixOS module can only take them on one —
    services.snapper.configs.<name>.FSTYPE is types.enum [ "btrfs" ]
    (nixos/modules/services/misc/snapper.nix:57-62). A non-btrfs root is
    refused where the config is rendered; nothing is set up for it here.
    """
    if not ((doc.get("storage", {}) or {}).get("snapshots") or {}).get("enabled"):
        return False
    return any(mp == "/" and fs == "btrfs" for mp, _, fs, _ in mount_table(doc)[0])


def check_boot_menu(doc: dict) -> None:
    """Turn down storage.snapshots.boot_menu with the NixOS reason for it.

    The shared helper states the outcome for nine distributions at once, so
    the reader cannot tell whether the menu is missing because this applier
    never got around to it or because the channel has nothing to build it
    from. On NixOS it is the second.
    """
    if not ((doc.get("storage", {}) or {}).get("snapshots") or {}).get("boot_menu"):
        return
    refuse("storage.snapshots.boot_menu: nixos-24.11 has no grub-btrfs. Not "
           "as a package — the channel's whole btrfs attribute set is "
           "btrfs-assistant, btrfs-auto-snapshot, btrfs-heatmap, btrfs-progs, "
           "btrfs-snap and ntfs2btrfs — and not as a module: no file under "
           "nixos/modules names it, so nothing would regenerate the menu when "
           "a snapshot is taken. Both loaders this applier can install "
           "enumerate system *generations* instead, and their one escape "
           "hatch (boot.loader.grub.extraEntries, "
           "boot.loader.systemd-boot.extraEntries) is a fixed string written "
           "at nixos-rebuild time, which no later snapshot can appear in "
           "(schema.md §6.6)")


def snapshot_commands(doc: dict) -> list[str]:
    """Create the subvolume snapper stores its snapshots in.

    `services.snapper` writes /etc/snapper/configs/<name> and nothing else: its
    own documentation says the configured path "has to contain a subvolume named
    .snapshots" (services/misc/snapper.nix:50-52) and the module never creates
    it. Without it snapper exits non-zero on every timeline tick, so the
    generated configuration described snapshots the system would never take.

    disko now creates it at format time as `<root subvolume>/.snapshots`
    (Topology.extra_subvolumes) — it can, because it mounts the filesystem's
    top level and can nest a subvolume under another. This stays as the
    fallback for a root that disko did not lay out (an adopted or pre-existing
    filesystem), and it owns the mode either way: `btrfs subvolume create`
    leaves 0755, and snapper's history is not world-readable.
    """
    if not snapshots_wanted(doc):
        return []
    return ["if [ ! -d /.snapshots ]; then btrfs subvolume create /.snapshots; fi",
            "chmod 750 /.snapshots"]


def host_stage_hooks(doc: dict, stage: str) -> list[tuple[str, str, str, bytes]]:
    """One live-installer stage, resolved to (label, interpreter, policy, body).

    The four leaves SPEC §13 gives a script entry are answered here as well as
    in the generated configuration: a host-side hook has an `interpreter` and
    an `on_failure` too, and `--apply` used to run every body through
    `subprocess.run(..., shell=True, check=False)` — one shell for all of them
    and no failure policy at all.
    """
    scripts = doc.get("scripts", {}) or {}
    sides = STAGE_SIDES[stage]
    out: list[tuple[str, str, str, bytes]] = []
    for index, item in enumerate(scripts.get(stage, []) or []):
        # post/post_install have both sides, so only the entries whose `chroot`
        # resolves to `false` belong here; the rest go into the target's
        # activation script. Enumerated before the filter so the label keeps
        # naming the entry's real position in the document.
        if script_side(stage, item, sides) != "host":
            continue
        label = f"scripts.{stage}[{index}]"
        body = script_payload(item, label)
        resolved = script_interpreter(item, label)
        policy = script_policy(item, label)
        if body is None or resolved is None:
            continue
        out.append((label, resolved[0], policy, body))
    return out


def run_host_stage(doc: dict, stage: str) -> int:
    """Run one live-installer-environment stage on this host.

    Returns the exit status the installation should take: non-zero only when a
    hook failed and its `on_failure` is SPEC §13's default `fail`.
    """
    import shutil
    import subprocess
    import tempfile

    for label, interpreter, policy, body in host_stage_hooks(doc, stage):
        binary = interpreter if interpreter.startswith("/") \
            else shutil.which(interpreter)
        if not binary:
            print(f"{label}: interpreter {interpreter!r} is not on this "
                  "installer's PATH", file=sys.stderr)
            if policy == "fail":
                return 127
            continue
        print(f"running {label} on the installer host")
        with tempfile.NamedTemporaryFile(suffix=".lis-hook") as handle:
            handle.write(body)
            handle.flush()
            status = subprocess.run([binary, handle.name], check=False).returncode
        if status and policy == "fail":
            print(f"{label} exited {status} and its on_failure is 'fail' "
                  "(SPEC §13) — aborting", file=sys.stderr)
            return status
        if status:
            print(f"{label} exited {status}; on_failure is 'continue'",
                  file=sys.stderr)
    return 0


def world_readable_store(mode: str | None) -> bool:
    """True when `mode` denies world read, i.e. the store copy contradicts it."""
    if not mode:
        return False
    try:
        return not int(mode, 8) & 0o004
    except ValueError:
        return False


def render_files(doc: dict) -> tuple[list[str], list[str]]:
    """files[] → environment.etc under /etc, activation elsewhere.

    Returns (configuration lines, shell commands for the activation script).
    Anything outside /etc used to refuse outright; NixOS has no declarative
    option for an arbitrary path, but activation runs inside the target with a
    writable root, so the file can still be delivered — as ⚙, not ✅, which the
    warning says. Content travels base64-encoded in both directions so a shell
    metacharacter, a newline or a real binary payload cannot change the meaning
    of what is emitted.
    """
    import shlex

    out: list[str] = []
    cmds: list[str] = []
    seen: set[str] = set()
    for entry in doc.get("files", []) or []:
        if not entry["path"].startswith("/"):
            refuse(f"files[] entry {entry['path']!r} is not an absolute path — "
                   "there is no working directory a relative one could be "
                   "resolved against, in the store or in an activation script")
            continue
        if entry["path"] in seen:
            # Two entries for one path define environment.etc.<name>.text twice,
            # which is a duplicate-attribute error at parse time — reached only
            # after disko has already destroyed the disks on an --apply run.
            refuse(f"files[] declares {entry['path']!r} twice; the document has "
                   "to say once what the file contains")
            continue
        seen.add(entry["path"])
        if entry.get("mode") is not None:
            try:
                int(entry["mode"], 8)
            except ValueError:
                # environment.etc.<n>.mode is a bare string handed to install(1)
                # during activation, so a mode that is not octal is not caught
                # by evaluation — it fails on the installed machine, at a point
                # where the file exists with whatever mode install fell back to.
                refuse(f"files[] entry {entry['path']!r}: mode "
                       f"{entry['mode']!r} is not an octal permission string")
                continue
        raw = entry["content"]
        blob = base64.b64decode(raw) if entry.get("encoding") == "base64" \
            else raw.encode()
        mode = entry.get("mode")
        owner = entry.get("owner")
        path = entry["path"]

        text: str | None
        try:
            text = blob.decode()
        except UnicodeDecodeError:
            text = None

        if path.startswith("/etc/") and text is not None:
            rest = path[len("/etc/"):]
            # environment.etc gets the /etc permissions right and the store
            # copy wrong: `.text` becomes a world-readable file in /nix/store
            # that the /etc entry is copied from, so a mode denying world read
            # confines the copy and not the content. Nothing in NixOS can keep
            # a declaratively-written file out of the store — an activation
            # script would carry the same bytes in a store-resident script —
            # so the honest answer is to say so rather than let the mode read
            # as a confidentiality guarantee it is not.
            if world_readable_store(mode):
                warn(f"files[] entry {path!r} asks for mode {mode!r}, which "
                     "denies world read: /etc gets that mode, but the content "
                     "is copied from a world-readable /nix/store path, so it "
                     "is not confidential on the installed machine (SPEC §2.4 "
                     "— put secrets on the seed, not in files[])")
            out.append(f"  environment.etc.{nix_str(rest)}.text = {nix_str(text)};")
            # uid/gid and mode only take effect when the entry is *copied*:
            # etc.nix:49-52 skips them when mode is the "symlink" default, so an
            # owner with no mode was silently discarded.
            if owner and not mode:
                mode = "0644"
            if mode:
                out.append(f"  environment.etc.{nix_str(rest)}.mode = {nix_str(mode)};")
            if owner:
                user, _, group = owner.partition(":")
                out.append(f"  environment.etc.{nix_str(rest)}.user = {nix_str(user)};")
                if group:
                    out.append(f"  environment.etc.{nix_str(rest)}.group = {nix_str(group)};")
            continue

        if text is None and path.startswith("/etc/"):
            warn(f"files[] entry {path!r}: content is not valid UTF-8, so it "
                 "cannot go through environment.etc (which takes text); it is "
                 "written by an activation script instead")
        else:
            warn(f"files[] entry {path!r} is outside /etc: NixOS has no "
                 "declarative option for an arbitrary path, so it is written by "
                 "an activation script on every activation instead of being "
                 "managed by the store")
        if world_readable_store(mode):
            # The activation script is itself a store path, and the content
            # travels inside it base64-encoded. The written file gets the mode
            # asked for; the bytes are readable by anyone who can read the
            # store, which on a NixOS machine is everyone.
            warn(f"files[] entry {path!r} asks for mode {mode!r}, which denies "
                 "world read: the file is written with that mode, but its "
                 "content is embedded in a world-readable /nix/store "
                 "activation script, so it is not confidential (SPEC §2.4 — "
                 "put secrets on the seed, not in files[])")
        quoted = shlex.quote(path)
        b64 = base64.b64encode(blob).decode()
        cmds.append(f"install -D -m {shlex.quote(mode or '0644')} /dev/null {quoted}")
        cmds.append(f"printf %s {b64} | base64 -d > {quoted}")
        if owner:
            cmds.append(f"chown {shlex.quote(owner)} {quoted}")
    return out, cmds


def render_script_hooks(doc: dict, file_cmds: list[str] | None = None) -> list[str]:
    """LIS script hooks → activation scripts and a first-boot unit.

    NixOS has no installer hook vocabulary, but it does have the two things the
    in-target hooks actually need: something that runs on every activation
    (which includes the one `nixos-install` performs) and something that runs
    once at first boot. The stages SPEC §13 places in the live installer
    environment are not emitted here at all — see HOST_STAGES.
    """
    scripts = doc.get("scripts", {}) or {}
    out: list[str] = []
    packages: list[str] = []

    def collect(items, stage: str, label: str, user: str = "",
                sides: tuple[str, ...] | None = None) -> list[str]:
        lines = []
        for index, item in enumerate(items or []):
            if sides and script_side(stage, item, sides) != "target":
                continue
            call = hook_call(item, f"{label}[{index}]", user)
            if call is None:
                continue
            line, package = call
            if package and package not in packages:
                packages.append(package)
            lines.append(line)
        return lines

    hooks: list[str] = []
    for stage in ("post_install", "post"):
        hooks += collect(scripts.get(stage), stage, f"scripts.{stage}",
                         sides=STAGE_SIDES[stage])
    for user in doc.get("users", []) or []:
        # `post` after `post_install`, the ordering SPEC §13 gives the two
        # phases. The user-level `post` stage used to reach nothing at all here:
        # only `post_install` and `firstboot` were collected, so a document that
        # put its per-user work in `post` had it silently dropped.
        for stage in ("post_install", "post"):
            hooks += collect((user.get("scripts", {}) or {}).get(stage), stage,
                             f"users['{user['name']}'].scripts.{stage}",
                             user["name"])

    firstboot = collect(scripts.get("firstboot"), "firstboot", "scripts.firstboot")
    firstboot += snapshot_commands(doc)
    firstboot += dotfiles_commands(doc)
    firstboot += enrollment_commands(doc)
    firstboot += registration_commands(doc, "nixos")
    for user in doc.get("users", []) or []:
        firstboot += collect((user.get("scripts", {}) or {}).get("firstboot"),
                             "firstboot",
                             f"users['{user['name']}'].scripts.firstboot",
                             user["name"])

    # Resolved here rather than at apply time so that a bad interpreter, an
    # unreadable `source` or an on_failure value SPEC §13 does not define is a
    # refusal *before* disko touches a disk, not a surprise between the wipe
    # and the install.
    host_side = {stage: host_stage_hooks(doc, stage) for stage
                 in ("pre_install", "pre") + HOST_STAGES + ("post_install", "post")}

    for stage in ("pre_install", "pre"):
        if scripts.get(stage):
            warn(f"scripts.{stage} runs on the installer host before disko "
                 "(--apply), not inside the generated configuration")
    for stage in HOST_STAGES:
        if scripts.get(stage):
            warn(f"scripts.{stage} runs {HOST_STAGE_CONTRACT[stage]} (--apply); "
                 "SPEC §13 places it in the live installer environment, which no "
                 "NixOS option can describe, so nothing about it is emitted into "
                 "the generated configuration")
    # SPEC §13.3's "host context" half. These entries are not emitted into the
    # configuration at all — they run where the flag asks, on the installer.
    for stage in ("post_install", "post"):
        if host_side[stage]:
            warn(f"scripts.{stage} entries with chroot: false run "
                 f"{HOST_STAGE_CONTRACT[stage]} (--apply), not from an "
                 "activation script; a translate-only run emits nothing for "
                 "them")
    check_stage_chroot(doc)

    # Birth certificate (delivery.md §8) — recorded on every activation.
    # redact_secrets(), not doc: this copy is baked into the activation script,
    # which lives in the Nix store and is world-readable. The file it writes is
    # 0600, but the bytes are public long before that chmod runs.
    birth = base64.b64encode(
        json.dumps(redact_secrets(doc), separators=(",", ":")).encode()).decode()
    activation = list(file_cmds or [])
    activation.append("install -d -m755 /var/lib/lis")
    activation.append(f"echo {birth} | base64 -d > /var/lib/lis/system.lis.json")
    activation.append("chmod 600 /var/lib/lis/system.lis.json")

    path_expr = hook_path(doc, packages)
    # Activation scripts run with a deliberately minimal PATH, so a hook that
    # calls anything outside coreutils (su, systemctl, sed) dies with 127 and
    # NixOS only prints a one-line "snippet failed". Give hooks a real PATH.
    if activation:
        out += ["  system.activationScripts.lis-hooks = {",
                # Hooks may reference the accounts the document declares, so
                # they must not run before the snippet that creates them — and
                # a post_install hook, or a files[] entry this applier writes
                # by hand, routinely edits something under /etc, which is not
                # populated until the `etc` snippet has run. `etc` is itself
                # stringAfter [ "users" "groups" ] (system/etc/etc-activation.
                # nix:15-19), so naming both is an ordering, not a cycle.
                "    deps = [ \"users\" \"etc\" ];",
                "    text ="
                f"      \"export PATH=\\\"${{lib.makeBinPath {path_expr}}}:$PATH\\\"\\n\" +",
                "      " + nix_script(AS_USER_FN + "\n" + "\n".join(activation)) + ";",
                "  };"]
    if hooks:
        # SPEC §13's post_install is an install-stage hook — "runs after OS
        # installation, package extraction, and file generation", once. The
        # stage does land there: nixos-enter runs the target's `activate`
        # before the command it was given (nixos-enter.sh:103), and that is
        # what nixos-install calls. What it did not do is stop: the whole set
        # used to sit in the same snippet as files[] and the birth
        # certificate, which NixOS re-runs on every boot and every
        # `nixos-rebuild switch`, so a hook appending a line to a config file
        # appended it forever. Its own snippet with its own marker is the
        # "once" half, and the marker is withheld when a hook whose
        # on_failure is `fail` failed, so the next activation retries it
        # instead of recording work that did not happen.
        warn("scripts.post_install / scripts.post run from an activation "
             "script, guarded by /var/lib/lis/.post-install-done so they run "
             "once: nixos-install reaches them through nixos-enter's `activate` "
             "call (nixos-enter.sh:103), which is wrapped in `|| true` — so a "
             "hook whose on_failure is 'fail' marks the activation failed and "
             "is retried on the next boot, but does not abort nixos-install "
             "itself")
        body = ("if [ ! -e /var/lib/lis/.post-install-done ]; then\n"
                "LIS_HOOK_FAILED=0\n"
                + "\n".join(hooks)
                + "\ninstall -d -m755 /var/lib/lis"
                + "\nif [ \"$LIS_HOOK_FAILED\" -eq 0 ]; then"
                " touch /var/lib/lis/.post-install-done; fi\nfi")
        out += ["  system.activationScripts.lis-post-install = {",
                # binsh, because SPEC §13's default interpreter is /bin/sh and
                # on NixOS that symlink is itself made by an activation snippet
                # (config/shells-environment.nix:242). Alphabetical order
                # happens to put it first today; naming it is what keeps a hook
                # from being "no such file or directory" if that ever changes.
                "    deps = [ \"lis-hooks\" \"binsh\" ];",
                "    text ="
                f"      \"export PATH=\\\"${{lib.makeBinPath {path_expr}}}:$PATH\\\"\\n\" +",
                "      " + nix_script(LIS_HOOK_FN + "\n" + body) + ";",
                "  };"]
    if firstboot:
        body = ("install -d -m755 /var/lib/lis\n" + LIS_HOOK_FN + "\n"
                + "\n".join(firstboot)
                + "\ntouch /var/lib/lis/.firstboot-done")
        out += ["  systemd.services.lis-firstboot = {",
                "    description = \"LIS first boot\";",
                "    wantedBy = [ \"multi-user.target\" ];",
                "    after = [ \"multi-user.target\" ];",
                "    unitConfig.ConditionPathExists = "
                "\"!/var/lib/lis/.firstboot-done\";",
                "    serviceConfig.Type = \"oneshot\";",
                # systemd.services.<n>.path *replaces* PATH rather than adding
                # to it, so a tool the unit needs has to be named here. btrfs
                # only when the document asks for snapshots: adding it
                # unconditionally would put btrfs-progs in the closure of every
                # system this translator emits.
                f"    path = {path_expr};",
                "    script = " + nix_script(body) + ";",
                "  };"]
    return out




# A console= parameter names a serial line, so the login prompt has to be
# asked for by name: getty.nix only instantiates serial-getty@ for the ttys
# something enables (getty.nix:153), and NixOS itself turns the unit on this
# way (virtualisation/amazon-image.nix:102, virtualisation/kubevirt.nix:28).
SERIAL_TTY = re.compile(r"^console=(tty(?:S|USB|AMA)\d+|hvc\d+)\b")

KERNEL_PACKAGES = {
    # `lts` is nixpkgs' default kernel on 24.11, an LTS series.
    "lts": "linuxPackages",
    "hardened": "linuxPackages_hardened",
    # Dash, not underscore: pkgs.linuxPackages_rt does not exist on 24.11, and
    # naming it is an evaluation error after disko has already wiped the disks.
    "realtime": "linuxPackages-rt",
    "zen": "linuxPackages_zen",
}


# nixpkgs 24.11 marks the ZFS kernel module broken against kernels newer than
# the series it supports, and `zen` is one of them: with `fs: zfs` in the
# document and this variant, evaluation dies with "Package zfs-kernel-2.2.7-
# 6.15.2 … is marked as broken" (pkgs/os-specific/linux/zfs/generic.nix:309) —
# inside nixos-install, after disko has wiped the disks. lts, hardened and
# realtime all track a series ZFS still builds against and were evaluated
# clean.
ZFS_BROKEN_VARIANTS = {"zen"}


def declares_zfs(doc: dict) -> bool:
    """Whether any filesystem in the document is ZFS."""
    storage = doc.get("storage", {}) or {}
    if any(part.get("fs") == "zfs" for part in storage.get("partitions", []) or []):
        return True
    if any(vol.get("fs") == "zfs" for group in storage.get("lvm", []) or []
           for vol in group.get("volumes", []) or []):
        return True
    return any(array.get("fs") == "zfs" for array in storage.get("raid", []) or [])


def boot_str_list(value, path: str) -> list[str]:
    """A `boot.*` list of strings, refused rather than crashed on.

    nix_str() raises AttributeError on anything that is not a string, so a
    number in kernel.params aborted the translation with a Python traceback
    instead of the stated reason SPEC §2.3 asks for.
    """
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        refuse(f"{path} must be a list of strings (SPEC §7)")
        return []
    return list(value)


def serial_console_ttys(params: list[str]) -> list[str]:
    """The serial lines named by console= parameters, in document order."""
    ttys = []
    for param in params:
        if (match := SERIAL_TTY.match(param)) and match.group(1) not in ttys:
            ttys.append(match.group(1))
    return ttys


def grub_serial_config(serial: str) -> list[str] | None:
    """`serial --unit=N --speed=S` plus the terminal switches, for grub.cfg.

    GRUB speaks to a UART by unit number, not by device node, so only ttyS<N>
    can be translated; a USB or AMBA console reaches the kernel and the getty
    but not the boot menu. The lines land verbatim in grub.cfg through
    boot.loader.grub.extraConfig (install-grub.pl:434 appends it before the
    menu entries, which is where a terminal switch has to be to take effect).
    """
    name, _, rest = serial.partition(",")
    if not (unit := re.fullmatch(r"ttyS(\d+)", name)):
        warn(f"boot.console.serial {serial!r}: GRUB addresses serial ports by unit "
             "number and only ttyS<N> has one, so the boot menu stays on the "
             "firmware console; the kernel and the getty still use the line")
        return None
    speed = match.group(1) if (match := re.match(r"(\d+)", rest)) else "115200"
    return [f"    serial --unit={unit.group(1)} --speed={speed}",
            "    terminal_input --append serial",
            "    terminal_output --append serial"]


def render_boot(doc: dict) -> list[str]:
    """boot.* → bootloader, kernel and console options.

    Every key the schema allows under `boot` is answered here: honored with a
    real option, or refused with the reason NixOS has none. Nothing in this
    section is allowed to fall through in silence, because a bootloader that
    quietly ignores half its instructions still installs — it just installs a
    machine that is not the one the document described.
    """
    boot = doc.get("boot", {}) or {}
    kernel = boot.get("kernel", {}) or {}
    target = doc.get("target", {}) or {}
    firmware = target.get("firmware", "uefi")
    declared_auto = "loader" in boot and boot.get("loader") == "auto"
    loader = boot.get("loader", "auto")
    os_prober = boot.get("os_prober")
    pwhash = boot.get("password_hash")
    serial = (boot.get("console", {}) or {}).get("serial")
    out: list[str] = []

    # Shape first. A value of the wrong type is a document error, not an
    # unsupported request, and every one of these reaches a generated Nix file:
    # `os_prober: "yes"` is truthy and would enable a probe the document did not
    # ask for, and a non-string hash or console name crashes nix_str().
    if os_prober is not None and not isinstance(os_prober, bool):
        refuse(f"boot.os_prober {os_prober!r} is not a boolean (SPEC §7)")
        os_prober = None
    if pwhash is not None and not isinstance(pwhash, str):
        refuse(f"boot.password_hash {pwhash!r} is not a string (SPEC §7)")
        pwhash = None
    if serial is not None and not isinstance(serial, str):
        refuse(f"boot.console.serial {serial!r} is not a string (SPEC §7)")
        serial = None

    # `auto` is the translator's choice, so it is made against the rest of the
    # document: two of these fields exist only in GRUB, and picking
    # systemd-boot for a document that asks for them would turn an honorable
    # translation into a refusal for no reason.
    resolution_reported = False
    if loader == "auto" and firmware != "bios" and (pwhash or os_prober):
        asked = " and ".join(n for n, v in (("boot.password_hash", pwhash),
                                            ("boot.os_prober", os_prober)) if v)
        warn(f"boot.loader auto resolves to grub, not systemd-boot: {asked} "
             "has no systemd-boot equivalent in NixOS")
        loader = "grub"
        resolution_reported = True

    on_systemd_boot = loader in ("auto", "systemd-boot") and firmware != "bios"
    # A document that says `auto` asked the applier to choose; SPEC §2.3 wants
    # the choice reported, §19 records it as a substitution. A document that
    # omitted `loader` asked for the distro default (SPEC §3) and is not a
    # substitution, so it stays quiet.
    if declared_auto and not resolution_reported:
        warn(f"boot.loader auto resolves to "
             f"{'systemd-boot' if on_systemd_boot else 'grub'} on "
             f"{firmware} firmware (SPEC §2.3 substitution)")
    if on_systemd_boot:
        out += ["  boot.loader.systemd-boot.enable = true;",
                "  boot.loader.efi.canTouchEfiVariables = true;"]
        if os_prober:
            refuse("boot.os_prober: only GRUB detects other operating systems on "
                   "NixOS (boot.loader.grub.useOSProber, grub.nix:485); systemd-boot "
                   "has no equivalent option — set boot.loader to grub")
        elif os_prober is False:
            out.append("  # boot.os_prober: false — systemd-boot lists only the "
                       "entries NixOS writes, so there is nothing to turn off.")
        if pwhash:
            refuse("boot.password_hash: NixOS protects boot entries through "
                   "boot.loader.grub.users (grub.nix:211), which systemd-boot has no "
                   "counterpart for — set boot.loader to grub")
    elif loader in ("auto", "grub", "systemd-boot"):
        if loader == "systemd-boot":
            refuse("boot.loader systemd-boot requires UEFI; target.firmware is bios")
        devices = [(d.get("match", {}) or {}).get("path")
                   for d in target.get("disks", [])]
        devices = [d for d in devices if d]
        out.append("  boot.loader.grub.enable = true;")
        if firmware == "bios":
            if not devices:
                # The same assertion the EFI branch answers with "nodev", except
                # a BIOS GRUB really does need a disk to embed itself in, and an
                # empty list is not one. Refused here rather than left to the
                # assertion, which fires only after disko has wiped the disks.
                refuse("boot.loader grub on BIOS firmware has no disk to install to: "
                       "no target.disks[] entry resolved to a device path, and "
                       "boot.loader.grub.devices = [ ] fails the NixOS assertion "
                       "'You must set the option boot.loader.grub.devices … to make "
                       "the system bootable' (grub.nix:852)")
            out.append(f"  boot.loader.grub.devices = {nix_list(devices)};")
        else:
            # efiSupport alone does not build: grub.nix asserts that devices or
            # mirroredBoots is set, and an EFI GRUB has no MBR to embed itself
            # in, so the sentinel "nodev" is the value that satisfies it
            # (grub.nix:896 accepts exactly "nodev" or an absolute path).
            # Verified: with efiSupport and no device, config.assertions carries
            # "You must set the option 'boot.loader.grub.devices' … to make the
            # system bootable" — an evaluation failure after disko has wiped
            # the disks.
            warn("boot.loader grub on UEFI installs to the ESP fallback path "
                 "(EFI/BOOT/BOOTX64.EFI) instead of writing an NVRAM entry, so the "
                 "firmware finds it with no efibootmgr call; a fallback loader "
                 "already on that ESP is replaced")
            out += ["  boot.loader.grub.efiSupport = true;",
                    "  boot.loader.grub.device = \"nodev\";",
                    # --removable, per install-grub.pl:775-777. Mutually
                    # exclusive with canTouchEfiVariables (grub.nix:871), which
                    # is why that option is not set on this branch.
                    "  boot.loader.grub.efiInstallAsRemovable = true;"]
        if os_prober is not None:
            # Stated in both directions. `false` used to emit nothing and lean
            # on the NixOS default, which reads the same in the generated file
            # as a document that never mentioned os-prober — and leaves the
            # answer to whatever a later `imports =` decides.
            out.append("  boot.loader.grub.useOSProber = "
                       f"{str(os_prober).lower()};")
        if pwhash:
            # A crypt(3) hash here is not a weaker password, it is no password:
            # GRUB only understands its own PBKDF2 format and rejects the rest,
            # leaving the menu locked against everyone including the operator.
            if not pwhash.startswith("grub.pbkdf2."):
                refuse("boot.password_hash is not a GRUB PBKDF2 hash — it must be the "
                       "'grub.pbkdf2.sha512.…' string grub-mkpasswd-pbkdf2 prints; GRUB "
                       "rejects any other format and the menu becomes unenterable")
            else:
                warn("boot.password_hash guards the GRUB menu and command line only, "
                     "not the disk, and the hash is copied into the Nix store and into "
                     "/boot/grub/grub.cfg, where any local user can read it "
                     "(grub.nix:218-226)")
                out.append("  boot.loader.grub.users.root.hashedPassword = "
                           f"{nix_str(pwhash)};")
        if serial and (config := grub_serial_config(serial)):
            out += ["  boot.loader.grub.extraConfig = ''"] + config + ["  '';"]
    else:
        refuse(f"boot.loader {loader!r} has no NixOS module in the default translator")
    if serial and on_systemd_boot:
        warn("boot.console.serial: systemd-boot has no serial-terminal option in "
             "NixOS, so the boot menu stays on the firmware console; the kernel "
             "and the getty do use the serial line")

    timeout = boot.get("timeout")
    if timeout is not None:
        # An unquoted value straight from the document: a string reaches
        # boot.loader.timeout as a bare token and fails to *parse*, which under
        # --apply happens inside nixos-install, after disko has wiped the disks.
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 0:
            refuse(f"boot.timeout {timeout!r} is not a non-negative integer (SPEC §7)")
        else:
            out.append(f"  boot.loader.timeout = {timeout};")

    # boot.console.serial is the intent "give me a serial console"; the console=
    # parameter is one of the two halves the spec says it expands to (§7).
    params = boot_str_list(kernel.get("params"), "boot.kernel.params")
    if serial and f"console={serial}" not in params:
        params.append(f"console={serial}")
    if params:
        out.append(f"  boot.kernelParams = {nix_list(params)};")
    ttys = serial_console_ttys(params)
    for tty in ttys:
        out.append(f"  systemd.services.\"serial-getty@{tty}\".enable = true;")
    if serial and not serial_console_ttys([f"console={serial}"]):
        # The other half of §7's obligation, and the half that goes missing in
        # silence: getty.nix:153 only instantiates serial-getty@ for a tty
        # something names, and this name is not one this translator recognises.
        warn(f"boot.console.serial {serial!r}: not a serial line NixOS starts a "
             "getty on (ttyS<n>, ttyUSB<n>, ttyAMA<n>, hvc<n>) — the console= "
             "kernel parameter is set, but no serial-getty unit is enabled")

    variant = kernel.get("variant")
    if kernel_set := check_kernel_variant(doc, KERNEL_PACKAGES, "NixOS"):
        # Dashes are legal in a Nix identifier, so linuxPackages-rt needs no
        # quoting; it is still an attribute name, not an option path.
        out.append(f"  boot.kernelPackages = pkgs.{kernel_set};")
        if variant in ZFS_BROKEN_VARIANTS and declares_zfs(doc):
            refuse(f"boot.kernel.variant {variant!r} with a zfs filesystem: nixpkgs "
                   "24.11 marks the ZFS kernel module broken against this kernel "
                   "series ('Package zfs-kernel-2.2.7-6.15.2 … is marked as broken', "
                   "pkgs/os-specific/linux/zfs/generic.nix:309), so the two cannot be "
                   "installed together — nixos-install would fail to evaluate after "
                   "disko has already wiped the disks; use default, lts, hardened or "
                   "realtime with zfs")

    uki = boot.get("uki")
    if uki is True:
        refuse("boot.uki: NixOS 24.11 can build a UKI (system.build.uki, "
               "modules/system/boot/uki.nix:108) but no bootloader module installs one "
               "— the single in-tree consumer is the disk-image builder "
               "modules/image/repart-verity-store.nix:169, and there is no "
               "boot.uki.enable; a UKI install needs the out-of-tree lanzaboote module")
    elif uki is False:
        out.append("  # boot.uki: false — the loader above manages a kernel and an "
                   "initrd as a pair, which is what this asks for.")
    elif uki is not None:
        refuse(f"boot.uki {uki!r} is not a boolean (SPEC §7)")

    # `is True` alone let the *string* "true" through with no option, no
    # refusal and no diagnostic: a document asking for Secure Boot installed an
    # unsigned system that claimed to honor it. Both booleans and `auto` are
    # answered here, and anything else is a document error.
    secure = boot.get("secure_boot")
    if secure is True:
        refuse("boot.secure_boot: NixOS 24.11 signs nothing — no module in "
               "nixos/modules installs a shim or a signed loader (the tree's only "
               "'secureBoot' is virtualisation.useSecureBoot, a guest-firmware switch "
               "for test VMs); Secure Boot needs the out-of-tree lanzaboote module")
    elif secure is False:
        out.append("  # boot.secure_boot: false — nothing here signs a loader or "
                   "enrolls a key, so the request is met as written.")
    elif secure == "auto":
        warn("boot.secure_boot auto resolves to off: NixOS 24.11 ships no shim and "
             "no signed loader, so the machine boots with Secure Boot disabled "
             "(SPEC §2.3 substitution)")
        out.append("  # boot.secure_boot: auto — resolved to off; NixOS 24.11 has "
                   "no in-tree shim or signed loader to enable it with.")
    elif secure is not None:
        refuse(f"boot.secure_boot {secure!r} is not a value SPEC §7 defines "
               "(auto | true | false)")

    generator = (boot.get("initramfs", {}) or {}).get("generator")
    if generator == "auto":
        out.append("  # boot.initramfs.generator: auto — NixOS builds its own "
                   "initrd from the module list below.")
    elif generator is not None:
        refuse(f"boot.initramfs.generator {generator!r}: NixOS builds its own initrd "
               "(optionally the systemd one via boot.initrd.systemd.enable); dracut, "
               "mkinitcpio and booster appear nowhere in nixos/modules")

    # check_boot_extras() names unread keys directly under `boot` and under
    # `boot.kernel`; these two objects are the ones it does not descend into, so
    # a key inside either would otherwise be dropped without a word.
    for group, known in (("console", {"serial"}),
                         ("initramfs", {"generator", "include_modules"})):
        for key in sorted(set(boot.get(group, {}) or {}) - known):
            warn(f"boot.{group}.{key} is not applied by this applier")
    return out


def redact_secrets(doc: dict) -> dict:
    """The document with key material stripped, for anything store-bound.

    Shared with render_script_hooks: the birth certificate it embeds ends up in
    the Nix store, so anything that is a credential on its own has to be taken
    out first. network.wifi[].psk_hash is such a credential — the PMK is the
    network, not a hash of a password — and it is the one field this translator
    delivers out of band, through write_wireless_secrets().
    """
    wifi = (doc.get("network", {}) or {}).get("wifi") or []
    if not any(net.get("psk_hash") for net in wifi):
        return doc
    clean = json.loads(json.dumps(doc))
    for net in clean["network"]["wifi"]:
        if net.get("psk_hash"):
            net["psk_hash"] = f"<redacted: see {WIRELESS_SECRETS}>"
    return clean


# ── network ──────────────────────────────────────────────────────────
#
# Everything below emits options verified against nixos-24.11 (the channel
# tools/e2e/iso.py pins). Paths and the module lines they were read from:
#   networking.interfaces.<n>.{useDHCP,ipv4.addresses,ipv6.addresses}
#                                       tasks/network-interfaces.nix:216,228,246
#   networking.defaultGateway/-6        tasks/network-interfaces.nix:655,668
#   networking.nameservers              tasks/network-interfaces.nix:~680
#   networking.networkmanager.unmanaged services/networking/networkmanager.nix:211
#   networking.networkmanager.ensureProfiles.{profiles,environmentFiles}
#                                       services/networking/networkmanager.nix:427,488
#   networking.wireless.{enable,secretsFile,networks.<s>.{pskRaw,hidden}}
#                                       services/networking/wpa_supplicant.nix:250,307,406
#   networking.firewall.allowed{TCP,UDP}Port{s,Ranges}
#   systemd.network.links.<n>.{matchConfig,linkConfig}
#                                       system/boot/networkd.nix:3162 — ".link units are
#                                       honored by udev, no matter if systemd-networkd is
#                                       enabled or not" (networkd.nix:3289)
#   systemd.suppressedSystemUnits       system/boot/systemd.nix:426

# The PSK of a WPA network is the credential itself, so it must not be written
# into configuration.nix: that file lands in the world-readable Nix store. Both
# wireless back-ends can read secrets from a file outside the store instead —
# wpa_supplicant through `secretsFile` (`varname=value` lines, wpa_supplicant.nix:250)
# and NetworkManager through `ensureProfiles.environmentFiles`, which envsubst's
# `$varname` into the profile (networkmanager.nix:488). Both formats are
# `NAME=value`, so one file serves either back-end.
WIRELESS_SECRETS = "/var/lib/lis/wireless.env"


def wireless_var(index: int) -> str:
    return f"LIS_WIFI_PSK_{index}"


def cidr(addr: str) -> tuple[str, int, int] | None:
    """'10.0.0.5/24' → ('10.0.0.5', 24, 4). None when it is not a CIDR."""
    host, sep, mask = addr.partition("/")
    if not sep or not mask.isdigit() or not valid_address(host):
        return None
    family = 6 if ":" in host else 4
    prefix = int(mask)
    if prefix > (128 if family == 6 else 32):
        return None
    return host, prefix, family


def valid_address(value: str) -> bool:
    """Whether a bare IPv4 or IPv6 address, with no prefix and no port."""
    try:
        ipaddress.ip_address(str(value))
    except ValueError:
        return False
    return True


def nix_addr_list(addrs: list[tuple[str, int]]) -> str:
    items = " ".join(f"{{ address = {nix_str(a)}; prefixLength = {p}; }}"
                     for a, p in addrs)
    return f"[ {items} ]"


# firewalld's shipped service definitions are the lingua franca the rest of
# this tree already hands `allow_services` to — lis_common.py:1215 shells out
# to `firewall-cmd --permanent --add-service=<name>` and lis2kickstart.py:389
# writes `firewall --service=<name>`. NixOS has no service database at all:
# networking.firewall.allowed*Ports are lists of integers. So the same names
# resolve here, applier-side, to the ports firewalld's own
# /usr/lib/firewalld/services/<name>.xml opens. Values are `allow_ports[]`
# syntax so both inputs go through one parser.
FIREWALL_SERVICES: dict[str, tuple[str, ...]] = {
    "amqp": ("5672/tcp",),
    "bitcoin": ("8333/tcp",),
    "cockpit": ("9090/tcp",),
    "dhcp": ("67/udp",),
    "dhcpv6": ("547/udp",),
    "dhcpv6-client": ("546/udp",),
    "dns": ("53/tcp", "53/udp"),
    "dns-over-tls": ("853/tcp",),
    "docker-registry": ("5000/tcp",),
    "elasticsearch": ("9200/tcp", "9300/tcp"),
    "ftp": ("21/tcp",),
    "git": ("9418/tcp",),
    "grafana": ("3000/tcp",),
    "http": ("80/tcp",),
    "https": ("443/tcp",),
    "imap": ("143/tcp",),
    "imaps": ("993/tcp",),
    "influxdb": ("8086/tcp",),
    "ipp": ("631/tcp",),
    "ipp-client": ("631/udp",),
    "ipsec": ("500/udp", "4500/udp"),
    "iscsi-target": ("3260/tcp",),
    "kerberos": ("88/tcp", "88/udp"),
    "kube-apiserver": ("6443/tcp",),
    "ldap": ("389/tcp",),
    "ldaps": ("636/tcp",),
    "memcached": ("11211/tcp",),
    "mdns": ("5353/udp",),
    "mongodb": ("27017/tcp",),
    "mosh": ("60000-61000/udp",),
    "mqtt": ("1883/tcp",),
    "mysql": ("3306/tcp",),
    "nfs": ("2049/tcp",),
    "nfs3": ("111/tcp", "111/udp", "2049/tcp", "2049/udp",
             "20048/tcp", "20048/udp"),
    "nrpe": ("5666/tcp",),
    "ntp": ("123/udp",),
    "openvpn": ("1194/udp",),
    "pop3": ("110/tcp",),
    "pop3s": ("995/tcp",),
    "postgresql": ("5432/tcp",),
    "prometheus": ("9090/tcp",),
    "ptp": ("319/udp", "320/udp"),
    "rdp": ("3389/tcp",),
    "redis": ("6379/tcp",),
    "rpc-bind": ("111/tcp", "111/udp"),
    "rsyncd": ("873/tcp",),
    "samba": ("137/udp", "138/udp", "139/tcp", "445/tcp"),
    "samba-client": ("137/udp", "138/udp"),
    "smtp": ("25/tcp",),
    "smtps": ("465/tcp",),
    "snmp": ("161/udp",),
    "snmptrap": ("162/udp",),
    "squid": ("3128/tcp",),
    "ssh": ("22/tcp",),
    "submission": ("587/tcp",),
    "svn": ("3690/tcp",),
    "syslog": ("514/udp",),
    "syslog-tls": ("6514/tcp",),
    "telnet": ("23/tcp",),
    "tftp": ("69/udp",),
    "tor-socks": ("9050/tcp",),
    "transmission-client": ("51413/tcp", "51413/udp"),
    "upnp-client": ("1900/udp",),
    "vnc-server": ("5900-5903/tcp",),
    "wireguard": ("51820/udp",),
    "ws-discovery": ("3702/udp",),
}


def render_firewall(firewall: dict, out: list[str]) -> None:
    """network.firewall → networking.firewall.*.

    A port *range* is why this is not two list comprehensions: '8000-8010/tcp'
    used to be pasted straight into `allowedTCPPorts`, which is not an integer
    and not even parseable Nix — and under --apply the disks are already gone by
    the time the evaluation fails.
    """
    enabled = firewall.get("enabled")
    if enabled is not None:
        if (literal := nix_bool(enabled, "network.firewall.enabled")) is None:
            enabled = None
        else:
            out.append(f"  networking.firewall.enable = {literal};")
    # (spec, where it came from) — allow_services[] is expanded into the same
    # syntax allow_ports[] uses so one parser and one refusal path serve both.
    specs: list[tuple[str, str]] = []
    for service in firewall.get("allow_services", []) or []:
        opened = FIREWALL_SERVICES.get(str(service).strip().lower())
        if opened is None:
            refuse(f"network.firewall.allow_services {service!r} is not a "
                   "service name this translator can resolve to ports: NixOS "
                   "has no service database (networking.firewall.allowed*Ports "
                   "are lists of integers), so the name is resolved here "
                   "against firewalld's service definitions — "
                   f"{len(FIREWALL_SERVICES)} names, from 'amqp' to "
                   "'ws-discovery'. Name the ports in "
                   "network.firewall.allow_ports instead")
            continue
        specs += [(spec, f"network.firewall.allow_services {service!r}")
                  for spec in opened]
    specs += [(str(spec), "network.firewall.allow_ports")
              for spec in firewall.get("allow_ports", []) or []]

    ports: dict[str, list[int]] = {"tcp": [], "udp": []}
    ranges: dict[str, list[tuple[int, int]]] = {"tcp": [], "udp": []}
    for spec, source in specs:
        port, _, proto = spec.partition("/")
        low, dash, high = port.partition("-")
        if proto not in ports or not low.isdigit() or (dash and not high.isdigit()):
            refuse(f"{source} {spec!r} is not <port>[-<port>]/<tcp|udp>")
            continue
        # networking.firewall.allowed*Ports is types.port — 0..65535 — and
        # types.port rejects anything outside it at evaluation time, which
        # under --apply is inside nixos-install with the disks already gone.
        bounds = [int(low)] + ([int(high)] if dash else [])
        if any(not 0 <= b <= 65535 for b in bounds):
            refuse(f"{source} {spec!r} is outside 0-65535; "
                   "networking.firewall.allowedTCPPorts is typed types.port")
            continue
        if dash:
            if int(low) > int(high):
                refuse(f"{source} {spec!r} counts down; "
                       "networking.firewall.allowed*PortRanges takes "
                       "{ from = <low>; to = <high>; } and opens nothing when "
                       "from exceeds to")
                continue
            if (int(low), int(high)) not in ranges[proto]:
                ranges[proto].append((int(low), int(high)))
        elif int(low) not in ports[proto]:
            ports[proto].append(int(low))
    if enabled is False and (any(ports.values()) or any(ranges.values())):
        warn("network.firewall.allow_ports / allow_services are not emitted: the "
             "same document sets network.firewall.enabled false, and "
             "networking.firewall ignores its allow lists when disabled")
        return
    for proto, opt in (("tcp", "TCP"), ("udp", "UDP")):
        if ports[proto]:
            listed = " ".join(str(p) for p in ports[proto])
            out.append(f"  networking.firewall.allowed{opt}Ports = [ {listed} ];")
        if ranges[proto]:
            listed = " ".join(f"{{ from = {lo}; to = {hi}; }}" for lo, hi in ranges[proto])
            out.append(f"  networking.firewall.allowed{opt}PortRanges = [ {listed} ];")


def is_glob(name: str | None) -> bool:
    """Whether a match.name is a pattern rather than one interface name.

    SPEC §10's own example is `{"match": {"name": "en*"}}`, and
    `networking.interfaces.<name>` is keyed by the literal name — an attribute
    called `en*` matches nothing and configures nothing. Only systemd-networkd
    matches patterns (`.network` MatchConfig Name, systemd.network(5)), which
    is why the manager is resolved before the interfaces are rendered.
    """
    return bool(name) and any(c in name for c in "*?[")


def iface_dhcp(index: int, iface: dict) -> tuple[bool | None, bool | None]:
    """dhcp4/dhcp6 as booleans; anything else is refused and read as unset.

    Both leaves are emitted as bare Nix tokens (`useDHCP = true;`,
    `DHCP = "ipv4";`), so a string slipping through is a parse error or a
    silently inverted flag rather than a bad value.
    """
    flags: list[bool | None] = []
    for leaf in ("dhcp4", "dhcp6"):
        value = iface.get(leaf)
        if value is not None and not isinstance(value, bool):
            refuse(f"network.interfaces[{index}].{leaf} {value!r} is not a "
                   "boolean (true or false)")
            value = None
        flags.append(value)
    return flags[0], flags[1]


def networkd_dhcp(dhcp4: bool | None, dhcp6: bool | None) -> str:
    """dhcp4/dhcp6 → the .network DHCP= value.

    networkd states the two families separately ("ipv4"/"ipv6"/"yes"/"no",
    systemd.network(5)), so a pattern-matched interface can honour a document
    that sets them to different values — which `networking.interfaces.<n>.
    useDHCP`, one flag for both, cannot.
    """
    if dhcp4 and dhcp6:
        return "yes"
    if dhcp4:
        return "ipv4"
    if dhcp6:
        return "ipv6"
    return "no"


def render_glob_interface(index: int, iface: dict, name: str, mac: str | None,
                          manager: str, out: list[str]) -> bool:
    """A `match.name` pattern → systemd.network.networks."80-lis<n>".

    The file name decides precedence, and networkd takes the first `.network`
    that matches in lexical order: NixOS writes `40-<name>` for every
    `networking.interfaces` entry and `99-ethernet-default-dhcp` /
    `99-wireless-client-dhcp` for its blanket fallbacks
    (network-interfaces-systemd.nix:59,80,106). `80-` therefore loses to an
    exact-name entry in the same document and wins over the fallback, which is
    the precedence the document states by naming a pattern at all.
    """
    if manager != "systemd-networkd":
        refuse(f"network.interfaces[{index}].match.name {name!r} is a pattern, "
               f"and network.manager is {manager!r}: only systemd-networkd "
               "matches interface patterns (the .network [Match] Name= field). "
               "networking.interfaces is an attribute set keyed by the literal "
               f"interface name, so an attribute called {name} would match no "
               "device and configure nothing. Set network.manager to "
               "systemd-networkd, or name one interface exactly")
        consume(iface)
        return False

    dhcp4, dhcp6 = iface_dhcp(index, iface)
    addresses: list[str] = []
    for addr in iface.get("addresses", []) or []:
        if cidr(addr) is None:
            refuse(f"network.interfaces[{index}].addresses {addr!r} is not "
                   "<address>/<prefix>; systemd.network.networks.<n>.address "
                   "takes CIDR notation")
            continue
        addresses.append(addr)
    gateway = iface.get("gateway")
    if gateway and not valid_address(gateway):
        refuse(f"network.interfaces[{index}].gateway {gateway!r} is not an IP "
               "address; a .network [Route] Gateway= takes an address, and "
               "networkd drops a route it cannot parse")
        gateway = None
    if dhcp4 is None and dhcp6 is None and not addresses and not gateway:
        warn(f"network.interfaces[{index}].match.name {name!r} carries no "
             "addressing (no dhcp4, no dhcp6, no addresses, no gateway), so "
             "nothing is emitted for it — a .network file with no DHCP= would "
             "switch the matched devices off rather than leave them alone")
        consume(iface)
        return False

    lines = [f"  systemd.network.networks.\"80-lis{index}\" = {{",
             f"    matchConfig.Name = {nix_str(name)};"]
    if mac:
        # [Match] conditions are ANDed (systemd.network(5)), so a document that
        # states both a pattern and a MAC gets both, not one of them.
        lines.append(f"    matchConfig.MACAddress = {nix_str(mac)};")
    if dhcp4 is not None or dhcp6 is not None or addresses:
        lines.append(f"    DHCP = {nix_str(networkd_dhcp(dhcp4, dhcp6))};")
    if addresses:
        lines.append(f"    address = {nix_list(addresses)};")
    if gateway:
        lines.append(f"    routes = [ {{ Gateway = {nix_str(gateway)}; }} ];")
    lines.append("  };")
    out += lines
    return True


def render_interfaces(interfaces: list, manager: str, out: list[str]) -> list[str]:
    """network.interfaces[] → networking.interfaces.<name> and friends.

    Returns the interface names configured here so the caller can hand them to
    NetworkManager as `unmanaged`: NixOS assigns the addresses through its own
    units, and NM left to itself would start a second, contradictory DHCP on the
    very interface the document pinned. A pattern match takes the other route —
    `systemd.network.networks` — and is not in that list, because networkd is
    then the manager and there is no NetworkManager to hold off.
    """
    names: list[str] = []
    nameservers: list[str] = []
    gateways: dict[int, tuple[str, str]] = {}
    dns_sources = 0
    for index, iface in enumerate(interfaces):
        match = iface.get("match", {}) or {}
        name, mac = match.get("name"), match.get("mac")
        if not name and not mac:
            refuse(f"network.interfaces[{index}].match names neither an interface "
                   "nor a MAC address; nothing to key networking.interfaces on")
            continue
        if is_glob(name):
            if render_glob_interface(index, iface, name, mac, manager, out):
                if dns := iface.get("dns"):
                    dns_sources += 1
                    nameservers += [d for d in dns if d not in nameservers]
            continue
        if name and name in names:
            # configuration.nix is one attribute set, so a second
            # networking.interfaces."eth0" is `error: attribute already
            # defined` — raised by nixos-install, after disko has wiped.
            refuse(f"network.interfaces[{index}].match.name {name!r} is already "
                   "configured by an earlier entry; networking.interfaces is an "
                   "attribute set and cannot hold the same interface twice")
            consume(iface)
            continue
        if not name:
            # networking.interfaces is keyed by name, so a MAC-only match needs a
            # name to exist first. A .link file gives it one; udev honours those
            # whether or not systemd-networkd runs (networkd.nix:3289).
            name = f"lis{index}"
            warn(f"network.interfaces[{index}].match.mac {mac!r}: NixOS keys "
                 f"networking.interfaces by name, so the device is renamed to "
                 f"{name!r} by a systemd.network.links entry first")
        if mac:
            out += [f"  systemd.network.links.\"10-{name}\" = {{",
                    f"    matchConfig.MACAddress = {nix_str(mac)};",
                    f"    linkConfig.Name = {nix_str(name)};",
                    "  };"]
        names.append(name)

        settings: list[str] = []
        dhcp4, dhcp6 = iface_dhcp(index, iface)
        if dhcp4 is not None and dhcp6 is not None and dhcp4 != dhcp6:
            refuse(f"network.interfaces[{index}] asks for dhcp4={dhcp4} and "
                   f"dhcp6={dhcp6}: networking.interfaces.<n>.useDHCP "
                   "(network-interfaces.nix:216) is one flag for both families")
            continue
        v4: list[tuple[str, int]] = []
        v6: list[tuple[str, int]] = []
        for addr in iface.get("addresses", []) or []:
            parsed = cidr(addr)
            if parsed is None:
                refuse(f"network.interfaces[{index}].addresses {addr!r} is not "
                       "<address>/<prefix>; networking.interfaces.<n>.ipv4.addresses "
                       "takes an address and a prefixLength separately")
                continue
            host, prefix, family = parsed
            (v6 if family == 6 else v4).append((host, prefix))
        dhcp = dhcp4 if dhcp4 is not None else dhcp6
        if dhcp is None and (v4 or v6):
            # Explicit addresses mean the document does not want a lease; leaving
            # useDHCP at its default lets dhcpcd overwrite them.
            dhcp = False
        if dhcp is not None:
            settings.append(f"    useDHCP = {str(dhcp).lower()};")
        if v4:
            settings.append(f"    ipv4.addresses = {nix_addr_list(v4)};")
        if v6:
            settings.append(f"    ipv6.addresses = {nix_addr_list(v6)};")
        if settings:
            out.append(f"  networking.interfaces.{nix_str(name)} = {{")
            out += settings
            out.append("  };")
        elif not iface.get("gateway") and not iface.get("dns"):
            warn(f"network.interfaces[{index}].match.name {name!r} carries no "
                 "addressing (no dhcp4, no dhcp6, no addresses, no gateway, no "
                 "dns), so no networking.interfaces entry is emitted for it")

        if gw := iface.get("gateway"):
            if not valid_address(gw):
                refuse(f"network.interfaces[{index}].gateway {gw!r} is not an "
                       "IP address; networking.defaultGateway.address is "
                       "types.str, so a hostname or a CIDR reaches the routing "
                       "table verbatim and the route is never installed")
                continue
            family = 6 if ":" in gw else 4
            if family in gateways and gateways[family] != (gw, name):
                refuse(f"network.interfaces[{index}].gateway {gw!r}: NixOS has one "
                       f"networking.defaultGateway{'6' if family == 6 else ''} "
                       "(network-interfaces.nix:655), already claimed by "
                       f"{gateways[family][0]!r} on {gateways[family][1]!r}")
            else:
                gateways[family] = (gw, name)
        if dns := iface.get("dns"):
            dns_sources += 1
            nameservers += [d for d in dns if d not in nameservers]

    for family, (address, iface_name) in sorted(gateways.items()):
        opt = "defaultGateway6" if family == 6 else "defaultGateway"
        out.append(f"  networking.{opt} = {{ address = {nix_str(address)}; "
                   f"interface = {nix_str(iface_name)}; }};")
    if nameservers:
        if dns_sources > 1:
            warn("network.interfaces[].dns is merged into one list: NixOS resolves "
                 "through networking.nameservers, which is system-wide and has no "
                 "per-interface form")
        out.append(f"  networking.nameservers = {nix_list(nameservers)};")
    return names


def render_wifi(wifi: list, manager: str, out: list[str]) -> None:
    """network.wifi[] → NetworkManager profiles or a wpa_supplicant network.

    Which back-end is not a preference: networkmanager.nix:551 asserts that
    networking.wireless and NetworkManager cannot both drive the radio, so the
    document's `manager` decides and the other back-end is never emitted.
    """
    if manager == "iwd":
        render_iwd_networks(wifi, out)
        return
    # Only an entry with a PSK causes the secrets file to be written, and both
    # back-ends treat a named-but-missing secrets file as a hard error: naming
    # it for an open-network-only document would take the radio down instead of
    # bringing it up.
    secrets = any(net.get("psk_hash") for net in wifi)
    if secrets:
        warn(f"network.wifi[].psk_hash is read at boot from {WIRELESS_SECRETS}, "
             "not written into configuration.nix, which the Nix store publishes "
             "world-readable; --apply installs that file, a translate-only run "
             "must provision it")
    if manager == "systemd-networkd":
        out.append("  networking.wireless.enable = true;")
        if secrets:
            out.append("  networking.wireless.secretsFile = "
                       f"{nix_str(WIRELESS_SECRETS)};")
        seen: set[str] = set()
        for index, net in enumerate(wifi):
            ssid = net["ssid"]
            if ssid in seen:
                # networking.wireless.networks is keyed by SSID, so a repeat is
                # `error: attribute already defined` at nixos-install time.
                refuse(f"network.wifi[{index}].ssid {ssid!r} is declared twice; "
                       "networking.wireless.networks is an attribute set keyed "
                       "by SSID and cannot hold the same network twice")
                consume(net)
                continue
            seen.add(ssid)
            psk = net.get("psk_hash")
            if psk is None:
                out.append(f"  networking.wireless.networks.{nix_str(ssid)}.auth = "
                           "\"key_mgmt=NONE\";")
            elif re.fullmatch(r"[0-9a-fA-F]{64}", str(psk)):
                out.append(f"  networking.wireless.networks.{nix_str(ssid)}.pskRaw = "
                           f"{nix_str('ext:' + wireless_var(index))};")
            else:
                refuse(f"network.wifi[{index}].psk_hash is not a 64-character hex "
                       "PSK; networking.wireless.networks.<ssid>.pskRaw is typed "
                       "strMatching \"([[:xdigit:]]{64})|(ext:…)\" "
                       "(wpa_supplicant.nix:308)")
                continue
            if net.get("hidden"):
                out.append(f"  networking.wireless.networks.{nix_str(ssid)}.hidden = true;")
        return
    # networkmanager — `auto` never arrives here, resolve_manager() has already
    # turned it into one of the three concrete back-ends.
    if secrets:
        out.append("  networking.networkmanager.ensureProfiles.environmentFiles = "
                   f"[ {nix_str(WIRELESS_SECRETS)} ];")
    for index, net in enumerate(wifi):
        ssid = net["ssid"]
        out += [f"  networking.networkmanager.ensureProfiles.profiles.\"lis-wifi-{index}\" = {{",
                f"    connection = {{ id = {nix_str(ssid)}; type = \"wifi\"; }};",
                f"    wifi = {{ ssid = {nix_str(ssid)}; mode = \"infrastructure\";"
                + (" hidden = true;" if net.get("hidden") else "") + " };"]
        if psk := net.get("psk_hash"):
            del psk  # the value never leaves the seed; only its variable name is emitted
            out.append("    \"wifi-security\" = { \"key-mgmt\" = \"wpa-psk\"; psk = "
                       f"{nix_str('$' + wireless_var(index))}; }};")
        out += ["    ipv4 = { method = \"auto\"; };",
                "    ipv6 = { method = \"auto\"; };",
                "  };"]


def iwd_profile_name(ssid: str, extension: str) -> str:
    """iwd's own file name for one SSID (iwd src/storage.c).

    iwd stores a network as /var/lib/iwd/<ssid>.<security>, and falls back to
    `=<hex>` whenever the SSID holds a byte outside ASCII printable, or a '/'.
    Its reader hex-decodes any name starting with '=', so an SSID that starts
    with one is encoded here too or it would be read back as hex.

    The test is on ASCII, not str.isalnum(): Python's is Unicode-aware, so an
    accented SSID passed it and was written under its literal name while iwd
    looked for the hex form — the profile was written and never found.
    """
    printable = ssid and all(0x20 <= ord(c) <= 0x7E and c != "/" for c in ssid)
    if printable and not ssid.startswith("="):
        return f"{ssid}.{extension}"
    return f"={ssid.encode().hex()}.{extension}"


def render_iwd_networks(wifi: list, out: list[str]) -> None:
    """network.wifi[] under `manager: iwd` → a unit that writes /var/lib/iwd.

    services/networking/iwd.nix exposes only `settings`, which is main.conf —
    the networks themselves live in /var/lib/iwd, outside any NixOS option and
    outside the store, which is where the PSK has to stay anyway (SPEC §2.4).
    So the profiles are written by a unit at boot rather than declared, and
    that emulation is warned about rather than hidden.
    """
    seen: set[str] = set()
    entries: list[tuple[int, dict]] = []
    for index, net in enumerate(wifi):
        ssid = net["ssid"]
        if ssid in seen:
            refuse(f"network.wifi[{index}].ssid {ssid!r} is declared twice; "
                   "iwd keys its stored networks by SSID and the second entry "
                   "would overwrite the first")
            consume(net)
            continue
        if psk := net.get("psk_hash"):
            if not re.fullmatch(r"[0-9a-fA-F]{64}", str(psk)):
                refuse(f"network.wifi[{index}].psk_hash is not a 64-character "
                       "hex PSK; iwd's [Security] PreSharedKey= is the "
                       "pairwise master key, not a passphrase")
                consume(net)
                continue
        seen.add(ssid)
        entries.append((index, net))
    if not entries:
        return

    secrets = any(net.get("psk_hash") for _, net in entries)
    warn("network.wifi[] under network.manager 'iwd': iwd keeps its networks "
         "in /var/lib/iwd, which services/networking/iwd.nix:36 does not "
         "expose — `settings` is main.conf only. The profiles are written by a "
         "boot-time unit (lis-iwd-networks) instead of being declared, so they "
         "are re-created on every boot and local edits to them do not survive")

    body = ["    set -eu", "    umask 077",
            "    install -d -m 0700 /var/lib/iwd"]
    if secrets:
        body.append(f"    . {WIRELESS_SECRETS}")
    for index, net in entries:
        ssid = net["ssid"]
        psk = net.get("psk_hash")
        path = "/var/lib/iwd/" + iwd_profile_name(ssid, "psk" if psk else "open")
        fmt, args = "", ""
        if psk:
            # %s and a quoted argument, not the variable inside the format: a
            # shell single-quoted string does not expand, so interpolating the
            # name would write the six characters "$LIS_…" as the key. The PSK
            # itself reaches the file from the 0600 environment file at boot
            # and never enters the Nix store.
            fmt += "[Security]\\nPreSharedKey=%s\\n"
            args = f' "${wireless_var(index)}"'
        if net.get("hidden"):
            fmt += "[Settings]\\nHidden=true\\n"
        if not fmt:
            # iwd needs the file to exist at all for an open network; a
            # [Settings] section it already defaults to is the smallest one.
            fmt = "[Settings]\\nAutoConnect=true\\n"
        body.append(f"    printf '{fmt}'{args} > {shlex.quote(path)}")

    out += ["  systemd.services.lis-iwd-networks = {",
            "    description = \"LIS: write the declared wireless networks "
            "into /var/lib/iwd\";",
            "    wantedBy = [ \"multi-user.target\" ];",
            "    before = [ \"iwd.service\" ];",
            "    serviceConfig = { Type = \"oneshot\"; RemainAfterExit = true; };"]
    if secrets:
        # Without the secrets file the shell would write an empty
        # PreSharedKey= and iwd would refuse to associate; skipping the unit
        # leaves whatever is already stored, which is the safer of the two.
        out.append("    unitConfig.ConditionPathExists = "
                   f"{nix_str(WIRELESS_SECRETS)};")
    out += ["    script = ''"] + body + ["    '';", "  };"]


def write_wireless_secrets(doc: dict) -> None:
    """Stage network.wifi[].psk_hash onto the target, outside the Nix store.

    Mirrors write_birth_certificate: --apply is the only moment the applier has
    both the document and the installed root, and mode 0600 under /var/lib is
    the one place a PSK can live where wpa_supplicant and NetworkManager can
    both read it and `nix store` cannot.
    """
    wifi = (doc.get("network", {}) or {}).get("wifi") or []
    secrets = [f"{wireless_var(i)}={net['psk_hash']}"
               for i, net in enumerate(wifi) if net.get("psk_hash")]
    if not secrets:
        return
    path = pathlib.Path("/mnt") / WIRELESS_SECRETS.lstrip("/")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(secrets) + "\n")
        path.chmod(0o600)
        print(f"wrote wireless secrets {path}")
    except OSError as err:
        print(f"warning: could not write wireless secrets: {err}", file=sys.stderr)


# SPEC §8's four `system.time.provider` values → the NixOS switch that starts
# that daemon. All three modules take their server list from
# `networking.timeServers` by default (chrony.nix, openntpd.nix, timesyncd.nix
# each default `servers` to it), so `system.time.servers[]` is emitted once and
# reaches whichever one is running.
TIME_PROVIDERS = {
    "chrony": "services.chrony.enable",
    "openntpd": "services.openntpd.enable",
    "systemd-timesyncd": "services.timesyncd.enable",
}


def render_time(time_cfg: dict) -> list[str]:
    """system.time.* → the time-synchronisation daemon and its servers.

    `ntp` and `provider` are answered together because they contradict each
    other when `ntp` is false: NixOS starts systemd-timesyncd by default
    (timesyncd.nix), so "do not synchronise" is an option that has to be
    written down, not a default that can be left alone.
    """
    out: list[str] = []
    provider = time_cfg.get("provider")
    ntp = time_cfg.get("ntp")
    servers = time_cfg.get("servers")
    if provider is not None and provider != "auto" and provider not in TIME_PROVIDERS:
        refuse(f"system.time.provider {provider!r} is not a value SPEC §8 "
               f"defines (auto | {' | '.join(sorted(TIME_PROVIDERS))})")
        provider = None
    if ntp is not None and nix_bool(ntp, "system.time.ntp") is None:
        ntp = None

    if ntp is False:
        # timesyncd is stated unconditionally because NixOS starts it by
        # default; a second daemon only when the document named one, and never
        # twice for the same option — a repeat is `attribute already defined`.
        out.append("  services.timesyncd.enable = false;")
        if provider in TIME_PROVIDERS:
            if TIME_PROVIDERS[provider] != "services.timesyncd.enable":
                out.append(f"  {TIME_PROVIDERS[provider]} = false;")
            warn(f"system.time.provider {provider!r} is not started: the same "
                 "document sets system.time.ntp false, and a daemon that is "
                 "not to synchronise the clock has nothing to do")
        if servers:
            warn("system.time.servers[] is not emitted: the same document sets "
                 "system.time.ntp false, so no daemon reads networking.timeServers")
        return out

    if servers:
        out.append(f"  networking.timeServers = {nix_list(servers)};")
    if ntp is None and provider is None:
        return out
    if provider in (None, "auto"):
        # Not a silent default: SPEC §2.3 wants the resolution of `auto` stated.
        # timesyncd is what a NixOS system runs unless a module displaces it,
        # and it is the only one of the three in the base closure.
        warn(f"system.time.provider {provider or '(unset)'} is resolved to "
             "systemd-timesyncd (services.timesyncd.enable), the NTP client a "
             "NixOS system runs by default")
        provider = "systemd-timesyncd"
    out.append(f"  {TIME_PROVIDERS[provider]} = true;")
    return out


NETWORK_MANAGERS = ("auto", "networkmanager", "systemd-networkd", "iwd")


def resolve_manager(network: dict, interfaces: list) -> str:
    """network.manager → the back-end everything else in this section renders for.

    Resolved before a single line is emitted, because the choice decides the
    shape of the interface, wifi and address blocks rather than adding one line
    of its own. `auto` picks NetworkManager — the NixOS default for a machine
    with a desktop — except when an interface is matched by *pattern*, which
    only systemd-networkd can do (see render_glob_interface).
    """
    manager = network.get("manager", "auto")
    if manager not in NETWORK_MANAGERS:
        refuse(f"network.manager {manager!r} is not a value SPEC §10 defines "
               f"({' | '.join(NETWORK_MANAGERS)})")
        return "networkmanager"
    globbed = [i for i, iface in enumerate(interfaces)
               if is_glob(((iface.get("match") or {}).get("name")))]
    if manager != "auto":
        return manager
    if globbed:
        warn("network.manager 'auto' is resolved to systemd-networkd because "
             f"network.interfaces[{globbed[0]}].match.name is a pattern, and "
             "networking.interfaces is keyed by literal interface name — "
             "NetworkManager, which 'auto' otherwise picks, would leave the "
             "pattern unmatched")
        return "systemd-networkd"
    if "manager" in network:
        warn("network.manager 'auto' is resolved to networkmanager "
             "(networking.networkmanager.enable), the back-end a NixOS system "
             "with a graphical session is normally installed with")
    return "networkmanager"


def manager_note(network: dict) -> list[str]:
    """A line in the file for a manager the document never named.

    SPEC §10 makes an omitted `network` section mean "DHCP on everything
    wired", which NetworkManager does — but so does the NixOS default of
    dhcpcd, and the choice between them is the applier's. Said in the output
    rather than through the warning channel: nothing was dropped, and a
    document that names no manager should not have to read a warning.
    """
    if "manager" in network:
        return []
    return ["  # network.manager is unset: NetworkManager is this translator's "
            "default,",
            "  # which covers SPEC §10's \"DHCP on everything wired\". Name "
            "systemd-networkd",
            "  # in network.manager for a machine that should not carry it."]


def proxy_env(doc: dict) -> dict[str, str]:
    """proxy.* → the environment variables the install run needs.

    SPEC §14 makes `proxy` two obligations, not one: it "applies to the
    installation *and* is persisted into the installed system". Only the second
    half reaches configuration.nix, so the first half is set on this process
    before disko and nixos-install are started — behind a filtering network an
    install that goes direct does not go at all.
    """
    proxy = doc.get("proxy", {}) or {}
    env: dict[str, str] = {}
    if http := proxy.get("http"):
        env["http_proxy"] = http
        env["all_proxy"] = http
    if https := (proxy.get("https") or proxy.get("http")):
        env["https_proxy"] = https
    if no_proxy := proxy.get("no_proxy"):
        env["no_proxy"] = ",".join(no_proxy)
    return env


def apply_proxy_env(doc: dict) -> None:
    """Put proxy_env() on this process, and say what it does not reach."""
    env = proxy_env(doc)
    if not env:
        return
    for name, value in env.items():
        os.environ[name] = value
        os.environ[name.upper()] = value
    print(f"proxy for the install run: {', '.join(sorted(env))}")


# The back-end render_network settled on, for the collector to register the
# switches that choice rules out.
_RESOLVED_MANAGER: str | None = None

# Every manager's own on/off switch. The one that was chosen is already in the
# emitted lines; the rest are off, and software.services.enable naming one of
# them is the document contradicting its own network.manager.
MANAGER_OPTION = {"networkmanager": "networking.networkmanager.enable",
                  "systemd-networkd": "networking.useNetworkd",
                  "iwd": "networking.wireless.iwd.enable"}


def claim_network_managers(opts: "NixOptions") -> None:
    """Register the managers network.manager ruled out as switched off.

    A glob interface resolves the manager to systemd-networkd, but
    `software.services.enable: ["NetworkManager"]` reaches a different option
    name, so the collector saw no collision and both were emitted: the .network
    units were written and NetworkManager took the devices anyway, which drops
    the interface section without a word.
    """
    if _RESOLVED_MANAGER is None:
        return
    for manager, option in MANAGER_OPTION.items():
        if manager != _RESOLVED_MANAGER:
            opts.taken(option, "false")


def render_network(doc: dict) -> list[str]:
    global _RESOLVED_MANAGER
    network = doc.get("network", {}) or {}
    out: list[str] = []

    interfaces = network.get("interfaces", []) or []
    wifi = network.get("wifi", []) or []
    manager = resolve_manager(network, interfaces)
    _RESOLVED_MANAGER = manager
    if manager == "networkmanager":
        out += manager_note(network)
        out.append("  networking.networkmanager.enable = true;")
    elif manager == "systemd-networkd":
        out.append("  networking.useNetworkd = true;")
    elif manager == "iwd":
        out.append("  networking.wireless.iwd.enable = true;")

    static = render_interfaces(interfaces, manager, out)
    if static and manager == "networkmanager":
        out.append(f"  networking.networkmanager.unmanaged = {nix_list(static)};")
    if wifi:
        render_wifi(wifi, manager, out)

    # Merged by address rather than emitted per entry: networking.hosts is an
    # attribute set keyed by the address, so two hosts[] entries naming the same
    # IP are `error: attribute already defined` — which under --apply is raised
    # by nixos-install, with the disks already wiped.
    host_names: dict[str, list[str]] = {}
    for index, entry in enumerate(network.get("hosts", []) or []):
        if not valid_address(entry["ip"]):
            refuse(f"network.hosts[{index}].ip {entry['ip']!r} is not an IP "
                   "address; networking.hosts is keyed by address and a name "
                   "there produces an /etc/hosts line nothing resolves")
            consume(entry)
            continue
        names_for = host_names.setdefault(entry["ip"], [])
        names_for += [n for n in entry["names"] if n not in names_for]
    for ip, host_list in host_names.items():
        out.append(f"  networking.hosts.{nix_str(ip)} = {nix_list(host_list)};")

    if firewall := network.get("firewall"):
        render_firewall(firewall, out)

    ssh = network.get("ssh", {}) or {}
    if "enabled" in ssh and (literal := nix_bool(ssh["enabled"],
                                                 "network.ssh.enabled")):
        # Emitted in both directions: `false` is intent, and a role or a later
        # module turning sshd on must lose to it, not silently win.
        out.append(f"  services.openssh.enable = {literal};")
        if literal == "false" and ("password_auth" in ssh or ssh.get("permit_root")):
            warn("network.ssh.password_auth / permit_root have no effect: the same "
                 "document sets network.ssh.enabled false")
    if "password_auth" in ssh:
        if literal := nix_bool(ssh["password_auth"], "network.ssh.password_auth"):
            out.append("  services.openssh.settings.PasswordAuthentication = "
                       f"{literal};")
    if permit_root := ssh.get("permit_root"):
        # SPEC §10's three values are all inside the NixOS enum
        # (services/networking/ssh/sshd.nix settings.PermitRootLogin), so the
        # check is against the spec's list — a value outside it is a type error
        # raised by nixos-install, after disko has wiped.
        if permit_root not in ("no", "prohibit-password", "yes"):
            refuse(f"network.ssh.permit_root {permit_root!r} is not a value "
                   "SPEC §10 defines (no | prohibit-password | yes); "
                   "services.openssh.settings.PermitRootLogin is typed as an "
                   "enum and rejects anything else")
        else:
            out.append("  services.openssh.settings.PermitRootLogin = "
                       f"{nix_str(permit_root)};")
    return out


class NixOptions:
    """A flat block of `option = value;` lines that refuses to define one twice.

    configuration.nix is a single attribute set, so writing the same option on
    two lines is `error: attribute already defined` — an evaluation failure,
    and under --apply one raised *after* disko has wiped the disks. Several
    fields legitimately reach for the same switch (`software.services.enable`
    and `network.ssh.enabled` both mean sshd), so the same value twice is one
    intent stated twice and is collapsed, while two different values is a
    document contradicting itself and is refused.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.values: dict[str, str] = {}

    def taken(self, path: str, value: str) -> None:
        """Record an option another renderer has already written out."""
        self.values[path] = value

    def claim(self, lines: list[str]) -> None:
        """Record every `  path = value;` an earlier section already emitted.

        Only complete one-line definitions are claimed: a block opener
        (`networking.interfaces."eth0" = {`) names an attribute set the
        collector never writes to, so there is nothing to collide with.
        """
        for line in lines:
            match = re.fullmatch(r"  ([A-Za-z0-9_.\"'-]+) = (.+);", line)
            if match and not match.group(2).endswith("{"):
                self.values.setdefault(match.group(1), match.group(2))

    def set(self, path: str, value: str, *, label: str = "") -> None:
        if path in self.values:
            if self.values[path] != value:
                refuse(f"{label or path}: the document asks for two different "
                       f"values of the NixOS option {path} "
                       f"({self.values[path]} and {value})")
            return
        self.values[path] = value
        self.lines.append(f"  {path} = {value};")

    def raw(self, line: str) -> None:
        self.lines.append(line)


# ── software ─────────────────────────────────────────────────────

# `software.role` → the session modules NixOS names. A role is a session, not
# a greeter: which display manager starts it is `desktop.display_manager`, so
# that the two fields cannot contradict each other.
ROLE_SESSION = {
    "desktop:gnome": [("services.xserver.enable", "true"),
                      ("services.xserver.desktopManager.gnome.enable", "true")],
    "desktop:kde": [("services.xserver.enable", "true"),
                    ("services.desktopManager.plasma6.enable", "true")],
    "desktop:xfce": [("services.xserver.enable", "true"),
                     ("services.xserver.desktopManager.xfce.enable", "true")],
    "desktop:sway": [("programs.sway.enable", "true")],
    "desktop:hyprland": [("programs.hyprland.enable", "true")],
}

# The command that starts each role's session. greetd runs one command and
# keeps no session list of its own, so a greeter needs this to have something
# to launch. Each name is the session binary the role's own module puts on the
# system path.
ROLE_COMMAND = {
    "desktop:gnome": "gnome-session",
    "desktop:kde": "startplasma-wayland",
    "desktop:xfce": "startxfce4",
    "desktop:sway": "sway",
    "desktop:hyprland": "Hyprland",
}

# nixos/modules/profiles/minimal.nix — NixOS's own answer to "minimal", copied
# rather than imported because a generated configuration.nix has no stable
# <nixpkgs/nixos/modules/…> path once it is evaluated from a flake. The
# profile uses mkDefault; a document that asks for the role is stating it
# outright, so the values are emitted plain.
MINIMAL_PROFILE = [
    ("documentation.enable", "false"),
    ("documentation.doc.enable", "false"),
    ("documentation.info.enable", "false"),
    ("documentation.man.enable", "false"),
    ("documentation.nixos.enable", "false"),
    ("programs.command-not-found.enable", "false"),
]

# environment.defaultPackages (nixos/modules/config/system-path.nix) — the
# only packages in a NixOS closure that an operator can subtract. Verified
# against nixos-24.11: the default evaluates to exactly these three.
DEFAULT_PACKAGES = ("perl", "rsync", "strace")

# `software.role: server` — NixOS has no server package set, so the only thing
# the role can say here is the one thing it means on a distro whose base
# closure is already headless. Stated rather than left to the default so the
# generated file records the role, and so a greeter asking for X contradicts it.
SERVER_PROFILE = [("services.xserver.enable", "false")]

# `software.exclude[]` on a desktop role. Each desktop module builds its own
# package list and offers exactly one subtraction hook; verified present on
# nixos-24.11 (environment.gnome/plasma6/xfce.excludePackages => OPTION). sway
# and hyprland have no equivalent — programs.sway.extraPackages is a list to
# replace, not a set to subtract from — so they are refused by name below.
DE_EXCLUDE_OPTION = {
    "desktop:gnome": "environment.gnome.excludePackages",
    "desktop:kde": "environment.plasma6.excludePackages",
    "desktop:xfce": "environment.xfce.excludePackages",
}

# systemd unit names (SPEC §11: "services uses systemd unit names as the
# lingua franca") → the NixOS option that owns the unit. Enabling a unit no
# module declares does nothing on NixOS, so only names with a real option are
# accepted; the rest refuse rather than pretend.
SERVICE_OPTIONS = {
    "sshd": "services.openssh.enable",
    "ssh": "services.openssh.enable",
    "tailscaled": "services.tailscale.enable",
    "docker": "virtualisation.docker.enable",
    "podman": "virtualisation.podman.enable",
    "libvirtd": "virtualisation.libvirtd.enable",
    "fail2ban": "services.fail2ban.enable",
    "avahi-daemon": "services.avahi.enable",
    "cups": "services.printing.enable",
    "cronie": "services.cron.enable",
    "cron": "services.cron.enable",
    "bluetooth": "hardware.bluetooth.enable",
    "nfs-server": "services.nfs.server.enable",
    # Every option below returns OPTION on nixos-24.11 (the channel
    # tools/e2e/iso.py:18 pins); a name that reached configuration.nix without
    # one is an evaluation error inside nixos-install, after the wipe.
    "acpid": "services.acpid.enable",
    "atd": "services.atd.enable",
    "auditd": "security.auditd.enable",
    "bind": "services.bind.enable",
    "chrony": "services.chrony.enable",
    "chronyd": "services.chrony.enable",
    "cloud-init": "services.cloud-init.enable",
    "dnsmasq": "services.dnsmasq.enable",
    "dovecot": "services.dovecot2.enable",
    "earlyoom": "services.earlyoom.enable",
    "fstrim": "services.fstrim.enable",
    "fwupd": "services.fwupd.enable",
    "gpm": "services.gpm.enable",
    "haveged": "services.haveged.enable",
    "httpd": "services.httpd.enable",
    "incus": "virtualisation.incus.enable",
    "irqbalance": "services.irqbalance.enable",
    "iwd": "networking.wireless.iwd.enable",
    "k3s": "services.k3s.enable",
    "logrotate": "services.logrotate.enable",
    "lvm2-monitor": "services.lvm.dmeventd.enable",
    "lxd": "virtualisation.lxd.enable",
    "mariadb": "services.mysql.enable",
    "mysql": "services.mysql.enable",
    "mysqld": "services.mysql.enable",
    "named": "services.bind.enable",
    "networkmanager": "networking.networkmanager.enable",
    "nftables": "networking.nftables.enable",
    "nginx": "services.nginx.enable",
    "ntpd": "services.ntp.enable",
    "openntpd": "services.openntpd.enable",
    "pcscd": "services.pcscd.enable",
    "postfix": "services.postfix.enable",
    "postgresql": "services.postgresql.enable",
    "power-profiles-daemon": "services.power-profiles-daemon.enable",
    "qemu-guest-agent": "services.qemuGuest.enable",
    "rpcbind": "services.rpcbind.enable",
    "rsyncd": "services.rsyncd.enable",
    "smartd": "services.smartd.enable",
    "smbd": "services.samba.enable",
    "spice-vdagentd": "services.spice-vdagentd.enable",
    "sysstat": "services.sysstat.enable",
    "syncthing": "services.syncthing.enable",
    "systemd-networkd": "networking.useNetworkd",
    "systemd-oomd": "systemd.oomd.enable",
    "systemd-resolved": "services.resolved.enable",
    "systemd-timesyncd": "services.timesyncd.enable",
    "thermald": "services.thermald.enable",
    "tlp": "services.tlp.enable",
    "tor": "services.tor.enable",
    "udisks2": "services.udisks2.enable",
    "unbound": "services.unbound.enable",
    "upower": "services.upower.enable",
    "vsftpd": "services.vsftpd.enable",
    "wpa_supplicant": "networking.wireless.enable",
    "zerotierone": "services.zerotierone.enable",
}


def check_wireless_backends(opts: "NixOptions") -> None:
    """Refuse the two wireless combinations nixos-24.11 asserts against.

    Both are module *assertions*, not option-name errors, so nothing catches
    them before nixos-install evaluates — by which time disko has already
    wiped the disks. `network.manager` alone can no longer produce either
    combination, but `software.services.enable` reaches the same three
    switches by unit name, from a section that knows nothing about the first.
    """
    on = {path for path, value in opts.values.items() if value == "true"}
    if {"networking.wireless.iwd.enable", "networking.wireless.enable"} <= on:
        refuse("wpa_supplicant and iwd are both enabled: "
               "services/networking/iwd.nix:57-62 asserts that "
               "networking.wireless.enable and networking.wireless.iwd.enable "
               "are mutually exclusive — only one wireless daemon may drive "
               "the radio")
    if ({"networking.networkmanager.enable", "networking.wireless.enable"} <= on
            and opts.values.get("networking.networkmanager.unmanaged",
                                "[ ]").strip() in ("[ ]", "[]")):
        refuse("NetworkManager and wpa_supplicant are both enabled with no "
               "unmanaged interface: services/networking/networkmanager.nix:"
               "549-554 asserts networking.wireless.enable -> "
               "networking.networkmanager.unmanaged != [ ]. Set "
               "network.manager to systemd-networkd, or drop wpa_supplicant "
               "from software.services.enable")


def service_option(unit: str) -> str | None:
    """A systemd unit name → the NixOS option that owns it, or None.

    Matched case-insensitively and with the unit suffix optional, because a
    systemd unit name is neither: the network manager's unit is
    `NetworkManager.service`, and SPEC §11 asks for unit names rather than for
    this table's spelling of them.
    """
    name = str(unit).strip()
    for suffix in (".service", ".socket", ".timer", ".target", ".path"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return SERVICE_OPTIONS.get(name.lower())

FLATHUB_REPO = "https://dl.flathub.org/repo/flathub.flatpakrepo"

# The application sources SPEC §11 defines for `apps[].preference`.
APP_SOURCES = ("native", "flatpak", "snap", "appimage")

APP_SOURCE_REFUSALS = {
    "snap": "snapd is not part of NixOS and the store has no writable "
            "/snap; software.snap[] refuses for the same reason",
}

# `apps[].appimage` used to be refused here beside snap, on the grounds that
# fetching one needs the network at install time. That reason does not survive
# contact with this applier's own behaviour: `software.flatpak[]` and
# `apps[].flatpak` fetch from Flathub on the installed machine's first boot and
# are ⚙ POST with a warning, not a refusal. Neither spec text makes the offline
# claim the refusal rested on — schema.md §11 asks only that an unresolvable
# `apps[]` item warn rather than abort, and delivery.md §7 has the installer
# bring up networking by default. So the two are made consistent the way the
# working one already works: fetch at first boot, warn that it is emulated.
# The one thing an AppImage does not have is flatpak's remote — there is no
# registry to resolve a name against — so the value must be the URL itself.
APPIMAGE_DIR = "/opt/appimages"


def appimage_refusal(name: str, value: str) -> str | None:
    """Why one `apps[].appimage` cannot be installed, or None if it can.

    Two shapes are unusable: a value that is not an http(s) URL (nothing to
    fetch), and an app name that is not a bare filename (it becomes both a path
    under /opt and the wrapper's command name).
    """
    if not re.match(r"^https?://", str(value)):
        return ("an AppImage resolves against no registry, so the field has to "
                f"be the URL of the file itself and {str(value)!r} is not one")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(name)):
        return (f"the app name {str(name)!r} is not usable as a file name under "
                f"{APPIMAGE_DIR} or as the command that runs it")
    return None


def app_source_refusal(source: str, name: str, value: str) -> str | None:
    """Why this applier cannot install `value` from `source`, or None."""
    if source == "appimage":
        return appimage_refusal(name, value)
    return APP_SOURCE_REFUSALS.get(source)


def nix_pkg_path(name: str) -> str:
    """A package name as a Nix attribute path, quoting what is not an identifier.

    `1password` and `python3.11` are both legal package names and only one of
    them is a legal bare Nix attribute path: the other is a syntax error in the
    generated file, raised by nixos-install after disko has wiped the disks.
    Dots stay attribute separators (`python3.11` selects `11` out of `python3`);
    everything else is quoted.

    A leading digit is renamed rather than quoted: a Nix attribute cannot start
    with one, so nixpkgs itself carries `1password-cli` as `_1password-cli`.
    Quoting it produced a file that parsed and then failed to evaluate —
    inside nixos-install, after disko had wiped the disks.
    """
    parts = []
    for index, part in enumerate(name.split(".")):
        if index == 0 and re.fullmatch(r"[0-9][A-Za-z0-9_'-]*", part):
            warn(f"software package {name!r}: a nixpkgs attribute cannot begin "
                 f"with a digit, so the package is taken as '_{part}' — the "
                 "name nixpkgs itself uses (SPEC §2.3 substitution)")
            parts.append("_" + part)
            continue
        parts.append(part if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'-]*", part)
                     else nix_str(part))
    return ".".join(parts)


def nix_optional_pkgs(names: list[str]) -> str:
    """A package list where a name nixpkgs does not have drops out, not throws.

    `pkgs.foo or null` is Nix's own "if the attribute is there"; filtering the
    nulls turns a missing attribute into an absent package instead of an
    evaluation error. That is the severity SPEC §11 gives `apps[]` and
    `exclude[]`, and the only one that cannot fail after the wipe.
    """
    items = " ".join(f"(pkgs.{nix_pkg_path(n)} or null)" for n in names)
    return f"(builtins.filter (p: p != null) [ {items} ])"


def resolve_app(app: dict) -> tuple[str | None, str | None]:
    """Pick the source one `software.apps[]` object is installed from.

    `preference[]` is an ordered preference, not a requirement: a source this
    applier cannot provide is skipped with a warning as long as a later one
    can be, and only refuses when nothing in the list survives. Without a
    preference the arbitration is native-first, as SPEC §11 describes.
    """
    available: dict[str, str] = {}
    if native := (app.get("package") or app.get("name")):
        available["native"] = native
    for source in ("flatpak", "snap", "appimage"):
        if value := app.get(source):
            available[source] = value

    name = app.get("name") or app.get("package") or "?"
    order = list(app.get("preference") or []) or ["native", "flatpak"]
    # Per value, not per source: whether an AppImage is installable depends on
    # what the field says, so the table alone can no longer answer it.
    refusals = {source: reason for source, value in available.items()
                if (reason := app_source_refusal(source, name, value))}
    skipped: list[str] = []
    for source in order:
        if source not in APP_SOURCES:
            refuse(f"software.apps[] {name!r}: preference {source!r} is not an "
                   f"application source LIS defines ({', '.join(APP_SOURCES)})")
            return None, None
        if source not in available:
            continue
        if source in refusals:
            skipped.append(source)
            continue
        for dropped in skipped:
            warn(f"software.apps[] {name!r}: {dropped!r} is preferred over "
                 f"{source!r} but {refusals[dropped]} — installing "
                 f"the {source} source instead")
        # An alternative the arbitration never reached is still a field the
        # document wrote and this applier did not act on. Naming it is the
        # difference between a documented choice and the silent drop the
        # spec forbids.
        for unused in sorted(set(available) - {source} - set(skipped)):
            if reason := refusals.get(unused):
                warn(f"software.apps[] {name!r}: the {unused!r} source is "
                     f"declared but {reason}; the {source} source is "
                     "installed instead")
            else:
                warn(f"software.apps[] {name!r}: the {unused!r} source is "
                     f"declared but {source!r} is installed instead — put it "
                     "first in preference[] if that is the wrong way round")
        return source, available[source]

    for dropped in skipped:
        refuse(f"software.apps[] {name!r}: preference {order} leaves no source "
               f"this applier can install — {dropped!r} is out because "
               f"{refusals[dropped]}")
    if not skipped:
        refuse(f"software.apps[] {name!r}: none of its declared sources "
               f"({', '.join(sorted(available)) or 'none'}) appears in "
               f"preference {order}")
    return None, None


def flatpak_unit(apps: list[str]) -> list[str]:
    """A first-boot unit that adds the flathub remote and installs the apps.

    A flatpak app ID resolves against a *remote*, and NixOS configures none:
    `services.flatpak.enable` installs the runtime and nothing else, so an app
    list on its own reaches nothing (MATRIX §2.10 fn 27, and the same
    missing-remote bug in six other appliers). Installation needs the network
    and a running system, so it cannot be declarative — it is emulated here,
    once, at first boot.
    """
    import shlex

    # Quoted: an application ID is document-supplied text on a shell command
    # line, so an unquoted one is arbitrary code running as root on the first
    # boot of the installed machine.
    body = [f"flatpak remote-add --if-not-exists flathub {shlex.quote(FLATHUB_REPO)}"]
    body += [f"flatpak install -y --noninteractive flathub {shlex.quote(app)}"
             for app in apps]
    body += ["install -d -m755 /var/lib/lis",
             "touch /var/lib/lis/.flatpak-done"]
    return ["  systemd.services.lis-flatpak = {",
            "    description = \"LIS flatpak application install\";",
            "    wantedBy = [ \"multi-user.target\" ];",
            "    after = [ \"network-online.target\" ];",
            "    wants = [ \"network-online.target\" ];",
            "    unitConfig.ConditionPathExists = \"!/var/lib/lis/.flatpak-done\";",
            "    serviceConfig.Type = \"oneshot\";",
            "    path = [ pkgs.flatpak pkgs.coreutils ];",
            "    script = " + nix_script("\n".join(body)) + ";",
            "  };"]


def appimage_wrapper(name: str) -> str:
    """The declarative half: a command on PATH that runs the fetched AppImage.

    Without it the unit leaves a file in /opt that nothing on PATH reaches, and
    `apps[]` asked for an application, not a download. The wrapper is built at
    install time from a path the unit fills in at first boot, so it needs no
    network of its own; `appimage-run` is named explicitly rather than left to
    the binfmt registration so the command works either way.
    """
    return ("(pkgs.writeShellScriptBin %s ''exec ${pkgs.appimage-run}/bin/"
            "appimage-run %s/%s.AppImage \"$@\"'')"
            % (nix_str(name), APPIMAGE_DIR, name))


def appimage_unit(apps: list[tuple[str, str]]) -> list[str]:
    """A first-boot unit that fetches each AppImage into /opt.

    Same shape and same reason as `lis-flatpak`: the fetch needs the network
    and a running system, so it cannot be declarative — it is emulated once, on
    first boot, and marker-guarded so a later boot does not re-download.
    """
    import shlex

    # Quoted for the same reason the flatpak unit quotes its IDs: the URL is
    # document-supplied text on a shell command line running as root.
    body = [f"install -d -m755 {APPIMAGE_DIR}"]
    for name, url in apps:
        dest = f"{APPIMAGE_DIR}/{name}.AppImage"
        body.append(f"curl -fsSL --retry 3 -o {shlex.quote(dest)} "
                    f"{shlex.quote(str(url))}")
        body.append(f"chmod 0755 {shlex.quote(dest)}")
    body += ["install -d -m755 /var/lib/lis",
             "touch /var/lib/lis/.appimage-done"]
    return ["  systemd.services.lis-appimage = {",
            "    description = \"LIS AppImage application fetch\";",
            "    wantedBy = [ \"multi-user.target\" ];",
            "    after = [ \"network-online.target\" ];",
            "    wants = [ \"network-online.target\" ];",
            "    unitConfig.ConditionPathExists = \"!/var/lib/lis/.appimage-done\";",
            "    serviceConfig.Type = \"oneshot\";",
            "    path = [ pkgs.curl pkgs.coreutils ];",
            "    script = " + nix_script("\n".join(body)) + ";",
            "  };"]


def render_software(doc: dict, opts: NixOptions) -> None:
    """software.* → packages, roles, services and application runtimes."""
    software = doc.get("software", {}) or {}
    role = software.get("role", "")

    if role in ROLE_SESSION:
        for path, value in ROLE_SESSION[role]:
            opts.set(path, value, label=f"software.role {role!r}")
    elif role == "minimal":
        for path, value in MINIMAL_PROFILE:
            opts.set(path, value, label="software.role 'minimal'")
    elif role == "server":
        # NixOS ships no server metapackage, but the role is not therefore
        # unprovidable: what "server" means on a distro whose base closure is
        # already headless is *headless*, and that is a statement the file can
        # make. Emitting it also gives NixOptions something to catch a
        # contradiction against — role 'server' plus a greeter that turns the
        # X server on is now a refusal instead of a silently graphical server.
        for path, value in SERVER_PROFILE:
            opts.set(path, value, label="software.role 'server'")
        warn("software.role 'server': NixOS has no server package set to "
             "install — the role is honored as 'headless' "
             "(services.xserver.enable = false) and nothing more. Name what "
             "the server needs in software.packages[] and "
             "software.services.enable[]")
    elif role:
        refuse(f"software.role {role!r} has no default-translator mapping")

    # software.exclude[] — environment.defaultPackages is the only subtraction
    # a bare NixOS closure offers, but a desktop role adds a second one: each
    # desktop module carries its own excludePackages list, which is exactly the
    # "removes packages a role would otherwise pull in" of SPEC §11.
    excluded = list(software.get("exclude", []) or [])
    declared = set(software.get("packages", []) or [])
    from_role = [n for n in excluded if n not in DEFAULT_PACKAGES]
    for name in from_role:
        if name in declared:
            refuse(f"software.exclude {name!r} names a package "
                   "software.packages[] asks to install; the document has to "
                   "say once whether the machine gets it")
    if from_role and (option := DE_EXCLUDE_OPTION.get(role)):
        opts.set(option, nix_optional_pkgs(from_role),
                 label=f"software.exclude under {role!r}")
        warn(f"software.exclude {', '.join(from_role)}: subtracted from the "
             f"{role.split(':')[1]} session through {option}, which matches by "
             "package name — a name that session does not pull in is dropped "
             "silently there (SPEC §11: excluding what no role provides is not "
             "an error)")
    elif from_role and role in ("desktop:sway", "desktop:hyprland"):
        refuse(f"software.exclude {', '.join(from_role)} under {role!r}: the "
               "sway and hyprland modules expose no excludePackages — "
               "programs.sway.extraPackages is a list to replace wholesale, "
               "not a set to subtract from — so the exclusion cannot be "
               "expressed. Set the module's own package list instead")
    elif from_role:
        warn(f"software.exclude {', '.join(from_role)}: software.role "
             f"{role or 'unset'!r} pulls in nothing by those names, and the "
             "only subtractable set in a bare NixOS closure is "
             "environment.defaultPackages "
             f"(nixos/modules/config/system-path.nix: {', '.join(DEFAULT_PACKAGES)}) "
             "— nothing to remove, which SPEC §11 calls not an error")
    if role == "minimal":
        opts.set("environment.defaultPackages", "lib.mkForce [ ]")
    elif any(name in DEFAULT_PACKAGES for name in excluded):
        # Only when a name actually names one of them: rewriting the option to
        # a mkForce of its own default said "the document asked for this" about
        # a list the document never touched.
        keep = [p for p in DEFAULT_PACKAGES if p not in excluded]
        keep_expr = f"(with pkgs; [ {' '.join(keep)} ])" if keep else "[ ]"
        opts.set("environment.defaultPackages", f"lib.mkForce {keep_expr}")

    # Two severities, because SPEC §11 states two: a `packages[]` name the
    # applier cannot resolve "MUST fail (not skip)", while an unresolvable
    # `apps[]` item "emit[s] a non-fatal warning … rather than aborting
    # installation". Both used to be spliced into one `with pkgs; [ … ]`, which
    # gave apps[] the packages[] severity — and gave it at the worst moment,
    # since an undefined attribute is an eval error nixos-install raises after
    # disko has already wiped the disks.
    packages = list(software.get("packages", []) or [])
    optional: list[str] = []
    flatpaks = list(software.get("flatpak", []) or [])
    appimages: list[tuple[str, str]] = []
    for app in software.get("apps", []) or []:
        if isinstance(app, str):
            optional.append(app)
            continue
        source, value = resolve_app(app)
        if source == "native":
            optional.append(value)
        elif source == "flatpak":
            flatpaks.append(value)
        elif source == "appimage":
            appimages.append((app.get("name") or app.get("package"), value))

    terms = []
    if packages:
        # `pkgs.<name>` and not `with pkgs; [ <name> ]`: a package name that is
        # not a bare Nix identifier (`1password`) has to be quoted, and inside
        # a `with` list a quoted name is a *string* in the list rather than the
        # package — environment.systemPackages is types.listOf package, so that
        # is a type error at evaluation time, after the wipe.
        terms.append("[ %s ]" % " ".join(f"pkgs.{nix_pkg_path(p)}"
                                         for p in packages))
    if optional:
        terms.append(nix_optional_pkgs(optional))
    if appimages:
        terms.append("[ %s ]" % " ".join(appimage_wrapper(name)
                                         for name, _ in appimages))
    if terms:
        if packages:
            opts.raw("  # software.packages[]: names pass through verbatim, and "
                     "an unresolvable one fails the build (SPEC §11).")
        if optional:
            opts.raw("  # software.apps[]: an item nixpkgs has no attribute for "
                     "is dropped, not fatal (SPEC §11).")
            warn("software.apps[]: application names are resolved against "
                 "nixpkgs at build time and an item with no attribute of that "
                 "name is dropped — SPEC §11 makes that non-fatal, so the "
                 "install will not say which ones went missing")
        opts.set("environment.systemPackages", " ++ ".join(terms))

    services = software.get("services", {}) or {}
    # network.ssh writes services.openssh itself; read it so that asking for
    # sshd in both places is one switch rather than a duplicate attribute.
    if ((doc.get("network") or {}).get("ssh") or {}).get("enabled") is not None:
        enabled = ((doc.get("network") or {}).get("ssh") or {})["enabled"]
        opts.taken("services.openssh.enable", str(bool(enabled)).lower())
    for unit in services.get("enable", []) or []:
        if option := service_option(unit):
            opts.set(option, "true", label=f"software.services.enable {unit!r}")
        else:
            near = difflib.get_close_matches(str(unit).lower(),
                                             SERVICE_OPTIONS, n=3, cutoff=0.6)
            refuse(f"software.services.enable {unit!r}: NixOS builds its unit "
                   "set from modules, so enabling a unit no module declares is "
                   "a no-op — this translator maps the "
                   f"{len(SERVICE_OPTIONS)} unit names that have an option"
                   + (f" (did you mean {', '.join(near)}?)" if near else "")
                   + "; declare the rest in your own module")
    suppressed: list[str] = []
    for unit in services.get("disable", []) or []:
        if option := service_option(unit):
            opts.set(option, "false", label=f"software.services.disable {unit!r}")
        else:
            # systemd.suppressedSystemUnits removes the unit file from the
            # generated system, which is the one mechanism that works on a
            # unit this configuration never declares. `systemd.services.<n>.
            # enable = false` would be the silent no-op instead.
            suppressed.append(unit if "." in unit else unit + ".service")
    if suppressed:
        opts.set("systemd.suppressedSystemUnits", nix_list(suppressed))
    check_wireless_backends(opts)

    if flatpaks or software.get("flatpak") is not None:
        opts.set("services.flatpak.enable", "true")
        # nixos/modules/services/desktops/flatpak.nix asserts
        # `xdg.portal.enable == true`, and the portal module in turn asserts
        # that at least one implementation is present. Neither is optional:
        # emitting services.flatpak.enable on its own — which is all this
        # translator used to do — does not evaluate at all. The list merges,
        # so a desktop that ships its own portal keeps it too.
        opts.set("xdg.portal.enable", "true")
        opts.set("xdg.portal.extraPortals", "[ pkgs.xdg-desktop-portal-gtk ]")
        # xdg-desktop-portal 1.17 stopped picking a backend on its own, and
        # nixpkgs warns during evaluation when neither config nor
        # configPackages says which to use. Without it a flatpak app's file
        # chooser and screenshot portals resolve to nothing on a machine with
        # no desktop module of its own.
        opts.set("xdg.portal.config.common.default", nix_str("*"))
    if flatpaks:
        opts.lines.extend(flatpak_unit(flatpaks))
        warn("software.flatpak[]: flatpak applications are installed by a "
             "first-boot unit (lis-flatpak), not by the configuration — the "
             "machine needs the network on its first boot")

    if appimages:
        # programs.appimage.enable brings appimage-run in; binfmt registers the
        # two AppImage magics so the fetched file also runs when it is invoked
        # directly (nixos/modules/programs/appimage.nix, both verified on
        # nixos-24.11).
        opts.set("programs.appimage.enable", "true")
        opts.set("programs.appimage.binfmt", "true")
        opts.lines.extend(appimage_unit(appimages))
        warn("software.apps[].appimage: the AppImage is fetched by a first-boot "
             "unit (lis-appimage) into " + APPIMAGE_DIR + ", not by the "
             "configuration — the machine needs the network on its first boot, "
             "and the file is not in the store, so it is neither rolled back "
             "nor rebuilt by nixos-rebuild")

    if (snaps := software.get("snap")) is not None:
        # Consumed so the refusal is the single diagnostic: without it the
        # tracker reports each of name/channel/classic a second time as
        # "declared but never read", which reads like three more problems.
        consume(snaps)
        refuse("software.snap[] is not available on NixOS: snapd needs a "
               "writable /snap and the FHS mount namespaces the store does not "
               "provide, and nixpkgs ships no snapd service module")


# ── desktop and drivers ──────────────────────────────────────────

# Verified against nixos-24.11: `services.displayManager.gdm` does not exist
# on this release (the move out of services.xserver landed later), while sddm
# and ly are already under services.displayManager. Getting either one wrong
# is an evaluation error, not a drop.
DM_OPTIONS = {
    "gdm": "services.xserver.displayManager.gdm.enable",
    "sddm": "services.displayManager.sddm.enable",
    "lightdm": "services.xserver.displayManager.lightdm.enable",
    "ly": "services.displayManager.ly.enable",
}

# Greeters that need an X server of their own. sddm and ly run on Wayland.
DM_NEEDS_XSERVER = ("gdm", "lightdm")

# `display_manager: auto` — the greeter each role is normally started by.
ROLE_DM = {
    "desktop:gnome": "gdm",
    "desktop:kde": "sddm",
    "desktop:xfce": "lightdm",
    "desktop:sway": "greetd",
    "desktop:hyprland": "greetd",
}

TUIGREET = "${pkgs.greetd.tuigreet}/bin/tuigreet --time --remember"


def render_desktop(doc: dict, opts: NixOptions) -> None:
    """desktop.* → greeter, autologin and session plumbing."""
    desktop = doc.get("desktop") or {}
    role = (doc.get("software", {}) or {}).get("role", "")

    if desktop and not role.startswith("desktop:"):
        warn("desktop.* is declared without a desktop:* software.role; "
             "schema.md §12 says the section must be absent unless the role is "
             "a desktop one — the plumbing is emitted as declared")

    asked = desktop.get("display_manager", "auto")
    manager = ROLE_DM.get(role) if asked == "auto" else asked
    if manager == "none":
        manager = None
    if manager and manager not in DM_OPTIONS and manager != "greetd":
        refuse(f"desktop.display_manager {manager!r} has no NixOS module in the "
               "default translator")
        manager = None

    session = ROLE_COMMAND.get(role)
    if manager in DM_OPTIONS:
        if manager in DM_NEEDS_XSERVER:
            opts.set("services.xserver.enable", "true",
                     label=f"desktop.display_manager {manager!r}")
        opts.set(DM_OPTIONS[manager], "true")
        if manager == "sddm" and "services.xserver.enable" not in opts.values:
            # sddm.nix asserts that one of services.xserver.enable and
            # services.displayManager.sddm.wayland.enable is set. A Wayland
            # role (sway, hyprland) turns on neither, so sddm has to be told
            # to run its Wayland greeter or the configuration does not
            # evaluate at all.
            opts.set("services.displayManager.sddm.wayland.enable", "true")
    elif manager == "greetd":
        if session is None:
            refuse("desktop.display_manager 'greetd': greetd starts a single "
                   "session command and keeps no session list "
                   "(nixos/modules/services/display-managers/greetd.nix), so "
                   "without a desktop:* software.role there is nothing for it "
                   "to launch")
            manager = None
        else:
            opts.set("services.greetd.enable", "true")
            opts.set("services.greetd.settings.default_session.command",
                     f'"{TUIGREET} --cmd {session}"')
    elif asked == "none" and role in ROLE_SESSION:
        warn(f"desktop.display_manager 'none' with software.role {role!r}: the "
             "session is installed but nothing starts it — log in on a tty and "
             "start it by hand")

    if autologin := desktop.get("autologin"):
        accounts = {user["name"]: user for user in doc.get("users", []) or []}
        locked = ((accounts.get(autologin) or {}).get("password") or {}).get("locked")
        if autologin not in accounts:
            refuse(f"desktop.autologin {autologin!r} names an account this "
                   "document does not declare (schema.md §12: autologin must "
                   "name an existing user)")
        elif locked:
            # schema.md §12 names this one outright: "combining it with a
            # locked user is a validation error". NixOS would not catch it —
            # autoLogin.user takes any string — so the machine would come up
            # trying to start a session as an account that cannot authenticate.
            refuse(f"desktop.autologin {autologin!r} names a user whose "
                   "password is locked; schema.md §12 makes that combination a "
                   "validation error")
        elif manager is None:
            # A greeter is one way to log a user in and not the only one:
            # services.getty.autologinUser is the console's own, and it is what
            # "log this user in automatically, no display manager" means on a
            # machine with no greeter. Verified on nixos-24.11 (=> OPTION).
            opts.set("services.getty.autologinUser", nix_str(autologin),
                     label="desktop.autologin")
            warn(f"desktop.autologin {autologin!r} with "
                 "desktop.display_manager resolved to none: there is no "
                 "greeter to log in through, so it is honored on the console "
                 "instead (services.getty.autologinUser) — tty1 comes up "
                 "logged in and no graphical session is started")
        elif manager == "ly":
            refuse("desktop.autologin with ly: the ly module asserts "
                   "!services.displayManager.autoLogin.enable "
                   "(nixos/modules/services/display-managers/ly.nix), so the "
                   "combination cannot be evaluated — ly has no autologin of "
                   "its own")
        elif manager == "greetd":
            # greetd ignores services.displayManager.autoLogin entirely; its
            # own autologin is an initial_session that runs before the greeter.
            opts.set("services.greetd.settings.initial_session.command",
                     f'"{session}"')
            opts.set("services.greetd.settings.initial_session.user",
                     nix_str(autologin))
        else:
            opts.set("services.displayManager.autoLogin.enable", "true")
            opts.set("services.displayManager.autoLogin.user", nix_str(autologin))
            if role in ROLE_SESSION:
                # sddm and gdm pick the autologin session out of the session
                # list, which is ambiguous the moment a second one is
                # installed; naming it keeps the choice the document's.
                opts.set("services.displayManager.defaultSession",
                         nix_str(ROLE_DEFAULT_SESSION[role]))

    if not desktop:
        return
    audio = desktop.get("audio", "auto")
    if audio in ("auto", "pipewire"):
        opts.set("services.pipewire.enable", "true")
        opts.set("services.pipewire.alsa.enable", "true")
        opts.set("services.pipewire.pulse.enable", "true")
    elif audio == "pulseaudio":
        # `services.pulseaudio` does not exist on the release this translator
        # targets: the rename from `hardware.pulseaudio` landed after 24.11,
        # so the new spelling is an evaluation error — and under --apply it is
        # one raised *after* disko has wiped the disks.
        opts.set("hardware.pulseaudio.enable", "true")
        # Every desktop role turns pipewire on by default, and
        # nixos/modules/services/audio/pipewire.nix asserts that pulseaudio is
        # off. Asking for pulseaudio without saying so fails the whole
        # evaluation, so the document's choice has to displace the default.
        opts.set("services.pipewire.enable", "lib.mkForce false")
    elif audio == "none":
        # A desktop module turns a sound server on by default, so "none" has
        # to say so out loud or it is not honored at all.
        opts.set("services.pipewire.enable", "lib.mkForce false")
        opts.set("hardware.pulseaudio.enable", "lib.mkForce false")
    if desktop.get("bluetooth") is not None:
        opts.set("hardware.bluetooth.enable",
                 str(bool(desktop["bluetooth"])).lower(), label="desktop.bluetooth")
    if desktop.get("printing") is not None:
        opts.set("services.printing.enable",
                 str(bool(desktop["printing"])).lower(), label="desktop.printing")
        # CUPS on its own finds a USB printer and nothing on the network: every
        # driverless (IPP Everywhere) printer is discovered over mDNS, which on
        # NixOS is avahi plus the nsswitch entry that resolves .local. Skipped
        # when the document already stated its own answer for avahi through
        # software.services, so "printing yes, mDNS no" stays sayable.
        if desktop["printing"] and "services.avahi.enable" not in opts.values:
            opts.set("services.avahi.enable", "true")
            opts.set("services.avahi.nssmdns4", "true")
            opts.set("services.avahi.openFirewall", "true")
            warn("desktop.printing: CUPS discovers network printers over mDNS, "
                 "so avahi is enabled alongside it with nssmdns4 and "
                 "services.avahi.openFirewall = true (UDP 5353 inbound). Put "
                 "'avahi-daemon' in software.services.disable[] to keep "
                 "printing local-only")


# The session name each role registers, for services.displayManager.defaultSession.
ROLE_DEFAULT_SESSION = {
    "desktop:gnome": "gnome",
    "desktop:kde": "plasma",
    "desktop:xfce": "xfce",
    "desktop:sway": "sway",
    "desktop:hyprland": "hyprland",
}


def render_drivers(doc: dict, opts: NixOptions) -> None:
    """drivers.gpu / drivers.firmware → video drivers and firmware sets."""
    drivers = doc.get("drivers", {}) or {}
    gpu = drivers.get("gpu")
    if gpu in ("amdgpu", "intel"):
        opts.set("services.xserver.videoDrivers", nix_list([gpu]))
    elif gpu in ("nvidia", "nvidia-open"):
        opts.set("services.xserver.videoDrivers", nix_list(["nvidia"]))
        opts.set("hardware.nvidia.open", str(gpu == "nvidia-open").lower())
        # The nvidia driver is unfree; without this the configuration stops
        # with "Package 'nvidia-x11' has an unfree license", which under
        # --apply lands after disko has already wiped the disks.
        opts.set("nixpkgs.config.allowUnfree", "true")
    elif gpu == "auto":
        # Read and answered rather than read and dropped. Leaving
        # services.xserver.videoDrivers unset *is* the automatic choice on
        # NixOS: xserver.nix defaults it to [ "modesetting" "fbdev" ], the
        # kernel binds the DRM driver for whatever card is present, and
        # hardware.enableRedistributableFirmware (hardware.nix) supplies the
        # blobs. SPEC §19 wants that resolution recorded, not assumed.
        opts.raw("  # drivers.gpu: auto — no vendor driver is pinned; "
                 "services.xserver.videoDrivers keeps its default of "
                 "[ \"modesetting\" \"fbdev\" ] and the kernel's own DRM "
                 "driver binds the card.")
    elif gpu == "none":
        opts.raw("  # drivers.gpu: none — no vendor driver package is "
                 "installed; the kernel's in-tree modesetting driver is all "
                 "the machine gets.")
    elif gpu is not None:
        refuse(f"drivers.gpu {gpu!r} has no NixOS mapping")

    # hardware.nix writes hardware.cpu.<vendor>.updateMicrocode for an explicit
    # `intel` or `amd`; `auto` and `none` were read by nothing at all. Both
    # options default to false on 24.11 (hardware/cpu/intel-microcode.nix:13),
    # so `auto` was silently installing no microcode — the opposite of what it
    # asks for — and `none` was right only by accident.
    microcode = drivers.get("microcode")
    if microcode == "auto":
        # Both blobs, because the vendor is not known until the machine boots
        # and a CPU ignores the other vendor's update. This is what
        # nixos-generate-config writes when it cannot tell either.
        opts.set("hardware.cpu.intel.updateMicrocode", "true",
                 label="drivers.microcode 'auto'")
        opts.set("hardware.cpu.amd.updateMicrocode", "true",
                 label="drivers.microcode 'auto'")
        warn("drivers.microcode 'auto': the target CPU vendor is not known at "
             "translate time, so both the Intel and the AMD microcode images "
             "are prepended to the initrd — a CPU applies only its own. Name "
             "the vendor to carry one")
    elif microcode == "none":
        opts.set("hardware.cpu.intel.updateMicrocode", "false",
                 label="drivers.microcode 'none'")
        opts.set("hardware.cpu.amd.updateMicrocode", "false",
                 label="drivers.microcode 'none'")
    elif microcode not in (None, "intel", "amd"):
        refuse(f"drivers.microcode {microcode!r} has no NixOS mapping")

    # `auto` and `none` are already answered in hardware.nix, which writes
    # hardware.enableRedistributableFirmware for every document. Only "all"
    # was being dropped: it is a strictly larger set than the redistributable
    # one and has its own option (MATRIX §2.11 fn 10).
    firmware = drivers.get("firmware")
    if firmware == "all":
        opts.set("hardware.enableAllFirmware", "true")
        # all-firmware.nix asserts on allowUnfree — the non-redistributable
        # blobs cannot even be evaluated without it.
        opts.set("nixpkgs.config.allowUnfree", "true")
    elif firmware not in (None, "auto", "none"):
        refuse(f"drivers.firmware {firmware!r} has no NixOS mapping")


# SPEC §17.1 names the whole type vocabulary. The JSON schema types the field
# as a bare string, so a misspelling validates cleanly and then selects nothing
# — which is how a document asking for a hardware-token unlock can install a
# machine whose only key is the seed passphrase and be reported as honored.
KEY_TYPES = {"yubikey_fido2", "yubikey_challenge", "tpm2", "gpg", "age",
             "keyfile", "passphrase", "ssh"}

# Types this applier can actually turn into key material or an enrollment.
# `keyfile` reaches disko as a passwordFile (lis_common.luks_key_path); `tpm2`
# and `fido2` are the two flags lis_common.enrollment_commands can hand to
# systemd-cryptenroll.
KEY_TYPES_HONORED = {"keyfile", "tpm2", "fido2"}

# Only one purpose is consumed by any installer in this repository. The rest
# describe Phase-2 identity work — PAM, SSH, payload decryption — that nothing
# in a NixOS translation reads.
KEY_PURPOSES = {"payload_decryption", "disk_encryption", "secret_decryption",
                "user_ssh_key", "user_pam_auth", "remote_auth"}

# The PAM module each token type authenticates through on 24.11. Both are real
# options — security.pam.u2f.enable and security.pam.yubico.enable — so
# `purpose: user_pam_auth` is a line nobody wrote rather than a capability
# NixOS lacks; it used to be refused with the same sentence as the four roles
# that genuinely have nowhere to go.
PAM_KEY_MODULES = {"yubikey_fido2": "u2f", "yubikey_challenge": "yubico"}


def render_keys_pam(doc: dict) -> list[str]:
    """keys[].purpose: user_pam_auth → the PAM module for that token type.

    `control = "sufficient"` and nothing stronger. pam_u2f consults a mapping
    file that only an operator with the token in hand can write (pamu2fcfg
    prompts for a touch), so a `required` stack would lock every account out of
    the machine this applier just installed. `sufficient` adds the token as an
    additional way in and leaves the password path intact, which is the only
    setting that is safe to reach unattended — check_keys() warns that the
    enrollment half is the operator's.
    """
    out: list[str] = []
    for module in dict.fromkeys(
            PAM_KEY_MODULES[entry["type"]]
            for entry in doc.get("keys", []) or []
            if "user_pam_auth" in (entry.get("purpose", []) or [])
            and entry.get("type") in PAM_KEY_MODULES):
        out.append(f"  security.pam.{module}.enable = true;")
        out.append(f"  security.pam.{module}.control = \"sufficient\";")
        if module == "u2f":
            # cue makes pam_u2f print "touch your token" instead of appearing
            # to hang; without it a sufficient module that is waiting looks
            # like a dead login prompt.
            out.append("  security.pam.u2f.settings.cue = true;")
        else:
            # The default mode is `client`, which sends the OTP to Yubico's
            # validation service over the network. A key declared for local
            # PAM authentication is challenge-response, and choosing the
            # default would make every login depend on an internet round trip.
            out.append("  security.pam.yubico.mode = \"challenge-response\";")
    return out


def check_keys(doc: dict) -> None:
    """keys[] — honor what reaches the target, refuse the rest by name.

    Every leaf of this section used to be either unread or read by exactly one
    helper with no diagnostic: `id`, `match` and `pin_required` reached nothing,
    an unknown `type` selected nothing, a `purpose` other than disk_encryption
    was a no-op, and `gpg`/`age` handed *ciphertext* to cryptsetup as if it were
    a key. The whole section then earned one warning that said enrollment would
    happen. Fail closed instead (SPEC §2.3): a key the machine will not have is
    not a detail to warn about, it is a machine that does not unlock.
    """
    material = []
    for index, entry in enumerate(doc.get("keys", []) or []):
        kid = entry.get("id") or f"#{index}"
        ktype = entry.get("type")
        purposes = list(entry.get("purpose", []) or [])
        # Read before any branch returns, so that a refusal on one leaf does
        # not leave a sibling looking like intent nobody ever consulted.
        source = secret_ref(entry.get("source"))
        consume(entry.get("match", {}) or {})

        if ktype is None:
            refuse(f"keys['{kid}']: no type — SPEC §17.1 makes it the field that "
                   "says what the key is, and nothing selects a key without it")
            continue
        if ktype not in KEY_TYPES and ktype not in KEY_TYPES_HONORED:
            refuse(f"keys['{kid}'].type {ktype!r} is not one of SPEC §17.1's types "
                   f"({', '.join(sorted(KEY_TYPES))}) — the schema types this field "
                   "as a bare string, so a misspelling validates and then matches "
                   "no key at all")
            continue
        if entry.get("match"):
            refuse(f"keys['{kid}'].match: no applier in this repository evaluates "
                   "hardware-token matching rules, and NixOS has no option that "
                   "selects a token by serial or vendor — the key would be used "
                   "whatever token happened to be plugged in")
        if entry.get("pin_required"):
            refuse(f"keys['{kid}'].pin_required: enrollment goes through "
                   "lis_common.enrollment_commands, which runs systemd-cryptenroll "
                   "without --tpm2-with-pin, so the slot would be created with no "
                   "PIN and the document's requirement would be silently relaxed")

        if not purposes:
            refuse(f"keys['{kid}']: no purpose — SPEC §17.1 makes purpose[] what "
                   "binds a key to a job, and an entry with none is consulted by "
                   "nothing")
            continue
        for purpose in purposes:
            # One refusal per role, naming the thing that is missing. The old
            # branch turned down five of SPEC §17.1's six roles with the same
            # sentence — "neither of which has anywhere to consume a key for
            # that role" — which was already wrong for user_pam_auth, where
            # NixOS has two PAM modules, and told the reader nothing about the
            # other four.
            if purpose not in KEY_PURPOSES:
                refuse(f"keys['{kid}'].purpose {purpose!r} is not one of SPEC §17.1's "
                       f"roles ({', '.join(sorted(KEY_PURPOSES))})")
            elif purpose == "user_pam_auth" and ktype not in PAM_KEY_MODULES:
                refuse(f"keys['{kid}'].purpose 'user_pam_auth' with type {ktype!r}: "
                       "NixOS 24.11 has a PAM module for FIDO2 tokens "
                       "(security.pam.u2f) and one for Yubico challenge-response "
                       "(security.pam.yubico), and none that authenticates a "
                       f"{ktype} — the account's PAM stack would be unchanged")
            elif purpose == "user_ssh_key":
                refuse(f"keys['{kid}'].purpose 'user_ssh_key': SPEC §17.2 binds a "
                       "key to an account through users[].ssh_keys: "
                       "[{from: 'key:…'}], which schema v0.1 does not carry — "
                       "users[].ssh_authorized_keys is an array of literal key "
                       "strings — so there is no account this key names and no "
                       "authorized_keys file it would be written to")
            elif purpose == "payload_decryption":
                refuse(f"keys['{kid}'].purpose 'payload_decryption': that is SPEC "
                       "§17's Phase 1, decrypting the payload before the document "
                       "is read — this applier is handed an already-plain "
                       "document and has no payload stage to give the key to")
            elif purpose == "secret_decryption":
                refuse(f"keys['{kid}'].purpose 'secret_decryption': nothing here "
                       "decrypts a secret reference. SPEC §17.2's "
                       "files[].content.decrypt_with is not in schema v0.1, and "
                       "every seed: reference this applier resolves is read "
                       "verbatim (lis_common.secret_ref)")
            elif purpose == "remote_auth":
                refuse(f"keys['{kid}'].purpose 'remote_auth': the only remote "
                       "service LIS names is SPEC §15 registration, which this "
                       "applier refuses whole — NixOS has no subscription client "
                       "to hand a credential to")
        if "user_pam_auth" in purposes and ktype in PAM_KEY_MODULES:
            warn(f"keys['{kid}'].purpose user_pam_auth: "
                 f"security.pam.{PAM_KEY_MODULES[ktype]} is enabled for every "
                 "PAM service, but the token itself is not enrolled — pamu2fcfg "
                 "and ykpamcfg both need the key physically present and touched, "
                 "which no unattended install can do. The module is set "
                 "`sufficient`, so password login still works until an operator "
                 "enrolls the token")
        if "disk_encryption" not in purposes:
            continue

        if ktype in ("gpg", "age"):
            # lis_common.luks_key_path returns the seed path for these types and
            # emit_content() hands it to disko as passwordFile, which cryptsetup
            # reads byte for byte. Nothing on the path decrypts the file, so the
            # container is created with the *armoured ciphertext* as its
            # passphrase and no later unlock can reproduce it.
            refuse(f"keys['{kid}'].type {ktype!r}: the material is handed to disko as "
                   "a passwordFile and used verbatim — nothing in this applier "
                   "decrypts it, so the container would be keyed on the encrypted "
                   "file's own bytes; export the key to a plain keyfile on the seed "
                   "and declare type keyfile")
            continue
        if ktype not in KEY_TYPES_HONORED:
            refuse(f"keys['{kid}'].type {ktype!r} with purpose disk_encryption: this "
                   "applier can only turn 'keyfile' into disko key material and "
                   "'tpm2'/'fido2' into a systemd-cryptenroll flag; the entry would "
                   "be read by nothing and the container would fall back to the "
                   "seed passphrase")
            continue
        if ktype == "keyfile":
            if not source:
                refuse(f"keys['{kid}'].source: a keyfile needs a secret reference "
                       "such as {from: 'seed:keys/luks-root.key'} (SPEC §2.4); "
                       "without one there is no key material to give disko")
                continue
            material.append(kid)

    if len(material) > 1:
        # luks_key_path() returns the *first* match and never looks again, so a
        # second keyfile is not a second key — it is a key the document declared
        # and the machine will not have.
        refuse("keys[]: " + ", ".join(repr(k) for k in material)
               + " all declare type keyfile with purpose disk_encryption, but LIS "
               "v0.1 has no way to say which container takes which key "
               "(storage.encryption[].key names a keyfile or a passphrase, not a "
               "keys[] id — SPEC §17.2 lists the cross-reference but the schema "
               "does not carry it), so the first one would silently key every "
               "container")
    elif material:
        warn(f"keys['{material[0]}'] is used for every storage.encryption[] "
             "container: SPEC §17.2's keys[].id cross-reference is not expressible "
             "in schema v0.1, so the binding is by purpose, not by id")


def render_security(doc: dict) -> list[str]:
    """system.security.module → the Linux security module, actually switched on.

    The failure this replaces is one the audit found on three other appliers:
    `module: apparmor` installs the userland tools and stops there, so the
    machine boots with no LSM and the report says the request was honored.
    NixOS can do the whole thing declaratively — `security.apparmor.enable`
    adds `apparmor=1 security=apparmor` to the kernel command line
    (nixos/modules/security/apparmor.nix:194-197 on 24.11), which is what
    actually activates the module, and pulls in the parser, aa-status and the
    profile-loading unit.
    """
    module = ((doc.get("system", {}) or {}).get("security") or {}).get("module")
    out: list[str] = []
    if module == "apparmor":
        out += ["  security.apparmor.enable = true;",
                # `enable` on its own leaves the parser with an empty include
                # path, so any profile that says `include <abstractions/base>`
                # — which is nearly all of them — fails to load. nixpkgs ships
                # the same line as an opt-in module that is not in
                # module-list.nix (security/apparmor/profiles.nix:6).
                "  security.apparmor.packages = [ pkgs.apparmor-profiles ];"]
        warn("system.security.module apparmor: the LSM is enabled and the "
             "upstream abstractions are on the parser include path, but LIS has "
             "no vocabulary for profiles and NixOS confines only what "
             "security.apparmor.policies names — which is empty here, so nothing "
             "beyond the profiles other NixOS modules declare is confined")
    elif module == "none":
        # Explicit, not inherited: 'none' is a decision the document made, and
        # a file that simply omits the option reads the same as one that never
        # considered it.
        out.append("  security.apparmor.enable = false;")
    elif module == "selinux":
        refuse("system.security.module selinux: NixOS 24.11 has no SELinux module "
               "— nothing in nixos/modules enables it, the kernel is built without "
               "CONFIG_SECURITY_SELINUX, and there is no policy store; AppArmor is "
               "the only LSM this applier can switch on")
    elif module == "auto":
        # 'auto' asks for the distro's own default, and NixOS's is no LSM.
        # Emitted as a comment so the file distinguishes "asked for the default"
        # from "nobody looked".
        out.append("  # system.security.module: auto — NixOS enables no LSM by "
                   "default; set apparmor to turn one on.")
    elif module is not None:
        refuse(f"system.security.module {module!r} is not a value SPEC §8 defines "
               "(auto | selinux | apparmor | none)")
    return out


def render_users(doc: dict) -> list[str]:
    """users[] → users.users.<name>, the groups they name, sudo and shells."""
    out: list[str] = []
    nopasswd: list[str] = []
    withpasswd: list[str] = []
    declared: list[str] = []
    # An authorized_keys file on a machine with no sshd authorises nothing.
    # Both spellings count: network.ssh.enabled and software.services.enable
    # reach the same services.openssh.enable (see render_software).
    sshd = bool(((doc.get("network", {}) or {}).get("ssh", {}) or {}).get("enabled")) \
        or bool({"sshd", "ssh"} & set(((doc.get("software", {}) or {})
                                       .get("services", {}) or {}).get("enable", []) or []))
    seen: set[str] = set()
    for user in doc.get("users", []) or []:
        name = user["name"]
        if name in seen:
            # Two entries become two `users.users.<name> = { … }` definitions
            # in one attribute set, which is "attribute already defined" — an
            # evaluation error, and under --apply one raised by nixos-install
            # after disko has wiped the disks. The schema does not make the
            # name unique, so nothing else catches it.
            refuse(f"users[]: {name!r} is declared twice — one account cannot "
                   "take two sets of fields, and the schema does not say which "
                   "of the two would win")
            continue
        seen.add(name)
        out.append(f"  users.users.{nix_attr(name)} = {{")
        if name != "root":
            out.append("    isNormalUser = true;")
        if user.get("uid") is not None:
            if name == "root" and user["uid"] != 0:
                # users.users.root.uid defaults to 0 and update-users-groups.pl
                # keys the account by name, so a foreign uid here produces a
                # /etc/passwd whose root line is not uid 0 — an unbootable
                # system, and one the evaluation does not object to.
                refuse(f"user 'root': uid {user['uid']} — the superuser is uid 0 "
                       "by definition (SPEC §9 configures the root account, it "
                       "does not renumber it)")
            else:
                out.append(f"    uid = {user['uid']};")
        if user.get("comment"):
            out.append(f"    description = {nix_str(user['comment'])};")
        groups = list(user.get("groups", []))
        if user.get("admin") and "wheel" not in groups:
            groups.insert(0, "wheel")
        for group in groups:
            if not GROUP_NAME.fullmatch(group):
                refuse(f"user '{name}': group {group!r} is not a usable group "
                       "name — useradd/groupadd accept "
                       "[a-z_][a-z0-9_-]* with an optional trailing '$'")
            elif group not in declared:
                declared.append(group)
        if groups:
            # No longer skipped for root. users.users.root is an ordinary entry
            # of the same submodule (config/users-groups.nix:702-719 documents
            # it as "This can also be used to set options for root"), so
            # extraGroups works there exactly as it does for anyone else; the
            # old guard dropped `groups` for root without a word.
            out.append(f"    extraGroups = {nix_list(groups)};")
        password = user.get("password") or {}
        if password.get("plain"):
            # SPEC §2.4: documents never carry plaintext secrets.
            refuse(f"user '{name}': password.plain is a plaintext secret")
        else:
            # password_field() implements SPEC §9's rule that `locked` wins
            # while keeping the hash: '!' in front of the stored crypt(3) field
            # is what `passwd -l` writes, so `passwd -u` can undo it later.
            # NixOS copies hashedPassword into /etc/shadow verbatim
            # (config/users-groups.nix:344), so the prefix arrives intact.
            if password.get("locked") and password.get("hash"):
                out.append("    # locked with the hash kept, as passwd -l writes "
                           "it; NixOS's hash-shape")
                out.append("    # heuristic warns about the '!' prefix "
                           "(users-groups.nix:1196) — expected.")
            out.append(f"    hashedPassword = {nix_str(password_field(user) or '!')};")
        if keys := user.get("ssh_authorized_keys"):
            out.append(f"    openssh.authorizedKeys.keys = {nix_list(keys)};")
            if not sshd:
                warn(f"user '{name}': ssh_authorized_keys is installed into "
                     "openssh.authorizedKeys.keys, but nothing in the document "
                     "starts an sshd (network.ssh.enabled, or sshd in "
                     "software.services.enable), so the keys authorise nothing")
        shell = user.get("shell")
        package = None
        if shell in ("sh", "/bin/sh"):
            # NixOS builds /bin/sh itself (system/build.binsh), so this one
            # path needs no package and no substitution: it is exactly the
            # shell the document named.
            out.append("    shell = \"/bin/sh\";")
        elif shell in SHELL_INTENT:
            # SPEC §9: an intent name "obliges the applier to install the
            # shell". users.users.<n>.shell being a package is what does that —
            # config/users-groups.nix puts every shell package it is given into
            # environment.systemPackages and /etc/shells.
            package = SHELL_INTENT[shell]
        elif shell in SHELL_PATHS:
            # NixOS has no /bin/bash: outside the store only /bin/sh and
            # /usr/bin/env exist, so a literal path would produce an account
            # whose login fails with "Cannot execute". Map to the package that
            # the path names.
            package = SHELL_PATHS[shell]
        elif shell in NOLOGIN_PATHS:
            # Not a shell package: shadow's nologin has no shellPath, so the
            # option takes it as a plain path. pkgs.shadow is in every NixOS
            # system's default profile, so the path exists.
            out.append(f"    shell = {nix_str(NOLOGIN)};")
        elif shell and (shell.startswith("/nix/store/")
                        or shell.startswith("/run/current-system/sw/bin/")):
            out.append(f"    shell = {nix_str(shell)};")
        elif shell and shell.startswith("/"):
            refuse(f"user '{name}': shell {shell!r} is an absolute path "
                   "that does not exist on NixOS outside the store — name the "
                   "shell by intent (" + " | ".join(sorted(SHELL_INTENT))
                   + ") and it is installed for the account")
        elif shell:
            refuse(f"user '{name}': shell {shell!r} is neither a SPEC §9 intent "
                   "name this applier installs (" + " | ".join(sorted(SHELL_INTENT))
                   + ") nor an absolute path")
        if package:
            out.append(f"    shell = pkgs.{package};")
        out.append("  };")
        if user.get("sudo") == "nopasswd":
            nopasswd.append(name)
        elif user.get("sudo") == "default" and not user.get("admin"):
            # `sudo` is the *mode* of an escalation grant, so an account that
            # names it and is not admin still asked to be able to sudo. Only
            # the nopasswd half used to be emitted, which left `sudo: default`
            # on a non-admin account as a silent drop: no wheel membership, no
            # rule, no diagnostic, and an account that cannot escalate at all.
            withpasswd.append(name)
        elif user.get("sudo") is not None and user.get("sudo") not in ("default",
                                                                      "nopasswd"):
            refuse(f"user '{name}': sudo {user['sudo']!r} is not one of SPEC §9's "
                   "values (default | nopasswd)")
        if package in PROGRAMS_MODULE:
            # zsh and fish need their NixOS module, not just the package: the
            # module writes /etc/zshrc and /etc/fish/config.fish, without which
            # the account gets a shell with no system environment at all.
            out.append(f"  programs.{PROGRAMS_MODULE[package]}.enable = true;")
    if declared:
        # SPEC §9: "the applier MUST create groups that do not exist."
        # extraGroups on its own does not — update-users-groups.pl drops a
        # membership naming a group nothing defined, with one line in the
        # journal and a zero exit. Defining the group with an empty submodule
        # merges cleanly with the ones NixOS already declares (video keeps
        # gid 26) and allocates a gid for the ones it does not.
        out.append("  # SPEC §9: groups the document names are created, not "
                   "assumed; an empty")
        out.append("  # definition merges with any NixOS already declares and "
                   "keeps its gid.")
        for group in declared:
            out.append(f"  users.groups.{nix_attr(group)} = {{ }};")
    if nopasswd or withpasswd:
        # Per account, not per wheel. The old line was
        # `security.sudo.wheelNeedsPassword = false`, which hands passwordless
        # root to every member of wheel — including accounts the document
        # created with `admin: true` and no `sudo:` key at all, and including
        # any account added later. security.sudo.extraRules names the user
        # (security/sudo.nix:90-205); our definitions carry the default
        # priority 1000 and so land after the built-in wheel rule at
        # mkOrder 600 (sudo.nix:252-264), which is what makes them win —
        # sudoers takes the last matching rule. One list, not two: a second
        # `security.sudo.extraRules = …` in the same attribute set is a
        # duplicate-attribute error, not a merge.
        out.append("  security.sudo.extraRules = [")
        for name in withpasswd:
            out.append(f"    {{ users = [ {nix_str(name)} ]; commands = "
                       "[ { command = \"ALL\"; options = [ \"SETENV\" ]; } ]; }")
        for name in nopasswd:
            out.append(f"    {{ users = [ {nix_str(name)} ]; commands = "
                       "[ { command = \"ALL\"; options = [ \"NOPASSWD\" ]; } ]; }")
        out.append("  ];")
    return out


def render_configuration(doc: dict) -> str:
    system = doc.get("system", {}) or {}
    boot = doc.get("boot", {}) or {}
    network = doc.get("network", {}) or {}
    software = doc.get("software", {}) or {}
    desktop = doc.get("desktop")
    storage = doc.get("storage", {}) or {}

    out = ["# Generated from a LIS document by lis2nixos (default translator).",
           "# Pair with hardware.nix.",
           "{ config, lib, pkgs, ... }:", "", "{",
           "  imports = [ ./hardware.nix ];", ""]

    out += render_boot(doc)
    out.append("")

    # networking.hostName is types.strMatching
    # "^$|^[[:alnum:]]([[:alnum:]_-]{0,61}[[:alnum:]])?$" on 24.11: a dotted
    # name is not a hostname there, it is an evaluation error — and under
    # --apply one raised after disko has wiped the disks. An FQDN in
    # system.hostname is the ordinary way to write this, so it is split at the
    # first dot rather than refused, and only refused when the two halves would
    # contradict a system.domain the document also states.
    hostname, domain = system.get("hostname"), system.get("domain")
    if hostname and "." in hostname:
        short, _, rest = hostname.partition(".")
        if domain and domain != rest:
            refuse(f"system.hostname {hostname!r} carries the domain {rest!r} "
                   f"while system.domain says {domain!r}; the document has to "
                   "say once what the machine's domain is")
            hostname = short
        else:
            warn(f"system.hostname {hostname!r} is a fully-qualified name, "
                 "which networking.hostName cannot hold (it is types.strMatching "
                 "on a single label) — split into networking.hostName = "
                 f"{short!r} and networking.domain = {rest!r}")
            hostname, domain = short, domain or rest
    if hostname and not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?",
                                     hostname):
        refuse(f"system.hostname {hostname!r} is not a label networking.hostName "
               "accepts (types.strMatching "
               "\"^$|^[[:alnum:]]([[:alnum:]_-]{0,61}[[:alnum:]])?$\"); the "
               "generated configuration would not evaluate")
        hostname = None
    if hostname:
        out.append(f"  networking.hostName = {nix_str(hostname)};")
    if domain:
        out.append(f"  networking.domain = {nix_str(domain)};")
    if system.get("timezone"):
        out.append(f"  time.timeZone = {nix_str(system['timezone'])};")
    hwclock = system.get("hwclock")
    if hwclock in ("utc", "localtime"):
        # Stated in both directions. `utc` used to emit nothing and lean on the
        # NixOS default, which reads the same in the generated file as a
        # document that never mentioned the clock at all — and leaves the answer
        # to whatever a later `imports =` decides.
        out.append("  time.hardwareClockInLocalTime = "
                   f"{str(hwclock == 'localtime').lower()};")
    elif hwclock is not None:
        refuse(f"system.hwclock {hwclock!r} is not a value SPEC §8 defines "
               "(utc | localtime)")
    if system.get("locale"):
        out.append(f"  i18n.defaultLocale = {nix_str(system['locale'])};")
    consume(system.get("locale_overrides", {}) or {})
    for key, value in (system.get("locale_overrides", {}) or {}).items():
        out.append(f"  i18n.extraLocaleSettings.{key} = {nix_str(value)};")
    keymap = system.get("keymap", {}) or {}
    if keymap.get("console"):
        out.append(f"  console.keyMap = {nix_str(keymap['console'])};")
    if keymap.get("font"):
        out.append(f"  console.font = {nix_str(keymap['font'])};")
        if keymap["font"].startswith("ter-"):
            # console.packages defaults to [ ] (config/console.nix:109-111), so
            # setfont only ever sees the fonts kbd itself ships. Terminus is not
            # one of them — and `ter-v16n` is the example SPEC §8 gives — so the
            # name alone produced a boot that logged "cannot open font file"
            # and kept the default font.
            out.append("  console.packages = [ pkgs.terminus_font ];")
    if keymap.get("layout"):
        out.append(f"  services.xserver.xkb.layout = {nix_str(keymap['layout'])};")
    if keymap.get("variant"):
        # No longer nested inside `layout`: a document that names only a variant
        # used to have it dropped and warned about, though nothing was in the
        # way. services.xserver.xkb.layout defaults to "us", so a lone variant
        # is a variant of the distro default layout — which is exactly what a
        # document that leaves the layout unstated is asking for.
        out.append(f"  services.xserver.xkb.variant = {nix_str(keymap['variant'])};")
    if (keymap.get("layout") or keymap.get("variant")) and not keymap.get("console"):
        # An X layout on a machine with no X reached nothing. console.keyMap
        # defaults to `mkIf config.console.useXkbConfig (…)` (config/console.nix),
        # so this is the switch that compiles the xkb layout into a console
        # keymap — the console follows the layout the document did state
        # instead of staying on the us default it did not. Only when the
        # document left console unset: an explicit console.keyMap wins anyway,
        # and saying both would read as a contradiction rather than a fallback.
        out.append("  console.useXkbConfig = true;")
    out += render_time(system.get("time", {}) or {})
    init = system.get("init")
    if init not in (None, "systemd", "auto"):
        refuse(f"system.init {init!r}: NixOS is systemd-only — the whole module "
               "system is built on systemd units, and nixpkgs packages no "
               "alternative PID 1 for it (SPEC §8, subject to §2.3)")
    elif init == "auto":
        # SPEC §19 wants an `auto` recorded as the choice it resolved to, not
        # left looking like a field nobody read.
        out.append("  # system.init: auto — resolved to systemd, the only init "
                   "a NixOS system runs.")
    telemetry = system.get("telemetry")
    if telemetry in ("off", "default"):
        # Not a drop: SPEC §8 asks the applier to disable any telemetry it
        # installs, and a plain NixOS closure installs none — there is no
        # popularity-contest, no phone-home timer, nothing to switch off. Said
        # in the file rather than left to the tracker, so the next reader can
        # tell "nothing to do" from "nobody looked".
        out.append(f"  # system.telemetry: {telemetry} — a plain NixOS system "
                   "collects and reports nothing, so there is no opt-out to emit.")
    elif telemetry is not None:
        refuse(f"system.telemetry {telemetry!r} is not a value SPEC §8 defines "
               "(off | default)")
    kdump = system.get("kdump")
    if kdump:
        # boot.crashDump kexecs a second kernel on panic and leaves /proc/vmcore
        # in rescue (nixos/modules/misc/crashdump.nix:20-56) — the kdump
        # mechanism, under a NixOS name.
        out.append("  boot.crashDump.enable = true;")
        warn("system.kdump: honored through boot.crashDump.enable, which adds a "
             "boot.kernelPatches entry (CRASH_DUMP, DEBUG_INFO, PROC_VMCORE) — "
             "the kernel can no longer come from the binary cache and will be "
             "compiled from source during nixos-install")
    elif kdump is not None:
        out.append("  boot.crashDump.enable = false;")
    out.append("")

    out += render_network(doc)
    out += render_security(doc)
    out += render_keys_pam(doc)

    # hwclock and locale_overrides are emitted where the rest of the i18n and
    # time settings are; duplicating them here defined the same Nix option
    # twice, which is an evaluation error rather than a merge.
    if extra := system.get("extra_locales"):
        # i18n.supportedLocales *replaces* a default that already carries
        # C.UTF-8, en_US.UTF-8, i18n.defaultLocale and every extraLocaleSettings
        # value (config/i18n.nix:59-74). Listing only en_US plus the extras
        # therefore built a glibc locale archive with no entry for the
        # document's own `system.locale`, so LANG named a locale the system did
        # not have. Rebuild the whole set instead of overwriting part of it.
        def supported(name: str) -> str:
            base, dot, codeset = name.partition(".")
            if not dot:
                return name + "/UTF-8"
            if codeset.lower().replace("-", "") == "utf8":
                codeset = "UTF-8"
            return f"{base}.{codeset}/{codeset}"

        wanted = ["C.UTF-8", "en_US.UTF-8"]
        if system.get("locale"):
            wanted.append(system["locale"])
        wanted += [v for k, v in (system.get("locale_overrides", {}) or {}).items()
                   if k != "LANGUAGE"]
        wanted += list(extra)
        locales = list(dict.fromkeys(supported(l) for l in wanted))
        out.append(f"  i18n.supportedLocales = {nix_list(locales)};")

    mirror = doc.get("mirror", {}) or {}
    if mirror_url := mirror.get("url"):
        out.append(f"  nix.settings.substituters = [ {nix_str(mirror_url)} ];")
    if country := mirror.get("country"):
        # A warning here was a silent substitution wearing a label: the
        # document asked for a nearby mirror, the run continued, and
        # cache.nixos.org was used anyway. SPEC §2.3 allows that only for a
        # field marked `preference`, and schema.json's mirror object has no
        # such key (properties: url, country; additionalProperties: false).
        out.append(f"  # mirror.country: {country} — refused: NixOS has no "
                   "per-country mirror to select. Use mirror.url to pin a "
                   "substituter.")
        refuse(f"mirror.country {country!r}: NixOS keeps no mirror list to "
               "pick a nearby entry from — nix.settings.substituters is one "
               "global endpoint whose 24.11 default is the single "
               "[ \"https://cache.nixos.org/\" ] "
               "(nixos/modules/config/nix.nix:443), "
               "a geo-routed CDN with no country dimension to select on. "
               "SPEC §14's \"pick nearby mirrors\" has no selection to make "
               "here, and §2.3 forbids substituting cache.nixos.org for it "
               "silently. Set mirror.url to pin a substituter explicitly, or "
               "drop mirror.country")
    proxy = doc.get("proxy", {}) or {}
    if proxy.get("http"):
        # `default` rather than `httpProxy`: config/networking.nix:88-133 makes
        # httpProxy, httpsProxy, ftpProxy, rsyncProxy and allProxy all fall back
        # to it, so one LIS proxy.http covers the protocols the document did not
        # mention instead of leaving them direct.
        out.append(f"  networking.proxy.default = {nix_str(proxy['http'])};")
    if proxy.get("https"):
        # Explicit wins: networking.proxy.httpsProxy defaults to proxy.default
        # (config/networking.nix:98-101) and is what fills https_proxy when set
        # (config/networking.nix:245-247).
        out.append(f"  networking.proxy.httpsProxy = {nix_str(proxy['https'])};")
    if proxy.get("no_proxy"):
        out.append(f"  networking.proxy.noProxy = {nix_str(','.join(proxy['no_proxy']))};")
    if proxy:
        # The other half of SPEC §14 — see proxy_env(). Said here rather than at
        # apply time so --strict sees it and a translate-only run knows what the
        # installer host still has to be told.
        warn("proxy.*: --apply exports http_proxy/https_proxy/no_proxy for the "
             "install run, which covers the flake and channel fetches. "
             "nix-daemon is a separate systemd service and inherits none of "
             "them, so substituter downloads during nixos-install go direct "
             "unless the installer host's nix-daemon.service carries the same "
             "environment (systemctl edit nix-daemon, "
             "Environment=\"https_proxy=…\")")
    out.append("")

    out += render_users(doc)
    out.append("")

    # software, desktop and drivers share one option namespace: a role, a
    # display manager and a service list can all reach for the same switch, so
    # they are rendered through one collector that refuses a contradiction
    # instead of emitting the same attribute twice.
    opts = NixOptions()
    # The sections above write plain lines rather than going through the
    # collector, and several of the options they own are reachable from
    # software.services[] too — networking.networkmanager.enable,
    # services.timesyncd.enable, services.openssh.enable. Registering what has
    # already been written turns "the same attribute twice", an evaluation
    # error raised inside nixos-install, into a refusal raised here.
    opts.claim(out)
    claim_network_managers(opts)
    render_software(doc, opts)
    render_desktop(doc, opts)
    render_drivers(doc, opts)
    out += opts.lines

    file_lines, file_cmds = render_files(doc)
    out += file_lines

    out += render_script_hooks(doc, file_cmds)

    check_keys(doc)
    if (storage.get("snapshots", {}) or {}).get("enabled"):
        # SPEC §20.9: snapshots need a filesystem that can take them, and the
        # NixOS module can only take them on one — services.snapper.configs.
        # <n>.FSTYPE is types.enum [ "btrfs" ] (services/misc/snapper.nix:57-62).
        # Emitting a snapper config over ext4 or zfs installed a timer that
        # fails on every tick and a system with no snapshots at all.
        if not snapshots_wanted(doc):
            root_fs = next((fs for mp, _, fs, _ in mount_table(doc)[0] if mp == "/"),
                           None)
            refuse("storage.snapshots.enabled on a root filesystem of type "
                   f"{root_fs or 'unknown'}: the NixOS snapper module accepts "
                   "only btrfs (services.snapper.configs.<name>.FSTYPE is "
                   "types.enum [\"btrfs\"]), so no snapshot would ever be taken")
        else:
            out.append("  services.snapper.configs.root = { SUBVOLUME = \"/\"; "
                       "FSTYPE = \"btrfs\"; TIMELINE_CREATE = true; "
                       "TIMELINE_CLEANUP = true; };")
    swap = storage.get("swap", {}) or {}
    if zram := swap.get("zram"):
        out.append("  zramSwap.enable = true;")
        # storage.swap.zram.size is sizeOrPercent (schema.json $defs), and the
        # module has one option per shape: memoryPercent is a share of RAM,
        # memoryMax a hard byte ceiling. Neither was read at all, so a document
        # asking for 50% got whatever the module defaults to.
        size = zram.get("size")
        if isinstance(size, str) and size.endswith("%"):
            out.append(f"  zramSwap.memoryPercent = {int(size[:-1])};")
        elif size is not None:
            if (mib := size_mib(size, "storage.swap.zram.size")) is not None:
                out.append(f"  zramSwap.memoryMax = {mib * 1024 * 1024};")
                out.append("  zramSwap.memoryPercent = 100;")
    if swap.get("file"):
        # swapDevices[].size is MiB (nixos/modules/config/swap.nix). The reader
        # here was `int(size[:-3]) if size.endswith("GiB") else 4`, so every
        # MiB and TiB spelling silently became a 4 GiB file.
        mib = size_mib(swap["file"]["size"], "storage.swap.file.size")
        if mib is not None:
            out.append(f"  swapDevices = [ {{ device = "
                       f"{nix_str(swap['file']['path'])}; size = {mib}; }} ];")

    out += ["", "  # Pin to the release the generator targeted; do not blindly bump.",
            "  system.stateVersion = \"25.05\";", "}"]
    return "\n".join(out) + "\n"


# Forces system.build.toplevel, the same attribute nixos-install realises. The
# weaker `attrNames sys.config.system.build` form does not force
# environment.systemPackages, so a package name nixpkgs has no attribute for
# survives it and fails later.
PREFLIGHT_EXPR = """
let sys = import <nixpkgs/nixos> {
      configuration = { imports = [ %s ]; };
    };
in builtins.seq (builtins.toString sys.config.system.build.toplevel) "ok"
"""


def preflight_evaluation(config_file: pathlib.Path) -> int:
    """Evaluate the generated configuration before disko is allowed to run.

    nixos-install evaluates this file anyway — but it does so *after* disko has
    destroyed the partition table, which makes every evaluation error a
    wipe-then-fail. An option name nixpkgs 24.11 spells differently, a
    `software.packages[]` entry with no attribute, an unfree package, a module
    assertion: all of them are found here, with the disks still intact.

    A skipped check is reported rather than treated as a pass: an installer
    host with no <nixpkgs> can still install from a flake, and silently
    claiming the configuration was verified would be the drift SPEC §2.3
    forbids.
    """
    import subprocess

    # A Nix *path* literal: a relative one is parsed as a variable selection.
    config_file = config_file.resolve()
    print(f"pre-flight: evaluating {config_file} before touching the disks")
    try:
        res = subprocess.run(
            ["nix-instantiate", "--eval", "--strict", "-E",
             PREFLIGHT_EXPR % str(config_file)],
            capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as err:
        print(f"warning: pre-flight evaluation could not run ({err}) — the "
              "configuration is unverified and any error in it will be raised "
              "by nixos-install, after disko has wiped the disks",
              file=sys.stderr)
        return 0
    if res.returncode == 0:
        print("pre-flight: the configuration evaluates")
        return 0
    if "file 'nixpkgs' was not found" in res.stderr or "attribute 'nixos'" in res.stderr:
        print("warning: pre-flight evaluation found no <nixpkgs> to evaluate "
              "against — the configuration is unverified", file=sys.stderr)
        return 0
    sys.stderr.write(res.stderr)
    print("refused: the generated configuration does not evaluate against this "
          "host's nixpkgs (see above). Refusing to run disko: nixos-install "
          "would raise the same error with the disks already wiped.",
          file=sys.stderr)
    return 1


# SPEC §16's five keys. The section is apply-run behavior, not system state, so
# nothing here reaches configuration.nix: on_finish and on_error steer main()
# after nixos-install, unattended gates the destructive run, and the two
# frontend keys are refused because this applier has no frontend.
INSTALLER_FIELDS = frozenset({"on_finish", "on_error", "unattended",
                              "interactive", "answers"})


def check_installer(doc: dict) -> tuple[str, str]:
    """Answer every SPEC §16 key and return the (on_finish, on_error) to obey.

    Replaces `check_section_fields(doc, "installer", set())`, which reported
    the whole section as unapplied and then let check_unread report each leaf
    a second time — two warnings for one drop, and no behavior either way.
    """
    installer = doc.get("installer") or {}
    if not isinstance(installer, dict):
        # schema.json types the section as an object. Falling through with a
        # list or a string reached .get() on it and ended the run in a
        # traceback, which says nothing about what the document asked for.
        refuse(f"installer must be an object, not {_shape(installer)} "
               "— none of the SPEC §16 keys can be read from it, so neither "
               "on_finish nor the consent gate has a value to obey")
        return "stay", "fail"
    check_section_fields(doc, "installer", INSTALLER_FIELDS)

    on_finish = installer.get("on_finish") or "stay"
    if on_finish not in ("reboot", "poweroff", "stay"):
        refuse(f"installer.on_finish {on_finish!r} is not one of reboot, "
               "poweroff or stay (SPEC §16)")
        on_finish = "stay"

    on_error = installer.get("on_error") or "fail"
    if on_error == "prompt":
        # The applier is driven from a serial console by a harness, an
        # ssh session or a PXE boot; none of them guarantees an operator is
        # watching. Prompting there stops a half-installed machine forever
        # instead of failing it, which is worse than the default.
        refuse("installer.on_error 'prompt': this applier runs unattended "
               "with no interactive frontend and no guaranteed terminal — "
               "a prompt on a serial or netboot console would hang the run "
               "at a half-installed target rather than surface the failure. "
               "Use the SPEC §16 default 'fail'")
        on_error = "fail"
    elif on_error != "fail":
        refuse(f"installer.on_error {on_error!r} is not one of fail or "
               "prompt (SPEC §16)")
        on_error = "fail"

    if interactive := installer.get("interactive"):
        # A bare string is iterable, so listing `name for name in interactive`
        # spelled a section name out one character at a time.
        sections = (", ".join(sorted({str(name) for name in interactive}))
                    if isinstance(interactive, (list, tuple, set))
                    else repr(interactive))
        refuse(f"installer.interactive [{sections}]: this applier is a "
               "non-interactive translator — it has no frontend and asks no "
               "questions, so it cannot re-ask for a section. SPEC §16 lets a "
               "frontend re-ask; there is none here, and the listed sections "
               "would be applied from the document without confirmation")

    if answers := installer.get("answers"):
        # A non-object `answers` is a schema error; naming its type is still a
        # better answer than a traceback, and the refusal below is the same
        # either way because no id can match a question this applier never asks.
        ids = (", ".join(sorted(dict.keys(answers)))
               if isinstance(answers, dict) else _shape(answers))
        refuse(f"installer.answers ({ids}): this applier defines no questions, "
               "so no answer id can match one. SPEC §16 keys answers by "
               "applier-defined question id; every id here is unanswerable by "
               "construction, and leaving them unused would hide a document "
               "that expects a question this applier never asks")
        # The refusal is the verdict for each id; without this check_unread
        # reports the same keys again as never looked at.
        consume(answers)

    # Read here so the field is decided about even on a translate-only run,
    # where there is no destructive step for check_consent() to gate.
    if installer.get("unattended") and not doc.get("storage", {}).get("wipe"):
        warn("installer.unattended: true with no storage.wipe: true — "
             "delivery.md §5 wants both before a prompt-free destructive run, "
             "so --apply will still stop at the confirmation step")

    return on_finish, on_error


def channel_consent(doc_path: pathlib.Path) -> str | None:
    """The delivery half of the two-key rule, or None (delivery.md §5).

    Either the empty `unattended` marker at the root of the LIS seed volume
    (delivery.md §1) or `lis.unattended=1` on the kernel command line, which is
    the network-delivery form (delivery.md §7).
    """
    # Only where delivery.md puts it: the volume root, which is either the
    # document's own directory or its parent when the document sits in
    # recipes/. Walking further up would let a stray /unattended on the live
    # ISO's root filesystem grant consent for every document on the machine.
    here = doc_path.resolve().parent
    for root in (here, here.parent, pathlib.Path("/run/lis/seed")):
        try:
            # Case-insensitive: the seed is a FAT volume, where the name a
            # mount presents depends on the driver's short/long-name handling.
            for entry in root.iterdir():
                if entry.name.lower() == "unattended" and entry.is_file():
                    return f"the '{entry}' consent marker on the delivery channel"
        except OSError:
            continue
    try:
        cmdline = pathlib.Path("/proc/cmdline").read_text().split()
    except OSError:
        cmdline = []
    if "lis.unattended=1" in cmdline:
        return "lis.unattended=1 on the kernel command line"
    return None


def check_consent(doc: dict, doc_path: pathlib.Path, confirmed: bool) -> int:
    """Refuse a prompt-free destructive run that nobody consented to.

    delivery.md §5: erasing a machine needs both a document key
    (`installer.unattended: true` with `storage.wipe: true`) and a delivery
    key. Missing either, the run must stop at a confirmation step — which for
    a command-line applier is --confirm-destroy, typed by the operator who is
    standing in for the missing key.
    """
    installer = doc.get("installer") or {}
    storage = doc.get("storage") or {}
    missing = []
    if not installer.get("unattended"):
        missing.append("installer.unattended: true")
    if not storage.get("wipe"):
        missing.append("storage.wipe: true")
    channel = channel_consent(doc_path)
    if channel is None:
        missing.append("an 'unattended' marker on the seed volume or "
                       "lis.unattended=1 on the kernel command line")
    if not missing:
        print(f"consent: both keys present — {channel}, and "
              "installer.unattended with storage.wipe in the document")
        return 0
    if confirmed:
        warn("--confirm-destroy stood in for the two-key consent rule: "
             + "; ".join(missing) + " (delivery.md §5). The disks named in "
             "storage are about to be erased on the operator's word alone")
        return 0
    refuse("refusing a destructive run without consent (delivery.md §5): "
           + "; ".join(missing) + " — missing. Supply the missing key, or "
           "pass --confirm-destroy to stand at the confirmation step in "
           "person. Nothing has been written to any disk")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Translate a LIS document into a plain NixOS configuration.")
    add_common_args(ap)
    ap.add_argument("--apply", "-a", action="store_true",
                    help="partition with disko and run nixos-install on the live system")
    ap.add_argument("--confirm-destroy", action="store_true",
                    help="stand in for the two-key consent rule (delivery.md "
                         "§5) when the document or the delivery channel does "
                         "not carry its half: the operator confirms the wipe")
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
    # This applier translates the core document only: there is no `x-nixos`
    # key it acts on, so the whole namespace is reported once and ignored.
    check_extensions(doc, "x-nixos", set())
    # Every key the schema allows under boot is answered in render_boot, either
    # with an option or with a refusal carrying its reason. Leaving them out of
    # this set produced a warning saying applied fields were not applied, which
    # is how a warning channel stops being read.
    check_boot_extras(doc, {"kernel", "variant", "params", "modules", "blacklist",
                            "loader", "timeout", "os_prober", "password_hash",
                            "console", "secure_boot", "uki", "initramfs"})
    check_mirror(doc, {"url", "country"})
    check_section_fields(doc, "desktop", {"audio", "autologin", "bluetooth",
                                          "display_manager", "printing"})
    on_finish, on_error = check_installer(doc)
    check_keymap(doc, {"console", "font", "layout", "variant"})

    args.out.mkdir(parents=True, exist_ok=True)
    disko_file = args.out / "disko.nix"
    hw_file = args.out / "hardware.nix"
    config_file = args.out / "configuration.nix"

    # disko.nix is checked on its own rather than together with its two
    # siblings. The trio does describe one install, but only disko.nix creates
    # the container: hardware.nix and configuration.nix describe the system
    # once it exists, so a joint check would let a plaintext disko.nix pass on
    # the strength of a `boot.initrd.luks.devices` entry that unlocks nothing.
    # The marker is the mapper name as emit_content() writes it, so several
    # containers are checked one by one instead of as a group.
    if args.apply:
        # schema.md §20.8 puts the resolution of an `existing.match` at apply
        # time, and everything the layout pins — index, first and last sector,
        # partition GUID, partition name — is read from the live table here,
        # before a single line of disko.nix is written.
        resolve_adoptions(doc)
    disko_nix = render_disko(doc)
    check_encryption_emitted(doc, disko_nix,
                             marker=lambda c: f"name = {nix_str(c['id'])}",
                             label="generated disko.nix")
    disko_file.write_text(disko_nix)
    hw_file.write_text(render_hardware(doc))
    config_file.write_text(render_configuration(doc))
    report(disko_file, hw_file, config_file)

    # Fail closed *before* touching the machine, not after.
    check_raid_consumers(doc)
    check_boot_menu(doc)
    # boot_menu is already answered, by check_boot_menu, with the reason that
    # is true of NixOS specifically; the shared helper would state the outcome
    # a second time in wording that fits nine distributions and cites none.
    check_snapshots(doc, tools={"snapper"}, boot_menu=True)
    # honors_chroot: the shared helper carries one boundary default per applier
    # and this applier straddles the boundary — pre_install runs on the live
    # ISO, post_install runs inside the target during activation — so the flag
    # is answered per stage by check_stage_chroot() instead.
    # interpreter, source and on_failure are answered per entry by hook_call()
    # and host_stage_hooks(); honors_chroot for the same reason one level down
    # — this applier straddles the boundary, so check_stage_chroot() answers
    # the flag per stage.
    check_script_fields(doc, honors_chroot=True, honors_interpreter=True,
                        honors_source=True, honors_on_failure=True)
    # registration is refused whole (SPEC §15: NixOS has no subscription
    # service to attach to), so its leaves have been decided about — reporting
    # them a second time as "never read" says the applier overlooked something
    # it in fact turned down, and trains the reader to skim the channel the
    # real drops arrive on.
    # installer.* is the same case: check_installer() answered every key, so
    # the leaves are decided rather than overlooked. installer.unattended is
    # read only under --apply, where it gates the wipe.
    decided = {f"{section}.{leaf}" for section in ("registration", "installer")
               for leaf in _leaf_paths(raw.get(section) or {})}
    check_unread(doc, ignore=APPLY_TIME_PATHS | decided
                 | {f"scripts.{stage}[].content" for stage in HOST_STAGES})

    if status := enforce(args.strict):
        return status

    # Consent is checked outside enforce() on purpose: --lenient downgrades
    # refusals to warnings, and a wipe nobody consented to must not be one of
    # the things it can wave through (delivery.md §5).
    if args.apply and check_consent(doc, args.file, args.confirm_destroy):
        return 1

    if args.apply:
        import subprocess

        # SPEC §14: proxy.* is an install-time obligation as well as a
        # persisted one. Set before the first subprocess, which inherits it.
        apply_proxy_env(doc)

        def run_stage(stage: str) -> None:
            """Run one live-installer-environment stage on this host.

            The translation warns that these run here rather than reaching the
            generated configuration; running them is what keeps that warning a
            statement of fact instead of a promise. SPEC §13's default
            `on_failure: fail` means abort the installation, which is what
            run_host_stage's non-zero return says.
            """
            if status := run_host_stage(doc, stage):
                raise SystemExit(status)

        if status := preflight_evaluation(config_file):
            run_stage("on_error")
            return status

        for stage in ("pre_install", "pre"):
            run_stage(stage)
        print(f"partitioning disks via disko: {disko_file}")
        # `--mode disko` is the legacy alias of destroy,format,mount, and the
        # legacy branch of the disko CLI runs the script directly, which is the
        # one path that never sees --yes-wipe-all-disks (disko:180-187). Naming
        # the mode in full puts the run back on the branch that has a safety
        # check, and passes it only because check_consent() has already
        # established the two keys. The nixpkgs fallback is disko 1.9.0, whose
        # CLI accepts "format, mount or disko" and nothing else, so the alias
        # stays there — it is reached only after the same consent check.
        mode, flags = "destroy,format,mount", " --yes-wipe-all-disks"
        if PRESERVING:
            # Something on a declared disk is being kept, so the destroy stage —
            # which clears the partition table of every declared disk — must not
            # run. `format` creates partitions and filesystems only where they
            # are not there yet, and `mount` mounts what the layout describes
            # (disko:29-34).
            mode, flags = "format,mount", ""
            print("a partition is adopted from the live disk: running disko in "
                  f"{mode}, without its destroy stage")
        disko = ("nix --extra-experimental-features 'nix-command flakes' "
                 "run github:nix-community/disko/latest -- "
                 f"--mode {mode}{flags} {disko_file}")
        res = subprocess.run(disko, shell=True)
        if res.returncode != 0:
            if PRESERVING:
                # The only fallback available is disko 1.9.0, whose CLI takes
                # one mode word and whose combined word is `disko` — destroy
                # included. Retrying a preserving install with it would clear
                # the table the run exists to keep, so the failure is reported
                # instead (SPEC §2.3).
                print("the non-destroying disko run failed; not retrying with a "
                      "mode whose destroy stage would clear the adopted "
                      "partitions", file=sys.stderr)
                run_stage("on_error")
                return res.returncode
            res = subprocess.run(
                f"nix-shell -p disko --run 'disko --mode disko {disko_file}'", shell=True)
            if res.returncode != 0:
                run_stage("on_error")
                return res.returncode

        # SPEC §13.2: the live installer environment, right after the target is
        # formatted and mounted — which on this applier is disko's default root
        # mount point, /mnt, not the /target the spec's example names.
        run_stage("post_storage")

        print("installing NixOS via nixos-install...")
        subprocess.run(f"mkdir -p /mnt/etc/nixos && cp -f {config_file} {hw_file} "
                       f"{disko_file} /mnt/etc/nixos/", shell=True, check=False)
        res = subprocess.run("nixos-install --no-root-passwd --root /mnt", shell=True)
        if res.returncode != 0:
            res = subprocess.run(
                "nix-shell -p nixos-install --run "
                "'nixos-install --no-root-passwd --root /mnt'", shell=True)
        if res.returncode == 0:
            write_birth_certificate(doc)
            write_wireless_secrets(doc)
            # SPEC §13.3's host-context half: the entries whose `chroot` is
            # false. Their chrooted siblings already ran, inside the target,
            # from the activation script nixos-install invoked.
            run_stage("post_install")
            run_stage("post")
            run_stage("on_success")
            unmount_target(doc)
            run_stage("pre_reboot")
            finish_run(on_finish)
        else:
            # SPEC §16 on_error: 'fail' is the only value check_installer lets
            # through, and this is it — the on_error hooks run and the non-zero
            # status is returned rather than swallowed.
            print(f"installation failed; installer.on_error is {on_error!r}",
                  file=sys.stderr)
            run_stage("on_error")
        return res.returncode
    return 0


def unmount_target(doc: dict) -> None:
    """Unmount /mnt, because SPEC §13.4 puts pre_reboot *after* the unmount.

    Only when the document declares the phase: with no pre_reboot hook there is
    nothing whose contract depends on the target being gone, and leaving the
    mount up is what `installer.on_finish: stay` means for an operator who is
    about to look at the result. A busy mount is reported rather than swallowed
    — the phase then runs in a state the spec does not describe.
    """
    import subprocess

    if not (doc.get("scripts", {}) or {}).get("pre_reboot"):
        return
    print("unmounting the target before scripts.pre_reboot (SPEC §13.4)")
    if subprocess.run("umount -R /mnt", shell=True).returncode:
        print("could not unmount /mnt; scripts.pre_reboot runs with the target "
              "still mounted, which is not the state SPEC §13.4 names",
              file=sys.stderr)


def finish_run(on_finish: str) -> None:
    """Carry out SPEC §16 installer.on_finish once the target is installed.

    `stay` is the section's default and was already the behavior: main()
    returns and the live environment is left as it is. The other two were
    dropped silently — a document asking for `reboot` got a shell prompt.
    """
    import subprocess

    if on_finish == "stay":
        print("installer.on_finish 'stay': leaving the live environment up")
        return
    # Target is unmounted first: disko mounts it at /mnt and systemd's own
    # shutdown does not always flush a filesystem it did not mount, so a
    # reboot straight after nixos-install can lose the last writes.
    subprocess.run("umount -R /mnt", shell=True, check=False)
    subprocess.run("swapoff -a", shell=True, check=False)
    command = "systemctl reboot" if on_finish == "reboot" else "systemctl poweroff"
    print(f"installer.on_finish {on_finish!r}: {command}")
    if subprocess.run(command, shell=True).returncode != 0:
        # A live ISO shell is not always talking to a running systemd (a
        # chroot, a rescue shell), where systemctl exits non-zero and the
        # machine simply stays up against the document's instruction.
        subprocess.run("reboot -f" if on_finish == "reboot" else "poweroff -f",
                       shell=True, check=False)


def write_birth_certificate(doc: dict) -> None:
    """Record the applied document on the installed system (delivery.md §8).

    Written through redact_secrets, the same filter the store-bound copy in
    render_script_hooks goes through. delivery.md:144 is explicit — an applier
    "MUST NOT copy resolved secrets into target log files or birth
    certificates" — and network.wifi[].psk_hash is a credential in its own
    right: the PMK *is* the network, so a certificate carrying it hands out
    wireless access to anyone who can read the file. The redacted field names
    write_wireless_secrets()'s 0600 path instead of the value, so the record
    still says what was applied and where the material went.
    """
    target = pathlib.Path("/mnt/var/lib/lis")
    try:
        target.mkdir(parents=True, exist_ok=True)
        cert = target / "system.lis.json"
        cert.write_text(json.dumps(redact_secrets(doc), separators=(",", ":")) + "\n")
        cert.chmod(0o600)
        print(f"wrote birth certificate {cert}")
    except OSError as err:
        print(f"warning: could not write birth certificate: {err}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
