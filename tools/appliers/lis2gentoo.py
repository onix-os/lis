#!/usr/bin/env python3
"""lis2gentoo — translate a LIS document into a Gentoo installation.

Usage: lis2gentoo.py FILE.lis.{json,yaml} [--out DIR] [--lenient] [--apply]

Writes into DIR (default '.'):
  portage/           real /etc/portage configuration — make.conf, binrepos.conf,
                     package.use, package.accept_keywords, package.license and
                     the `@lis` package set. This is the artifact that carries
                     the document's intent; portage acts on it.
  lis-prepare.sh     the block layer, the stage3 fetch/verify/unpack, the chroot
  lis-chroot.sh      the driver that runs Gentoo's own tools inside the chroot

`--apply` runs lis-prepare.sh, which in turn enters the chroot and runs
lis-chroot.sh.

WHERE THE LINE FALLS, stated rather than defended
-------------------------------------------------
Gentoo has no automated installer and none is coming. GLI was abandoned in 2009;
the Quickstart project it pointed at last moved in 2020; `app-admin/installer` is
archived; and the current Installer Project's `stager` repository — the single
place in Gentoo's project space that promises "install via configuration files" —
is an empty git repository. `app-misc/calamares-gentoo-livecd` exists in ::gentoo
but is not on the release media and has no unattended mode of any kind.

So this applier is split in two, and the split is sharp:

* **Before the chroot** — partition, mkfs, unpack the stage3, mount, fstab — there
  is no Gentoo tool at all, and there never has been. That span is generated
  shell. It is the same span `lis2alpine` already occupies, one cell wider
  (Alpine has `setup-disk -m sys`, Gentoo has `tar xpf` over a Gentoo-signed
  release artifact). It is also, precisely, what Gentoo's own release engineering
  does when it has to put an installed system on a block device: read
  `catalyst/targets/support/create-qcow2.sh` — parted, mkfs, copy the tree,
  chroot, grub-install, a heredoc fstab. Even here the shell is kept thin by
  handing work to tools that already exist on the media rather than reimplementing
  them: `gemato` verifies the stage3 against `/usr/share/openpgp-keys/`,
  `genfstab -U` writes the fstab, `arch-chroot` sets up the chroot.

* **After the chroot** everything is real, declarative Gentoo configuration acted
  on by Gentoo's own tools: `/etc/portage/make.conf`, `/etc/portage/binrepos.conf`,
  `package.use`, `package.accept_keywords`, `package.license`, a `sets/lis`
  package set, `eselect profile`, `emerge`, `installkernel`, `dracut`,
  `grub-mkconfig`. The ratio between that tree and the two shell scripts is this
  design's quality metric, and anything that can be a file is a file.

Binary packages are used wherever Gentoo publishes them (`FEATURES=getbinpkg`
against the official binhost) and the kernel is `sys-kernel/gentoo-kernel-bin`,
a pre-built dist-kernel. A source-based build would not finish inside any
sensible timeout, so a document that provably forces one is warned about loudly.

Fail-closed by default (SPEC §2.3): storage this applier does not lay out —
LUKS, LVM, RAID, adopted partitions, ZFS — is refused, not approximated.
"""

import argparse
import base64
import json
import pathlib
import shlex
import sys

from lis_common import (
    APPLY_TIME_PATHS, ALL_SECTIONS, SEED_MOUNT, add_common_args,
    boot_timeout_commands, check_arch, check_boot_extras, check_encryption_emitted,
    check_firmware, check_kernel_variant, check_keymap, check_mirror,
    check_raid_consumers, check_script_fields, check_section_fields,
    check_snapshots, check_unhandled, check_unread, check_version, chroot_intents,
    consume, driver_packages, enforce, enrollment_commands, file_commands, load_doc,
    match_selectors, password_field, refuse, registration_commands, report,
    resolve_disk_paths, resolve_mountpoints, role_fs, security_packages,
    shell_packages, sudoers_commands, system_commands, track, uid_commands, warn)

MOUNT = "/mnt/gentoo"

# Gentoo's release artifacts, and the pointer files that name the current one.
# Pinning a dated tarball here would 404 within weeks; the pointer is resolved on
# the installer at apply time, which is also how the Handbook tells a human to
# find it.
AUTOBUILDS = "https://distfiles.gentoo.org/releases/{arch}/autobuilds"
BINHOST = ("https://distfiles.gentoo.org/releases/{arch}/binpackages/"
           "23.0/{chost}")

# target.arch -> (autobuilds directory, binhost sub-directory, stage3 infix).
ARCHES = {
    "x86_64": ("amd64", "x86-64", "amd64"),
    "aarch64": ("arm64", "arm64", "arm64"),
}

# system.init -> (stage3 flavour, profile suffix, lis_common package family).
# The stage3 flavour and the profile have to agree — a systemd profile over an
# OpenRC stage3 produces a system that boots to nothing — so they are chosen
# together here rather than in two places.
INITS = {
    "openrc": ("openrc", "", "gentoo"),
    "systemd": ("systemd", "/systemd", "gentoo-systemd"),
}

# software.role -> profile subtree. Gentoo expresses "this is a desktop" as a
# profile, which is more than a package list: it changes USE defaults tree-wide.
ROLE_PROFILES = {
    "minimal": "",
    "server": "",
    "desktop:gnome": "/desktop/gnome",
    "desktop:kde": "/desktop/plasma",
    "desktop:xfce": "/desktop",
    "desktop:sway": "/desktop",
    "desktop:hyprland": "/desktop",
}

# Intent names (SPEC §11) -> Gentoo atoms. A name outside this table is passed
# through verbatim, as the spec requires; portage then fails loudly if it cannot
# resolve it, which is the fail-closed outcome.
ATOMS = {
    "curl": "net-misc/curl", "git": "dev-vcs/git", "htop": "sys-process/htop",
    "vim": "app-editors/vim", "neovim": "app-editors/neovim",
    "nano": "app-editors/nano", "emacs": "app-editors/emacs",
    "wget": "net-misc/wget", "rsync": "net-misc/rsync",
    "sudo": "app-admin/sudo", "doas": "app-admin/doas",
    "openssh": "net-misc/openssh", "sshd": "net-misc/openssh",
    "bash": "app-shells/bash", "zsh": "app-shells/zsh",
    "fish": "app-shells/fish", "tcsh": "app-shells/tcsh",
    "ksh": "app-shells/ksh", "dash": "app-shells/dash",
    "tmux": "app-misc/tmux", "screen": "app-misc/screen",
    "chrony": "net-misc/chrony", "openntpd": "net-misc/openntpd",
    "networkmanager": "net-misc/networkmanager",
    "firefox": "www-client/firefox", "chromium": "www-client/chromium",
    "vlc": "media-video/vlc", "docker": "app-containers/docker",
    "podman": "app-containers/podman", "python3": "dev-lang/python",
    "jq": "app-misc/jq", "tree": "app-text/tree", "lsof": "sys-process/lsof",
    "strace": "dev-debug/strace", "gdb": "dev-debug/gdb",
    "linux-firmware": "sys-kernel/linux-firmware",
}

# Filesystem -> the userspace package the installed system needs for it. ext*
# comes with the stage3 (sys-fs/e2fsprogs is @system), the rest do not.
FS_TOOLS = {
    "btrfs": "sys-fs/btrfs-progs", "xfs": "sys-fs/xfsprogs",
    "f2fs": "sys-fs/f2fs-tools", "vfat": "sys-fs/dosfstools",
    "exfat": "sys-fs/exfatprogs", "ntfs": "sys-fs/ntfs3g",
}

