#!/usr/bin/env python3
"""lis2void — translate a LIS document into a Void Linux VAI answer file.

Usage: lis2void.py FILE.lis.{json,yaml} [--out DIR] [--lenient]
       lis2void.py --check-vai PATH/TO/01-install.sh   (self-audit, see below)

Writes into DIR (default '.'):
  autoinstall.cfg — the answer file VAI reads

WHAT DRIVES THE INSTALL
-----------------------
void-mklive ships an official automated installer, **VAI**, as a dracut module
(`dracut/autoinstaller`, installed into every live ISO's initramfs as
`/usr/lib/dracut/hooks/pre-mount/01-install.sh`). It is triggered purely from
the kernel command line:

    auto=1 autourl=http://host/autoinstall.cfg

VAI then fetches that URL with `xbps-uhelper fetch`, **sources it as /bin/sh**,
and runs its sixteen steps: dhclient, partition (sfdisk), mkfs, mount,
`xbps-install -Sy -R $repo -r /mnt base-system grub`, sudoers, hostname,
rc.conf, chroot prep, useradd, `grub-install` + `update-grub`, fstab,
glibc-locales, end action. `end_action=func` runs the `end_function` the answer
file defines, with the target still mounted at /mnt and proc/sys/dev bound.

That is a real answer-file mechanism, the same class as preseed or kickstart,
and it is what this applier targets. Every variable and every function name
emitted here appears verbatim in the shipped `01-install.sh`; `--check-vai`
asserts that against a copy extracted from the pinned ISO, because an
answer file that is *sourced* silently ignores a name it does not know — which
is the exact failure mode this project exists to prevent.

WHERE THE LINE FALLS, AND WHY
-----------------------------
VAI's install steps are functions in the shell it sources our file into, so the
file can replace one. This applier replaces exactly four, and only because each
is hardcoded to a single layout that cannot express a LIS `storage` section:

  VAI_format_disk     always mkfs.ext4 on p1 and p3
  VAI_mount_target    always root then /boot, no subvolumes
  VAI_configure_fstab always `ext4 defaults,errors=remount-ro`
  VAI_add_user        one user, `-G wheel,users,…`, **plaintext** password

The other twelve steps are VAI's own and run untouched — including the two that
matter most: **VAI partitions the disk** (its own sfdisk call, steered by the
`disk`, `bootpartitionsize` and `swapsize` knobs) and **VAI installs the system**
(`xbps-install -r /mnt base-system grub`). This applier writes no partition
table. Anything VAI's fixed three-partition MBR scheme cannot express is
refused, not worked around: see the refusals below. Do not add a partitioner
here — VAI runs partitioning at step 3 and `end_function` at step 16, so a
partitioner in `end_function` could only destroy what the install is sitting on.

NOT SUPPORTED (refused, per SPEC §2.3)
  UEFI            the shipped VAI writes an MBR label and its initramfs carries
                  no mkfs.vfat, so there is no way to make an ESP
  LUKS, LVM, RAID VAI creates none of them and formats p3 directly
  >1 disk         VAI takes a single `disk`
  any layout other than [boot, swap, root(rest)] on one disk
  `existing` partition adoption

CAVEAT WORTH STATING: VAI is undocumented — it appears nowhere in the Void
handbook — and it moves. The version in void-mklive master already differs from
the one on the pinned ISO (GPT + ESP, `disk_expr`/`hostname_expr`, a
`VAI_udev_settle` step). This applier is written against the ISO's copy, and
`--check-vai` is how that stays true.
"""

import argparse
import base64
import json
import pathlib
import re
import shlex
import sys

from lis_common import (
    ALL_SECTIONS, APPLY_TIME_PATHS, SEED_MOUNT, add_common_args,
    boot_timeout_commands, check_arch, check_boot_extras, check_encryption_emitted,
    check_firmware, check_keymap, check_kernel_variant, check_mirror,
    check_raid_consumers, check_script_fields, check_section_fields,
    check_snapshots, check_unhandled, check_unread, check_version, chroot_intents,
    driver_packages, enforce, file_commands, load_doc, match_selectors,
    parse_size, password_field, refuse, registration_commands, report,
    resolve_mountpoints, role_fs, security_packages, shell_packages,
    sudoers_commands, system_commands, track, warn,
)

# ── the VAI vocabulary this applier is allowed to speak ──────────────
#
# Every name here must exist in the shipped autoinstaller. `--check-vai`
# proves it against a copy extracted from the pinned ISO's initramfs:
#
#   osirrox -indev void-live-x86_64-20250202-base.iso -extract /boot/initrd initrd
#   # skip the early-cpio microcode segment, then:
#   zstd -d < initrd.payload | cpio -id
#   ./tools/appliers/lis2void.py --check-vai var/lib/dracut/hooks/pre-mount/01-install.sh
VAI_VARIABLES = (
    "disk", "bootpartitionsize", "swapsize", "xbpsrepository", "pkgs",
    "hostname", "timezone", "keymap", "libclocale", "end_action", "target",
    "XBPS_ARCH", "username", "password", "end_script",
)
VAI_FUNCTIONS = (
    "VAI_format_disk", "VAI_mount_target", "VAI_configure_fstab",
    "VAI_add_user", "end_function",
)

