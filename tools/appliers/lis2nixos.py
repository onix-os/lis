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
import json
import pathlib
import re
import sys

from lis_common import (track, check_unread, luks_key_path, check_raid_consumers, registration_commands, enrollment_commands, resolve_disk_paths, check_snapshots, match_selectors, consume, password_field, secret_ref, APPLY_TIME_PATHS,ALL_SECTIONS, add_common_args, check_firmware,
                        check_encryption_emitted, resolve_mountpoints,
                        check_unhandled, check_section_fields, check_mirror, check_kernel_variant, check_user_sudo,
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
               label=None, where="filesystem", extra_subvolumes=()):
    """Emit the `content = { … }` block for a plain filesystem or swap area."""
    if fs in (None, "none"):
        return
    extra = label_args(fs, label, where) if label is not None else []
    if fs == "swap":
        lines += [f"{pad}content = {{", f"{pad}  type = \"swap\";"]
        if extra:
            lines.append(f"{pad}  extraArgs = {nix_list(extra)};")
        lines.append(f"{pad}}};")
        return
    if fs == "btrfs" and (subvolumes or extra_subvolumes):
        lines += [f"{pad}content = {{",
                  f"{pad}  type = \"btrfs\";",
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
              f"{pad}  type = \"filesystem\";",
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
            if handle and (handle in self.spare_handles
                           or self.owner_of(handle) is not None):
                continue    # an aggregate or a pool owns it, directly or through luks
            if part.get("existing"):
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
            elif fs in (None, "none") and spec.get("role") not in ("raw", None):
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
                return partition_device(*names[id(part)])
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

    def emit_content(self, lines: list, pad: str, handle: str, spec: dict) -> None:
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
            if keyfile := (crypt.get("key", {}) or {}).get("keyfile"):
                lines.append(f"{pad}  settings.keyFile = {nix_str(keyfile)};")
            elif key_path := luks_key_path(self.doc, crypt["id"]):
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
        self.emit_layer(lines, pad, inner_handle, spec)
        if crypt:
            lines.append(f"{pad[:-2]}}};")

    def emit_layer(self, lines: list, pad: str, handle: str, spec: dict) -> None:
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
            fs_content(lines, pad, fs, mp, spec.get("mount_options", []),
                       spec.get("subvolumes", []), label=spec.get("label"),
                       where=spec_where(spec, handle),
                       extra_subvolumes=self.extra_subvolumes(spec, fs, mp))

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


def render_mdadm(topology: Topology, out: list) -> None:
    """disko.devices.mdadm — one entry per LIS raid array."""
    if not topology.raid:
        return
    out.append("    mdadm = {")
    for array in topology.raid:
        name = array["name"]
        missing = [d for d in array.get("devices", []) if d not in topology.specs
                   and d not in {c["id"] for c in topology.encryption}]
        for dev in missing:
            warn(f"raid '{name}': device handle {dev!r} does not resolve to a partition")
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


def render_disko(doc: dict) -> str:
    storage = doc.get("storage")
    if not storage:
        raise SystemExit("error: document has no storage section — nothing to generate")
    partitions = storage.get("partitions", [])
    lvm = storage.get("lvm", []) or []
    topology = topology_for(doc)
    names = partition_names(storage)

    if not storage.get("wipe", False):
        # --apply runs `disko --mode destroy,format,mount`; disko recreates the
        # table unconditionally, so honouring wipe: false is not possible here.
        refuse("storage.wipe: false — disko destroys and recreates the "
               "declared disks; it cannot preserve an existing layout")

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
        if bios_grub:
            out += [f"            {nix_str(bios_grub)} = {{",
                    "              size = \"1M\";",
                    "              type = \"EF02\";",
                    "              priority = 1;",
                    "            };"]
        for part in [p for p in partitions if p.get("disk") == disk["id"]]:
            where = f"partition {part.get('id') or part.get('role') or '?'!r}"
            if part.get("existing"):
                # Was a warn() and a `continue`, which is the silent drop §2.3
                # forbids twice over: the adopted partition disappeared from
                # disko.nix while hardware.nix still mounted a device nobody
                # created, and because the per-disk index was counted after the
                # skip the *remaining* partitions were renamed too. Nothing in
                # this translator resizes or adopts, so the honest answer is to
                # say no. disko can express it — explicit `start`/`end`/`uuid`
                # on a gpt partition (lib/types/gpt.nix:126,139,185,190),
                # `--mode format,mount` to leave the table alone, and
                # preCreateHook for the resize — so this is a gap to close, not
                # a hard limit.
                refuse(f"{where}: storage.partitions[].existing (adoption) is not "
                       "implemented by this translator — it runs disko with "
                       "destroy,format,mount, which recreates the whole table, "
                       "so an adopted partition would be destroyed rather than "
                       "kept (schema.md §6.2)")
                # The refusal is the answer for every leaf under it; without
                # this the birth certificate also reports each one as an
                # unnoticed field, which reads as a second, softer verdict.
                consume(part["existing"])
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
                mp = topology.mountpoint_of(part)
                out += ["              type = \"EF00\";",
                        "              content = {",
                        "                type = \"filesystem\";",
                        "                format = \"vfat\";"]
                if label := part.get("label"):
                    if args := label_args("vfat", label, where):
                        out.append(f"                extraArgs = {nix_list(args)};")
                if mp:
                    # An ESP nothing mounts is a refusal above, not a partial
                    # attribute set: disko's filesystem type wants a real path.
                    out.append(f"                mountpoint = {nix_str(mp)};")
                opts = list(part.get("mount_options") or []) or ["umask=0077"]
                out += [f"                mountOptions = {nix_list(opts)};",
                        "              };"]
            else:
                topology.emit_content(out, "              ",
                                      part.get("id", ""), part)
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
    out += ["  };", "}"]
    return "\n".join(out) + "\n"


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
    for name, backing, keyfile in luks_initrd_devices(doc):
        entry = (f"  boot.initrd.luks.devices.{nix_str(name)} = "
                 f"{{ device = {nix_str(backing)}; allowDiscards = true;")
        if keyfile:
            entry += f" keyFile = {nix_str(keyfile)};"
        out.append(entry + " };")
    if any(fstype == "zfs" for _, _, fstype, _ in mounts):
        out.append("  boot.supportedFilesystems = [ \"zfs\" ];")
        out.append(f"  networking.hostId = {nix_str(host_id(doc))};")
    if storage.get("raid"):
        out.append("  boot.swraid.enable = true;")

    out.append("}")
    return "\n".join(out) + "\n"


def luks_initrd_devices(doc: dict) -> list[tuple[str, str, str | None]]:
    """Each LUKS container, paired with the device disko will have put it on.

    Stage 1 opens only the containers it was told about: luksroot.nix builds its
    unlock units from `boot.initrd.luks.devices`, and it is also what pulls
    dm_crypt and the cipher modules into the initrd. Without an entry the root
    filesystem — or, with LVM inside the container, the whole volume group —
    never appears and the boot stalls in stage 1. disko would emit these from
    its own NixOS module, but this translator generates plain NixOS options
    only, so it states them itself.

    The passphrase is not named here: `unlock: passphrase` means the operator
    types it at boot, and the seed that holds it is not attached by then.
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
        devices.append((crypt["id"], device,
                        (crypt.get("key", {}) or {}).get("keyfile")))
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
        options = list(spec.get("mount_options", []))
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
        if part.get("existing"):
            continue   # refused in render_disko; nothing here would exist to mount
        disk_id, name = names[id(part)]
        handle = part.get("id") or name
        if handle in topology.spare_handles:
            continue   # a hot spare carries no filesystem of its own
        device = partition_device(disk_id, name)
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
  _f=$(mktemp /tmp/lis-hook-XXXXXX) || return 1
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
  if [ "$_rc" -ne 0 ]; then
    echo "lis: $_lbl exited $_rc" >&2
    if [ "$_pol" != continue ]; then return "$_rc"; fi
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
# system-wide rc file the login shell sources.
PROGRAMS_MODULE = {"zsh": "zsh", "fish": "fish"}

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
        return None if content is None else content.encode()
    if content is not None:
        refuse(f"{label}: content and source both name the script body — "
               "SPEC §13 gives an entry one body, and there is no rule that "
               "says which of the two would run")
        return None
    path = secret_ref(ref)
    if path is None:
        refuse(f"{label}.source {ref!r}: this applier resolves seed: and file: "
               "references against the running installer's filesystem; https: "
               "is not fetched and env:/key: name secret material rather than "
               "a script (SPEC §2.4)")
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
}

# Which side of the chroot boundary each stage genuinely runs on here. SPEC §13
# defaults `chroot` to true for post_install and false for the host hooks; a
# document that asks for the other side is asking for something this applier
# cannot do, so it refuses rather than running the body on the wrong machine.
STAGE_IN_TARGET = {
    "pre": False, "pre_install": False, "post_storage": False,
    "post": True, "post_install": True,
    "pre_reboot": False, "on_success": False, "on_error": False,
    "firstboot": True,
}


def check_stage_chroot(doc: dict) -> None:
    """Refuse a `chroot` flag that names the side of the boundary we are not on.

    `check_script_fields` is called with honors_chroot=True precisely so this
    can answer per stage: the shared helper has one default for the whole
    applier, and this applier straddles the boundary — `pre_install` runs on the
    live ISO, `post_install` runs inside the target during activation.
    """
    def inspect(stage: str, items, label: str) -> None:
        in_target = STAGE_IN_TARGET[stage]
        for item in items or []:
            flag = item.get("chroot")
            if flag is None or bool(flag) is in_target:
                continue
            if in_target:
                refuse(f"{label}.chroot false: this applier runs {stage} from "
                       "system.activationScripts, which nixos-install executes "
                       "inside the target — there is no host-side stage for it")
            else:
                refuse(f"{label}.chroot true: this applier runs {stage} "
                       f"{HOST_STAGE_CONTRACT.get(stage, 'on the installer host')}"
                       ", where no target root is available to enter")

    scripts = doc.get("scripts", {}) or {}
    for stage in STAGE_IN_TARGET:
        inspect(stage, scripts.get(stage), f"scripts.{stage}[]")
    for user in doc.get("users", []) or []:
        user_scripts = user.get("scripts", {}) or {}
        for stage in ("post", "post_install", "firstboot"):
            inspect(stage, user_scripts.get(stage),
                    f"users['{user.get('name')}'].scripts.{stage}[]")


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
    out: list[tuple[str, str, str, bytes]] = []
    for index, item in enumerate(scripts.get(stage, []) or []):
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

    def collect(items, stage: str, label: str, user: str = "") -> list[str]:
        lines = []
        for index, item in enumerate(items or []):
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
        hooks += collect(scripts.get(stage), stage, f"scripts.{stage}")
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
    for stage in ("pre_install", "pre") + HOST_STAGES:
        host_stage_hooks(doc, stage)

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
        # whole set used to sit in the same activation snippet as files[] and
        # the birth certificate, which NixOS re-runs on every boot and every
        # `nixos-rebuild switch`: a hook appending a line to a config file
        # appended it forever. Its own snippet with its own marker restores
        # the "once" half; the marker is written whatever the hooks returned,
        # because a failed fail-policy hook has already made this activation
        # exit non-zero and re-running a half-completed hook on the next boot
        # is not a recovery.
        #
        # `switch-to-configuration boot` exits before the activation script
        # (switch-to-configuration.pl:125), so nixos-install does not run this
        # — the first activation is the target's own first boot, ahead of
        # systemd and therefore ahead of lis-firstboot.
        warn("scripts.post_install / scripts.post: NixOS has no install-stage "
             "hook — nixos-install runs `switch-to-configuration boot`, which "
             "exits before activation — so these run from an activation script "
             "guarded by /var/lib/lis/.post-install-done, i.e. once, on the "
             "target's first boot and before the first-boot unit")
        body = ("if [ ! -e /var/lib/lis/.post-install-done ]; then\n"
                + "\n".join(hooks)
                + "\ninstall -d -m755 /var/lib/lis"
                + "\ntouch /var/lib/lis/.post-install-done\nfi")
        out += ["  system.activationScripts.lis-post-install = {",
                "    deps = [ \"lis-hooks\" ];",
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
    if not sep or not mask.isdigit() or not host:
        return None
    family = 6 if ":" in host else 4
    prefix = int(mask)
    if prefix > (128 if family == 6 else 32):
        return None
    return host, prefix, family


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
        out.append(f"  networking.firewall.enable = {str(enabled).lower()};")
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
        if dash:
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

    dhcp4, dhcp6 = iface.get("dhcp4"), iface.get("dhcp6")
    addresses: list[str] = []
    for addr in iface.get("addresses", []) or []:
        if cidr(addr) is None:
            refuse(f"network.interfaces[{index}].addresses {addr!r} is not "
                   "<address>/<prefix>; systemd.network.networks.<n>.address "
                   "takes CIDR notation")
            continue
        addresses.append(addr)
    gateway = iface.get("gateway")
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
        dhcp4, dhcp6 = iface.get("dhcp4"), iface.get("dhcp6")
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
        refuse("network.wifi with network.manager 'iwd': the NixOS iwd module "
               "(services/networking/iwd.nix:36) exposes only `settings` for "
               "main.conf and has no declarative network list — set "
               "network.manager to networkmanager or systemd-networkd")
        # The section is refused as a whole; leaving its leaves unread would add
        # a second, weaker "never read" warning for the same decision.
        for net in wifi:
            consume(net)
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
    # networkmanager, and `auto` — which the manager block resolved to NM.
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


def render_network(doc: dict) -> list[str]:
    network = doc.get("network", {}) or {}
    out: list[str] = []

    interfaces = network.get("interfaces", []) or []
    wifi = network.get("wifi", []) or []
    manager = resolve_manager(network, interfaces)
    if manager == "networkmanager":
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
    for entry in network.get("hosts", []) or []:
        names_for = host_names.setdefault(entry["ip"], [])
        names_for += [n for n in entry["names"] if n not in names_for]
    for ip, host_list in host_names.items():
        out.append(f"  networking.hosts.{nix_str(ip)} = {nix_list(host_list)};")

    if firewall := network.get("firewall"):
        render_firewall(firewall, out)

    ssh = network.get("ssh", {}) or {}
    if ssh.get("enabled"):
        out.append("  services.openssh.enable = true;")
    elif "enabled" in ssh:
        # Emitted rather than left to the default: `false` is intent, and a role
        # or a later module turning sshd on must lose to it, not silently win.
        out.append("  services.openssh.enable = false;")
        if "password_auth" in ssh or ssh.get("permit_root"):
            warn("network.ssh.password_auth / permit_root have no effect: the same "
                 "document sets network.ssh.enabled false")
    if "password_auth" in ssh:
        out.append("  services.openssh.settings.PasswordAuthentication = "
                   f"{str(ssh['password_auth']).lower()};")
    if ssh.get("permit_root"):
        out.append("  services.openssh.settings.PermitRootLogin = "
                   f"{nix_str(ssh['permit_root'])};")
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
    "appimage": "an AppImage is fetched from the network at install time, "
                "which contradicts the seed/offline model every LIS "
                "applier installs under",
}


def nix_pkg_path(name: str) -> str:
    """A package name as a Nix attribute path, quoting what is not an identifier.

    `1password` and `python3.11` are both legal package names and only one of
    them is a legal bare Nix attribute path: the other is a syntax error in the
    generated file, raised by nixos-install after disko has wiped the disks.
    Dots stay attribute separators (`python3.11` selects `11` out of `python3`);
    everything else is quoted.
    """
    parts = []
    for part in name.split("."):
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
    skipped: list[str] = []
    for source in order:
        if source not in APP_SOURCES:
            refuse(f"software.apps[] {name!r}: preference {source!r} is not an "
                   f"application source LIS defines ({', '.join(APP_SOURCES)})")
            return None, None
        if source not in available:
            continue
        if source in APP_SOURCE_REFUSALS:
            skipped.append(source)
            continue
        for dropped in skipped:
            warn(f"software.apps[] {name!r}: {dropped!r} is preferred over "
                 f"{source!r} but {APP_SOURCE_REFUSALS[dropped]} — installing "
                 f"the {source} source instead")
        # An alternative the arbitration never reached is still a field the
        # document wrote and this applier did not act on. Naming it is the
        # difference between a documented choice and the silent drop the
        # spec forbids.
        for unused in sorted(set(available) - {source} - set(skipped)):
            if reason := APP_SOURCE_REFUSALS.get(unused):
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
               f"{APP_SOURCE_REFUSALS[dropped]}")
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
    body = [f"flatpak remote-add --if-not-exists flathub {FLATHUB_REPO}"]
    body += [f"flatpak install -y --noninteractive flathub {app}" for app in apps]
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
    elif excluded:
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
    for app in software.get("apps", []) or []:
        if isinstance(app, str):
            optional.append(app)
            continue
        source, value = resolve_app(app)
        if source == "native":
            optional.append(value)
        elif source == "flatpak":
            flatpaks.append(value)

    terms = []
    if packages:
        terms.append("(with pkgs; [ %s ])" % " ".join(map(nix_pkg_path, packages)))
    if optional:
        terms.append(nix_optional_pkgs(optional))
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
    if flatpaks:
        opts.lines.extend(flatpak_unit(flatpaks))
        warn("software.flatpak[]: flatpak applications are installed by a "
             "first-boot unit (lis-flatpak), not by the configuration — the "
             "machine needs the network on its first boot")

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
            if purpose not in KEY_PURPOSES:
                refuse(f"keys['{kid}'].purpose {purpose!r} is not one of SPEC §17.1's "
                       f"roles ({', '.join(sorted(KEY_PURPOSES))})")
            elif purpose != "disk_encryption":
                refuse(f"keys['{kid}'].purpose {purpose!r}: this translator emits a "
                       "NixOS configuration and a disko layout, neither of which has "
                       "anywhere to consume a key for that role — only "
                       "disk_encryption reaches the target")
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
    for user in doc.get("users", []) or []:
        name = user["name"]
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
    if system.get("init") not in (None, "systemd", "auto"):
        refuse("system.init: NixOS is systemd-only")
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
        # Read and answered instead of read by nobody. NixOS has no mirror list
        # to pick a nearby entry from: nix.settings.substituters defaults to
        # the single https://cache.nixos.org, which is one geo-routed CDN
        # origin rather than a country's mirror. SPEC §14's "pick nearby
        # mirrors" therefore has no selection to make here — the substituter
        # already resolves to the nearest edge — and mirror.url is the field
        # that pins a different one.
        out.append(f"  # mirror.country: {country} — NixOS has one binary "
                   "cache, cache.nixos.org, served from a geo-routed CDN, so "
                   "there is no per-country mirror to select. Use mirror.url "
                   "to pin a different substituter.")
        warn(f"mirror.country {country!r}: NixOS keeps no mirror list — "
             "nix.settings.substituters is the single geo-routed "
             "cache.nixos.org — so no nearby mirror is selected. Set "
             "mirror.url to pin a substituter explicitly (SPEC §19: recorded "
             "as a substitution, not a drop)")
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


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Translate a LIS document into a plain NixOS configuration.")
    add_common_args(ap)
    ap.add_argument("--apply", "-a", action="store_true",
                    help="partition with disko and run nixos-install on the live system")
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
    check_section_fields(doc, "installer", set())
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
    check_snapshots(doc, tools={"snapper"}, boot_menu=False)
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
    decided = {f"registration.{leaf}" for leaf in
               _leaf_paths(raw.get("registration") or {})}
    check_unread(doc, ignore=APPLY_TIME_PATHS | decided
                 | {f"scripts.{stage}[].content" for stage in HOST_STAGES})

    if status := enforce(args.strict):
        return status

    if args.apply:
        import subprocess

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

        for stage in ("pre_install", "pre"):
            run_stage(stage)
        print(f"partitioning disks via disko: {disko_file}")
        disko = ("nix --extra-experimental-features 'nix-command flakes' "
                 f"run github:nix-community/disko/latest -- --mode disko {disko_file}")
        res = subprocess.run(disko, shell=True)
        if res.returncode != 0:
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
            run_stage("on_success")
            run_stage("pre_reboot")
        else:
            run_stage("on_error")
        return res.returncode
    return 0


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
