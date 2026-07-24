#!/usr/bin/env python3
"""lis2autoinstall — translate a LIS document into Ubuntu autoinstall seed files.

Usage: lis2autoinstall.py FILE.lis.json [--out DIR] [--strict]

Writes a cloud-init NoCloud seed into DIR (default '.'):
  user-data   — `#cloud-config` with the `autoinstall:` section (subiquity)
  meta-data   — minimal NoCloud metadata

Put both files on a volume labeled CIDATA (or build one with
`cloud-localds seed.img user-data meta-data`) and boot the Ubuntu Server
installer with it.

Best-effort: core intent that autoinstall cannot express is reported as a
warning; with --strict any dropped intent exits non-zero (SPEC §2.3).
"""

import argparse
import json
import pathlib
import sys

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"warning: {msg}", file=sys.stderr)


def size_bytes(size: str) -> int | str:
    """LIS size → curtin size (bytes, or -1 for `rest`)."""
    if size == "rest":
        return -1
    if size.endswith("%"):
        warn(f"percent size {size!r} approximated as -1 (remaining space)")
        return -1
    units = {"MiB": 1 << 20, "GiB": 1 << 30, "TiB": 1 << 40}
    for unit, factor in units.items():
        if size.endswith(unit):
            return int(size[: -len(unit)]) * factor
    raise ValueError(f"unparseable size: {size}")


def storage_config(doc: dict) -> list | None:
    """LIS storage → curtin action list (disk/partition/format/mount + LVM)."""
    storage = doc.get("storage", {}) or {}
    target = doc.get("target", {}) or {}
    disks = {d["id"]: (d.get("match", {}) or {}).get("path") for d in target.get("disks", [])}
    wipe = bool(storage.get("wipe", False))

    config: list[dict] = []
    disk_action_ids: dict[str, str] = {}
    part_action_ids: dict[str, str] = {}  # LIS partition id -> curtin action id
    first_disk_done = False

    for handle, path in disks.items():
        if not path:
            warn(f"disk '{handle}' has no match.path — curtin needs a device path; "
                 "using match rules is not expressible in autoinstall")
            continue
        action_id = f"disk-{handle}"
        disk_action_ids[handle] = action_id
        action = {
            "type": "disk",
            "id": action_id,
            "path": path,
            "ptable": "gpt",
            "wipe": "superblock-recursive" if wipe else "preserve",
        }
        if not first_disk_done:
            action["grub_device"] = False
            first_disk_done = True
        config.append(action)

    fmt_n = 0

    def format_and_mount(volume_id: str, fs: str | None, mountpoint: str | None) -> None:
        nonlocal fmt_n
        if fs in (None, "none"):
            return
        fmt_n += 1
        fstype = {"vfat": "fat32"}.get(fs, fs)
        fmt_id = f"fmt-{fmt_n}"
        config.append({"type": "format", "id": fmt_id, "volume": volume_id, "fstype": fstype})
        if fs == "swap":
            return
        if mountpoint:
            config.append({"type": "mount", "id": f"mnt-{fmt_n}", "device": fmt_id,
                           "path": mountpoint})

    for i, part in enumerate(storage.get("partitions", [])):
        disk_action = disk_action_ids.get(part.get("disk"))
        if not disk_action:
            continue
        if part.get("existing"):
            warn("partition adoption ('existing') is not translated "
                 "(curtin preserve flows differ); skipping that entry")
            continue
        role = part.get("role")
        action_id = part.get("id") or f"part-auto-{i}"
        part_action_ids[action_id] = action_id
        action = {
            "type": "partition",
            "id": action_id,
            "device": disk_action,
            "size": size_bytes(part.get("size", "rest")),
            "wipe": "superblock",
        }
        if role == "esp":
            action["flag"] = "boot"
            action["grub_device"] = True
        config.append(action)
        fs = part.get("fs") or {"esp": "vfat", "swap": "swap", "root": "btrfs"}.get(role)
        mountpoint = part.get("mountpoint") or {
            "esp": "/boot/efi", "root": "/"}.get(role)
        if part.get("subvolumes"):
            warn(f"btrfs subvolumes on {action_id} are not expressible in curtin; "
                 "flattened to a plain btrfs filesystem (recreate them in late-commands)")
        format_and_mount(action_id, fs, mountpoint)

    for group in storage.get("lvm", []) or []:
        devices = [part_action_ids[d] for d in group.get("devices", [])
                   if d in part_action_ids]
        missing = [d for d in group.get("devices", []) if d not in part_action_ids]
        for d in missing:
            warn(f"lvm '{group['name']}' device '{d}' does not resolve to a partition")
        if not devices:
            continue
        vg_id = f"vg-{group['name']}"
        config.append({"type": "lvm_volgroup", "id": vg_id, "name": group["name"],
                       "devices": devices})
        for vol in group.get("volumes", []):
            lv_id = f"lv-{group['name']}-{vol['name']}"
            config.append({"type": "lvm_partition", "id": lv_id, "name": vol["name"],
                           "volgroup": vg_id, "size": size_bytes(vol.get("size", "rest"))})
            if vol.get("subvolumes"):
                warn(f"btrfs subvolumes on {lv_id} flattened (curtin limitation)")
            format_and_mount(lv_id, vol.get("fs"), vol.get("mountpoint"))

    if storage.get("raid"):
        warn("raid arrays are not translated in this proof-of-concept")
    if (storage.get("swap", {}) or {}).get("zram"):
        warn("zram swap is not an autoinstall concept (configure via user-data)")

    return config or None


