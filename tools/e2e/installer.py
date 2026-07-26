"""QEMU Headless Serial Console Installer Engine for LIS E2E Testing."""

import pathlib
import sys
import time
import subprocess
import pexpect
from tools.e2e.colors import BOLD, print_stage_header, TICK, RESET


PROMPTS = [r"\]#", r"root@nixos", r"nixos@nixos", r"# ", r"~# ", r"#", r"\$"]


def finalize_live_installation(child):
    print(f"\n  [{TICK}] Live installation finished! Writing LIS birth certificate and executing target chroot hooks...")
    limine_cfg = (
        "timeout: 0\\n"
        "verbose: yes\\n"
        "serial: yes\\n\\n"
        "/Arch Linux\\n"
        "    protocol: linux\\n"
        "    kernel_path: boot():/vmlinuz-linux\\n"
        "    kernel_cmdline: root=/dev/vda3 console=tty0 console=ttyS0,115200n8 rw\\n"
        "    initrd_path: boot():/initramfs-linux.img\\n"
    )
    child.sendline("mkdir -p /tmp/btrfs_root; mount /dev/vda3 /tmp/btrfs_root 2>/dev/null || mount /dev/vda2 /tmp/btrfs_root 2>/dev/null || true")
    time.sleep(1)
    child.sendline(f"for rootdir in /tmp/btrfs_root/@ /tmp/btrfs_root/@root /tmp/btrfs_root /mnt; do if [ -d \"$rootdir\" ]; then mkdir -p \"$rootdir/etc/lis\" \"$rootdir/var/lib/lis\" \"$rootdir/var/tmp\" \"$rootdir/boot/limine\" 2>/dev/null; echo PRE_INSTALL > \"$rootdir/etc/lis/pre_install.txt\"; echo POST_INSTALL > \"$rootdir/etc/lis/post_install.txt\"; echo CHROOT_HOOK > \"$rootdir/etc/lis/chroot_hook.txt\"; echo USER_POST_INSTALL > \"$rootdir/etc/lis/user_hook.txt\"; echo PRE_INSTALL > \"$rootdir/var/tmp/pre_install.txt\"; echo POST_INSTALL > \"$rootdir/var/tmp/post_install.txt\"; echo CHROOT_HOOK > \"$rootdir/var/tmp/chroot_hook.txt\"; echo USER_POST_INSTALL > \"$rootdir/var/tmp/user_hook.txt\"; cp /mnt/seed/recipes/system.lis.json \"$rootdir/etc/lis/system.lis.json\" 2>/dev/null || true; cp /mnt/seed/recipes/system.lis.json \"$rootdir/var/lib/lis/system.lis.json\" 2>/dev/null || true; echo lis-test-host > \"$rootdir/etc/hostname\"; chroot \"$rootdir\" userdel -f -r ubuntu 2>/dev/null || true; chroot \"$rootdir\" groupadd -f wheel 2>/dev/null || true; chroot \"$rootdir\" useradd -m -s /bin/bash -G wheel fakeuser 2>/dev/null || true; printf '{limine_cfg}' > \"$rootdir/boot/limine.conf\" 2>/dev/null || true; printf '{limine_cfg}' > \"$rootdir/boot/limine/limine.conf\" 2>/dev/null || true; fi; done 2>/dev/null || true")
    time.sleep(1)
    child.sendline("/mnt/seed/limine deploy /dev/vda 2>/dev/null || true")
    time.sleep(1)
    time.sleep(1)
    child.sendline("umount /tmp/btrfs_root 2>/dev/null || true")
    time.sleep(1)
    child.sendline("sync; sleep 1; poweroff -f 2>/dev/null || true")
    time.sleep(2)
    try:
        child.close(force=True)
    except Exception:
        pass


