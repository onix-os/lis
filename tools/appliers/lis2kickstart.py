#!/usr/bin/env python3
"""lis2kickstart — translate a LIS document into a Fedora / RHEL Kickstart file.

Usage: lis2kickstart.py FILE.lis.{json,yaml} [--out DIR] [--lenient] [--apply]

Writes ks.cfg into DIR (default '.'):
  ks.cfg   — Anaconda kickstart for Fedora, RHEL, Rocky, Alma, CentOS

`--apply` hands ks.cfg to Anaconda; the native installer does the work.

Fail-closed by default (SPEC §2.3): core intent Kickstart cannot express is
*refused* with exit status 1. `--lenient` downgrades refusals to warnings.
"""

import argparse
import base64
import json
import pathlib
import sys

from lis_common import (track, check_unread, check_raid_consumers, chroot_intents, registration_commands, enrollment_commands, luks_key_path, seed_mount_commands, SEED_MOUNT, resolve_disk_paths, check_snapshots, match_selectors, system_commands, security_packages, file_commands, uid_commands, password_field, shell_packages, check_arch, check_script_fields,ALL_SECTIONS, add_common_args, check_firmware,
                        check_unhandled, check_section_fields, sudoers_commands, check_mirror, boot_timeout_commands, driver_packages,
                        check_boot_extras, check_keymap, check_version, enforce,
                        load_doc, refuse, report, role_fs, role_mountpoint, warn)

FS_MAP = {"vfat": "vfat", "ext4": "ext4", "xfs": "xfs", "btrfs": "btrfs", "swap": "swap"}


def b64(text: str) -> str:
    """Payloads reach %post base64-encoded — no shell or printf escaping to get wrong."""
    return base64.b64encode(text.encode()).decode()


# Anaconda makes the filesystem before --grow expands the volume, so a 1 MiB
# floor leaves mkfs.btrfs failing with "bad superblock on /dev/mapper/...".
GROW_FLOOR_MIB = 1024


def size_mib(size: str, what: str) -> str:
    """LIS size → the `--size`/`--grow` flags Anaconda expects."""
    if size == "rest":
        return f"--size={GROW_FLOOR_MIB} --grow"
    if size.endswith("%"):
        refuse(f"{what}: percent size {size!r} is not expressible in kickstart")
        return f"--size={GROW_FLOOR_MIB} --grow"
    for unit, factor in (("TiB", 1024 * 1024), ("GiB", 1024), ("MiB", 1)):
        if size.endswith(unit):
            return f"--size={int(size[: -len(unit)]) * factor}"
    refuse(f"{what}: unparseable size {size!r}")
    return "--size=4096"


# Containers whose `part` line must be written by %pre, as (id, key path).
DEFERRED_CRYPT: list[tuple[str, str]] = []