# The live ISO's initramfs carries mkfs.ext4/mke2fs/mkswap and nothing else, so
# any other filesystem needs its mkfs fetched with xbps — Void's own package
# manager, over the network VAI has already brought up — into a scratch root.
MKFS_PACKAGE = {"btrfs": "btrfs-progs", "xfs": "xfsprogs", "f2fs": "f2fs-tools"}
# Filesystems the target itself needs the tools for (fsck at boot, dracut, grub).
TARGET_FS_PACKAGE = MKFS_PACKAGE

BOOT_FILESYSTEMS = ("ext2", "ext3", "ext4")
ROOT_FILESYSTEMS = ("ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs")

DEFAULT_REPO = "https://repo-default.voidlinux.org/current"
MUSL_REPO = "https://repo-default.voidlinux.org/current/musl"

# software.role -> Void package, verified against the current x86_64 repository
# index (repo-default.voidlinux.org/current/x86_64-repodata).
ROLE_PACKAGES = {
    "minimal": [], "server": [],
    "desktop:gnome": ["gnome"], "desktop:kde": ["kde5"],
    "desktop:xfce": ["xfce4"],
}

# Only the ttys runit-void ships a service directory for can carry a getty.
SERIAL_TTYS = ("ttyS0", "ttyS1", "ttyS2", "ttyS3", "ttyUSB0", "ttyAMA0")

POST_DELIM = "__LIS_VOID_POST__"


def q(value) -> str:
    return shlex.quote(str(value))


# ── storage ──────────────────────────────────────────────────────────

class Layout:
    """The three partitions VAI's 'Atomic' scheme lays down, or nothing."""

    def __init__(self):
        self.boot = self.swap = self.root = None
        self.ok = False


def read_layout(doc: dict) -> Layout:
    """Match the document's partitions onto VAI's fixed scheme, or refuse.

    VAI's sfdisk heredoc is literally three lines — `,$bootpartitionsize`,
    `,${swapsize}K`, `;` — so p1 is /boot, p2 is swap and p3 is the root, which
    takes the rest of the disk. There is no knob for anything else, and this
    applier does not write a partition table, so a document that wants another
    shape is refused rather than quietly installed as this one.
    """
    layout = Layout()
    storage = doc.get("storage", {}) or {}
    target = doc.get("target", {}) or {}

    for key in sorted(set(storage) - {"wipe", "partitions"}):
        if storage[key] in (None, {}, []):
            continue
        refuse(f"storage.{key} is not created by VAI — its install scheme "
               "formats one partition directly, with no encryption, volume "
               "group, array or snapshot layer")

    if not storage.get("wipe", False):
        refuse("storage.wipe is not true — VAI always runs sfdisk over the whole "
               "disk and mkfs over the result, so it cannot preserve anything")

    disks = target.get("disks", []) or []
    for entry in disks:
        _ = entry.get("id")
        match_selectors(entry)
    paths = [(d.get("match", {}) or {}).get("path") for d in disks]
    if len(disks) != 1:
        refuse(f"{len(disks)} target.disks declared — VAI installs to the single "
               "device named by its `disk` knob")
    if not paths or not paths[0]:
        refuse("target.disks[0].match.path is required — VAI's `disk` knob is a "
               "device path and this applier never runs on the target, so a "
               "selector rule cannot be evaluated")

    parts = list(storage.get("partitions", []) or [])
    mounts = resolve_mountpoints(parts)
    for i, part in enumerate(parts):
        _ = part.get("disk"), part.get("id")
        if part.get("existing"):
            refuse(f"partition {i}: VAI always repartitions; adopting an existing "
                   "partition is not expressible")

    if len(parts) != 3:
        refuse(f"storage.partitions declares {len(parts)} partition(s) — VAI's "
               "scheme creates exactly three: /boot, swap and the root, in that "
               "order on one disk")
        return layout

    boot, swap, root = parts
    if mounts.get(0) != "/boot" or boot.get("role") != "boot":
        refuse("storage.partitions[0] must be the /boot partition — VAI's first "
               "partition is sized by `bootpartitionsize` and mounted at /boot")
    if swap.get("role") != "swap" or role_fs(swap) != "swap":
        refuse("storage.partitions[1] must be the swap partition — VAI's second "
               "partition is sized by `swapsize` and made with mkswap")
    if mounts.get(2) != "/" or root.get("role") != "root":
        refuse("storage.partitions[2] must be the root partition — VAI's third "
               "partition takes the rest of the disk and is mounted at /")
    if root.get("size", "rest") != "rest":
        refuse(f"storage.partitions[2].size {root.get('size')!r}: VAI gives the "
               "third partition whatever is left, so the root must be 'rest'")
    if boot.get("size", "rest") == "rest" or swap.get("size", "rest") == "rest":
        refuse("only the root partition may be 'rest' — `bootpartitionsize` and "
               "`swapsize` are absolute sizes")

    boot_fs = role_fs(boot)
    if boot_fs not in BOOT_FILESYSTEMS:
        refuse(f"/boot filesystem {boot_fs!r}: the installer initramfs carries "
               f"mke2fs only, so /boot must be one of {', '.join(BOOT_FILESYSTEMS)}")
    root_fs = role_fs(root)
    if root_fs not in ROOT_FILESYSTEMS:
        refuse(f"root filesystem {root_fs!r} is not one this applier can create "
               f"(supports {', '.join(ROOT_FILESYSTEMS)})")
    if root.get("subvolumes") and root_fs != "btrfs":
        refuse(f"storage.partitions[2].subvolumes needs a btrfs root, not {root_fs!r}")
    for i, part in enumerate(parts):
        if part.get("subvolumes") and part is not root:
            refuse(f"partition {i}: subvolumes are only honored on the root partition")

    subs = root.get("subvolumes", []) or []
    if subs and not any(s.get("mountpoint") == "/" for s in subs):
        refuse("storage.partitions[2].subvolumes declares no subvolume at '/' — "
               "the root filesystem would have nothing to boot from")

    layout.boot, layout.swap, layout.root = boot, swap, root
    layout.ok = True
    return layout


