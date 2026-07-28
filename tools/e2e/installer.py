"""Stage 2 — drive each distro's own installer inside QEMU over the serial console.

The rule this module lives by: whatever ends up on the target disk must be put
there by the distro's native installer, acting on a configuration this repo's
appliers generated from the LIS document. The harness boots the VM, hands the
installer its configuration, and waits. It never partitions, never copies a root
filesystem, and never writes the artifacts that Stage 3/4 go on to verify —
doing any of that would make the test assert against its own output.
"""

import pathlib
import shutil
import subprocess
import sys
import time

import pexpect

from tools.e2e.colors import BOLD, GRAY, print_stage_header, TICK, RESET

APPLIERS = pathlib.Path(__file__).resolve().parent.parent / "appliers"
HTTP_PORT = 8088
SERIAL = "console=ttyS0,115200n8"
# The openSUSE NET image installs from a remote repository.
SUSE_REPO = "http://download.opensuse.org/distribution/leap/15.6/repo/oss/"


class InstallFailed(Exception):
    """The distro installer did not reach a successful end state."""


class DocumentRefused(InstallFailed):
    """The applier refused the document (SPEC §2.3) — a verdict, not a malfunction."""

    def __init__(self, applier: str, reasons: list[str]):
        self.applier = applier
        self.reasons = reasons
        super().__init__(f"{applier} refused the document: "
                         + "; ".join(reasons or ["see the applier output above"]))


# ── applier invocation ───────────────────────────────────────────

def run_applier(name: str, recipe: pathlib.Path, out_dir: pathlib.Path) -> pathlib.Path:
    """Generate a distro configuration from the LIS document, fail-closed.

    A non-zero exit means the applier refused the document (SPEC §2.3). That is
    a legitimate test outcome and must surface as a failure, not be worked
    around by hand-writing the configuration here.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(APPLIERS / name), str(recipe), "--out", str(out_dir)]
    print(f"  [{TICK}] Translating recipe with {BOLD}{name}{RESET}...")
    res = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(res.stdout)
    sys.stderr.write(res.stderr)
    if res.returncode != 0:
        reasons = [line[len("refused: "):] for line in res.stderr.splitlines()
                   if line.startswith("refused: ")]
        raise DocumentRefused(name, reasons)
    return out_dir


def serve(directory: pathlib.Path, log: pathlib.Path | None = None) -> None:
    """Expose a directory to the guest at http://10.0.2.2:8088/ (QEMU user net).

    The access log is kept: when an installer says it cannot retrieve its
    profile, the first thing worth knowing is whether it ever asked.
    """
    subprocess.run(f"pkill -f 'http.server {HTTP_PORT}' || true", shell=True)
    sink = open(log, "w") if log else subprocess.DEVNULL
    subprocess.Popen([sys.executable, "-m", "http.server", str(HTTP_PORT),
                      "--directory", str(directory)],
                     stdout=sink, stderr=sink)
    time.sleep(1)
    if log:
        print(f"  [{TICK}] HTTP access log: {GRAY}{log}{RESET}")


def build_cidata(seed_files: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    """Wrap user-data/meta-data in a CIDATA volume — cloud-init's own discovery path."""
    if out.exists():
        out.unlink()
    with open(out, "wb") as f:
        f.truncate(32 * 1024 * 1024)  # below ~16MB mkfs.vfat rejects a FAT16 geometry
    subprocess.run(["mkfs.vfat", "-F", "16", "-n", "CIDATA", str(out)],
                   check=True, capture_output=True)
    for name in ("user-data", "meta-data"):
        subprocess.run(["mcopy", "-i", str(out), str(seed_files / name), f"::{name}"],
                       check=True)
    return out


def extract(iso: pathlib.Path, mapping: dict[str, pathlib.Path]) -> None:
    """Pull kernel/initrd out of an ISO so the cmdline can be set without a GRUB dance."""
    if all(dest.exists() for dest in mapping.values()):
        return
    args = " ".join(f"-extract {src} {dest}" for src, dest in mapping.items())
    subprocess.run(f"osirrox -indev {iso} {args}", shell=True, check=True)


# ── QEMU ─────────────────────────────────────────────────────────