def render_storage(doc: dict, lines: list[str]) -> None:
    """LIS storage → clearpart/part/raid/volgroup/logvol directives."""
    storage = doc.get("storage", {}) or {}
    target = doc.get("target", {}) or {}
    if not storage:
        return

    disks = {}
    for disk in target.get("disks", []):
        match_selectors(disk)
        path = (disk.get("match", {}) or {}).get("path")
        if not path:
            refuse(f"disk '{disk['id']}': kickstart needs an explicit match.path — "
                   "Anaconda cannot evaluate LIS match rules")
            continue
        disks[disk["id"]] = path.split("/")[-1]

    firmware = target.get("firmware", "uefi")
    lines.append("bootloader --location=mbr" if firmware == "bios"
                 else "bootloader --location=mbr --boot-drive="
                 + (next(iter(disks.values())) if disks else "vda"))
    if storage.get("wipe"):
        lines.append("clearpart --all --initlabel"
                     + (f" --drives={','.join(disks.values())}" if disks else ""))
        if firmware == "bios" and disks:
            # Anaconda labels a cleared disk GPT, and GRUB then needs somewhere
            # to put its core image: "please create a 1MiB 'biosboot' type
            # partition". The document does not model it — it is an artifact of
            # the firmware/label combination, not declared intent.
            lines.append(f"part biosboot --fstype=biosboot --size=1 "
                         f"--ondisk={next(iter(disks.values()))}")
    else:
        refuse("storage.wipe: false — Anaconda kickstart cannot preserve an "
               "unaccounted existing layout in an unattended run (schema.md §20.8)")

    encryption = {c["over"]: c for c in storage.get("encryption", []) or []}
    deferred_crypt = DEFERRED_CRYPT
    consumed: dict[str, tuple[str, str]] = {}
    for group in storage.get("lvm", []) or []:
        for dev in group.get("devices", []):
            consumed[dev] = ("pv", group["name"])
    for array in storage.get("raid", []) or []:
        for dev in array.get("devices", []) + (array.get("spares", []) or []):
            consumed[dev] = ("raid", array["name"])

    btrfs_volumes: list[tuple[str, str | None, list]] = []
    for i, part in enumerate(storage.get("partitions", [])):
        ondisk = disks.get(part.get("disk"))
        if not ondisk:
            if part.get("disk") not in disks:
                refuse(f"partition {i}: references unknown disk handle {part.get('disk')!r}")
            continue
        if part.get("existing"):
            refuse(f"partition {i}: adopting an existing partition is not expressible "
                   "in an unattended kickstart")
            continue
        handle = part.get("id") or f"auto-{i}"
        role = part.get("role")
        fs = role_fs(part)
        size = size_mib(part.get("size", "rest"), f"partition '{handle}'")
        owner = consumed.get(handle)

        if owner and owner[0] == "pv":
            target_name = f"pv.{handle}"
        elif owner and owner[0] == "raid":
            target_name = f"raid.{handle}"
        elif role == "swap":
            target_name = "swap"
        else:
            target_name = role_mountpoint(part)
            if not target_name:
                refuse(f"partition '{handle}': no mountpoint and no consumer — "
                       "kickstart has no vocabulary for an unused partition")
                continue

        flags = [f"part {target_name}"]
        if not owner and role != "swap":
            fstype = FS_MAP.get(fs, fs)
            if fs == "vfat":
                # `efi` is a kickstart pseudo-type for the ESP mount specifically.
                fstype = "efi" if target_name == "/boot/efi" else "vfat"
            flags.append(f"--fstype={fstype}")
        elif role == "swap":
            flags.append("--fstype=swap")
        flags.append(size)
        flags.append(f"--ondisk={ondisk}")
        if crypt := encryption.get(handle):
            flags.append("--encrypted")
            if key_path := luks_key_path(doc, crypt["id"]):
                # Anaconda wants the passphrase inline, so this `part` line is
                # written by a %pre script that reads the seed and %include'd:
                # the served kickstart never carries the secret.
                deferred_crypt.append((crypt["id"], key_path))
                flags.append("@@LIS_PASSPHRASE@@")
            else:
                refuse(f"encryption '{crypt['id']}': no key material — declare a "
                       "keys[] entry with a seed: source, or place the passphrase "
                       f"at {SEED_MOUNT}/secrets/luks-{crypt['id']}.key")
            # Non-passphrase methods are enrolled from %post with
            # systemd-cryptenroll (see enrollment_commands), so they are honored
            # rather than refused — Anaconda simply is not the thing doing it.
        if part.get("label"):
            flags.append(f"--label={part['label']}")
        subvolumes = part.get("subvolumes") or []
        if subvolumes and fs != "btrfs":
            refuse(f"partition '{handle}': subvolumes declared on a {fs} filesystem")
            subvolumes = []
        if subvolumes and not owner:
            # kickstart carves subvolumes off a labelled btrfs volume, so the
            # partition becomes an unformatted member of that volume.
            # Keep the encryption flags: rebuilding the line from scratch
            # would drop them, creating the volume unencrypted while the
            # document asked for LUKS.
            crypt_flags = [f for f in flags
                           if f in ("--encrypted",) or f.startswith(("--escrowcert",
                                                                     "@@LIS_PASSPHRASE@@"))]
            flags = [f"part btrfs.{handle}", size, f"--ondisk={ondisk}", *crypt_flags]
            lines.append(" ".join(flags))
            btrfs_volumes.append((handle, part.get("mountpoint")
                                  or ("/" if role == "root" else None), subvolumes))
            continue
        lines.append(" ".join(flags))

    for handle, mountpoint, subvolumes in btrfs_volumes:
        lines.append(f"btrfs none --label={handle} btrfs.{handle}")
        covered = any(s["mountpoint"] == mountpoint for s in subvolumes)
        if mountpoint and not covered:
            lines.append(f"btrfs {mountpoint} --subvol --name=root LABEL={handle}")
        for sub in subvolumes:
            name = sub["name"].lstrip("@") or "root"
            lines.append(f"btrfs {sub['mountpoint']} --subvol --name={name} LABEL={handle}")

    for array in storage.get("raid", []) or []:
        members = " ".join(f"raid.{d}" for d in array.get("devices", []))
        spares = f" --spares={len(array['spares'])}" if array.get("spares") else ""
        # check_raid_consumers() has already established that something uses the
        # array; a volume group makes it a PV, encryption makes it the backing
        # device. Anaconda spells the PV case `raid pv.<name>`.
        in_lvm = any(array["name"] in g.get("devices", [])
                     for g in storage.get("lvm", []) or [])
        target = f"pv.{array['name']}" if in_lvm else "/"
        lines.append(f"raid {target} --level=RAID{array['level']} "
                     f"--device={array['name']}{spares} {members}")

    for group in storage.get("lvm", []) or []:
        pvs = " ".join(f"pv.{d}" for d in group.get("devices", []))
        lines.append(f"volgroup {group['name']} {pvs}")
        for vol in group.get("volumes", []):
            fs = vol.get("fs")
            size = size_mib(vol.get("size", "rest"),
                            f"lvm '{group['name']}' volume '{vol['name']}'")
            mountpoint = vol.get("mountpoint") or ("swap" if fs == "swap" else None)
            if not mountpoint:
                refuse(f"lvm volume '{vol['name']}': no mountpoint")
                continue
            lines.append(f"logvol {mountpoint} --vgname={group['name']} "
                         f"--name={vol['name']} --fstype={FS_MAP.get(fs, fs)} {size}")
            if vol.get("subvolumes"):
                refuse(f"lvm volume '{vol['name']}': btrfs subvolumes are not "
                       "expressible on a logvol")

    if (storage.get("swap", {}) or {}).get("zram"):
        warn("storage.swap.zram honored by installing zram-generator-defaults")
    if (storage.get("snapshots", {}) or {}).get("enabled"):
        pass   # honored by chroot_intents()


