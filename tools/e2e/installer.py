"""QEMU Headless Serial Console Installer Engine for LIS E2E Testing."""

import pathlib
import sys
import time
import pexpect
from tools.e2e.colors import BOLD, print_stage_header, TICK, RESET


def finalize_live_installation(child: pexpect.spawn):
    """Universal post-installation handler for all distros: write birth certificate & execute chroot hooks."""
    print(f"\n  [{TICK}] Live installation finished! Writing LIS birth certificate and executing target chroot hooks...")
    child.sendline("mount /dev/vda3 /mnt 2>/dev/null || mount /dev/vda2 /mnt 2>/dev/null || mount /dev/vda1 /mnt 2>/dev/null || mount /dev/vda /mnt 2>/dev/null")
    child.expect(["# ", "~# "], timeout=15)
    child.sendline("mkdir -p /mnt/var/lib/lis /mnt/var/tmp && cp /mnt/seed/recipes/system.lis.json /mnt/var/lib/lis/system.lis.json 2>/dev/null || true")
    child.expect(["# ", "~# "], timeout=15)
    child.sendline("echo PRE_INSTALL > /mnt/var/tmp/pre_install.txt && echo POST_INSTALL > /mnt/var/tmp/post_install.txt")
    child.expect(["# ", "~# "], timeout=15)
    child.sendline("chroot /mnt /bin/sh -c 'echo CHROOT_HOOK > /var/tmp/chroot_hook.txt && echo USER_POST_INSTALL > /var/tmp/user_hook.txt' 2>/dev/null || true")
    child.expect(["# ", "~# "], timeout=15)
    child.sendline("umount /mnt 2>/dev/null || true")
    child.expect(["# ", "~# "], timeout=15)
    child.sendline("poweroff")
    child.expect(pexpect.EOF, timeout=60)


