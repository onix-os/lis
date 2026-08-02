#!/usr/bin/env python3
"""lis2archinstall — translate a LIS document into archinstall configuration.

Usage: lis2archinstall.py FILE.lis.{json,yaml} [--out DIR] [--lenient] [--apply]

Writes archinstall's two files into DIR (default '.'):
  user_configuration.json   — system configuration
  user_credentials.json     — users (password hashes applied via custom-commands)

`--apply` hands the pair to `archinstall --config … --creds … --silent`; the
native installer does the partitioning and pacstrap. This applier never
touches a block device itself.

Fail-closed by default (SPEC §2.3): core intent archinstall cannot express is
*refused* with exit status 1. `--lenient` downgrades refusals to warnings.
"""

import argparse
import base64
import json
import pathlib
import sys
import uuid

from lis_common import (track, check_unread, check_raid_consumers, chroot_intents, registration_commands, enrollment_commands, luks_key_path, SEED_MOUNT, resolve_disk_paths, check_snapshots, match_selectors, system_commands, security_packages, file_commands, uid_commands, password_field, shell_packages, check_arch, check_script_fields,ALL_SECTIONS, add_common_args, check_firmware, check_encryption_emitted,
                        check_unhandled, check_section_fields, sudoers_commands, boot_timeout_commands, driver_packages,
                        check_boot_extras, check_keymap, check_version, enforce,
                        load_doc, refuse, report, role_fs, role_mountpoint, warn)

SECTOR = {"unit": "B", "value": 512}
UNIT_BYTES = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30, "TiB": 1 << 40}
# A document with storage.raid takes two archinstall runs (see apply_raid): the
# profile for the first one, and the profile the second one is actually given.
DISKS_PROFILE = "user_configuration.disks.json"
APPLY_PROFILE = "user_configuration.apply.json"
# mdadm levels archinstall's target can assemble at boot.
RAID_LEVELS = {"0", "1", "4", "5", "6", "10"}

# LIS partition handle → the obj_id archinstall knows it by.
PV_IDS: dict[str, str] = {}

# obj_ids whose size the document left open ('rest' / percent).
REST_SIZED: set[str] = set()

# storage.wipe, which belongs to the first of the two archinstall runs a RAID
# layout takes. The profile the second run is given must never carry it.
FIRST_PASS_WIPE = False


def b64(text: str) -> str:
    """Payloads reach custom-commands base64-encoded — no shell escaping to get wrong."""
    return base64.b64encode(text.encode()).decode()


FS_MAP = {"vfat": "fat32", "ext4": "ext4", "btrfs": "btrfs", "xfs": "xfs",
          "f2fs": "f2fs", "swap": "linux-swap"}
KERNEL_MAP = {"default": "linux", "lts": "linux-lts", "hardened": "linux-hardened",
              "zen": "linux-zen", "realtime": "linux-rt"}
ROLE_MAP = {"desktop:gnome": "Gnome", "desktop:kde": "Kde", "desktop:hyprland": "Hyprland",
            "desktop:sway": "Sway", "desktop:xfce": "Xfce4", "desktop:cinnamon": "Cinnamon",
            "desktop:mate": "Mate", "desktop:budgie": "Budgie"}
BOOTLOADER_MAP = {"grub": "Grub", "systemd-boot": "Systemd-boot", "limine": "Limine",
                  "efistub": "Efistub"}


def size_obj(size: str, what: str) -> dict:
    """LIS size → archinstall Size object.

    archinstall's Unit enum is absolute lengths only (B/KiB/MiB/... /sectors) —
    there is no percent member — so 'rest' and percent sizes cannot be written
    into the profile. They are left as a placeholder here and resolved against
    the real device by resolve_rest_sizes() at apply time.
    """
    if size == "rest" or size.endswith("%"):
        return {"unit": "B", "value": 0, "sector_size": SECTOR, "lis_rest": True}
    for unit in ("GiB", "MiB", "TiB"):
        if size.endswith(unit):
            return {"unit": unit, "value": int(size[: -len(unit)]), "sector_size": SECTOR}
    refuse(f"{what}: unparseable size {size!r}")
    return {"unit": "GiB", "value": 1, "sector_size": SECTOR}


def start_of(size: str) -> int:
    """MiB consumed by a partition of this size, for laying out sequential starts."""
    if size.endswith("GiB"):
        return int(size[:-3]) * 1024
    if size.endswith("TiB"):
        return int(size[:-3]) * 1024 * 1024
    if size.endswith("MiB"):
        return int(size[:-3])
    return 0


def backing(storage: dict, handle: str) -> str:
    """A device handle resolved to the partition archinstall actually knows.

    A LUKS container is not a thing an archinstall profile can name: encryption
    is an attribute of the partition underneath it (DiskEncryption.partitions is
    a list of PartitionModification obj_ids, models/device.py:1477), and the
    mapper device that comes out is what the volume group is built on
    (filesystem.py:145 `self._setup_lvm(lvm_config, enc_mods)`). So a volume
    group whose `devices` name a container is really consuming that container's
    `over` partition.
    """
    seen: set[str] = set()
    containers = {c["id"]: c.get("over") for c in storage.get("encryption", []) or []}
    while handle in containers and handle not in seen:
        seen.add(handle)
        handle = containers[handle] or handle
    return handle


def lvm_pv_handles(storage: dict) -> set[str]:
    """Partition handles that end up carrying a physical volume."""
    return {backing(storage, handle)
            for group in storage.get("lvm", []) or []
            for handle in group.get("devices", []) or []}