def qemu(target_disk, ram, *, iso=None, extra_drives=(), kernel=None, initrd=None,
         append=None, boot=None, timeout=1800, log=None) -> pexpect.spawn:
    cmd = ["qemu-system-x86_64", "-enable-kvm", "-m", ram, "-smp", "4", "-cpu", "host",
           "-net", "nic", "-net", "user",
           "-drive", f"file={target_disk},if=virtio,format=qcow2"]
    for drive in extra_drives:
        cmd += ["-drive", f"file={drive},if=virtio,format=raw"]
    if iso:
        cmd += ["-cdrom", str(iso)]
    if kernel:
        cmd += ["-kernel", str(kernel)]
    if initrd:
        cmd += ["-initrd", str(initrd)]
    if append:
        cmd += ["-append", append]
    if boot:
        cmd += ["-boot", boot]
    # The installer boots from -kernel/-initrd, so a guest reboot would re-enter
    # the installer and overwrite what it just built. Exit instead.
    cmd += ["-no-reboot", "-nographic"]
    print(f"  [{TICK}] Spawning QEMU: {GRAY}{' '.join(cmd[:8])} …{RESET}")
    child = pexpect.spawn(cmd[0], cmd[1:], encoding="utf-8", codec_errors="ignore",
                          timeout=timeout)
    if log:
        # The serial stream goes to a file, not stdout: it is megabytes of installer
        # chatter, and it is the only evidence available when a run fails.
        child.logfile_read = open(log, "w", encoding="utf-8", errors="replace")
        print(f"  [{TICK}] Serial console log: {GRAY}{log}{RESET}")
    return child


def wait_for_finish(child: pexpect.spawn, distro: str, markers: list[str],
                    timeout: int) -> None:
    """Wait for the installer to finish. Anything else is a failure."""
    failures = ["Installation failed", "installation failed", "Kernel panic",
                "An error occurred", "Sorry, an error occurred"]
    idx = child.expect(markers + failures + [pexpect.EOF, pexpect.TIMEOUT],
                       timeout=timeout)
    if idx < len(markers):
        print(f"\n  [{TICK}] {distro} installer reported completion.")
        return
    if idx < len(markers) + len(failures):
        raise InstallFailed(f"{distro} installer reported an error: "
                            f"{failures[idx - len(markers)]!r}")
    if idx == len(markers) + len(failures):
        # EOF: QEMU is gone. A guest that powered itself off is the expected end
        # for these installers, but a VM killed from outside looks identical
        # here — and calling that a finished install would hand Stage 3 a
        # half-written disk and blame the applier for it.
        child.close()
        if child.signalstatus is not None:
            raise InstallFailed(
                f"{distro} VM was killed by signal {child.signalstatus} before the "
                "installer finished — the target disk is incomplete")
        print(f"\n  [{TICK}] {distro} VM exited (installer powered the machine off).")
        return
    raise InstallFailed(f"{distro} installer timed out after {timeout}s")


def shutdown(child: pexpect.spawn) -> None:
    try:
        child.sendline("sync; poweroff -f")
        child.expect(pexpect.EOF, timeout=60)
    except Exception:  # noqa: BLE001 — the VM going away is the outcome we want
        pass
    finally:
        try:
            child.close(force=True)
        except Exception:  # noqa: BLE001
            pass


def run_in_live_shell(child, command, done_marker, timeout) -> None:
    """Run an applier from inside a live ISO shell and wait for it to finish."""
    child.sendline(f"{command}; echo {done_marker}=$?")
    child.expect(rf"{done_marker}=(\d+)", timeout=timeout)
    status = int(child.match.group(1))
    if status != 0:
        raise InstallFailed(f"applier exited {status} inside the live environment")


# ── per-distro drivers ───────────────────────────────────────────