def mkfs_command(part: dict, node: str, *, tools: str | None) -> str:
    """The mkfs invocation for one partition, run from the installer initramfs.

    `tools` is the scratch root a bootstrapped mkfs lives in, or None when the
    initramfs already has the program.
    """
    fs = role_fs(part)
    label = part.get("label")
    if fs == "swap":
        return f"mkswap -f{f' -L {q(label)}' if label else ''} {node}"
    if fs in ("ext2", "ext3", "ext4"):
        return (f"mke2fs -F -t {fs}{f' -L {q(label)}' if label else ''} {node}")
    flag = "-l" if fs == "f2fs" else "-L"
    args = f"-f{f' {flag} {q(label)}' if label else ''}"
    return (f"LD_LIBRARY_PATH=/usr/lib PATH=/usr/bin chroot \"{tools}\" "
            f"/usr/bin/mkfs.{fs} {args} {node}")


def fstab_lines(layout: Layout) -> list[str]:
    """Shell that writes the target's fstab from the document's filesystems."""
    root_fs = role_fs(layout.root)
    subs = layout.root.get("subvolumes", []) or []
    root_sub = next((s for s in subs if s.get("mountpoint") == "/"), None)
    check = "1" if root_fs.startswith("ext") else "0"

    def options(node: dict, extra: str = "") -> str:
        opts = list(node.get("mount_options", []) or [])
        if extra:
            opts.insert(0, extra)
        return ",".join(["defaults", *opts]) if opts else "defaults"

    out = ['uuid1="$(blkid -s UUID -o value "${disk}1")"',
           'uuid2="$(blkid -s UUID -o value "${disk}2")"',
           'uuid3="$(blkid -s UUID -o value "${disk}3")"']
    root_opts = options(layout.root, f"subvol={root_sub['name']}" if root_sub else "")
    out.append(f'echo "UUID=$uuid3 / {root_fs} {root_opts} 0 {check}" '
               '>> "${target}/etc/fstab"')
    for sub in subs:
        if sub is root_sub:
            continue
        out.append(f'echo "UUID=$uuid3 {sub["mountpoint"]} {root_fs} '
                   f'{options(sub, f"subvol={sub['name']}")} 0 0" '
                   '>> "${target}/etc/fstab"')
    out.append(f'echo "UUID=$uuid1 /boot {role_fs(layout.boot)} '
               f'{options(layout.boot)} 0 2" >> "${{target}}/etc/fstab"')
    out.append(f'echo "UUID=$uuid2 swap swap {options(layout.swap)} 0 0" '
               '>> "${target}/etc/fstab"')
    return out