def disk_config(doc: dict) -> dict | None:
    """LIS storage → archinstall disk_config (partitions + LVM volume groups)."""
    storage = doc.get("storage", {}) or {}
    target = doc.get("target", {}) or {}

    for container in storage.get("encryption", []) or []:
        if not luks_key_path(doc, container["id"]):
            refuse(f"storage.encryption ({container['id']}): no key material — declare "
                   "a keys[] entry with a seed: source, or place the passphrase at "
                   f"{SEED_MOUNT}/secrets/luks-{container['id']}.key")
        for method in container.get("unlock", []) or []:
            if method not in ("passphrase", "keyfile"):
                warn(f"storage.encryption ({container['id']}): unlock method "
                     f"{method!r} must be enrolled after installation")

    disks = {}
    for disk in target.get("disks", []):
        match_selectors(disk)
        path = (disk.get("match", {}) or {}).get("path")
        if not path:
            refuse(f"disk '{disk['id']}': archinstall needs an explicit match.path — "
                   "it cannot evaluate LIS match rules (type/largest/smallest)")
            continue
        disks[disk["id"]] = path

    # Resolved through storage.encryption: a group that names a LUKS container
    # consumes the partition under it, and that partition must be left
    # unmounted and unformatted just as a bare physical volume would be.
    consumed = lvm_pv_handles(storage)

    mods: dict[str, dict] = {}
    pv_ids = PV_IDS                      # LIS partition handle → archinstall obj_id
    pv_ids.clear()
    starts: dict[str, int] = {}
    for i, part in enumerate(storage.get("partitions", [])):
        path = disks.get(part.get("disk"))
        if not path:
            if part.get("disk") not in disks:
                refuse(f"partition {i}: references unknown disk handle {part.get('disk')!r}")
            continue
        if part.get("existing"):
            refuse(f"partition {i} on '{part['disk']}': adopting an existing partition "
                   "is not expressible in an archinstall profile")
            continue
        mod = mods.setdefault(path, {
            "device": path,
            "wipe": bool(storage.get("wipe", False)),
            "partitions": [],
        })
        handle = part.get("id") or f"auto-{i}"
        role = part.get("role")
        fs = role_fs(part)
        obj_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"lis:{path}:{handle}"))
        pv_ids[handle] = obj_id
        cursor = starts.setdefault(path, 1)
        entry = {
            "status": "create",
            "type": "primary",
            "obj_id": obj_id,
            # parted needs a filesystem type to write the partition entry, so
            # archinstall raises "File system type is not set" for a member with
            # none. ext4 is only the type recorded in the partition table; the
            # LVM/RAID step claims the device before anything is made on it.
            # A physical volume takes that placeholder whatever the document
            # says its role would imply — the filesystem the role stands for is
            # made on a logical volume, never here.
            "fs_type": ("ext4" if handle in consumed
                        else FS_MAP.get(fs, fs) if fs not in (None, "none")
                        else None),
            "start": {"unit": "MiB", "value": cursor, "sector_size": SECTOR},
            "size": size_obj(part.get("size", "rest"), f"partition '{handle}'"),
            "mountpoint": None,
            "mount_options": part.get("mount_options", []),
            "flags": [],
            "wipe": True,
            "dev_path": None,
        }
        if entry["size"].pop("lis_rest", False):
            REST_SIZED.add(obj_id)
        starts[path] = cursor + start_of(part.get("size", "rest"))

        if role == "esp":
            entry["mountpoint"] = role_mountpoint(part) or "/boot"
            entry["flags"] = ["boot", "esp"]
        elif role == "boot":
            # archinstall finds the boot partition by flag, not by mountpoint:
            # is_boot() is `PartitionFlag.BOOT in flags and mountpoint`. Without
            # the flag it installs the system and then refuses to lay down a
            # bootloader with "Could not detect boot at mountpoint /mnt".
            entry["mountpoint"] = role_mountpoint(part) or "/boot"
            entry["flags"] = ["boot"]
        elif role == "swap":
            entry["flags"] = ["swap"]
        elif handle not in consumed:
            entry["mountpoint"] = part.get("mountpoint") or ("/" if role == "root" else None)
        if subs := part.get("subvolumes"):
            if fs != "btrfs":
                refuse(f"partition '{handle}': subvolumes declared on a {fs} filesystem")
            entry["btrfs"] = [{"name": s["name"], "mountpoint": s["mountpoint"]} for s in subs]
        mod["partitions"].append(entry)

    if not mods:
        return None

    config = {"config_type": "manual_partitioning",
              "device_modifications": list(mods.values())}
    if lvm := lvm_config(storage, pv_ids):
        config["lvm_config"] = lvm
    return config


def part_node(device: str, number: int) -> str:
    """Kernel node of the Nth partition: /dev/vda1, but /dev/nvme0n1p1."""
    return f"{device}p{number}" if device[-1].isdigit() else f"{device}{number}"


def array_node(name: str) -> str:
    """The node mdadm gives an array — /dev/md0 for the usual md<N> names."""
    return (f"/dev/{name}" if name.startswith("md") and name[2:].isdigit()
            else f"/dev/md/{name}")


