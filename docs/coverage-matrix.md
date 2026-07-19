# Coverage matrix — what installers configure, and where LIS stands

Survey across the automation formats of the major distro families:
[Kickstart](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/automatically_installing_rhel/kickstart-commands-and-options-reference)
(RHEL/Fedora),
[autoinstall](https://canonical-subiquity.readthedocs-hosted.com/en/latest/reference/autoinstall-reference.html)
(Ubuntu),
[Agama](https://documentation.suse.com/sles/16.0/html/SLES-x86-64-agama-automated-installation/index.html)
(SUSE 16) and AutoYaST,
[archinstall](https://wiki.archlinux.org/title/Archinstall) (Arch),
[setup-alpine answerfiles](https://wiki.alpinelinux.org/wiki/Using_an_answerfile_with_setup-alpine)
(Alpine),
[Calamares](https://calamares.euroquis.nl/) (Manjaro/EndeavourOS/many desktop distros),
disko (NixOS).

Legend: ✔ = in LIS v0.1 · ✚ = **added to LIS v0.1 by this survey** · ◌ = planned · ✗ = non-goal

| Concern | Who asks for it | LIS |
|---|---|---|
| Disk selection by match rules | Agama, Kickstart (`ignoredisk`), disko | ✔ `target.disks[].match` |
| Partitions / LVM / RAID / LUKS / btrfs subvols | all | ✔ `storage` |
| RAID hot spares | Kickstart, AutoYaST | ✚ `raid[].spares` |
| **Adopt/reuse existing partitions (keep ESP)** | Calamares, autoinstall, Agama | ✚ `partitions[].existing` |
| **Resize existing partition (install alongside)** | Calamares, Agama | ✚ `existing.resize` |
| Root snapshots + bootable rollback | openSUSE (snapper), Manjaro (timeshift), grub-btrfs | ✚ `storage.snapshots` |
| zram / swapfile | archinstall, Fedora default | ✔ `storage.swap` |
| ZFS root | Ubuntu (past), NixOS, DIY | ✚ `fs: zfs` (single-fs; datasets ◌) |
| Bootloader choice + timeout | all | ✔ `boot` |
| os-prober / dual-boot menu entries | GRUB distros, Calamares | ✚ `boot.os_prober` |
| Bootloader password | Kickstart (`bootloader --password`) | ✚ `boot.password_hash` |
| Serial console | Kickstart, server farms | ✚ `boot.console.serial` |
| Secure Boot posture | Ubuntu (shim), Fedora, sbctl world | ✚ `boot.secure_boot` |
| Unified Kernel Images | systemd/UAPI direction, Arch | ✚ `boot.uki` |
| Kernel variant / params / module blacklist | archinstall (kernels), Kickstart | ✔ `boot.kernel` |
| Initramfs generator + extra modules | Kickstart `driverdisk`-ish, Arch mkinitcpio | ✚ `boot.initramfs` |
| Hostname / timezone / locale / keymap | all | ✔ `system` |
| **hwclock UTC vs localtime** (Windows dual boot) | archinstall, Calamares | ✚ `system.hwclock` |
| **NTP enable + servers + provider** | Kickstart `timesource`, Alpine `NTPOPTS`, autoinstall | ✚ `system.time` |
| Granular `LC_*` overrides | AutoYaST, Debian | ✚ `system.locale_overrides` |
| Console font | Alpine, Arch console setups | ✚ `system.keymap.font` |
| Domain name | Kickstart, AutoYaST | ✚ `system.domain` |
| SELinux/AppArmor mode | Kickstart `selinux`, SUSE `security` | ✔ `system.security.module` |
| Kernel crash dumps | autoinstall `kernel-crash-dumps`, RHEL kdump | ✚ `system.kdump` |
| Telemetry / popularity-contest opt-out | Debian popcon, Ubuntu report | ✚ `system.telemetry` |
| Users, groups, admin, hashes, ssh keys | all | ✔ `users` |
| Passwordless sudo | archinstall, cloud-init | ✚ `users[].sudo` |
| Static /etc/hosts entries | Kickstart `%post` folklore, AutoYaST | ✚ `network.hosts` |
| Interfaces, wifi, firewall, sshd | all | ✔ `network` |
| Bonds / bridges / VLANs | Kickstart, autoinstall (netplan), AutoYaST | ◌ minor revision |
| Role/profile selection | archinstall profiles, Kickstart groups, Agama patterns | ✔ `software.role` |
| Package excludes | Kickstart `%packages -pkg` | ✚ `software.exclude` |
| Flatpaks / snaps | Ubuntu autoinstall `snaps`, Fedora flatpaks | ✔/✚ `software.flatpak`, `software.snap` |
| Extra repositories + keys | Kickstart `repo`, Agama `extraRepositories`, apt config | ✗ core → `x-<distro>` |
| Display manager + autologin | Calamares `displaymanager`, archinstall | ✚ `desktop` |
| Audio stack (pipewire/pulse) | archinstall | ✚ `desktop.audio` |
| Bluetooth / printing enable | Calamares services, desktop distros | ✚ `desktop.bluetooth/printing` |
| GPU driver + microcode + firmware | Ubuntu `drivers`, Manjaro mhwd, archinstall | ✔/✚ `drivers` |
| HTTP proxy (install + target) | Alpine `PROXYOPTS`, autoinstall `proxy`, Kickstart | ✚ `proxy` |
| Mirror selection | archinstall regions, autoinstall apt, Alpine `APKREPOSOPTS` | ✚ `mirror` |
| Vendor registration (RHEL/SUSE/Ubuntu Pro) | Kickstart `rhsm`, Agama `registration`, autoinstall `ubuntu-pro` | ✚ `registration` |
| Literal files into target | Ignition, Agama `files` | ✔ `files` |
| Pre/post scripts | Kickstart `%pre/%post`, autoinstall `late-commands`, Agama `scripts` | ✔ `scripts` |
| **First-boot scripts** | Kickstart `firstboot`, Agama, cloud-init | ✚ `scripts.firstboot` |
| Finish action (reboot/poweroff) | Kickstart `reboot`, autoinstall `shutdown` | ✚ `installer.on_finish` |
| Partially-interactive runs | autoinstall `interactive-sections` | ✚ `installer.interactive` |
| Predefined answers to installer questions | Agama `questions` | ✚ `installer.answers` |
| OEM / first-run user creation mode | Calamares OEM, systemd-firstboot | ◌ |
| iSCSI / DASD / zFCP / multipath activation | Agama (SLES on IBM Z), Kickstart | ◌ `target` extension |
| LUKS key escrow | Kickstart `--escrowcert` | ◌ |
| Accessibility profiles | d-i, Fedora | ◌ |
| Cloud/image build configs | mkosi, Ignition, cloud-init | ✗ non-goal |
| Post-boot config management | Ansible/cloud-init | ✗ non-goal |

## Notes on the deliberate exclusions

- **Repositories stay distro-specific** (`x-arch.repositories`, `x-debian.apt`):
  a repo URL + key is meaningless across distro boundaries, and pretending
  otherwise would break the no-silent-drift rule.
- **Package names**: only the small intent registry is portable; everything else
  is verbatim-with-MUST-fail, or namespaced.
- **OEM mode** is deferred until the `users`-at-first-boot semantics are designed
  properly (it inverts when §9 runs).