def storage_overrides(layout: Layout) -> tuple[list[str], list[str]]:
    """The three VAI step functions that express `storage`, and the extra pkgs.

    VAI's originals are hardcoded to ext4-on-p1-and-p3, one mount each and a
    fixed fstab line; they are replaced because they cannot say what the
    document says, not because the applier prefers its own.
    """
    root_fs = role_fs(layout.root)
    boot_fs = role_fs(layout.boot)
    needed = sorted({MKFS_PACKAGE[fs] for fs in (root_fs, boot_fs)
                     if fs in MKFS_PACKAGE})
    tools = "${lis_tools}"

    body = ["VAI_format_disk() {",
            "    # The kernel has only just been told the table changed; VAI's own",
            "    # copy races udev here (master added a settle step for this).",
            "    udevadm settle 2>/dev/null || true",
            "    _i=0",
            '    while [ $_i -lt 30 ] && [ ! -b "${disk}3" ]; do '
            "sleep 1; _i=$((_i+1)); done"]
    if needed:
        body.append(f"    lis_fetch_tools {' '.join(needed)}")
    body += [f"    {mkfs_command(layout.boot, '\"${disk}1\"', tools=tools)}",
             f"    {mkfs_command(layout.swap, '\"${disk}2\"', tools=tools)}",
             f"    {mkfs_command(layout.root, '\"${disk}3\"', tools=tools)}",
             "}"]

    subs = layout.root.get("subvolumes", []) or []
    root_sub = next((s for s in subs if s.get("mountpoint") == "/"), None)
    mount = ["", "VAI_mount_target() {"]
    if subs:
        mount += ['    mount "${disk}3" "${lis_tools}/tmp/fs"']
        for sub in subs:
            mount.append(f'    btrfs subvolume create "${{lis_tools}}/tmp/fs/{sub["name"]}"')
        if root_sub:
            # The kernel mounts the top level unless told otherwise; making the
            # root subvolume the filesystem default means a plain mount — a
            # rescue shell, a forgotten rootflags= — still lands on a system.
            mount.append(f'    btrfs subvolume set-default '
                         f'"${{lis_tools}}/tmp/fs/{root_sub["name"]}" || true')
        mount.append('    umount "${lis_tools}/tmp/fs"')
    mount.append('    mkdir -p "${target}"')
    if root_sub:
        mount.append(f'    mount -o subvol={root_sub["name"]} "${{disk}}3" "${{target}}"')
    else:
        mount.append('    mount "${disk}3" "${target}"')
    mount.append('    mkdir -p "${target}/boot"')
    mount.append('    mount "${disk}1" "${target}/boot"')
    for sub in subs:
        if sub is root_sub:
            continue
        mount += [f'    mkdir -p "${{target}}{sub["mountpoint"]}"',
                  f'    mount -o subvol={sub["name"]} "${{disk}}3" '
                  f'"${{target}}{sub["mountpoint"]}"']
    mount.append("}")

    fstab = ["", "VAI_configure_fstab() {"]
    fstab += [f"    {line}" for line in fstab_lines(layout)]
    fstab.append("}")

    target_packages = sorted({TARGET_FS_PACKAGE[fs] for fs in (root_fs, boot_fs)
                              if fs in TARGET_FS_PACKAGE})
    return body + mount + fstab, target_packages


TOOLS_HELPER = """
# Filesystem tools the installer initramfs does not carry, fetched with xbps
# (Void's own package manager) into a scratch root over the network VAI has
# already brought up. Nothing is written to the target from here.
lis_fetch_tools() {
    mkdir -p "${lis_tools}"
    mount -t tmpfs -o size=512M tmpfs "${lis_tools}"
    mkdir -p "${lis_tools}/var/db/xbps/keys" "${lis_tools}/usr/bin" \\
             "${lis_tools}/usr/lib" "${lis_tools}/tmp/fs"
    ln -s usr/bin "${lis_tools}/bin"
    ln -s usr/bin "${lis_tools}/sbin"
    ln -s usr/lib "${lis_tools}/lib"
    ln -s usr/lib "${lis_tools}/lib64"
    cp /var/db/xbps/keys/* "${lis_tools}/var/db/xbps/keys/"
    XBPS_ARCH="${XBPS_ARCH}" xbps-install -Sy -R "${xbpsrepository}" \\
        -r "${lis_tools}" "$@"
    mkdir -p "${lis_tools}/dev" "${lis_tools}/proc" "${lis_tools}/sys"
    mount --rbind /dev "${lis_tools}/dev"
    mount -t proc proc "${lis_tools}/proc"
    mount --rbind /sys "${lis_tools}/sys"
}
"""


# ── the post script, run inside the installed system ─────────────────

def service_link(name: str) -> list[str]:
    """Enable a runit service the way Void documents it, failing on a typo.

    `ln -s` into runsvdir succeeds whether or not /etc/sv/<name> exists, so a
    name the target has no service for would be dropped in silence.
    """
    return [f"[ -d /etc/sv/{q(name)} ] || {{ echo \"lis: no runit service "
            f"/etc/sv/{name}\" >&2; exit 1; }}",
            f"ln -sfn /etc/sv/{q(name)} /etc/runit/runsvdir/default/{q(name)}"]


def user_commands(doc: dict) -> list[str]:
    """Create the accounts the document declares, with their real hashes.

    Void ships shadow-utils, so `useradd`/`usermod -p` take the crypt(3) string
    from the document directly — VAI's own step takes a plaintext password and
    is replaced for exactly that reason (SPEC §2.4).
    """
    out: list[str] = []
    for user in doc.get("users", []) or []:
        name = user["name"]
        field = password_field(user)
        if name == "root":
            if field:
                out.append(f"usermod -p {q(field)} root")
            else:
                out.append("passwd -l root")
                warn("users['root'] declares no password hash; the account is locked")
            for key in user.get("ssh_authorized_keys", []) or []:
                out += ["install -d -m700 /root/.ssh",
                        f"printf '%s\\n' {q(key)} >> /root/.ssh/authorized_keys",
                        "chmod 600 /root/.ssh/authorized_keys"]
            continue

        groups = list(user.get("groups", []) or [])
        if user.get("admin"):
            groups.append("wheel")
        for group in groups:
            out.append(f"groupadd -f {q(group)}")
        add = ["useradd", "-m", "-U"]
        if (uid := user.get("uid")) is not None:
            add += ["-u", str(uid)]
        if shell := user.get("shell"):
            add += ["-s", q(shell if shell.startswith("/") else f"/bin/{shell}")]
        if comment := user.get("comment"):
            add += ["-c", q(comment)]
        if groups:
            add += ["-G", q(",".join(dict.fromkeys(groups)))]
        add.append(q(name))
        out.append(f"id -u {q(name)} >/dev/null 2>&1 || {' '.join(add)}")
        if field:
            out.append(f"usermod -p {q(field)} {q(name)}")
        elif not (user.get("password") or {}).get("locked"):
            refuse(f"users['{name}']: no password hash and not marked locked — "
                   "the account would be unusable or open")
        if keys := user.get("ssh_authorized_keys", []) or []:
            home = f"/home/{name}"
            out.append(f"install -d -m700 -o {q(name)} -g {q(name)} {home}/.ssh")
            for key in keys:
                out.append(f"printf '%s\\n' {q(key)} >> {home}/.ssh/authorized_keys")
            out.append(f"chown {q(name)} {home}/.ssh/authorized_keys")
            out.append(f"chmod 600 {home}/.ssh/authorized_keys")
    return out


