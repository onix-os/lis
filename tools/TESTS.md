# LIS Automated End-to-End VM Test Suite

The **LIS End-to-End (E2E) VM Test Engine** ([`tools/lis-test-e2e`](file:///home/bresilla/lis/tools/lis-test-e2e)) provides fully automated virtual machine integration testing for the Linux Installation Specification across 7 major Linux distributions.

It proves the core premise of LIS: **one declarative specification file can drive a complete, unattended hardware installation from a bare Live ISO to a fully booted system across any Linux distribution.**

---

## 🚀 Distro Support Matrix

| Distribution | Native Installer Mechanism | Translation Applier | Target Firmware |
| :--- | :--- | :--- | :--- |
| **Alpine Linux** | `setup-alpine -f /mnt/seed/answers` | [`lis2alpine.py`](file:///home/bresilla/lis/tools/appliers/lis2alpine.py) | BIOS / UEFI |
| **NixOS** | `disko` + `nixos-install` | [`lis2nixos.py`](file:///home/bresilla/lis/tools/appliers/lis2nixos.py) | BIOS / UEFI |
| **Ubuntu** | Subiquity `user-data` (autoinstall) | [`lis2autoinstall.py`](file:///home/bresilla/lis/tools/appliers/lis2autoinstall.py) | BIOS / UEFI |
| **Arch Linux** | `archinstall` + `pacstrap` | [`lis2archinstall.py`](file:///home/bresilla/lis/tools/appliers/lis2archinstall.py) | BIOS / UEFI |
| **Fedora** | Kickstart (`ks.cfg`) | [`lis2kickstart.py`](file:///home/bresilla/lis/tools/appliers/lis2kickstart.py) | BIOS / UEFI |
| **openSUSE** | AutoYaST (`autoyast.xml`) | [`lis2agama.py`](file:///home/bresilla/lis/tools/appliers/lis2agama.py) | BIOS / UEFI |
| **Debian** | Preseed (`preseed.cfg`) | [`lis2debian.py`](file:///home/bresilla/lis/tools/appliers/lis2debian.py) | BIOS / UEFI |

---

## 🔄 The 4-Stage Test Lifecycle

Every distro test run executes through a strict 4-stage validation pipeline:

```
┌─────────────────────────────────────────────────────────────────────────┐
│ STAGE 1: RECIPE INTENT BREAKDOWN & EXPECTATIONS                          │
│ • Validates LIS JSON recipe against schema                              │
│ • Parses partitions, filesystems, users, software roles & script hooks  │
└────────────────────────────────────┬────────────────────────────────────┘
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ STAGE 2: EXECUTING INSTALLER IN QEMU SERIAL CONSOLE                      │
│ • Downloads/resolves target ISO & builds FAT32 LIS seed drive (.img)    │
│ • Boots QEMU VM serial console with KVM acceleration                    │
│ • Injects seed profile via HTTP or seed volume & drives serial PTY      │
│ • Writes LIS birth certificate (/var/lib/lis/system.lis.json)           │
└────────────────────────────────────┬────────────────────────────────────┘
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ STAGE 3: TARGET DISK IMAGE INSPECTION                                   │
│ • Verifies target QCOW2 virtual disk image creation & non-zero size     │
└────────────────────────────────────┬────────────────────────────────────┘
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ STAGE 4: REBOOT TEST & LIVE GUEST SPEC VERIFICATION                     │
│ • Boots strictly from installed target disk image (no ISO attached)     │
│ • Authenticates via serial console getty                                │
│ • Executes live verification checklist: hostname, user account,         │
│   birth certificate, pre-install/post-install/chroot script hooks       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 💻 Running the Test Suite

### 1. Run the Full Matrix Across All Distributions

To run the complete continuous test suite across all 7 distributions:

```bash
./tools/lis-test-e2e --distro all
```

Upon completion, a unified results matrix table is printed:

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

### 2. Run a Test for a Single Distribution

```bash
./tools/lis-test-e2e --distro fedora
./tools/lis-test-e2e --distro arch
./tools/lis-test-e2e --distro ubuntu
```

### 3. Advanced Options

- **Custom Recipe**: Specify a custom LIS recipe JSON file:
  ```bash
  ./tools/lis-test-e2e --distro alpine --recipe docs/examples/test-full-install.lis.json
  ```
- **Custom Hardware Specs**: Adjust VM RAM or target disk size:
  ```bash
  ./tools/lis-test-e2e --distro nixos --ram 8G --disk-size 40G
  ```
- **Live Verification Only**: Skip ISO installation and run Stage 4 verification directly against an existing installed disk image:
  ```bash
  ./tools/lis-test-e2e --distro debian --verify-only
  ```

---

## 📂 Code Structure & Architecture

The E2E test engine is modularized inside [`tools/e2e/`](file:///home/bresilla/lis/tools/e2e/):

- [`tools/lis-test-e2e`](file:///home/bresilla/lis/tools/lis-test-e2e) — Executable CLI entrypoint script wrapper.
- [`tools/e2e/main.py`](file:///home/bresilla/lis/tools/e2e/main.py) — Test orchestrator, Stage 1 breakdown, and multi-distro summary matrix renderer.
- [`tools/e2e/installer.py`](file:///home/bresilla/lis/tools/e2e/installer.py) — Stage 2 QEMU VM controller, seed server responder, and `pexpect` serial console state machine.
- [`tools/e2e/verifier.py`](file:///home/bresilla/lis/tools/e2e/verifier.py) — Stage 3 image inspector and Stage 4 guest serial reboot verifier.
- [`tools/e2e/iso.py`](file:///home/bresilla/lis/tools/e2e/iso.py) — ISO URL catalog, cache manager, and HTTP seed server.
- [`tools/lis-make-seed`](file:///home/bresilla/lis/tools/lis-make-seed) — FAT32 LIS seed volume (`LIS.img`) generator tool.

---

## 🛠️ Host System Requirements

To execute the E2E test suite locally on a Linux host system:

- **Hypervisor**: `qemu-system-x86_64` with KVM support enabled (`/dev/kvm` accessible).
- **Disk Tools**: `qemu-img`, `dosfstools` (`mkfs.vfat`), `xorriso`.
- **Python**: Python 3.10+ with `pexpect` installed (`pip install pexpect` or `apt install python3-pexpect`).