def install_ubuntu(target_disk, seed_disk, iso, ram, recipe, work):
    """Subiquity's own autoinstall path: a NoCloud seed plus `autoinstall`.

    No terminal hijack and no live-CD copy — the installer that runs is the one
    Ubuntu ships, reading the user-data lis2autoinstall generated.
    """
    seed = run_applier("lis2autoinstall.py", recipe, work / "autoinstall")
    cidata = build_cidata(seed, work / "cidata.img")
    vmlinuz, initrd = work / "ubuntu-vmlinuz", work / "ubuntu-initrd"
    extract(iso, {"/casper/vmlinuz": vmlinuz, "/casper/initrd": initrd})
    child = qemu(target_disk, ram, iso=iso, extra_drives=[seed_disk, cidata],
                 kernel=vmlinuz, initrd=initrd,
                 append=f"{SERIAL} autoinstall --- {SERIAL}", timeout=2400,
                 log=work / "serial.log")
    # `finish: subiquity/...` is printed for every internal step, so match only
    # the states that mean the whole install is over.
    wait_for_finish(child, "ubuntu",
                    ["finish: subiquity/Reboot/reboot", "reboot: Power down",
                     "reboot: Restarting system", "Installation complete"],
                    timeout=2400)
    shutdown(child)


def install_debian(target_disk, seed_disk, iso, ram, recipe, work):
    out = run_applier("lis2debian.py", recipe, work / "preseed")
    serve(out, work / "http.log")
    vmlinuz, initrd = work / "debian-vmlinuz", work / "debian-initrd.gz"
    extract(iso, {"/install.amd/vmlinuz": vmlinuz, "/install.amd/initrd.gz": initrd})
    child = qemu(target_disk, ram, iso=iso, extra_drives=[seed_disk],
                 kernel=vmlinuz, initrd=initrd,
                 append=f"{SERIAL} auto=true priority=critical "
                        f"url=http://10.0.2.2:{HTTP_PORT}/preseed.cfg", timeout=2400,
                 log=work / "serial.log")
    wait_for_finish(child, "debian",
                    ["Restarting system", "reboot: Power down", "Rebooting"], timeout=2400)
    shutdown(child)


def install_fedora(target_disk, seed_disk, iso, ram, recipe, work):
    out = run_applier("lis2kickstart.py", recipe, work / "kickstart")
    serve(out, work / "http.log")
    vmlinuz, initrd = work / "fedora-vmlinuz", work / "fedora-initrd.img"
    extract(iso, {"/images/pxeboot/vmlinuz": vmlinuz, "/images/pxeboot/initrd.img": initrd})
    label = iso_label(iso)
    child = qemu(target_disk, ram, iso=iso, extra_drives=[seed_disk],
                 kernel=vmlinuz, initrd=initrd,
                 append=f"{SERIAL} inst.stage2=hd:LABEL={label} "
                        f"inst.ks=http://10.0.2.2:{HTTP_PORT}/ks.cfg inst.text",
                 timeout=2400, log=work / "serial.log")
    wait_for_finish(child, "fedora",
                    ["Restarting system", "reboot: Power down", "Rebooting"], timeout=2400)
    shutdown(child)


def install_suse(target_disk, seed_disk, iso, ram, recipe, work):
    out = run_applier("lis2agama.py", recipe, work / "autoyast")
    serve(out, work / "http.log")
    vmlinuz, initrd = work / "suse-linux", work / "suse-initrd"
    extract(iso, {"/boot/x86_64/loader/linux": vmlinuz,
                  "/boot/x86_64/loader/initrd": initrd})
    child = qemu(target_disk, ram, iso=iso, extra_drives=[seed_disk],
                 kernel=vmlinuz, initrd=initrd,
                 # linuxrc does not bring up the network on its own, and the NET
                 # image carries no packages — without netsetup it cannot fetch
                 # the profile, and without install= it has nothing to install.
                 append=f"{SERIAL} netsetup=dhcp install={SUSE_REPO} "
                        f"autoyast=http://10.0.2.2:{HTTP_PORT}/autoyast.xml "
                        "autoyast_validation=0", timeout=3600,
                 log=work / "serial.log")
    wait_for_finish(child, "suse",
                    ["Restarting system", "reboot: Power down", "System halt"],
                    timeout=3600)
    shutdown(child)