def grub_commands(doc: dict, params: list[str]) -> list[str]:
    """Apply boot.* to the target's own /etc/default/grub, then regenerate.

    VAI runs grub-install and update-grub at step 13, before `end_function`, so
    the configuration is edited and update-grub run again rather than the whole
    step being replaced.
    """
    out: list[str] = []
    if params:
        joined = " ".join(params)
        # Void's file ships GRUB_CMDLINE_LINUX_DEFAULT and no GRUB_CMDLINE_LINUX,
        # so a plain sed on the latter would be a silent no-op.
        out.append("sed -i '/^GRUB_CMDLINE_LINUX_DEFAULT=/d' /etc/default/grub")
        out.append(f"printf 'GRUB_CMDLINE_LINUX_DEFAULT=\"%s\"\\n' {q(joined)} "
                   ">> /etc/default/grub")
    if any(re.search(r"\bconsole=tty(S|USB|AMA)\d", p) for p in params):
        out += ["sed -i '/^GRUB_TERMINAL/d' /etc/default/grub",
                "printf 'GRUB_TERMINAL_INPUT=\"console serial\"\\n"
                "GRUB_TERMINAL_OUTPUT=\"console serial\"\\n"
                "GRUB_SERIAL_COMMAND=\"serial --unit=0 --speed=115200\"\\n' "
                ">> /etc/default/grub"]
    out += boot_timeout_commands(doc, "void", "grub")
    if params and "update-grub" not in out:
        out.append("update-grub")
    return out


def kernel_module_commands(doc: dict) -> list[str]:
    kernel = ((doc.get("boot") or {}).get("kernel") or {})
    out: list[str] = []
    if modules := kernel.get("modules"):
        out.append("install -d -m755 /etc/modules-load.d")
        out.append("printf '%s\\n' " + " ".join(q(m) for m in modules)
                   + " > /etc/modules-load.d/lis.conf")
    if blacklist := kernel.get("blacklist"):
        out.append("install -d -m755 /etc/modprobe.d")
        for module in blacklist:
            out.append(f"printf 'blacklist %s\\n' {q(module)} "
                       ">> /etc/modprobe.d/lis-blacklist.conf")
    initramfs = (doc.get("boot") or {}).get("initramfs") or {}
    generator = initramfs.get("generator")
    if generator not in (None, "auto", "dracut"):
        refuse(f"boot.initramfs.generator {generator!r}: Void builds initramfs "
               "images with dracut")
    if extra := initramfs.get("include_modules"):
        out.append("install -d -m755 /etc/dracut.conf.d")
        out.append("printf 'add_drivers+=\" %s \"\\n' "
                   + q(" ".join(extra)) + " > /etc/dracut.conf.d/10-lis.conf")
        out.append("xbps-reconfigure -f "
                   "\"$(xbps-query -p pkgver linux 2>/dev/null || echo linux)\" "
                   "|| true")
    return out


def network_commands(doc: dict) -> list[str]:
    network = doc.get("network", {}) or {}
    out: list[str] = []
    if interfaces := network.get("interfaces"):
        refuse(f"network.interfaces ({len(interfaces)} declared): static "
               "addressing is not generated by this applier")
    if network.get("wifi"):
        refuse("network.wifi is not configured by this applier")
    for entry in network.get("hosts", []) or []:
        names = " ".join(entry.get("names", []) or [])
        out.append(f"printf '%s\\t%s\\n' {q(entry['ip'])} {q(names)} >> /etc/hosts")
    ssh = network.get("ssh") or {}
    if ssh.get("enabled"):
        out.append("xbps-install -Sy openssh")
        out += service_link("sshd")
        if ssh.get("password_auth") is False:
            out.append("printf 'PasswordAuthentication no\\n' "
                       "> /etc/ssh/sshd_config.d/99-lis.conf 2>/dev/null || "
                       "printf 'PasswordAuthentication no\\n' >> /etc/ssh/sshd_config")
        if permit := ssh.get("permit_root"):
            out.append(f"printf 'PermitRootLogin %s\\n' {q(permit)} "
                       ">> /etc/ssh/sshd_config")
    elif ssh:
        out.append("rm -f /etc/runit/runsvdir/default/sshd")
    return out


