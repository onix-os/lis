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

from lis_common import (track, check_unread, registration_commands, enrollment_commands, luks_key_path, SEED_MOUNT, resolve_disk_paths, check_snapshots, match_selectors, system_commands, security_packages, file_commands, uid_commands, password_field, shell_packages, check_arch, check_script_fields,ALL_SECTIONS, add_common_args, check_firmware,
                        check_unhandled, check_section_fields, sudoers_commands, boot_timeout_commands, driver_packages,
                        check_boot_extras, check_keymap, check_version, enforce,
                        load_doc, refuse, report, role_fs, role_mountpoint, warn)

SECTOR = {"unit": "B", "value": 512}
UNIT_BYTES = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30, "TiB": 1 << 40}
# LIS partition handle → the obj_id archinstall knows it by.
PV_IDS: dict[str, str] = {}

# obj_ids whose size the document left open ('rest' / percent).
REST_SIZED: set[str] = set()


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


def disk_config(doc: dict) -> dict | None:
    """LIS storage → archinstall disk_config (partitions + LVM volume groups)."""
    storage = doc.get("storage", {}) or {}
    target = doc.get("target", {}) or {}

    if storage.get("raid"):
        names = ", ".join(a["name"] for a in storage["raid"])
        refuse(f"storage.raid ({names}): archinstall has no mdadm array vocabulary — "
               "build the array first and adopt it, or use an applier that supports RAID")
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

    consumed: set[str] = set()
    for group in storage.get("lvm", []) or []:
        consumed.update(group.get("devices", []))

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
            "fs_type": FS_MAP.get(fs, fs) if fs not in (None, "none") else None,
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


def lvm_config(storage: dict, pv_ids: dict[str, str]) -> dict | None:
    """LIS storage.lvm[] → archinstall's manual_lvm volume groups."""
    groups = storage.get("lvm", []) or []
    if not groups:
        return None
    vol_groups = []
    for group in groups:
        pvs, missing = [], []
        for handle in group.get("devices", []):
            (pvs if handle in pv_ids else missing).append(pv_ids.get(handle, handle))
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
            if subs := vol.get("subvolumes"):
                if fs != "btrfs":
                    refuse(f"lvm volume '{vol['name']}': subvolumes on a {fs} filesystem")
                entry["btrfs"] = [{"name": s["name"], "mountpoint": s["mountpoint"]}
                                  for s in subs]
            volumes.append(entry)
        vol_groups.append({"name": group["name"], "pvs": pvs, "lvm_volumes": volumes})
    if not vol_groups:
        return None
    return {"config_type": "manual_lvm", "vol_groups": vol_groups}


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
    if dc := disk_config(doc):
        # Schema read from archinstall itself (models/device.py DiskEncryption):
        # disk_config carries disk_encryption {encryption_type, partitions:
        # [obj_id], lvm_volumes: []}, and the passphrase travels separately in
        # the credentials file as `encryption_password`.
        encrypted = [c["over"] for c in (storage.get("encryption", []) or [])]
        obj_ids = [PV_IDS[h] for h in encrypted if h in PV_IDS]
        if obj_ids:
            dc["disk_encryption"] = {
                "encryption_type": "luks",
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
        refuse("network.firewall is not expressible in an archinstall profile")

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
        refuse("software.exclude has no archinstall equivalent (packages are additive)")

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
            refuse(f"users['{user['name']}'].dotfiles is not applied by this applier")
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
    commands += system_commands(doc, "arch")
    commands += boot_timeout_commands(
        doc, "arch", "systemd-boot" if bootloader == "Systemd-boot" else "grub")

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
            length = end - start_bytes
            if length <= 0:
                sys.exit(f"error: no space left on {device} for the 'rest' partition")
            part["size"] = {"unit": "B", "value": length, "sector_size": SECTOR}

    lvm = disk_config.get("lvm_config") or {}
    for group in lvm.get("vol_groups", []):
        for vol in group.get("lvm_volumes", []):
            if vol.get("obj_id") in REST_SIZED:
                # The volume group's free extents are not visible until the PVs
                # exist, so archinstall is asked for a whole-VG volume instead.
                vol["length"] = {"unit": "B", "value": 0, "sector_size": SECTOR}
                warn(f"lvm volume '{vol['name']}': 'rest' is applied as the "
                     "remaining volume group space")

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
    check_snapshots(doc, tools=frozenset(), boot_menu=False)
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
        resolve_rest_sizes(config)
        cfg_file.write_text(json.dumps(config, indent=2) + "\n")
        cmd = ["archinstall", "--config", str(cfg_file), "--creds", str(creds_file), "--silent"]
        print(f"executing native installer: {' '.join(cmd)}")
        return subprocess.run(cmd).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
