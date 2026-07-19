# LIS — Linux Installation Specification

**Version 0.1.0-draft**

## 1. Conformance language

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are to be interpreted as
described in RFC 2119.

Two roles are defined:

- **Producer** — software that writes a LIS document (an installer frontend, a
  generator, a text editor).
- **Applier** — software that consumes a LIS document and produces an installed
  system on a specific distribution.

## 2. Document format

- The canonical encoding is **JSON** (UTF-8). A document is a single JSON object.
- **YAML** and **TOML** are accepted authoring encodings. Their data model MUST map
  1:1 onto the canonical JSON (no anchors/aliases semantics, no non-string keys).
  Appliers MAY accept only JSON; converters are trivial and producers SHOULD offer
  JSON output.
- The recommended file extension is `.lis.json` / `.lis.yaml` / `.lis.toml`.
- A document MUST validate against the published JSON Schema for its version.

### 2.1 Versioning

The top-level `lis` key is REQUIRED and holds a semantic version of the spec the
document targets:

```json
{ "lis": "0.1.0" }
```

- Appliers MUST declare the versions they accept and MUST reject documents with a
  major version they do not support.
- Within a major version, unknown *optional* fields introduced by later minor
  versions MUST be ignored with a warning.

### 2.2 Unknown fields and strictness

- Unknown fields in **core** sections are an error.
- Keys beginning with `x-` are **extensions** (§13) and MUST be ignored by appliers
  that do not recognize them.

### 2.3 The no-silent-drift rule

An applier MUST refuse to apply a document containing core intent it cannot honor
(e.g. `"init": "openrc"` on a distro that only ships systemd, `"fs": "zfs"` without
ZFS support). It MUST NOT substitute silently. A field explicitly marked
`"preference": true` by the producer is exempt: the applier MAY substitute and MUST
report the substitution.

## 3. Top-level structure

```json
{
  "lis": "0.1.0",
  "meta":     { },
  "target":   { },
  "storage":  { },
  "boot":     { },
  "system":   { },
  "users":    [ ],
  "network":  { },
  "software": { },
  "drivers":  { },
  "files":    [ ],
  "scripts":  { },
  "x-nixos":  { }
}
```

Only `lis` is required. Every omitted section means "applier default". An empty
document is a valid (if useless) LIS file; appliers supply their distro's defaults
for anything unstated — the Ubuntu-autoinstall model, not the preseed model.

## 4. `meta` — provenance

Informational only; appliers MUST NOT change behavior based on it.

| field | type | meaning |
|---|---|---|
| `name` | string | human name for this profile |
| `description` | string | free text |
| `generator` | string | producing software, e.g. `"nox 0.1.0"` |
| `created` | string (RFC 3339) | when the document was written |

## 5. `target` — where to install

```json
{
  "arch": "x86_64",
  "firmware": "uefi",
  "disks": [
    { "id": "main",  "match": { "path": "/dev/sda" } },
    { "id": "bulk",  "match": { "serial": "WD-XYZ123" } },
    { "id": "any",   "match": { "min_size": "500GiB", "type": "ssd" } }
  ]
}
```

- `arch`: `x86_64` | `aarch64` | `riscv64` — MUST match the target machine.
- `firmware`: `uefi` | `bios` | `auto` (default `auto`).
- `disks[]`: declares **disk handles**. Every disk referenced elsewhere MUST be
  declared here. `id` is a document-local handle; `match` selects hardware by
  the first matching rule, with fields:
  `path`, `serial`, `model`, `wwn`, `min_size`, `max_size`,
  `type` (`ssd`|`hdd`|`nvme`), `smallest: true`, `largest: true`.
- Matching MUST be deterministic: if a `match` selects more than one device and
  neither `smallest` nor `largest` disambiguates, the applier MUST fail.
- A matched disk is **owned** by the document: appliers MUST NOT touch unmatched
  disks.

## 6. `storage` — disks, partitions, volumes

The storage model has four layers, each optional: **partitions** on disks,
**encryption** containers, **aggregates** (LVM/RAID), and **filesystems** (with
btrfs subvolumes). Simple installs use only `partitions`.

```json
{
  "wipe": true,
  "partitions": [
    { "disk": "main", "role": "esp",  "size": "1GiB" },
    { "disk": "main", "role": "swap", "size": "8GiB" },
    { "disk": "main", "id": "p-root", "role": "root", "size": "rest", "fs": "btrfs",
      "mount_options": ["compress=zstd"],
      "subvolumes": [
        { "name": "@",     "mountpoint": "/" },
        { "name": "@home", "mountpoint": "/home" },
        { "name": "@nix",  "mountpoint": "/nix" }
      ] }
  ]
}
```

### 6.1 `partitions[]`