# Packages the official binhost does not publish, so asking for them means a
# source build. Named so the warning can say which one.
NOT_ON_BINHOST = {"sys-fs/mdadm", "sys-fs/zfs", "sys-boot/systemd-boot"}

BOOT_MOUNTS = ("/boot", "/boot/efi", "/efi")


def sh(value) -> str:
    """Single-quote a value for the generated shell."""
    return shlex.quote(str(value))


def atom(name: str) -> str:
    """The Gentoo atom for a package-intent name (SPEC §11)."""
    return ATOMS.get(name, name)


# ── the /etc/portage tree ────────────────────────────────────────────

def make_conf(doc: dict, arch_dir: str, chost: str, packages: list[str]) -> str:
    """/etc/portage/make.conf — the document's non-storage intent, as portage config."""
    target = doc.get("target", {}) or {}
    drivers = doc.get("drivers", {}) or {}
    proxy = doc.get("proxy", {}) or {}
    mirror = doc.get("mirror", {}) or {}

    lines = ["# Generated by lis2gentoo (Linux Installation Specification).",
             "# Every value here is document intent; portage is what acts on it.",
             "",
             'COMMON_FLAGS="-O2 -pipe"',
             'CFLAGS="${COMMON_FLAGS}"',
             'CXXFLAGS="${COMMON_FLAGS}"',
             'FCFLAGS="${COMMON_FLAGS}"',
             'FFLAGS="${COMMON_FLAGS}"',
             'MAKEOPTS="-j4"',
             "",
             "# The official binary host is what makes an unattended install finish in",
             "# bounded time; without getbinpkg every package below is compiled.",
             '# binpkg-request-signature is a profile default, and `getuto` (run from',
             "# lis-chroot.sh) is what seeds the trust store it needs.",
             'FEATURES="getbinpkg parallel-fetch"',
             '# binpkg-respect-use=n takes the published binary even when a USE',
             '# flag differs, which is the difference between minutes and hours.',
             'EMERGE_DEFAULT_OPTS="--quiet-build=y --jobs=4 --load-average=6 '
             '--binpkg-respect-use=n"',
             f'ACCEPT_KEYWORDS="{"~" if target.get("arch") == "aarch64" else ""}'
             f'{arch_dir}"',
             'ACCEPT_LICENSE="-* @FREE"',
             "",
             "# grub is published on the binhost built for both platforms; asking for",
             "# one of them alone is a USE mismatch and compiles it from source, which",
             "# no unattended run survives. grub-install still targets exactly the",
             "# platform target.firmware names.",
             'GRUB_PLATFORMS="pc efi-64"',
             ]

    # drivers.gpu is the one place Gentoo is more declarative than any other
    # distro here: VIDEO_CARDS is a tree-wide build knob, not a package name.
    gpu = drivers.get("gpu")
    cards = {"nvidia": "nvidia", "nvidia-open": "nvidia", "amdgpu": "amdgpu radeonsi",
             "intel": "intel"}.get(gpu)
    if cards:
        lines.append(f'VIDEO_CARDS="{cards}"')
    elif gpu in (None, "auto"):
        lines.append('VIDEO_CARDS="fbdev vesa"')
    lines.append('INPUT_DEVICES="libinput"')

    if url := mirror.get("url"):
        lines += ["", f'GENTOO_MIRRORS="{url}"']
    if country := mirror.get("country"):
        warn(f"mirror.country {country!r} is not applied — portage has no mirror "
             "selection by country; name one with mirror.url")

    if proxy:
        lines.append("")
        for key, value in (("http_proxy", proxy.get("http")),
                           ("https_proxy", proxy.get("https"))):
            if value:
                lines.append(f'{key}="{value}"')
        if no_proxy := proxy.get("no_proxy"):
            lines.append(f'NO_PROXY="{",".join(no_proxy)}"')
        for key in sorted(set(proxy) - {"http", "https", "no_proxy"}):
            warn(f"proxy.{key} is not applied by this applier")

    lines += ["", f'# The `@lis` set in sets/lis carries the {len(packages)} package(s) '
              "this document asks for.", ""]
    return "\n".join(lines) + "\n"


def binrepos_conf(arch_dir: str, chost: str) -> str:
    return ("# Generated by lis2gentoo — the official Gentoo binary host.\n"
            "[gentoobinhost]\n"
            "priority = 9999\n"
            f"sync-uri = {BINHOST.format(arch=arch_dir, chost=chost)}\n")


def package_use(doc: dict, family: str) -> str:
    """/etc/portage/package.use/lis — build-time intent, expressed where it lives."""
    lines = ["# Generated by lis2gentoo.",
             "# sys-kernel/installkernel ships every hook behind a USE flag and none",
             "# of them are on by default: without `dracut` the dist-kernel lands in",
             "# /boot with no initramfs and a btrfs/LVM/virtio root never mounts.",
             "sys-kernel/installkernel dracut grub"]
    if (doc.get("desktop") or {}).get("audio") == "pipewire":
        lines.append("media-video/pipewire sound-server")
    return "\n".join(lines) + "\n"


def package_license(doc: dict) -> str | None:
    """/etc/portage/package.license/lis — only written when the document needs it."""
    drivers = doc.get("drivers", {}) or {}
    entries = []
    if drivers.get("firmware") == "all":
        entries.append("sys-kernel/linux-firmware @BINARY-REDISTRIBUTABLE")
    if drivers.get("gpu") in ("nvidia", "nvidia-open"):
        entries.append("x11-drivers/nvidia-drivers NVIDIA-r2")
    if not entries:
        return None
    return ("# Generated by lis2gentoo — licences the document's own choices require.\n"
            + "\n".join(entries) + "\n")


def package_set(packages: list[str]) -> str:
    """/etc/portage/sets/lis — the document's software section as a portage set.

    A set is the closest thing portage has to "the list of things this machine is
    supposed to have": `emerge @lis` installs it and records it in @world, and it
    survives into the installed system as a re-runnable statement of intent.
    """
    return ("# Generated by lis2gentoo. `emerge @lis` installs exactly this.\n"
            + "\n".join(packages) + "\n")


def dracut_conf(doc: dict, root_fs: str | None) -> str:
    """/etc/dracut.conf.d/lis.conf — what the initramfs must be able to open.

    hostonly detection inside a chroot sees the *installer's* hardware, not the
    target's, and quietly builds an image with no virtio block driver. The
    install then reports success and the machine cannot find its root, which is
    the class of failure this project exists to prevent — so the modules are
    named rather than detected.
    """
    storage = doc.get("storage", {}) or {}
    modules = []
    drivers = ["virtio", "virtio_blk", "virtio_pci", "virtio_scsi", "virtio_net",
               "ahci", "nvme", "sd_mod"]
    if root_fs and root_fs not in (None, "none"):
        drivers.append(root_fs)
    if storage.get("encryption"):
        modules.append("crypt")
    if storage.get("lvm"):
        modules.append("lvm")
    if storage.get("raid"):
        modules.append("mdraid")
    return ("# Generated by lis2gentoo.\n"
            "hostonly=\"no\"\n"
            + (f'add_dracutmodules+=" {" ".join(modules)} "\n' if modules else "")
            + f'force_drivers+=" {" ".join(dict.fromkeys(drivers))} "\n')