def raid_disk_config(doc: dict) -> dict | None:
    """LIS storage carrying storage.raid[] → archinstall's disk config.

    archinstall has no mdadm vocabulary (grep -rniE 'mdadm|raid' over the
    installed package returns nothing), and its device model cannot address an
    array that already exists either: load_devices() keeps only devices whose
    path is a *top-level* entry of `lsblk --json` — find_lsblk_info in
    lib/disk/utils.py scans that flat list and never descends into `children` —
    and an assembled array is always nested under its member partitions. On the
    ISO's archinstall 4.4 with /dev/md0 up, device_handler.devices reports
    exactly ['/dev/vda', '/dev/vdb']. A device_modification naming the array is
    therefore dropped in silence by DiskLayoutConfiguration.parse_arg
    ("if not device: continue"), so no physical volume can be placed on it.
    Nor is the pre-mounted layout a way round that: detect_pre_mounted_mods
    builds its model out of parted partition-table entries only, so an LVM
    volume — a device-mapper device with no partition table — is invisible to
    it, and add_bootloader dies with "Could not detect root at mountpoint"
    after pacstrap has already run (archlinux/archinstall#2925, #3914).

    What is left is to build the array *between* two archinstall runs and hand
    the second one an array that presents itself as a disk. This function emits
    that second run's profile; apply_raid() drives both. Everything the document
    asks for is still made by archinstall: it partitions both disks, it puts the
    partition table on the array, it creates the physical volume, the volume
    group, the logical volumes and their filesystems, and it mounts, bootstraps
    and installs the bootloader. This applier only assembles the array out of
    partitions archinstall itself created — mdadm --create writes no partition
    table and no filesystem.
    """
    storage = doc.get("storage", {}) or {}
    target = doc.get("target", {}) or {}

    disks: dict[str, str] = {}
    for disk in target.get("disks", []):
        match_selectors(disk)
        path = (disk.get("match", {}) or {}).get("path")
        if not path:
            refuse(f"disk '{disk['id']}': archinstall needs an explicit match.path — "
                   "it cannot evaluate LIS match rules (type/largest/smallest)")
            continue
        disks[disk["id"]] = path

    members = {handle for array in storage.get("raid", []) or []
               for handle in array.get("devices", []) or []}
    # Asking the second run to wipe a disk it is only re-reading would hand
    # device_handler.partition() a freshDisk() with no partitions to add to it,
    # and commit that empty table over the array's own members.
    global FIRST_PASS_WIPE
    FIRST_PASS_WIPE = bool(storage.get("wipe", False))

    mods: dict[str, dict] = {}
    numbers: dict[str, int] = {}
    starts: dict[str, int] = {}
    pv_ids = PV_IDS
    pv_ids.clear()
    boot_mounted = False

    for i, part in enumerate(storage.get("partitions", []) or []):
        path = disks.get(part.get("disk"))
        if not path:
            if part.get("disk") not in disks:
                refuse(f"partition {i}: references unknown disk handle {part.get('disk')!r}")
            continue
        if part.get("existing"):
            refuse(f"partition {i} on '{part['disk']}': adopting an existing partition "
                   "is not expressible in an archinstall profile")
            continue
        mod = mods.setdefault(path, {"device": path, "wipe": False, "partitions": []})
        handle = part.get("id") or f"auto-{i}"
        number = numbers[path] = numbers.get(path, 0) + 1
        obj_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"lis:{path}:{handle}"))
        pv_ids[handle] = obj_id
        cursor = starts.setdefault(path, 1)
        fs = role_fs(part)
        role = part.get("role")
        entry = {
            # The first pass creates these; by the second one they are on the
            # disk already. first_pass_profile() flips this back to 'create'.
            "status": "existing",
            "type": "primary",
            "obj_id": obj_id,
            # A partition-table filesystem type is mandatory — device_handler
            # _setup_partition reads safe_fs_type, which raises without one. On
            # an array member it is only the type recorded in the table: mdadm
            # claims the partition before anything is made on it.
            "fs_type": (FS_MAP.get(fs, fs) if fs not in (None, "none") else "ext4"),
            "start": {"unit": "MiB", "value": cursor, "sector_size": SECTOR},
            "size": size_obj(part.get("size", "rest"), f"partition '{handle}'"),
            # Only the mountpoint the document names, never the role default: a
            # mirrored layout declares a boot partition per disk, and mounting
            # both at /boot would hide one behind the other.
            "mountpoint": part.get("mountpoint"),
            "mount_options": part.get("mount_options", []),
            "flags": [],
            "wipe": True,
            "dev_path": part_node(path, number),
        }
        if entry["size"].pop("lis_rest", False):
            REST_SIZED.add(obj_id)
        starts[path] = cursor + start_of(part.get("size", "rest"))
        if role == "esp":
            entry["flags"] = ["boot", "esp"]
        elif role == "boot":
            # archinstall finds the boot partition by flag *and* mountpoint
            # (DeviceModification.get_boot_partition), and refuses to lay down a
            # bootloader without one.
            entry["flags"] = ["boot"]
        elif role == "swap":
            entry["flags"] = ["swap"]
        if entry["mountpoint"] and role in ("esp", "boot"):
            boot_mounted = True
        if handle in members and entry["mountpoint"]:
            refuse(f"partition '{handle}': an array member cannot carry a mountpoint")
        if part.get("subvolumes"):
            refuse(f"partition '{handle}': subvolumes on an array member or a plain "
                   "partition of a RAID layout are not expressible here")
        mod["partitions"].append(entry)

    if not mods:
        return None

    if not boot_mounted:
        refuse("storage.partitions: no boot/esp partition carries a mountpoint — "
               "archinstall installs a bootloader only onto a partition that is "
               "flagged boot *and* mounted, and the root of a RAID layout cannot "
               "hold /boot for it")

    for array in storage.get("raid", []) or []:
        node = array_node(array["name"])
        level = str(array.get("level", "")).removeprefix("raid")
        if level not in RAID_LEVELS:
            refuse(f"storage.raid ({array['name']}): mdadm has no level "
                   f"{array.get('level')!r}")
        for handle in array.get("devices", []) or []:
            if handle not in pv_ids:
                refuse(f"storage.raid ({array['name']}): member {handle!r} does not "
                       "resolve to a partition on a declared disk")
        obj_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"lis:raid:{array['name']}"))
        pv_ids[array["name"]] = obj_id
        REST_SIZED.add(obj_id)
        # The array is handed to archinstall as a disk, and a physical volume in
        # an archinstall profile is a *partition* (LvmVolumeGroup.pvs is a list
        # of PartitionModification), so the group lands on a partition table
        # written across the array rather than on the bare array. ArchWiki's
        # RAID page documents exactly that arrangement.
        mods[node] = {
            "device": node,
            "wipe": True,
            "partitions": [{
                "status": "create",
                "type": "primary",
                "obj_id": obj_id,
                "fs_type": "ext4",
                "start": {"unit": "MiB", "value": 1, "sector_size": SECTOR},
                "size": {"unit": "B", "value": 0, "sector_size": SECTOR},
                "mountpoint": None,
                "mount_options": [],
                "flags": [],
                "wipe": True,
                "dev_path": None,
            }],
        }

    config = {"config_type": "manual_partitioning",
              "device_modifications": list(mods.values())}
    if lvm := lvm_config(storage, pv_ids):
        config["lvm_config"] = lvm
    return config


