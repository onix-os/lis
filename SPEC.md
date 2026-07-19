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
- Keys beginning with `x-` are **extensions** (§17) and MUST be ignored by appliers
  that do not recognize them.

### 2.3 The no-silent-drift rule

An applier MUST refuse to apply a document containing core intent it cannot honor
(e.g. `"init": "openrc"` on a distro that only ships systemd, `"fs": "zfs"` without
ZFS support). It MUST NOT substitute silently. A field explicitly marked
`"preference": true` by the producer is exempt: the applier MAY substitute and MUST
report the substitution.

### 2.4 Secrets

Passwords MUST be crypt(3) hashes; WPA keys MUST be PSK hashes; registration
tokens and other secret material MUST be **references** (`{ "from": "file:…" }` or
`{ "from": "env:…" }`), resolved by the applier at apply time. A document MUST
never contain plaintext secrets — documents are meant to be committed, shared, and
templated.

## 3. Top-level structure

```json
{
  "lis": "0.1.0",
  "meta":         { },
  "target":       { },
  "storage":      { },
  "boot":         { },
  "system":       { },
  "users":        [ ],
  "network":      { },
  "software":     { },
  "desktop":      { },
  "drivers":      { },
  "proxy":        { },
  "mirror":       { },
  "registration": { },
  "files":        [ ],
  "scripts":      { },
  "installer":    { },
  "x-nixos":      { }
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

> SAN / enterprise device activation (iSCSI, DASD, zFCP, multipath) is a planned
> `target` extension — see `docs/coverage-matrix.md`.

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
| `fs` | string | `ext4` \| `btrfs` \| `xfs` \| `f2fs` \| `zfs` \| `vfat` \| `swap` \| `none` |
| `label` | string | filesystem label |
| `mountpoint` | string | absolute path; implied by `role` when omitted (`esp`→`/boot/efi` or `/boot` per applier convention, `root`→`/`) |
| `mount_options` | string[] | passed through |
| `subvolumes` | object[] | btrfs only: `{ name, mountpoint, mount_options? }` |
| `existing` | object | adopt an existing partition instead of creating (§6.2) |

- Roles imply sensible types/flags (`esp` → EF00 + vfat). Appliers MUST create GPT
  unless `firmware: bios` demands otherwise.
- `wipe: true` (default `false`) permits destroying existing data on owned disks.
  With `wipe: false`, an applier MUST fail if any owned disk holds data not
  accounted for by `existing` adoptions.
- Exactly one filesystem in the document must resolve to mountpoint `/` (directly,
  via subvolume, or via an aggregate volume); otherwise the document is invalid.
- `fs: zfs` is single-filesystem use only in v0.1; pool/dataset modeling is a
  planned extension.

### 6.2 Adopting existing partitions (dual boot, reuse ESP)

The most common non-wipe installs keep something: an existing EFI System
Partition, or a Windows partition that must shrink. A partition entry with
`existing` adopts instead of creating:

```json
{ "disk": "main", "role": "esp",
  "existing": { "match": { "partition": 1 }, "format": false } },
{ "disk": "main", "id": "p-win",
  "existing": { "match": { "fs": "ntfs" }, "format": false,
                "resize": "200GiB" } },
{ "disk": "main", "role": "root", "size": "rest", "fs": "btrfs" }
```

- `existing.match`: `partition` (number), `label`, `uuid`, or `fs` — MUST match
  exactly one partition on that disk.
- `format` (default `false`): re-mkfs the adopted partition.
- `resize`: shrink/grow the adopted partition to the given size before other
  partitions are created. Appliers that cannot resize the filesystem in question
  MUST fail (§2.3).
- Adopted partitions are exempt from `wipe`.

### 6.3 `encryption[]` — LUKS containers

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

### 6.4 `aggregates` — LVM and RAID

```json
{
  "lvm": [ { "name": "pool", "devices": ["p-sda2", "p-sdb1"],
             "volumes": [ { "name": "root", "size": "100GiB", "fs": "ext4", "mountpoint": "/" },
                          { "name": "data", "size": "rest",   "fs": "xfs",  "mountpoint": "/data" } ] } ],
  "raid": [ { "name": "md0", "level": 1, "devices": ["p-sda3", "p-sdb3"],
              "spares": ["p-sdc3"] } ]
}
```

`devices` reference partition ids (or encryption container ids). LVM volumes accept
the same `fs`/`mountpoint`/`subvolumes` fields as partitions. RAID devices are
referenced by `"md:<name>"` from LVM or filesystems. `spares` lists hot-spare
partition ids.

### 6.5 `swap`

Swap is either a partition/volume with `fs: swap`, or:

```json
{ "swap": { "zram": { "size": "50%" } } }
{ "swap": { "file": { "path": "/swap/swapfile", "size": "8GiB" } } }
```

### 6.6 `snapshots` — root filesystem snapshots

The openSUSE/Manjaro pattern: automatic btrfs snapshots with rollback from the
boot menu.

```json
{ "snapshots": { "enabled": true, "tool": "auto", "boot_menu": true } }
```

- `tool`: `auto` | `snapper` | `timeshift`.
- `boot_menu: true` requests bootable snapshots (e.g. grub-btrfs); combined with
  `boot.loader` constraints per §2.3.
- Requires a btrfs root; otherwise the document is invalid.

## 7. `boot`

```json
{
  "loader": "systemd-boot",
  "timeout": 3,
  "os_prober": true,
  "password_hash": "grub.pbkdf2.sha512.…",
  "console": { "serial": "ttyS0,115200n8" },
  "secure_boot": "auto",
  "uki": false,
  "kernel": { "variant": "default", "params": ["quiet"], "modules": ["kvm-intel"],
              "blacklist": ["nouveau"] },
  "initramfs": { "generator": "auto", "include_modules": ["nvme"] }
}
```

- `loader`: `systemd-boot` | `grub` | `auto` (default). `systemd-boot` requires
  `firmware: uefi` — mismatch is a validation error.
- `os_prober` (default `true` when adopted foreign partitions exist): include
  other detected operating systems in the boot menu.
- `password_hash`: bootloader edit protection (GRUB pbkdf2 hash). Appliers whose
  loader has no such concept MUST fail unless marked preference (§2.3).
- `console.serial`: enable a serial console (adds the console= parameters and
  getty).
- `secure_boot`: `auto` | `true` | `false` — `true` obliges the applier to produce
  a Secure-Boot-bootable system (shim or signed UKI per distro convention).
- `uki: true` requests a Unified Kernel Image instead of a loader-managed
  kernel+initrd pair.
- `kernel.variant` is intent: `default` | `lts` | `hardened` | `realtime` | `zen`.
  Appliers map to their nearest kernel package and MUST fail (not substitute) if
  nothing reasonable exists, unless marked as preference (§2.3).
- `initramfs.generator`: `auto` | `dracut` | `mkinitcpio` | `booster` (subject to
  §2.3); `include_modules` are always embedded.

## 8. `system`

```json
{
  "hostname": "tron",
  "domain": "lan",
  "timezone": "Europe/Amsterdam",
  "hwclock": "utc",
  "time": { "ntp": true, "servers": ["pool.ntp.org"], "provider": "auto" },
  "locale": "en_US.UTF-8",
  "extra_locales": ["nl_NL.UTF-8"],
  "locale_overrides": { "LC_TIME": "nl_NL.UTF-8" },
  "keymap": { "console": "us", "font": "ter-v16n", "layout": "us", "variant": "" },
  "init": "systemd",
  "security": { "module": "auto" },
  "kdump": false,
  "telemetry": "off"
}
```

- `hwclock`: `utc` (default) | `localtime` — the Windows-dual-boot switch.
- `time.ntp` enables time synchronization; `provider` is intent:
  `auto` | `chrony` | `systemd-timesyncd` | `openntpd` (subject to §2.3).
- `locale_overrides` sets individual `LC_*` categories.
- `init`: `systemd` | `openrc` | `runit` | `s6` | `auto`. Subject to §2.3.
- `security.module`: `auto` | `selinux` | `apparmor` | `none` — intent for the MAC
  framework; `auto` means distro default.
- `kdump: true` sets up kernel crash dumps (kdump/kdump-tools).
- `telemetry`: `off` | `default` — `off` obliges the applier to disable/opt out of
  any installed telemetry or popularity-contest mechanisms.

## 9. `users[]`

```json
[
  { "name": "root", "password": { "locked": true },
    "ssh_authorized_keys": ["ssh-ed25519 AAAA…"] },
  { "name": "bresilla", "uid": 1000, "admin": true, "sudo": "default",
    "shell": "zsh",
    "groups": ["video", "audio"],
    "password": { "hash": "$6$rounds…" },
    "ssh_authorized_keys": [],
    "dotfiles": { "repo": "https://github.com/bresilla/dot.git" } }
]
```

- `name: "root"` configures the root account; all others are created.
- `admin: true` is intent for "can administer the machine" — the applier maps it to
  `wheel`/`sudo`/`sudoers` as appropriate. `sudo`: `default` (password) |
  `nopasswd`. Raw `groups` are passed through; the applier MUST create groups that
  do not exist.
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
  "hosts": [ { "ip": "10.0.0.9", "names": ["nas", "nas.lan"] } ],
  "firewall": { "enabled": true, "allow_services": ["ssh"], "allow_ports": ["8080/tcp"] },
  "ssh": { "enabled": true, "password_auth": false, "permit_root": "prohibit-password" }
}
```