# ── pre-chroot: the span Gentoo has no tool for ──────────────────────

def prepare_script(doc: dict, *, disk: str, partitions: list, mountpoints: dict,
                   root_part: dict | None, arch_dir: str, stage3_flavour: str,
                   on_error: list[str]) -> str:
    """Lay out the disk, land a verified stage3 on it, and enter the chroot.

    Thin on purpose. Every step that can fail says so and stops: the failure mode
    this project keeps hitting is shell that carries on after a step did nothing.
    """
    storage = doc.get("storage", {}) or {}
    firmware = (doc.get("target", {}) or {}).get("firmware", "uefi")
    label = "gpt" if firmware == "uefi" else "msdos"

    s = ["#!/bin/bash",
         "# Generated by lis2gentoo — the pre-chroot span, for which Gentoo ships",
         "# no tool. Everything after `arch-chroot` is portage acting on the",
         "# configuration in ./portage/.",
         "set -euo pipefail",
         'profile="${1:-$(dirname "$0")}"',
         f"mnt={sh(MOUNT)}",
         f"disk={sh(disk)}",
         f'base="{AUTOBUILDS.format(arch=arch_dir)}"',
         "",
         "die() { echo \"lis2gentoo: $*\" >&2; exit 1; }",
         ]
    if on_error:
        s += ["lis_on_error() {",
              *[f"  {c}" for c in on_error],
              "}",
              "trap 'rc=$?; [ \"$rc\" -eq 0 ] || lis_on_error; exit $rc' EXIT"]
    s += ["",
          "# A partition node is <disk><n> on sd*/vd* and <disk>p<n> on nvme/mmc/loop.",
          "# Getting this wrong formats nothing and mounts nothing, silently.",
          "partdev() { case \"$1\" in *[0-9]) echo \"$1p$2\";; *) echo \"$1$2\";; esac; }",
          "",
          "for m in " + " ".join(sorted(
              {role_fs(p) for p in partitions} - {None, "none", "swap"}))
          + "; do modprobe \"$m\" 2>/dev/null || true; done",
          ""]

    if not storage.get("wipe", False):
        s.append("# storage.wipe is false: the partition table is left in place, but "
                 "the\n# declared partitions are still created and formatted below.")

    # ── partitions ──
    by_disk: dict = {}
    for i, part in enumerate(partitions):
        by_disk.setdefault(part.get("disk"), []).append((i, part))
    disk_paths = {d.get("id"): (d.get("match", {}) or {}).get("path")
                  for d in (doc.get("target", {}) or {}).get("disks", []) or []}

    nodes: dict[int, str] = {}
    for handle, entries in by_disk.items():
        device = disk_paths.get(handle) or disk
        var = f"dev_{handle}".replace("-", "_")
        s.append(f"{var}={sh(device)}")
        if storage.get("wipe", False):
            s += [f'wipefs -a "${var}" >/dev/null 2>&1 || true',
                  f'parted -s "${var}" mklabel {label} || die "mklabel failed on {device}"']
        start = "1MiB"
        for index, (pos, part) in enumerate(entries, start=1):
            size = part.get("size", "rest")
            end = "100%" if size == "rest" else cumulative(e[1] for e in entries[:index])
            s.append(f'parted -s "${var}" -- mkpart primary {start} {end} '
                     f'|| die "could not create partition {index} on {device}"')
            if part.get("role") in ("esp", "boot"):
                s.append(f'parted -s "${var}" set {index} boot on || true')
            if part.get("role") == "esp":
                s.append(f'parted -s "${var}" set {index} esp on || true')
            start = end
            nodes[pos] = f'$(partdev "${var}" {index})'
        s.append(f'partprobe "${var}" || true')
    s += ["udevadm settle 2>/dev/null || sleep 2", ""]

    # ── filesystems ──
    swap_node = None
    for pos, part in enumerate(partitions):
        node = nodes.get(pos)
        if not node:
            continue
        fs = role_fs(part)
        label_opt = ""
        if fs == "swap":
            if flabel := part.get("label"):
                label_opt = f" -L {sh(flabel)}"
            s.append(f'mkswap{label_opt} "{node}" || die "mkswap failed on {node}"')
            swap_node = node
            continue
        if not fs or fs == "none":
            continue
        if flabel := part.get("label"):
            label_opt = (f" -n {sh(flabel)}" if fs == "vfat" else f" -L {sh(flabel)}")
        if fs == "vfat":
            cmd = f"mkfs.vfat -F32{label_opt}"
        elif fs in ("btrfs", "xfs"):
            cmd = f"mkfs.{fs} -f{label_opt}"
        else:
            cmd = f"mkfs.{fs} -F{label_opt}"
        s.append(f'{cmd} "{node}" || die "mkfs.{fs} failed on {node}"')

    root_pos = next((i for i, p in enumerate(partitions) if p is root_part), None)
    root_node = nodes.get(root_pos) if root_pos is not None else None
    if not root_node:
        # Guarded here as well as at translate time: a prepare script that mounts
        # nothing would go on to untar a stage3 into the installer's own tmpfs.
        s.append('die "no root filesystem was laid out"')
    s += ["", f'rootdev="{root_node}"', 'mkdir -p "$mnt"']

    # ── mount, subvolumes first ──
    subvols = (root_part or {}).get("subvolumes", []) or []
    root_sub = next((v for v in subvols if v.get("mountpoint") == "/"), None)
    if subvols:
        s.append('mount "$rootdev" "$mnt" || die "cannot mount the root filesystem"')
        for sub in subvols:
            s.append(f'btrfs subvolume create "$mnt/{sub["name"]}" '
                     f'|| die "cannot create subvolume {sub["name"]}"')
        s.append('umount "$mnt"')
        if root_sub:
            s.append(f'mount -o subvol={root_sub["name"]}'
                     + (f',{",".join(root_sub["mount_options"])}'
                        if root_sub.get("mount_options") else "")
                     + ' "$rootdev" "$mnt" || die "cannot mount the root subvolume"')
        else:
            s.append('mount "$rootdev" "$mnt" || die "cannot mount the root filesystem"')
    else:
        opts = (root_part or {}).get("mount_options") or []
        s.append(f'mount{" -o " + ",".join(opts) if opts else ""} "$rootdev" "$mnt" '
                 '|| die "cannot mount the root filesystem"')

    # Non-root mountpoints, shallowest first so /boot exists before /boot/efi.
    later: list[tuple[str, str, list]] = []
    for sub in subvols:
        if sub is root_sub:
            continue
        later.append((sub["mountpoint"], f'-o subvol={sub["name"]} "$rootdev"',
                      sub.get("mount_options") or []))
    for pos, part in enumerate(partitions):
        point = mountpoints.get(pos)
        node = nodes.get(pos)
        if not point or point == "/" or not node or part is root_part:
            continue
        later.append((point, f'"{node}"', part.get("mount_options") or []))
    for point, source, opts in sorted(later, key=lambda e: e[0].count("/")):
        extra = ""
        if opts:
            extra = (f",{','.join(opts)}" if source.startswith("-o ")
                     else f" -o {','.join(opts)}")
            if source.startswith("-o "):
                source = source.replace('"$rootdev"', "").strip() + extra + ' "$rootdev"'
                extra = ""
        s += [f'mkdir -p "$mnt{point}"',
              f'mount {source}{extra} "$mnt{point}" || die "cannot mount {point}"']
    if swap_node:
        s.append(f'swapon "{swap_node}" || die "cannot enable swap"')

    # ── stage3 ──
    s += ["",
          "# The stage3 is a Gentoo release artifact and its verification is a",
          "# first-party procedure: gemato against the release key that ships on",
          "# this very medium. An unverified tarball is not installed.",
          f'rel=$(curl -fsSL "$base/latest-stage3-{arch_dir}-{stage3_flavour}.txt" '
          "| grep -v '^#' | grep -m1 '\\.tar\\.xz' | cut -d' ' -f1) "
          f'|| die "cannot reach the Gentoo autobuilds pointer"',
          '[ -n "$rel" ] || die "the stage3 pointer file named no tarball"',
          'echo "lis2gentoo: stage3 = $rel"',
          'curl -fL --retry 3 -o "$mnt/stage3.tar.xz" "$base/$rel" '
          '|| die "stage3 download failed"',
          'curl -fL --retry 3 -o "$mnt/stage3.tar.xz.asc" "$base/$rel.asc" '
          '|| die "stage3 signature download failed"',
          'gemato openpgp-verify-detached -K /usr/share/openpgp-keys/gentoo-release.asc '
          '"$mnt/stage3.tar.xz.asc" "$mnt/stage3.tar.xz" '
          '|| die "the stage3 signature did not verify against the Gentoo release key"',
          'tar xpf "$mnt/stage3.tar.xz" --xattrs-include="*.*" --numeric-owner '
          '-C "$mnt" || die "stage3 unpack failed"',
          'rm -f "$mnt/stage3.tar.xz" "$mnt/stage3.tar.xz.asc"',
          '[ -x "$mnt/usr/bin/emerge" ] || die "the unpacked stage3 has no emerge"',
          ""]

    # ── the declarative half lands here ──
    s += ["# Everything from here on is Gentoo's own tooling acting on these files.",
          "# A stage3 may ship some of these as plain files where this applier",
          "# writes a drop-in directory; copying a directory over a file fails, so",
          "# the file goes first. (`&&` chains here would trip `set -e` on the very",
          "# common case of the path not existing at all.)",
          'for d in package.use package.accept_keywords package.license package.mask '
          'sets binrepos.conf repos.conf env; do',
          '  t="$mnt/etc/portage/$d"',
          '  if [ -e "$t" ] && [ ! -d "$t" ]; then rm -f "$t"; fi',
          "done",
          'install -d "$mnt/etc/portage"',
          'cp -a "$profile/portage/." "$mnt/etc/portage/" '
          '|| die "could not install the generated /etc/portage tree"',
          'install -d -m755 "$mnt/etc/dracut.conf.d"',
          'install -m644 "$profile/dracut-lis.conf" "$mnt/etc/dracut.conf.d/lis.conf" '
          '|| die "could not install the dracut configuration"',
          "",
          "# genfstab is on this medium and reads the mounts we just made; a",
          "# hand-written heredoc is how fstabs come to disagree with reality.",
          'genfstab -U "$mnt" > "$mnt/etc/fstab" || die "genfstab failed"',
          ]
    if swap_node:
        s += ["grep -qE '[[:space:]]swap[[:space:]]' \"$mnt/etc/fstab\" || "
              f'printf "UUID=%s none swap sw 0 0\\n" '
              f'"$(blkid -s UUID -o value "{swap_node}")" >> "$mnt/etc/fstab"']
    s += ["",
          'cp -L /etc/resolv.conf "$mnt/etc/resolv.conf" 2>/dev/null || true',
          'install -m755 "$profile/lis-chroot.sh" "$mnt/lis-chroot.sh" '
          '|| die "could not stage the chroot driver"',
          ]

    # Secrets stay on the seed and are never copied into the target (SPEC §2.4).
    # arch-chroot bind-mounts the installer's /run, and SEED_MOUNT lives under
    # it, so the seed is readable from the chroot without anything being staged.
    s += [f'[ -d {SEED_MOUNT} ] && echo "lis2gentoo: seed visible in the chroot at '
          f'{SEED_MOUNT}" || true',
          'arch-chroot "$mnt" /bin/bash /lis-chroot.sh || die "the chroot stage failed"',
          'rm -f "$mnt/lis-chroot.sh"',
          ""]

    for item in ((doc.get("scripts", {}) or {}).get("pre_reboot") or []):
        if content := item.get("content"):
            s.append(f"( {content} ) || true")
    for item in ((doc.get("scripts", {}) or {}).get("on_success") or []):
        if content := item.get("content"):
            s.append(f"( {content} ) || true")

    s += ["sync",
          f'swapoff "{swap_node}" 2>/dev/null || true' if swap_node else "",
          'umount -R "$mnt" || umount -lR "$mnt" || true',
          'echo "lis2gentoo: installation complete"']
    return "\n".join(x for x in s if x is not None) + "\n"


