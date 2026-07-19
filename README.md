<p align="center"><img src="assets/logo.svg" width="180" alt="LIS — an oak tree whose roots end in three nodes"></p>

# LIS — Linux Installation Specification

**One file that describes an installed Linux system. Any wizard can write it. Any distro can apply it.**

*Lis* is Albanian for **oak**: one trunk, many roots.

LIS is a declarative, distro-neutral specification of a Linux installation: disks,
filesystems, boot, users, network, and software *intent*. A LIS document is produced
by an installer frontend (a TUI, a GUI, a web form, a script — anything) and consumed
by a distro-specific **applier** that turns it into a real system.

```
┌─────────────┐   ┌─────────────┐   ┌──────────────┐
│  nox TUI    │   │ web wizard  │   │ hand-written │      frontends
└──────┬──────┘   └──────┬──────┘   └──────┬───────┘      (produce LIS)
       └───────────────  ▼  ───────────────┘
                 ╔═══════════════╗
                 ║  system.lis  ║                        the standard
                 ╚═══════╤═══════╝
       ┌───────────────  ▼  ───────────────┐
┌──────┴──────┐   ┌──────┴──────┐   ┌──────┴───────┐
│ NixOS       │   │ Arch        │   │ Debian       │      appliers
│ applier     │   │ applier     │   │ applier      │      (consume LIS)
│ → disko.nix │   │ → pacstrap  │   │ → debootstrap│
│ → config.nix│   │ → archinstall│  │ → preseed    │
└─────────────┘   └─────────────┘   └──────────────┘
```

## Why

Every distro reinvented the same file with an incompatible dialect:

| Distro | Format | Scope |
|---|---|---|
| RHEL/Fedora | Kickstart | own directives, own parser |
| Debian | preseed | debconf answers |
| Ubuntu | autoinstall (YAML) | subiquity-only |
| SUSE 16 | Agama profiles (JSON) | Agama-only |
| Arch | archinstall JSON | archinstall-only |
| NixOS | disko + configuration.nix | Nix-only |
| CoreOS/Flatcar | Ignition (JSON) | image-based first boot |

They all answer the same questions: *which disk, what partitions, which filesystems,
who are the users, what's the hostname, what software role does this machine play.*
cloud-init proved that a single cross-distro config format can win — but it only
covers post-install configuration. Installation itself has no standard. LIS is that
standard.

## Design principles

1. **Declarative intent, not commands.** A LIS file states *what the system is*,
   never *how to build it*. `"role": "desktop:gnome"` — not a package list of 400 names.
2. **Portable core + namespaced extensions.** Sections that are genuinely universal
   (storage, boot, system, users, network) are the core. Everything distro-specific
   lives under `x-<name>` extensions (`x-nixos`, `x-arch`) that other appliers ignore.
3. **No silent drift.** An applier MUST refuse a document containing core intent it
   cannot honor. A LIS file that applies successfully means the same thing everywhere.
4. **Machine-validated.** The canonical format is JSON with a published JSON Schema.
   YAML and TOML are accepted authoring formats with 1:1 mapping (the Butane→Ignition
   pattern).
5. **Versioned.** Every document carries `"lis": "<semver>"`. Appliers declare which
   versions they accept.
6. **Secrets-safe.** Passwords are crypt(3) hashes, never plaintext. External material
   (keys, tokens) is referenced, not embedded.

## The 30-second example

```json
{
  "lis": "0.1.0",
  "system": { "hostname": "tron", "timezone": "Europe/Amsterdam", "locale": "en_US.UTF-8" },
  "target": { "firmware": "uefi", "disks": [ { "id": "main", "match": { "largest": true } } ] },
  "storage": {
    "partitions": [
      { "disk": "main", "role": "esp", "size": "1GiB" },
      { "disk": "main", "role": "root", "size": "rest", "fs": "btrfs",
        "subvolumes": [ { "name": "@", "mountpoint": "/" },
                        { "name": "@home", "mountpoint": "/home" } ] }
    ]
  },
  "users": [ { "name": "bresilla", "admin": true,
               "password": { "hash": "$6$…" }, "shell": "zsh" } ],
  "software": { "role": "server", "services": { "enable": ["sshd"] } }
}
```

An applier on NixOS turns this into `disko.nix` + `configuration.nix`; on Arch into a
partitioning plan + `pacstrap` + config files. Same file, same machine, either distro.

## Repository layout

- [`SPEC.md`](SPEC.md) — the specification (v0.1.0-draft)
- [`schema/lis-0.1.schema.json`](schema/lis-0.1.schema.json) — JSON Schema (draft 2020-12)
- [`examples/`](examples/) — complete documents, JSON and YAML
- [`docs/prior-art.md`](docs/prior-art.md) — what exists, what LIS learned from each

## Status

**v0.1.0-draft** — with working implementations on both sides of the contract:

- **producer**: the [nox](https://github.com/bresilla/nixos) installer TUI writes
  `host/generated/system.lis.json` at every config generation, and resumes a
  previous session's answers from it on startup.
- **applier**: `nox lis-apply --file system.lis.json` consumes a LIS document and
  generates the NixOS config (disko.nix, host.nix, user.nix) from it — no disk is
  touched; applying the generated config stays a separate, confirmed step.
- **translator**: [`tools/lis2archinstall.py`](tools/lis2archinstall.py) converts
  a LIS document into archinstall's `user_configuration.json` +
  `user_credentials.json` (plain-partition subset; warns on anything it must drop,
  `--strict` makes dropped intent fatal).
- **translator (ubuntu)**: [`tools/lis2autoinstall.py`](tools/lis2autoinstall.py)
  converts a LIS document into an Ubuntu autoinstall **cloud-init NoCloud seed**
  (`user-data` + `meta-data`, ready for a `CIDATA` volume) — including full
  curtin storage config with LVM, hashed passwords in `identity`, and
  `scripts.firstboot` mapped onto cloud-init `runcmd`.
- **rust crate**: [`bindings/rust`](bindings/rust) is the reference
  implementation — a typed model of every spec section with JSON emit/parse and
  the SPEC §19 validation. Depend on it with
  `lis = { git = "https://github.com/onix-os/lis" }`; nox uses it directly.
- **validator**: [`tools/lis-validate`](tools/lis-validate) checks documents
  against the JSON Schema *and* the SPEC §19 semantic rules (reference
  resolution, exactly-one-root, firmware/loader coherence, no plaintext
  secrets, …). CI runs it on every example.

## Non-goals

- Configuration management after first boot (that's cloud-init / Ansible territory).
- Image building (mkosi, Ignition). LIS describes an installation onto hardware.
- Replacing distro package managers or their package names.