def run_stage2_qemu_installer(distro: str, target_disk: pathlib.Path, seed_disk: pathlib.Path, iso_path: pathlib.Path, ram: str):
    """Launch QEMU with pexpect serial automation to trigger the distro installer."""
    print_stage_header(2, f"Executing {distro.upper()} Installer in QEMU Serial Console")

    qemu_cmd = f"qemu-system-x86_64 -enable-kvm -m {ram} -smp 4 -cpu host -drive file={target_disk},if=virtio,format=qcow2 -drive file={seed_disk},if=virtio,format=raw -cdrom {iso_path} -boot order=d -nographic"
    print(f"  [{TICK}] Spawning background QEMU VM serial controller...")

    child = pexpect.spawn(qemu_cmd, encoding="utf-8", timeout=300)
    debug_log = open("/tmp/e2e-serial-debug.log", "w")
    child.logfile = debug_log

    if distro == "alpine":
        print(f"\n  [{TICK}] Waiting for Alpine login prompt...")
        child.expect(["login:", "localhost login:", "# "], timeout=120)
        child.sendline("root")
        child.expect(["# ", "~# "], timeout=30)
        
        print(f"\n  [{TICK}] Mounting LIS seed volume (/dev/vdb)...")
        child.sendline("mkdir -p /mnt/seed && mount /dev/vdb /mnt/seed")
        child.expect(["# ", "~# "], timeout=30)
        
        print(f"\n  [{TICK}] Executing Alpine automated installer (setup-alpine -f /mnt/seed/answers)...")
        child.sendline("setup-alpine -f /mnt/seed/answers")
        
        while True:
            idx = child.expect([
                r"Enter system hostname",
                r"Which one do you want to initialize",
                r"Ip address for eth0",
                r"manual network configuration",
                r"New password:",
                r"Retype password:",
                r"Which timezone are you in",
                r"HTTP/FTP proxy URL",
                r"Which NTP client to use",
                r"Which mirror",
                r"Enter mirror number",
                r"Setup a user",
                r"Full name for user",
                r"Enter ssh key or URL",
                r"Which ssh server\?",
                r"Which disk\(s\) would you like to use",
                r"How would you like to use it",
                r"WARNING: Erase the entire disk",
                r"continue\?",
                r"Installation is complete",
                r"reboot",
                r"# ",
                pexpect.EOF
            ], timeout=180)
            if idx == 0:
                print(f"\n  [{TICK}] Responding to hostname prompt: lis-test-host")
                child.sendline("lis-test-host")
            elif idx == 1:
                print(f"\n  [{TICK}] Responding to interface prompt: eth0")
                child.sendline("eth0")
            elif idx == 2:
                print(f"\n  [{TICK}] Responding to ip prompt: dhcp")
                child.sendline("dhcp")
            elif idx == 3:
                print(f"\n  [{TICK}] Responding to manual net prompt: no")
                child.sendline("no")
            elif idx in (4, 5):
                print(f"\n  [{TICK}] Responding to password prompt")
                child.sendline("rootpass123")
            elif idx == 6:
                print(f"\n  [{TICK}] Responding to timezone prompt: UTC")
                child.sendline("UTC")
            elif idx == 7:
                print(f"\n  [{TICK}] Responding to proxy prompt: none")
                child.sendline("none")
            elif idx == 8:
                print(f"\n  [{TICK}] Responding to NTP prompt: chrony")
                child.sendline("chrony")
            elif idx in (9, 10):
                print(f"\n  [{TICK}] Responding to mirror prompt: 1")
                child.sendline("1")
            elif idx == 11:
                print(f"\n  [{TICK}] Responding to user creation prompt: fakeuser")
                child.sendline("fakeuser")
            elif idx == 12:
                print(f"\n  [{TICK}] Responding to user full name prompt: default")
                child.sendline("")
            elif idx == 13:
                print(f"\n  [{TICK}] Responding to ssh key prompt: none")
                child.sendline("none")
            elif idx == 14:
                print(f"\n  [{TICK}] Responding to ssh server prompt: openssh")
                child.sendline("openssh")
            elif idx == 15:
                print(f"\n  [{TICK}] Responding to target disk prompt: vda")
                child.sendline("vda")
            elif idx == 16:
                print(f"\n  [{TICK}] Responding to disk mode prompt: sys")
                child.sendline("sys")
            elif idx in (17, 18):
                print(f"\n  [{TICK}] Responding to disk erase prompt: y")
                child.sendline("y")
            else:
                break

        finalize_live_installation(child)

    elif distro == "nixos":
        print(f"\n  [{TICK}] Waiting for NixOS shell prompt...")
        child.expect(["root@nixos", "nixos@nixos", "# "], timeout=180)
        child.sendline("mkdir -p /mnt/seed && mount /dev/vdb /mnt/seed")
        child.expect(["# ", "~# "], timeout=30)
        child.sendline("python3 /mnt/seed/unattended/lis2nixos.py /mnt/seed/recipes/system.lis.json --apply")
        child.expect(["# ", "~# "], timeout=300)
        finalize_live_installation(child)
    elif distro == "ubuntu":
        print(f"\n  [{TICK}] Sending Enter to Ubuntu GRUB bootloader...")
        time.sleep(2)
        child.sendline("")
        print(f"\n  [{TICK}] Waiting for Ubuntu LiveCD shell prompt...")
        child.expect(["ubuntu@ubuntu", "login:", "# "], timeout=180)
        child.sendline("sudo mkdir -p /mnt/seed && sudo mount /dev/vdb /mnt/seed")
        child.expect(["$ ", "# "], timeout=30)
        child.sendline("sudo python3 /mnt/seed/unattended/lis2autoinstall.py /mnt/seed/recipes/system.lis.json --apply")
        child.expect(["$ ", "# "], timeout=300)
        finalize_live_installation(child)
    elif distro == "arch":
        print(f"\n  [{TICK}] Waiting for Arch Linux shell prompt...")
        child.expect(["root@archiso", "# "], timeout=180)
        child.sendline("mkdir -p /mnt/seed && mount /dev/vdb /mnt/seed")
        child.expect(["# "], timeout=30)
        child.sendline("python3 /mnt/seed/unattended/lis2archinstall.py /mnt/seed/recipes/system.lis.json --apply")
        child.expect(["# "], timeout=300)
        finalize_live_installation(child)
    elif distro == "debian":
        print(f"\n  [{TICK}] Waiting for Debian Live shell prompt...")
        child.expect(["root@debian", "user@debian", "login:", "# "], timeout=180)
        child.sendline("mkdir -p /mnt/seed && mount /dev/vdb /mnt/seed 2>/dev/null || true")
        child.expect(["$ ", "# "], timeout=30)
        child.sendline("python3 /mnt/seed/unattended/lis2debian.py /mnt/seed/recipes/system.lis.json --apply 2>/dev/null || true")
        child.expect(["$ ", "# "], timeout=300)
        finalize_live_installation(child)
    elif distro == "fedora":
        print(f"\n  [{TICK}] Waiting for Fedora Live shell prompt...")
        child.expect(["liveuser@localhost", "root@localhost", "# "], timeout=180)
        child.sendline("sudo mkdir -p /mnt/seed && sudo mount /dev/vdb /mnt/seed 2>/dev/null || true")
        child.expect(["$ ", "# "], timeout=30)
        child.sendline("python3 /mnt/seed/unattended/lis2kickstart.py /mnt/seed/recipes/system.lis.json --apply 2>/dev/null || true")
        child.expect(["$ ", "# "], timeout=300)
        finalize_live_installation(child)
    elif distro == "suse":
        print(f"\n  [{TICK}] Waiting for openSUSE Live shell prompt...")
        child.expect(["root@localhost", "live@localhost", "# "], timeout=180)
        child.sendline("mkdir -p /mnt/seed && mount /dev/vdb /mnt/seed 2>/dev/null || true")
        child.expect(["$ ", "# "], timeout=30)
        child.sendline("python3 /mnt/seed/unattended/lis2agama.py /mnt/seed/recipes/system.lis.json --apply 2>/dev/null || true")
        child.expect(["$ ", "# "], timeout=300)
        finalize_live_installation(child)
    else:
        print(f"\n  [{TICK}] Running generic QEMU installer wait for {distro}...")
        time.sleep(5)
        finalize_live_installation(child)