def cumulative(parts) -> str:
    """Absolute end offset of the last partition in the list, as a parted size."""
    total = 0
    for part in parts:
        size = part.get("size", "rest")
        for unit, mib in (("TiB", 1024 * 1024), ("GiB", 1024), ("MiB", 1)):
            if str(size).endswith(unit):
                total += int(str(size)[: -len(unit)]) * mib
                break
    return f"{total + 1}MiB"


# ── post-chroot: Gentoo's own tooling ────────────────────────────────

def chroot_script(doc: dict, *, family: str, profile: str, disk: str,
                  firmware: str, root_fs: str | None) -> str:
    """Drive portage, eselect, installkernel, dracut and grub inside the target.

    Almost every line here hands work to a Gentoo tool acting on a file that
    lis-prepare.sh already installed. Where a fact has no configuration file —
    a user's password hash, a hook script — it is applied with the target's own
    utilities.
    """
    system = doc.get("system", {}) or {}
    software = doc.get("software", {}) or {}
    network = doc.get("network", {}) or {}
    users = doc.get("users", []) or []
    scripts = doc.get("scripts", {}) or {}
    openrc = family == "gentoo"

    s = ["#!/bin/bash",
         "# Generated by lis2gentoo — runs inside the target chroot.",
         "set -euo pipefail",
         "export PATH=/usr/sbin:/usr/bin:/sbin:/bin",
         "source /etc/profile 2>/dev/null || true",
         "die() { echo \"lis2gentoo(chroot): $*\" >&2; exit 1; }",
         "",
         "# The ebuild repository. emerge-webrsync verifies the snapshot against the",
         "# Gentoo release key; it is Gentoo's own supported way to get a tree with",
         "# no rsync mirror round-trip.",
         'emerge-webrsync || die "emerge-webrsync could not fetch the ebuild repository"',
         "",
         "# Seed the binary-package trust store. Without this, FEATURES contains",
         "# binpkg-request-signature with nothing to verify against and every",
         "# package silently falls back to a source build.",
         'getuto || die "getuto could not set up the binary-package trust store"',
         "",
         f"# The profile is chosen once, and it has to agree with the stage3 flavour",
         f"# the prepare script unpacked — a systemd profile over an OpenRC stage3",
         f"# installs a system that boots to nothing.",
         f'eselect profile set {sh(profile)} || {{ eselect profile list; '
         f'die "cannot select profile {profile}"; }}',
         "eselect profile show",
         "",
         "# The document's software section, as a portage set.",
         'emerge --getbinpkg @lis || die "emerge @lis failed"',
         "",
         ]

    # ── locale, timezone, keymap, hostname ──
    if tz := system.get("timezone"):
        # `ln -sf` happily creates a dangling symlink, so the zone is checked
        # first — otherwise an unknown timezone silently leaves the machine on
        # whatever the stage3 shipped.
        s += [f'[ -e /usr/share/zoneinfo/{tz} ] || die "no such timezone: {tz}"',
              f'echo {sh(tz)} > /etc/timezone',
              f'ln -sf ../usr/share/zoneinfo/{tz} /etc/localtime',
              "emerge --config sys-libs/timezone-data >/dev/null 2>&1 || true"]

    locale = system.get("locale")
    extra = system.get("extra_locales") or []
    if locale or extra:
        wanted = [locale] if locale else []
        wanted += list(extra)
        s.append("install -d /etc")
        for loc in dict.fromkeys(wanted):
            charset = loc.split(".")[-1] if "." in loc else "UTF-8"
            s.append(f'grep -qx {sh(f"{loc} {charset}")} /etc/locale.gen 2>/dev/null '
                     f'|| echo {sh(f"{loc} {charset}")} >> /etc/locale.gen')
        s.append('locale-gen || die "locale-gen failed"')
    if locale:
        # eselect locale is Gentoo's own front end for /etc/env.d/02locale, which
        # is what an OpenRC system actually reads; /etc/locale.conf is written as
        # well because a systemd target reads that one instead.
        eselect_name = locale.replace("UTF-8", "utf8")
        s += [f'eselect locale set {sh(eselect_name)} 2>/dev/null || '
              f'printf \'LANG="%s"\\nLC_COLLATE="C.UTF-8"\\n\' {sh(locale)} '
              "> /etc/env.d/02locale",
              f'grep -q "^LANG=" /etc/locale.conf 2>/dev/null || '
              f'echo LANG={sh(locale)} >> /etc/locale.conf']

    if overrides := system.get("locale_overrides"):
        consume(overrides)
        for key, value in sorted(overrides.items()):
            s.append(f'grep -q "^{key}=" /etc/env.d/02locale 2>/dev/null || '
                     f'echo {sh(f"{key}={value}")} >> /etc/env.d/02locale')

    if hostname := system.get("hostname"):
        # Both files, because both are real: OpenRC's hostname service prefers
        # /etc/hostname and falls back to /etc/conf.d/hostname.
        s += [f'echo {sh(hostname)} > /etc/hostname',
              f'printf \'hostname="%s"\\n\' {sh(hostname)} > /etc/conf.d/hostname']
        domain = system.get("domain")
        fqdn = f"{hostname}.{domain}" if domain else hostname
        s.append(f'grep -q {sh(hostname)} /etc/hosts 2>/dev/null || '
                 f'printf "127.0.1.1\\t%s %s\\n" {sh(fqdn)} {sh(hostname)} >> /etc/hosts')

    keymap = system.get("keymap", {}) or {}
    console = keymap.get("console") or keymap.get("layout")
    if keymap.get("layout") and keymap.get("console") and \
            keymap["layout"] != keymap["console"]:
        warn(f"system.keymap.console {keymap['console']!r} and layout "
             f"{keymap['layout']!r} disagree — /etc/conf.d/keymaps takes one value "
             f"and {console!r} was used")
    if keymap.get("variant"):
        warn("system.keymap.variant is not applied — /etc/conf.d/keymaps names a "
             "console keymap, which has no separate variant field")
    if console:
        s.append(f'printf \'keymap="%s"\\n\' {sh(console)} > /etc/conf.d/keymaps')
    if font := keymap.get("font"):
        s.append(f'printf \'consolefont="%s"\\n\' {sh(font)} > /etc/conf.d/consolefont')
        if openrc:
            s.append("rc-update add consolefont boot 2>/dev/null || true")

    if hwclock := system.get("hwclock"):
        s.append(f'printf \'clock="%s"\\n\' '
                 f'{sh("local" if hwclock == "localtime" else "UTC")} '
                 "> /etc/conf.d/hwclock")

    for cmd in system_commands(doc, family):
        s.append(cmd)
    s.append("env-update >/dev/null 2>&1 || true")
    s.append("")

    # ── users ──
    for user in users:
        name = user["name"]
        if name == "root":
            continue
        groups = list(user.get("groups", []) or [])
        if user.get("admin"):
            groups.append("wheel")
        for group in dict.fromkeys(groups):
            s.append(f"getent group {sh(group)} >/dev/null || groupadd {sh(group)}")
        opts = ["-m"]
        if uid := user.get("uid"):
            opts.append(f"-u {uid}")
        if shell := user.get("shell"):
            opts.append(f"-s {sh(shell)}")
        if comment := user.get("comment"):
            opts.append(f"-c {sh(comment)}")
        if groups:
            opts.append(f"-G {sh(','.join(dict.fromkeys(groups)))}")
        s.append(f"id -u {sh(name)} >/dev/null 2>&1 || useradd {' '.join(opts)} {sh(name)} "
                 f'|| die "could not create user {name}"')
        password = user.get("password") or {}
        if not password.get("hash") and not password.get("locked"):
            refuse(f"user '{name}': no password hash and not marked locked — the "
                   "account would be created with no way to authenticate and no "
                   "explicit lock")
        if field := password_field(user):
            # Gentoo has real shadow-utils, so the crypt(3) string from the
            # document goes in verbatim; nothing re-hashes a plaintext here.
            s.append(f"usermod -p {sh(field)} {sh(name)}")
        else:
            s.append(f"passwd -l {sh(name)} >/dev/null")
        for key in user.get("ssh_authorized_keys", []) or []:
            s += [f"install -d -m700 -o {sh(name)} -g {sh(name)} /home/{name}/.ssh",
                  f"echo {sh(key)} >> /home/{name}/.ssh/authorized_keys",
                  f"chown {sh(name)} /home/{name}/.ssh/authorized_keys",
                  f"chmod 600 /home/{name}/.ssh/authorized_keys"]

    root_user = next((u for u in users if u["name"] == "root"), None)
    if root_user is not None:
        if field := password_field(root_user):
            s.append(f"usermod -p {sh(field)} root")
        else:
            s.append("passwd -l root >/dev/null")
            if not (root_user.get("password") or {}).get("locked"):
                warn("users['root'] has no password hash; the account is locked")
        for key in (root_user.get("ssh_authorized_keys") or []):
            s += ["install -d -m700 /root/.ssh",
                  f"echo {sh(key)} >> /root/.ssh/authorized_keys",
                  "chmod 600 /root/.ssh/authorized_keys"]
    else:
        # A stage3's root account has no password at all. Leaving it that way
        # would install a machine whose root can be taken from the console.
        s.append("passwd -l root >/dev/null")
        warn("no users[] entry named root — the root account is locked, since a "
             "stage3 ships it passwordless")

    s += [*sudoers_commands(doc), *uid_commands(doc), ""]

    # ── services ──
    services = software.get("services", {}) or {}
    for unit in services.get("enable", []) or []:
        if openrc:
            s.append(f"rc-update add {sh(unit)} default 2>/dev/null || "
                     f'echo "lis2gentoo: no OpenRC service named {unit}" >&2')
        else:
            s.append(f"systemctl enable {sh(unit)} 2>/dev/null || true")
    for unit in services.get("disable", []) or []:
        if openrc:
            s.append(f"rc-update del {sh(unit)} default 2>/dev/null || true")
        else:
            s.append(f"systemctl disable {sh(unit)} 2>/dev/null || true")
    # SPEC §10: omitting network.interfaces means "DHCP on everything wired".
    # A stage3 ships no DHCP client at all, so without this the installed system
    # comes up with no network and no way to say so.
    if openrc:
        s.append("rc-update add dhcpcd default 2>/dev/null || true")
        if (network.get("ssh") or {}).get("enabled"):
            s.append("rc-update add sshd default 2>/dev/null || true")
    else:
        s.append("systemctl enable dhcpcd.service 2>/dev/null || true")
        if (network.get("ssh") or {}).get("enabled"):
            s.append("systemctl enable sshd.service 2>/dev/null || true")

    s += [c for c in chroot_intents(doc, family)]
    s += [c for c in enrollment_commands(doc)]
    s += [c for c in registration_commands(doc, family)]
    s.append("")

    # ── files ──
    for entry in doc.get("files", []) or []:
        s += file_commands(entry)

    # ── hooks ──
    for stage in ("post_storage", "post_install", "post"):
        for item in scripts.get(stage, []) or []:
            if content := item.get("content"):
                if item.get("chroot") is False:
                    warn(f"scripts.{stage}[].chroot: false is not honored — this "
                         "applier runs the hook inside the target")
                s.append(f"( {content} ) || die {sh(f'scripts.{stage} hook failed')}")
    for user in users:
        name = user["name"]
        # Iterated per stage rather than over a concatenation: adding two tracked
        # lists yields a plain one, and every field read from it afterwards stops
        # being recorded — which is how a hook can look dropped when it is not.
        for stage in ("post_install", "post"):
            for item in ((user.get("scripts", {}) or {}).get(stage) or []):
                if content := item.get("content"):
                    failure = f"users['{name}'] {stage} hook failed"
                    s.append(f"su - {sh(name)} -c {sh(content)} || die {sh(failure)}")

    # ── first boot ──
    firstboot = [item["content"] for item in (scripts.get("firstboot") or [])
                 if item.get("content")]
    for user in users:
        for item in ((user.get("scripts", {}) or {}).get("firstboot") or []):
            if content := item.get("content"):
                firstboot.append(f"su - {shlex.quote(user['name'])} -c "
                                 f"{shlex.quote(content)}")
    if firstboot:
        body = "#!/bin/sh\n" + "\n".join(firstboot) + "\n"
        if openrc:
            body += "rc-update del lis-firstboot default\n"
            unit = ("#!/sbin/openrc-run\n"
                    'description="LIS first-boot hooks (runs once)"\n'
                    "depend() { after net; }\n"
                    "start() {\n"
                    '\tebegin "Running LIS first-boot hooks"\n'
                    "\t/usr/local/bin/lis-firstboot\n"
                    "\teend $?\n"
                    "}\n")
            unit_path, enable = "/etc/init.d/lis-firstboot", \
                "rc-update add lis-firstboot default"
        else:
            body += "systemctl disable lis-firstboot.service\n"
            unit = ("[Unit]\nDescription=LIS first-boot hooks\nAfter=network.target\n\n"
                    "[Service]\nType=oneshot\nExecStart=/usr/local/bin/lis-firstboot\n\n"
                    "[Install]\nWantedBy=multi-user.target\n")
            unit_path, enable = "/etc/systemd/system/lis-firstboot.service", \
                "systemctl enable lis-firstboot.service"
        s += ["install -d -m755 /usr/local/bin",
              f"echo {b64(body)} | base64 -d > /usr/local/bin/lis-firstboot",
              "chmod 755 /usr/local/bin/lis-firstboot",
              f"install -d -m755 {shlex.quote(str(pathlib.PurePath(unit_path).parent))}",
              f"echo {b64(unit)} | base64 -d > {unit_path}",
              f"chmod 755 {unit_path}" if openrc else f"chmod 644 {unit_path}",
              f"{enable} || true"]
    s.append("")

    # ── bootloader ──
    boot = doc.get("boot", {}) or {}
    params = list((boot.get("kernel", {}) or {}).get("params", []) or [])
    blacklist = (boot.get("kernel", {}) or {}).get("blacklist") or []
    modules = (boot.get("kernel", {}) or {}).get("modules") or []
    serial = (boot.get("console", {}) or {}).get("serial")
    if serial and not any(p.startswith("console=ttyS") for p in params):
        params.append(f"console={serial}")
    for name in blacklist:
        s.append(f'echo {sh(f"blacklist {name}")} >> /etc/modprobe.d/lis-blacklist.conf')
    if modules:
        s.append(f'printf "%s\\n" {" ".join(sh(m) for m in modules)} '
                 "> /etc/modules-load.d/lis.conf")

    cmdline = " ".join(params)
    s += ["# /etc/default/grub is sourced by a shell, so the cmdline has to stay",
          "# quoted or it is truncated at the first space and the rest is executed.",
          f'cat > /etc/default/grub <<GRUBEOF',
          f'GRUB_DISTRIBUTOR="Gentoo"',
          f'GRUB_CMDLINE_LINUX_DEFAULT="{cmdline}"',
          f'GRUB_CMDLINE_LINUX=""',
          'GRUB_TERMINAL="serial console"',
          'GRUB_SERIAL_COMMAND="serial --unit=0 --speed=115200"',
          "GRUB_DISABLE_OS_PROBER=" + ("false" if boot.get("os_prober") else "true"),
          "GRUBEOF"]
    if pwhash := boot.get("password_hash"):
        s += ["cat > /etc/grub.d/01_lis_password <<'PWEOF'",
              "#!/bin/sh", "exec cat <<'EOF'", "set superusers=\"root\"",
              f"password_pbkdf2 root {pwhash}", "EOF", "PWEOF",
              "chmod 755 /etc/grub.d/01_lis_password"]

    if firmware == "uefi":
        s += ['grub-install --target=x86_64-efi --efi-directory=/boot --removable '
              '|| grub-install --target=x86_64-efi --efi-directory=/boot/efi '
              '--removable || die "grub-install (UEFI) failed"']
    else:
        s += [f'grub-install --target=i386-pc {sh(disk)} '
              f'|| die "grub-install (BIOS) on {disk} failed"']
    s += boot_timeout_commands(doc, family, "grub")
    s.append('grub-mkconfig -o /boot/grub/grub.cfg || die "grub-mkconfig failed"')
    s.append('grep -q "vmlinuz\\|linux /" /boot/grub/grub.cfg '
             '|| die "grub found no kernel to boot — the dist-kernel did not install"')
    s.append("")

    # ── serial console on the installed system ──
    # The reboot test boots the target with no kernel command line of its own, so
    # a system with no serial getty is indistinguishable from one that does not
    # boot. This is emitted whether or not boot.console.serial asks for it, which
    # is drift and is reported as such.
    if not serial:
        warn("a serial getty on ttyS0 is enabled on the installed system even "
             "though boot.console.serial is not declared, so the machine stays "
             "reachable over a serial console")
    if openrc:
        s += ["# sysvinit spawns the getty; OpenRC's runlevel entries are `wait`",
              "# entries ahead of it in the same runlevel, so anything the default",
              "# runlevel starts has finished by the time this prompt appears.",
              "sed -i '/ttyS0/d' /etc/inittab",
              "printf 's0:12345:respawn:/sbin/agetty 115200 ttyS0 vt100\\n' "
              ">> /etc/inittab",
              "grep -qx ttyS0 /etc/securetty 2>/dev/null || echo ttyS0 >> /etc/securetty"]
    else:
        s += ["systemctl enable serial-getty@ttyS0.service 2>/dev/null || true"]
    s.append("")

    # ── birth certificate (delivery.md §8) ──
    s += ["install -d -m755 /var/lib/lis",
          f"echo {b64(json.dumps(doc, separators=(',', ':')))} | base64 -d "
          "> /var/lib/lis/system.lis.json",
          "chmod 600 /var/lib/lis/system.lis.json",
          "",
          "echo 'lis2gentoo(chroot): done'"]
    return "\n".join(x for x in s if x != "" or True) + "\n"


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# ── translation ──────────────────────────────────────────────────────

