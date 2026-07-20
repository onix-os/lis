<p align="center"><img src="assets/logo.svg" width="180" alt="LIS — an oak tree whose roots end in three nodes"></p>

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

## Delivery — the LIS seed

A document describes an installation; the **seed** is how the file physically
reaches the machine. Anaconda has `OEMDRV` (plug in a labeled USB stick, the
installer finds the kickstart), cloud-init has `CIDATA`. LIS has `LISDATA` —
a volume with that label, holding a handful of well-known files:

```
LISDATA/
├── system.lis.json    the intent   — what to install          (level 2)
├── authorized_keys    the trust    — who may install remotely (level 1)
├── unattended         the consent  — empty marker: no prompts
└── secrets/…          material referenced as  { "from": "seed:secrets/…" }
```

The two levels are the interesting part:

**Level 1 — trust.** A seed with only `authorized_keys` turns any booted
installer into a machine that *waits*: it brings up the network, starts sshd
with those keys authorized, announces itself on the LAN via mDNS
(`_lis-installer._tcp`), and does nothing else. An operator's frontend — a
TUI on a laptop — discovers the waiting machine, connects, probes its disks
and hardware, and the human drives the install interactively *over the
wire*, producing the LIS document live. One generic stick provisions any
number of machines forever, because the per-machine decisions happen at the
operator's side, not on the stick. Boot three machines from the same USB
key, see all three appear in your frontend, install each one differently.

**Level 2 — intent.** A seed carrying `system.lis.json` is a concrete
install order. Whether it runs hands-free depends on consent — which is
deliberately a **two-key rule**:

1. the *document* must say `installer.unattended: true`, **and**
2. the *seed* must carry the empty `unattended` marker file
   (for network delivery: `lis.unattended=1` on the kernel command line).

Documents get copied, committed, and shared — so a document alone can never
authorize wiping a machine it was never meant for. The second key lives on
the physical object someone deliberately prepared and plugged into *this*
machine. Missing either key, the installer loads the document as prefilled
answers and stops for a human. (Compare OEMDRV, where a discovered kickstart
with wipe instructions simply executes.)

Both levels compose: a seed with the document *and* keys applies the
document while keeping SSH open as the operator's escape hatch to watch or
abort. Secrets never enter the document — `seed:` references resolve against
the stick, so `system.lis.json` stays shareable while tokens stay physical.

Beyond the stick, installers also accept `lis.url=` and `lis.device=` kernel
parameters (PXE fleets), search in a fixed order (`lis.url=` → `lis.device=`
→ `LISDATA` → `system.lis.json` piggybacked on a `CIDATA` or `OEMDRV`
volume → await/interactive), and must fail on ambiguity rather than guess.
After a successful install, the applier records the document on the target
at `/var/lib/lis/system.lis.json` — the machine's **birth certificate**:
every LIS-installed system can answer *"how were you built?"*, and
reinstalling it is: take the file, make a seed.

Full normative text: [`docs/delivery.md`](docs/delivery.md).

## Repository layout

- [`SPEC.md`](SPEC.md) — the specification (v0.1.0-draft)
- [`schema/lis-0.1.schema.json`](schema/lis-0.1.schema.json) — JSON Schema (draft 2020-12)
- [`docs/delivery.md`](docs/delivery.md) — the seed convention (delivery, discovery, consent)
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
- **translators**: [`tools/`](tools/) holds converters to archinstall and
  Ubuntu autoinstall configurations.
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