- `manager`: `auto` | `networkmanager` | `systemd-networkd` | `iwd`. Subject to §2.3.
- Omitting `interfaces` means "DHCP on everything wired" — the universal default.
- `wifi[].psk_hash` is the WPA-PSK hex hash (as produced by `wpa_passphrase`), not
  the passphrase.
- `hosts[]` appends static `/etc/hosts` entries.

> Bonds, bridges, and VLANs are planned for a minor revision — see
> `docs/coverage-matrix.md`.

## 11. `software`

The hardest section to standardize; LIS keeps the portable core small and pushes
the rest to extensions.

```json
{
  "role": "desktop:gnome",
  "packages": ["firefox", "git", "htop"],
  "exclude": ["nano"],
  "services": { "enable": ["sshd", "tailscaled"], "disable": ["bluetooth"] },
  "flatpak": ["org.mozilla.firefox"],
  "snap": [ { "name": "chromium", "channel": "stable", "classic": false } ]
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
- `exclude[]` removes packages a role would otherwise pull in (the Kickstart
  `%packages` minus-list). Excluding something no role provides is not an error.
- `services` uses systemd unit names (without `.service`) as the lingua franca;
  non-systemd appliers translate.
- `flatpak[]` (app IDs) and `snap[]` are portable across distros that ship those
  runtimes; subject to §2.3 where they don't.
- Additional package **repositories** are inherently distro-specific and belong in
  `x-*` extensions.

## 12. `desktop`

Desktop-session plumbing that every graphical installer asks about and no
server needs. Only meaningful when `software.role` is a `desktop:*` role;
otherwise the section MUST be absent.

```json
{
  "display_manager": "auto",
  "autologin": "bresilla",
  "audio": "pipewire",
  "bluetooth": true,
  "printing": true
}
```

- `display_manager`: `auto` | `gdm` | `sddm` | `lightdm` | `greetd` | `ly` | `none`.
- `autologin`: a `users[].name` to log in automatically (the Calamares/archinstall
  option). Combining `autologin` with full-disk encryption is allowed; combining
  it with a locked user is a validation error.
- `audio`: `auto` | `pipewire` | `pulseaudio` | `none`.
- `bluetooth` / `printing`: enable the standard stacks (bluez, CUPS).

## 13. `drivers`, `files`, `scripts`

```json
{ "drivers": { "gpu": "auto", "microcode": "auto", "firmware": "auto" } }
```
`gpu`: `auto` | `nvidia` | `nvidia-open` | `amdgpu` | `intel` | `none`.
`microcode`: `auto` | `intel` | `amd` | `none`.
`firmware`: `auto` | `all` | `none` — linux-firmware installation intent.

```json
{ "files": [ { "path": "/etc/motd", "mode": "0644", "owner": "root:root",
               "content": "welcome\n" } ] }
