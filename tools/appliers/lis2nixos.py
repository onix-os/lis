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
import re
import sys

from lis_common import (track, check_unread, luks_key_path, check_raid_consumers, registration_commands, enrollment_commands, resolve_disk_paths, check_snapshots, match_selectors, consume, password_field, secret_ref, APPLY_TIME_PATHS,ALL_SECTIONS, add_common_args, check_firmware,
                        check_encryption_emitted, resolve_mountpoints,
                        check_unhandled, check_section_fields, check_mirror, check_kernel_variant, check_user_sudo,
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

    def mountpoint_of(self, spec: dict) -> str | None:
        """Where this spec's filesystem goes, after the whole layout arbitrated.

        Partitions get the arbitrated answer; an array or a logical volume is
        not a partition, was not in that list, and keeps what it declares.
        """
        if id(spec) in self.part_mounts:
            return self.part_mounts[id(spec)]
        return spec.get("mountpoint") or ("/" if spec.get("role") == "root" else None)

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
                mp = topology.mountpoint_of(part)
                out += ["              type = \"EF00\";",
                        "              content = {",
                        "                type = \"filesystem\";",
                        "                format = \"vfat\";"]
                if mp:
                    # An ESP nothing mounts is a refusal above, not a partial
                    # attribute set: disko's filesystem type wants a real path.
                    out.append(f"                mountpoint = {nix_str(mp)};")
                out += ["                mountOptions = [ \"umask=0077\" ];",
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
    topology = Topology(storage, doc)
    disks = storage.get("disks", []) or (doc.get("target", {}) or {}).get("disks", [])
    disk_of = {}
    for disk in disks:
        for part in storage.get("partitions", []) or []:
            if part.get("disk") == disk["id"]:
                disk_of[id(part)] = disk["id"]

    mounts: list[tuple[str, str, str, list[str]]] = []
    swaps: list[str] = []

    def add(spec: dict, device: str, mountpoint: str | None) -> None:
        # The mountpoint is decided by the caller, not rediscovered here: the
        # partitions come from one arbitration over the whole layout, so the
        # fstab this builds cannot disagree with the disko config that made it.
        role = spec.get("role")
        fs = spec.get("fs") or {"esp": "vfat", "swap": "swap", "root": "btrfs"}.get(role)
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


# The binary each dotfiles method needs on the first-boot unit's PATH. A
# systemd unit has no inherited PATH at all, so an unlisted tool is a plain
# "command not found" in the journal and a home directory that never appears.
DOTFILES_TOOLS = {"raw": ["pkgs.git"],
                  "stow": ["pkgs.git", "pkgs.stow"],
                  "chezmoi": ["pkgs.git", "pkgs.chezmoi"]}


def hook_path(doc: dict) -> str:
    """HOOK_PATH plus whatever this document's own first-boot work needs."""
    extra = []
    if snapshots_wanted(doc):
        extra.append("pkgs.btrfs-progs")
    for user in doc.get("users", []) or []:
        method = (user.get("dotfiles") or {}).get("method") or "raw"
        if (user.get("dotfiles") or {}).get("repo"):
            extra += [p for p in DOTFILES_TOOLS.get(method, []) if p not in extra]
    if not extra:
        return HOOK_PATH
    return HOOK_PATH[:-1] + " ".join(extra) + " ]"


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
    disko cannot make it either — it is a subvolume of the root subvolume, which
    does not exist until the root filesystem is mounted.
    """
    if not snapshots_wanted(doc):
        return []
    return ["if [ ! -d /.snapshots ]; then "
            "btrfs subvolume create /.snapshots && chmod 750 /.snapshots; fi"]


def host_stage_bodies(doc: dict, stage: str) -> list[str]:
    """The script bodies `--apply` runs on the installer host for one stage."""
    scripts = doc.get("scripts", {}) or {}
    return [s["content"] for s in scripts.get(stage, []) or [] if s.get("content")]


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
    for entry in doc.get("files", []) or []:
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

    # files[] first: a post_install hook may well edit a file the document drops.
    activation = list(file_cmds or [])
    activation += [s["content"] for stage in ("post_install", "post")
                   for s in scripts.get(stage, []) if s.get("content")]
    for user in doc.get("users", []) or []:
        # `post` after `post_install`, the ordering SPEC §13 gives the two
        # phases. The user-level `post` stage used to reach nothing at all here:
        # only `post_install` and `firstboot` were collected, so a document that
        # put its per-user work in `post` had it silently dropped.
        for stage in ("post_install", "post"):
            for s in (user.get("scripts", {}) or {}).get(stage, []):
                if c := s.get("content"):
                    activation.append(as_user(user["name"], c))

    firstboot = [s["content"] for s in scripts.get("firstboot", []) if s.get("content")]
    firstboot += snapshot_commands(doc)
    firstboot += dotfiles_commands(doc)
    firstboot += enrollment_commands(doc)
    firstboot += registration_commands(doc, "nixos")
    for user in doc.get("users", []) or []:
        for s in (user.get("scripts", {}) or {}).get("firstboot", []):
            if c := s.get("content"):
                firstboot.append(as_user(user["name"], c))

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
    activation.append("install -d -m755 /var/lib/lis")
    activation.append(f"echo {birth} | base64 -d > /var/lib/lis/system.lis.json")
    activation.append("chmod 600 /var/lib/lis/system.lis.json")

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
                # systemd.services.<n>.path *replaces* PATH rather than adding
                # to it, so a tool the unit needs has to be named here. btrfs
                # only when the document asks for snapshots: adding it
                # unconditionally would put btrfs-progs in the closure of every
                # system this translator emits.
                f"    path = {hook_path(doc)};",
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
    loader = boot.get("loader", "auto")
    os_prober = boot.get("os_prober")
    pwhash = boot.get("password_hash")
    serial = (boot.get("console", {}) or {}).get("serial")
    out: list[str] = []

    # `auto` is the translator's choice, so it is made against the rest of the
    # document: two of these fields exist only in GRUB, and picking
    # systemd-boot for a document that asks for them would turn an honorable
    # translation into a refusal for no reason.
    if loader == "auto" and firmware != "bios" and (pwhash or os_prober):
        asked = " and ".join(n for n, v in (("boot.password_hash", pwhash),
                                            ("boot.os_prober", os_prober)) if v)
        warn(f"boot.loader auto resolves to grub, not systemd-boot: {asked} "
             "has no systemd-boot equivalent in NixOS")
        loader = "grub"

    on_systemd_boot = loader in ("auto", "systemd-boot") and firmware != "bios"
    if on_systemd_boot:
        out += ["  boot.loader.systemd-boot.enable = true;",
                "  boot.loader.efi.canTouchEfiVariables = true;"]
        if os_prober:
            refuse("boot.os_prober: only GRUB detects other operating systems on "
                   "NixOS (boot.loader.grub.useOSProber, grub.nix:485); systemd-boot "
                   "has no equivalent option — set boot.loader to grub")
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
        if os_prober:
            out.append("  boot.loader.grub.useOSProber = true;")
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

    if boot.get("timeout") is not None:
        out.append(f"  boot.loader.timeout = {boot['timeout']};")

    # boot.console.serial is the intent "give me a serial console"; the console=
    # parameter is one of the two halves the spec says it expands to (§7).
    params = list(kernel.get("params", []) or [])
    if serial and f"console={serial}" not in params:
        params.append(f"console={serial}")
    if params:
        out.append(f"  boot.kernelParams = {nix_list(params)};")
    for tty in serial_console_ttys(params):
        out.append(f"  systemd.services.\"serial-getty@{tty}\".enable = true;")

    if kernel_set := check_kernel_variant(doc, KERNEL_PACKAGES, "NixOS"):
        # Dashes are legal in a Nix identifier, so linuxPackages-rt needs no
        # quoting; it is still an attribute name, not an option path.
        out.append(f"  boot.kernelPackages = pkgs.{kernel_set};")

    if boot.get("uki"):
        refuse("boot.uki: NixOS 24.11 can build a UKI (system.build.uki, "
               "modules/system/boot/uki.nix:108) but no bootloader module installs one "
               "— the single in-tree consumer is the disk-image builder "
               "modules/image/repart-verity-store.nix:169, and there is no "
               "boot.uki.enable; a UKI install needs the out-of-tree lanzaboote module")
    if boot.get("secure_boot") is True:
        refuse("boot.secure_boot: NixOS 24.11 signs nothing — no module in "
               "nixos/modules installs a shim or a signed loader (the tree's only "
               "'secureBoot' is virtualisation.useSecureBoot, a guest-firmware switch "
               "for test VMs); Secure Boot needs the out-of-tree lanzaboote module")
    generator = (boot.get("initramfs", {}) or {}).get("generator")
    if generator not in (None, "auto"):
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


def render_firewall(firewall: dict, out: list[str]) -> None:
    """network.firewall → networking.firewall.*.

    A port *range* is why this is not two list comprehensions: '8000-8010/tcp'
    used to be pasted straight into `allowedTCPPorts`, which is not an integer
    and not even parseable Nix — and under --apply the disks are already gone by
    the time the evaluation fails.
    """
    if services := firewall.get("allow_services"):
        refuse(f"network.firewall.allow_services {services!r}: NixOS has no "
               "service-name to port table — networking.firewall only takes "
               "numbers (allowedTCPPorts/allowedTCPPortRanges); name the ports "
               "in network.firewall.allow_ports instead")
    enabled = firewall.get("enabled")
    if enabled is not None:
        out.append(f"  networking.firewall.enable = {str(enabled).lower()};")
    ports: dict[str, list[int]] = {"tcp": [], "udp": []}
    ranges: dict[str, list[tuple[int, int]]] = {"tcp": [], "udp": []}
    for spec in firewall.get("allow_ports", []) or []:
        port, _, proto = str(spec).partition("/")
        low, dash, high = port.partition("-")
        if proto not in ports or not low.isdigit() or (dash and not high.isdigit()):
            refuse(f"network.firewall.allow_ports {spec!r} is not <port>[-<port>]/<tcp|udp>")
            continue
        if dash:
            ranges[proto].append((int(low), int(high)))
        else:
            ports[proto].append(int(low))
    if enabled is False and (any(ports.values()) or any(ranges.values())):
        warn("network.firewall.allow_ports is not emitted: the same document sets "
             "network.firewall.enabled false, and networking.firewall ignores its "
             "allow lists when disabled")
        return
    for proto, opt in (("tcp", "TCP"), ("udp", "UDP")):
        if ports[proto]:
            listed = " ".join(str(p) for p in ports[proto])
            out.append(f"  networking.firewall.allowed{opt}Ports = [ {listed} ];")
        if ranges[proto]:
            listed = " ".join(f"{{ from = {lo}; to = {hi}; }}" for lo, hi in ranges[proto])
            out.append(f"  networking.firewall.allowed{opt}PortRanges = [ {listed} ];")


def render_interfaces(interfaces: list, manager: str, out: list[str]) -> list[str]:
    """network.interfaces[] → networking.interfaces.<name> and friends.

    Returns the interface names configured here so the caller can hand them to
    NetworkManager as `unmanaged`: NixOS assigns the addresses through its own
    units, and NM left to itself would start a second, contradictory DHCP on the
    very interface the document pinned.
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
        for index, net in enumerate(wifi):
            ssid = net["ssid"]
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


def render_network(doc: dict) -> list[str]:
    network = doc.get("network", {}) or {}
    out: list[str] = []

    interfaces = network.get("interfaces", []) or []
    wifi = network.get("wifi", []) or []
    manager = network.get("manager", "auto")
    if manager in ("auto", "networkmanager"):
        out.append("  networking.networkmanager.enable = true;")
    elif manager == "systemd-networkd":
        out.append("  networking.useNetworkd = true;")
    elif manager == "iwd":
        out.append("  networking.wireless.iwd.enable = true;")

    static = render_interfaces(interfaces, manager, out)
    if static and manager in ("auto", "networkmanager"):
        out.append(f"  networking.networkmanager.unmanaged = {nix_list(static)};")
    if wifi:
        render_wifi(wifi, manager, out)

    for entry in network.get("hosts", []) or []:
        out.append(f"  networking.hosts.{nix_str(entry['ip'])} = {nix_list(entry['names'])};")

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
}

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
        # No refusal and no silence: NixOS genuinely has no server package
        # set, and saying so is the difference between "we chose not to" and
        # the silent drop this line used to be.
        warn("software.role 'server': NixOS ships no server metapackage — its "
             "base closure is already headless, so the role adds nothing. Name "
             "what the server needs in software.packages[] and "
             "software.services.enable[]")
    elif role:
        refuse(f"software.role {role!r} has no default-translator mapping")

    # software.exclude[] — the only subtractable packages in a NixOS closure.
    excluded = list(software.get("exclude", []) or [])
    for name in excluded:
        if name not in DEFAULT_PACKAGES:
            refuse(f"software.exclude {name!r}: NixOS builds a closure rather "
                   "than installing into one, so a package cannot be removed "
                   "after the fact — the only subtractable set is "
                   "environment.defaultPackages "
                   "(nixos/modules/config/system-path.nix), which holds exactly "
                   f"{', '.join(DEFAULT_PACKAGES)}. Anything else arrives "
                   "because a module pulls it in; turn that module off instead")
    if role == "minimal":
        opts.set("environment.defaultPackages", "lib.mkForce [ ]")
    elif excluded:
        keep = [p for p in DEFAULT_PACKAGES if p not in excluded]
        keep_expr = f"(with pkgs; [ {' '.join(keep)} ])" if keep else "[ ]"
        opts.set("environment.defaultPackages", f"lib.mkForce {keep_expr}")

    packages = list(software.get("packages", []) or [])
    flatpaks = list(software.get("flatpak", []) or [])
    for app in software.get("apps", []) or []:
        if isinstance(app, str):
            packages.append(app)
            continue
        source, value = resolve_app(app)
        if source == "native":
            packages.append(value)
        elif source == "flatpak":
            flatpaks.append(value)

    if packages:
        opts.raw("  # Package names pass through verbatim; "
                 "unresolvable names fail the build.")
        opts.set("environment.systemPackages",
                 f"with pkgs; [ {' '.join(packages)} ]")

    services = software.get("services", {}) or {}
    # network.ssh writes services.openssh itself; read it so that asking for
    # sshd in both places is one switch rather than a duplicate attribute.
    if ((doc.get("network") or {}).get("ssh") or {}).get("enabled") is not None:
        enabled = ((doc.get("network") or {}).get("ssh") or {})["enabled"]
        opts.taken("services.openssh.enable", str(bool(enabled)).lower())
    for unit in services.get("enable", []) or []:
        if option := SERVICE_OPTIONS.get(unit):
            opts.set(option, "true", label=f"software.services.enable {unit!r}")
        else:
            refuse(f"software.services.enable {unit!r}: NixOS builds its unit "
                   "set from modules, so enabling a unit no module declares is "
                   "a no-op — this translator maps only the units that have an "
                   f"option ({', '.join(sorted(SERVICE_OPTIONS))}); declare the "
                   "rest in your own module")
    suppressed: list[str] = []
    for unit in services.get("disable", []) or []:
        if option := SERVICE_OPTIONS.get(unit):
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
        declared = {user["name"] for user in doc.get("users", []) or []}
        if autologin not in declared:
            refuse(f"desktop.autologin {autologin!r} names an account this "
                   "document does not declare (schema.md §12: autologin must "
                   "name an existing user)")
        elif manager is None:
            refuse("desktop.autologin needs a greeter to log in through, and "
                   "desktop.display_manager resolved to none")
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
    elif gpu not in (None, "auto", "none"):
        refuse(f"drivers.gpu {gpu!r} has no NixOS mapping")

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
    """users[] → users.users.<name>, per-user sudo rules and login shells."""
    out: list[str] = []
    nopasswd: list[str] = []
    # An authorized_keys file on a machine with no sshd authorises nothing.
    # Both spellings count: network.ssh.enabled and software.services.enable
    # reach the same services.openssh.enable (see render_software).
    sshd = bool(((doc.get("network", {}) or {}).get("ssh", {}) or {}).get("enabled")) \
        or bool({"sshd", "ssh"} & set(((doc.get("software", {}) or {})
                                       .get("services", {}) or {}).get("enable", []) or []))
    for user in doc.get("users", []) or []:
        name = user["name"]
        out.append(f"  users.users.{name} = {{")
        if name != "root":
            out.append("    isNormalUser = true;")
        if user.get("uid") is not None:
            out.append(f"    uid = {user['uid']};")
        if user.get("comment"):
            out.append(f"    description = {nix_str(user['comment'])};")
        groups = list(user.get("groups", []))
        if user.get("admin") and "wheel" not in groups:
            groups.insert(0, "wheel")
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
            refuse(f"user '{name}': shell {shell!r} is an absolute path "
                   "that does not exist on NixOS outside the store")
        elif shell:
            refuse(f"user '{name}': shell {shell!r} has no pkgs attribute")
        out.append("  };")
        if user.get("sudo") == "nopasswd":
            nopasswd.append(name)
        if shell in ("zsh", "fish"):
            out.append(f"  programs.{shell}.enable = true;")
    if nopasswd:
        # Per account, not per wheel. The old line was
        # `security.sudo.wheelNeedsPassword = false`, which hands passwordless
        # root to every member of wheel — including accounts the document
        # created with `admin: true` and no `sudo:` key at all, and including
        # any account added later. security.sudo.extraRules names the user
        # (security/sudo.nix:90-205); our definitions carry the default
        # priority 1000 and so land after the built-in wheel rule at
        # mkOrder 600 (sudo.nix:252-264), which is what makes them win —
        # sudoers takes the last matching rule.
        out.append("  security.sudo.extraRules = [")
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

    if system.get("hostname"):
        out.append(f"  networking.hostName = {nix_str(system['hostname'])};")
    if system.get("domain"):
        out.append(f"  networking.domain = {nix_str(system['domain'])};")
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

    if mirror_url := (doc.get("mirror", {}) or {}).get("url"):
        out.append(f"  nix.settings.substituters = [ {nix_str(mirror_url)} ];")
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
    if swap.get("zram"):
        out.append("  zramSwap.enable = true;")
    if swap.get("file"):
        size = swap["file"]["size"]
        gib = int(size[:-3]) if size.endswith("GiB") else 4
        out.append(f"  swapDevices = [ {{ device = {nix_str(swap['file']['path'])}; size = {gib * 1024}; }} ];")

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
    check_mirror(doc, {"url"})
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
    check_script_fields(doc, honors_chroot=True)
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
            statement of fact instead of a promise.
            """
            for content in host_stage_bodies(doc, stage):
                print(f"running scripts.{stage} on the installer host")
                subprocess.run(content, shell=True, check=False)

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