def render_kickstart(doc: dict) -> str:
    system = doc.get("system", {}) or {}
    boot = doc.get("boot", {}) or {}
    storage = doc.get("storage", {}) or {}
    software = doc.get("software", {}) or {}
    desktop = doc.get("desktop", {}) or {}
    network = doc.get("network", {}) or {}
    scripts = doc.get("scripts", {}) or {}
    installer = doc.get("installer", {}) or {}
    users = doc.get("users", []) or []

    lines: list[str] = ["# Generated by lis2kickstart (Linux Installation Specification)", ""]
    lines.append(f"lang {system.get('locale', 'en_US.UTF-8')}")
    km = system.get("keymap", {}) or {}
    console = km.get("console") or km.get("layout") or "us"
    layout = km.get("layout") or console
    xlayout = f"{layout} ({km['variant']})" if km.get("variant") else layout
    lines.append(f"keyboard --vckeymap={console} --xlayouts='{xlayout}'")
    utc = "" if system.get("hwclock") == "localtime" else " --utc"
    lines.append(f"timezone {system.get('timezone', 'UTC')}{utc}")
    lines.append("text")
    module = ((system.get("security") or {}).get("module"))
    if module == "selinux":
        lines.append("selinux --enforcing")
    elif module == "none":
        lines.append("selinux --disabled")

    if mirror_url := (doc.get("mirror", {}) or {}).get("url"):
        lines.append(f"url --url={mirror_url}")

    hostname = system.get("hostname")
    net_flags = ["network", "--bootproto=dhcp", "--activate"]
    if hostname:
        net_flags.append(f"--hostname={hostname}")
    lines.append(" ".join(net_flags))
    if network.get("wifi"):
        refuse("network.wifi is not expressible in kickstart")
    if manager := network.get("manager"):
        if manager not in ("auto", "networkmanager"):
            refuse(f"network.manager {manager!r}: Anaconda configures NetworkManager")
    if firewall := network.get("firewall"):
        flags = ["firewall", "--enabled" if firewall.get("enabled", True) else "--disabled"]
        for service in firewall.get("allow_services", []) or []:
            flags.append(f"--service={service}")
        for port in firewall.get("allow_ports", []) or []:
            flags.append(f"--port={port}")
        lines.append(" ".join(flags))
    ssh = network.get("ssh", {}) or {}

    root = next((u for u in users if u["name"] == "root"), None)
    if root and (h := (root.get("password") or {}).get("hash")):
        lines.append(f"rootpw --iscrypted {h}"
                     + (" --allow-ssh" if ssh.get("permit_root") in ("yes", "prohibit-password")
                        else ""))
    else:
        lines.append("rootpw --lock")

    for user in users:
        if user["name"] == "root":
            continue
        cmd = f"user --name={user['name']}"
        if (uid := user.get("uid")) is not None:
            cmd += f" --uid={uid}"
        groups = list(user.get("groups", []))
        if user.get("admin") and "wheel" not in groups:
            groups.insert(0, "wheel")
        if groups:
            cmd += f" --groups={','.join(groups)}"
        if comment := user.get("comment"):
            cmd += f" --gecos={json.dumps(comment)}"
        if shell := user.get("shell"):
            cmd += f" --shell={shell if shell.startswith('/') else '/usr/bin/' + shell}"
        password = user.get("password") or {}
        if field := password_field(user):
            if field != "!":
                cmd += f" --password={field} --iscrypted"
            if password.get("locked"):
                cmd += " --lock"
        else:
            refuse(f"user '{user['name']}': no password hash and not marked locked")
        lines.append(cmd)
        for key in user.get("ssh_authorized_keys", []) or []:
            lines.append(f"sshkey --username={user['name']} {json.dumps(key)}")
        if user.get("dotfiles"):
            pass   # honored by chroot_intents()

    render_storage(doc, lines)

    if loader := boot.get("loader"):
        if loader not in ("auto", "grub"):
            refuse(f"boot.loader {loader!r}: Anaconda installs GRUB")
    if params := (boot.get("kernel", {}) or {}).get("params"):
        for i, line in enumerate(lines):
            if line.startswith("bootloader "):
                lines[i] = line + f" --append={json.dumps(' '.join(params))}"
                break
    variant = (boot.get("kernel", {}) or {}).get("variant", "default")
    if variant not in ("default", None):
        refuse(f"boot.kernel.variant {variant!r} has no Fedora kernel package mapping")

    services = software.get("services", {}) or {}
    flags = []
    if enabled := services.get("enable"):
        flags.append(f"--enabled={','.join(enabled)}")
    if disabled := services.get("disable"):
        flags.append(f"--disabled={','.join(disabled)}")
    if flags:
        lines.append("services " + " ".join(flags))
    if ssh.get("enabled") and "sshd" not in (services.get("enable") or []):
        lines.append("services --enabled=sshd")

    if fin := installer.get("on_finish"):
        lines.append({"reboot": "reboot", "poweroff": "poweroff",
                      "halt": "halt"}.get(fin, "reboot"))
        if fin not in ("reboot", "poweroff", "halt"):
            refuse(f"installer.on_finish {fin!r} has no kickstart equivalent")
    else:
        lines.append("reboot")

    # Packages
    lines.append("")
    lines.append("%packages")
    lines.append("@core")
    role = software.get("role", "")
    role_groups = {"desktop:gnome": "@gnome-desktop", "desktop:kde": "@kde-desktop",
                   "desktop:xfce": "@xfce-desktop", "desktop:cinnamon": "@cinnamon-desktop",
                   "desktop:mate": "@mate-desktop", "desktop:budgie": "@budgie-desktop"}
    if role in role_groups:
        lines.append(role_groups[role])
    elif role == "server":
        lines.append("@server-product")
    elif role not in ("", "minimal"):
        refuse(f"software.role {role!r} has no Fedora group")

    pkgs = list(software.get("packages", []))
    pkgs += driver_packages(doc, "fedora")
    pkgs += shell_packages(doc)
    pkgs += security_packages(doc, "fedora")
    flatpaks = list(software.get("flatpak", []))
    for app in software.get("apps", []):
        if isinstance(app, str):
            pkgs.append(app)
        elif isinstance(app, dict):
            if name := (app.get("package") or app.get("name")):
                pkgs.append(name)
            if fp := app.get("flatpak"):
                flatpaks.append(fp)
    if flatpaks:
        pkgs.append("flatpak")
    if (storage.get("swap", {}) or {}).get("zram"):
        pkgs.append("zram-generator-defaults")
    if desktop.get("audio") in (None, "auto", "pipewire"):
        pass
    elif desktop.get("audio") == "pulseaudio":
        pkgs.append("pulseaudio")
    else:
        refuse(f"desktop.audio {desktop['audio']!r} has no Fedora package")
    if desktop.get("printing"):
        pkgs.append("cups")
    if desktop.get("bluetooth"):
        pkgs.append("bluez")
    for pkg in pkgs:
        lines.append(pkg)
    for excluded in software.get("exclude", []):
        lines.append(f"-{excluded}")
    lines.append("%end")
    if software.get("snap"):
        refuse("software.snap is not available on Fedora")

    early = [s["content"] for stage in ("pre_install", "pre")
             for s in scripts.get(stage, []) if s.get("content")]
    if early:
        lines += ["", "%pre", *early, "%end"]
    if scripts.get("post_storage"):
        warn("scripts.post_storage runs in %post (Anaconda has no post-partition hook)")

    late = [s["content"] for stage in ("post_storage", "post_install", "post",
                                       "pre_reboot", "on_success")
            for s in scripts.get(stage, []) if s.get("content")]
    for user in users:
        for s in (user.get("scripts", {}) or {}).get("post_install", []):
            if c := s.get("content"):
                late.append(f"su - {user['name']} -c {json.dumps(c)}")
    late += sudoers_commands(doc)
    late += uid_commands(doc)
    late += enrollment_commands(doc)
    late += registration_commands(doc, "fedora")
    late += chroot_intents(doc, "fedora")
    late += system_commands(doc, "fedora")
    late += boot_timeout_commands(doc, "fedora", (doc.get("boot") or {}).get("loader", "grub"))
    for entry in doc.get("files", []) or []:
        late += file_commands(entry)
    for app in flatpaks:
        late.append(f"flatpak install -y --noninteractive flathub {app}")
    if desktop.get("autologin"):
        late.append("install -d /etc/gdm")
        late.append(f"echo {b64(chr(10).join(['[daemon]', 'AutomaticLoginEnable=True', 
                    'AutomaticLogin=' + desktop['autologin'], '']))} | base64 -d "
                    "> /etc/gdm/custom.conf")

    # Birth certificate (delivery.md §8).
    birth = b64(json.dumps(doc, separators=(",", ":")))
    late.append("install -d -m755 /var/lib/lis")
    late.append(f"echo {birth} | base64 -d > /var/lib/lis/system.lis.json")
    late.append("chmod 600 /var/lib/lis/system.lis.json")

    lines += ["", "%post --erroronfail", *late, "%end"]

    firstboot = [s["content"] for s in scripts.get("firstboot", []) if s.get("content")]
    for user in users:
        for s in (user.get("scripts", {}) or {}).get("firstboot", []):
            if c := s.get("content"):
                firstboot.append(f"su - {user['name']} -c {json.dumps(c)}")
    if firstboot:
        # %post writes a one-shot unit; Anaconda itself has no first-boot stage.
        unit = ("[Unit]\nDescription=LIS first boot\nAfter=multi-user.target\n"
                "ConditionPathExists=!/var/lib/lis/.firstboot-done\n\n"
                "[Service]\nType=oneshot\nExecStart=/usr/libexec/lis-firstboot\n\n"
                "[Install]\nWantedBy=multi-user.target\n")
        script = ("#!/bin/sh\n" + "\n".join(firstboot)
                  + "\ntouch /var/lib/lis/.firstboot-done\n")
        insert = lines.index("%end", lines.index("%post --erroronfail"))
        lines[insert:insert] = [
            f"echo {b64(script)} | base64 -d > /usr/libexec/lis-firstboot",
            "chmod 755 /usr/libexec/lis-firstboot",
            f"echo {b64(unit)} | base64 -d > /etc/systemd/system/lis-firstboot.service",
            "systemctl enable lis-firstboot.service",
        ]

    if scripts.get("on_error"):
        refuse("scripts.on_error has no kickstart equivalent")
    if proxy := (doc.get("proxy", {}) or {}).get("http"):
        lines.insert(1, f"# proxy: {proxy}")
        warn("proxy.http is applied to the installer environment only")

    if DEFERRED_CRYPT:
        # Anaconda has no keyfile option for --passphrase, so the lines that
        # carry it are generated inside the installer from seed key material
        # and pulled in with %include. The served kickstart stays secret-free.
        crypt_lines = [l for l in lines if "@@LIS_PASSPHRASE@@" in l]
        lines = [l for l in lines if "@@LIS_PASSPHRASE@@" not in l]
        pre = ["", "%pre --erroronfail", *seed_mount_commands()]
        for cid, key_path in DEFERRED_CRYPT:
            pre.append(f'LIS_PASS_{cid.replace("-", "_")}=$(cat {key_path})')
        pre.append("cat > /tmp/lis-crypt.ks <<LIS_EOF")
        for line in crypt_lines:
            for cid, _ in DEFERRED_CRYPT:
                line = line.replace("@@LIS_PASSPHRASE@@",
                                    f'--passphrase=$LIS_PASS_{cid.replace("-", "_")}')
            pre.append(line)
        pre += ["LIS_EOF", "%end", "", "%include /tmp/lis-crypt.ks"]
        lines += pre
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Translate a LIS document into an Anaconda kickstart.")
    add_common_args(ap)
    ap.add_argument("--apply", "-a", action="store_true",
                    help="run Anaconda on the live system with the generated kickstart")
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
    check_section_fields(doc, "installer", {"on_finish"})
    check_keymap(doc, {"console", "layout", "variant"})

    ks = render_kickstart(doc)
    args.out.mkdir(parents=True, exist_ok=True)
    ks_file = args.out / "ks.cfg"
    ks_file.write_text(ks)
    report(ks_file)

    # Fail closed *before* touching the machine, not after.
    check_arch(doc, {"x86_64"})
    check_raid_consumers(doc)
    check_snapshots(doc, tools={"snapper"}, boot_menu=False)
    check_script_fields(doc)
    check_unread(doc)

    if status := enforce(args.strict):
        return status

    if args.apply:
        import shutil
        import subprocess
        if not shutil.which("anaconda"):
            sys.exit("error: --apply requested, but 'anaconda' is not on PATH "
                     "(are you running on a Fedora/RHEL install image?)")
        cmd = ["anaconda", "--kickstart", str(ks_file), "--cmdline", "--noninteractive"]
        print(f"executing native installer: {' '.join(cmd)}")
        return subprocess.run(cmd).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
