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

from lis_common import (add_common_args, check_firmware, check_version, enforce,
                        load_doc, refuse, report, role_fs, role_mountpoint, warn)

SECTOR = {"unit": "B", "value": 512}


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
    """LIS size → archinstall Size object. 'rest' is resolved by archinstall itself."""
    if size in ("rest", "100%"):
        return {"unit": "Percent", "value": 100, "sector_size": SECTOR}
    if size.endswith("%"):
        return {"unit": "Percent", "value": int(size[:-1]), "sector_size": SECTOR}
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
    if storage.get("encryption"):
        ids = ", ".join(c["id"] for c in storage["encryption"])
        refuse(f"storage.encryption ({ids}): archinstall's LUKS support needs the "
               "passphrase in its credentials file, which SPEC §2.4 forbids the "
               "document from carrying")

    disks = {}
    for disk in target.get("disks", []):
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
    pv_ids: dict[str, str] = {}          # LIS partition handle → archinstall obj_id
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
        starts[path] = cursor + start_of(part.get("size", "rest"))

        if role == "esp":
            entry["mountpoint"] = part.get("mountpoint") or "/boot"
            entry["flags"] = ["boot", "esp"]
        elif role != "swap" and handle not in consumed:
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
    storage = doc.get("storage", {}) or {}
    software = doc.get("software", {}) or {}
    desktop = doc.get("desktop", {}) or {}
    network = doc.get("network", {}) or {}
    scripts = doc.get("scripts", {}) or {}

    pkgs = list(software.get("packages", []))
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
        "archinstall-language": "English",
        "hostname": system.get("hostname", "archlinux"),
        "timezone": system.get("timezone", "UTC"),
        "ntp": bool((system.get("time", {}) or {}).get("ntp", True)),
        "locale_config": {
            "kb_layout": (system.get("keymap", {}) or {}).get("console", "us"),
            "sys_enc": "UTF-8",
            "sys_lang": system.get("locale", "en_US.UTF-8"),
        },
        "bootloader": bootloader,
        "kernels": [KERNEL_MAP.get(variant, "linux")],
        "packages": pkgs,
        "services": (software.get("services", {}) or {}).get("enable", []),
        "swap": bool((storage.get("swap", {}) or {}).get("zram", True)),
        "silent": True,
    }
    if params := (boot.get("kernel", {}) or {}).get("params"):
        config["kernel-cmdline"] = " ".join(params)
    if dc := disk_config(doc):
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
    if doc.get("keys"):
        refuse("keys[] hardware-token enrollment is not expressible in an archinstall "
               "profile; enroll with systemd-cryptenroll from a post_install script")
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
        creds_users.append({
            "username": user["name"],
            "!password": None,
            "sudo": bool(user.get("admin", False)),
        })
        if h := password.get("hash"):
            commands.append(f"usermod -p '{h}' {user['name']}")
        elif password.get("locked"):
            commands.append(f"usermod -L {user['name']}")
        else:
            refuse(f"user '{user['name']}': no password hash and not marked locked "
                   "(SPEC §2.4 forbids inlining a plaintext password)")
        if groups := user.get("groups"):
            commands.append(f"usermod -aG {','.join(groups)} {user['name']}")
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

    for stage in ("post_install", "post", "pre_reboot", "on_success"):
        for item in scripts.get(stage, []):
            if content := item.get("content"):
                commands.append(content)

    if scripts.get("on_error"):
        refuse("scripts.on_error has no archinstall equivalent")

    for entry in doc.get("files", []) or []:
        commands.append(f"install -d {json.dumps(str(pathlib.PurePath(entry['path']).parent))}")
        commands.append(f"printf '%s' {json.dumps(entry['content'])} > "
                        f"{json.dumps(entry['path'])}")
        if mode := entry.get("mode"):
            commands.append(f"chmod {mode} {json.dumps(entry['path'])}")

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
    commands.append("install -d -m755 /var/lib/lis")
    commands.append(f"echo {b64(json.dumps(doc, separators=(',', ':')))} | base64 -d "
                    "> /var/lib/lis/system.lis.json")
    commands.append("chmod 600 /var/lib/lis/system.lis.json")

    if commands:
        config["custom-commands"] = commands
    x_arch = doc.get("x-arch", {}) or {}
    if extra := x_arch.get("packages"):
        config["packages"] = config["packages"] + extra

    root = next((u for u in doc.get("users", []) if u["name"] == "root"), None)
    creds = {"users": creds_users}
    if root:
        password = root.get("password") or {}
        if password.get("hash"):
            commands.append(f"usermod -p '{password['hash']}' root")
        elif password.get("locked"):
            commands.append("passwd -l root")
    return config, creds


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

    doc = load_doc(args.file)
    check_version(doc, args.file)
    check_firmware(doc)

    config, creds = translate(doc)
    args.out.mkdir(parents=True, exist_ok=True)
    cfg_file = args.out / "user_configuration.json"
    creds_file = args.out / "user_credentials.json"
    cfg_file.write_text(json.dumps(config, indent=2) + "\n")
    creds_file.write_text(json.dumps(creds, indent=2) + "\n")
    creds_file.chmod(0o600)
    report(cfg_file, creds_file)

    # Fail closed *before* touching the machine, not after.
    if status := enforce(args.strict):
        return status

    if args.apply:
        import shutil
        import subprocess
        if not shutil.which("archinstall"):
            sys.exit("error: --apply requested, but 'archinstall' is not on PATH "
                     "(are you running on the Arch live ISO?)")
        cmd = ["archinstall", "--config", str(cfg_file), "--creds", str(creds_file), "--silent"]
        print(f"executing native installer: {' '.join(cmd)}")
        return subprocess.run(cmd).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