def raid_encryption(storage: dict, dc: dict) -> None:
    """Refuse every LUKS container declared alongside storage.raid[].

    Nothing is emitted here, and that is the whole point: the two-pass RAID
    arrangement has no place to put a `disk_encryption` block, so a container
    reaching this branch was being dropped in silence and the machine installed
    in plaintext with a success report. Each of the three places a container can
    sit hits a different wall.

    * **Over an array member.** The array is built by mdadm between the two
      archinstall runs (assemble_arrays), out of the raw partition nodes and
      after `wipefs -a` clears the signature the first pass left — a LUKS header
      written there would be wiped and then overwritten by the array's own
      metadata. Nothing can be rearranged to avoid it: archinstall has no mdadm
      vocabulary at all, so it cannot be handed an open mapper to assemble the
      array on instead.

    * **Over a plain partition of either disk.** Those partitions are created by
      the first pass, so the second run is given them with
      status 'existing' (raid_disk_config), and encryption is applied inside
      FilesystemHandler._format_partitions, which acts only on partitions where
      `p.is_create_or_modify()` holds (disk/filesystem.py:86-95;
      ModificationStatus.EXIST fails that test, models/device.py:1015-1016). An
      existing partition is never re-formatted and so never encrypted. The first
      pass cannot carry it either — it runs `--script only_hd` with no
      credentials file, and DiskEncryption.parse_arg returns None without a
      password (models/device.py:1536-1537).

    * **Over the array itself.** The partition written across the array is the
      one partition the second run does create, so it is the one archinstall
      could encrypt — but `storage.raid[]` declares no `fs` and no `mountpoint`
      (spec/schema.json), so a container over an array is only usable through a
      volume group, and DiskEncryption.validate_enc (models/device.py:1511-1524)
      returns False for any layout with more than two partitions *and* an
      lvm_config. parse_arg (:1528-1530) then returns None, leaving archinstall
      to install with no encryption at all. A RAID layout is never that small:
      two array members, a mounted boot partition and the array's own partition
      is already four.
    """
    containers = storage.get("encryption", []) or []
    if not containers:
        return
    arrays = storage.get("raid", []) or []
    members = {handle for array in arrays
               for handle in (array.get("devices") or []) + (array.get("spares") or [])}
    names = {array["name"] for array in arrays}

    for container in containers:
        cid, over = container["id"], container.get("over")
        if over in members:
            refuse(f"storage.encryption ({cid}): {over!r} is a member of a RAID "
                   "array, and archinstall cannot install onto encrypted array "
                   "members — it has no mdadm vocabulary, so this applier builds "
                   "the array itself with `mdadm --create` over the bare "
                   "partition nodes (after `wipefs -a`), which would destroy the "
                   "LUKS header rather than assemble over it")
        elif over in names:
            if dc.get("lvm_config"):
                refuse(f"storage.encryption ({cid}): archinstall drops the whole "
                       "encryption config for this layout — "
                       "DiskEncryption.validate_enc (models/device.py:1511-1524) "
                       "returns False when a volume group is combined with more "
                       "than two partitions, parse_arg (:1528-1530) then returns "
                       "None, and the install proceeds unencrypted. A RAID layout "
                       "has at least four (two array members, a mounted boot "
                       "partition, and the partition written across the array)")
            else:
                refuse(f"storage.encryption ({cid}): nothing can consume a "
                       f"container over array {over!r} — storage.raid[] declares "
                       "no fs and no mountpoint, so the filesystem inside the "
                       "container would have to come from a volume group, and "
                       "archinstall refuses encryption with a volume group at "
                       "this partition count (models/device.py:1511-1524)")
        elif over in PV_IDS:
            refuse(f"storage.encryption ({cid}): partition {over!r} is created by "
                   "the first of the two archinstall runs a RAID layout takes and "
                   "handed to the second one as 'existing'. archinstall encrypts "
                   "only partitions it creates or modifies "
                   "(disk/filesystem.py:86-95, models/device.py:1015-1016), and "
                   "the first pass runs `--script only_hd` with no credentials "
                   "file, so neither run would encrypt it")
        else:
            refuse(f"storage.encryption ({cid}): 'over' handle {over!r} does not "
                   "resolve to a partition on a declared disk or to a declared "
                   "array")


def raid_commands(storage: dict) -> list[str]:
    """What the installed system needs in order to find its array at boot.

    archinstall writes the HOOKS line itself (Installer.mkinitcpio) and the only
    storage hook it ever adds is 'lvm2' (minimal_installation) — it has no mdadm
    hook, which is what archinstall#3914 asks for. Without mdadm_udev and an
    /etc/mdadm.conf the array is never assembled in early userspace, so the
    volume group on it never appears and the root filesystem is not found.
    """
    if not storage.get("raid"):
        return []
    return [
        "mdadm --detail --scan >> /etc/mdadm.conf",
        # Assemble the array before LVM goes looking for physical volumes; when
        # there is no volume group, fall back to right after udev.
        "sed -i '/^HOOKS=/ { s/\\blvm2\\b/mdadm_udev lvm2/; t; "
        "s/\\budev\\b/udev mdadm_udev/ }' /etc/mkinitcpio.conf",
        # archinstall already built an initramfs, before mdadm existed in the
        # target and with its own HOOKS line.
        "mkinitcpio -P",
    ]


def lvm_config(storage: dict, pv_ids: dict[str, str]) -> dict | None:
    """LIS storage.lvm[] → archinstall's LVM volume groups.

    `config_type` is 'default': archinstall's LvmLayoutType enum has only that
    member — `Manual = 'manual_lvm'` is commented out in its source, and passing
    it makes archinstall exit with "is not a valid LvmLayoutType".
    """
    groups = storage.get("lvm", []) or []
    if not groups:
        return None
    vol_groups = []
    for group in groups:
        pvs, missing = [], []
        for handle in group.get("devices", []):
            # A LUKS container is named by the group but is not an archinstall
            # object of its own; the partition it wraps is the physical volume,
            # and archinstall opens it and hands the mapper to vgcreate.
            under = backing(storage, handle)
            if under in pv_ids:
                pvs.append(pv_ids[under])
            else:
                missing.append(handle)
        for handle in missing:
            refuse(f"lvm '{group['name']}': device handle {handle!r} does not resolve "
                   "to a partition on a declared disk")
        if not pvs:
            continue
        volumes = []
        for vol in group.get("volumes", []):
            fs = vol.get("fs")
            entry = {
                "status": "create",
                "obj_id": str(uuid.uuid5(uuid.NAMESPACE_OID,
                                         f"lis:lvm:{group['name']}:{vol['name']}")),
                "name": vol["name"],
                "fs_type": FS_MAP.get(fs, fs) if fs not in (None, "none") else None,
                "length": size_obj(vol.get("size", "rest"),
                                   f"lvm '{group['name']}' volume '{vol['name']}'"),
                "mountpoint": vol.get("mountpoint"),
                "mount_options": vol.get("mount_options", []),
                "flags": [],
            }
            if entry["length"].pop("lis_rest", False):
                REST_SIZED.add(entry["obj_id"])
            if fs == "btrfs" and not vol.get("subvolumes"):
                # archinstall mounts a btrfs volume's subvolumes, not the volume:
                # with none declared it formats, probes, unmounts and mounts
                # nothing, and pacstrap then installs into the live filesystem.
                refuse(f"lvm volume '{vol['name']}': a btrfs volume needs at least "
                       "one subvolume — archinstall mounts subvolumes, so nothing "
                       "would be mounted at its mountpoint")
            if subs := vol.get("subvolumes"):
                if fs != "btrfs":
                    refuse(f"lvm volume '{vol['name']}': subvolumes on a {fs} filesystem")
                entry["btrfs"] = [{"name": s["name"], "mountpoint": s["mountpoint"]}
                                  for s in subs]
            volumes.append(entry)
        # LvmVolumeGroup.json() names these 'lvm_pvs' and 'volumes';
        # anything else raises KeyError deep inside archinstall's parser.
        vol_groups.append({"name": group["name"], "lvm_pvs": pvs, "volumes": volumes})
    # archinstall's LVM path formats the boot partition and nothing else
    # (filesystem.py: perform_filesystem_operations), so any other plain
    # partition is created but never made — swapon then fails on it.
    in_group = lvm_pv_handles(storage)
    for part in storage.get("partitions", []) or []:
        handle = part.get("id")
        if handle in in_group or part.get("role") in ("esp", "boot"):
            continue
        if role_fs(part) not in (None, "none"):
            refuse(f"partition '{handle}': archinstall formats only the boot "
                   "partition when a volume group is present — put it in the "
                   "group as a volume, or drop storage.lvm")
    if not vol_groups:
        return None
    return {"config_type": "default", "vol_groups": vol_groups}