| field | type | meaning |
|---|---|---|
| `disk` | string | REQUIRED — a `target.disks[].id` handle |
| `id` | string | document-local handle for aggregates/encryption to reference |
| `role` | string | `esp` \| `boot` \| `root` \| `swap` \| `data` \| `raw` |
| `size` | string | `"<n>MiB"` / `"<n>GiB"` / `"<n>%"` / `"rest"` (max one `rest` per disk) |
| `fs` | string | `ext4` \| `btrfs` \| `xfs` \| `f2fs` \| `vfat` \| `swap` \| `none` |
| `label` | string | filesystem label |
| `mountpoint` | string | absolute path; implied by `role` when omitted (`esp`→`/boot/efi` or `/boot` per applier convention, `root`→`/`) |
| `mount_options` | string[] | passed through |
| `subvolumes` | object[] | btrfs only: `{ name, mountpoint, mount_options? }` |

- Roles imply sensible types/flags (`esp` → EF00 + vfat). Appliers MUST create GPT
  unless `firmware: bios` demands otherwise.
- `wipe: true` (default `false`) permits destroying existing data on owned disks.
  With `wipe: false`, an applier MUST fail if any owned disk is not empty.
- Exactly one filesystem in the document must resolve to mountpoint `/` (directly,
  via subvolume, or via an aggregate volume); otherwise the document is invalid.

### 6.2 `encryption[]` — LUKS containers

```json
{ "id": "crypt-root", "over": "p-root", "type": "luks2",
  "key": { "passphrase": true },
  "unlock": ["tpm2", "passphrase"] }
```

`over` references a partition or aggregate volume `id`. `key` declares how the
initial key is provided (`passphrase: true` = ask interactively at install;
`keyfile: "<path-on-installer>"`), `unlock` the boot-time unlock order
(`passphrase` | `keyfile` | `tpm2` | `fido2`). Filesystems/aggregates reference the
container's `id` instead of the raw partition.

### 6.3 `aggregates` — LVM and RAID

```json
{
  "lvm": [ { "name": "pool", "devices": ["p-sda2", "p-sdb1"],
             "volumes": [ { "name": "root", "size": "100GiB", "fs": "ext4", "mountpoint": "/" },
                          { "name": "data", "size": "rest",   "fs": "xfs",  "mountpoint": "/data" } ] } ],
  "raid": [ { "name": "md0", "level": 1, "devices": ["p-sda3", "p-sdb3"] } ]
}
```

`devices` reference partition ids (or encryption container ids). LVM volumes accept
the same `fs`/`mountpoint`/`subvolumes` fields as partitions. RAID devices are
referenced by `"md:<name>"` from LVM or filesystems.

### 6.4 `swap`

Swap is either a partition/volume with `fs: swap`, or:

```json
{ "swap": { "zram": { "size": "50%" } } }
{ "swap": { "file": { "path": "/swap/swapfile", "size": "8GiB" } } }
```

## 7. `boot`

```json
{
  "loader": "systemd-boot",
  "timeout": 3,
  "kernel": { "variant": "default", "params": ["quiet"], "modules": ["kvm-intel"],
              "blacklist": ["nouveau"] }
}
```

- `loader`: `systemd-boot` | `grub` | `auto` (default). `systemd-boot` requires
  `firmware: uefi` — mismatch is a validation error.
- `kernel.variant` is intent: `default` | `lts` | `hardened` | `realtime` | `zen`.
  Appliers map to their nearest kernel package and MUST fail (not substitute) if
  nothing reasonable exists, unless marked as preference (§2.3).

## 8. `system`

```json
{
  "hostname": "tron",
  "timezone": "Europe/Amsterdam",
  "locale": "en_US.UTF-8",
  "extra_locales": ["nl_NL.UTF-8"],
  "keymap": { "console": "us", "layout": "us", "variant": "" },
  "init": "systemd",
  "security": { "module": "auto" }
}
```

- `init`: `systemd` | `openrc` | `runit` | `s6` | `auto`. Subject to §2.3.
- `security.module`: `auto` | `selinux` | `apparmor` | `none` — intent for the MAC
  framework; `auto` means distro default.

## 9. `users[]`

```json
[
  { "name": "root", "password": { "locked": true },
    "ssh_authorized_keys": ["ssh-ed25519 AAAA…"] },
  { "name": "bresilla", "uid": 1000, "admin": true,
    "shell": "zsh",
    "groups": ["video", "audio"],
    "password": { "hash": "$6$rounds…" },
    "ssh_authorized_keys": [],
    "dotfiles": { "repo": "https://github.com/bresilla/dot.git" } }
]
```

- `name: "root"` configures the root account; all others are created.
- `admin: true` is intent for "can administer the machine" — the applier maps it to
  `wheel`/`sudo`/`sudoers` as appropriate. Raw `groups` are passed through and MUST
  exist or be creatable on the target.
