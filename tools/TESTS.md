# LIS Automated End-to-End VM Test Suite

The **LIS End-to-End (E2E) VM Test Engine** ([`tools/lis-test-e2e`](file:///home/bresilla/lis/tools/lis-test-e2e)) provides fully automated virtual machine integration testing for the Linux Installation Specification across 7 major Linux distributions.

It validates the core contract of LIS: **a single declarative specification file (`system.lis.json`) can drive a complete, unattended hardware installation from a bare Live ISO to a fully booted system across any supported Linux distribution.**

---

## System Architecture and Integration Flow

The diagram below illustrates how a single LIS recipe document ties together with seed volumes, translation appliers, distro installers, QEMU serial controllers, and live verification checks:

```text
                           ┌───────────────────────────┐
                           │   system.lis.json Recipe  │
                           └─────────────┬─────────────┘
                                         │
                                         ▼
                           ┌───────────────────────────┐
                           │    tools/lis-make-seed    │
                           └─────────────┬─────────────┘
                                         │
                                         ▼
                           ┌───────────────────────────┐
                           │    FAT32 LIS Seed Volume  │
                           │   (LIS.img / lis.json)    │
                           └─────────────┬─────────────┘
                                         │
              ┌──────────────────────────┼──────────────────────────┐
              │                          │                          │
              ▼                          ▼                          ▼
    ┌───────────────────┐      ┌───────────────────┐      ┌───────────────────┐
    │  lis2alpine.py    │      │  lis2kickstart.py │      │  lis2debian.py    │
    │  -> setup-alpine  │      │  -> Kickstart ks  │      │  -> Preseed cfg   │
    └─────────┬─────────┘      └─────────┬─────────┘      └─────────┬─────────┘
              │                          │                          │
              └──────────────────────────┼──────────────────────────┘
                                         │
                                         ▼
                           ┌───────────────────────────┐
                           │  Stage 2: QEMU Serial VM  │
                           │ (qemu-system-x86_64 + KVM)│
                           └─────────────┬─────────────┘
                                         │
                                         ▼
                           ┌───────────────────────────┐
                           │ Stage 3: Target QCOW2 Disk│
                           └─────────────┬─────────────┘
                                         │
                                         ▼
                           ┌───────────────────────────┐
                           │ Stage 4: Reboot & Verify  │
                           │ (Live Guest Verification) │
                           └───────────────────────────┘
```

---

## Distro Applier and Installer Mapping

Each Linux distribution uses a dedicated LIS applier script to convert the abstract LIS intent into the distribution's native unattended installer configuration:

| Distribution | Native Installer Mechanism | Translation Applier | Target Config Generated | Target Firmware |
| :--- | :--- | :--- | :--- | :--- |
| **Alpine Linux** | `setup-alpine -f answers` | [`lis2alpine.py`](file:///home/bresilla/lis/tools/appliers/lis2alpine.py) | `answers` + `lis-post.sh` | BIOS / UEFI |
| **NixOS** | `disko` + `nixos-install` | [`lis2nixos.py`](file:///home/bresilla/lis/tools/appliers/lis2nixos.py) | `disko.nix` + `configuration.nix` | BIOS / UEFI |
| **Ubuntu** | Subiquity autoinstall | [`lis2autoinstall.py`](file:///home/bresilla/lis/tools/appliers/lis2autoinstall.py) | `user-data` (Subiquity YAML) | BIOS / UEFI |
| **Arch Linux** | `archinstall` + `pacstrap` | [`lis2archinstall.py`](file:///home/bresilla/lis/tools/appliers/lis2archinstall.py) | `user_configuration.json` + `creds` | BIOS / UEFI |
| **Fedora** | Kickstart | [`lis2kickstart.py`](file:///home/bresilla/lis/tools/appliers/lis2kickstart.py) | `ks.cfg` (Kickstart format) | BIOS / UEFI |
| **openSUSE** | Agama / AutoYaST | [`lis2agama.py`](file:///home/bresilla/lis/tools/appliers/lis2agama.py) | `profile.json` + `autoyast.xml` | BIOS / UEFI |
| **Debian** | Preseed | [`lis2debian.py`](file:///home/bresilla/lis/tools/appliers/lis2debian.py) | `preseed.cfg` (Debconf answers) | BIOS / UEFI |

---

## What the suite is allowed to do

The suite exists to answer one question: **did the distro's own installer, driven
by a configuration generated from the LIS document, produce the system the
document describes?**

Three rules keep that question honest, and they are worth stating because
earlier revisions of this harness broke all three:

1. **The harness never installs anything itself.** No `sfdisk`, no
   `rsync -aHAX / /target/`, no hand-placed bootloader. Stage 2 boots the
   distro's installer and hands it a configuration; whatever lands on the disk
   is that installer's work.
2. **The configuration always comes from an applier.** Stage 2 shells out to
   `tools/appliers/lis2*.py`. It never embeds a hand-written preseed, kickstart
   or AutoYaST profile — a hand-written one would test nothing about the
   translator it is supposed to exercise.
3. **The verifier never writes what it checks.** Stages 3 and 4 read the
   installed system. They do not create the user, write the hook markers or set
   the hostname before looking for them.

An applier that refuses the document (SPEC §2.3) is reported as `REFUSED`, not
`FAILED`: the recipe declared intent that distro's installer cannot express, the
applier said so before anything was written, and the fix belongs in the recipe or
the applier — never in the harness. The default recipe declares a btrfs root
subvolume (`@` at `/`), which is why Ubuntu, Debian and Alpine refuse it: curtin,
partman and setup-disk all install into the top-level subvolume and cannot
relocate the installed root afterwards, while disko, archinstall, Anaconda and
AutoYaST create subvolumes before installing.

---

## Detailed 4-Stage Test Lifecycle

Every distribution test follows a 4-stage pipeline orchestrated by
[`tools/e2e/main.py`](file:///home/bresilla/lis/tools/e2e/main.py):

### Stage 1: Recipe intent breakdown
- Reads the LIS recipe (default
  [`docs/examples/test-full-install.lis.json`](file:///home/bresilla/lis/docs/examples/test-full-install.lis.json)).
- Prints the declared storage stack, users, role, software and script hooks —
  the same facts Stages 3 and 4 go on to check.

### Stage 2: The distro's own installer, in QEMU
- **Seed**: [`tools/lis-make-seed`](file:///home/bresilla/lis/tools/lis-make-seed)
  builds the FAT32 `LIS` volume (`/lis.json`, `/recipes/system.lis.json`, the
  empty `/unattended` consent marker, and `/appliers/`).
- **Translation**: the matching applier runs on the host. A non-zero exit ends
  the test — the applier refused the document.
- **Delivery**, per distro:

  | Distro | How the installer receives the generated config |
  | :--- | :--- |
  | Ubuntu | `user-data`/`meta-data` on a `CIDATA` volume; kernel cmdline `autoinstall`. Subiquity's own NoCloud path — no terminal hijack. |
  | Debian | `preseed.cfg` over HTTP; `auto=true priority=critical url=…` |
  | Fedora | `ks.cfg` over HTTP; `inst.ks=…` |
  | openSUSE | `autoyast.xml` over HTTP; `autoyast=…` |
  | Arch | live shell mounts the seed, runs `lis2archinstall.py --apply` → `archinstall` |
  | NixOS | live shell mounts the seed, runs `lis2nixos.py --apply` → `disko` + `nixos-install` |
  | Alpine | live shell mounts the seed, runs `lis2alpine.py --apply` → `setup-alpine` |

  Ubuntu, Debian, Fedora and openSUSE boot with `-kernel`/`-initrd` extracted
  from the ISO, so the kernel command line is set directly instead of by
  simulating keystrokes in a bootloader menu.

### Stage 3: Offline image inspection
Mounts the target image read-only with `guestfish` and checks, against the
recipe: hostname, each declared user in `/etc/passwd`, each declared password
hash in `/etc/shadow`, the birth certificate at `/var/lib/lis/system.lis.json`,
and every script-hook marker file the recipe writes. Skipped with a notice when
`guestfish` is unavailable — Stage 4 covers the same facts.

### Stage 4: Reboot and live verification
Boots the target disk alone (no ISO, no seed), reaches a shell, and re-checks
the same expectations in the running system, plus first-boot hook markers and
whether declared packages are on `PATH`. Any failed check fails the distro.

Expectations are derived from the recipe, not hardcoded: hook markers are
discovered by scanning each `scripts.*` entry for `echo MARKER > /path`, so
changing the recipe changes what is verified.

---

## Command Line Usage and Examples

### Run the Full Matrix Across All Distributions

To execute the complete test suite across all 7 distributions sequentially:

```bash
./tools/lis-test-e2e --distro all
```

Output summary matrix rendered upon completion:

```text
╔══════════════════════════════════════════════════════════════════════╗
║           LIS MULTI-DISTRO END-TO-END SUITE RESULTS MATRIX           ║
╚══════════════════════════════════════════════════════════════════════╝

  • ALPINE     : ✓ PASSED
  • NIXOS      : ✓ PASSED
  • UBUNTU     : ✓ PASSED
  • ARCH       : ✓ PASSED
  • FEDORA     : ✓ PASSED
  • SUSE       : ✓ PASSED
  • DEBIAN     : ✓ PASSED
```

### Run a Single Distribution Test

```bash
./tools/lis-test-e2e --distro alpine
./tools/lis-test-e2e --distro nixos
./tools/lis-test-e2e --distro fedora
```

### Advanced Flags

- `--recipe <path>`: Use a custom LIS recipe JSON file.
- `--ram <size>`: Set VM memory allocation (default: `4G`).
- `--disk-size <size>`: Set target QCOW2 virtual disk size (default: `20G`).
- `--verify-only`: Skip the install (Stage 2) and verify an existing `e2e-<distro>-target.qcow2` image.
- `--keep`: Keep build artifacts between distros during an `--distro all` run.

The runner exits non-zero if any distro fails, so it can gate CI directly.

---

## Code Architecture and Modules

The E2E test engine codebase is located under [`tools/e2e/`](file:///home/bresilla/lis/tools/e2e/):

- [`tools/lis-test-e2e`](file:///home/bresilla/lis/tools/lis-test-e2e) — Executable CLI entrypoint.
- [`tools/e2e/main.py`](file:///home/bresilla/lis/tools/e2e/main.py) — Main test runner, CLI argument parser, and multi-distro summary table generator.
- [`tools/e2e/installer.py`](file:///home/bresilla/lis/tools/e2e/installer.py) — Stage 2: runs the appliers, builds the CIDATA volume, serves generated configs over HTTP, boots QEMU and waits. One driver per distro.
- [`tools/e2e/verifier.py`](file:///home/bresilla/lis/tools/e2e/verifier.py) — Stages 3 and 4: expectations derived from the recipe, read back from the installed system.
- [`tools/e2e/iso.py`](file:///home/bresilla/lis/tools/e2e/iso.py) — ISO URL catalog and download cache.
- [`tools/lis-make-seed`](file:///home/bresilla/lis/tools/lis-make-seed) — FAT32 LIS seed drive (`LIS.img`) builder.

---

## Host System Prerequisites

To run the E2E VM test suite on a Linux host:

1. **Hypervisor**: `qemu-system-x86_64` with KVM support (`/dev/kvm` accessible).
2. **Utilities**: `qemu-img`, `dosfstools` (`mkfs.vfat`), `mtools` (`mcopy`/`mmd`), `xorriso` (`osirrox`), and optionally `libguestfs` (`guestfish`) for Stage 3.
3. **Python Environment**: Python 3.10+ with `pexpect` (`pip install pexpect` or `apt install python3-pexpect`).

Each run writes the guest's serial console to `/tmp/lis-e2e/<distro>/serial.log`. When
a distro fails, that file is where the installer says why.
