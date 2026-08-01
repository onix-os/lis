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
import json
import pathlib
import sys

from lis_common import (track, check_unread, luks_key_path, check_raid_consumers, registration_commands, enrollment_commands, resolve_disk_paths, check_snapshots, match_selectors, consume, check_script_fields, APPLY_TIME_PATHS,ALL_SECTIONS, add_common_args, check_firmware,
                        check_unhandled, check_section_fields, check_mirror, check_kernel_variant, check_user_sudo,
                        check_boot_extras, check_keymap, check_version, enforce,
                        load_doc, refuse, report, warn)


def nix_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def nix_list(items: list[str]) -> str:
    if not items:
        return "[ ]"
    return "[ " + " ".join(nix_str(i) for i in items) + " ]"


def disko_size(size: str) -> str:
    if size == "rest":
        return "100%"
    for unit, letter in (("MiB", "M"), ("GiB", "G"), ("TiB", "T")):
        if size.endswith(unit):
            return size[: -len(unit)] + letter
    if size.endswith("%"):
        return size
    raise ValueError(f"unparseable size: {size}")


# ── disko.nix ──────────────────────────────────────

DEFAULT_ZPOOL = "rpool"


def fs_content(lines, pad, fs, mountpoint, mount_options, subvolumes):
    """Emit the `content = { … }` block for a plain filesystem or swap area."""
    if fs in (None, "none"):
        return
    if fs == "swap":
        lines += [f"{pad}content = {{", f"{pad}  type = \"swap\";", f"{pad}}};"]
        return
    if fs == "btrfs" and subvolumes:
        lines += [f"{pad}content = {{",
                  f"{pad}  type = \"btrfs\";",
                  f"{pad}  extraArgs = [ \"-f\" ];",
                  f"{pad}  subvolumes = {{"]
        covered = any(s["mountpoint"] == mountpoint for s in subvolumes)
        if mountpoint and not covered:
            lines.append(f"{pad}    \"@\" = {{ mountpoint = {nix_str(mountpoint)}; }};")
        for sub in subvolumes:
            name = sub["name"] if sub["name"].startswith("@") else "@" + sub["name"]
            entry = f"{pad}    {nix_str(name)} = {{ mountpoint = {nix_str(sub['mountpoint'])};"
            if sub.get("mount_options"):
                entry += f" mountOptions = {nix_list(sub['mount_options'])};"
            lines.append(entry + " };")
        lines += [f"{pad}  }};", f"{pad}}};"]
        return
    lines += [f"{pad}content = {{",
              f"{pad}  type = \"filesystem\";",
              f"{pad}  format = {nix_str('vfat' if fs == 'vfat' else fs)};"]
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

        for group in self.lvm:
            for dev in group.get("devices", []):
                self.consumer[dev] = ("lvm_pv", group["name"])
        for array in self.raid:
            for dev in array.get("devices", []) + (array.get("spares", []) or []):
                self.consumer[dev] = ("mdraid", array["name"])

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
            mp = spec.get("mountpoint") or ("/" if spec.get("role") == "root" else None)
            fs = spec.get("fs") or ("swap" if spec.get("role") == "swap" else None)
            fs_content(lines, pad, fs, mp, spec.get("mount_options", []),
                       spec.get("subvolumes", []))


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
        if spares := array.get("spares"):
            out.append(f"        # {len(spares)} spare(s) declared: "
                       f"{', '.join(spares)} — disko marks them by device order")
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
            mp = spec.get("mountpoint") or ("/" if spec.get("role") == "root" else None)
            base = (spec.get("id") or spec.get("name") or "root")
            if mp:
                out += [f"          {nix_str(base)} = {{",
                        "            type = \"zfs_fs\";",
                        f"            mountpoint = {nix_str(mp)};"]
                if spec.get("mount_options"):
                    out.append(f"            options.mountpoint = \"legacy\";")
                out.append("          };")
            for sub in spec.get("subvolumes", []) or []:
                name = f"{base}-{sub['name'].lstrip('@')}"
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
    topology = Topology(storage, doc)

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
        index = 0
        for part in [p for p in partitions if p["disk"] == disk["id"]]:
            if part.get("existing"):
                warn(f"partition adoption ('existing') on disk '{disk['id']}' "
                     "is not supported by the default translator")
                continue
            index += 1
            name = part.get("id") or f"{part.get('role', 'part')}{index}"
            out.append(f"            {nix_str(name)} = {{")
            if part.get("size"):
                out.append(f"              size = {nix_str(disko_size(part['size']))};")
            if part.get("role") == "esp":
                mp = part.get("mountpoint", "/boot")
                out += ["              type = \"EF00\";",
                        "              content = {",
                        "                type = \"filesystem\";",
                        "                format = \"vfat\";",
                        f"                mountpoint = {nix_str(mp)};",
                        "                mountOptions = [ \"umask=0077\" ];",
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
                out.append(f"          {nix_str(vol['name'])} = {{")
                out.append(f"            size = {nix_str(disko_size(vol.get('size', 'rest')))};")
                fs_content(out, "            ", vol.get("fs"), vol.get("mountpoint"),
                           vol.get("mount_options", []), vol.get("subvolumes", []))
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
    for module in initramfs.get("include_modules", []):
        if module not in initrd:
            initrd.append(module)

    out = ["# Generated from a LIS document by lis2nixos (default translator).",
           "{ config, lib, pkgs, modulesPath, ... }:", "", "{",
           "  imports = [ (modulesPath + \"/installer/scan/not-detected.nix\") ];", "",
           f"  boot.initrd.availableKernelModules = {nix_list(initrd)};",
           "  boot.initrd.kernelModules = [ \"virtio_pci\" \"virtio_blk\" \"btrfs\" \"vfat\" ];",
           f"  boot.kernelModules = {nix_list(kernel.get('modules', []))};"]
    if kernel.get("blacklist"):
        out.append(f"  boot.blacklistedKernelModules = {nix_list(kernel['blacklist'])};")
    if kernel.get("params"):
        out.append(f"  boot.kernelParams = {nix_list(kernel['params'])};")
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
    topology = Topology(storage, doc)
    if not topology.encryption:
        return []
    disks = storage.get("disks", []) or (doc.get("target", {}) or {}).get("disks", [])
    disk_ids = [d["id"] for d in disks]

    # Same naming as mount_table: disko labels a partition `disk-<disk>-<id>`.
    backing: dict[str, str] = {}
    index: dict[str, int] = {}
    for part in storage.get("partitions", []) or []:
        disk_id = part.get("disk") or (disk_ids[0] if disk_ids else "main")
        index[disk_id] = index.get(disk_id, 0) + 1
        name = part.get("id") or f"{part.get('role', 'part')}{index[disk_id]}"
        backing[part.get("id") or name] = f"/dev/disk/by-partlabel/disk-{disk_id}-{name}"
    for array in storage.get("raid", []) or []:
        backing[array["name"]] = f"/dev/md/{array['name']}"

    devices = []
    for crypt in topology.encryption:
        device = backing.get(crypt["over"])
        if not device:
            warn(f"encryption '{crypt['id']}': over {crypt['over']!r} does not resolve "
                 "to a partition or array; the booted system will not unlock it")
            continue
        devices.append((crypt["id"], device,
                        (crypt.get("key", {}) or {}).get("keyfile")))
    return devices


def host_id(doc: dict) -> str:
    """ZFS demands a stable 8-hex-digit host id; derive it from the hostname."""
    import hashlib
    hostname = (doc.get("system", {}) or {}).get("hostname", "nixos")
    return hashlib.sha256(hostname.encode()).hexdigest()[:8]


def mount_table(doc: dict) -> tuple[list[tuple[str, str, str, list[str]]], list[str]]:
    """Every mount the document implies, resolved to the device disko will create.

    disko names GPT partitions `disk-<disk>-<partition>`, LUKS mappings
    `/dev/mapper/<id>`, arrays `/dev/md/<name>` and logical volumes
    `/dev/<vg>/<lv>` — so the mount table can be derived rather than guessed.
    """
    storage = doc.get("storage", {}) or {}
    topology = Topology(storage, doc)
    disks = storage.get("disks", []) or (doc.get("target", {}) or {}).get("disks", [])
    disk_of = {}
    for disk in disks:
        for part in storage.get("partitions", []) or []:
            if part.get("disk") == disk["id"]:
                disk_of[id(part)] = disk["id"]

    mounts: list[tuple[str, str, str, list[str]]] = []
    swaps: list[str] = []

    def add(spec: dict, device: str, default_mp: str | None = None) -> None:
        role = spec.get("role")
        fs = spec.get("fs") or {"esp": "vfat", "swap": "swap", "root": "btrfs"}.get(role)
        mountpoint = spec.get("mountpoint") or default_mp
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
                name = sub["name"] if sub["name"].startswith("@") else "@" + sub["name"]
                mounts.append((sub["mountpoint"], device, fs,
                               list(sub.get("mount_options", []) or options)
                               + [f"subvol={name}"]))
            return
        if mountpoint:
            mounts.append((mountpoint, device, fs, options))

    index: dict[str, int] = {}
    for part in storage.get("partitions", []) or []:
        disk_id = disk_of.get(id(part), "main")
        index[disk_id] = index.get(disk_id, 0) + 1
        name = part.get("id") or f"{part.get('role', 'part')}{index[disk_id]}"
        handle = part.get("id") or name
        device = f"/dev/disk/by-partlabel/disk-{disk_id}-{name}"
        if crypt := topology.luks_over.get(handle):
            device = f"/dev/mapper/{crypt['id']}"
            handle = crypt["id"]
        owner = topology.consumer.get(handle)
        if owner and owner[0] != "zfs":
            continue  # a raid or volume group owns it; its mounts come from there
        if owner:
            continue  # zpool datasets are emitted from the pool pass below
        add(part, device, "/boot" if part.get("role") == "esp" else None)

    for array in storage.get("raid", []) or []:
        handle = array["name"]
        device = f"/dev/md/{handle}"
        if crypt := topology.luks_over.get(handle):
            device = f"/dev/mapper/{crypt['id']}"
            handle = crypt["id"]
        if topology.consumer.get(handle):
            continue
        add(array, device)

    for group in storage.get("lvm", []) or []:
        for vol in group.get("volumes", []):
            add(vol, f"/dev/{group['name']}/{vol['name']}")

    for pool, info in topology.zpools.items():
        for spec in info["datasets"]:
            base = spec.get("id") or spec.get("name") or "root"
            mountpoint = spec.get("mountpoint") or ("/" if spec.get("role") == "root" else None)
            if mountpoint:
                mounts.append((mountpoint, f"{pool}/{base}", "zfs",
                               list(spec.get("mount_options", []))))
            for sub in spec.get("subvolumes", []) or []:
                mounts.append((sub["mountpoint"], f"{pool}/{base}-{sub['name'].lstrip('@')}",
                               "zfs", list(sub.get("mount_options", []))))

    mounts.sort(key=lambda m: (m[0].count("/"), m[0]))
    return mounts, swaps


# ── configuration.nix ────────────────────────────────────────────

def nix_script(body: str) -> str:
    """Wrap a shell body in a Nix indented string, escaping the two magic sequences."""
    return "''\n" + body.replace("''", "''''").replace("${", "''${") + "\n    ''"


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


# Absolute shell paths a document may name, mapped to the package NixOS needs.
SHELL_PATHS = {
    "/bin/bash": "bashInteractive", "/usr/bin/bash": "bashInteractive",
    "/bin/sh": "bashInteractive",
    "/bin/zsh": "zsh", "/usr/bin/zsh": "zsh",
    "/bin/fish": "fish", "/usr/bin/fish": "fish",
}

# Packages put on PATH for LIS script hooks, on both activation and first boot.
# pkgs.shadow splits su into its own output, so pkgs.shadow alone yields
# "su: command not found" in a hook that switches user.
HOOK_PATH = ("[ pkgs.bash pkgs.coreutils pkgs.shadow pkgs.shadow.su pkgs.util-linux "
             "pkgs.gnused pkgs.gnugrep pkgs.gawk pkgs.findutils pkgs.systemd ]")


def render_script_hooks(doc: dict) -> list[str]:
    """LIS script hooks → activation scripts and a first-boot unit.

    NixOS has no installer hook vocabulary, but it does have the two things the
    hooks actually need: something that runs on every activation (which includes
    the one `nixos-install` performs) and something that runs once at first boot.
    """
    scripts = doc.get("scripts", {}) or {}
    out: list[str] = []

    activation = [s["content"] for stage in ("post_storage", "post_install", "post",
                                             "pre_reboot", "on_success")
                  for s in scripts.get(stage, []) if s.get("content")]
    for user in doc.get("users", []) or []:
        for s in (user.get("scripts", {}) or {}).get("post_install", []):
            if c := s.get("content"):
                activation.append(
                    f"lis_as_user {user['name']} {json.dumps(c)}")

    firstboot = [s["content"] for s in scripts.get("firstboot", []) if s.get("content")]
    firstboot += enrollment_commands(doc)
    firstboot += registration_commands(doc, "nixos")
    for user in doc.get("users", []) or []:
        for s in (user.get("scripts", {}) or {}).get("firstboot", []):
            if c := s.get("content"):
                firstboot.append(
                    f"lis_as_user {user['name']} {json.dumps(c)}")

    for stage in ("pre_install", "pre"):
        if scripts.get(stage):
            warn(f"scripts.{stage} runs on the installer host before disko "
                 "(--apply), not inside the generated configuration")
    if scripts.get("on_error"):
        refuse("scripts.on_error has no NixOS equivalent")

    # Birth certificate (delivery.md §8) — recorded on every activation.
    birth = base64.b64encode(json.dumps(doc, separators=(",", ":")).encode()).decode()
    activation.append("install -d -m755 /var/lib/lis")
    activation.append(f"echo {birth} | base64 -d > /var/lib/lis/system.lis.json")
    activation.append("chmod 600 /var/lib/lis/system.lis.json")

    # Activation scripts run with a deliberately minimal PATH, so a hook that
    # calls anything outside coreutils (su, systemctl, sed) dies with 127 and
    # NixOS only prints a one-line "snippet failed". Give hooks a real PATH.
    if activation:
        out += ["  system.activationScripts.lis-hooks = {",
                # Hooks may reference the accounts the document declares, so they
                # must not run before the snippet that creates them.
                "    deps = [ \"users\" ];",
                "    text ="
                f"      \"export PATH=\\\"${{lib.makeBinPath {HOOK_PATH}}}:$PATH\\\"\\n\" +",
                "      " + nix_script(AS_USER_FN + "\n" + "\n".join(activation)) + ";",
                "  };"]
    if firstboot:
        body = ("install -d -m755 /var/lib/lis\n" + AS_USER_FN + "\n"
                + "\n".join(firstboot)
                + "\ntouch /var/lib/lis/.firstboot-done")
        out += ["  systemd.services.lis-firstboot = {",
                "    description = \"LIS first boot\";",
                "    wantedBy = [ \"multi-user.target\" ];",
                "    after = [ \"multi-user.target\" ];",
                "    unitConfig.ConditionPathExists = "
                "\"!/var/lib/lis/.firstboot-done\";",
                "    serviceConfig.Type = \"oneshot\";",
                f"    path = {HOOK_PATH};",
                "    script = " + nix_script(body) + ";",
                "  };"]
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

    target = doc.get("target", {}) or {}
    firmware = target.get("firmware", "uefi")
    loader = boot.get("loader", "auto")
    if loader in ("auto", "systemd-boot") and firmware != "bios":
        out += ["  boot.loader.systemd-boot.enable = true;",
                "  boot.loader.efi.canTouchEfiVariables = true;"]
    elif loader in ("auto", "grub", "systemd-boot"):
        if loader == "systemd-boot":
            refuse("boot.loader systemd-boot requires UEFI; target.firmware is bios")
        devices = [(d.get("match", {}) or {}).get("path")
                   for d in target.get("disks", [])]
        devices = [d for d in devices if d]
        out.append("  boot.loader.grub.enable = true;")
        if firmware == "bios":
            out.append(f"  boot.loader.grub.devices = {nix_list(devices)};")
        else:
            out.append("  boot.loader.grub.efiSupport = true;")
        if boot.get("os_prober"):
            out.append("  boot.loader.grub.useOSProber = true;")
    else:
        refuse(f"boot.loader {loader!r} has no NixOS module in the default translator")
    if boot.get("timeout") is not None:
        out.append(f"  boot.loader.timeout = {boot['timeout']};")
    if params := (boot.get("kernel", {}) or {}).get("params"):
        out.append(f"  boot.kernelParams = {nix_list(params)};")
        if any(p.startswith("console=ttyS") for p in params):
            # A serial console in the document implies a getty on it.
            out.append("  systemd.services.\"serial-getty@ttyS0\".enable = true;")
    out.append("")

    if system.get("hostname"):
        out.append(f"  networking.hostName = {nix_str(system['hostname'])};")
    if system.get("domain"):
        out.append(f"  networking.domain = {nix_str(system['domain'])};")
    if system.get("timezone"):
        out.append(f"  time.timeZone = {nix_str(system['timezone'])};")
    if system.get("hwclock") == "localtime":
        out.append("  time.hardwareClockInLocalTime = true;")
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
    if keymap.get("layout"):
        out.append(f"  services.xserver.xkb.layout = {nix_str(keymap['layout'])};")
        if keymap.get("variant"):
            out.append(f"  services.xserver.xkb.variant = {nix_str(keymap['variant'])};")
    time_cfg = system.get("time", {}) or {}
    if time_cfg.get("servers"):
        out.append(f"  networking.timeServers = {nix_list(time_cfg['servers'])};")
    if time_cfg.get("provider") == "chrony":
        out.append("  services.chrony.enable = true;")
    elif time_cfg.get("provider") == "openntpd":
        out.append("  services.openntpd.enable = true;")
    if time_cfg.get("ntp") is False:
        out.append("  services.timesyncd.enable = false;")
    if system.get("init") not in (None, "systemd", "auto"):
        refuse("system.init: NixOS is systemd-only")
    out.append("")

    manager = network.get("manager", "auto")
    if manager in ("auto", "networkmanager"):
        out.append("  networking.networkmanager.enable = true;")
    elif manager == "systemd-networkd":
        out.append("  networking.useNetworkd = true;")
    elif manager == "iwd":
        out.append("  networking.wireless.iwd.enable = true;")
    if network.get("interfaces"):
        refuse("network.interfaces: static interface configuration is not emitted by "
               "the default translator — declare it in your own module")
    if network.get("wifi"):
        refuse("network.wifi: NetworkManager profiles are stateful and are not emitted")
    for entry in network.get("hosts", []) or []:
        out.append(f"  networking.hosts.{nix_str(entry['ip'])} = {nix_list(entry['names'])};")
    firewall = network.get("firewall")
    if firewall:
        if "enabled" in firewall:
            out.append(f"  networking.firewall.enable = {str(firewall['enabled']).lower()};")
        tcp = [p.split("/")[0] for p in firewall.get("allow_ports", []) if p.endswith("/tcp")]
        udp = [p.split("/")[0] for p in firewall.get("allow_ports", []) if p.endswith("/udp")]
        if tcp:
            out.append(f"  networking.firewall.allowedTCPPorts = [ {' '.join(tcp)} ];")
        if udp:
            out.append(f"  networking.firewall.allowedUDPPorts = [ {' '.join(udp)} ];")
    ssh = network.get("ssh", {}) or {}
    if ssh.get("enabled"):
        out.append("  services.openssh.enable = true;")
        if "password_auth" in ssh:
            out.append(f"  services.openssh.settings.PasswordAuthentication = {str(ssh['password_auth']).lower()};")
        if ssh.get("permit_root"):
            out.append(f"  services.openssh.settings.PermitRootLogin = {nix_str(ssh['permit_root'])};")
    if kernel_set := check_kernel_variant(
            doc, {"lts": "linuxPackages", "hardened": "linuxPackages_hardened",
                  "realtime": "linuxPackages_rt"}, "NixOS"):
        out.append(f"  boot.kernelPackages = pkgs.{kernel_set};")
    module = ((system.get("security") or {}).get("module"))
    if module == "apparmor":
        out.append("  security.apparmor.enable = true;")
    elif module == "none":
        out.append("  security.apparmor.enable = false;")
    elif module == "selinux":
        refuse("system.security.module 'selinux' is not supported by NixOS")

    # hwclock and locale_overrides are emitted where the rest of the i18n and
    # time settings are; duplicating them here defined the same Nix option
    # twice, which is an evaluation error rather than a merge.
    if extra := system.get("extra_locales"):
        locales = ["en_US.UTF-8/UTF-8"] + [f"{l}/{l.split('.')[-1]}" for l in extra]
        out.append(f"  i18n.supportedLocales = {nix_list(locales)};")

    if mirror_url := (doc.get("mirror", {}) or {}).get("url"):
        out.append(f"  nix.settings.substituters = [ {nix_str(mirror_url)} ];")
    proxy = doc.get("proxy", {}) or {}
    if proxy.get("http"):
        out.append(f"  networking.proxy.default = {nix_str(proxy['http'])};")
    if proxy.get("no_proxy"):
        out.append(f"  networking.proxy.noProxy = {nix_str(','.join(proxy['no_proxy']))};")
    out.append("")

    wheel_nopasswd = False
    for user in doc.get("users", []):
        out.append(f"  users.users.{user['name']} = {{")
        if user["name"] != "root":
            out.append("    isNormalUser = true;")
        if user.get("uid") is not None:
            out.append(f"    uid = {user['uid']};")
        if user.get("comment"):
            out.append(f"    description = {nix_str(user['comment'])};")
        groups = list(user.get("groups", []))
        if user.get("admin") and "wheel" not in groups:
            groups.insert(0, "wheel")
        if user["name"] != "root" and groups:
            out.append(f"    extraGroups = {nix_list(groups)};")
        password = user.get("password") or {}
        if password.get("plain"):
            # SPEC §2.4: documents never carry plaintext secrets.
            refuse(f"user '{user['name']}': password.plain is a plaintext secret")
        elif password.get("locked"):
            out.append("    hashedPassword = \"!\";")
        elif password.get("hash"):
            out.append(f"    hashedPassword = {nix_str(password['hash'])};")
        else:
            # SPEC §9: "Omitting `password` leaves the account passwordless-locked."
            out.append("    hashedPassword = \"!\";")
        if user.get("ssh_authorized_keys"):
            out.append(f"    openssh.authorizedKeys.keys = {nix_list(user['ssh_authorized_keys'])};")
        shell = user.get("shell")
        if shell in ("zsh", "fish"):
            out.append(f"    shell = pkgs.{shell};")
        elif shell == "bash":
            out.append("    shell = pkgs.bashInteractive;")
        elif shell in SHELL_PATHS:
            # NixOS has no /bin/bash: outside the store only /bin/sh and
            # /usr/bin/env exist, so a literal path would produce an account
            # whose login fails with "Cannot execute". Map to the package that
            # the path names.
            out.append(f"    shell = pkgs.{SHELL_PATHS[shell]};")
        elif shell and shell.startswith("/nix/store/"):
            out.append(f"    shell = {nix_str(shell)};")
        elif shell and shell.startswith("/"):
            refuse(f"user '{user['name']}': shell {shell!r} is an absolute path "
                   "that does not exist on NixOS outside the store")
        elif shell:
            refuse(f"user '{user['name']}': shell {shell!r} has no pkgs attribute")
        if user.get("dotfiles"):
            pass   # honored by chroot_intents()
        out.append("  };")
        if user.get("sudo") == "nopasswd":
            wheel_nopasswd = True
        if shell in ("zsh", "fish"):
            out.append(f"  programs.{shell}.enable = true;")
    if wheel_nopasswd:
        out.append("  security.sudo.wheelNeedsPassword = false;")
    out.append("")

    role = software.get("role", "")
    role_map = {
        "desktop:gnome": ["  services.xserver.enable = true;",
                          "  services.xserver.displayManager.gdm.enable = true;",
                          "  services.xserver.desktopManager.gnome.enable = true;"],
        "desktop:kde": ["  services.xserver.enable = true;",
                        "  services.displayManager.sddm.enable = true;",
                        "  services.desktopManager.plasma6.enable = true;"],
        "desktop:xfce": ["  services.xserver.enable = true;",
                         "  services.xserver.desktopManager.xfce.enable = true;"],
        "desktop:sway": ["  programs.sway.enable = true;"],
        "desktop:hyprland": ["  programs.hyprland.enable = true;"],
    }
    if role in role_map:
        out += role_map[role]
    elif role not in ("", "minimal", "server"):
        refuse(f"software.role {role!r} has no default-translator mapping")
    
    # Process software.packages + software.apps
    pkgs_list = list(software.get("packages", []))
    for app in software.get("apps", []):
        if isinstance(app, str):
            pkgs_list.append(app)
        elif isinstance(app, dict):
            if name := (app.get("package") or app.get("name")):
                pkgs_list.append(name)

    if pkgs_list:
        out += ["  # Package names pass through verbatim; unresolvable names fail the build.",
                f"  environment.systemPackages = with pkgs; [ {' '.join(pkgs_list)} ];"]
    if software.get("exclude"):
        pass   # honored by chroot_intents()
    services = software.get("services", {}) or {}
    for unit in services.get("enable", []):
        mapped = {"sshd": None, "tailscaled": "  services.tailscale.enable = true;",
                  "docker": "  virtualisation.docker.enable = true;"}.get(unit, "?")
        if mapped == "?":
            refuse(f"software.services.enable {unit!r} has no default mapping — "
                   "add the module option yourself")
        elif mapped:
            out.append(mapped)
    for unit in services.get("disable", []):
        refuse(f"software.services.disable {unit!r} is not mapped by the default translator")
    if software.get("flatpak"):
        out.append("  services.flatpak.enable = true;")
        warn("flatpak app installation happens at runtime, not in configuration")
    if software.get("snap"):
        refuse("software.snap is not available on NixOS")

    if desktop:
        audio = desktop.get("audio", "auto")
        if audio in ("auto", "pipewire"):
            out.append("  services.pipewire = { enable = true; alsa.enable = true; pulse.enable = true; };")
        elif audio == "pulseaudio":
            out.append("  services.pulseaudio.enable = true;")
        if desktop.get("bluetooth"):
            out.append("  hardware.bluetooth.enable = true;")
        if desktop.get("printing"):
            out.append("  services.printing.enable = true;")
        if desktop.get("autologin"):
            out += ["  services.displayManager.autoLogin.enable = true;",
                    f"  services.displayManager.autoLogin.user = {nix_str(desktop['autologin'])};"]

    drivers = doc.get("drivers", {}) or {}
    gpu = drivers.get("gpu")
    if gpu in ("amdgpu", "intel"):
        out.append(f"  services.xserver.videoDrivers = [ {nix_str(gpu)} ];")
    elif gpu not in (None, "auto", "none", "nvidia", "nvidia-open"):
        refuse(f"drivers.gpu {gpu!r} has no NixOS mapping")
    if drivers.get("gpu") in ("nvidia", "nvidia-open"):
        out += ["  services.xserver.videoDrivers = [ \"nvidia\" ];",
                f"  hardware.nvidia.open = {str(drivers['gpu'] == 'nvidia-open').lower()};"]

    for entry in doc.get("files", []):
        content = entry["content"]
        if entry.get("encoding") == "base64":
            content = base64.b64decode(content).decode()
        if entry["path"].startswith("/etc/"):
            rest = entry["path"][len("/etc/"):]
            out.append(f"  environment.etc.{nix_str(rest)}.text = {nix_str(content)};")
            if entry.get("mode"):
                out.append(f"  environment.etc.{nix_str(rest)}.mode = {nix_str(entry['mode'])};")
            if owner := entry.get("owner"):
                user, _, group = owner.partition(":")
                out.append(f"  environment.etc.{nix_str(rest)}.user = {nix_str(user)};")
                if group:
                    out.append(f"  environment.etc.{nix_str(rest)}.group = {nix_str(group)};")
        else:
            refuse(f"files[] entry {entry['path']!r} is outside /etc; the default "
                   "translator writes files through environment.etc")

    out += render_script_hooks(doc)

    # Keys matrix
    if doc.get("keys"):
        warn("keys[] enrollment runs from the first-boot unit via "
             "systemd-cryptenroll; the generated configuration assumes the "
             "slot exists on the next boot")
    if (storage.get("snapshots", {}) or {}).get("enabled"):
        out.append("  services.snapper.configs.root = { SUBVOLUME = \"/\"; TIMELINE_CREATE = true; TIMELINE_CLEANUP = true; };")
    swap = storage.get("swap", {}) or {}
    if swap.get("zram"):
        out.append("  zramSwap.enable = true;")
    if swap.get("file"):
        size = swap["file"]["size"]
        gib = int(size[:-3]) if size.endswith("GiB") else 4
        out.append(f"  swapDevices = [ {{ device = {nix_str(swap['file']['path'])}; size = {gib * 1024}; }} ];")
    if system.get("kdump"):
        refuse("system.kdump has no NixOS default mapping")

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
    check_boot_extras(doc, {"kernel", "loader", "params", "timeout", "variant"})
    check_mirror(doc, {"url"})
    check_section_fields(doc, "desktop", {"audio", "autologin", "bluetooth", "printing"})
    check_section_fields(doc, "installer", set())
    check_keymap(doc, {"console", "font", "layout", "variant"})

    args.out.mkdir(parents=True, exist_ok=True)
    disko_file = args.out / "disko.nix"
    hw_file = args.out / "hardware.nix"
    config_file = args.out / "configuration.nix"

    disko_file.write_text(render_disko(doc))
    hw_file.write_text(render_hardware(doc))
    config_file.write_text(render_configuration(doc))
    report(disko_file, hw_file, config_file)

    # Fail closed *before* touching the machine, not after.
    check_raid_consumers(doc)
    check_snapshots(doc, tools={"snapper"}, boot_menu=False)
    check_script_fields(doc)
    check_unread(doc, ignore=APPLY_TIME_PATHS)

    if status := enforce(args.strict):
        return status

    if args.apply:
        import subprocess
        # The translation warns that pre-install hooks run here; run them, or
        # that warning is a promise the applier does not keep.
        for stage in ("pre_install", "pre"):
            for item in (doc.get("scripts", {}) or {}).get(stage, []) or []:
                if content := item.get("content"):
                    print(f"running scripts.{stage} on the installer host")
                    subprocess.run(content, shell=True, check=False)
        print(f"partitioning disks via disko: {disko_file}")
        disko = ("nix --extra-experimental-features 'nix-command flakes' "
                 f"run github:nix-community/disko/latest -- --mode disko {disko_file}")
        res = subprocess.run(disko, shell=True)
        if res.returncode != 0:
            res = subprocess.run(
                f"nix-shell -p disko --run 'disko --mode disko {disko_file}'", shell=True)
            if res.returncode != 0:
                return res.returncode

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
        return res.returncode
    return 0


def write_birth_certificate(doc: dict) -> None:
    """Record the applied document on the installed system (delivery.md §8)."""
    target = pathlib.Path("/mnt/var/lib/lis")
    try:
        target.mkdir(parents=True, exist_ok=True)
        cert = target / "system.lis.json"
        cert.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
        cert.chmod(0o600)
        print(f"wrote birth certificate {cert}")
    except OSError as err:
        print(f"warning: could not write birth certificate: {err}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
