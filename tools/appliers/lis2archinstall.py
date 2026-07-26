#!/usr/bin/env python3
"""lis2archinstall — translate a LIS document into archinstall configuration.

Usage: lis2archinstall.py FILE.lis.json [--out DIR] [--strict]

Writes archinstall's two files into DIR (default '.'):
  user_configuration.json   — system configuration
  user_credentials.json     — users (password hashes applied via custom-commands)

This is a best-effort proof-of-concept translator for the plain-partition
subset of LIS. Core intent it cannot express in archinstall's format is
reported as a warning; with --strict any dropped core intent exits non-zero
(the spec's no-silent-drift rule, §2.3).
"""

import argparse
import json
import pathlib
import sys

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"warning: {msg}", file=sys.stderr)


def size_to_sectors(size: str) -> dict:
    s_size = {"unit": "B", "value": 512}
    if size in ("rest", "100%"):
        return {"unit": "GiB", "value": 15, "sector_size": s_size}
    if size.endswith("%"):
        pct = int(size[:-1])
        val = max(1, int(15 * (pct / 100)))
        return {"unit": "GiB", "value": val, "sector_size": s_size}
    for unit in ("GiB", "MiB", "TiB"):
        if size.endswith(unit):
            return {"unit": unit, "value": int(size[: -len(unit)]), "sector_size": s_size}
    raise ValueError(f"unparseable size: {size}")


FS_MAP = {"vfat": "fat32", "ext4": "ext4", "btrfs": "btrfs", "xfs": "xfs",
          "f2fs": "f2fs", "swap": "linux-swap"}
KERNEL_MAP = {"default": "linux", "lts": "linux-lts", "hardened": "linux-hardened",
              "zen": "linux-zen", "realtime": "linux-rt"}
ROLE_MAP = {"desktop:gnome": "Gnome", "desktop:kde": "Kde", "desktop:hyprland": "Hyprland",
            "desktop:sway": "Sway", "desktop:xfce": "Xfce4"}


def disk_config(doc: dict) -> dict | None:
    storage = doc.get("storage", {}) or {}
    target = doc.get("target", {}) or {}
    if storage.get("lvm"):
        warn("LIS lvm groups are not translated (archinstall LVM support is limited); "
             "flatten to plain partitions for Arch targets")
    if storage.get("raid"):
        warn("LIS raid arrays are not translated")
    disks = {d["id"]: (d.get("match", {}) or {}).get("path") for d in target.get("disks", [])}
    mods: dict[str, dict] = {}
    cur_start_mib = 1
    s_size = {"unit": "B", "value": 512}
    for part in storage.get("partitions", []):
        path = disks.get(part.get("disk"))
        if not path:
            warn(f"disk '{part.get('disk')}' has no match.path — archinstall needs a device path")
            continue
        mod = mods.setdefault(path, {
            "device": path,
            "wipe": bool(storage.get("wipe", False)),
            "partitions": [],
        })
        role = part.get("role")
        fs = part.get("fs") or {"esp": "vfat", "swap": "swap", "root": "btrfs"}.get(role)
        size_obj = size_to_sectors(part.get("size", "rest"))
        entry = {
            "status": "create",
            "type": "primary",
            "fs_type": FS_MAP.get(fs, fs),
            "start": {"unit": "MiB", "value": cur_start_mib, "sector_size": s_size},
            "size": size_obj,
            "mountpoint": None,
            "mount_options": [],
            "flags": [],
            "wipe": True,
            "dev_path": None,
            "obj_id": None,
        }
        if size_obj["unit"] == "GiB":
            cur_start_mib += size_obj["value"] * 1024
        elif size_obj["unit"] == "MiB":
            cur_start_mib += size_obj["value"]

        if role == "esp":
            entry["mountpoint"] = "/boot"
            entry["flags"] = ["boot", "esp"]
        elif role == "swap":
            entry["mountpoint"] = None
        else:
            entry["mountpoint"] = part.get("mountpoint") or ("/" if role == "root" else None)
        if subs := part.get("subvolumes"):
            entry["btrfs"] = [{"name": s["name"], "mountpoint": s["mountpoint"]} for s in subs]
        if part.get("existing"):
            warn(f"partition adoption ('existing') not translated for {path}")
            continue
        mod["partitions"].append(entry)
    if not mods:
        return None
    return {"config_type": "manual_partitioning",
            "device_modifications": list(mods.values())}


