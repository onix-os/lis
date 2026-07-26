"""ISO Downloader & Resolver Module for LIS E2E Testing."""

import pathlib
import sys
import urllib.request
from tools.e2e.colors import BOLD, CYAN, GRAY, RED, TICK, RESET

DISTRO_ISOS = {
    "alpine": {
        "url": "https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/x86_64/alpine-standard-3.21.3-x86_64.iso",
        "file": "alpine-standard-3.21.3-x86_64.iso",
    },
    "arch": {
        "url": "https://geo.mirror.pkgbuild.com/iso/latest/archlinux-x86_64.iso",
        "file": "archlinux-x86_64.iso",
    },
    "nixos": {
        "url": "https://channels.nixos.org/nixos-24.11/latest-nixos-minimal-x86_64-linux.iso",
        "file": "nixos-minimal-x86_64-linux.iso",
    },
    "ubuntu": {
        "url": "https://releases.ubuntu.com/24.04/ubuntu-24.04.4-live-server-amd64.iso",
        "file": "ubuntu-24.04.4-live-server-amd64.iso",
    },
    "debian": {
        "url": "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-13.6.0-amd64-netinst.iso",
        "file": "debian-13.6.0-amd64-netinst.iso",
    },
    "fedora": {
        "url": "https://download.fedoraproject.org/pub/fedora/linux/releases/41/Everything/x86_64/iso/Fedora-Everything-netinst-x86_64-41-1.4.iso",
        "file": "Fedora-Everything-netinst-x86_64-41-1.4.iso",
    },
    "suse": {
        "url": "https://download.opensuse.org/distribution/leap/15.6/iso/openSUSE-Leap-15.6-NET-x86_64-Media.iso",
        "file": "openSUSE-Leap-15.6-NET-x86_64-Media.iso",
    },
}


def download_iso_if_missing(distro: str) -> pathlib.Path:
    """Download ISO to /tmp/ if not present on system."""
    meta = DISTRO_ISOS.get(distro)
    if not meta:
        sys.exit(f"{RED}error: unknown distro '{distro}'{RESET}")

    tmp_iso = pathlib.Path("/tmp") / meta["file"]
    if tmp_iso.exists() and tmp_iso.stat().st_size > 10 * 1024 * 1024:
        print(f"  [{TICK}] Found cached ISO in /tmp: {BOLD}{tmp_iso}{RESET}")
        return tmp_iso

    url = meta["url"]
    print(f"  [{TICK}] Downloading ISO from {CYAN}{url}{RESET} -> {BOLD}{tmp_iso}{RESET}...")
    
    def _progress(block_num, block_size, total_size):
        downloaded = block_num * block_size
        pct = min(100.0, (downloaded / total_size) * 100.0) if total_size > 0 else 0
        sys.stdout.write(f"\r    {GRAY}Downloading: {downloaded / 1024 / 1024:.1f}MB ({pct:.1f}%){RESET}")
        sys.stdout.flush()

    urllib.request.urlretrieve(url, tmp_iso, reporthook=_progress)
    print(f"\n  [{TICK}] Download complete: {BOLD}{tmp_iso}{RESET} ({tmp_iso.stat().st_size / 1024 / 1024:.1f}MB)")
    return tmp_iso
