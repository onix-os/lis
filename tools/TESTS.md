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
| **Alpine Linux** | `setup-alpine -f answers` | [`lis2alpine.py`](file:///home/bresilla/lis/tools/appliers/lis2alpine.py) | `/mnt/seed/answers` | BIOS / UEFI |
| **NixOS** | `disko` + `nixos-install` | [`lis2nixos.py`](file:///home/bresilla/lis/tools/appliers/lis2nixos.py) | `disko.nix` + `configuration.nix` | BIOS / UEFI |
| **Ubuntu** | Subiquity autoinstall | [`lis2autoinstall.py`](file:///home/bresilla/lis/tools/appliers/lis2autoinstall.py) | `user-data` (Subiquity YAML) | BIOS / UEFI |
| **Arch Linux** | `archinstall` + `pacstrap` | [`lis2archinstall.py`](file:///home/bresilla/lis/tools/appliers/lis2archinstall.py) | `user_configuration.json` + `creds` | BIOS / UEFI |
| **Fedora** | Kickstart | [`lis2kickstart.py`](file:///home/bresilla/lis/tools/appliers/lis2kickstart.py) | `ks.cfg` (Kickstart format) | BIOS / UEFI |
| **openSUSE** | AutoYaST | [`lis2agama.py`](file:///home/bresilla/lis/tools/appliers/lis2agama.py) | `autoyast.xml` (AutoYaST profile) | BIOS / UEFI |
| **Debian** | Preseed | [`lis2debian.py`](file:///home/bresilla/lis/tools/appliers/lis2debian.py) | `preseed.cfg` (Debconf answers) | BIOS / UEFI |

---

## Detailed 4-Stage Test Lifecycle

Every distribution test execution follows a 4-stage pipeline orchestrated by [`tools/e2e/main.py`](file:///home/bresilla/lis/tools/e2e/main.py):

### Stage 1: Recipe Intent Breakdown and Expectations
- Reads and parses the LIS recipe document (e.g., [`docs/examples/test-full-install.lis.json`](file:///home/bresilla/lis/docs/examples/test-full-install.lis.json)).
- Validates structural syntax against the LIS JSON Schema ([`spec/schema.json`](file:///home/bresilla/lis/spec/schema.json)).
- Extracts and displays target parameters: target architecture (`x86_64`), partitioning scheme (`/boot` ESP VFAT, Swap, Root BTRFS), user accounts (`fakeuser`), software packages, and script hooks (`pre_install`, `post_install`, `firstboot`).

### Stage 2: QEMU Serial Console Automated Installation
- **Seed Drive Generation**: Invokes [`tools/lis-make-seed`](file:///home/bresilla/lis/tools/lis-make-seed) to assemble a 64MB FAT32 seed image containing `/lis.json`, `/recipes/system.lis.json`, and the empty `/unattended` consent marker.
- **ISO Resolution**: Resolves or downloads the official distro Live ISO into local cache using [`tools/e2e/iso.py`](file:///home/bresilla/lis/tools/e2e/iso.py).
- **Virtual Machine Boot**: Spawns a background QEMU process (`qemu-system-x86_64`) with KVM hardware acceleration, attaching the Live ISO, a blank 20GB target QCOW2 virtual disk image (`/home/bresilla/lis/build/e2e-<distro>-target.qcow2`), and the LIS seed drive.
- **Automated PTY Interaction**: Uses `pexpect` over serial console (`-nographic -append "console=ttyS0,115200n8"`) or an embedded HTTP seed responder server to inject installation profiles non-interactively.
- **Birth Certificate Registration**: Upon completion, the installer applier writes the LIS birth certificate to `/var/lib/lis/system.lis.json` in the target filesystem and executes chroot post-installation script hooks.

### Stage 3: Target Disk Image Inspection
- Inspects the newly created target virtual disk image (`e2e-<distro>-target.qcow2`) in `/home/bresilla/lis/build/`.
- Verifies that the disk image was formatted and populated with non-zero allocation size.

### Stage 4: Reboot Test and Live Guest Spec Verification
- **Clean Boot**: Boots QEMU strictly from the installed target disk image with no Live ISO or seed drive attached.
- **Console Authentication**: Monitors serial output for getty login prompts (`login:`) and authenticates into the installed system.
- **Live Verification Checklist**: Executes verification commands inside the live guest system over serial PTY and verifies expectations:
  1. Hostname configuration (`cat /etc/hostname` matches `lis-test-host`).
  2. User account creation (`id fakeuser` and `/etc/passwd` record).
  3. LIS birth certificate existence (`/var/lib/lis/system.lis.json`).
  4. Script hook execution markers (`pre_install`, `chroot_hook`, `post_install`, `user_hook`).

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
- `--verify-only`: Skip Live ISO installation (Stage 2) and run Stage 4 live verification directly against an existing `e2e-<distro>-target.qcow2` image.

---

## Code Architecture and Modules

The E2E test engine codebase is located under [`tools/e2e/`](file:///home/bresilla/lis/tools/e2e/):

- [`tools/lis-test-e2e`](file:///home/bresilla/lis/tools/lis-test-e2e) — Executable CLI entrypoint.
- [`tools/e2e/main.py`](file:///home/bresilla/lis/tools/e2e/main.py) — Main test runner, CLI argument parser, and multi-distro summary table generator.
- [`tools/e2e/installer.py`](file:///home/bresilla/lis/tools/e2e/installer.py) — QEMU process manager, HTTP seed server, and `pexpect` serial console state machine for Stage 2.
- [`tools/e2e/verifier.py`](file:///home/bresilla/lis/tools/e2e/verifier.py) — Target disk inspector for Stage 3 and live guest reboot verifier for Stage 4.
- [`tools/e2e/iso.py`](file:///home/bresilla/lis/tools/e2e/iso.py) — ISO URL catalog, cache manager, kernel/initrd extractor, and HTTP preseed/kickstart/autoyast server.
- [`tools/lis-make-seed`](file:///home/bresilla/lis/tools/lis-make-seed) — FAT32 LIS seed drive (`LIS.img`) builder.

---

## Host System Prerequisites

To run the E2E VM test suite on a Linux host:

1. **Hypervisor**: `qemu-system-x86_64` with KVM support (`/dev/kvm` accessible).
2. **Utilities**: `qemu-img`, `dosfstools` (`mkfs.vfat`), `xorriso`.
3. **Python Environment**: Python 3.10+ with `pexpect` (`pip install pexpect` or `apt install python3-pexpect`).