def install_from_live_shell(distro, target_disk, seed_disk, iso, ram, work,
                            *, applier, boot_hint, login=None, timeout=2400,
                            bootstrap=None, become_root=False):
    """Boot a live ISO, mount the LIS seed, and let the applier drive the install."""
    child = qemu(target_disk, ram, iso=iso, extra_drives=[seed_disk], boot="order=d",
                 timeout=timeout, log=work / "serial.log")
    boot_hint(child)
    prompts = [r"root@archiso", r"root@nixos", r"nixos@nixos", r"localhost",
               r"\]#", r"~ ?[#$]", r"# "]
    if login:
        child.expect(["login:", *prompts], timeout=600)
        child.sendline(login)
    child.expect(prompts, timeout=900)
    if become_root:
        # Some live images autologin as an unprivileged user, and mounting the
        # seed then fails with EACCES before anything else can go wrong.
        child.sendline("sudo -i")
        child.expect(prompts, timeout=120)
    child.sendline("mkdir -p /run/lis/seed && mount /dev/vdb /run/lis/seed")
    child.expect(prompts, timeout=120)
    if bootstrap:
        # Some live images ship no python at all, so the applier cannot run
        # until its interpreter is there.
        print(f"  [{TICK}] Bootstrapping the live environment: {GRAY}{bootstrap}{RESET}")
        child.sendline(bootstrap)
        child.expect(prompts, timeout=600)
    command = (f"python3 /run/lis/seed/appliers/{applier} "
               "/run/lis/seed/recipes/system.lis.json --apply")
    if distro == "nixos":
        command = f"nix-shell -p python3 --run {command!r}"
    print(f"  [{TICK}] Running {applier} inside the live environment...")
    run_in_live_shell(child, command, "LIS_APPLY_STATUS", timeout)
    shutdown(child)


def iso_label(iso: pathlib.Path) -> str:
    """The ISO's volume label, which Anaconda needs for inst.stage2."""
    res = subprocess.run(["blkid", "-s", "LABEL", "-o", "value", str(iso)],
                         capture_output=True, text=True)
    label = res.stdout.strip()
    if not label and shutil.which("isoinfo"):
        res = subprocess.run(f"isoinfo -d -i {iso} | sed -n 's/^Volume id: //p'",
                             shell=True, capture_output=True, text=True)
        label = res.stdout.strip()
    return (label or "Fedora").replace(" ", "\\x20")


def isolinux_serial(child) -> None:
    """Arch and NixOS boot isolinux/syslinux: tab to edit, append a serial console."""
    time.sleep(3)
    child.send("\t")
    time.sleep(1)
    child.send(f" {SERIAL}")
    time.sleep(1)
    child.sendline("")


def run_stage2_qemu_installer(distro: str, target_disk: pathlib.Path,
                              seed_disk: pathlib.Path, iso_path: pathlib.Path,
                              ram: str, recipe: pathlib.Path) -> None:
    """Run the distro's native installer against the LIS document. Raises on failure."""
    print_stage_header(2, f"Running the native {distro.upper()} installer in QEMU")
    work = pathlib.Path("/tmp/lis-e2e") / distro
    work.mkdir(parents=True, exist_ok=True)

    if distro == "ubuntu":
        install_ubuntu(target_disk, seed_disk, iso_path, ram, recipe, work)
    elif distro == "debian":
        install_debian(target_disk, seed_disk, iso_path, ram, recipe, work)
    elif distro == "fedora":
        install_fedora(target_disk, seed_disk, iso_path, ram, recipe, work)
    elif distro == "suse":
        install_suse(target_disk, seed_disk, iso_path, ram, recipe, work)
    elif distro == "arch":
        install_from_live_shell(distro, target_disk, seed_disk, iso_path, ram, work,
                                applier="lis2archinstall.py", boot_hint=isolinux_serial,
                                login="root")
    elif distro == "nixos":
        install_from_live_shell(distro, target_disk, seed_disk, iso_path, ram, work,
                                applier="lis2nixos.py", boot_hint=isolinux_serial,
                                timeout=3600, become_root=True)
    elif distro == "alpine":
        install_from_live_shell(distro, target_disk, seed_disk, iso_path, ram, work,
                                applier="lis2alpine.py", boot_hint=lambda c: None,
                                login="root",
                                # The live image boots with no network and only
                                # the CD repository, which has no python at all.
                                bootstrap="ip link set eth0 up; udhcpc -i eth0 -q; "
                                          "setup-apkrepos -1; apk update; "
                                          "apk add python3")
    else:
        raise InstallFailed(f"no installer driver for distro {distro!r}")