```
Small literal files only (the Ignition primitive). Binary content uses
`"encoding": "base64"`.

```json
{ "scripts": {
    "pre":  [ { "interpreter": "/bin/sh", "content": "echo before partitioning" } ],
    "post": [ { "chroot": true, "interpreter": "/bin/sh", "content": "echo inside target" } ],
    "firstboot": [ { "interpreter": "/bin/sh", "content": "echo once, on first boot" } ] } }
```
The escape hatch. Documents SHOULD prefer declarative sections. `pre` runs before
disks are touched, `post` after all declarative work (in the target chroot when
`chroot: true`), `firstboot` exactly once on the installed system's first boot.

## 14. `proxy` and `mirror`

```json
{ "proxy": { "http": "http://proxy:8080", "https": "http://proxy:8080",
             "no_proxy": ["localhost", ".lan"] } }
```
Applies to the installation *and* is persisted into the installed system's
package-manager/environment configuration.

```json
{ "mirror": { "country": "NL" } }
{ "mirror": { "url": "https://mirror.example.org" } }
```
Package-mirror intent: `country` asks the applier to pick nearby mirrors; `url`
pins one explicitly (applier-interpreted for its own repo layout). Distro-specific
repo lists belong in `x-*`.

## 15. `registration`

Vendor registration/subscription — the enterprise blocker (RHEL subscription,
SUSE SCC, Ubuntu Pro). Token material is a secret **reference** (§2.4).

```json
{ "registration": {
    "server": "default",
    "token": { "from": "file:/run/install-secrets/token" },
    "email": "ops@example.org" } }