def translate(doc: dict) -> tuple[dict, dict]:
    system = doc.get("system", {}) or {}
    boot = doc.get("boot", {}) or {}
    target = doc.get("target", {}) or {}
    storage = doc.get("storage", {}) or {}
    software = doc.get("software", {}) or {}
    desktop = doc.get("desktop", {}) or {}
    network = doc.get("network", {}) or {}
    scripts = doc.get("scripts", {}) or {}

    pkgs = list(software.get("packages", []))
    pkgs += driver_packages(doc, "arch")
    pkgs += shell_packages(doc)
    pkgs += security_packages(doc, "arch")
    for app in software.get("apps", []):
        if isinstance(app, str):
            pkgs.append(app)
        elif isinstance(app, dict):
            if name := (app.get("package") or app.get("name")):
                pkgs.append(name)
            if app.get("flatpak"):
                pkgs.append("flatpak")
    if storage.get("raid"):
        # The live ISO carries mdadm; the target does not get it from anywhere
        # else, and without it the array is not assembled at boot.
        pkgs.append("mdadm")

    loader = boot.get("loader", "auto")
    bootloader = BOOTLOADER_MAP.get(loader, "Systemd-boot" if loader == "auto" else None)
    if bootloader is None:
        refuse(f"boot.loader {loader!r} has no archinstall bootloader")
        bootloader = "Systemd-boot"

    variant = (boot.get("kernel", {}) or {}).get("variant", "default")
    if variant not in KERNEL_MAP:
        refuse(f"boot.kernel.variant {variant!r} has no Arch kernel package")

    config: dict = {
        "archinstall_language": "English",
        "hostname": system.get("hostname", "archlinux"),
        "timezone": system.get("timezone", "UTC"),
        "ntp": bool((system.get("time", {}) or {}).get("ntp", True)),
        "locale_config": {
            "kb_layout": _kb_layout(system),
            "sys_enc": "UTF-8",
            "sys_lang": system.get("locale", "en_US.UTF-8"),
        },
        "bootloader_config": {"bootloader": bootloader, "uki": False,
                              "removable": target.get("firmware") != "bios"},
        "kernels": [KERNEL_MAP.get(variant, "linux")],
        "packages": pkgs,
        "services": (software.get("services", {}) or {}).get("enable", []),
        "swap": bool((storage.get("swap", {}) or {}).get("zram", True)),
    }
    # archinstall has no kernel command line key — `kernels` names packages, not
    # parameters — so boot.kernel.params has to be written into the installed
    # bootloader instead. Setting an invented key would drop them silently.
    kernel_params = " ".join((boot.get("kernel", {}) or {}).get("params", []))
    if arrays := storage.get("raid"):
        names = ", ".join(a["name"] for a in arrays)
        warn(f"storage.raid ({names}): archinstall has no mdadm vocabulary, so the "
             "array is assembled by this applier out of partitions archinstall "
             "itself created, between two archinstall runs — every filesystem is "
             "still made by archinstall")
        warn(f"storage.raid ({names}): the volume group lands on a partition "
             "written across the array rather than on the bare array — an "
             "archinstall physical volume is a partition, never a whole device")
        raid_dc = raid_disk_config(doc)
        if raid_dc is not None:
            # An archinstall profile has exactly one place for encryption, and
            # this arrangement cannot reach it; say so rather than emit a
            # plaintext layout from a document that declared LUKS.
            raid_encryption(storage, raid_dc)
        config["disk_config"] = raid_dc
    elif dc := disk_config(doc):
        # Schema read from archinstall itself (models/device.py DiskEncryption):
        # disk_config carries disk_encryption {encryption_type, partitions:
        # [obj_id], lvm_volumes: []}, and the passphrase travels separately in
        # the credentials file as `encryption_password`.
        containers = storage.get("encryption", []) or []
        # Handles the volume groups name, before resolution: a container listed
        # here is a physical volume, so the layout is LVM *inside* LUKS.
        pv_devices = {handle for group in (storage.get("lvm", []) or [])
                      for handle in group.get("devices", []) or []}
        obj_ids, over_pv = [], []
        for container in containers:
            over = container.get("over")
            if over not in PV_IDS:
                refuse(f"storage.encryption ({container['id']}): 'over' handle "
                       f"{over!r} does not resolve to a partition on a declared "
                       "disk — archinstall encrypts partitions and logical "
                       "volumes, and only ever names them by obj_id "
                       "(models/device.py:1465 _DiskEncryptionSerialization)")
                continue
            obj_ids.append(PV_IDS[over])
            if container["id"] in pv_devices:
                over_pv.append(container["id"])
        if obj_ids:
            # EncryptionType (models/device.py:1439) distinguishes the two
            # nestings: LVM_ON_LUKS opens the LUKS partitions first and builds
            # the volume group on their mappers (filesystem.py:142-146), while
            # LUKS_ON_LVM creates the group first and encrypts each logical
            # volume (filesystem.py:151-154). The document wraps the *physical*
            # volume, so it is LVM on LUKS; emitting the other one would invert
            # the layout the document describes.
            enc_type = "luks"
            if dc.get("lvm_config"):
                if len(over_pv) != len(containers):
                    refuse("storage.encryption: with a volume group present, "
                           "archinstall runs an encrypted layout only through "
                           "_setup_lvm_encrypted (filesystem.py:141-159), which "
                           "matches LVM_ON_LUKS or LUKS_ON_LVM and nothing else "
                           "— every container has to wrap a physical volume of "
                           "the group, or none may")
                enc_type = "lvm_on_luks"
                # DiskEncryption.validate_enc (models/device.py:1511) drops the
                # whole encryption config — parse_arg returns None — when a
                # volume group is combined with more than two partitions, so
                # the install would silently come out unencrypted.
                total = sum(len(mod["partitions"]) for mod in dc["device_modifications"])
                if total > 2:
                    refuse(f"storage.partitions: {total} partitions with a volume "
                           "group and encryption — DiskEncryption.validate_enc "
                           "(models/device.py:1511) refuses more than two, and "
                           "archinstall then installs with no encryption at all")
            dc["disk_encryption"] = {
                "encryption_type": enc_type,
                "partitions": obj_ids,
                "lvm_volumes": [],
            }
        config["disk_config"] = dc
    elif storage:
        refuse("storage section could not be translated into an archinstall disk config")

    manager = network.get("manager", "auto")
    if manager in (None, "auto", "networkmanager"):
        config["network_config"] = {"type": "nm"}
    elif manager == "systemd-networkd":
        config["network_config"] = {"type": "iso"}
        warn("network.manager systemd-networkd: archinstall keeps the ISO network config")
    else:
        refuse(f"network.manager {manager!r} is not selectable in archinstall")
    if network.get("wifi"):
        refuse("network.wifi profiles are not expressible in an archinstall profile")
    if network.get("firewall"):
        pass   # honored by chroot_intents()

    role = software.get("role", "")
    if role in ROLE_MAP:
        config["profile_config"] = {
            "gfx_driver": gfx_driver(doc),
            "greeter": desktop.get("display_manager") if desktop.get("display_manager")
            not in (None, "auto") else None,
            "profile": {"main": "Desktop", "details": [ROLE_MAP[role]]},
        }
    elif role == "server":
        config["profile_config"] = {"profile": {"main": "Server", "details": []}}
    elif role not in ("", "minimal"):
        refuse(f"software.role {role!r} has no archinstall profile")

    if audio := desktop.get("audio"):
        if audio in ("pipewire", "pulseaudio", "auto"):
            config["audio_config"] = {"audio": "pipewire" if audio == "auto" else audio}
        else:
            refuse(f"desktop.audio {audio!r} has no archinstall mapping")

    for section in ("registration", "proxy", "mirror"):
        if doc.get(section):
            refuse(f"section {section!r} is not expressible in an archinstall profile")
    if software.get("snap"):
        refuse("software.snap is not available on Arch")
    if software.get("exclude"):
        pass   # honored by chroot_intents()

    creds_users, commands = [], []
    firstboot = [item["content"] for item in scripts.get("firstboot", [])
                 if item.get("content")]

    for stage in ("pre_install", "pre", "post_storage"):
        for item in scripts.get(stage, []):
            if content := item.get("content"):
                warn(f"scripts.{stage} runs after pacstrap as a custom-command "
                     "(archinstall has no pre-partition hook)")
                commands.append(content)

    for user in doc.get("users", []):
        if user["name"] == "root":
            continue
        password = user.get("password") or {}
        groups = list(user.get("groups", []))
        entry: dict = {
            "username": user["name"],
            "sudo": bool(user.get("admin", False)),
            "groups": groups,
        }
        if field := password_field(user):
            entry["enc_password"] = field
        else:
            refuse(f"user '{user['name']}': no password hash and not marked locked "
                   "(SPEC §2.4 forbids inlining a plaintext password)")
        creds_users.append(entry)
        if comment := user.get("comment"):
            commands.append(f"usermod -c {json.dumps(comment)} {user['name']}")
        if shell := user.get("shell"):
            commands.append(f"chsh -s {shell if shell.startswith('/') else '/usr/bin/' + shell} "
                            f"{user['name']}")
        for key in user.get("ssh_authorized_keys", []) or []:
            commands.append(
                f"install -d -m700 -o {user['name']} /home/{user['name']}/.ssh && "
                f"echo {json.dumps(key)} >> /home/{user['name']}/.ssh/authorized_keys")
        if user.get("dotfiles"):
            pass   # honored by chroot_intents()
        for script_item in (user.get("scripts", {}) or {}).get("post_install", []):
            if content := script_item.get("content"):
                commands.append(f"su - {user['name']} -c {json.dumps(content)}")
        for script_item in (user.get("scripts", {}) or {}).get("firstboot", []):
            if content := script_item.get("content"):
                firstboot.append(f"su - {user['name']} -c {json.dumps(content)}")

    commands += sudoers_commands(doc)
    commands += uid_commands(doc)
    commands += enrollment_commands(doc)
    commands += registration_commands(doc, "arch")
    commands += chroot_intents(doc, "arch")
    commands += system_commands(doc, "arch")
    commands += boot_timeout_commands(
        doc, "arch", "systemd-boot" if bootloader == "Systemd-boot" else "grub")
    commands += raid_commands(storage)

    for stage in ("post_install", "post", "pre_reboot", "on_success"):
        for item in scripts.get(stage, []):
            if content := item.get("content"):
                commands.append(content)

    if scripts.get("on_error"):
        refuse("scripts.on_error has no archinstall equivalent")

    for entry in doc.get("files", []) or []:
        commands += file_commands(entry)

    if firstboot:
        # archinstall's custom-commands run in the chroot during install, so a
        # first-boot stage has to be created rather than assumed.
        body = ("#!/bin/sh\n" + "\n".join(firstboot)
                + "\ntouch /var/lib/lis/.firstboot-done\n")
        unit = ("[Unit]\nDescription=LIS first boot\nAfter=multi-user.target\n"
                "ConditionPathExists=!/var/lib/lis/.firstboot-done\n\n"
                "[Service]\nType=oneshot\nExecStart=/usr/local/bin/lis-firstboot\n\n"
                "[Install]\nWantedBy=multi-user.target\n")
        commands.append("install -d -m755 /var/lib/lis /usr/local/bin")
        commands.append(f"echo {b64(body)} | base64 -d > /usr/local/bin/lis-firstboot")
        commands.append("chmod 755 /usr/local/bin/lis-firstboot")
        commands.append(f"echo {b64(unit)} | base64 -d "
                        "> /etc/systemd/system/lis-firstboot.service")
        commands.append("systemctl enable lis-firstboot.service")

    # Birth certificate (delivery.md §8).
    if kernel_params:
        if bootloader == "Grub":
            commands.append(
                "sed -i " + json.dumps(
                    f's|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT="'
                    f'{kernel_params}"|')
                + " /etc/default/grub")
            commands.append("grub-mkconfig -o /boot/grub/grub.cfg")
        elif bootloader == "Systemd-boot":
            commands.append(
                "for e in /boot/loader/entries/*.conf; do "
                f"[ -f \"$e\" ] && sed -i \"s|^options .*|& {kernel_params}|\" "
                "\"$e\"; done")
        else:
            refuse(f"boot.kernel.params cannot be applied to the {bootloader} "
                   "bootloader by this applier")

    commands.append("install -d -m755 /var/lib/lis")
    commands.append(f"echo {b64(json.dumps(doc, separators=(',', ':')))} | base64 -d "
                    "> /var/lib/lis/system.lis.json")
    commands.append("chmod 600 /var/lib/lis/system.lis.json")

    if commands:
        config["custom_commands"] = commands
    x_arch = doc.get("x-arch", {}) or {}
    if extra := x_arch.get("packages"):
        config["packages"] = config["packages"] + extra

    root = next((u for u in doc.get("users", []) if u["name"] == "root"), None)
    creds: dict = {"users": creds_users}
    root_password = (root or {}).get("password") or {}
    if root_password.get("hash"):
        creds["root_enc_password"] = root_password["hash"]
    else:
        # No root entry, or one without a usable hash: lock the account rather
        # than leave whatever archinstall would default to.
        commands.append("passwd -l root")
    return config, creds