- `password.hash` MUST be a crypt(3) string (`$6$` sha512-crypt or `$y$` yescrypt).
  Producers MUST NOT emit plaintext passwords. `password.locked: true` disables
  password login. Omitting `password` leaves the account passwordless-locked.
- `shell` is either an intent name (`bash`, `zsh`, `fish`) or an absolute path.
  Intent names oblige the applier to install the shell.
- `dotfiles` is intent: clone the repo into the user's home by the applier's
  mechanism. `method` MAY name a convention (`raw` | `stow` | `chezmoi`).

## 10. `network`

```json
{
  "manager": "auto",
  "interfaces": [
    { "match": { "name": "en*" }, "dhcp4": true },
    { "match": { "mac": "aa:bb:cc:dd:ee:ff" },
      "addresses": ["10.0.0.5/24"], "gateway": "10.0.0.1",
      "dns": ["10.0.0.1", "9.9.9.9"] }
  ],
  "wifi": [ { "ssid": "home", "psk_hash": "…", "hidden": false } ],
  "firewall": { "enabled": true, "allow_services": ["ssh"], "allow_ports": ["8080/tcp"] },
  "ssh": { "enabled": true, "password_auth": false, "permit_root": "prohibit-password" }
}
```

- `manager`: `auto` | `networkmanager` | `systemd-networkd` | `iwd`. Subject to §2.3.
- Omitting `interfaces` means "DHCP on everything wired" — the universal default.
- `wifi[].psk_hash` is the WPA-PSK hex hash (as produced by `wpa_passphrase`), not
  the passphrase.

## 11. `software`

The hardest section to standardize; LIS keeps the portable core small and pushes
the rest to extensions.

```json
{
  "role": "desktop:gnome",
  "packages": ["firefox", "git", "htop"],
  "services": { "enable": ["sshd", "tailscaled"], "disable": ["bluetooth"] },
  "flatpak": ["org.mozilla.firefox"]
}
```

- `role` is the coarse intent, from a small registry this spec owns:
  `minimal`, `server`, `desktop:gnome`, `desktop:kde`, `desktop:hyprland`,
  `desktop:sway`, `desktop:xfce`. Appliers map a role to their curated set and MUST
  fail on roles they cannot provide.
- `packages[]` are **common-intent names**: the spec maintains a small alias
  registry (`docs/package-registry.md`) mapping intent names to per-distro package
  names. Names outside the registry are passed through verbatim; an applier that
  cannot resolve a name MUST fail (not skip). Distro-specific lists belong in
  extensions (`x-arch.packages`, `x-debian.packages`).
- `services` uses systemd unit names (without `.service`) as the lingua franca;
  non-systemd appliers translate.
- `flatpak[]` (app IDs) is portable across distros that ship flatpak.

## 12. `drivers`, `files`, `scripts`

```json
{ "drivers": { "gpu": "auto", "microcode": "auto" } }
```
`gpu`: `auto` | `nvidia` | `nvidia-open` | `amdgpu` | `intel` | `none`.

```json
{ "files": [ { "path": "/etc/motd", "mode": "0644", "owner": "root:root",
               "content": "welcome\n" } ] }
```
Small literal files only (the Ignition primitive). Binary content uses
`"encoding": "base64"`.

```json
{ "scripts": {
    "pre":  [ { "interpreter": "/bin/sh", "content": "echo before partitioning" } ],
    "post": [ { "chroot": true, "interpreter": "/bin/sh", "content": "echo inside target" } ] } }
```
The escape hatch. Documents SHOULD prefer declarative sections; appliers MUST run
`post` scripts after all declarative work, `pre` before touching disks.

## 13. `x-*` extensions

Any top-level key starting with `x-` is an extension namespace owned by the named
project, passed verbatim to appliers that recognize it and ignored by all others:

```json
{
  "x-nixos": { "flake": true, "repo": "https://github.com/bresilla/nixos", "secrets": false },
  "x-arch":  { "aur_helper": "paru", "aur_packages": ["nox-git"] }
}
```

Extensions MUST NOT change the meaning of core sections — they add, never override.

## 14. Applier report

An applier SHOULD emit a machine-readable report after applying:

```json
{ "lis": "0.1.0", "applied": true, "distro": "nixos",
  "substitutions": [ { "path": "/boot/loader", "wanted": "auto", "chose": "systemd-boot" } ],
  "warnings": [], "log": "…" }
```

## 15. Validation summary (normative checklist)

A document is invalid if any of these fail:

1. `lis` version present and supported.
2. Validates against the JSON Schema for that version.
3. All disk/partition/volume references resolve; no dangling `id`s.
4. At most one `size: "rest"` per disk; sizes fit the matched device when sizes are
   absolute and the device is known.
5. Exactly one mountpoint `/` resolves.
6. `firmware`/`loader` combination is coherent.
7. No plaintext secrets anywhere.
8. `wipe: false` with non-empty owned disks → applier MUST refuse at apply time.