def firstboot_commands(doc: dict) -> list[str]:
    """Install the firstboot payload as a runit core service, guarded by a marker.

    /etc/runit/1 sources every /etc/runit/core-services/*.sh — Void's own
    documented place for "one-time system tasks" — after the root filesystem is
    remounted read-write and all filesystems are mounted. The body runs in its
    own /bin/sh so a failing hook cannot take stage 1 down with it.
    """
    body: list[str] = []
    for item in (doc.get("scripts", {}) or {}).get("firstboot", []) or []:
        if content := item.get("content"):
            body.append(content)
    for user in doc.get("users", []) or []:
        for item in (user.get("scripts", {}) or {}).get("firstboot", []) or []:
            if content := item.get("content"):
                body.append(f"su - {q(user['name'])} -c {q(content)}")
    if not body:
        return []
    payload = "#!/bin/sh\n" + "\n".join(body) + "\n"
    service = (
        "# Generated by lis2void — LIS scripts.firstboot, once per machine.\n"
        "if [ ! -e /var/lib/lis/.firstboot-done ]; then\n"
        '    msg "Running LIS first-boot hooks"\n'
        "    /bin/sh /usr/local/lib/lis/firstboot >/var/log/lis-firstboot.log 2>&1 "
        "|| true\n"
        "    install -d -m755 /var/lib/lis\n"
        "    : > /var/lib/lis/.firstboot-done\n"
        "fi\n")
    return ["install -d -m755 /usr/local/lib/lis",
            f"echo {b64(payload)} | base64 -d > /usr/local/lib/lis/firstboot",
            "chmod 755 /usr/local/lib/lis/firstboot",
            "install -d -m755 /etc/runit/core-services",
            f"echo {b64(service)} | base64 -d "
            "> /etc/runit/core-services/99-lis-firstboot.sh"]


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def post_script(doc: dict, layout: Layout, params: list[str],
                serial_ttys: list[str]) -> list[str]:
    """Everything VAI has no key for, run inside the installed system."""
    system = doc.get("system", {}) or {}
    software = doc.get("software", {}) or {}
    scripts = doc.get("scripts", {}) or {}

    out = ["#!/bin/sh",
           "# Generated by lis2void — runs inside the installed system, from the",
           "# end_function VAI calls at step 16.",
           "set -e", ""]

    out += user_commands(doc)
    out += sudoers_commands(doc)

    services = software.get("services", {}) or {}
    for unit in services.get("enable", []) or []:
        out += service_link(re.sub(r"\.service$", "", unit))
    for unit in services.get("disable", []) or []:
        out.append(f"rm -f /etc/runit/runsvdir/default/{q(re.sub(r'.service$', '', unit))}")
    for tty in serial_ttys:
        out += service_link(f"agetty-{tty}")

    out += network_commands(doc)

    for entry in doc.get("files", []) or []:
        out += file_commands(entry)

    out += chroot_intents(doc, "void")

    if locale := system.get("locale"):
        out += ["touch /etc/locale.conf",
                "sed -i '/^LANG=/d' /etc/locale.conf",
                f"printf 'LANG=%s\\n' {q(locale)} >> /etc/locale.conf"]
    if hwclock := system.get("hwclock"):
        # rc.conf is what runit-void reads at boot; /etc/adjtime (written by
        # system_commands below) is what hwclock(8) and every other tool read.
        mode = "LOCAL" if hwclock == "localtime" else "UTC"
        out.append("sed -i 's|^#\\?HARDWARECLOCK=.*|HARDWARECLOCK=\"" + mode
                   + "\"|' /etc/rc.conf")
    if font := (system.get("keymap") or {}).get("font"):
        out.append(f"sed -i 's|^#\\?FONT=.*|FONT=\"{font}\"|' /etc/rc.conf")
    out += system_commands(doc, "void")
    out += kernel_module_commands(doc)
    out += grub_commands(doc, params)

    out += registration_commands(doc, "void")

    for stage in ("post_storage", "post_install", "post", "pre_reboot", "on_success"):
        for item in scripts.get(stage, []) or []:
            if content := item.get("content"):
                out.append(content)
    for user in doc.get("users", []) or []:
        for item in (user.get("scripts", {}) or {}).get("post_install", []) or []:
            if content := item.get("content"):
                out.append(f"su - {q(user['name'])} -c {q(content)}")

    out += firstboot_commands(doc)

    # Birth certificate (delivery.md §8).
    birth = b64(json.dumps(doc, separators=(",", ":")))
    out += ["install -d -m755 /var/lib/lis",
            f"echo {birth} | base64 -d > /var/lib/lis/system.lis.json",
            "chmod 600 /var/lib/lis/system.lis.json"]
    return out


# ── the answer file ──────────────────────────────────────────────────