def _kb_layout(system: dict) -> str:
    """archinstall takes a single kb_layout; report the map it cannot carry."""
    km = system.get("keymap", {}) or {}
    console, layout = km.get("console"), km.get("layout")
    if console and layout and console != layout:
        warn(f"system.keymap.console {console!r} is not applied — archinstall "
             f"takes one kb_layout, and layout {layout!r} was declared")
    return layout or console or "us"


def first_pass_profile(config: dict, arrays: set[str]) -> dict:
    """The disk-only profile for `archinstall --script only_hd`.

    Nothing is mounted by this pass and no volume group is named: it exists so
    that the partitions the array is built out of are made by archinstall, with
    the same geometry the real profile describes. only_hd runs
    FilesystemHandler.perform_filesystem_operations and stops before pacstrap.
    """
    import copy

    disks = copy.deepcopy(config["disk_config"])
    disks["device_modifications"] = [mod for mod in disks["device_modifications"]
                                     if mod["device"] not in arrays]
    for mod in disks["device_modifications"]:
        mod["wipe"] = FIRST_PASS_WIPE
        for part in mod["partitions"]:
            part["status"] = "create"
            part["dev_path"] = None
            # A mountpoint here would leave the first pass's mounts under /mnt
            # for the second one to trip over.
            part["mountpoint"] = None
    disks.pop("lvm_config", None)
    disks.pop("disk_encryption", None)
    return {"archinstall_language": "English", "disk_config": disks}