```

Appliers for unregistered distros MUST fail on this section (§2.3) — silently
skipping a subscription is drift.

## 16. `installer` — apply-run behavior

How the *run* behaves, not what the system is. (Kickstart `reboot`, autoinstall
`interactive-sections`, Agama questions.)

```json
{
  "on_finish": "reboot",
  "on_error": "fail",
  "interactive": ["storage"],
  "answers": { "overwrite_disk": "yes" }
}
```

- `on_finish`: `reboot` | `poweroff` | `stay` (default `stay`).
- `on_error`: `fail` (default) | `prompt`.
- `interactive[]`: section names the frontend MAY re-ask interactively even though
  the document provides values; everything else is unattended.
- `answers`: predefined answers to applier-defined questions, by question id.

## 17. `x-*` extensions

Any top-level key starting with `x-` is an extension namespace owned by the named
project, passed verbatim to appliers that recognize it and ignored by all others:

```json
{
  "x-nixos": { "flake": true, "repo": "https://github.com/bresilla/nixos", "secrets": false },
  "x-arch":  { "aur_helper": "paru", "aur_packages": ["nox-git"] }
}
```

Extensions MUST NOT change the meaning of core sections — they add, never override.

## 18. Applier report

An applier SHOULD emit a machine-readable report after applying:

```json
{ "lis": "0.1.0", "applied": true, "distro": "nixos",
  "substitutions": [ { "path": "/boot/loader", "wanted": "auto", "chose": "systemd-boot" } ],
  "warnings": [], "log": "…" }
```

## 19. Validation summary (normative checklist)

A document is invalid if any of these fail:

1. `lis` version present and supported.
2. Validates against the JSON Schema for that version.
3. All disk/partition/volume references resolve; no dangling `id`s.
4. At most one `size: "rest"` per disk; sizes fit the matched device when sizes are
   absolute and the device is known.
5. Exactly one mountpoint `/` resolves.
6. `firmware`/`loader` combination is coherent.
7. No plaintext secrets anywhere (§2.4).
8. `wipe: false` with unaccounted data on owned disks → applier MUST refuse at
   apply time; `existing` matches MUST resolve to exactly one partition.
9. `storage.snapshots.enabled` requires a btrfs root.
10. `desktop` requires a `desktop:*` role; `desktop.autologin` must name an
    unlocked user.