def render(doc: dict) -> str:
    system = doc.get("system", {}) or {}
    software = doc.get("software", {}) or {}
    boot = doc.get("boot", {}) or {}
    target = doc.get("target", {}) or {}

    if (init := system.get("init")) not in (None, "auto", "runit"):
        refuse(f"system.init {init!r}: Void runs runit and ships no other init")
    if target.get("firmware") not in ("bios",):
        refuse(f"target.firmware {target.get('firmware', 'auto')!r}: the "
               "autoinstaller on the pinned Void ISO writes an MBR label and its "
               "initramfs has no mkfs.vfat, so it can only install a BIOS system "
               "— declare firmware 'bios'")

    loader = boot.get("loader", "auto")
    if loader not in ("auto", "grub"):
        refuse(f"boot.loader {loader!r}: VAI installs GRUB and nothing else")
    if boot.get("password_hash"):
        refuse("boot.password_hash is not applied by this applier — the boot "
               "menu would be left unprotected")
    if boot.get("secure_boot") in (True, "true"):
        refuse("boot.secure_boot: Void ships no signed shim, and this applier "
               "produces no Secure-Boot-bootable system")
    if boot.get("uki"):
        refuse("boot.uki: VAI installs a GRUB kernel+initrd pair, not a unified "
               "kernel image")
    if boot.get("os_prober") is not None:
        warn("boot.os_prober is not applied — VAI wipes the disk it installs to, "
             "so there is nothing else on it to detect")

    layout = read_layout(doc)

    lines = ["#!/bin/sh",
             "# Generated by lis2void (Linux Installation Specification).",
             "#",
             "# This is a Void Linux VAI answer file: the autoinstaller in the live",
             "# ISO's initramfs fetches it from autourl= and sources it. Every name",
             "# below appears in that installer's own source.",
             ""]

    disks = target.get("disks", []) or []
    disk = ((disks[0] if disks else {}).get("match", {}) or {}).get("path") or "/dev/vda"
    lines.append(f"disk={q(disk)}")

    if layout.ok:
        boot_kib = parse_size(layout.boot.get("size", "1GiB")) // 1024
        swap_kib = parse_size(layout.swap.get("size", "2GiB")) // 1024
        lines.append(f'bootpartitionsize="{boot_kib}K"')
        lines.append(f'swapsize="{swap_kib}"')

    mirror = (doc.get("mirror", {}) or {}).get("url")
    lines.append(f'xbpsrepository={q(mirror or DEFAULT_REPO)}')

    packages = list(software.get("packages", []) or [])
    for app in software.get("apps", []) or []:
        if isinstance(app, str):
            packages.append(app)
        elif isinstance(app, dict):
            if name := (app.get("package") or app.get("name")):
                packages.append(name)
            if app.get("flatpak") or app.get("snap"):
                warn(f"software.apps['{app.get('name')}']: the native package is "
                     "installed; flatpak/snap alternatives are not")
    role = software.get("role", "minimal")
    if role not in ROLE_PACKAGES:
        refuse(f"software.role {role!r} has no Void package set")
    else:
        packages += ROLE_PACKAGES[role]
    packages += driver_packages(doc, "void")
    packages += shell_packages(doc)
    packages += security_packages(doc, "void")
    if kernel_pkg := check_kernel_variant(doc, {"lts": "linux-lts"}, "Void"):
        packages.append(kernel_pkg)
    if software.get("flatpak"):
        packages.append("flatpak")
    if layout.ok:
        overrides, fs_packages = storage_overrides(layout)
        packages += fs_packages
    else:
        overrides, fs_packages = [], []
    if packages:
        lines.append(f'pkgs={q(" ".join(dict.fromkeys(packages)))}')

    if hostname := system.get("hostname"):
        lines.append(f"hostname={q(hostname)}")
    if domain := system.get("domain"):
        warn(f"system.domain {domain!r} is not applied — VAI writes a short "
             "hostname only")
    lines.append(f'timezone={q(system.get("timezone", "UTC"))}')

    keymap = system.get("keymap", {}) or {}
    console = keymap.get("console")
    console_map = console or keymap.get("layout") or "us"
    if keymap.get("layout") and console and keymap["layout"] != console:
        warn(f"system.keymap.layout {keymap['layout']!r} is not applied — Void's "
             f"rc.conf takes one console keymap, and console {console!r} was "
             "declared")
    if variant := keymap.get("variant"):
        warn(f"system.keymap.variant {variant!r} is not applied — rc.conf's "
             "KEYMAP names a single loadkeys map")
    lines.append(f"keymap={q(console_map)}")

    locale = system.get("locale")
    if locale:
        lines.append(f"libclocale={q(locale)}")
    lines.append('end_action="func"')

    proxy = doc.get("proxy", {}) or {}
    for key, var in (("http", "http_proxy"), ("https", "https_proxy")):
        if url := proxy.get(key):
            lines.append(f"export {var}={q(url)}")
    if no_proxy := proxy.get("no_proxy"):
        lines.append(f"export no_proxy={q(','.join(no_proxy))}")

    lines.append("")
    lines.append('lis_tools="/lis-tools"')

    pre = [item.get("content") for stage in ("pre", "pre_install")
           for item in (doc.get("scripts", {}) or {}).get(stage, []) or []
           if item.get("content")]
    if pre:
        # The answer file is sourced at step 2, before VAI partitions anything,
        # so this is genuinely a pre-install hook rather than a re-homed one.
        lines += ["", "# scripts.pre_install — sourced before VAI touches the disk."]
        lines += pre

    lines.append(TOOLS_HELPER)
    lines += overrides

    # VAI's own step creates one account with a plaintext password and prompts
    # interactively when none is given; neither is compatible with SPEC §9/§2.4.
    lines += ["", "VAI_add_user() {",
              "    : # accounts are created by the post script below",
              "}"]

    params = list((boot.get("kernel") or {}).get("params", []) or [])
    serial = (boot.get("console") or {}).get("serial")
    if serial:
        params.append(f"console={serial}")
    ttys = []
    for param in params:
        if match := re.match(r"console=(tty(?:S|USB|AMA)\d+)", param):
            if match.group(1) in SERIAL_TTYS:
                ttys.append(match.group(1))
            else:
                warn(f"boot: no runit service for a getty on {match.group(1)}")
    if ttys:
        warn(f"a serial console is declared, so the getty service(s) "
             f"{', '.join('agetty-' + t for t in ttys)} are enabled in the target")

    post = post_script(doc, layout, params, ttys)
    if any(line.strip() == POST_DELIM for line in post):
        raise RuntimeError("post script contains the heredoc delimiter")

    lines += ["", "end_function() {",
              '    cp /etc/resolv.conf "${target}/etc/resolv.conf" 2>/dev/null || true',
              f'    cat > "${{target}}/lis-post.sh" <<\'{POST_DELIM}\'']
    lines += post
    lines += [POST_DELIM,
              '    chmod 0755 "${target}/lis-post.sh"',
              '    chroot "${target}" /bin/sh /lis-post.sh',
              '    rm -f "${target}/lis-post.sh"',
              "    sync",
              '    umount -R "${lis_tools}" 2>/dev/null || true',
              '    umount -R "${target}"',
              "    # end_action=func neither unmounts nor powers off; without this",
              "    # the machine sits in the initramfs forever.",
              "    poweroff -f",
              "}"]
    return "\n".join(lines) + "\n"