def assemble_arrays(doc: dict) -> dict[str, str] | None:
    """Create the declared arrays and expose each one as a whole disk.

    This is the single storage step archinstall cannot take — it has no mdadm
    vocabulary at all. Assembling an array out of partitions the installer has
    already created is not partitioning: no partition table is written here and
    no filesystem is made. The partition table that goes *onto* the array, the
    physical volume, the volume group, the volumes and their filesystems are all
    created by the second archinstall run.

    The loop alias is what makes that second run possible. archinstall's
    load_devices() keeps a device only if its path is a top-level entry of
    `lsblk --json` (find_lsblk_info scans that flat list), and an assembled
    array is always listed as a child of its members, so /dev/md0 is never in
    device_handler.devices — verified on archinstall 4.4. A loop device over the
    array has no holders, so it is top-level, and it is the same bytes at the
    same offsets: what archinstall writes through /dev/loop<N> is what the array
    carries, and the kernel finds it again as /dev/md0p1 at boot.

    Returns {array device path: path archinstall should be given}.
    """
    import subprocess

    storage = doc.get("storage", {}) or {}
    disks = {d["id"]: (d.get("match", {}) or {}).get("path")
             for d in (doc.get("target", {}) or {}).get("disks", [])}
    numbers: dict[str, int] = {}
    node_of: dict[str, str] = {}
    for part in storage.get("partitions", []) or []:
        device = disks.get(part.get("disk"))
        if not device:
            continue
        numbers[device] = numbers.get(device, 0) + 1
        node_of[part.get("id")] = part_node(device, numbers[device])

    def run(step: str) -> int:
        print(f"raid: {step}")
        return subprocess.run(step, shell=True).returncode

    aliases: dict[str, str] = {}
    for array in storage.get("raid", []) or []:
        node = array_node(array["name"])
        members = [node_of.get(handle, "") for handle in array.get("devices", [])]
        if not all(members):
            print(f"error: array '{array['name']}' names a member with no partition")
            return None
        level = str(array.get("level", "")).removeprefix("raid")
        # The first pass put a filesystem in every partition it created (an
        # archinstall partition must declare one); mdadm stops to ask about that
        # signature, and nothing is there to answer it.
        if run(f"wipefs -a {' '.join(members)}"):
            return None
        if run(f"mdadm --create {node} --run --level={level} "
               f"--raid-devices={len(members)} {' '.join(members)}"):
            return None
        run("udevadm settle || sleep 2")
        # -P so the kernel offers the partitions archinstall is about to create
        # on the array as /dev/loop<N>p<M>.
        result = subprocess.run(f"losetup -P --show -f {node}", shell=True,
                                capture_output=True, text=True)
        alias = result.stdout.strip()
        if result.returncode or not alias:
            print(f"error: could not expose {node} as a disk: {result.stderr.strip()}")
            return None
        print(f"raid: {node} is addressable as {alias}")
        aliases[node] = alias
    run("udevadm settle || sleep 2")
    return aliases