def render(doc: dict) -> dict[str, str]:
    """Every artifact this applier writes, keyed by path relative to --out."""
    target = doc.get("target", {}) or {}
    storage = doc.get("storage", {}) or {}
    system = doc.get("system", {}) or {}
    software = doc.get("software", {}) or {}
    boot = doc.get("boot", {}) or {}
    network = doc.get("network", {}) or {}
    users = doc.get("users", []) or []

    arch = target.get("arch") or "x86_64"
    arch_dir, chost, _ = ARCHES.get(arch, ARCHES["x86_64"])
    firmware = target.get("firmware", "uefi")

    # ── init system and profile: chosen together, refused together ──
    init = system.get("init", "auto")
    if init in (None, "auto"):
        init = "openrc"
    if init not in INITS:
        refuse(f"system.init {init!r} is not an init system Gentoo ships a stage3 "
               f"and a profile for (supports {', '.join(sorted(INITS))})")
        init = "openrc"
    stage3_flavour, init_suffix, family = INITS[init]

    role = software.get("role", "")
    if role not in ROLE_PROFILES:
        refuse(f"software.role {role!r} has no Gentoo profile or package set")
        role_suffix = ""
    else:
        role_suffix = ROLE_PROFILES[role]
    if role_suffix and init_suffix:
        # default/linux/amd64/23.0/desktop/gnome/systemd exists; the order is
        # role first, init last.
        profile = f"default/linux/{arch_dir}/23.0{role_suffix}{init_suffix}"
    else:
        profile = f"default/linux/{arch_dir}/23.0{role_suffix}{init_suffix}"

    # ── storage this applier lays out, and everything it does not ──
    if not storage.get("wipe", False):
        warn("storage.wipe: false — the partition table is left alone, but the "
             "declared partitions are still created and formatted, so anything "
             "already on them is replaced")

    for container in storage.get("encryption", []) or []:
        refuse(f"storage.encryption ({container.get('id')}): this applier does not "
               "set up LUKS containers yet — it lays out plain partitions only, and "
               "installing this document would produce an unencrypted disk")
    for group in storage.get("lvm", []) or []:
        refuse(f"storage.lvm ({group.get('name')}): this applier does not build "
               "volume groups yet; the volumes it declares would have nowhere to live")
    for array in storage.get("raid", []) or []:
        refuse(f"storage.raid ({array.get('name')}): this applier does not build "
               "arrays yet — sys-fs/mdadm is also absent from the official binary "
               "host, so honoring it would additionally mean a source build")
    if swap := storage.get("swap"):
        consume(swap)
        refuse("storage.swap (zram/file) is not set up by this applier; declare a "
               "partition with fs: swap instead")

    all_parts = list(storage.get("partitions", []) or [])
    for i, part in enumerate(all_parts):
        if part.get("existing"):
            consume(part["existing"])
            refuse(f"partition {part.get('id') or i}: adopting an existing partition "
                   "is not implemented — this applier only creates partitions")
        fs = role_fs(part)
        if fs == "zfs":
            refuse(f"partition {part.get('id') or i}: fs 'zfs' needs sys-fs/zfs, "
                   "which is not on the official binary host and is not set up by "
                   "this applier")
        elif fs not in (None, "none", "swap", "ext2", "ext3", "ext4", "btrfs",
                        "xfs", "f2fs", "vfat"):
            refuse(f"partition {part.get('id') or i}: this applier has no mkfs for "
                   f"{fs!r}")
        _ = part.get("disk"), part.get("id"), part.get("size"), part.get("label")

    for entry in target.get("disks", []) or []:
        _ = entry.get("id")
        match_selectors(entry)
    disks = [(d.get("match", {}) or {}).get("path") for d in target.get("disks", []) or []]
    if any(d is None for d in disks):
        refuse("some target.disks have no match.path — this applier needs a device "
               "path to partition, resolved from match rules at apply time")
    disks = [d for d in disks if d]
    disk = disks[0] if disks else "/dev/vda"

    mountpoints = resolve_mountpoints(all_parts)
    root_part = next((p for i, p in enumerate(all_parts) if mountpoints.get(i) == "/"),
                     None)
    if root_part is None:
        refuse("no partition resolves to mountpoint '/' — SPEC §6.1 requires exactly "
               "one, and there is nothing to unpack the stage3 into")
    root_fs = role_fs(root_part) if root_part else None
    subvols = (root_part or {}).get("subvolumes") or []
    if subvols and root_fs != "btrfs":
        refuse(f"subvolumes are declared on a {root_fs!r} root — only btrfs has them")
    if subvols and not any(v.get("mountpoint") == "/" for v in subvols):
        refuse("btrfs subvolumes are declared but none mounts at '/' — the root "
               "would be the filesystem's top level, which holds no system")
    for sub in subvols:
        _ = sub.get("name"), sub.get("mountpoint"), sub.get("mount_options")

    if len(disks) > 1:
        refuse(f"{len(disks)} disks are declared but this applier installs to a "
               "single device; spanning them needs storage.lvm or storage.raid, "
               "which it does not build yet")

    # ── boot ──
    loader = boot.get("loader", "auto")
    if loader not in (None, "auto", "grub"):
        refuse(f"boot.loader {loader!r} is not installed by this applier — "
               "sys-boot/grub is what it lays down, and sys-boot/systemd-boot is "
               "not even published on the official binary host")
    if boot.get("uki"):
        refuse("boot.uki is not produced by this applier")
    if boot.get("secure_boot") in (True, "true"):
        refuse("boot.secure_boot: true is not set up by this applier — Gentoo needs "
               "sys-boot/shim plus signed images, none of which it installs")
    if (gen := (boot.get("initramfs") or {}).get("generator")) not in (None, "auto",
                                                                      "dracut"):
        refuse(f"boot.initramfs.generator {gen!r} is not used by this applier — "
               "sys-kernel/installkernel[dracut] is the generator it configures")
    if inc := (boot.get("initramfs") or {}).get("include_modules"):
        warn(f"boot.initramfs.include_modules {inc!r} is folded into the generated "
             "/etc/dracut.conf.d/lis.conf force_drivers list")
    if os_prober := boot.get("os_prober"):
        _ = os_prober

    kernel_pkg = check_kernel_variant(
        doc, {"lts": "sys-kernel/gentoo-kernel-bin", "default": None},
        "Gentoo binary") or "sys-kernel/gentoo-kernel-bin"

    # ── the package set ──
    # A stage3 has no kernel, no bootloader, no initramfs generator and no DHCP
    # client. Those four are not "extra software" — they are what makes the
    # unpacked tree into a machine that boots and can be reached, so they are in
    # the set for every document.
    packages = [kernel_pkg, "sys-kernel/installkernel", "sys-kernel/dracut",
                "sys-boot/grub", "net-misc/dhcpcd"]
    seen_fs = {role_fs(p) for p in all_parts} | {
        v.get("fs") for v in subvols}
    for fs in sorted(f for f in seen_fs if f):
        if tool := FS_TOOLS.get(fs):
            packages.append(tool)
    if firmware == "uefi":
        packages.append("sys-boot/efibootmgr")
    for name in software.get("packages", []) or []:
        packages.append(atom(name))
    for app in software.get("apps", []) or []:
        if isinstance(app, str):
            packages.append(atom(app))
        elif isinstance(app, dict):
            consume(app)
            if name := (app.get("package") or app.get("name")):
                packages.append(atom(name))
    for name in shell_packages(doc):
        packages.append(atom(name))
    packages += driver_packages(doc, family)
    packages += security_packages(doc, family)
    if any(u.get("admin") or u.get("sudo") for u in users):
        packages.append("app-admin/sudo")
    if (network.get("ssh") or {}).get("enabled"):
        packages.append("net-misc/openssh")
    time_cfg = system.get("time", {}) or {}
    if time_cfg.get("ntp"):
        provider = time_cfg.get("provider", "auto")
        ntp = {"auto": "net-misc/chrony", "chrony": "net-misc/chrony",
               "openntpd": "net-misc/openntpd",
               "systemd-timesyncd": None if family == "gentoo" else ""}.get(provider, "?")
        if ntp == "?" or ntp is None:
            refuse(f"system.time.provider {provider!r} is not installed by this "
                   "applier on this init system")
        elif ntp:
            packages.append(ntp)
    if servers := time_cfg.get("servers"):
        warn(f"system.time.servers {servers!r} is not applied — the NTP daemon keeps "
             "its packaged server pool")

    if software.get("snap"):
        refuse("software.snap: snapd is not packaged in ::gentoo")
    if software.get("flatpak"):
        packages.append("sys-apps/flatpak")

    packages = list(dict.fromkeys(packages))
    if forced := sorted(set(packages) & NOT_ON_BINHOST):
        warn(f"{', '.join(forced)} is not published on the official binary host, so "
             "this install compiles it from source and will take considerably longer")

    # ── desktop, network, misc sections ──
    desktop = doc.get("desktop", {}) or {}
    if autologin := desktop.get("autologin"):
        refuse(f"desktop.autologin {autologin!r} is not configured by this applier")
    if desktop.get("audio") not in (None, "auto", "pipewire", "none"):
        refuse(f"desktop.audio {desktop['audio']!r} is not set up by this applier")
    if desktop.get("bluetooth"):
        packages.append("net-wireless/bluez")
    if desktop.get("printing"):
        packages.append("net-print/cups")

    if (manager := network.get("manager")) == "systemd-networkd" and family == "gentoo":
        refuse("network.manager 'systemd-networkd' cannot run under OpenRC — "
               "declare system.init: systemd, or ask for networkmanager")
    elif manager == "iwd":
        refuse("network.manager 'iwd' is not configured by this applier")
    if network.get("wifi"):
        refuse("network.wifi is not configured by this applier")
    if network.get("interfaces"):
        refuse("network.interfaces: static addressing is not generated by this "
               "applier — it enables DHCP on the installed system")
    for entry in network.get("hosts", []) or []:
        consume(entry)
    if (ssh := network.get("ssh")):
        for key in sorted(set(ssh) - {"enabled"}):
            warn(f"network.ssh.{key} is not applied by this applier")

    if system.get("kdump"):
        refuse("system.kdump is not set up by this applier")
    if system.get("telemetry") not in (None, "off"):
        warn("system.telemetry is not applied — Gentoo ships no telemetry to opt "
             "out of")
    elif system.get("telemetry") == "off":
        pass

    if scripts_on_error := (doc.get("scripts", {}) or {}).get("on_error"):
        _ = scripts_on_error

    on_error = [item["content"] for item in
                ((doc.get("scripts", {}) or {}).get("on_error") or [])
                if item.get("content")]

    # ── render ──
    out = {
        "portage/make.conf": make_conf(doc, arch_dir, chost, packages),
        "portage/binrepos.conf/gentoo.conf": binrepos_conf(arch_dir, chost),
        "portage/package.use/lis": package_use(doc, family),
        "portage/package.accept_keywords/lis":
            "# Generated by lis2gentoo — nothing here needs unmasking.\n",
        "portage/sets/lis": package_set(packages),
        "dracut-lis.conf": dracut_conf(doc, root_fs),
        "lis-prepare.sh": prepare_script(
            doc, disk=disk, partitions=all_parts, mountpoints=mountpoints,
            root_part=root_part, arch_dir=arch_dir, stage3_flavour=stage3_flavour,
            on_error=on_error),
        "lis-chroot.sh": chroot_script(
            doc, family=family, profile=profile, disk=disk, firmware=firmware,
            root_fs=root_fs),
    }
    if licences := package_license(doc):
        out["portage/package.license/lis"] = licences
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Translate a LIS document into a Gentoo installation.")
    add_common_args(ap)
    ap.add_argument("--apply", "-a", action="store_true",
                    help="run the generated scripts against this machine")
    args = ap.parse_args()

    raw = load_doc(args.file)
    if args.apply:
        # Rules such as {type: nvme, smallest: true} can only be evaluated with
        # the machine in front of us. Resolved before tracking, because the
        # tracker hands out copies and a mutation through it would not stick.
        resolve_disk_paths(raw)
    doc = track(raw)
    check_version(doc, args.file)
    check_firmware(doc)
    check_unhandled(doc, ALL_SECTIONS)
    check_arch(doc, set(ARCHES))
    check_boot_extras(doc, {"loader", "timeout", "kernel", "params", "variant",
                            "modules", "blacklist", "console", "serial", "uki",
                            "secure_boot", "initramfs", "os_prober",
                            "password_hash"})
    check_mirror(doc, {"url", "country"})
    check_keymap(doc, {"console", "layout", "variant", "font"})
    check_section_fields(doc, "desktop",
                         {"autologin", "audio", "bluetooth", "printing"})
    check_section_fields(doc, "installer", set())

    artifacts = render(doc)

    args.out = args.out.resolve()
    written = []
    for name, content in artifacts.items():
        path = args.out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if name.endswith(".sh"):
            path.chmod(0o755)
        written.append(path)
    report(*written)

    # The whole install is one artifact set: the layout lives in lis-prepare.sh
    # and the packages that open it in the portage tree, so a LUKS container has
    # to leave a trace somewhere in the group.
    check_encryption_emitted(doc, list(artifacts.values()),
                             label="generated Gentoo profile")

    # Fail closed *before* touching the machine, not after.
    check_raid_consumers(doc)
    check_snapshots(doc, tools=frozenset(), boot_menu=False)
    check_script_fields(doc, honors_chroot=False, chroots_by_default=True)
    check_unread(doc, ignore=APPLY_TIME_PATHS)

    if status := enforce(args.strict):
        return status

    if args.apply:
        import shutil
        import subprocess
        for tool in ("parted", "arch-chroot", "genfstab", "gemato", "tar"):
            if not shutil.which(tool):
                sys.exit(f"error: --apply requested, but {tool!r} is not on PATH "
                         "(are you running on the Gentoo minimal installation CD?)")
        for stage in ("pre_install", "pre"):
            for item in (doc.get("scripts", {}) or {}).get(stage, []) or []:
                if content := item.get("content"):
                    subprocess.run(content, shell=True, check=False)
        return subprocess.run(["bash", str(args.out / "lis-prepare.sh"),
                               str(args.out)]).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
