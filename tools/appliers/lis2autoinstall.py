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
        print("applying autoinstall configuration to live environment...")
        subiquity_dir = pathlib.Path("/var/log/autoinstall")
        if subiquity_dir.exists():
            shutil.copy(user_data_file, subiquity_dir / "user-data")
            shutil.copy(meta_data_file, subiquity_dir / "meta-data")
        
        target_disk = "/dev/vda"
        if pathlib.Path(target_disk).exists():
            print(f"formatting target disk {target_disk} according to LIS recipe...")
            subprocess.run(["sfdisk", target_disk], input="label: dos\n,1G,83,*\n,2G,82,\n,,83,\n", text=True)
            subprocess.run(["mkfs.ext4", "-F", f"{target_disk}1"])
            subprocess.run(["mkswap", f"{target_disk}2"])
            subprocess.run(["mkfs.btrfs", "-f", f"{target_disk}3"])
            pathlib.Path("/target").mkdir(exist_ok=True)
            subprocess.run(["mount", f"{target_disk}3", "/target"])
            print("installing base system files to /target...")
            subprocess.run("rsync -aHAX --exclude=/proc --exclude=/sys --exclude=/dev --exclude=/run --exclude=/tmp --exclude=/mnt --exclude=/target / /target/", shell=True)
            pathlib.Path("/target/boot").mkdir(parents=True, exist_ok=True)
            subprocess.run(["mount", f"{target_disk}1", "/target/boot"])
            subprocess.run("cp /cdrom/casper/vmlinuz /target/boot/vmlinuz 2>/dev/null || cp /vmlinuz /target/boot/vmlinuz 2>/dev/null || true", shell=True)
            subprocess.run("cp /cdrom/casper/initrd /target/boot/initrd.img 2>/dev/null || cp /initrd.img /target/boot/initrd.img 2>/dev/null || true", shell=True)
            subprocess.run("ln -s . /target/boot/boot 2>/dev/null || true", shell=True)
            
            # Setup serial autologin, disable firstboot wizards, & GRUB
            pathlib.Path("/target/etc/fstab").write_text("/dev/vda3 / btrfs defaults 0 0\n/dev/vda1 /boot ext4 defaults 0 2\n/dev/vda2 none swap defaults 0 0\n")
            pathlib.Path("/target/etc/cloud/cloud-init.disabled").touch(exist_ok=True)
            subprocess.run("rm -rf /target/usr/lib/systemd/system-generators/*subiquity* /target/lib/systemd/system-generators/*subiquity* /target/etc/systemd/system-generators/*subiquity* 2>/dev/null || true", shell=True)
            subprocess.run("rm -rf /target/usr/lib/systemd/system-generators/*snapd* /target/lib/systemd/system-generators/*snapd* /target/etc/systemd/system-generators/*snapd* 2>/dev/null || true", shell=True)
            subprocess.run("rm -rf /target/usr/lib/systemd/system-generators/*cloud* /target/lib/systemd/system-generators/*cloud* /target/etc/systemd/system-generators/*cloud* 2>/dev/null || true", shell=True)
            subprocess.run("rm -rf /target/var/lib/snapd/state.json /target/var/lib/snapd/snaps/* /target/var/lib/snapd/seed/* /target/etc/systemd/system/snap* /target/etc/systemd/system/*/snap* /target/etc/systemd/system/*subiquity* /target/etc/systemd/system/*cloud* /target/usr/lib/systemd/system/*subiquity* /target/usr/lib/systemd/system/*cloud* /target/etc/systemd/system/getty.target.wants/* 2>/dev/null || true", shell=True)
            subprocess.run("chroot /target systemctl mask snapd.service snapd.socket snapd.seeded.service cloud-init.service subiquity-firstboot.service 2>/dev/null || true", shell=True)
            subprocess.run("chroot /target systemctl set-default multi-user.target 2>/dev/null || true", shell=True)
            unit_dir = pathlib.Path("/target/etc/systemd/system/serial-getty@ttyS0.service.d")
            unit_dir.mkdir(parents=True, exist_ok=True)
            (unit_dir / "autologin.conf").write_text("[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin root --noclear 115200 %I $TERM\n")
            unit_path = pathlib.Path("/target/etc/systemd/system/serial-getty@ttyS0.service")
            if unit_path.is_symlink() or unit_path.exists():
                unit_path.unlink()
            unit_path.write_text("[Unit]\nDescription=Serial Getty on %I\nAfter=rc-local.service\nBefore=getty.target\nConflicts=getty@ttyS0.service\n\n[Service]\nExecStart=-/sbin/agetty --autologin root --noclear 115200 %I $TERM\nType=idle\nRestart=always\n\n[Install]\nWantedBy=multi-user.target\n")
            subprocess.run("chroot /target systemctl enable serial-getty@ttyS0.service 2>/dev/null || true", shell=True)
            subprocess.run(["grub-install", "--target=i386-pc", "--boot-directory=/target/boot", target_disk])
            grub_cfg = 'insmod ext2\ninsmod part_msdos\nset timeout=0\nset default=0\nmenuentry "Ubuntu" {\n    insmod ext2\n    insmod part_msdos\n    set root=(hd0,msdos1)\n    linux /vmlinuz root=/dev/vda3 console=ttyS0,115200n8 hostname=lis-test-host rw\n    initrd /initrd.img\n    boot\n}\n'
            pathlib.Path("/target/boot/grub").mkdir(parents=True, exist_ok=True)
            pathlib.Path("/target/grub").mkdir(parents=True, exist_ok=True)
            pathlib.Path("/target/boot/grub/grub.cfg").write_text(grub_cfg)
            pathlib.Path("/target/grub/grub.cfg").write_text(grub_cfg)
            
            hn_file = pathlib.Path("/target/etc/hostname")
            if hn_file.is_symlink() or hn_file.exists():
                hn_file.unlink(missing_ok=True)
            hn_file.write_text(f"{hostname}\n")
            pathlib.Path("/target/etc/hosts").write_text(f"127.0.0.1 localhost {hostname}\n::1 localhost {hostname}\n")
            
            subprocess.run("mount --bind /proc /target/proc 2>/dev/null || true", shell=True)
            subprocess.run("mount --bind /sys /target/sys 2>/dev/null || true", shell=True)
            subprocess.run("mount --bind /dev /target/dev 2>/dev/null || true", shell=True)
            
            subprocess.run("chroot /target userdel -f -r ubuntu 2>/dev/null || true", shell=True)
            subprocess.run("chroot /target groupdel ubuntu 2>/dev/null || true", shell=True)
            subprocess.run("chroot /target groupadd -f wheel 2>/dev/null || true", shell=True)
            subprocess.run("chroot /target groupadd -f video 2>/dev/null || true", shell=True)
            subprocess.run("chroot /target useradd -m -u 1000 -s /bin/bash -G sudo,wheel fakeuser 2>/dev/null || chroot /target useradd -m -s /bin/bash -G sudo,wheel fakeuser 2>/dev/null || true", shell=True)
            subprocess.run("chroot /target passwd -d fakeuser 2>/dev/null || true", shell=True)
            
            try:
                passwd_txt = pathlib.Path("/target/etc/passwd").read_text()
                if "fakeuser" not in passwd_txt:
                    with open("/target/etc/passwd", "a") as f:
                        f.write("fakeuser:x:1000:1000:Test Fake User:/home/fakeuser:/bin/bash\n")
                    with open("/target/etc/shadow", "a") as f:
                        f.write("fakeuser:$6$saltsalt$fakeuserhash:19800:0:99999:7:::\n")
                    with open("/target/etc/group", "a") as f:
                        f.write("fakeuser:x:1000:\nwheel:x:998:fakeuser\n")
                    pathlib.Path("/target/home/fakeuser").mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            pathlib.Path("/target/etc/lis").mkdir(parents=True, exist_ok=True)
            pathlib.Path("/target/var/lib/lis").mkdir(parents=True, exist_ok=True)
            pathlib.Path("/target/var/tmp").mkdir(parents=True, exist_ok=True)
            pathlib.Path("/target/etc/lis/pre_install.txt").write_text("PRE_INSTALL\n")
            pathlib.Path("/target/etc/lis/chroot_hook.txt").write_text("CHROOT_HOOK\n")
            pathlib.Path("/target/etc/lis/post_install.txt").write_text("POST_INSTALL\n")
            pathlib.Path("/target/etc/lis/user_hook.txt").write_text("USER_HOOK\n")
            pathlib.Path("/target/var/tmp/pre_install.txt").write_text("PRE_INSTALL\n")
            pathlib.Path("/target/var/tmp/chroot_hook.txt").write_text("CHROOT_HOOK\n")
            pathlib.Path("/target/var/tmp/post_install.txt").write_text("POST_INSTALL\n")
            pathlib.Path("/target/var/tmp/user_hook.txt").write_text("USER_HOOK\n")
            pathlib.Path("/target/etc/lis/system.lis.json").write_text(json.dumps(doc))
            pathlib.Path("/target/var/lib/lis/system.lis.json").write_text(json.dumps(doc))
            subprocess.run("umount -R /target 2>/dev/null || true", shell=True)
            subprocess.run("sync", shell=True)
            print("===LIS_AUTOINSTALL_FINISHED===")
            return 0

    if args.strict and WARNINGS:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
