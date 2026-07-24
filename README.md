<p align="center"><img src="docs/assets/logo.svg" width="180" alt="LIS — an oak tree whose roots end in three nodes"></p>

# LIS — Linux Installation Specification

**One file that describes an installed Linux system. Any wizard can write it. Any distro can apply it.**

*Lis* is Albanian for **oak**: one trunk, many roots.

LIS is a declarative, distro-neutral specification of a Linux installation: disks,
filesystems, boot, users, network, and software *intent*. A LIS document is produced
by an installer frontend (a TUI, a GUI, a web form, a script — anything) and consumed
by a distro-specific **applier** that turns it into a real system.

```
┌─────────────┐   ┌─────────────┐   ┌───────────────┐
│   CLI/TUI   │   │   GUI/WEB   │   │ hand-written  │      frontends
└──────┬──────┘   └──────┬──────┘   └───────┬───────┘      (produce LIS)
       └───────────────  ▼  ────────────────┘
                 ╔═══════════════╗
                 ║  system.lis   ║                         the standard
                 ╚═══════╤═══════╝
       ┌───────────────  ▼  ────────────────┐
┌──────┴──────┐   ┌──────┴───────┐   ┌──────┴───────┐
│ NixOS       │   │ Arch         │   │ Debian       │      appliers
│ applier     │   │ applier      │   │ applier      │      (consume LIS)
│ → disko.nix │   │ → pacstrap   │   │ → debootstrap│
│ → config.nix│   │ → archinstall│   │ → preseed    │
└─────────────┘   └──────────────┘   └──────────────┘
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

## Delivery — the LIS seed & `lis.json` boot manifest

A document describes an installation (the recipe); the **seed** is how the installer locates, fetches, and authorizes it. LIS uses a volume labeled `LIS` holding a lightweight boot manifest named `lis.json`:

```
LIS/
├── lis.json           the manifest — HOW to fetch the recipe & keys
├── authorized_keys    the trust    — who may install remotely (level 1)
├── unattended         the consent  — empty marker: no prompts
├── keys/…             local key files (GPG, binary keyfiles)
└── secrets/…          material referenced as { "from": "seed:secrets/…" }
```

The boot manifest (`lis.json`) specifies flexible recipe resolution via `source`:
* **Local or Disk**: `"source": { "type": "file", "path": "/recipes/server.lis.json" }` or disk matching.
* **Network & NAS**: `"source": { "type": "nfs", "server": "nas.local", "export": "/deployments", "path": "node1.lis.json" }` (supports SMB, Git, S3, HTTPS).
* **Remote Hook (`await`)**: `"source": { "type": "await", "protocol": "ssh" }` — turns the live ISO into an SSH server that waits for an operator's remote wizard to push the recipe.
* **Fallback Priority Chains**: Specify an array of sources (e.g. NFS $\rightarrow$ local USB backup $\rightarrow$ SSH await $\rightarrow$ interactive TUI).

**The Two-Key Rule for Consent:** Whether an installation runs hands-free depends on consent:
1. the *document* must say `installer.unattended: true`, **and**
2. the *seed volume* must carry the empty `unattended` marker file (or `lis.unattended=1` on kernel command line).

Missing either key, the installer loads the document as prefilled answers and pauses for human confirmation. Secrets stay on physical media (`seed:secrets/...`) or hardware tokens (`keys`), keeping recipes shareable.

Beyond the stick, installers accept `lis.url=` and `lis.device=` kernel parameters, search in a fixed order (`lis.url=` $\rightarrow$ `lis.device=` $\rightarrow$ `LIS` $\rightarrow$ `LISDATA` $\rightarrow$ `CIDATA`/`OEMDRV` piggyback $\rightarrow$ await/interactive), and fail on ambiguity. After install, the applier records the birth certificate at `/var/lib/lis/system.lis.json` — so every LIS-installed system can answer *"how were you built?"*, and reinstalling it is: take the file, make a seed.

Full normative text: [`spec/delivery.md`](spec/delivery.md).

## Repository layout

- [`spec/schema.md`](spec/schema.md) — the core specification (v0.1.0-draft)
- [`spec/delivery.md`](spec/delivery.md) — the delivery & boot manifest specification
- [`spec/schema.json`](spec/schema.json) — canonical JSON Schema (draft 2020-12)
- [`tools/`](tools/) — implementation code, appliers (`tools/appliers/`), validators (`tools/validators/`), and bindings (`tools/bindings/`)
- [`docs/`](docs/) — research documentation (`docs/prior-art.md`, `docs/coverage-matrix.md`) and examples (`docs/examples/`)

## Status

**v0.1.0-draft** — with working implementations on both sides of the contract:

- **producer**: the [nox](https://github.com/bresilla/nixos) installer TUI writes
  `host/generated/system.lis.json` at every config generation, and resumes a
  previous session's answers from it on startup.
- **applier**: `nox lis-apply --file system.lis.json` consumes a LIS document and
  generates the NixOS config (disko.nix, host.nix, user.nix) from it — no disk is
  touched; applying the generated config stays a separate, confirmed step.
- **translators**: [`tools/appliers/`](tools/appliers/) holds converters to archinstall and
  Ubuntu autoinstall configurations.
- **translator (nixos)**: [`tools/appliers/lis2nixos.py`](tools/appliers/lis2nixos.py) turns a
  document into the classic trio — `disko.nix`, `hardware.nix`,
  `configuration.nix` — using plain NixOS options only. Opinionated flakes
  bring their own translators; the default acts as default.
- **rust crate**: [`tools/bindings/rust`](tools/bindings/rust) is the reference
  implementation — a typed model of every spec section with JSON emit/parse and
  the SPEC validation. Depend on it with
  `lis-spec = { git = "https://github.com/onix-os/lis", path = "tools/bindings/rust" }`; nox uses it directly.
- **validator**: [`tools/validators/lis-validate`](tools/validators/lis-validate) checks documents
  against the JSON Schema *and* semantic rules (reference
  resolution, exactly-one-root, firmware/loader coherence, no plaintext
  secrets, …). CI runs it on every example.

## Non-goals

- Configuration management after first boot (that's cloud-init / Ansible territory).
- Image building (mkosi, Ignition). LIS describes an installation onto hardware.
- Replacing distro package managers or their package names.