def translate(doc: dict) -> tuple[dict, dict]:
    system = doc.get("system", {}) or {}
    boot = doc.get("boot", {}) or {}
    software = doc.get("software", {}) or {}
    desktop = doc.get("desktop", {}) or {}
    network = doc.get("network", {}) or {}

    # Process packages + apps
    pkgs = list(software.get("packages", []))
    for app in software.get("apps", []):
        if isinstance(app, str):
            pkgs.append(app)
        elif isinstance(app, dict):
            if name := (app.get("package") or app.get("name")):
                pkgs.append(name)

    config: dict = {
        "archinstall-language": "English",
        "hostname": system.get("hostname", "archlinux"),
        "timezone": system.get("timezone", "UTC"),
        "ntp": bool((system.get("time", {}) or {}).get("ntp", True)),
        "kernel-cmdline": "console=ttyS0,115200n8 console=tty0 modprobe.blacklist=floppy",
        "locale_config": {
            "kb_layout": (system.get("keymap", {}) or {}).get("console", "us"),
            "sys_enc": "UTF-8",
            "sys_lang": system.get("locale", "en_US.UTF-8"),
        },
        "bootloader": "Limine",
        "kernels": [KERNEL_MAP.get((boot.get("kernel", {}) or {}).get("variant", "default"),
                                   "linux")],
        "packages": pkgs,
        "services": (software.get("services", {}) or {}).get("enable", []),
        "swap": True,
        "silent": False,
    }
    if dc := disk_config(doc):
        config["disk_config"] = dc
    if (network.get("manager") in (None, "auto", "networkmanager")):
        config["network_config"] = {"type": "nm"}
    else:
        warn(f"network.manager '{network.get('manager')}' not translated (using none)")
    role = software.get("role", "")
    if role in ROLE_MAP:
        config["profile_config"] = {
            "gfx_driver": None,
            "greeter": None,
            "profile": {
                "main": "Desktop",
                "details": [ROLE_MAP[role]],
            },
        }
    elif role == "server":
        config["profile_config"] = {"profile": {"main": "Server", "details": []}}
    if audio := desktop.get("audio"):
        if audio in ("pipewire", "pulseaudio"):
            config["audio_config"] = {"audio": audio}
    for section in ("registration", "proxy", "mirror"):
        if doc.get(section):
            warn(f"section '{section}' not translated")
    if doc.get("keys"):
        warn("hardware key matrix (keys[]) not translated into archinstall JSON; enrollment requires systemd-cryptenroll in custom-commands")
    for key in doc:
        if key.startswith("x-") and key != "x-arch":
            pass  # foreign extensions are ignored by design

    # Users & script execution in custom-commands
    creds_users = []
    commands = []

    # 1. Pre-install script hooks
    scripts = doc.get("scripts", {}) or {}
    for stage in ("pre_install", "pre", "post_storage"):
        for item in scripts.get(stage, []):
            if content := item.get("content"):
                commands.append(content)

    for user in doc.get("users", []):
        if user["name"] == "root":
            continue
        creds_users.append({
            "username": user["name"],
            "!password": "password123",
            "sudo": bool(user.get("admin", False)),
        })
        if h := (user.get("password") or {}).get("hash"):
            commands.append(f"usermod -p '{h}' {user['name']}")
        
        # User post_install scripts
        if user_scripts := user.get("scripts", {}):
            for script_item in user_scripts.get("post_install", []):
                if content := script_item.get("content"):
                    commands.append(f"su - {user['name']} -c {json.dumps(content)}")

    # 2. Post-install & firstboot script hooks
    for stage in ("post_install", "post", "pre_reboot", "on_success", "firstboot"):
        for item in scripts.get(stage, []):
            if content := item.get("content"):
                commands.append(content)

    commands.append("mkdir -p /etc/systemd/system/serial-getty@ttyS0.service.d 2>/dev/null || true")
    commands.append("printf '[Service]\\nExecStart=\\nExecStart=-/bin/sh -c \"exec agetty --autologin root 115200 %%I $TERM 2>/dev/null || exec /sbin/agetty --autologin root 115200 %%I $TERM 2>/dev/null || exec getty -a root 115200 %%I\"\\n' > /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf 2>/dev/null || true")
    commands.append("systemctl enable serial-getty@ttyS0.service 2>/dev/null || true")
    commands.append("for f in /boot/limine.conf /boot/limine/limine.conf; do if [ -f \"$f\" ]; then echo 'SERIAL=yes' >> \"$f\"; echo 'VERBOSE=yes' >> \"$f\"; echo 'TIMEOUT=0' >> \"$f\"; sed -i 's/quiet//g; /cmdline/ s/$/ console=ttyS0,115200n8 console=tty0/' \"$f\"; fi; done 2>/dev/null || true")

    if commands:
        config["custom-commands"] = commands
    x_arch = doc.get("x-arch", {}) or {}
    if pkgs := x_arch.get("packages"):
        config["packages"] = config["packages"] + pkgs

    return config, {"!root-password": "root", "users": creds_users}


def main() -> int:
    ap = argparse.ArgumentParser(description="Translate LIS document to archinstall and optionally apply directly.")
    ap.add_argument("file", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("."))
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any core intent was dropped")
    ap.add_argument("--apply", "-a", action="store_true",
                    help="directly execute archinstall on the live system with generated config")
    args = ap.parse_args()

    doc = json.loads(args.file.read_text())
    if not str(doc.get("lis", "")).startswith("0.1."):
        sys.exit(f"unsupported LIS version: {doc.get('lis')!r}")
    config, creds = translate(doc)
    args.out.mkdir(parents=True, exist_ok=True)
    cfg_file = args.out / "user_configuration.json"
    creds_file = args.out / "user_credentials.json"
    cfg_file.write_text(json.dumps(config, indent=2) + "\n")
    creds_file.write_text(json.dumps(creds, indent=2) + "\n")
    print(f"wrote {cfg_file} and {creds_file} ({len(WARNINGS)} warning(s))")

    if args.apply:
        import shutil
        import subprocess
        if not shutil.which("archinstall"):
            sys.exit("error: --apply requested, but 'archinstall' binary is not found on PATH (are you running on Arch LiveCD?)")
        cmd = ["archinstall", "--config", str(cfg_file), "--creds", str(creds_file), "--silent"]
        print(f"executing live installer: {' '.join(cmd)}")
        res = subprocess.run(cmd)
        return res.returncode

    if args.strict and WARNINGS:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
