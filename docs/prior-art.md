# Prior art

Every major distro family already has a declarative(ish) install format. None of
them cross distro boundaries. LISS borrows deliberately from each.

## Per-distro automation formats

| Format | Distro | Encoding | What LISS takes from it |
|---|---|---|---|
| [Kickstart](https://pykickstart.readthedocs.io/) | RHEL/Fedora (Anaconda) | own directive language | `%post` script escape hatch; proof that install automation is 25+ years old |
| [preseed](https://wiki.debian.org/DebianInstaller/Preseed) | Debian (d-i) | debconf answers | cautionary tale: answering *installer prompts* instead of describing the *system* couples the file to one installer forever |
| [autoinstall](https://canonical-subiquity.readthedocs-hosted.com/en/latest/reference/autoinstall-reference.html) | Ubuntu (subiquity) | YAML, JSON-Schema-validated | the defaulting rule: any unanswered question takes the default instead of prompting; schema validation |
| [Agama profiles](https://documentation.suse.com/sles/16.0/html/SLES-x86-64-agama-automated-installation/index.html) | SUSE 16 | JSON (+ Jsonnet) | the closest relative: JSON profile, local validation, device `match` selectors for disks |
| [archinstall config](https://wiki.archlinux.org/title/Archinstall) | Arch | JSON | separation of config vs credentials files; `--dry-run` producing a replayable config |
| [AutoYaST](https://doc.opensuse.org/projects/autoyast/) | SUSE (legacy) | XML | what to avoid: implementation-coupled XML that had to be replaced wholesale (by Agama) |
| [disko](https://github.com/nix-community/disko) | NixOS | Nix | the layered storage model (disk → partition → LUKS → LVM → fs → subvolume) as data |

## Adjacent standards

| Project | Scope | Relevance |
|---|---|---|
| [cloud-init](https://cloudinit.readthedocs.io/) | first-boot configuration | **the existence proof**: one YAML format consumed by every distro and every cloud. Won by being useful to consume, not by decree. Does not do installation. |
| [Ignition](https://coreos.github.io/ignition/rationale/) / [Butane](https://coreos.github.io/butane/) | image-based first boot (CoreOS/Flatcar) | strict versioned JSON schema; the human-YAML → canonical-JSON compiler split; `files` as a primitive; declarative-or-fail attitude |
| [systemd-repart](https://www.freedesktop.org/software/systemd/man/latest/repart.d.html) | declarative partitioning | partition *roles* rather than explicit numbers |
| [UAPI Group](https://uapi-group.org/specifications/) — Discoverable Partitions Spec | partition semantics | GPT type UUIDs per role; LISS `role` maps onto DPS types |
| [OpenSUSE Agama's storage `match`](https://agama-project.github.io/) | device selection | deterministic disk matching by serial/model/size instead of `/dev/sdX` lottery |

## The gap LISS fills

- Kickstart/preseed/autoinstall/Agama/archinstall each describe an installation —
  **for exactly one installer**. The knowledge encoded in a Kickstart file is
  unportable to Arch, and vice versa.
- cloud-init/Ignition are cross-platform but start **after** the OS exists (or the
  image is built); they do not choose disks, bootloaders, or distros.
- Configuration management (Ansible, Chef) assumes a running system.

Nothing today lets one frontend (wizard, web UI, fleet tool) target many distros,
or lets one machine description be re-used when switching distros. That interop
layer — the file *between* the wizard and the installer — is LISS.

## Lessons applied

1. **Describe the system, not the dialog** (anti-preseed).
2. **Defaults, not prompts** (autoinstall's rule).
3. **Versioned schema + local validation** (Ignition, Agama).
4. **Human format compiles to canonical format** (Butane → Ignition; LISS YAML → JSON).
5. **Deterministic device matching** (Agama `match`, never `/dev/sda` by faith).
6. **Roles over magic numbers** (systemd-repart, UAPI DPS).
7. **Win by being useful to consume** (cloud-init): ship working appliers, not RFCs.