def translate(doc: dict) -> dict:
    system = doc.get("system", {}) or {}
    boot = doc.get("boot", {}) or {}
    software = doc.get("software", {}) or {}
    network = doc.get("network", {}) or {}
    scripts = doc.get("scripts", {}) or {}
    installer = doc.get("installer", {}) or {}
    drivers = doc.get("drivers", {}) or {}
    keymap = system.get("keymap", {}) or {}

    users = [u for u in doc.get("users", []) if u["name"] != "root"]
    primary = users[0] if users else None

    auto: dict = {
        "version": 1,
        "locale": system.get("locale", "en_US.UTF-8"),
        "timezone": system.get("timezone", "UTC"),
        "keyboard": {"layout": keymap.get("layout") or keymap.get("console") or "us",
                     "variant": keymap.get("variant", "")},
    }

    if primary:
        identity = {
            "hostname": system.get("hostname", "ubuntu"),
            "username": primary["name"],
        }
        # autoinstall takes crypt(3) hashes directly — no plaintext needed.
        if hash_ := (primary.get("password") or {}).get("hash"):
            identity["password"] = hash_
        else:
            warn(f"user '{primary['name']}' has no password hash; "
                 "account will be locked until one is set")
            identity["password"] = "!"
        auto["identity"] = identity
    else:
        auto["identity"] = {"hostname": system.get("hostname", "ubuntu"),
                            "username": "ubuntu", "password": "!"}
        warn("no non-root users in document; created locked 'ubuntu' user")

    ssh = network.get("ssh", {}) or {}
    auto["ssh"] = {
        "install-server": bool(ssh.get("enabled", False)),
        "allow-pw": bool(ssh.get("password_auth", False)),
        "authorized-keys": (primary or {}).get("ssh_authorized_keys", []),
    }

    if sc := storage_config(doc):
        auto["storage"] = {"config": sc}
    
    # Packages + software.apps
    packages = list(software.get("packages", []))
    for app in software.get("apps", []):
        if isinstance(app, str):
            packages.append(app)
        elif isinstance(app, dict):
            if name := (app.get("package") or app.get("name")):
                packages.append(name)
            if fp := app.get("flatpak"):
                if "flatpak" not in software:
                    software["flatpak"] = []
                software["flatpak"].append(fp)

    role = software.get("role", "")
    role_packages = {"desktop:gnome": "ubuntu-desktop", "desktop:kde": "kubuntu-desktop",
                     "desktop:xfce": "xubuntu-desktop"}
    if role in role_packages:
        packages.append(role_packages[role])
    elif role.startswith("desktop:"):
        warn(f"role {role!r} has no ubuntu meta-package mapping")
    if packages:
        auto["packages"] = packages
    if software.get("exclude"):
        warn("software.exclude is not expressible in autoinstall")
    if snaps := software.get("snap"):
        auto["snaps"] = [{"name": s["name"],
                          **({"channel": s["channel"]} if s.get("channel") else {}),
                          **({"classic": True} if s.get("classic") else {})}
                         for s in snaps]
    if software.get("flatpak"):
        warn("flatpak apps moved to first-boot runcmd (no autoinstall support)")

    variant = (boot.get("kernel", {}) or {}).get("variant", "default")
    if variant not in ("default", None):
        kernel_map = {"lts": "linux-generic", "hardened": None, "realtime": None, "zen": None}
        pkg = kernel_map.get(variant)
        if pkg:
            auto["kernel"] = {"package": pkg}
        else:
            warn(f"kernel variant {variant!r} has no ubuntu package; using default")
    if boot.get("loader") == "grub" or boot.get("loader") in (None, "auto", "systemd-boot"):
        if boot.get("loader") == "systemd-boot":
            warn("ubuntu server installs grub; boot.loader systemd-boot dropped")

    if drivers.get("gpu") in ("nvidia", "nvidia-open"):
        auto["drivers"] = {"install": True}

    if proxy := (doc.get("proxy", {}) or {}).get("http"):
        auto["proxy"] = proxy
    if mirror_url := (doc.get("mirror", {}) or {}).get("url"):
        auto["apt"] = {"primary": [{"arches": ["default"], "uri": mirror_url}]}

    # 1. Early / Pre-install commands
    early = []
    for stage in ("pre_install", "pre", "post_storage"):
        for s in scripts.get(stage, []):
            if c := s.get("content"):
                early.append(c)
    if early:
        auto["early-commands"] = early

    # 2. Late / Post-install commands
    late = []
    for stage in ("post_install", "post", "pre_reboot", "on_success"):
        for s in scripts.get(stage, []):
            if c := s.get("content"):
                if s.get("chroot"):
                    late.append(f"curtin in-target -- sh -c {json.dumps(c)}")
                else:
                    late.append(c)

    # Extra users beyond the primary: created in-target.
    for user in users[1:]:
        hash_ = (user.get("password") or {}).get("hash", "!")
        groups = ",".join(user.get("groups", []))
        cmd = (f"curtin in-target -- useradd -m -p '{hash_}'"
               + (f" -G '{groups}'" if groups else "")
               + (" -s /usr/bin/" + user["shell"] if user.get("shell") else "")
               + f" {user['name']}")
        late.append(cmd)

    # User post_install scripts
    for user in users:
        if user_scripts := user.get("scripts", {}):
            for s in user_scripts.get("post_install", []):
                if c := s.get("content"):
                    late.append(f"curtin in-target -- su - {user['name']} -c {json.dumps(c)}")

    if late:
        auto["late-commands"] = late

    if fin := installer.get("on_finish"):
        if fin in ("reboot", "poweroff"):
            auto["shutdown"] = fin
    if interactive := installer.get("interactive"):
        auto["interactive-sections"] = interactive

    # First-boot work rides on cloud-init proper, next to autoinstall.
    cloud_config: dict = {"autoinstall": auto}
    runcmd = [s["content"] for stage in ("firstboot",) for s in scripts.get(stage, []) if s.get("content")]
    
    # User firstboot scripts
    for user in users:
        if user_scripts := user.get("scripts", {}):
            for s in user_scripts.get("firstboot", []):
                if c := s.get("content"):
                    runcmd.append(f"su - {user['name']} -c {json.dumps(c)}")

    for app in software.get("flatpak", []):
        runcmd.append(f"flatpak install -y flathub {app}")
    if runcmd:
        # user-data for the *installed* system.
        auto["user-data"] = {"runcmd": runcmd}

    if doc.get("keys"):
        warn("hardware key matrix (keys[]) not translated into autoinstall; requires subiquity cryptenroll hooks")
    for section in ("registration",):
        if doc.get(section):
            warn(f"section '{section}' not translated (use ubuntu-pro token via user-data)")

    return cloud_config


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


def scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v)  # JSON string quoting is valid YAML


def main() -> int:
    ap = argparse.ArgumentParser(description="Translate LIS document to Ubuntu autoinstall and optionally apply directly.")
    ap.add_argument("file", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("."))
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any core intent was dropped")
    ap.add_argument("--apply", "-a", action="store_true",
                    help="directly copy autoinstall seed to live system subiquity path")
    args = ap.parse_args()

    doc = json.loads(args.file.read_text())
    if not str(doc.get("lis", "")).startswith("0.1."):
        sys.exit(f"unsupported LIS version: {doc.get('lis')!r}")

    cloud_config = translate(doc)
    args.out.mkdir(parents=True, exist_ok=True)
    user_data_file = args.out / "user-data"
    meta_data_file = args.out / "meta-data"
    
    user_data = "#cloud-config\n" + to_yaml(cloud_config)
    user_data_file.write_text(user_data)
    hostname = (doc.get("system", {}) or {}).get("hostname", "ubuntu")
    meta_data_file.write_text(f"instance-id: lis-{hostname}\nlocal-hostname: {hostname}\n")
    print(f"wrote {user_data_file} and {meta_data_file} ({len(WARNINGS)} warning(s))")

    if args.apply:
        import shutil
        import subprocess
        print("applying autoinstall configuration to live Subiquity environment...")
        subiquity_dir = pathlib.Path("/var/log/autoinstall")
        if subiquity_dir.exists():
            shutil.copy(user_data_file, subiquity_dir / "user-data")
            shutil.copy(meta_data_file, subiquity_dir / "meta-data")
            print("copied seed files to /var/log/autoinstall/")
        if shutil.which("subiquity"):
            res = subprocess.run(["subiquity", "--autoinstall"])
            return res.returncode

    if args.strict and WARNINGS:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