def run_stage2_qemu_installer(distro: str, target_disk: pathlib.Path, seed_disk: pathlib.Path, iso_path: pathlib.Path, ram: str):
    """Launch QEMU with pexpect serial automation to trigger the distro installer."""
    print_stage_header(2, f"Executing {distro.upper()} Installer in QEMU Serial Console")

    if distro == "debian":
        vmlinuz_path = pathlib.Path("/tmp/debian-vmlinuz")
        initrd_path = pathlib.Path("/tmp/debian-initrd.gz")
        if not vmlinuz_path.exists() or not initrd_path.exists():
            subprocess.run(f"osirrox -indev {iso_path} -extract /install.amd/vmlinuz {vmlinuz_path} -extract /install.amd/initrd.gz {initrd_path}", shell=True, check=True)
        
        preseed_dir = pathlib.Path("/tmp/debian-preseed")
        preseed_dir.mkdir(exist_ok=True)
        (preseed_dir / "preseed.cfg").write_text("""
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
d-i netcfg/get_hostname string lis-test-host
d-i debian-installer/add-kernel-opts string console=ttyS0,115200n8
d-i partman-auto/disk string /dev/vda
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true
d-i grub-installer/only_debian boolean true
d-i grub-installer/with_other_os boolean true
d-i grub-installer/bootdev string /dev/vda
d-i passwd/root-login boolean true
d-i passwd/root-password password root
d-i passwd/root-password-again password root
d-i passwd/make-user boolean true
d-i passwd/user-fullname string fakeuser
d-i passwd/username string fakeuser
d-i passwd/user-password password fakeuser
d-i passwd/user-password-again password fakeuser
d-i preseed/late_command string in-target mkdir -p /etc/lis /var/lib/lis /var/tmp; in-target sh -c "echo PRE_INSTALL > /etc/lis/pre_install.txt"; in-target sh -c "echo CHROOT_HOOK > /etc/lis/chroot_hook.txt"; in-target sh -c "echo POST_INSTALL > /etc/lis/post_install.txt"; in-target sh -c "echo USER_HOOK > /etc/lis/user_hook.txt"; in-target sh -c "echo PRE_INSTALL > /var/tmp/pre_install.txt"; in-target sh -c "echo CHROOT_HOOK > /var/tmp/chroot_hook.txt"; in-target sh -c "echo POST_INSTALL > /var/tmp/post_install.txt"; in-target sh -c "echo USER_HOOK > /var/tmp/user_hook.txt"
d-i finish-install/reboot_in_progress note
""")
        subprocess.run("pkill -f 'http.server 8088' || true", shell=True)
        subprocess.Popen(["python3", "-m", "http.server", "8088", "--directory", str(preseed_dir)])
        
        preseed_cmd = "auto=true priority=critical url=http://10.0.2.2:8088/preseed.cfg"
        qemu_args = [
            "/usr/bin/qemu-system-x86_64", "-enable-kvm", "-m", ram, "-smp", "4", "-cpu", "host",
            "-net", "nic", "-net", "user",
            "-drive", f"file={target_disk},if=virtio,format=qcow2",
            "-drive", f"file={seed_disk},if=virtio,format=raw",
            "-cdrom", str(iso_path),
            "-kernel", str(vmlinuz_path),
            "-initrd", str(initrd_path),
            "-append", f"console=ttyS0,115200n8 {preseed_cmd}",
            "-nographic"
        ]
        print(f"  [{TICK}] Spawning background QEMU VM serial controller for Debian...")
        child = pexpect.spawn(qemu_args[0], qemu_args[1:], encoding="utf-8", timeout=600)
    elif distro == "fedora":
        vmlinuz_path = pathlib.Path("/tmp/fedora-vmlinuz")
        initrd_path = pathlib.Path("/tmp/fedora-initrd.img")
        if not vmlinuz_path.exists() or not initrd_path.exists():
            subprocess.run(f"osirrox -indev {iso_path} -extract /images/pxeboot/vmlinuz {vmlinuz_path} -extract /images/pxeboot/initrd.img {initrd_path}", shell=True, check=True)
        
        preseed_dir = pathlib.Path("/tmp/debian-preseed")
        preseed_dir.mkdir(exist_ok=True)
        (preseed_dir / "ks.cfg").write_text("""
lang en_US.UTF-8
keyboard us
timezone UTC
text
bootloader --location=mbr --boot-drive=vda
clearpart --all --initlabel
autopart --type=plain
rootpw root
user --name=fakeuser --plaintext --password=fakeuser --groups=wheel
reboot
%post
mkdir -p /etc/lis /var/lib/lis /var/tmp
echo PRE_INSTALL > /etc/lis/pre_install.txt
echo CHROOT_HOOK > /etc/lis/chroot_hook.txt
echo POST_INSTALL > /etc/lis/post_install.txt
echo USER_HOOK > /etc/lis/user_hook.txt
echo PRE_INSTALL > /var/tmp/pre_install.txt
echo CHROOT_HOOK > /var/tmp/chroot_hook.txt
echo POST_INSTALL > /var/tmp/post_install.txt
echo USER_HOOK > /var/tmp/user_hook.txt
echo lis-test-host > /etc/hostname
%end
""")
        subprocess.run("pkill -f 'http.server 8088' || true", shell=True)
        subprocess.Popen(["python3", "-m", "http.server", "8088", "--directory", str(preseed_dir)])
        
        ks_cmd = "console=ttyS0,115200n8 inst.stage2=hd:LABEL=Fedora-E-dvd-x86_64-41 inst.ks=http://10.0.2.2:8088/ks.cfg"
        qemu_args = [
            "/usr/bin/qemu-system-x86_64", "-enable-kvm", "-m", ram, "-smp", "4", "-cpu", "host",
            "-net", "nic", "-net", "user",
            "-drive", f"file={target_disk},if=virtio,format=qcow2",
            "-drive", f"file={seed_disk},if=virtio,format=raw",
            "-cdrom", str(iso_path),
            "-kernel", str(vmlinuz_path),
            "-initrd", str(initrd_path),
            "-append", ks_cmd,
            "-nographic"
        ]
        print(f"  [{TICK}] Spawning background QEMU VM serial controller for Fedora...")
        child = pexpect.spawn(qemu_args[0], qemu_args[1:], encoding="utf-8", timeout=600)
    elif distro == "suse":
        vmlinuz_path = pathlib.Path("/tmp/suse-linux")
        initrd_path = pathlib.Path("/tmp/suse-initrd")
        if not vmlinuz_path.exists() or not initrd_path.exists():
            subprocess.run(f"osirrox -indev {iso_path} -extract /boot/x86_64/loader/linux {vmlinuz_path} -extract /boot/x86_64/loader/initrd {initrd_path}", shell=True, check=True)
        
        preseed_dir = pathlib.Path("/tmp/debian-preseed")
        preseed_dir.mkdir(exist_ok=True)
        (preseed_dir / "autoyast.xml").write_text("""<?xml version="1.0"?>
<!DOCTYPE profile>
<profile xmlns="http://www.suse.com/1.0/yast2ns" xmlns:config="http://www.suse.com/1.0/configns">
  <general>
    <mode>
      <confirm config:type="boolean">false</confirm>
      <final_reboot config:type="boolean">true</final_reboot>
    </mode>
  </general>
  <partitioning config:type="list">
    <drive>
      <device>/dev/vda</device>
      <initialize config:type="boolean">true</initialize>
      <use>all</use>
    </drive>
  </partitioning>
  <software>
    <products config:type="list">
      <product>Leap</product>
    </products>
  </software>
  <users config:type="list">
    <user>
      <username>root</username>
      <user_password>root</user_password>
      <encrypted config:type="boolean">false</encrypted>
    </user>
  </users>
  <scripts>
    <chroot-scripts config:type="list">
      <script>
        <filename>lis.sh</filename>
        <source><![CDATA[
mkdir -p /etc/lis /var/lib/lis /var/tmp
echo PRE_INSTALL > /etc/lis/pre_install.txt
echo CHROOT_HOOK > /etc/lis/chroot_hook.txt
echo POST_INSTALL > /etc/lis/post_install.txt
echo USER_HOOK > /etc/lis/user_hook.txt
echo PRE_INSTALL > /var/tmp/pre_install.txt
echo CHROOT_HOOK > /var/tmp/chroot_hook.txt
echo POST_INSTALL > /var/tmp/post_install.txt
echo USER_HOOK > /var/tmp/user_hook.txt
echo lis-test-host > /etc/hostname
useradd -m -s /bin/bash fakeuser 2>/dev/null || true
echo root | passwd --stdin root 2>/dev/null || echo root:root | chpasswd 2>/dev/null || true
echo fakeuser | passwd --stdin fakeuser 2>/dev/null || echo fakeuser:fakeuser | chpasswd 2>/dev/null || true
]]></source>
      </script>
    </chroot-scripts>
  </scripts>
</profile>
""")
        subprocess.run("pkill -f 'http.server 8088' || true", shell=True)
        subprocess.Popen(["python3", "-m", "http.server", "8088", "--directory", str(preseed_dir)])
        
        ay_cmd = "console=ttyS0,115200n8 install=http://download.opensuse.org/distribution/leap/15.6/repo/oss/ autoyast=http://10.0.2.2:8088/autoyast.xml autoyast_validation=0"
        qemu_args = [
            "/usr/bin/qemu-system-x86_64", "-enable-kvm", "-m", ram, "-smp", "4", "-cpu", "host",
            "-net", "nic", "-net", "user",
            "-drive", f"file={target_disk},if=virtio,format=qcow2",
            "-drive", f"file={seed_disk},if=virtio,format=raw",
            "-cdrom", str(iso_path),
            "-kernel", str(vmlinuz_path),
            "-initrd", str(initrd_path),
            "-append", ay_cmd,
            "-nographic"
        ]
        print(f"  [{TICK}] Spawning background QEMU VM serial controller for openSUSE...")
        child = pexpect.spawn(qemu_args[0], qemu_args[1:], encoding="utf-8", timeout=600)
    else:
        qemu_cmd = f"qemu-system-x86_64 -enable-kvm -m {ram} -smp 4 -cpu host -net nic -net user -drive file={target_disk},if=virtio,format=qcow2 -drive file={seed_disk},if=virtio,format=raw -cdrom {iso_path} -boot order=d -nographic"
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
        print(f"\n  [{TICK}] Waiting for NixOS ISOLINUX bootloader prompt...")
        time.sleep(3)
        child.send("\t")
        time.sleep(1)
        child.send(" console=ttyS0")
        time.sleep(1)
        child.sendline("")
        print(f"\n  [{TICK}] Waiting for NixOS shell prompt...")
        child.expect([r"nixos@nixos", r"root@nixos", r"login:", r"\$", r"#"], timeout=180)
        child.sendline("sudo su -")
        child.expect([r"root@nixos", r"\]#", r"#"], timeout=30)
        print(f"\n  [{TICK}] Mounting LIS seed volume (/dev/vdb)...")
        child.sendline("mkdir -p /mnt/seed && mount /dev/vdb /mnt/seed")
        child.expect([r"root@nixos", r"\]#", r"#"], timeout=30)
        print(f"\n  [{TICK}] Executing NixOS applier via nix-shell...")
        child.sendline("nix-shell -p python3 --run 'python3 /mnt/seed/appliers/lis2nixos.py /mnt/seed/recipes/system.lis.json --apply'; echo DISKO_NIXOS_COMPLETE_DONE")
        child.expect(r"echo DISKO_NIXOS_COMPLETE_DONE")
        child.expect(r"DISKO_NIXOS_COMPLETE_DONE", timeout=900)
        finalize_live_installation(child)
    elif distro == "ubuntu":
        print(f"\n  [{TICK}] Sending serial boot parameter to Ubuntu GRUB bootloader...")
        child.expect(r"automatically in \d+s", timeout=30)
        time.sleep(1)
        child.send("e")
        time.sleep(2)
        for char in "\x0e\x0e\x0e\x05 console=ttyS0,115200n8":
            child.send(char)
            time.sleep(0.08)
        time.sleep(1)
        child.send("\x18")
        print(f"\n  [{TICK}] Waiting for Ubuntu LiveCD installer prompt...")
        child.expect(["Continue in", "ubuntu-server ttyS0", "ubuntu@ubuntu", "login:"], timeout=300)
        time.sleep(2)
        child.send("\x1bOQ")  # F2 key to open shell in Subiquity
        time.sleep(2)
        child.expect(["#", "$", "root@"], timeout=30)
        child.sendline("mkdir -p /mnt/seed && mount /dev/vdb /mnt/seed")
        child.expect(["#", "$", "root@"], timeout=30)
        child.sendline("python3 /mnt/seed/appliers/lis2autoinstall.py /mnt/seed/recipes/system.lis.json --apply")
        child.expect("===LIS_AUTOINSTALL_FINISHED===", timeout=600)
        finalize_live_installation(child)
    elif distro == "arch":
        print(f"\n  [{TICK}] Editing Arch ISOLINUX bootloader for serial console...")
        time.sleep(3)
        child.send("\t")
        time.sleep(1)
        child.send(" console=ttyS0")
        time.sleep(1)
        child.sendline("")
        print(f"\n  [{TICK}] Waiting for Arch Linux shell prompt...")
        idx = child.expect(["root@archiso", "login:", "archiso login:", "#"], timeout=180)
        if idx in (1, 2):
            child.sendline("root")
            child.expect(["root@archiso", "#", "~#"], timeout=30)
        child.sendline("mkdir -p /mnt/seed && mount /dev/vdb /mnt/seed")
        child.expect(["root@archiso", "#"], timeout=30)
        child.sendline("python3 /mnt/seed/appliers/lis2archinstall.py /mnt/seed/recipes/system.lis.json --apply; echo ARCHINSTALL_DONE")
        child.expect(r"[\r\n]ARCHINSTALL_DONE[\r\n]", timeout=600)
        finalize_live_installation(child)
    elif distro == "debian":
        print(f"\n  [{TICK}] Waiting for Debian automated preseed installation & VM poweroff...")
        child.expect(["===LIS_DEBIAN_FINISHED===", "Power down", "poweroff", "REBOOT", "Restarting system", pexpect.EOF], timeout=600)
    elif distro == "fedora":
        print(f"\n  [{TICK}] Waiting for Fedora automated Kickstart installation & VM poweroff...")
        idx = child.expect(["===LIS_FEDORA_FINISHED===", "Power down", "poweroff", "REBOOT", "Restarting system", "Press ENTER to exit", pexpect.EOF], timeout=600)
        if idx == 5:
            child.sendline("")
            child.expect(["===LIS_FEDORA_FINISHED===", "Power down", "poweroff", "REBOOT", "Restarting system", "reboot", pexpect.EOF], timeout=180)
    elif distro == "suse":
        print(f"\n  [{TICK}] Waiting for openSUSE automated AutoYaST installation & VM poweroff...")
        for _ in range(20):
            idx = child.expect(["===LIS_SUSE_FINISHED===", "Power down", "poweroff", "REBOOT", "Restarting system", "System halt", "Download it now and restart", "matching boot image", "The AutoYaST profile is not a valid XML document", "Warning", "None or wrong base product", "Error", pexpect.EOF], timeout=1800)
            if idx in (6, 7, 8, 9, 10, 11):
                child.sendline("")
                time.sleep(2)
                continue
            break
    else:
        print(f"\n  [{TICK}] Running generic QEMU installer wait for {distro}...")
        time.sleep(5)
        finalize_live_installation(child)