# ── VAI vocabulary self-audit ────────────────────────────────────────

def check_vai(path: pathlib.Path) -> int:
    """Assert every VAI name this applier emits exists in a shipped installer."""
    text = path.read_text()
    missing = [name for name in VAI_VARIABLES
               if not re.search(rf"(^|[^\w]){re.escape(name)}\b", text, re.M)]
    missing += [name for name in VAI_FUNCTIONS if f"{name}" not in text]
    for name in missing:
        print(f"MISSING from {path}: {name}", file=sys.stderr)
    if missing:
        print(f"\n{len(missing)} name(s) this applier emits do not exist in that "
              "autoinstaller — an answer file is sourced, so they would be "
              "silently inert.", file=sys.stderr)
        return 1
    print(f"ok: all {len(VAI_VARIABLES) + len(VAI_FUNCTIONS)} VAI names "
          f"lis2void emits are present in {path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Translate a LIS document into a Void Linux VAI answer file.")
    ap.add_argument("--check-vai", type=pathlib.Path, metavar="01-install.sh",
                    help="verify the VAI names this applier emits against a "
                         "shipped autoinstaller, then exit")
    if "--check-vai" in sys.argv:
        return check_vai(ap.parse_known_args()[0].check_vai)
    add_common_args(ap)
    args = ap.parse_args()

    doc = track(load_doc(args.file))
    check_version(doc, args.file)
    check_firmware(doc)
    check_unhandled(doc, ALL_SECTIONS)
    check_boot_extras(doc, {"loader", "timeout", "kernel", "console", "os_prober",
                            "password_hash", "secure_boot", "uki", "initramfs",
                            "params", "variant", "modules", "blacklist"})
    check_mirror(doc, {"url"})
    check_section_fields(doc, "desktop", set())
    check_section_fields(doc, "installer", set())
    check_section_fields(doc, "proxy", {"http", "https", "no_proxy"})
    check_keymap(doc, {"console", "layout", "variant", "font"})
    for entry in doc.get("keys", []) or []:
        refuse(f"keys['{entry.get('id')}']: this applier configures no hardware "
               "or cryptographic key material — storage encryption is refused")

    cfg = render(doc)
    check_encryption_emitted(doc, cfg, label="VAI answer file")

    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    out_file = args.out / "autoinstall.cfg"
    out_file.write_text(cfg)
    report(out_file)

    check_arch(doc, {"x86_64"})
    check_raid_consumers(doc)
    check_snapshots(doc, tools=frozenset(), boot_menu=False)
    check_script_fields(doc, honors_chroot=False, chroots_by_default=True)
    if (doc.get("scripts", {}) or {}).get("on_error"):
        refuse("scripts.on_error has no VAI equivalent — the installer aborts "
               "into the dracut emergency shell instead")
    check_unread(doc, ignore=APPLY_TIME_PATHS | {"storage.partitions[].mount_options",
                                                 "target.disks[].match.path"})
    return enforce(args.strict)


if __name__ == "__main__":
    sys.exit(main())