def apply_raid(doc: dict, config: dict, creds_file: pathlib.Path,
               out: pathlib.Path) -> int:
    """Run archinstall twice with the array built in between."""
    import subprocess

    arrays = {array_node(a["name"])
              for a in (doc.get("storage", {}) or {})["raid"]}
    first = first_pass_profile(config, arrays)
    resolve_rest_sizes(first)
    first_file = out / DISKS_PROFILE
    first_file.write_text(json.dumps(first, indent=2) + "\n")
    print(f"executing native installer (partitioning pass): archinstall --script "
          f"only_hd --config {first_file} --silent")
    status = subprocess.run(["archinstall", "--script", "only_hd", "--config",
                             str(first_file), "--silent", "--offline"]).returncode
    if status:
        return status

    aliases = assemble_arrays(doc)
    if aliases is None:
        return 1
    for mod in config["disk_config"]["device_modifications"]:
        if alias := aliases.get(mod["device"]):
            mod["device"] = alias
    # Only knowable now: the array does not exist until it is assembled.
    resolve_rest_sizes(config)
    apply_file = out / APPLY_PROFILE
    apply_file.write_text(json.dumps(config, indent=2) + "\n")
    cmd = ["archinstall", "--config", str(apply_file), "--creds", str(creds_file),
           "--silent"]
    print(f"executing native installer: {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def resolve_rest_sizes(config: dict) -> None:
    """Turn 'rest' and percent sizes into byte counts against the real disks.

    archinstall's `Unit` enum has no percent member — a size is an absolute
    length — so "use whatever is left" cannot be expressed in the profile at
    all. It is only knowable once the target device is in front of us, which is
    why this runs at apply time rather than at translation time.
    """
    import subprocess

    disk_config = config.get("disk_config") or {}
    for mod in disk_config.get("device_modifications", []):
        device = mod.get("device")
        if not device:
            continue
        try:
            out = subprocess.run(["lsblk", "-bdno", "SIZE", device],
                                 capture_output=True, text=True, check=True)
            total = int(out.stdout.strip())
        except (subprocess.CalledProcessError, ValueError, FileNotFoundError) as err:
            sys.exit(f"error: cannot read the size of {device} to resolve a "
                     f"'rest' partition: {err}")
        # Leave a mebibyte at the end for the backup GPT header.
        end = total - (1 << 20)
        for part in mod.get("partitions", []):
            if part.get("obj_id") not in REST_SIZED:
                continue
            start = part.get("start", {})
            start_bytes = start.get("value", 1) * UNIT_BYTES.get(start.get("unit"), 1 << 20)
            # archinstall rejects a partition whose length is not a whole number
            # of mebibytes — DiskLayoutConfiguration.parse_arg compares it with
            # Size.align(), which truncates to 1 MiB, and raises 'Partition is
            # misaligned'. A disk is normally a round number of mebibytes and
            # this changes nothing; an mdadm array, whose capacity is whatever
            # is left after the metadata, is not.
            length = (end - start_bytes) & ~((1 << 20) - 1)
            if length <= 0:
                sys.exit(f"error: no space left on {device} for the 'rest' partition")
            part["size"] = {"unit": "B", "value": length, "sector_size": SECTOR}

    # A 'rest' volume needs a real byte count: archinstall runs `lvcreate -L`
    # with whatever length it is given, and a zero length creates nothing — it
    # then polls for the missing volume ("LVM info query failed") until it gives
    # up. The group's capacity is the sum of its physical volumes.
    sizes = {}
    for mod in disk_config.get("device_modifications", []):
        for part in mod.get("partitions", []):
            size = part.get("size", {})
            sizes[part.get("obj_id")] = (size.get("value", 0)
                                         * UNIT_BYTES.get(size.get("unit"), 1))
    lvm = disk_config.get("lvm_config") or {}
    for group in lvm.get("vol_groups", []):
        capacity = sum(sizes.get(pv, 0) for pv in group.get("lvm_pvs", []))
        fixed = 0
        rest = []
        for vol in group.get("volumes", []):
            length = vol.get("length", {})
            if vol.get("obj_id") in REST_SIZED:
                rest.append(vol)
            else:
                fixed += length.get("value", 0) * UNIT_BYTES.get(length.get("unit"), 1)
        # LVM metadata and physical-extent rounding: leave a margin so lvcreate
        # cannot fail for being a few extents short.
        remainder = capacity - fixed - (16 << 20)
        for vol in rest:
            if remainder <= 0:
                sys.exit(f"error: no space left in volume group '{group['name']}' "
                         f"for '{vol['name']}'")
            vol["length"] = {"unit": "B", "value": remainder // len(rest),
                             "sector_size": SECTOR}

def gfx_driver(doc: dict) -> str | None:
    gpu = (doc.get("drivers", {}) or {}).get("gpu")
    return {"nvidia": "Nvidia (proprietary)", "nvidia-open": "Nvidia (open kernel module)",
            "amd": "AMD / ATI (open-source)", "intel": "Intel (open-source)"}.get(gpu)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Translate a LIS document into an archinstall profile.")
    add_common_args(ap)
    ap.add_argument("--apply", "-a", action="store_true",
                    help="run archinstall on the live system with the generated profile")
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
    check_section_fields(doc, "desktop", {"audio", "display_manager"})
    check_section_fields(doc, "installer", set())
    check_keymap(doc, {"console", "layout"})

    config, creds = translate(doc)

    # Fail-closed backstop for the layout artifact, checked on its own: the
    # credentials file carries only the passphrase, and only under --apply, so
    # pairing the two would let a plaintext profile pass on the strength of a
    # password sitting in its sibling. Evidence is per container — the obj_id of
    # the partition it wraps, which is the only name archinstall has for it
    # (models/device.py:1465 _DiskEncryptionSerialization) — so a document with
    # several containers is checked one by one rather than as a group.
    check_encryption_emitted(
        doc, (config.get("disk_config") or {}).get("disk_encryption") or {},
        marker=lambda container: PV_IDS.get(container.get("over")) or container["id"],
        label="archinstall profile's disk_encryption block")

    args.out.mkdir(parents=True, exist_ok=True)
    cfg_file = args.out / "user_configuration.json"
    creds_file = args.out / "user_credentials.json"
    cfg_file.write_text(json.dumps(config, indent=2) + "\n")
    if args.apply:
        # Resolved only when actually installing (delivery.md §6): a plain
        # translation stays shareable, and the passphrase never lands in an
        # artifact someone might commit. The file is already mode 0600.
        for container in (doc.get("storage", {}) or {}).get("encryption", []) or []:
            key_path = luks_key_path(doc, container["id"])
            if not key_path:
                continue
            try:
                creds["encryption_password"] = pathlib.Path(key_path).read_text().strip()
            except OSError as err:
                sys.exit(f"error: --apply needs key material for "
                         f"'{container['id']}' at {key_path}: {err}")
            break

    creds_file.write_text(json.dumps(creds, indent=2) + "\n")
    creds_file.chmod(0o600)
    report(cfg_file, creds_file)

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
        if not shutil.which("archinstall"):
            sys.exit("error: --apply requested, but 'archinstall' is not on PATH "
                     "(are you running on the Arch live ISO?)")
        if (doc.get("storage", {}) or {}).get("raid"):
            # An array cannot be sized, or even addressed, before it exists —
            # apply_raid resolves the profile as it builds it.
            return apply_raid(doc, config, creds_file, args.out)
        resolve_rest_sizes(config)
        cfg_file.write_text(json.dumps(config, indent=2) + "\n")
        cmd = ["archinstall", "--config", str(cfg_file), "--creds", str(creds_file), "--silent"]
        print(f"executing native installer: {' '.join(cmd)}")
        return subprocess.run(cmd).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
