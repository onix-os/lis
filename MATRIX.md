# LIS v0.1 — Capability Matrix

**What this is.** An exhaustive, per-field record of what each of the nine LIS appliers actually
does with every leaf a LIS v0.1 document can contain. All 233 schema leaves
(`spec/schema.json`, `$ref`s resolved) × 9 appliers.

**Who it is for.** Document generators and wizards. Before generating a document for a chosen
distro, consult this file to tell the user whether the feature they asked for will work, will
work differently, or will be accepted and thrown away.

**The single most important thing on this page:** `❌ DROPS` does **not** mean "the applier
rejects it". It means the applier accepts the field, validates it, and produces nothing. The
document is honoured *except* for that field, and the resulting machine is not what the document
described. Most drops print a `warning:` line, but warnings never fail a run — not even under the
default `--strict`, where `enforce` counts only refusals (`lis_common.py:1591`). A generator that
emits a `❌` field is producing a document that lies. **Warn or refuse instead of emitting it.**

Sources: `tools/appliers/lis2autoinstall.py` (Ubuntu), `lis2debian.py`, `lis2kickstart.py`
(Fedora), `lis2agama.py` (openSUSE), `lis2archinstall.py`, `lis2nixos.py`, `lis2alpine.py`,
`lis2void.py`, `lis2gentoo.py`, and the shared `lis_common.py`. Prior evidence:
`docs/AUDIT-2026-08-02.md`, `docs/DESIGN-gentoo-void.md`.

**Reading the two newest columns.** Void and Gentoo were added after the other seven and are
classified from generated output, not from a completed install, so the ✅/⚙ line is drawn
mechanically and stated here rather than left to inference. For **Void**, ✅ means a VAI
answer-file variable or one of the four VAI step functions the applier replaces, and ⚙ means
`lis-post.sh` — the script `end_function` chroots into at VAI step 16. For **Gentoo**, ✅ means a
generated `/etc/portage` artifact, the profile, `dracut-lis.conf`, or a real configuration file in
the target that a Gentoo tool then consumes, and ⚙ means an imperative command in `lis-prepare.sh`
or `lis-chroot.sh` with no such file behind it. Where a cell comes from a helper in
`lis_common.py` that other columns already mark ⚙ (`file_commands`, `sudoers_commands`,
`uid_commands`, `chroot_intents`, `boot_timeout_commands`, the script-hook plumbing), the same ⚙
is used, so the nine columns stay comparable. See §6.

---

## Legend

| Marker | Status | Meaning for a generator |
|---|---|---|
| ✅ | **YES** | Translated into the installer's own native mechanism. Emit freely. |
| ◐ | **PARTIAL** | Some values, some modes, or some of the semantics reach the target. Emit only after checking the footnote — the unsupported half is usually a drop or a refusal. |
| ⚙ | **POST** | Not native. Emulated by a post-install / chroot / firstboot script. The *effect* arrives, but later than the document implies, and it can fail after the disks are already written. |
| ⛔ | **REFUSE** | The applier exits with an error and produces nothing. Loud and safe. `--lenient` downgrades every refusal to a warning and writes the output anyway. |
| ❌ | **DROPS** | Accepted, never emitted. Usually warned, sometimes silent. **This is the dangerous one.** |
| – | **N/A** | Nothing to translate — informational, or the concept does not exist on that distro. |
| ? | **UNKNOWN** | Output is produced but the classifier could not confirm the installer accepts it. Not resolved by guessing. |

Two shorthand terms used in footnotes:

- **SILENT** — no warning, no refusal, no diagnostic of any kind. A generator is the only thing
  standing between the user and this class of bug.
- **`--apply` only** — the field is evaluated when the applier runs against real hardware, but a
  translate-only run (generating a profile to ship elsewhere) ignores or refuses it.

---

# 2. THE MATRIX

## 2.1 root

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `lis` | ✅¹ | ✅¹ | ✅¹ | ✅¹ | ✅¹ | ✅¹ | ✅¹ | ✅¹ | ✅¹ |

¹ Version gate only (`lis_common.py:1586-1588`). Anything not matching `0.1.x` exits before any
output. It changes nothing about the target.

## 2.2 meta

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `meta.name` | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ |
| `meta.description` | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ |
| `meta.generator` | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ |
| `meta.created` | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ | –¹ |

¹ `NON_INTENT_SECTIONS` (`lis_common.py:156`). Spec §4 forbids appliers acting on these. They are
copied verbatim into the birth certificate at `/var/lib/lis/system.lis.json` on Fedora, SUSE,
Arch, NixOS, Alpine, Void (`lis2void.py:657-661`) and Gentoo (`lis2gentoo.py:924-928`); that is
storage, not behaviour. Correct by design.

## 2.3 keys

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `keys[].id` | ❌¹ | ❌² | ❌² | ❌² | ❌² | ◐¹² | ❌² | ⛔⁹ | ❌¹¹ |
| `keys[].type` | ◐³ | ◐³ | ◐³ | ◐³ | ◐³ | ◐¹³ | ◐³ | ⛔⁹ | ❌¹⁰ |
| `keys[].purpose[]` | ◐⁴ | ◐⁴ | ◐⁴ | ◐⁴ | ◐⁴ | ◐¹⁴ | ◐⁴ | ⛔⁹ | ❌¹⁰ |
| `keys[].match` | ❌⁵ | ❌⁵ | ❌⁵ | ❌⁵ | ❌⁵ | ⛔¹⁵ | ❌⁵ | ⛔⁹ | ❌¹¹ |
| `keys[].source.from` | ◐⁶ | ◐⁶ | ◐⁶ | ◐⁶ | ◐⁶ | ◐¹⁶ | ❌⁷ | ⛔⁹ | ❌¹¹ |
| `keys[].pin_required` | ❌⁸ | ❌⁸ | ❌⁸ | ❌⁸ | ❌⁸ | ⛔¹⁷ | ❌⁸ | ⛔⁹ | ❌¹¹ |

¹ Ubuntu assigns `self.keys` at `lis2autoinstall.py:92` and never reads it — a dead map. SILENT;
no tracker warning fires because the assignment counts as a read.
² Never read; `check_unread` warns. Containers are named by `storage.encryption[].id`, never by
this handle. The `keys` ↔ `storage.encryption` cross-reference described in `schema.md` §17.2 is
not expressible in schema v0.1 and is implemented by no applier.
³ Only `keyfile`, `gpg` and `age` select key material (`lis_common.py:849`); `tpm2` and `fido2`
gate post-install enrollment (`:988`). `yubikey_fido2`, `yubikey_challenge`, `ssh` and any typo
are inert — and since the schema puts no enum on this field, a typo validates clean.
⁴ Only `purpose: "disk_encryption"` is ever consulted (`lis_common.py:847`, `:989`). Every other
purpose (`payload_decryption`, `user_ssh_key`, `user_pam_auth`, `remote_auth`, `secret_decryption`)
is a SILENT no-op on all nine.
⁵ Hardware token matching is never evaluated anywhere. Note the schema keeps `keys[].match` open
(no `additionalProperties: false`), so typos here are accepted by validation too.
⁶ Read only when `storage.encryption` exists **and** `purpose` includes `disk_encryption`
**and** `type` ∈ {keyfile, gpg, age}. Otherwise unread. On NixOS the path is emitted as
`passwordFile` but `gpg`/`age` material is never actually decrypted.
⁷ **Alpine, SILENT and fatal.** `lis2alpine.py:124` gates on the full document but `:468` re-calls
`luks_key_path` with only `{"storage": {"encryption": [container]}}` — `keys` stripped. A container
with no `key` object plus a valid `keys[]` keyfile emits
`cryptsetup luksFormat ... --key-file None`. Reproduced.
⁸ No PIN policy is emitted by any applier — not to crypttab, not to enrollment.
⁹ **Void refuses any `keys[]` entry outright** (`lis2void.py:886-888`), before anything else is
translated: it configures no key material, and it refuses `storage.encryption` in any case. The
most honest cell in this table.
¹⁰ **Gentoo, SILENT.** `enrollment_commands` builds its token map from `keys[].type` and
`keys[].purpose` (`lis_common.py:1046-1047`) — which counts as a read, so the tracker stays quiet
— and then iterates `storage.encryption`, which `lis2gentoo.py:983-986` has already refused. With
no encryption declared, the entry is accepted and **nothing whatsoever is emitted**. Verified: a
full `keys[]` entry produces no diagnostic for either leaf and appears in none of the eight
generated artifacts.
¹¹ Never read on Gentoo; `check_unread` warns.
¹² **NixOS reads `id`** (`lis2nixos.py` `check_keys`) — but only to name the entry in its
diagnostics and to detect ambiguity, because the §17.2 cross-reference still does not exist in
schema v0.1. One `keyfile` entry warns that it keys *every* `storage.encryption[]` container, by
purpose rather than by id; **two of them refuse**, since `luks_key_path` returns the first match
and never looks again, so the second key is one the document declared and the machine will not
have.
¹³ NixOS: `keyfile` becomes disko key material and `tpm2`/`fido2` become a `systemd-cryptenroll`
flag. Everything else refuses by name — an unknown type against SPEC §17.1's list (the schema puts
no enum here, so a typo validates), and `yubikey_fido2`/`yubikey_challenge`/`ssh`/`passphrase`
because nothing reads them and the container would silently fall back to the seed passphrase.
**`gpg` and `age` refuse**: `luks_key_path` hands the seed path to disko as `passwordFile` and
cryptsetup reads it byte for byte, so the container would be keyed on the *armoured ciphertext*
and no later unlock could reproduce it (footnote ⁶'s hazard, closed).
¹⁴ NixOS honours `disk_encryption` and refuses every other role **by name**, plus an unknown role
against SPEC §17.1's list and a missing `purpose` entirely. The five silent no-ops of footnote ⁴
are gone on this column.
¹⁵ **NixOS refuses `keys[].match`**: no applier here evaluates token matching and NixOS has no
option that selects a token by serial or vendor, so the key would be used with whatever token
happened to be plugged in. The sub-keys are consumed so the refusal is the single diagnostic.
¹⁶ NixOS emits the resolved path as disko's `passwordFile` for `type: keyfile`, and **refuses a
keyfile with no secret reference** (SPEC §2.4) rather than handing disko `None`. Irrelevant to
`tpm2`/`fido2`, which carry no material. Read before any sibling refusal returns, so a refusal on
one leaf does not leave this one looking unconsulted.
¹⁷ **NixOS refuses `pin_required`**: enrollment goes through `lis_common.enrollment_commands`,
which runs `systemd-cryptenroll` **without** `--tpm2-with-pin`, so the slot would be created with
no PIN and the document's requirement silently relaxed. If that helper ever gains the flag, this
refusal is the one line to delete.

## 2.4 target

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `target.arch` | ◐¹ | ◐¹ | ◐¹ | ◐¹ | ◐¹ | ✅² | ✅¹ | ◐¹⁸ | ◐²⁰ |
| `target.firmware` | ✅³ | ◐⁴ | ✅⁵ | ◐⁴ | ◐⁶ | ✅⁷ | ◐⁸ | ⛔¹⁹ | ◐²¹ |
| `target.disks[].id` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ◐⁹ | ◐²² | ✅ |
| `target.disks[].match.path` | ✅ | ✅¹⁰ | ✅¹⁰ | ✅¹⁰ | ✅¹⁰ | ✅¹⁰ | ✅¹⁰ | ✅ | ✅¹⁰ |
| `target.disks[].match.serial` | ◐¹¹ | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ❌²³ | ◐¹² |
| `target.disks[].match.model` | ◐¹¹ | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ❌²³ | ◐¹² |
| `target.disks[].match.wwn` | ◐¹³ | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ❌²³ | ◐¹² |
| `target.disks[].match.min_size` | ⛔¹⁴ | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ❌²³ | ◐¹² |
| `target.disks[].match.max_size` | ⛔¹⁴ | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ❌²³ | ◐¹² |
| `target.disks[].match.type` | ◐¹⁵ | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹⁶ | ❌²³ | ◐¹² |
| `target.disks[].match.smallest` | ◐¹⁷ | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ❌²³ | ◐¹² |
| `target.disks[].match.largest` | ◐¹⁷ | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ◐¹² | ❌²³ | ◐¹² |

¹ Gate only, via `check_arch`. `x86_64` passes and emits nothing; `aarch64` and `riscv64` refuse.
² NixOS is the only applier that actually *uses* the value: all three enum members map to
`nixpkgs.hostPlatform` (`lis2nixos.py:515-517`). There is no `check_arch` call.
³ Drives `bios_grub`, `grub_device` and the ESP flag (`lis2autoinstall.py:82,183-192,218-220`).
⁴ Only the "ESP declared on a BIOS target" coherence check runs (`lis_common.py:144-151`); no
firmware directive is emitted — d-i / YaST autodetect.
⁵ `bios` adds a `biosboot` partition; `"auto"` is silently treated as `uefi`.
⁶ Only sets `removable = firmware != "bios"`; archinstall autodetects the rest.
⁷ `bios` adds an `EF02` partition and `grub.devices`; absent means UEFI.
⁸ Manual path only (see combination rule C-27). On the simple path the value is read and reaches
nothing.
⁹ Alpine, manual path only. On the simple path only `disks[0]`'s path is used.
¹⁰ Required in translate-only mode: a disk with no `match.path` refuses outright unless `--apply`
resolves it from the other selectors.
¹¹ Emitted into the subiquity match spec, but **only when `match.path` is absent**
(`lis2autoinstall.py:139-141,173-177`); with a path present these are warned-ignored.
¹² `--apply` only, via `resolve_disk_paths` → `lsblk` (`lis_common.py:1219-1245`). In a
translate-only run they are warned as "not evaluated" (`:772-774`) and the disk refuses for lack
of a path. **Note the asymmetry with Ubuntu**: Ubuntu emits a real match spec, so the selector
survives into a shippable profile.
¹³ Emitted, but `wwn` is not a documented subiquity match key — output is produced, effect
unconfirmed.
¹⁴ Ubuntu explicitly refuses these two: "has no subiquity equivalent"
(`lis2autoinstall.py:152-155`). Under `--apply` they are honoured by `lis_common.py:1233-1236`
instead. The only cell in this table where Ubuntu is stricter than the rest.
¹⁵ `ssd`/`hdd` become the boolean `ssd:` key; `nvme` becomes a `/dev/nvme*` path glob.
¹⁶ Selecting an NVMe device on Alpine then breaks partition-node naming (`lis2alpine.py:449`
appends a bare ordinal, producing `/dev/nvme0n11` instead of `/dev/nvme0n1p1`).
¹⁷ Emitted as `size: largest` / `size: smallest`; setting both silently keeps the last one.
¹⁸ Void: gate only, as ¹. Note that `XBPS_ARCH` is one of the VAI variables this applier declares
it may speak (`lis2void.py:97-101`) and it is never assigned, so the live ISO's own architecture
governs.
¹⁹ **Void refuses every firmware but `bios`** (`lis2void.py:675-679`) — including `auto`. The VAI
copy on the pinned ISO writes an MBR label and its initramfs carries no `mkfs.vfat`, so no ESP can
be created. Void is the only one of the nine that cannot install a UEFI system (rule C-42).
²⁰ Gentoo: `x86_64` is complete. `aarch64` correctly selects the `arm64` autobuilds directory,
CHOST and `ACCEPT_KEYWORDS="~arm64"` (`lis2gentoo.py:85-88,193`), but `grub-install` is hardcoded
to `i386-pc`/`x86_64-efi` and `GRUB_PLATFORMS` to `pc efi-64` (`:201,891-896`), so the install
dies at the bootloader. Loud, not silent — but not an aarch64 install either.
²¹ **Gentoo: only the literal `"uefi"` means UEFI.** The key defaults to `uefi` when absent, but
the enum member `"auto"` falls into the `else` branch at `lis2gentoo.py:319-320` and silently
yields an msdos label, no ESP flag, no `sys-boot/efibootmgr` and `grub-install --target=i386-pc`.
Verified. Declaring `auto` on Gentoo means BIOS (rule C-43).
²² Void consumes the handle and emits it nowhere: VAI takes one `disk` path, and the applier
refuses any document that does not declare exactly one disk, so the handle can only ever name
that one.
²³ **Void has no `--apply` path at all**, so unlike ¹² these selectors are not deferred — they are
warned "not evaluated" by `match_selectors` and reach nothing, and a disk without `match.path`
refuses (`lis2void.py:180-183`).

## 2.5 storage

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `storage.wipe` | ◐¹ | ◐² | ◐³ | ✅ | ✅⁴ | ◐³ | ◐⁵ | ⛔⁷¹ | ◐⁷² |
| `storage.partitions[].disk` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ◐⁶ | ❌⁷³ | ✅ |
| `storage.partitions[].id` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ◐⁶ | ❌⁷³ | ◐⁷⁴ |
| `storage.partitions[].role` | ◐⁷ | ✅ | ✅ | ✅ | ✅ | ◐⁸ | ◐⁶ | ✅⁷⁵ | ✅⁷⁵ |
| `storage.partitions[].size` | ◐⁹ | ◐⁹ | ◐¹⁰ | ◐⁹ | ◐¹¹ | ✅ | ◐¹² | ◐⁷⁶ | ◐⁷⁷ |
| `storage.partitions[].fs` | ◐¹³ | ◐¹⁴ | ?¹⁵ | ◐¹⁴ | ◐¹⁶ | ✅¹⁷ | ◐¹⁸ | ◐⁷⁸ | ◐⁷⁹ |
| `storage.partitions[].label` | ✅ | ❌ | ◐¹⁹ | ❌ | ❌ | ❌ | ❌ | ✅⁸⁰ | ✅⁸⁰ |
| `storage.partitions[].mountpoint` | ◐²⁰ | ✅ | ✅ | ✅ | ✅ | ✅ | ◐²¹ | ◐⁸¹ | ✅ |
| `storage.partitions[].mount_options[]` | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ | ✅⁸² | ✅⁸² |
| `…partitions[].subvolumes[].name` | ⚙²² | ⚙²³ | ✅ | ◐²⁴ | ✅ | ✅ | ◐²⁵ | ✅⁸³ | ✅⁸³ |
| `…partitions[].subvolumes[].mountpoint` | ⚙²² | ⚙²³ | ✅ | ✅ | ✅ | ✅ | ◐²⁵ | ✅⁸³ | ✅⁸³ |
| `…partitions[].subvolumes[].mount_options[]` | ⚙²² | ◐²⁶ | ❌ | ❌ | ❌ | ✅ | ❌ | ✅⁸² | ✅⁸² |
| `…partitions[].existing.match.partition` | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ❌²⁸ | ⛔²⁷ | ⛔⁸⁴ | ⛔⁸⁴ |
| `…partitions[].existing.match.label` | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ❌²⁸ | ⛔²⁷ | ⛔⁸⁴ | ⛔⁸⁴ |
| `…partitions[].existing.match.uuid` | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ❌²⁸ | ⛔²⁷ | ⛔⁸⁴ | ⛔⁸⁴ |
| `…partitions[].existing.match.fs` | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ❌²⁸ | ⛔²⁷ | ⛔⁸⁴ | ⛔⁸⁴ |
| `…partitions[].existing.format` | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ❌²⁸ | ⛔²⁷ | ⛔⁸⁴ | ⛔⁸⁴ |
| `…partitions[].existing.resize` | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ⛔²⁷ | ❌²⁸ | ⛔²⁷ | ⛔⁸⁴ | ⛔⁸⁴ |
| `storage.encryption[].id` | ✅ | ◐²⁹ | ✅ | ✅ | ✅ | ✅ | ✅ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.encryption[].over` | ✅ | ✅ | ✅ | ◐³⁰ | ✅ | ✅ | ◐³¹ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.encryption[].type` | ◐³² | ❌³³ | ❌³³ | ✅ | ❌³³ | ✅³⁴ | ❌³³ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.encryption[].key.passphrase` | ◐³⁵ | ◐³⁵ | ✅³⁶ | ✅³⁶ | ◐³⁵ | ◐³⁵ | ◐³⁵ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.encryption[].key.keyfile` | ✅ | ◐³⁷ | ✅ | ✅ | ◐³⁸ | ✅³⁹ | ✅ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.encryption[].unlock[]` | ◐⁴⁰ | ⚙⁴¹ | ◐⁴¹ | ⚙⁴¹ | ◐⁴² | ⚙⁴¹ | ◐⁴³ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.lvm[].name` | ✅ | ◐⁴⁴ | ✅ | ◐⁴⁵ | ✅ | ✅ | ✅ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.lvm[].devices[]` | ✅ | ✅ | ✅ | ◐⁴⁵ | ✅ | ✅ | ✅ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.lvm[].volumes[].name` | ✅ | ✅ | ✅ | ◐⁴⁵ | ✅ | ✅ | ✅ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.lvm[].volumes[].size` | ◐⁹ | ◐⁹ | ◐¹⁰ | ◐⁹ | ◐¹¹ | ✅ | ◐⁴⁶ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.lvm[].volumes[].fs` | ◐⁴⁷ | ◐¹⁴ | ✅⁴⁸ | ◐⁴⁵ | ◐⁴⁹ | ✅ | ◐⁵⁰ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.lvm[].volumes[].label` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.lvm[].volumes[].mountpoint` | ✅ | ✅ | ✅ | ◐⁴⁵ | ✅ | ✅ | ◐²¹ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.lvm[].volumes[].mount_options[]` | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ | ⛔⁸⁵ | ⛔⁸⁶ |
| `…lvm[].volumes[].subvolumes[].name` | ⚙²² | ⚙²³ | ✅ | ◐⁴⁵ | ✅ | ✅ | ◐⁵¹ | ⛔⁸⁵ | ⛔⁸⁶ |
| `…lvm[].volumes[].subvolumes[].mountpoint` | ⚙²² | ⚙²³ | ✅ | ◐⁴⁵ | ✅ | ✅ | ◐⁵¹ | ⛔⁸⁵ | ⛔⁸⁶ |
| `…lvm[].volumes[].subvolumes[].mount_options[]` | ⚙²² | ◐²⁶ | ❌ | ❌ | ❌ | ✅ | ❌ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.raid[].name` | ✅ | ◐⁵² | ✅ | ◐⁵³ | ⚙⁵⁴ | ✅ | ✅ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.raid[].level` | ✅ | ✅ | ✅ | ◐⁵³ | ⚙⁵⁵ | ✅ | ✅ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.raid[].devices[]` | ✅ | ✅ | ✅ | ◐⁵³ | ⚙⁵⁴ | ✅ | ✅ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.raid[].spares[]` | ✅ | ✅ | ◐⁵⁶ | ❌ | ❌⁵⁷ | ❌⁵⁸ | ❌ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.swap.zram.size` | ❌⁵⁹ | ❌⁵⁹ | ❌⁵⁹ | ❌⁵⁹ | ❌⁵⁹ | ❌⁵⁹ | ❌⁶⁰ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.swap.file.path` | ⚙⁶¹ | ⚙⁶¹ | ❌ | ❌ | ❌ | ✅ | ❌ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.swap.file.size` | ⚙⁶² | ⚙⁶² | ❌ | ❌ | ❌ | ◐⁶³ | ❌ | ⛔⁸⁵ | ⛔⁸⁶ |
| `storage.snapshots.enabled` | ⚙⁶⁴ | ⚙⁶⁴ | ⚙⁶⁴ | ◐⁶⁵ | ⚙⁶⁴ | ✅⁶⁶ | ⛔ | ⛔⁸⁷ | ⛔⁸⁷ |
| `storage.snapshots.tool` | ◐⁶⁷ | ◐⁶⁷ | ◐⁶⁷ | ◐⁶⁷ | ◐⁶⁷ | ◐⁶⁷ | ⛔ | ⛔⁸⁷ | ⛔⁸⁷ |
| `storage.snapshots.boot_menu` | ⚙⁶⁸ | ⚙⁶⁸ | ⛔ | ◐⁶⁹ | ⚙⁷⁰ | ⛔ | ⛔ | ⛔⁸⁷ | ⛔⁸⁷ |

¹ `true` is correct. `false` emits `wipe: preserve` on the disk — **not a valid curtin value** —
while child partitions carry `preserve: false`. Every repo example sets `true`, so this path is
untested.
² `true` accepted; `false` refuses. partman wipes regardless of the value.
³ `false` refuses outright, so every honoured Fedora/NixOS document is destructive.
⁴ `false` is accepted but partitions are still created from 1MiB with no accounting of what was
there.
⁵ `false` still runs mkpart + mkfs (only `mklabel` is skipped); on the simple path it wipes
regardless.
⁶ Manual path only (combination rule C-27). Dropped on the simple path with no tracker warning —
the field *is* read, it just reaches nothing.
⁷ `esp`/`swap` get their flags; `data` and `raw` silently get no filesystem.
⁸ **The NixOS silent killer.** `role: root|boot|swap` with no explicit `fs` produces a disko
partition with no `content` block (`lis2nixos.py:306`) while `hardware.nix` still writes
`fileSystems."/"`. Verified: 0 warnings, 0 refusals, unbootable result. Always emit an explicit
`fs`.
⁹ Absolute sizes and `rest` work; `NN%` refuses.
¹⁰ Percent refuses; `rest` becomes `--size=1024 --grow`.
¹¹ **SILENT**: `%` is treated as `rest`; two percent siblings overlap. Resolved only under
`--apply` (`lis2archinstall.py:1036`); a shipped profile carries `value: 0`.
¹² **SILENT**: `%` computes 0MiB.
¹³ `zfs` refuses; `f2fs` is emitted, curtin support unverified.
¹⁴ `f2fs` and `zfs` pass through unmapped and break partman / YaST at run time. SILENT.
¹⁵ **UNKNOWN, carried through from the Fedora classifier.** `f2fs`/`zfs` are emitted verbatim as
`--fstype=…` with no refusal and no warning. Anaconda's acceptance of f2fs varies by release; zfs
certainly fails. Output *is* produced, so this is not a drop — but the failure mode was not
executed.
¹⁶ `zfs` passed verbatim and `none` emits null; archinstall rejects both.
¹⁷ `zfs` silently joins an invented pool named `rpool`; `f2fs` passes through.
¹⁸ `zfs`/`f2fs` refuse. Manual path only — on the simple path `fs` is dropped silently.
¹⁹ Honoured alone, **silently discarded** when the same partition declares `subvolumes`
(`lis2kickstart.py:227` rebuilds the `part` line keeping only size/ondisk/crypt). Verified.
²⁰ Emitted, but Ubuntu never calls `resolve_mountpoints`: two partitions resolving to the same
path emit two curtin `mount` actions, SILENTLY.
²¹ Only `/` and the first `/boot`|`/boot/efi` are mounted. Other mountpoints are formatted and
then abandoned.
²² curtin has no subvolume vocabulary — emulated in btrfs late-commands
(`lis2autoinstall.py:483-528`). The root-subvol variant relocates the installed root.
²³ late_command btrfs conversion, root filesystem only (`lis2debian.py:590-691`).
²⁴ AutoYaST renames `@home` → `@/home` under the volume prefix (warned); Agama takes the name
verbatim.
²⁵ Only on the root partition (or the root LV). Elsewhere, dropped.
²⁶ Nested subvolumes' options reach fstab; the **root subvolume's own** options are silently
ignored.
²⁷ The whole `existing.*` subtree refuses: dual-boot adoption is not expressible in any of these
unattended installers.
²⁸ **NixOS differs — it does not refuse.** Adoption is skipped at `lis2nixos.py:439` and the
leaves are warned as unread. A dual-boot document silently becomes a fresh-install document.
²⁹ Drives the key path and the wiring, but the mapper name stays whatever partman chooses.
³⁰ Must name a partition handle. `over` a RAID array or an LV refuses via
`check_encryption_emitted`.
³¹ **SILENT**: `crypt_over` is keyed by partition id and consulted only in the partition loop
(`lis2alpine.py:459-478`). `over` naming a `raid[]` array emits **no `luksFormat` at all**, then
hands `/dev/mapper/<id>` to `pvcreate`. Reproduced.
³² Warned only: `luks1` is requested, curtin creates LUKS2.
³³ `luks1` silently becomes the installer default (LUKS2). A document asking for `luks1` because
of legacy GRUB gets an unbootable machine.
³⁴ Genuinely honoured at format time via `extraFormatArgs`.
³⁵ Resolves to the seed path `/run/lis/seed/secrets/luks-<id>.key`; substituted only under
`--apply`. Shadowed entirely when a `keys[]` disk_encryption entry supplies material (rule C-3).
³⁶ Read directly into `%pre` / the profile key placeholder.
³⁷ Read as a **passphrase from the seed**, not as a keyfile on the target.
³⁸ Unread (shadowed) whenever a `keys[]` disk_encryption entry exists.
³⁹ The file must also exist inside the booted initrd, which the applier does not arrange.
⁴⁰ Per member: `passphrase` ◐ (boot always prompts), `keyfile` ◐ — **the crypttab keyfile field is
rewritten to `none` (`lis2autoinstall.py:814-816`) with a code comment and no `warn()`, so a
keyfile-unlock document produces a machine that prompts at every boot** — `tpm2` ⚙ (emulated via
`systemd-cryptenroll`, warned), `fido2` ⛔.
⁴¹ `passphrase`/`keyfile` are no-ops; `tpm2`/`fido2` are enrolled post-install with
`systemd-cryptenroll` (`lis_common.py:993-1015`). Fedora is marked ◐ rather than ⚙ because the
passphrase/keyfile members reach nothing at all.
⁴² tpm2/fido2 enrolled post-install, but the initramfs still only knows the passphrase.
⁴³ Emits `systemd-cryptenroll` on an Alpine system that has no systemd.
⁴⁴ First LVM group only. Later groups get an `in_vg` with no backing PV — SILENT (rule C-10).
⁴⁵ **AutoYaST output only.** The Agama `profile.json` carries no volume group at all. Choosing
the wrong one of SUSE's two mutually exclusive outputs loses the whole LVM stack (rule C-25).
⁴⁶ `%` emits an invalid `lvcreate -L 50%`; `rest` is not ordered last.
⁴⁷ An omitted `fs` creates the LV unformatted, SILENTLY.
⁴⁸ btrfs LVs use the thinpool/noformat trick (`lis2kickstart.py:297-329`).
⁴⁹ Absent or `none` emits null; `archinstall`'s `LvmVolume.parse_arg` then raises.
⁵⁰ An absent `fs` leaves the LV unformatted with no refusal.
⁵¹ Honoured only when the LV is root **and** no root partition exists.
⁵² Handle only; an array not consumed by `lvm.devices` or `encryption.over` refuses
(`lis_common.py:733`).
⁵³ AutoYaST output only — the Agama profile carries no array; members are emitted as empty
partitions.
⁵⁴ Emulated between two archinstall runs: `wipefs -a` then `mdadm --create`
(`lis2archinstall.py:974-985`), array then presented as a disk.
⁵⁵ Levels 0/1/4/5/6/10 only; anything else refuses.
⁵⁶ `--spares=N` is emitted but **the spare's partition is never appended to the member list**. A
RAID1 of 2 + 1 spare becomes a 2-member array declaring 1 spare — one active mirror. Verified.
⁵⁷ The partition is created and then never passed to `mdadm`.
⁵⁸ Emits a Nix comment only; spares silently become active members. No warning.
⁵⁹ zram *is* enabled (via `zram-config` / `zram-tools` / `zramSwap.enable` /
`systemd-zram-service`) but at that package's own default size. The requested size never arrives.
⁶⁰ Alpine installs no zram package at all.
⁶¹ `fallocate` + `mkswap` + fstab in a late-command.
⁶² The `iB` suffix is stripped for `fallocate`; btrfs NOCOW is not handled.
⁶³ Only `GiB` is parsed — `MiB` and `TiB` **silently become 4GiB**.
⁶⁴ `%post`/late-command installs snapper and runs `create-config /`. btrfs-root is not checked.
⁶⁵ Package added always; the Agama btrfs `snapshots` flag is set only when subvolumes are
declared.
⁶⁶ Native `services.snapper.configs.root`, and the btrfs requirement **is** now checked. Three
fixes behind one unchanged marker, because the old cell described snapshots that would never
happen: (a) a non-btrfs root **refuses** — `services.snapper.configs.<n>.FSTYPE` is
`types.enum [ "btrfs" ]` (`services/misc/snapper.nix:57-62`) and SPEC §6.6 makes such a document
invalid — where before it emitted a config whose timer failed on every tick; (b) `FSTYPE =
"btrfs"` is stated explicitly; (c) the first-boot unit **creates `/.snapshots`**, which the
module's own documentation requires (`snapper.nix:50-52`) and which the module never creates and
disko cannot, since it is a subvolume of the root subvolume and does not exist until the root
filesystem is mounted. `pkgs.btrfs-progs` is added to that unit's `path` only when needed — a
unit's `path` *replaces* PATH rather than extending it.
⁶⁷ `auto`/`snapper` honoured; `timeshift` refuses everywhere (including Arch, where archinstall
does support it).
⁶⁸ Installs `grub-btrfs`.
⁶⁹ Installs `grub2-snapper-plugin`; no guaranteed grub regeneration.
⁷⁰ Installs `grub-btrfs` but never enables `grub-btrfsd` — and installs it even under
systemd-boot.
⁷¹ **Void refuses `wipe: false`** (`lis2void.py:168-170`): VAI runs `sfdisk` over the whole disk
and `mkfs` over the result unconditionally, so there is nothing to preserve.
⁷² Gentoo warns and continues: the partition table is left in place, but every declared partition
is still created and formatted (`lis2gentoo.py:978-981`, `:349-351`). Same non-preservation as
everyone else, at least stated.
⁷³ **Void, SILENT.** Both handles are consumed (`lis2void.py:188`) and reach nothing. VAI's scheme
is positional — p1 is `/boot`, p2 is swap, p3 is the root — so a document with the wrong `disk`
handle or duplicate ids installs identically. Harmless because only one disk may be declared, but
the field is not honoured; it is ignored.
⁷⁴ Gentoo uses the id only to name the generated shell variable (`dev_<id>`) and the `die`
messages.
⁷⁵ The roles are checked against the fixed layout, not merely read: Void asserts
`[boot, swap, root]` in that order and refuses anything else (`lis2void.py:199-214`); Gentoo sets
the `boot`/`esp` parted flags and fills a missing `fs`/`mountpoint` from `ROLE_FS`/`ROLE_MOUNT`.
⁷⁶ Void: `bootpartitionsize` and `swapsize` take absolute sizes and the root **must** be `rest`;
`rest` on either of the first two refuses. **`NN%` is handled nowhere: `parse_size` raises an
uncaught `ValueError` and the applier exits 1 with a Python traceback** — not a refusal, not a
warning, no output at all. Reproduced (rule C-46).
⁷⁷ **Gentoo, SILENT.** `cumulative()` (`lis2gentoo.py:562-571`) understands only `TiB`, `GiB` and
`MiB`. `NN%`, the decimal units (`MB`/`GB`/`TB`), `KiB` and a bare byte count all contribute
**zero**, so the partition is emitted as `mkpart primary 1MiB 1MiB` and every sibling after it
starts at the wrong offset. Reproduced with both `"10%"` and `"512MB"`: no warning, no refusal
(rule C-45).
⁷⁸ Void: `/boot` must be ext2/3/4 — the installer initramfs carries `mke2fs` and nothing else —
and the root may additionally be btrfs, xfs or f2fs, whose `mkfs` is fetched with `xbps` into a
scratch tmpfs root over the network VAI has already brought up (`lis2void.py:355-376`). Every
other value refuses.
⁷⁹ Gentoo: ext2/3/4, btrfs, xfs, f2fs, vfat and swap. `zfs` refuses by name (not on the binary
host, not set up here) and any other value refuses generically (`lis2gentoo.py:1005-1013`).
⁸⁰ `mkfs -L` / `mkswap -L`, and `mkfs.vfat -n` on Gentoo. Both honour the label on every
partition, not only the root.
⁸¹ Void honours the mountpoint only insofar as it must equal VAI's fixed layout — partition 0 at
`/boot` and partition 2 at `/`. Any other value refuses rather than being relocated.
⁸² Both reach the target's fstab: Void by generating the `VAI_configure_fstab` step from the
document, Gentoo by passing the options to `mount` and letting `genfstab -U` record what is
actually mounted.
⁸³ Created from the replaced `VAI_mount_target` step (Void) or `lis-prepare.sh` (Gentoo); Void
additionally runs `btrfs subvolume set-default` on the `/` subvolume so a plain mount still lands
on a system. Both refuse subvolumes on a non-btrfs root, and both refuse a subvolume set with none
mounted at `/`.
⁸⁴ Refused as a subtree on both: Void always repartitions (`lis2void.py:189-192`), Gentoo only
creates partitions (`lis2gentoo.py:1001-1004`).
⁸⁵ **Void refuses every `storage` key except `wipe` and `partitions` in one loop**
(`lis2void.py:161-166`), so encryption, LVM, RAID, swap and snapshots all refuse together with a
single message. This is not five decisions — it is one: VAI formats a partition directly and there
is no other storage shape (rule C-41).
⁸⁶ Gentoo refuses each of these by name (`lis2gentoo.py:983-997`): no LUKS containers, no volume
groups, no arrays (and `sys-fs/mdadm` is not on the official binary host either, so honouring one
would additionally mean a source build), no zram and no swapfile.
⁸⁷ Both call `check_snapshots(tools=frozenset(), boot_menu=False)`, so all three leaves refuse.

## 2.6 boot

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `boot.loader` | ◐¹ | ◐² | ◐² | ◐³ | ✅⁴ | ✅⁵ | ◐⁶ | ◐³⁶ | ◐³⁶ |
| `boot.timeout` | ⚙⁷ | ⚙⁷ | ⚙⁷ | ✅⁸ | ⚙⁷ | ✅ | ◐⁹ | ⚙⁷ | ⚙⁷ |
| `boot.kernel.variant` | ◐¹⁰ | ◐¹¹ | ◐¹² | ◐¹³ | ✅¹⁴ | ✅¹⁵ | ◐¹⁶ | ◐³⁷ | ◐³⁷ |
| `boot.kernel.params[]` | ⚙¹⁷ | ✅ | ✅ | ✅ | ⚙¹⁸ | ✅¹⁹ | ◐²⁰ | ✅³⁸ | ✅³⁸ |
| `boot.kernel.modules[]` | ❌ | ❌ | ❌ | ❌ | ❌ | ✅²¹ | ❌²² | ✅³⁹ | ✅³⁹ |
| `boot.kernel.blacklist[]` | ❌ | ❌ | ❌ | ❌ | ❌ | ✅²¹ | ❌ | ✅³⁹ | ✅³⁹ |
| `boot.os_prober` | ❌ | ✅²³ | ❌ | ❌ | ❌ | ◐²⁴ | ❌ | ❌⁴⁰ | ✅⁴⁰ |
| `boot.password_hash` | ❌²⁵ | ❌²⁵ | ❌²⁵ | ❌²⁵ | ❌²⁵ | ◐⁴⁸ | ❌²⁵ | ⛔⁴¹ | ✅⁴¹ |
| `boot.console.serial` | ❌ | ❌ | ❌ | ❌ | ❌ | ✅²⁶ | ❌²⁷ | ✅⁴² | ◐⁴³ |
| `boot.secure_boot` | ❌²⁸ | ❌²⁸ | ❌²⁸ | ❌²⁹ | ❌²⁸ | ⛔⁴⁹ | ❌³⁰ | ⛔⁴⁴ | ⛔⁴⁴ |
| `boot.uki` | ❌ | ❌ | ❌ | ❌ | ❌³¹ | ⛔³² | ❌ | ⛔⁴⁴ | ⛔⁴⁴ |
| `boot.initramfs.generator` | ❌³³ | ❌³³ | ❌³³ | ❌ | ❌³³ | ⛔⁵⁰ | ❌³³ | ⛔⁴⁵ | ◐⁴⁵ |
| `boot.initramfs.include_modules[]` | ❌ | ❌ | ❌ | ❌ | ❌ | ✅³⁴ | ❌³⁵ | ✅⁴⁶ | ❌⁴⁷ |

¹ `systemd-boot` refuses; `grub`/`auto` emit nothing and subiquity installs GRUB anyway.
² `grub`/`auto` only; `systemd-boot` refuses.
³ Never selects a loader at all. A `systemd-boot` value only misdirects where the timeout is
written.
⁴ `auto` maps to systemd-boot **even when `target.firmware` is `bios`** (rule C-37).
⁵ `systemd-boot` + bios refuses; an unknown value refuses. `auto` now resolves to **grub** rather
than systemd-boot when the document also asks for `password_hash` or `os_prober`, since neither
exists under systemd-boot — picking systemd-boot would turn an honourable translation into a
refusal for no reason. GRUB on UEFI emits `device = "nodev"` + `efiInstallAsRemovable`; without a
device the configuration carries the failed assertion *"You must set the option
'boot.loader.grub.devices'"*, which under `--apply` fires **after disko has wiped the disks**.
GRUB on BIOS with no resolved `target.disks[]` path refuses for the same reason, before disko
instead of after.
⁶ `systemd-boot` refuses. Manual path only; on the simple path the value is dropped silently.
⁷ Post-install edit of `/etc/default/grub` or `loader.conf` (`lis_common.py:284-308`).
⁸ Native `<timeout>` in AutoYaST; Agama receives it only through the chroot script.
⁹ Manual path **and** grub only. On the simple path it is warned unread.
¹⁰ `lts`/`realtime` map to a kernel package; `hardened`/`zen` refuse.
¹¹ Only `default`; every other variant refuses.
¹² Only `default`; every other variant refuses.
¹³ `lts`/`realtime` mapped; `zen`/`hardened` refuse.
¹⁴ Unmapped variants refuse.
¹⁵ **All five schema values now map**: `default` → the nixpkgs default, `lts` → `linuxPackages`,
`hardened` → `linuxPackages_hardened`, `zen` → `linuxPackages_zen`, `realtime` →
**`linuxPackages-rt`** — a dash, because `pkgs.linuxPackages_rt` does not exist on 24.11 and
naming it was an evaluation error raised *after* disko had wiped the disks. Each attribute was
confirmed by forcing `boot.kernelPackages.kernel.version` (6.6.94 / 6.6.83 / 6.15.2 / 5.15.177-rt83);
an unmapped value still refuses.
¹⁶ Only `default` and `lts`.
¹⁷ `sed` on `GRUB_CMDLINE_LINUX` in a late-command; warned.
¹⁸ `sed` on loader entries or `/etc/default/grub`; refuses for loaders other than grub /
systemd-boot.
¹⁹ A `console=ttyS*` parameter also enables `serial-getty`. The list used to be declared in
**both** `hardware.nix` and `configuration.nix`, and NixOS list options merge by concatenation, so
every parameter reached the kernel command line twice; it is now emitted once, from the boot
block alone.
²⁰ Manual path only; warned "not applied" **even when it is applied**.
²¹ Applied as `boot.kernelModules` / `boot.blacklistedKernelModules` in `hardware.nix`. The false
"not applied" warning is gone: `check_boot_extras` is now passed **every** schema key under
`boot`, so a field that is applied no longer reports itself as dropped.
²² Alpine hardcodes its own module list at `lis2alpine.py:190-199`.
²³ Native `grub-installer/with_other_os`. The warning Debian prints for this field is spurious.
²⁴ grub only (`boot.loader.grub.useOSProber`, the only os-prober in `nixos/modules`). Under
systemd-boot the field is no longer dropped with a warning — it **refuses**, naming the reason.
With `loader: auto` on UEFI, asking for it now selects grub instead (footnote ⁵).
²⁵ GRUB is left unprotected on these six. There is no systemd-boot equivalent anyway. Gentoo and
NixOS implement the field (footnotes 41 and 48) and Void refuses it (footnote 41).
²⁶ **NixOS now reads the field itself.** It appends `console=<value>` to `boot.kernelParams`,
enables `systemd.services."serial-getty@<tty>"`, and under GRUB writes `serial --unit=N --speed=S`
plus `terminal_input/output --append serial` into `boot.loader.grub.extraConfig`, which
`install-grub.pl:434` places ahead of the menu entries. Two honest caveats, both warned: GRUB
addresses UARTs **by unit number**, so a console that is not `ttyS<N>` reaches the kernel and the
getty but not the boot menu; and systemd-boot has no serial-terminal option in NixOS at all, so
its menu stays on the firmware console.
²⁷ Worse than a drop: Alpine **hardcodes** `ttyS0,115200` (`lis2alpine.py:646-647`), so you get a
serial console you did not ask for, on a port you did not choose.
²⁸ Nothing is emitted. This does **not** mean Secure Boot necessarily fails — several of these
distros ship a signed shim by default — only that the document's request influences nothing.
Carried through as the Fedora classifier's honest gap. Void, Gentoo and NixOS refuse it instead
(footnotes 44 and 49).
²⁹ AutoYaST has a `<secure_boot>` element; it is never emitted.
³⁰ `grub-install --removable`, unsigned.
³¹ Arch hardcodes `"uki": false` (`lis2archinstall.py:646`) — the profile asserts the **opposite**
of `uki: true`.
³² **NixOS refuses it.** The classification `boot.uki.enable` "exists and is unused" was wrong —
that option does **not** exist on 24.11 (only `boot.uki.name/.settings/.version`). The tree can
*build* a UKI (`system.build.uki`, `modules/system/boot/uki.nix:108`) but no bootloader module
installs one: the single in-tree consumer is the disk-image builder
`modules/image/repart-verity-store.nix:169`. A UKI install needs the out-of-tree lanzaboote module.
³³ The distro's own generator (initramfs-tools / dracut / mkinitcpio / mkinitfs) is used
regardless of the request.
³⁴ Applied as `boot.initrd.availableKernelModules`. The contradictory "boot.initramfs is not
applied" warning is gone (footnote 21).
³⁵ The Alpine module list at `:190-200` is fixed.
³⁶ `grub`/`auto` only on both; `systemd-boot` refuses. VAI installs GRUB and nothing else, and
`sys-boot/systemd-boot` is not published on Gentoo's official binary host.
³⁷ Void: `lts` → the `linux-lts` package; `default`/absent are fine; everything else refuses.
**Gentoo maps `lts` to `sys-kernel/gentoo-kernel-bin` — the exact package `default` gets**
(`lis2gentoo.py:1068-1070`), so an LTS request is honoured in name only, SILENTLY; `zen`,
`hardened` and `realtime` refuse.
³⁸ Void deletes and re-appends `GRUB_CMDLINE_LINUX_DEFAULT` in the target's `/etc/default/grub`
and re-runs `update-grub` (`lis2void.py:456-460`) — deliberately not `GRUB_CMDLINE_LINUX`, which
Void's shipped file does not contain, so a plain `sed` on it would be a silent no-op. Gentoo
writes the whole `/etc/default/grub` from a heredoc and runs `grub-mkconfig`.
³⁹ Both write the target's own `/etc/modules-load.d/lis.conf` and
`/etc/modprobe.d/lis-blacklist.conf` (`lis2void.py:473-484`, `lis2gentoo.py:867-871`). **With
NixOS, these are the only three columns where either field reaches anything.**
⁴⁰ Void warns: VAI wipes the disk it installs to, so there is nothing else on it to detect.
Gentoo emits `GRUB_DISABLE_OS_PROBER` from the value (`lis2gentoo.py:882`) — the second applier
after Debian to honour it, and the first to honour `false` explicitly.
⁴¹ **Gentoo is the only applier of the nine that implements it**: `/etc/grub.d/01_lis_password`
with `set superusers="root"` and `password_pbkdf2` (`lis2gentoo.py:884-888`). Note GRUB's own
semantics — `superusers` with no `--unrestricted` menu entries protects *booting*, not only
editing. Void refuses the field rather than leaving the menu open.
⁴² **Void is the only applier where this field reaches the installed system.** The value is
appended to the kernel command line as `console=<value>` (`lis2void.py:805-807`), GRUB is given a
serial terminal, and a matching `agetty-<tty>` runit service is enabled — guarded, so a tty runit
ships no `/etc/sv` directory for aborts `lis-post.sh` rather than being dropped in silence. A tty
outside `SERIAL_TTYS` warns.
⁴³ Gentoo: the value reaches `GRUB_CMDLINE_LINUX_DEFAULT` **only when no `console=ttyS*` kernel
parameter is already present** (`lis2gentoo.py:865-866`), and the getty is hardcoded to `ttyS0` at
115200 whatever baud or port the document names — and is enabled even when the field is absent.
The same hazard as Alpine's ²⁷, with the difference that Gentoo warns about it
(`lis2gentoo.py:908-911`).
⁴⁴ **Void and Gentoo refuse `secure_boot: true` and `boot.uki` rather than dropping them** — the
first two of the nine to do so. `false` on either is a no-op, which is what it asks for.
⁴⁵ Both refuse any generator other than `auto`/`dracut`. Void then emits nothing for the value —
dracut is what Void uses regardless — while Gentoo genuinely configures it:
`sys-kernel/installkernel dracut` in `package.use` plus `sys-kernel/dracut` in `@lis`, without
which the dist-kernel lands in `/boot` with no initramfs.
⁴⁶ Void writes `/etc/dracut.conf.d/10-lis.conf` (`add_drivers+=`) and reconfigures the kernel
package (`lis2void.py:490-496`).
⁴⁷ **Gentoo warns that this field "is folded into the generated `/etc/dracut.conf.d/lis.conf`
force_drivers list" (`lis2gentoo.py:1062-1064`) — and it is not.** `dracut_conf` (`:281-305`)
builds `force_drivers` from a hardcoded device list plus the root filesystem and never reads
`include_modules`. Reproduced with a unique module name: it appears in none of the eight generated
artifacts. This is worse than a silent drop — the diagnostic channel asserts the opposite of the
truth (rule C-47).
⁴⁸ **NixOS is the second applier after Gentoo to implement it.** `boot.loader.grub.users.root.
hashedPassword` (grub.nix:211); the module writes `set superusers=` itself
(`install-grub.pl:296`), so no `/etc/grub.d` file is needed. Two guards: under **systemd-boot it
refuses**, there being no counterpart; and a value that is not a `grub.pbkdf2.*` string **refuses**
rather than being written, because GRUB understands only its own PBKDF2 format and a crypt(3) hash
is not a weaker password but *no* password — it leaves the menu unenterable by everyone including
the operator. Warned, per Gentoo's footnote 41: this guards the menu and command line, not the
disk, and the hash is readable from both the Nix store and `/boot/grub/grub.cfg`.
⁴⁹ **NixOS refuses `secure_boot: true`** (`auto`/`false` are the no-op they ask for). Nothing in
`nixos/modules` signs or installs a shim; the tree's only `secureBoot` is
`virtualisation.useSecureBoot` (`qemu-vm.nix:997`), a guest-firmware switch for test VMs. Secure
Boot on NixOS needs the out-of-tree lanzaboote module.
⁵⁰ **NixOS refuses any generator but `auto`.** `dracut`, `mkinitcpio` and `booster` appear nowhere
in `nixos/modules` (`grep -rl` returns nothing); NixOS builds its own initrd, optionally the
systemd one via `boot.initrd.systemd.enable`.

## 2.7 system

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `system.hostname` | ✅ | ✅ | ✅ | ✅ | ✅¹ | ✅² | ✅³ | ✅⁴⁴ | ✅⁴⁵ |
| `system.timezone` | ✅⁴ | ✅ | ✅⁴ | ✅ | ✅⁴ | ✅ | ✅⁴ | ✅⁴ | ✅⁴⁵ |
| `system.locale` | ✅⁵ | ✅ | ✅⁵ | ✅⁶ | ✅ | ✅ | ❌⁷ | ✅⁴⁴ | ✅⁴⁵ |
| `system.extra_locales[]` | ⚙⁸ | ⚙⁸ | ❌⁹ | ❌⁹ | ⚙⁸ | ✅¹⁰ | ❌⁹ | ⚙⁴⁶ | ✅⁴⁷ |
| `system.keymap.console` | ◐¹¹ | ◐¹¹ | ✅ | ✅ | ◐¹¹ | ✅ | ◐¹¹ | ✅⁴⁴ | ✅⁴⁵ |
| `system.keymap.layout` | ✅ | ✅ | ✅ | ✅ | ✅¹² | ✅ | ✅ | ◐⁴⁸ | ◐⁴⁸ |
| `system.keymap.variant` | ✅ | ✅ | ✅ | ◐¹³ | ❌¹⁴ | ✅¹⁵ | ✅ | ❌ | ❌ |
| `system.keymap.font` | ❌ | ❌ | ❌ | ❌ | ❌¹⁶ | ✅⁶⁰ | ❌ | ?⁴⁹ | ✅⁵⁰ |
| `system.init` | ❌¹⁷ | ❌¹⁷ | ❌¹⁷ | ❌¹⁷ | ❌¹⁷ | ◐¹⁸ | ❌¹⁹ | ◐⁵¹ | ◐⁵¹ |
| `system.security.module` | ◐²⁰ | ◐²⁰ | ◐²¹ | ◐²⁰ | ◐²² | ◐²³ | ⛔²⁴ | ⛔⁵² | ◐⁵³ |
| `system.domain` | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ | ❌²⁵ | ❌⁵⁴ | ✅⁵⁴ |
| `system.hwclock` | ⚙²⁶ | ✅²⁷ | ✅²⁷ | ⚙²⁶ | ⚙²⁶ | ✅²⁸ | ⚙²⁶ | ✅⁵⁵ | ✅⁵⁵ |
| `system.time.ntp` | ❌ | ✅ | ❌ | ❌ | ✅²⁹ | ◐³⁰ | ✅ | ✅⁵⁶ | ◐⁵⁷ |
| `system.time.servers[]` | ❌ | ◐³¹ | ❌ | ❌ | ❌ | ✅ | ❌ | ✅⁵⁶ | ❌⁵⁷ |
| `system.time.provider` | ❌ | ❌ | ❌ | ❌ | ❌³² | ◐³³ | ◐³⁴ | ◐⁵⁶ | ◐⁵⁷ |
| `system.locale_overrides.<LC_*>` | ⚙³⁵ | ◐³⁶ | ⚙³⁵ | ⚙³⁵ | ⚙³⁵ | ✅ | ⚙³⁷ | ⚙³⁵ | ✅⁵⁸ |
| `system.kdump` | ❌ | ❌ | ❌³⁸ | ❌³⁹ | ❌ | ✅⁴⁰ | ❌ | ❌ | ⛔⁵⁹ |
| `system.telemetry` | ❌⁴¹ | ◐⁴² | ❌ | ❌ | –⁴³ | –⁶¹ | –⁴³ | –⁴³ | –⁴³ |

¹ Defaults to `archlinux` when absent.
² Also seeds the ZFS `hostId`.
³ Defaults to `alpine` when absent.
⁴ Defaults to UTC when absent.
⁵ Defaults to `en_US.UTF-8` when absent.
⁶ The codeset is stripped for AutoYaST's `<language>`.
⁷ Explicitly warned: Alpine is musl and `setup-alpine` has no locale step.
⁸ `locale.gen` append + `locale-gen` in a post-install script.
⁹ `LOCALE_GEN` is `None` for this distro (`lis_common.py:698-701`): the value is read, warned and
discarded. No `glibc-langpack-*` packages are added on Fedora.
¹⁰ `i18n.supportedLocales`, **rebuilt from the whole document** rather than from `en_US.UTF-8` plus
the extras. That option *replaces* a default which already carries `C.UTF-8`, `en_US.UTF-8`,
`i18n.defaultLocale` and every `extraLocaleSettings` value (`config/i18n.nix:59-74`), so the old
two-element base built a glibc locale archive with **no entry for the document's own
`system.locale`** — `LANG` named a locale the system did not have. The set is now
`C.UTF-8`, `en_US.UTF-8`, `system.locale`, every `locale_overrides` value, and the extras.
¹¹ Used only as a fallback when `keymap.layout` is absent; warned when the two differ.
¹² One keymap serves both console and X — archinstall has no separate console keymap.
¹³ Agama only (`layout(variant)`); AutoYaST's `<keymap>` ignores the variant.
¹⁴ archinstall takes no variant.
¹⁵ Now emitted independently of `keymap.layout` (rule C-20 no longer applies to this column):
`services.xserver.xkb.variant` stands on its own, and since `xkb.layout` defaults to `"us"`, a
document naming only a variant gets a variant of the distro default — which is what it asked for.
¹⁶ `locale_config.console_font` exists in archinstall and is unused.
¹⁷ **Spec §2.3 says an applier MUST fail on an init it cannot provide. None of these six do.** A
document asking for `openrc` on Debian, or `systemd` on Alpine, is accepted and the distro's own
init is installed.
¹⁸ `systemd`/`auto` OK; `openrc`/`runit`/`s6` refuse. NixOS, Void and Gentoo are the three
appliers that obey §2.3 (footnote 51).
¹⁹ Asking for systemd on OpenRC Alpine is not refused.
²⁰ Package only (`apparmor` / `selinux-policy`); the LSM is never activated, no `lsm=` kernel
parameter, no relabel. On Debian, `selinux` gets a package it cannot use; on SUSE the same.
²¹ `selinux`/`none` honoured; `apparmor` refuses.
²² apparmor package only, no `lsm=` parameter, no unit; `selinux` refuses.
²³ **The one column where `apparmor` actually switches the LSM on.** `security.apparmor.enable`
adds `apparmor=1 security=apparmor` to `boot.kernelParams` itself (`security/apparmor.nix:194-197`)
— verified by reading `config.boot.kernelParams` back — so this is not the "package only" gap of
footnotes 20/22/53. `security.apparmor.packages = [ pkgs.apparmor-profiles ]` is emitted alongside
it, because `enable` on its own leaves the parser include path empty and any profile carrying
`include <abstractions/base>` fails to load; nixpkgs ships that line as an opt-in module which is
**not** in `module-list.nix`. Warned honestly: `security.apparmor.policies` is empty, so nothing
beyond the shipped profiles is confined. `none` is now stated outright rather than omitted, `auto`
records that NixOS's own default is no LSM, `selinux` refuses **with the reason** (no SELinux
module in `nixos/modules`, kernel built without `CONFIG_SECURITY_SELINUX`, no policy store), and
anything outside SPEC §8's enum refuses.
²⁴ `LSM_PACKAGES["alpine"]` is `None`, so both `apparmor` and `selinux` refuse; `auto`/`none` are
no-ops.
²⁵ `setup-dns -d` exists and is unused.
²⁶ `/etc/adjtime` written by a post-install script.
²⁷ Native (`clock-setup/utc`, `timezone --utc`) **plus** the `/etc/adjtime` script.
²⁸ Both directions are stated: `time.hardwareClockInLocalTime = true|false`. `utc` used to emit
nothing and lean on the NixOS default, which reads the same in the generated file as a document
that never mentioned the clock — and leaves the answer to whatever a later `imports =` decides. A
value outside the enum refuses.
²⁹ Defaults to `true` when absent.
³⁰ Only `false` emits; `true` is silently the default.
³¹ Only `servers[0]` reaches the target; the rest are silently dropped.
³² archinstall uses systemd-timesyncd regardless.
³³ `chrony`/`openntpd` emitted; `timesyncd`/`auto` emit nothing.
³⁴ `systemd-timesyncd` refuses; `auto` silently means chrony.
³⁵ `/etc/locale.conf` written by a post-install script.
³⁶ Writes `/etc/locale.conf`, but **Debian reads `/etc/default/locale`** — the file lands in the
wrong place.
³⁷ Written, but musl ignores `/etc/locale.conf` in practice.
³⁸ The kickstart `%addon com_redhat_kdump` exists and is unused.
³⁹ AutoYaST has a `<kdump>` section; never emitted.
⁴⁰ **NixOS is the only applier of the nine that honours it.** `boot.crashDump.enable`
(`misc/crashdump.nix:20-56`) kexecs a second kernel on panic and leaves `/proc/vmcore` in rescue —
kdump, under a NixOS name — and `false` is now stated explicitly instead of being a silent no-op.
The refusal it replaces was wrong, but the cost is real and is warned loudly: the module adds a
`boot.kernelPatches` entry (CRASH_DUMP, DEBUG_INFO, PROC_VMCORE), so the kernel leaves the binary
cache and is **compiled from source** during `nixos-install`.
⁴¹ `ubuntu-report` / popcon untouched.
⁴² Emits popcon, but tests for the string `"on"` while the schema enum is `off`/`default` — so
`"default"` wrongly disables it.
⁴³ These distros ship no telemetry to switch off. Behaviourally identical to a drop, but nothing
is lost. Void never reads the field; Gentoo reads it and warns for anything but `off`.
⁴⁴ A native VAI answer-file variable: `hostname`, `timezone` (UTC when absent), `keymap`,
`libclocale`. These four plus `disk`, `bootpartitionsize`, `swapsize`, `xbpsrepository`, `pkgs`
and `end_action` are the whole of Void's declarative surface; everything else in this file marked
⚙ for Void is `lis-post.sh`.
⁴⁵ Gentoo writes the target's own files from the chroot stage: `/etc/hostname` **and**
`/etc/conf.d/hostname` (OpenRC prefers the first and falls back to the second); `/etc/timezone`
plus `/etc/localtime`, with an existence check first so an unknown zone `die`s instead of leaving
a dangling symlink; `/etc/conf.d/keymaps`; `/etc/locale.gen` + `locale-gen` + `eselect locale`.
⁴⁶ Void uncomments the locale in `/etc/default/libc-locales` and runs
`xbps-reconfigure -f glibc-locales` (`lis_common.py:706-715`) — the only family with a
`LOCALE_LIST_FILE`, because appending to Void's file would leave a duplicate the generator
ignores.
⁴⁷ Gentoo emits each extra locale twice, once from its own code (`lis2gentoo.py:646-654`) and once
from `system_commands`; both append to `/etc/locale.gen` and run `locale-gen`, so the duplication
is harmless.
⁴⁸ Used only as the fallback when `keymap.console` is absent, and warned when the two differ. Void
additionally warns that `rc.conf`'s `KEYMAP` names a single `loadkeys` map.
⁴⁹ **Void, UNKNOWN.** `sed -i 's|^#\?FONT=.*|FONT="…"|' /etc/rc.conf` (`lis2void.py:638-639`)
replaces a line and appends nothing, so it takes effect only if the shipped `/etc/rc.conf` carries
a `FONT=` line, commented or not. Output *is* produced; the stock file's contents were not
checked, and it is not resolved by guessing. The same pattern is used for `hwclock`, which is safe
regardless because `/etc/adjtime` is written unconditionally alongside it.
⁵⁰ Gentoo: `/etc/conf.d/consolefont` plus `rc-update add consolefont boot`. With NixOS and Void
these are the only columns where `keymap.font` reaches anything.
⁵¹ **Void and Gentoo obey §2.3, as NixOS does.** Void accepts `runit`/`auto` and refuses
everything else (`lis2void.py:673-674`). Gentoo accepts `openrc`, `systemd` and `auto`, and the
value picks *both* the stage3 flavour and the profile suffix (`lis2gentoo.py:94-97,962,973`) —
they have to agree or the machine boots to nothing — while `runit` and `s6` refuse.
⁵² `LSM_PACKAGES` gives Void `None` for both modules (`lis_common.py:728,733`), so `apparmor` and
`selinux` both refuse; `auto`/`none` are no-ops. Void does package apparmor, but it needs
`security=apparmor apparmor=1` on the command line and a policy set before the LSM does anything,
so installing the package alone would look honoured and enforce nothing.
⁵³ Gentoo installs `sys-apps/apparmor` and nothing else — no `lsm=` parameter, no profile load, no
relabel, exactly as on Debian (footnote 20). `selinux` refuses, correctly: on Gentoo it is a
profile subtree plus a policy rebuild of `@world`, not a package.
⁵⁴ Gentoo builds the FQDN into `/etc/hosts` (`lis2gentoo.py:677-680`), joining Debian and NixOS.
Void warns: VAI writes a short hostname only.
⁵⁵ Void writes `HARDWARECLOCK` into `/etc/rc.conf`, which is what runit-void reads at boot, as
well as `/etc/adjtime`; Gentoo writes `/etc/conf.d/hwclock` as well as `/etc/adjtime`.
⁵⁶ Void installs and enables chrony or openntpd, appends **every** declared server to its
configuration file, and on `ntp: false` removes both runsvdir links. With NixOS it is the only
applier that honours `system.time.servers[]`. `systemd-timesyncd` refuses.
⁵⁷ Gentoo reads `time.provider` **only when `time.ntp` is true** (`lis2gentoo.py:1104-1113`), so a
document that omits `ntp` gets the provider warned-unread; `ntp: false` emits nothing at all and
the stage3's default governs. `systemd-timesyncd` refuses under OpenRC and is a silent no-op under
systemd. The server list is warned and discarded.
⁵⁸ Gentoo writes both `/etc/env.d/02locale`, which an OpenRC system reads, and `/etc/locale.conf`,
which a systemd one does — the only applier that covers both.
⁵⁹ Gentoo refuses a truthy `kdump`; `kdump: false` is a silent no-op. Void never reads the field.
NixOS no longer belongs in this note — it implements the field (footnote 40).
⁶⁰ The font name alone was **not enough**: `console.packages` defaults to `[ ]`
(`config/console.nix:109-111`), so `kbd` only ever saw the fonts it ships itself, and Terminus is
not one of them — `ter-v16n` is SPEC §8's own example, and it produced a boot that logged "cannot
open font file" and kept the default. `console.packages = [ pkgs.terminus_font ]` is now emitted
alongside `console.font` whenever the name starts with `ter-`. Verified: `nix-build -A
terminus_font` ships `share/consolefonts/ter-v16n.psf.gz`.
⁶¹ Same substance as footnote 43 — a plain NixOS closure collects and reports nothing, so there is
no opt-out to emit — but the decision is now **recorded in the generated file** as a comment
instead of being left to the unread-field tracker, which lets the next reader tell "nothing to do"
from "nobody looked". A value outside SPEC §8's enum refuses.

## 2.8 users

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `users[].name` | ✅¹ | ✅ | ✅² | ✅ | ✅³ | ✅ | ✅⁴ | ✅ | ✅ |
| `users[].uid` | ⚙⁵ | ⚙⁵ | ✅⁶ | ⚙⁵ | ⚙⁵ | ✅ | ⚙⁷ | ◐⁵⁸ | ✅⁵⁹ |
| `users[].comment` | ✅ | ◐⁸ | ✅⁹ | ◐¹⁰ | ⚙¹¹ | ✅ | ◐¹² | ◐⁵⁸ | ◐⁵⁸ |
| `users[].admin` | ◐¹³ | ✅ | ✅⁹ | ◐¹⁴ | ✅¹⁵ | ✅ | ◐¹⁶ | ◐⁶⁰ | ◐⁶⁰ |
| `users[].shell` | ◐¹⁷ | ⚙¹⁸ | ◐¹⁹ | ◐²⁰ | ⚙²¹ | ✅²² | ⚙²³ | ◐⁶¹ | ◐⁶¹ |
| `users[].groups[]` | ⚙²⁴ | ✅ | ✅⁹ | ◐²⁵ | ✅¹⁵ | ✅²⁶ | ⚙ | ◐⁵⁸ | ◐⁵⁸ |
| `users[].password.hash` | ✅ | ✅ | ✅ | ✅²⁷ | ✅ | ✅ | ◐²⁸ | ✅⁶² | ✅⁶² |
| `users[].password.locked` | ✅ | ◐²⁹ | ✅ | ✅ | ◐³⁰ | ✅³¹ | ◐²⁸ | ✅⁶² | ✅⁶² |
| `users[].ssh_authorized_keys[]` | ◐³² | ⚙ | ✅⁹ | ◐³³ | ⚙³⁴ | ✅ | ◐³⁵ | ✅⁶³ | ✅⁶³ |
| `users[].dotfiles.repo` | ⚙³⁶ | ⚙ | ⚙³⁷ | ⚙ | ⚙³⁷ | ⚙³⁸ | ⚙ | ⚙ | ⚙ |
| `users[].dotfiles.method` | ❌³⁹ | ◐³⁹ | ◐³⁹ | ◐³⁹ | ◐³⁹ | ⚙⁶⁷ | ❌³⁹ | ◐³⁹ | ◐³⁹ |
| `users[].sudo` | ⚙⁴⁰ | ⚙ | ⚙ | ⚙ | ⚙ | ✅⁴¹ | ◐⁴² | ⚙ | ⚙ |
| `users[].scripts.post[].interpreter` | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ |
| `users[].scripts.post[].chroot` | ❌⁴⁴ | ❌⁴⁴ | ❌⁴⁴ | ❌⁴⁴ | ◐⁴⁴ | ◐⁶⁸ | ❌⁴⁴ | ❌⁴⁴ | ❌⁴⁴ |
| `users[].scripts.post[].content` | ❌⁴⁵ | ❌⁴⁵ | ❌⁴⁵ | ❌⁴⁵ | ❌⁴⁵ | ⚙⁶⁹ | ❌⁴⁵ | ❌⁴⁵ | ⚙⁶⁴ |
| `users[].scripts.post[].source.from` | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ |
| `users[].scripts.post[].on_failure` | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ |
| `users[].scripts.post_install[].interpreter` | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ |
| `users[].scripts.post_install[].chroot` | ❌⁴⁸ | ❌⁴⁸ | ❌⁴⁸ | ❌⁴⁸ | ◐⁴⁸ | ◐⁶⁸ | ◐⁴⁸ | ❌⁴⁸ | ❌⁴⁸ |
| `users[].scripts.post_install[].content` | ⚙⁴⁹ | ⚙⁴⁹ | ✅⁵⁰ | ⚙⁴⁹ | ⚙⁵¹ | ⚙⁵² | ⚙⁵³ | ⚙⁶⁵ | ⚙⁶⁵ |
| `users[].scripts.post_install[].source.from` | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ |
| `users[].scripts.post_install[].on_failure` | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ |
| `users[].scripts.firstboot[].interpreter` | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ | ❌⁴³ |
| `users[].scripts.firstboot[].chroot` | ❌⁴⁸ | ❌⁴⁸ | ❌⁴⁸ | ❌⁴⁸ | ◐⁴⁸ | ◐⁶⁸ | ◐⁴⁸ | ❌⁴⁸ | ❌⁴⁸ |
| `users[].scripts.firstboot[].content` | ⚙⁵⁴ | ⚙⁵⁵ | ⚙⁵⁵ | ⚙⁵⁶ | ⚙⁵⁵ | ⚙⁵⁵ | ⚙⁵⁷ | ⚙⁶⁶ | ⚙⁶⁶ |
| `users[].scripts.firstboot[].source.from` | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ | ⛔⁴⁶ |
| `users[].scripts.firstboot[].on_failure` | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ | ❌⁴⁷ |

¹ **A `users[]` entry named `root` is filtered out entirely** (`lis2autoinstall.py:576`) whenever
another user exists — every field on it is dropped, SILENTLY. If `root` is the only user, the
document refuses (rule C-15).
² `name: "root"` routes to `rootpw`; most other fields on that entry are skipped (footnote 9).
³ `name: "root"` only sets `root_enc_password`; everything else on that entry is silently
dropped.
⁴ The first non-root user is the "primary" and is treated specially.
⁵ `usermod -u` + `chown` in a post-install script.
⁶ Native `--uid`, plus the `usermod` fallback.
⁷ `sed` on `/etc/passwd` (busybox has no `usermod`).
⁸ First non-root user only; `useradd` for later users has no `-c`. SILENT for users 2..n.
⁹ Skipped for a `root` entry (`lis2kickstart.py:404-405`), each warned.
¹⁰ Agama drops it for the third and subsequent users (its `useradd` has no `-c`).
¹¹ `usermod -c`; SILENTLY dropped for the root entry.
¹² The primary user's `chfn` does not exist on Alpine and is `|| true`'d — silently lost.
¹³ The primary user is **always** admin; `admin: false` is warned only. Secondary users get sudo
correctly.
¹⁴ AutoYaST only; the Agama primary user gets no `wheel`.
¹⁵ Dropped for the root entry.
¹⁶ Only the first normal user gets `-a`; a non-primary `admin` is dropped SILENTLY, while `-g
wheel` is forced anyway.
¹⁷ `chsh` is emitted but the shell **package is never installed** (`shell_packages` is not
imported).
¹⁸ `apt-get install <shell>` + `chsh`.
¹⁹ For root, only the package is installed — no `chsh`.
²⁰ AutoYaST `<shell>` only; the Agama profile has no shell field.
²¹ `chsh` + package; `chsh` SILENTLY skipped for root.
²² Resolved to `pkgs.<shell>`; an unknown name or a non-store path refuses.
²³ `sed` on passwd / `adduser -s`, plus the package.
²⁴ Primary user's groups go through a late-command `usermod -aG`.
²⁵ The Agama primary user's groups are dropped.
²⁶ The `!= "root"` guard is gone: `users.users.root.extraGroups` is emitted like any other
account's. The option's own documentation says it "can also be used to set options for root"
(`config/users-groups.nix:702-719`); verified by reading `config.users.users.root.extraGroups`
back out of a full evaluation.
²⁷ A user with neither a hash nor `locked` refuses.
²⁸ `lis-post.sh` calls `usermod`, **which does not exist on Alpine**, under `set -e`. Any root
password hash or second user aborts the script there — taking files, sudoers, uid fixups, hooks
and the birth certificate with it (rule C-29).
²⁹ Ignored for `name: root` when a hash is present — the root account ends up unlocked. SILENT.
³⁰ For root **with** a hash, the lock is silently ignored and the account is left unlocked.
³¹ The hash is **kept** and prefixed: `hashedPassword = "!" + hash`, through the shared
`lis_common.password_field()`. NixOS copies the value verbatim into `/etc/shadow`
(`config/users-groups.nix:344`), which is exactly what `passwd -l` writes, so `passwd -u` can undo
it — a locked account whose password survives, rather than one whose hash was thrown away. NixOS's
hash-shape heuristic (`users-groups.nix:1180-1206`) prints a *warning* about the `!` prefix, not an
assertion; confirmed by forcing `config.warnings`, and a generated comment says so.
³² Only `users[0]`'s keys use the native `authorized-keys`; other users get a late-command.
³³ **Agama output only, and only `root`'s `keys[0]`.** Non-root keys and every key after the
first are dropped SILENTLY; the AutoYaST output gets none at all.
³⁴ Appended to `authorized_keys`; SILENTLY dropped for root.
³⁵ Only `keys[0]` of the primary user (`USERSSHKEY`). Root's and everyone else's are dropped
SILENTLY.
³⁶ For secondary users this `git clone` runs **before** their `useradd`.
³⁷ For a root entry this clones into `/home/root`.
³⁸ The dead `pass # honored by chroot_intents()` — a helper this applier never imported — is
replaced by real work in the `lis-firstboot` unit, run as the account through `lis_as_user`:
`raw` clones to `$HOME/.dotfiles`, `stow` restows each package directory, `chezmoi` runs
`init --apply`. `pkgs.git`/`pkgs.stow`/`pkgs.chezmoi` are added to that unit's `path` as needed —
a systemd unit inherits no PATH, so an unlisted tool is a plain "command not found" in the journal
and a home directory that never appears. Idempotent (`test -e … ||`), and warned as emulated.
³⁹ Only `raw` works. `stow` and `chezmoi` are warned (Ubuntu: not even warned) and the repo is
cloned raw regardless.
⁴⁰ Emitted before the secondary `useradd` that needs it.
⁴¹ **Security fix, not just a coverage one.** `nopasswd` used to become the *global*
`security.sudo.wheelNeedsPassword = false`, which granted passwordless root to **every** wheel
member — including `admin: true` accounts that never asked for it and any account added later. It
is now a per-user `security.sudo.extraRules` entry (`{ users = ["u"]; commands = [{ command =
"ALL"; options = ["NOPASSWD"]; }]; }`, submodule shape at `security/sudo.nix:90-205`). Ordering
proven rather than assumed: evaluating `config.security.sudo.configFile` shows the generated rule
landing **after** the built-in `%wheel` rule (`mkOrder 600`, `sudo.nix:252-264`), and sudoers takes
the last match.
⁴² Alpine installs `doas`, not `sudo`; the `/etc/sudoers.d` drop-in is inert.
⁴³ The body always runs under the installer's `/bin/sh`.
⁴⁴ The whole user `post` phase is unimplemented, so the flag is moot; Arch warns on `false`.
⁴⁵ **No applier except Gentoo implements the user-level `post` phase** (footnote 64). Use
`post_install`. (`post` is also undocumented in `schema.md` §9.)
⁴⁶ External script bodies are never fetched by any applier (`lis_common.py:1294`). Inline the body
as `content`.
⁴⁷ `on_failure` is honoured by no applier for any phase; the installer's own failure policy
governs.
⁴⁸ Always chrooted regardless of the value; `false` is warned (Arch, Alpine) or read and discarded
in silence (Ubuntu, `lis_common.py:1299`).
⁴⁹ `su - <user> -c` in a late-command / chroot stage.
⁵⁰ Native `%post` running `su - <user> -c`.
⁵¹ archinstall `custom_commands`; SILENTLY dropped for root.
⁵² `system.activationScripts` + `setpriv` — **re-runs on every activation**, not only at install.
The body is now single-quoted with `shlex.quote` instead of `json.dumps`: as a *double*-quoted
shell word, `$HOME`, `$USER` and every `$(…)` in a per-user hook were expanded by the **root**
shell before `setpriv` dropped privilege, so a hook writing to `"$HOME/.config"` wrote into
`/root`. `setpriv` already exports HOME, USER and LOGNAME for the account. The activation script
is also ordered `deps = [ "users" "etc" ]`, so hooks can no longer run before `/etc` exists.
⁵³ `chroot su - <user> -c`; the host shell expands `$()` before the chroot (json.dumps quoting).
⁵⁴ cloud-init `runcmd su - <user>`.
⁵⁵ `lis-firstboot.service` oneshot with a done-marker.
⁵⁶ AutoYaST `<init-scripts>` / Agama `scripts.init`.
⁵⁷ OpenRC `lis-firstboot` service, base64-encoded (so quoting is safe here).
⁵⁸ **Void and Gentoo: a `users[]` entry named `root` gets only its password hash and its ssh
keys.** `uid`, `comment`, `shell`, `groups` and `admin` on that entry are skipped in silence
(`lis2void.py:403-413`, `lis2gentoo.py:712-713`). Unlike Ubuntu (rule C-27) the rest of the
document is unaffected, and unlike Fedora and Arch nothing warns.
⁵⁹ Gentoo passes `useradd -u` at creation **and** runs `uid_commands` over every account including
`root`, so root's uid is corrected afterwards. Void never calls `uid_commands` at all, so a `uid`
on the root entry is lost.
⁶⁰ The account joins `wheel` (and on Gentoo pulls `app-admin/sudo` into `@lis`), **but neither
applier writes a `%wheel` sudoers rule**. `admin: true` on its own therefore grants group
membership and nothing more; add `sudo: nopasswd` if the account has to escalate unattended
(rule C-49).
⁶¹ `useradd -s` plus the shell's own package (`shell_packages`; Gentoo maps the name through its
atom table). Dropped for a `root` entry — footnote 58.
⁶² `usermod -p` with the crypt(3) string from the document, verbatim; `password_field` prefixes
`!` when `locked` is set. **An account with neither a hash nor `locked` refuses on both**
(`lis2void.py:434-435`, `lis2gentoo.py:731-734`) — rule C-50. Void replaces VAI's own `add_user`
step for exactly this reason: it takes a plaintext password, which SPEC §2.4 forbids.
⁶³ Every key of every user, root included. The only two columns of the nine with no per-user and
no per-index loss.
⁶⁴ **Gentoo is the only applier that implements the user-level `post` phase**:
`su - <user> -c <content> || die` in the chroot stage (`lis2gentoo.py:815-819`), ordered *after*
`post_install`.
⁶⁵ Void: `su - <user> -c` inside `lis-post.sh`, which `end_function` chroots into at VAI step 16.
Gentoo: the same in `lis-chroot.sh`, where a failing hook `die`s the whole install rather than
being swallowed.
⁶⁶ Void: a runit core-service at `/etc/runit/core-services/99-lis-firstboot.sh`, base64-encoded
and guarded by a done-marker — Void's own documented place for one-time system tasks, sourced by
`/etc/runit/1` after the root filesystem is read-write. Gentoo: an OpenRC init script or a systemd
oneshot that deregisters itself on first run.
⁶⁷ All three schema methods are implemented on NixOS (footnote 38); an unknown method **refuses**
rather than silently falling back to `raw`.
⁶⁸ **NixOS answers `chroot` per stage** rather than dropping it. `users[].scripts.post` and
`.post_install` run in `system.activationScripts`, which `nixos-install` executes inside the
target, and `.firstboot` runs in the booted machine — so `chroot: true` is honoured on all three
and `chroot: false` **refuses**, naming the stage. See scripts footnote 58 for the mechanism; the
flag can never *move* a stage on this applier, so a value that contradicts the stage's real side is
the only thing it can meaningfully say.
⁶⁹ The user-level `post` phase is now collected — into the activation script, immediately after the
`post_install` bodies, per SPEC §13's ordering. NixOS joins Gentoo as the second applier to
implement it (footnote 45). Same `$HOME`-quoting and re-runs-on-activation caveats as footnote 52.

## 2.9 network

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `network.manager` | ◐¹ | ◐² | ◐³ | ◐⁴ | ◐⁵ | ✅⁶ | ◐⁷ | ◐⁴⁷ | ◐⁴⁸ |
| `network.interfaces[].match.name` | ◐⁸ | ⛔⁹ | ❌¹⁰ | ❌¹¹ | ❌¹² | ✅¹³ | ⛔¹⁴ | ⛔⁴⁹ | ⛔⁴⁹ |
| `network.interfaces[].match.mac` | ❌¹⁵ | ⛔⁹ | ❌¹⁰ | ❌¹¹ | ❌¹² | ✅⁵⁸ | ⛔¹⁴ | ⛔⁴⁹ | ⛔⁴⁹ |
| `network.interfaces[].dhcp4` | ✅¹⁶ | ⛔⁹ | ❌¹⁷ | ❌¹¹ | ❌¹² | ◐⁵⁹ | ⛔¹⁸ | ⛔⁴⁹ | ⛔⁴⁹ |
| `network.interfaces[].dhcp6` | ❌¹⁹ | ⛔⁹ | ❌¹⁰ | ❌¹¹ | ❌¹² | ◐⁵⁹ | ⛔¹⁴ | ⛔⁴⁹ | ⛔⁴⁹ |
| `network.interfaces[].addresses[]` | ✅ | ⛔⁹ | ❌²⁰ | ❌²⁰ | ❌²⁰ | ✅⁶⁰ | ⛔¹⁴ | ⛔⁴⁹ | ⛔⁴⁹ |
| `network.interfaces[].gateway` | ✅²¹ | ⛔⁹ | ❌ | ❌ | ❌ | ✅⁶¹ | ⛔¹⁴ | ⛔⁴⁹ | ⛔⁴⁹ |
| `network.interfaces[].dns[]` | ❌²² | ⛔⁹ | ❌ | ❌ | ❌ | ◐⁶² | ⛔²³ | ⛔⁴⁹ | ⛔⁴⁹ |
| `network.wifi[].ssid` | ⛔²⁴ | ⛔²⁵ | ⛔²⁵ | ⛔²⁵ | ⛔²⁵ | ✅²⁶ | ⛔²⁷ | ⛔⁵⁰ | ⛔⁵⁰ |
| `network.wifi[].psk_hash` | ⛔²⁴ | ⛔²⁵ | ⛔²⁵ | ⛔²⁵ | ⛔²⁵ | ◐⁶³ | ⛔²⁷ | ⛔⁵⁰ | ⛔⁵⁰ |
| `network.wifi[].hidden` | ⛔²⁴ | ⛔²⁵ | ⛔²⁵ | ⛔²⁵ | ⛔²⁵ | ✅²⁶ | ⛔²⁷ | ⛔⁵⁰ | ⛔⁵⁰ |
| `network.firewall.enabled` | ❌²⁸ | ❌²⁸ | ◐²⁹ | ❌²⁸ | ❌²⁸ | ✅³⁰ | ❌³¹ | ❌⁵¹ | ❌⁵¹ |
| `network.firewall.allow_services[]` | ⚙³² | ⚙³³ | ✅³⁴ | ⚙³⁵ | ⚙³² | ⛔⁶⁴ | ❌³¹ | ❌⁵¹ | ❌⁵¹ |
| `network.firewall.allow_ports[]` | ⚙³² | ⚙³³ | ✅³⁴ | ⚙³⁵ | ⚙³⁷ | ✅³⁸ | ❌³¹ | ❌⁵¹ | ❌⁵¹ |
| `network.ssh.enabled` | ✅ | ◐³⁹ | ◐³⁹ | ❌ | ❌⁴⁰ | ✅⁶⁵ | ✅ | ✅⁵² | ✅⁵² |
| `network.ssh.password_auth` | ✅ | ⚙⁴¹ | ❌ | ❌ | ❌ | ✅⁴² | ❌ | ◐⁵³ | ❌⁵⁵ |
| `network.ssh.permit_root` | ⚙⁴³ | ⚙⁴¹ | ◐⁴⁴ | ❌ | ❌ | ✅⁴² | ❌ | ✅⁵⁴ | ❌⁵⁵ |
| `network.hosts[].ip` | ❌⁴⁵ | ❌⁴⁶ | ❌ | ❌ | ❌ | ✅ | ❌ | ✅⁵⁶ | ❌⁵⁷ |
| `network.hosts[].names[]` | ❌⁴⁵ | ❌⁴⁶ | ❌ | ❌ | ❌ | ✅ | ❌ | ✅⁵⁶ | ❌⁵⁷ |

¹ Refuses `systemd-networkd`/`iwd` — **but only when `network.interfaces` is absent** (the check
sits inside `if not interfaces`). With interfaces declared, the same value is accepted and the
package installed (rule C-14).
² NetworkManager / systemd-networkd installed post-install; `iwd` refuses.
³ `auto`/`networkmanager` only; others refuse — yet `systemd-networkd` also emits `systemctl
enable systemd-networkd`, so `--lenient` gives a half-configured result.
⁴ `networkmanager`/`auto` only; `systemd-networkd` and `iwd` refuse.
⁵ `systemd-networkd` copies the ISO's config with no `.network` units; `iwd` refuses.
⁶ `auto` means NetworkManager, even for a `server` role. All four schema values map:
`networkmanager`/`auto` → `networking.networkmanager.enable`, `systemd-networkd` →
`networking.useNetworkd`, `iwd` → `networking.wireless.iwd.enable`. The value also **decides the
wireless back-end**, because `networkmanager.nix:551` asserts that `networking.wireless` and
NetworkManager cannot both drive the radio (footnote 26).
⁷ Non-`auto`/`networkmanager` refuse; NetworkManager is installed on top of
`/etc/network/interfaces`.
⁸ Becomes the netplan `ethernets` key — **a glob such as `en*` is emitted literally and matches
nothing**.
⁹ Any `interfaces[]` entry refuses: this preseed emits DHCP only.
¹⁰ No per-interface configuration is emitted; `network --bootproto=dhcp` is hardcoded.
¹¹ AutoYaST `<interfaces>` is never emitted; the installer's own DHCP default applies.
¹² archinstall `network_config.nics` is unused.
¹³ **No longer refused as a section.** `networking.interfaces."<name>"`
(`tasks/network-interfaces.nix:216`). The block is emitted *even when NetworkManager is enabled*,
and every statically-configured interface is added to `networking.networkmanager.unmanaged`
(`networkmanager.nix:211`) so NM cannot start a second, contradictory DHCP on the interface the
document pinned. An entry whose `match` names neither a name nor a MAC refuses — there is nothing
to key the option on.
¹⁴ Any `interfaces[]` entry refuses.
¹⁵ Dropped when `match.name` is also present; **refused** when it is the only selector
(`lis2autoinstall.py:898`).
¹⁶ Forced to `false` when `addresses` are present.
¹⁷ Silently becomes DHCP.
¹⁸ The answerfile hardcodes `eth0 dhcp`.
¹⁹ netplan supports `dhcp6` directly; the applier does not emit it.
²⁰ Static addressing silently becomes DHCP / nothing.
²¹ Emitted as a default route.
²² **Reads the wrong key**: `lis2autoinstall.py:908` looks for `iface["nameservers"]`, which no
LIS document can contain. Effectively unreachable.
²³ `DNSOPTS` is hardcoded to `1.1.1.1`.
²⁴ Refused as "not expressible in Ubuntu Server" — though netplan `wifis:` does exist.
²⁵ Refused.
²⁶ **No longer refused.** Under NetworkManager the network becomes a
`networking.networkmanager.ensureProfiles.profiles` keyfile (`networkmanager.nix:427`); under
`systemd-networkd` it becomes `networking.wireless.networks.<ssid>` with `pskRaw`, `hidden` and
`auth` (`wpa_supplicant.nix:250,307,406`). Which back-end is not a preference but a consequence of
`network.manager` (footnote 6). **`iwd` refuses**: `services/networking/iwd.nix:36` exposes only
`settings` for `main.conf` and has no declarative network list.
²⁷ Refused; `WIFIOPTS` exists in the answerfile and is unused.
²⁸ **Worse than dropped.** `lis_common.py:1118` tests the truthiness of the `firewall` *object*,
not the flag — a document explicitly setting `enabled: false` still installs and enables
ufw/firewalld. Void and Gentoo are exempt: they take the warn-and-skip branch instead
(footnote 51).
²⁹ `--disabled` is emitted, and then `%post` installs and enables firewalld anyway
(`lis_common.py:1122-1124`).
³⁰ The only applier that gets this right: both `true` and `false` reach
`networking.firewall.enable`. It also no longer emits an allow-list next to `enable = false`,
which read as though the ports were open; the list is skipped and **warned inert** (the NixOS
variant of rule X4).
³¹ The Alpine branch of the firewall helper warns and skips: nothing installed, nothing enabled,
no rules.
³² `ufw allow` in a post-install script (the `firewall-cmd` branch is dead on these distros).
³³ `firewall-cmd || ufw allow || true` — failures are swallowed.
³⁴ Native `firewall --service=` / `--port=`, plus `%post firewall-cmd`.
³⁵ `firewall-cmd --permanent` with no `--reload`; effective only after boot.
³⁶ *Retired.* Described NixOS reading `allow_services[]` and emitting nothing; it now refuses
(footnote 64). No column references this note.
³⁷ ufw is installed and "enabled" but left inactive (`ENABLED=no` never flipped).
³⁸ **Rule C-17/C-35 is closed on this column.** A port *range* used to be pasted in as though it
were an integer — `[ 80 8000-8010 ]`, a confirmed `nix-instantiate --parse` error raised under
`--apply` only after disko had already wiped the disks. Ranges now go to
`networking.firewall.allowed{TCP,UDP}PortRanges` as `[ { from = 8000; to = 8010; } ]`, single
ports to `allowed{TCP,UDP}Ports`.
³⁹ `true` installs/enables sshd; `false` emits nothing at all.
⁴⁰ openssh is not installed and sshd is not enabled either way.
⁴¹ Appended to `sshd_config` — and **dropped entirely when `ssh.enabled` is false**.
⁴² Native `services.openssh.settings.*`; unread and warned when `ssh.enabled` is false.
⁴³ Appended to `sshd_config`, not replaced — an existing directive may win.
⁴⁴ `rootpw --allow-ssh`; `permit_root: "no"` is unenforced, and the field needs a root user with a
hash to do anything.
⁴⁵ A specific warning fires only when `interfaces` is absent; `/etc/hosts` is never written either
way.
⁴⁶ `/etc/hosts` gets only the hostname line.
⁴⁷ Void: `auto`, `networkmanager` and `connman` are installed and linked into the runsvdir;
`systemd-networkd` and `iwd` refuse, because Void ships no runit service for either
(`lis2void.py:503,511-513`).
⁴⁸ **Gentoo installs the manager and then never enables it.** `NETWORK_MANAGERS` hands Gentoo the
*atom* `net-misc/networkmanager` (`lis_common.py:1148-1149`), and `chroot_intents` (`:1194-1196`)
feeds that straight to the enable verb, emitting `rc-update add net-misc/networkmanager` — which
is a package name, not a service name — swallowed by `|| true`. Reproduced in the generated chroot
script under both inits. `systemd-networkd` refuses under OpenRC, correctly; `iwd` refuses.
⁴⁹ Refused as a section on both (SPEC §2.3). Neither generates static addressing, and Gentoo says
what it does instead: `rc-update add dhcpcd default` / `systemctl enable dhcpcd.service`, because
a stage3 ships no DHCP client at all.
⁵⁰ Refused on both.
⁵¹ Neither applier configures a firewall: `chroot_intents` warns and skips
(`lis_common.py:1203-1210`). Nothing installed, nothing enabled, no rules — but also, unlike rule
C-6, nothing enabled that the document did not ask for.
⁵² Void installs openssh and links the sshd service — guarded, so a missing `/etc/sv/sshd` aborts
the post script rather than passing silently — and `enabled: false` removes the link. Gentoo puts
`net-misc/openssh` in `@lis` and runs `rc-update add sshd` / `systemctl enable sshd.service`.
⁵³ Void writes `PasswordAuthentication no` only for an explicit `false`; `true` relies on sshd's
own default. Dropped entirely when `ssh.enabled` is falsey (rule C-5).
⁵⁴ Appended to `sshd_config`, not replaced — an existing directive may win. Dropped when
`ssh.enabled` is falsey.
⁵⁵ Gentoo warns for every `network.ssh` key except `enabled` (`lis2gentoo.py:1151-1153`) and emits
nothing for them.
⁵⁶ Void writes every entry into the target's `/etc/hosts` — with NixOS, the only two columns that
do.
⁵⁷ **Gentoo, SILENT.** `lis2gentoo.py:1149-1150` calls `consume(entry)` on each host entry, which
marks both leaves read so the tracker stays quiet, and emits nothing. `/etc/hosts` gets only the
`127.0.1.1 <fqdn> <hostname>` line from `system.hostname`. Verified: no diagnostic of any kind
(rule C-48).
⁵⁸ A MAC-only match is honoured through `systemd.network.links."10-<name>"`
(`matchConfig.MACAddress` + `linkConfig.Name`): `networking.interfaces` is keyed by *name*, so the
device is first **renamed** — to `lis<N>` when the document gives no name, which is warned. `.link`
files are honoured by udev whether or not systemd-networkd is running, which `networkd.nix:3289`
states outright, so this works under NetworkManager too.
⁵⁹ PARTIAL by NixOS's shape, not by omission: `networking.interfaces.<n>.useDHCP`
(`network-interfaces.nix:216`) is **one flag for both address families**. `dhcp4` and `dhcp6` that
agree are emitted; a document that sets them to *different* values **refuses** rather than
silently picking one. Declaring static addresses with neither flag set implies `useDHCP = false`,
because leaving it at the default lets dhcpcd overwrite the addresses the document pinned.
⁶⁰ Split by family into `ipv4.addresses` / `ipv6.addresses` as `{ address; prefixLength; }`
(`network-interfaces.nix:228,246`, `addrOpts` `:69-91`). A value that is not `<address>/<prefix>`
refuses — the option takes the address and the prefix length separately, so there is nothing to
guess.
⁶¹ `networking.defaultGateway` / `defaultGateway6` as `{ address; interface; }`
(`network-interfaces.nix:655,668`). NixOS has exactly one of each, so a **second** gateway of the
same family refuses, naming the interface that already claimed it.
⁶² Emitted, but as system-wide `networking.nameservers`: NixOS resolves through one list and has
no per-interface form, so several interfaces' `dns[]` are merged — and warned when more than one
interface supplies them.
⁶³ The PSK reaches the machine, but **`--apply` only**, and deliberately. A WPA PSK is a
credential in its own right — the PMK *is* the network — so it is never written into
`configuration.nix`, which lands world-readable in the Nix store. Both back-ends read it from
`/var/lib/lis/wireless.env` (mode 0600), staged at apply time; the generated files carry only the
variable name. A translate-only run therefore produces a configuration that references a file that
does not exist yet. The same value is redacted from the birth certificate, per `delivery.md:144`.
⁶⁴ **NixOS refuses the service-name form.** `networking.firewall.allowed*Ports` takes numbers
only and NixOS ships no service-name → port table, so the list cannot be honoured; naming ports
numerically in `allow_ports[]` is the way through.
⁶⁵ `true` and `false` both reach `services.openssh.enable`. `false` is now *stated* rather than
left to the default, so a role or a later module turning sshd on loses to it instead of silently
winning — and `password_auth`/`permit_root` are warned moot in that case. This is also the switch
`software.services.enable: ["sshd"]` reaches, arbitrated through one collector so that asking in
both places is one attribute and asking for **opposite** values refuses (software footnote 46).

## 2.10 software

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `software.role` | ◐¹ | ◐² | ◐³ | ◐⁴ | ◐⁵ | ◐⁶ | ◐⁷ | ◐³³ | ◐³⁴ |
| `software.apps[]` (string form) | ✅⁸ | ✅⁸ | ✅⁸ | ✅⁸ | ✅⁸ | ✅⁹ | ✅⁸ | ✅⁸ | ✅³⁵ |
| `software.apps[].name` | ◐¹⁰ | ◐¹⁰ | ✅¹⁰ | ✅¹⁰ | ◐¹⁰ | ◐¹⁰ | ◐¹⁰ | ◐¹⁰ | ◐¹⁰ |
| `software.apps[].package` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `software.apps[].flatpak` | ⚙¹¹ | ⚙¹¹ | ⚙¹¹ | ◐¹² | ◐¹² | ⚙⁴³ | ❌ | ❌⁴² | ❌³⁶ |
| `software.apps[].snap` | ❌¹³ | ❌¹³ | ❌¹³ | ❌¹³ | ❌¹³ | ⛔⁴⁴ | ❌¹³ | ❌¹³ | ❌³⁶ |
| `software.apps[].appimage` | ❌¹⁴ | ❌¹⁴ | ❌¹⁴ | ❌¹⁴ | ❌¹⁴ | ⛔⁴⁴ | ❌¹⁴ | ❌¹⁴ | ❌³⁶ |
| `software.apps[].preference[]` | ❌¹⁵ | ❌¹⁵ | ❌¹⁵ | ❌¹⁵ | ❌¹⁵ | ✅⁴⁵ | ❌¹⁵ | ❌¹⁵ | ❌³⁶ |
| `software.packages[]` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅¹⁶ | ✅ | ✅³⁷ | ✅³⁷ |
| `software.services.enable[]` | ❌¹⁷ | ⚙¹⁸ | ✅¹⁹ | ❌²⁰ | ✅ | ◐²¹ | ⚙²² | ⚙³⁸ | ⚙³⁸ |
| `software.services.disable[]` | ❌¹⁷ | ⚙¹⁸ | ✅¹⁹ | ❌²⁰ | ❌²³ | ✅⁴⁶ | ⚙²⁴ | ⚙³⁹ | ⚙³⁹ |
| `software.flatpak[]` | ⚙²⁵ | ⚙²⁵ | ⚙²⁵ | ⚙²⁵ | ❌²⁶ | ⚙²⁷ | ⛔ | ⛔⁴⁰ | ◐⁴⁰ |
| `software.exclude[]` | ⚙²⁸ | ⚙²⁸ | ✅²⁹ | ⚙²⁸ | ⚙²⁸ | ◐³⁰ | ⚙²⁸ | ⚙²⁸ | ⚙²⁸ |
| `software.snap[].name` | ✅³¹ | ⛔³² | ⛔³² | ⛔³² | ⛔³² | ⛔³² | ⛔³² | ⛔⁴¹ | ⛔⁴¹ |
| `software.snap[].channel` | ✅ | ⛔³² | ⛔³² | ⛔³² | ⛔³² | ⛔³² | ⛔³² | ⛔⁴¹ | ⛔⁴¹ |
| `software.snap[].classic` | ✅ | ⛔³² | ⛔³² | ⛔³² | ⛔³² | ⛔³² | ⛔³² | ⛔⁴¹ | ⛔⁴¹ |

¹ `desktop:gnome|kde|xfce` map to meta-packages; `desktop:hyprland|sway` refuse; **`minimal` and
`server` are dropped SILENTLY** — `role_packages` holds only `desktop:*`.
² tasksel task; `desktop:sway|hyprland` refuse.
³ `minimal`/`server`/gnome/kde/xfce map; `hyprland`/`sway` refuse.
⁴ Pattern table; `hyprland`/`sway` refuse.
⁵ **SILENT failure**: the applier emits `"Gnome"` where archinstall expects `GNOME`, so
`desktop:gnome` produces no desktop at all.
⁶ `minimal` is now real: the five `documentation.*` switches, `programs.command-not-found.enable =
false` and `environment.defaultPackages = lib.mkForce [ ]`, copied from NixOS's **own**
`nixos/modules/profiles/minimal.nix` rather than invented (the profile is not imported, because a
generated `configuration.nix` has no stable `<nixpkgs/nixos/modules/…>` path once it is evaluated
from a flake). The five `desktop:*` roles map to their session modules. **`server` still emits
nothing** — NixOS genuinely ships no server metapackage and its base closure is already headless —
but it now says so instead of being silent, which is why this cell stays PARTIAL. An unknown role
refuses.
⁷ `gnome`/`plasma`/`xfce4` metapackages live in the `community` repository, which the applier does
not enable; `sway`/`hyprland` refuse.
⁸ Treated as a native package name for the distro's package manager.
⁹ Interpolated unquoted into `environment.systemPackages`; an odd name breaks evaluation.
¹⁰ Used only when `package` is absent — i.e. a human-facing display name is handed to the package
manager.
¹¹ `flatpak install` in a post-install/firstboot script, but **the `flathub` remote is never
added**, so the install fails.
¹² Only the `flatpak` package itself is installed; the app ID is SILENTLY discarded.
¹³ The per-app `snap` field is read by nothing on any distro, Ubuntu included.
¹⁴ AppImages are never fetched by any applier.
¹⁵ Source arbitration is hardcoded (native-first) everywhere. Note also that `schema.md` §2.3's
`"preference": true` producer flag has no schema representation at all.
¹⁶ Unresolvable names break Nix evaluation rather than failing gracefully.
¹⁷ No emitter anywhere, even though the applier emits `systemctl enable` for other purposes.
¹⁸ In-target `systemctl enable`/`disable`.
¹⁹ Native kickstart `services --enabled=`/`--disabled=`.
²⁰ Never read; no `systemctl` line is emitted for these.
²¹ A thirteen-name table now maps unit names to the option that owns them — including **`sshd`**,
which was a silent no-op (`services.openssh.enable`), plus `ssh`, `tailscaled`, `docker`, `podman`,
`libvirtd`, `fail2ban`, `avahi-daemon`, `cups`, `cronie`/`cron`, `bluetooth` and `nfs-server`.
Anything else still refuses, and now carries the reason: NixOS builds its unit set from modules, so
enabling a unit no module declares is a no-op, not an enablement.
²² `rc-update add`. **systemd unit names are not translated**, and the command is unguarded under
`set -e`.
²³ The channel exists (archinstall `services`) but no `systemctl disable` is emitted.
²⁴ `rc-update del || true`.
²⁵ Flatpak package installed and `flatpak install` scheduled, but no `remote-add flathub` — the
install fails at first boot.
²⁶ No flatpak install and no remote added.
²⁷ **`services.flatpak.enable` alone did not even evaluate**, let alone install anything:
`services/desktops/flatpak.nix` asserts `xdg.portal.enable`, and the portal module asserts a
non-empty `xdg.portal.extraPortals`, so every document using this field was an evaluation error
under `--apply` — after disko. Both are now emitted, and a first-boot unit (`lis-flatpak`) runs
`flatpak remote-add --if-not-exists flathub …` and then `flatpak install`, which closes the
missing-remote gap (audit X11): a flatpak app ID resolves against a *remote*, and NixOS configures
none. Warned as emulated, and as needing the network on first boot.
²⁸ `apt-get remove --purge` / `dnf remove` / `zypper rm` / `pacman -Rns` / `apk del` /
`xbps-remove -Ry` / `emerge --unmerge`, all with `|| true` so failures are swallowed.
²⁹ `-pkg` in `%packages` **plus** a `dnf remove`.
³⁰ The bare `pass` is gone (Tier 1 #6 closed). `perl`, `rsync` and `strace` are subtractable
through `environment.defaultPackages = lib.mkForce (with pkgs; [ … ])`; **anything else refuses**,
citing `config/system-path.nix`. That is the honest boundary: NixOS builds a closure rather than
installing into one, so a package cannot be removed after the fact, and the only subtractable set
is `defaultPackages`, which evaluates on 24.11 to exactly those three names. Anything else arrives
because a module pulls it in — turn the module off instead.
³¹ Native `snaps:` in autoinstall. (`lis_common.py:1096` also emits a second, malformed
`snap install {dict}` — harmless duplication, but it is there.)
³² snapd is not part of these distros; the whole section refuses.
³³ Void: `minimal` and `server` map to an empty package list and emit nothing; `desktop:gnome`,
`desktop:kde` and `desktop:xfce` map to the `gnome`, `kde5` and `xfce4` metapackages, checked
against the current repository index; `desktop:sway` and `desktop:hyprland` refuse.
³⁴ **Gentoo's role installs no desktop at all.** It selects a *profile* subtree
(`lis2gentoo.py:101-109`), which changes USE defaults tree-wide and nothing else — no metapackage
is ever added to `@lis`. `minimal` and `server` select the bare profile; `desktop:xfce`,
`desktop:sway` and `desktop:hyprland` all collapse to the same `/desktop` profile as one another.
Verified: `role: desktop:gnome` emits `eselect profile set …/23.0/desktop/gnome` and a package set
with no GNOME in it (rule C-44).
³⁵ Gentoo maps SPEC §11 intent names through its own atom table (`lis2gentoo.py:114-133`); a name
outside that table is passed through verbatim, so portage fails loudly rather than resolving
something else.
³⁶ **Gentoo, SILENT, four leaves at once.** `consume(app)` (`lis2gentoo.py:1092`) marks every leaf
of an `apps[]` object as read before only `package`/`name` are used, so `flatpak`, `snap`,
`appimage` and `preference` are discarded with no warning of any kind. Void at least warns for
`flatpak` (footnote 42). Reproduced: none of the four produces a diagnostic on Gentoo (rule C-48).
³⁷ Void: the `pkgs` VAI variable — `xbps-install -Sy -R $repo -r /mnt` installs them alongside
`base-system`. Gentoo: `/etc/portage/sets/lis`, installed with `emerge --getbinpkg @lis` and
recorded in `@world`, so the set survives into the installed system as a re-runnable statement of
intent.
³⁸ Void: a guarded runit symlink (`lis2void.py:381-389`) — a `.service` suffix is stripped, and a
name with no `/etc/sv` directory **aborts `lis-post.sh` under `set -e`**, which is loud rather
than silent. Gentoo: `rc-update add <unit> default` or `systemctl enable`, with the failure only
echoed to stderr. Neither translates systemd unit names into the target's own service names.
³⁹ Void removes the runsvdir symlink; Gentoo runs `rc-update del` / `systemctl disable`, both
`|| true`.
⁴⁰ **Void refuses the section**: it installs no flatpak runtime and adds no remote, so the app IDs
would reach nothing. Gentoo adds `sys-apps/flatpak` to `@lis` and neither the remote nor the apps
— the same shape as NixOS's ²⁷.
⁴¹ snapd is in neither distro; `chroot_intents` refuses the section (`lis_common.py:1173-1174`)
and Gentoo refuses it a second time by name.
⁴² Void warns once per app that "the native package is installed; flatpak/snap alternatives are
not" (`lis2void.py:727-729`). Because that test short-circuits on `flatpak`, `snap` is left unread
and picked up by the tracker instead — two channels, same outcome.
⁴³ The per-app `flatpak` field now feeds the same `lis-flatpak` first-boot unit as
`software.flatpak[]` (footnote 27), so an app declared only as a flatpak still arrives.
⁴⁴ **NixOS refuses these two rather than dropping them**, and only when they are the *sole*
surviving source for an app; when `preference[]` names an alternative this applier can provide, the
unusable source is skipped with a warning naming it (footnote 45). snapd needs a writable `/snap`
and FHS mount namespaces the store does not provide, and nixpkgs ships no snapd service module; an
AppImage is fetched from the network at install time, which contradicts the seed/offline model
every LIS applier installs under.
⁴⁵ **NixOS is the first applier to arbitrate on `preference[]` instead of hardcoding native-first.**
The list is walked in order; a source this applier cannot provide is skipped **with a warning** as
long as a later one works, and refuses only when nothing in the list survives. A declared source
the arbitration never reached is also named, so choosing between two sources is a documented
decision rather than a silent one. An unknown source name refuses against SPEC §11's vocabulary.
⁴⁶ A unit with a module option gets `= false`; one without gets
`systemd.suppressedSystemUnits = [ "<unit>.service" ]`, which removes the unit file from the
generated system — the only mechanism that works on a unit the configuration never declares
(`systemd.services.<n>.enable = false` would be the silent no-op). Both `enable[]` and `disable[]`
route through one collector with `network.ssh`, so the same option asked for twice is one attribute
and a contradiction **refuses** instead of emitting a duplicate — which in Nix is `attribute already
defined`, an evaluation failure raised after disko has wiped the disks.

## 2.11 drivers

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `drivers.gpu` | ◐¹ | ◐² | ◐³ | ◐⁴ | ◐⁵ | ✅⁶ | ◐⁷ | ◐¹¹ | ✅¹² |
| `drivers.microcode` | ✅ | ✅ | ◐⁸ | ✅ | ✅ | ✅ | ✅ | ◐¹¹ | ✅¹² |
| `drivers.firmware` | ◐⁹ | ◐⁹ | ◐⁹ | ◐⁹ | ◐⁹ | ✅¹⁰ | ✅⁹ | ◐⁹ | ◐¹³ |

¹ `nvidia*` sets `drivers.install: true`; `amdgpu`/`intel` add an xserver package. **Setting
`nvidia`/`nvidia-open` silently discards `system.security.module`** (rule C-13).
² Enables non-free repos; `nvidia-open` refuses.
³ `intel`/`amdgpu` map; `nvidia*` refuse (no RPM Fusion).
⁴ `amdgpu`/`intel` map; `nvidia` and `nvidia-open` refuse.
⁵ `amdgpu` sets `gfx_driver` to null SILENTLY; the `nvidia-open` string is invalid and archinstall
exits.
⁶ `amdgpu`/`intel` set `services.xserver.videoDrivers`; `nvidia`/`nvidia-open` set it to
`nvidia` plus `hardware.nvidia.open`, **and now emit `nixpkgs.config.allowUnfree = true`** —
without it the configuration stops with "Package 'nvidia-x11' has an unfree license", which
under `--apply` lands after disko has wiped the disks. `none`/`auto` are the no-ops they ask for;
an unmapped value refuses.
⁷ `amdgpu`/`intel` → `mesa-dri-gallium`; `nvidia*` refuse.
⁸ Both `intel` and `amd` map to the same `microcode_ctl` package.
⁹ Only `"all"` installs firmware; `auto` and `none` emit nothing.
¹⁰ `auto`/`none` keep `hardware.enableRedistributableFirmware`, which `hardware.nix` writes for
every document; **`"all"` now emits `hardware.enableAllFirmware`** (plus `allowUnfree`, which
`all-firmware.nix` asserts on — the non-redistributable blobs cannot even be evaluated without
it). The two files do not conflict: equal-valued booleans merge. Previously `"all"` was silently
treated as *redistributable* firmware — a strictly smaller set than the one it asks for.
¹¹ Void: `amdgpu`/`intel` → `mesa-dri`. `nvidia`, `nvidia-open` **and `microcode: intel`** refuse
— all three live in `void-repo-nonfree`, which `base-system` does not enable
(`lis_common.py:242-250`), so emitting the name would install nothing. `microcode: amd` →
`linux-firmware-amd`.
¹² **Gentoo is the only column where the GPU choice is a build-time knob rather than a package
name**: `VIDEO_CARDS` in `make.conf` (`lis2gentoo.py:206-213`) — which changes how the whole tree
is built — plus the X driver package, plus an `NVIDIA-r2` entry in `package.license` when the
document asks for nvidia. All four GPU values map, and `microcode: amd` resolves to
`sys-kernel/linux-firmware`, which is where AMD ships it.
¹³ Gentoo: only `"all"` installs `sys-kernel/linux-firmware`, together with the
`@BINARY-REDISTRIBUTABLE` entry in `package.license` that the generated
`ACCEPT_LICENSE="-* @FREE"` would otherwise block. `auto` and `none` emit nothing.

## 2.12 files

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `files[].path` | ⚙¹ | ⚙² | ⚙² | ⚙³ | ⚙² | ◐⁴ | ⚙³ | ⚙² | ⚙² |
| `files[].mode` | ⚙ | ⚙ | ⚙ | ⚙³ | ⚙ | ✅ | ⚙³ | ⚙ | ⚙ |
| `files[].owner` | ⚙ | ⚙ | ⚙ | ⚙³ | ⚙ | ✅ | ⚙³ | ⚙ | ⚙ |
| `files[].content` | ⚙⁵ | ⚙² | ⚙² | ⚙³ | ⚙² | ✅⁶ | ⚙³ | ⚙² | ⚙² |
| `files[].encoding` | ⚙⁷ | ⚙⁷ | ⚙⁷ | ⚙⁷ | ⚙⁷ | ✅ | ⚙⁷ | ⚙⁷ | ⚙⁷ |

¹ **cloud-init `write_files` — runs at first boot, after every late-command.** Ubuntu is the only
applier where files land later than the post-install hooks that might read them.
² `install -d` + `base64 -d` in the post-install script (`lis_common.py:655-663`) — on Void inside
`lis-post.sh`, on Gentoo inside `lis-chroot.sh`.
³ Same `file_commands` helper as ² — the SUSE and Alpine classifiers labelled this ✅ where the
others labelled it ⚙. The mechanism is identical (a chroot-stage script); ⚙ is used here for
consistency, and for the same reason on Void and Gentoo. Nothing behavioural turns on the
difference.
⁴ `/etc/*` becomes `environment.etc` natively. **A path outside `/etc` no longer refuses**: it is
written by the activation script with `install -D -m <mode>` and `printf %s <b64> | base64 -d`,
plus `chown`, and warned as emulated — hence PARTIAL, half native and half post-install. Content
is base64 in both directions, so shell metacharacters, newlines and real binary payloads cannot
change what is emitted; non-UTF-8 content under `/etc` takes the same route rather than crashing
the applier on `b64decode(...).decode()`. A duplicate path and a relative path both refuse. An
`owner` with no `mode` now defaults to `0644`, because `uid`/`gid` take effect only when the entry
is *copied* and `system/etc/etc.nix:49-52` skips them at the `"symlink"` default — the owner was
otherwise silently dropped.
⁵ `file_commands()` is not imported by the Ubuntu applier; content goes through cloud-init
instead.
⁶ **Rule C-36 is closed.** `nix_str` now escapes `${` → `\${` as well as `\` and `"`. Reproduced
before the fix: content of `prefix=${HOME}` made `environment.etc."…".text` fail with
`error: undefined variable 'HOME'` — a wipe-then-fail under `--apply`, and with a
document-controlled expression on the other side of it, an injection rather than merely a break.
Binary and non-UTF-8 content is also handled now, by routing it to the activation path
(footnote 4) instead of crashing on `b64decode(...).decode()`.
⁷ `base64` is passed through un-re-encoded; `plain` is encoded on the way in. Newline-safe. On
Debian this is the **only** content field exempt from the one-line preseed restriction (rule
C-11).

## 2.13 scripts

Nine phases × five leaves, on nine appliers. `schema.md` §13 describes seven hook points; `schema.json` exposes nine
independent keys (`pre` and `pre_install` are separate properties, as are `post` and
`post_install`), and every applier collapses them differently. **Cells marked ◐ for `content` mean
the body runs, but at a different point in the install than the phase name promises** — that is the
single most common script hazard.

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `scripts.pre[].interpreter` | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ |
| `scripts.pre[].chroot` | ❌² | ❌³ | ❌⁴ | ❌² | ◐⁵ | ◐⁵⁸ | ❌⁷ | ❌⁵⁰ | ❌⁵⁰ |
| `scripts.pre[].content` | ✅⁸ | ✅⁹ | ✅¹⁰ | ✅¹¹ | ⚙¹² | ◐¹³ | ◐¹⁴ | ✅⁴⁹ | ◐⁵¹ |
| `scripts.pre[].source.from` | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ |
| `scripts.pre[].on_failure` | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ |
| `scripts.pre_install[].interpreter` | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ |
| `scripts.pre_install[].chroot` | ❌² | ❌³ | ❌⁴ | ❌² | ◐⁵ | ◐⁵⁸ | ❌⁷ | ❌⁵⁰ | ❌⁵⁰ |
| `scripts.pre_install[].content` | ✅¹⁷ | ✅¹⁷ | ✅¹⁷ | ✅¹⁷ | ⚙¹² | ◐¹³ | ◐¹⁴ | ✅⁴⁹ | ◐⁵¹ |
| `scripts.pre_install[].source.from` | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ |
| `scripts.pre_install[].on_failure` | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ |
| `scripts.post_storage[].interpreter` | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ |
| `scripts.post_storage[].chroot` | ❌¹⁸ | ❌¹⁹ | ❌¹⁹ | ❌¹⁹ | ◐⁵ | ◐⁵⁸ | ◐¹⁹ | ❌⁵⁰ | ❌⁵⁰ |
| `scripts.post_storage[].content` | ◐²⁰ | ◐²⁰ | ◐²⁰ | ◐²⁰ | ⚙²¹ | ⚙²² | ◐²⁰ | ◐⁵² | ◐⁵² |
| `scripts.post_storage[].source.from` | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ |
| `scripts.post_storage[].on_failure` | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ |
| `scripts.post[].interpreter` | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ |
| `scripts.post[].chroot` | ✅²³ | ❌¹⁹ | ❌¹⁹ | ❌¹⁹ | ◐⁵ | ◐⁵⁸ | ◐¹⁹ | ❌⁵⁰ | ❌⁵⁰ |
| `scripts.post[].content` | ◐²⁴ | ✅²⁵ | ✅²⁶ | ✅²⁷ | ⚙²⁸ | ⚙²⁹ | ⚙³⁰ | ⚙⁵³ | ⚙⁵³ |
| `scripts.post[].source.from` | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ |
| `scripts.post[].on_failure` | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌³¹ | ❌¹⁶ | ❌¹⁶ |
| `scripts.post_install[].interpreter` | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ |
| `scripts.post_install[].chroot` | ✅²³ | ❌¹⁹ | ❌¹⁹ | ❌¹⁹ | ◐⁵ | ◐⁵⁸ | ◐¹⁹ | ❌⁵⁰ | ❌⁵⁰ |
| `scripts.post_install[].content` | ◐²⁴ | ✅³² | ✅³² | ✅³² | ⚙²⁸ | ⚙²⁹ | ⚙³⁰ | ⚙⁵³ | ⚙⁵³ |
| `scripts.post_install[].source.from` | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ |
| `scripts.post_install[].on_failure` | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ |
| `scripts.pre_reboot[].interpreter` | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ |
| `scripts.pre_reboot[].chroot` | ✅²³ | ❌¹⁹ | ❌¹⁹ | ❌¹⁹ | ◐⁵ | ◐⁵⁸ | ◐¹⁹ | ❌⁵⁰ | ❌⁵⁰ |
| `scripts.pre_reboot[].content` | ◐³³ | ◐³⁴ | ◐³⁴ | ◐³⁴ | ⚙³⁵ | ⚙³⁶ | ◐³⁷ | ◐⁵³ | ◐⁵⁴ |
| `scripts.pre_reboot[].source.from` | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ |
| `scripts.pre_reboot[].on_failure` | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ |
| `scripts.on_success[].interpreter` | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ | ❌¹ |
| `scripts.on_success[].chroot` | ✅²³ | ❌¹⁹ | ❌¹⁹ | ❌¹⁹ | ◐⁵ | ◐⁵⁸ | ◐¹⁹ | ❌⁵⁰ | ❌⁵⁰ |
| `scripts.on_success[].content` | ◐³⁸ | ◐³⁸ | ◐³⁸ | ◐³⁸ | ⚙³⁸ | ⚙⁵⁹ | ◐³⁸ | ◐³⁸ | ◐⁵⁴ |
| `scripts.on_success[].source.from` | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ |
| `scripts.on_success[].on_failure` | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ |
| `scripts.on_error[].interpreter` | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ❌¹ | ❌⁴⁰ | ⛔⁵⁶ | ❌¹ |
| `scripts.on_error[].chroot` | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ◐⁵⁸ | ❌⁴⁰ | ⛔⁵⁶ | ❌⁵⁰ |
| `scripts.on_error[].content` | ⛔⁴¹ | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ⚙⁶⁰ | ⛔³⁹ | ⛔⁵⁶ | ✅⁵⁵ |
| `scripts.on_error[].source.from` | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ⛔¹⁵ | ⛔¹⁵ | ⛔⁵⁶ | ⛔¹⁵ |
| `scripts.on_error[].on_failure` | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ⛔³⁹ | ❌¹⁶ | ❌⁴⁰ | ⛔⁵⁶ | ❌¹⁶ |
| `scripts.firstboot[].interpreter` | ❌⁴² | ❌⁴² | ❌⁴² | ❌⁴³ | ❌⁴² | ❌⁴² | ❌⁴² | ❌⁴² | ❌⁴² |
| `scripts.firstboot[].chroot` | ❌⁴⁴ | ❌⁴⁴ | ❌⁴⁴ | ❌⁴⁴ | ◐⁴⁴ | ◐⁵⁸ | ◐⁴⁴ | ❌⁴⁴ | ❌⁴⁴ |
| `scripts.firstboot[].content` | ⚙⁴⁵ | ⚙⁴⁶ | ⚙⁴⁶ | ✅⁴⁷ | ⚙⁴⁶ | ⚙⁴⁶ | ⚙⁴⁸ | ⚙⁵⁷ | ⚙⁵⁷ |
| `scripts.firstboot[].source.from` | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ | ⛔¹⁵ |
| `scripts.firstboot[].on_failure` | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ |

¹ No applier honours `interpreter` for any phase; the body runs under the stage's own shell
(`lis_common.py:1291` warns).
² Read and discarded; the stage is never chrooted.
³ Always host context — **the warning text states the opposite**.
⁴ `%pre` is not chrooted; the warning text is inverted.
⁵ Arch is the outlier: `lis_common.py:1299-1305` is shared, but the Arch classifier calls this
PARTIAL because `chroot: true` is implicitly satisfied (everything runs as a chroot
`custom_command`) while `chroot: false` cannot be honoured and is warned. Other classifiers call
the identical code a DROP. **Same helper, different labelling** — nothing behavioural distinguishes
them except which value happens to be the accidental default.
⁶ *Retired.* Recorded NixOS's inverted `chroot` warning on `pre`/`pre_install`; superseded by
footnote 58, which answers the flag per stage. No column references this note — but the class of
bug is still live on the columns footnotes 3, 4 and 50 describe.
⁷ SILENT: the stage is a host `subprocess` (`lis2alpine.py:741`); `chroot: true` is ignored with
no diagnostic.
⁸ subiquity `early-commands` — conflated with `pre_install` in the same bucket, unwarned.
⁹ `preseed/early_command`, on the installer host.
¹⁰ Native `%pre`, host-side, before partitioning. The only applier where `pre` genuinely means
"before storage".
¹¹ `scripts.pre` / `<pre-scripts>`; ordered *after* the `pre_install` bodies.
¹² **Re-homed to after `pacstrap`** — this is not a pre-partition hook (warned).
¹³ `--apply` only, executed on the installer host; a translate-only run emits nothing.
¹⁴ `--apply` only, on the installer host before `setup-alpine`; translate-only emits nothing with
no warning.
¹⁵ `source.from` is fetched by no applier. Inline the body as `content`. Note also that
`schema.md` §13 lists `https://` sources, which the schema pattern itself rejects.
¹⁶ `on_failure` reaches nothing anywhere; the installer's own policy governs. Gentoo comes closest
without honouring it: a failing `post*` hook `die`s the whole chroot stage regardless of the
declared policy, and a failing `pre_reboot`/`on_success` hook is discarded regardless.
¹⁷ Same bucket as `scripts.pre`; the two phases are merged with no warning about the collapse.
Ordering between them differs per applier (Debian and Fedora emit `pre_install` first; SUSE emits
`pre_install` before `pre`).
¹⁸ Always host context.
¹⁹ Always in-target.
²⁰ **Not a post-partition hook.** Re-homed into the general post-install batch, long after
storage. Fedora and Ubuntu warn; the others do not.
²¹ Runs after packages, not after formatting.
²² **No longer collapsed into the activation script.** SPEC §13.2 places `post_storage` in the
live installer environment right after the target is formatted and mounted, and no NixOS option
can describe that state — so it is not emitted into the configuration at all. `--apply` runs it on
the installer host at the correct point instead: after `disko`, with the target mounted at `/mnt`
(disko's default root, not the `/target` the spec's example names), before `nixos-install`. A
translate-only run emits nothing and warns with that exact contract. Audit X7, closed.
²³ Honoured: `in-target` vs `sh -c` is chosen from the flag (`lis2autoinstall.py:752-755`). Ubuntu
is the only applier that acts on `chroot` at all.
²⁴ Runs, but shares one late-command bucket with `post_install`, `pre_reboot` and `on_success` —
no ordering guarantee beyond emission order, and no warning about the merge.
²⁵ `preseed/late_command`, in-target.
²⁶ Native `%post --erroronfail`, chrooted.
²⁷ `chroot-scripts`, inside the target.
²⁸ archinstall `custom_commands`, in-target after the bootloader.
²⁹ `activationScripts` — **re-runs on every system activation**, not only at install. Ordered
`deps = [ "users" "etc" ]` so a hook can no longer run before `/etc` exists (`etc` is itself
`stringAfter [ "users" "groups" ]`, so this is an ordering, not a cycle), and `files[]` commands
are placed ahead of the script hooks.
³⁰ `chroot "$target" sh -c`; the **host** shell expands `$()` and backticks first (json.dumps
quoting).
³¹ Ignored, and `set -e` aborts the whole post script regardless.
³² Same bucket as `scripts.post`; runs first.
³³ The "after unmount" contract is not preserved.
³⁴ Merged into the general post batch; there is no distinct pre-reboot stage.
³⁵ Runs in the chroot before unmount, not after.
³⁶ Now run on the installer host after `nixos-install` returns and before the machine is
rebooted, rather than being folded into the activation script (footnote 22 for the mechanism and
the translate-only warning).
³⁷ No reboot is issued at all; runs inline with `post`.
³⁸ **Runs unconditionally.** Success is never tested on these columns, Void and Gentoo included.
A generator should treat `on_success` as an alias for `post` and warn. NixOS is now the exception
(footnote 59).
³⁹ The whole `on_error` phase is refused — no equivalent exists in any of these formats. (Ubuntu's
autoinstall does have a native `error-commands`; it is unused.) Gentoo and NixOS are the
exceptions (footnotes 55 and 60).
⁴⁰ Alpine refuses the phase via `content` but merely warns on these leaves.
⁴¹ "no autoinstall equivalent".
⁴² The firstboot unit script is `/bin/sh`.
⁴³ AutoYaST `<init-scripts>` accept no interpreter.
⁴⁴ Always runs in the booted target; the flag is moot.
⁴⁵ cloud-init `runcmd`.
⁴⁶ `lis-firstboot.service` systemd oneshot, guarded by a done-marker.
⁴⁷ Native `scripts.init` / `<init-scripts>`.
⁴⁸ OpenRC `lis-firstboot` service, base64-encoded, self-deregistering.
⁴⁹ **Void's `pre` and `pre_install` bodies are emitted into the answer file itself**, which VAI
sources at step 2 — before it partitions anything (`lis2void.py:786-793`). With Fedora's `%pre`
this is the only genuine pre-storage hook of the nine. The two phases share one block, `pre`
first, with no warning about the collapse.
⁵⁰ Both call `check_script_fields(honors_chroot=False, chroots_by_default=True)`, so `chroot:
false` warns "this applier always runs the script inside the target". That is true for the `post*`
stages and **false for Void's `pre`/`pre_install` (they run in the installer's initramfs) and for
Gentoo's `pre`, `pre_install`, `pre_reboot`, `on_success` and `on_error` (they run on the
installer host)**. Same inverted-warning class as ³ and ⁴.
⁵¹ Gentoo runs these on the installer host with `subprocess` at `--apply` time only
(`lis2gentoo.py:1256-1259`), and it runs **`pre_install` before `pre`**. A translate-only run emits
nothing for either.
⁵² Re-homed on both: Void puts the body in `lis-post.sh`, after the whole install; Gentoo puts it
in `lis-chroot.sh`, after `emerge @lis`. Neither is a post-storage hook, and neither warns about
the move.
⁵³ Void: `lis-post.sh`, chrooted, in the fixed order `post_storage, post_install, post,
pre_reboot, on_success` — so `post_install` runs *before* `post`, with no warning about the merge,
and `pre_reboot` runs before the unmount rather than after it. Gentoo: `lis-chroot.sh`, same
`post_install`-before-`post` order, but each body is wrapped in `( … ) || die`, so a failing hook
aborts the install instead of being swallowed.
⁵⁴ Gentoo emits these into `lis-prepare.sh` **after `arch-chroot` returns and before `umount`** —
on the installer host, not in the target — each wrapped in `( … ) || true`, so a failure is
discarded. The "after unmount" contract of `pre_reboot` is not preserved, and `on_success` runs
unconditionally.
⁵⁵ **Gentoo was the first of the nine to implement `scripts.on_error`** (NixOS is the second,
footnote 60): the bodies
become a `bash` EXIT trap in `lis-prepare.sh` (`lis2gentoo.py:335-338`) that fires on any non-zero
exit from the whole install. It runs on the installer host, so ⁵⁰ applies.
⁵⁶ Void refuses on the *presence* of the `on_error` array (`lis2void.py:903-905`) — the VAI
installer aborts into the dracut emergency shell and there is nowhere to hook — so every leaf
under it refuses. The per-leaf `interpreter`/`on_failure` warnings from `check_script_fields` fire
first and are moot.
⁵⁷ Void: a runit core-service under `/etc/runit/core-services/`, base64-encoded and marker-guarded
(so quoting is safe and it runs once per machine). Gentoo: an OpenRC init script or a systemd
oneshot that removes itself on first run.
⁵⁸ **NixOS answers `chroot` per stage instead of dropping it.** This applier straddles the
boundary — `pre`, `pre_install`, `post_storage`, `pre_reboot`, `on_success` and `on_error` run on
the installer host, while `post`, `post_install` and `firstboot` run inside the target — so a
single applier-wide default cannot be right, and `check_script_fields` is called with
`honors_chroot=True` so a per-stage table can answer instead. A flag naming the side the stage is
*not* on **refuses**, quoting that stage's real contract; a flag naming the correct side is
honoured. PARTIAL rather than YES because the flag can never *move* a stage here — it can only
agree or contradict. This also repairs the inverted warning of footnote 6, which told `pre` and
`pre_install` the opposite of the truth.
⁵⁹ `--apply` only, on the installer host, and — unlike every other column — **actually conditional**:
the bodies run only when `nixos-install` exits zero. Footnote 38's "success is never tested" no
longer holds here.
⁶⁰ **NixOS is the second applier to implement `on_error`.** The refusal it replaces was wrong for
this column: on NixOS `--apply` *is* the installer, so a failed step does have somewhere to report
from. The bodies run on the installer host when either `disko` or `nixos-install` fails. A
translate-only run emits nothing and warns.

## 2.14 desktop

`schema.md` §12 says this whole section MUST be absent unless `software.role` is `desktop:*`. No
applier enforces that, and the schema cannot express it.

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `desktop.display_manager` | ◐¹ | ⚙² | ⚙³ | ⚙⁴ | ◐⁵ | ✅⁶ | ⚙⁷ | ⚙¹⁹ | ?²⁰ |
| `desktop.autologin` | ⛔⁸ | ⛔⁸ | ⚙⁹ | ❌¹⁰ | ❌¹⁰ | ◐¹¹ | ⛔⁸ | ❌²¹ | ⛔²² |
| `desktop.audio` | ❌¹² | ❌ | ◐¹³ | ❌ | ✅¹⁴ | ✅¹⁵ | ❌ | ❌²¹ | ◐²³ |
| `desktop.bluetooth` | ❌ | ✅¹⁶ | ◐¹⁷ | ◐¹⁷ | ❌¹⁸ | ✅ | ❌ | ❌²¹ | ◐²⁴ |
| `desktop.printing` | ❌ | ✅¹⁶ | ◐¹⁷ | ◐¹⁷ | ❌¹⁸ | ✅ | ❌ | ❌²¹ | ◐²⁴ |

¹ Only `gdm`/`auto` pass the check — and **`gdm` is not an Ubuntu package name** (`gdm3` is).
² Installed and enabled in-target; `none` refuses.
³ `%post dnf install` + enable; `none` refuses. The applier *also* warns "not applied by this
applier", which is wrong — a wizard must not treat that warning as authoritative.
⁴ Installed + enabled; `none` refuses; a bogus "not applied" warning also fires.
⁵ `none` refuses; `lightdm`/`greetd` combined with a desktop role make archinstall exit 1.
⁶ **The field is now read.** `gdm` → `services.xserver.displayManager.gdm.enable`, `sddm` →
`services.displayManager.sddm.enable`, `lightdm` →
`services.xserver.displayManager.lightdm.enable`, `ly` → `services.displayManager.ly.enable`,
`greetd` → `services.greetd.*`, and `none` emits no greeter at all (warned when a session is
installed with nothing to start it). `auto` resolves per role — gnome→gdm, kde→sddm, xfce→lightdm,
sway/hyprland→greetd — so the role and this field can no longer contradict each other. Note
`services.displayManager.gdm` does **not** exist on 24.11; the `services.xserver.` spelling is the
correct one and getting it wrong is an evaluation error, not a drop. A Wayland role with `sddm`
also emits `sddm.wayland.enable`, because `sddm.nix` asserts one of that and
`services.xserver.enable` and a Wayland role turns on neither.
⁷ `apk add` + `rc-update`; `none` refuses; also mis-warned "not applied".
⁸ Not expressible in this installer format.
⁹ **Always writes a GDM config** (`/etc/gdm/custom.conf`) regardless of `display_manager` —
verified emitting both `dnf install sddm` and a GDM autologin file.
¹⁰ No autologin drop-in is written.
¹¹ Now gated on a greeter actually being resolved. `greetd` uses its own
`settings.initial_session.{command,user}`, since it ignores `services.displayManager.autoLogin`
entirely; the others use `autoLogin.{enable,user}` plus `defaultSession`, named explicitly because
gdm and sddm otherwise pick the autologin session out of a list that is ambiguous the moment a
second one is installed. Three refusals rather than a broken emission: an account the document
does not declare (schema.md §12), no greeter resolved, and **`ly`** — whose module asserts
`!autoLogin.enable`, so the combination cannot be evaluated at all. PARTIAL because ly genuinely
has no autologin of its own.
¹² `pipewire`/`auto` emit nothing at all (SILENT); anything else refuses. No audio package is
added.
¹³ `auto`/`pipewire` emit nothing; `none` refuses.
¹⁴ `auto` is mapped to pipewire (an invention, not a schema default); `none` refuses.
¹⁵ All four values now reach the target. `pulseaudio` emits **`hardware.pulseaudio.enable`** —
`services.pulseaudio` does *not* exist on 24.11, the rename landed later, and naming it was an
evaluation error after disko — together with `services.pipewire.enable = lib.mkForce false`,
because every desktop role turns pipewire on and `pipewire.nix` asserts pulseaudio is off, so
`desktop.audio: pulseaudio` on any desktop role did not evaluate at all. `none` now forces both
off rather than emitting nothing, since a desktop module turns a sound server on by default.
`desktop.bluetooth`/`printing` set to `false` are likewise stated instead of skipped.
¹⁶ Package added, `true` only — `false` does not remove or disable anything.
¹⁷ Package only; the corresponding service is not enabled.
¹⁸ `app_config.bluetooth_config` / `print_service_config` exist in archinstall and are unused.
¹⁹ Void: `chroot_intents` installs the package and links it into the runsvdir — **unguarded
(`|| true`), unlike every other runit service this applier enables** (footnote 38 of §2.10), so a
display manager Void ships no `/etc/sv` directory for is a silent no-op. `ly` refuses by name (it
is not in the Void repositories, `lis2void.py:881-882`) and `none` refuses.
²⁰ **Gentoo, UNKNOWN.** The package is emerged and `rc-update add <name>` / `systemctl enable
<name>` is emitted with `|| true` (`lis_common.py:1184-1185`), but the OpenRC service name
Gentoo's display-manager packages actually install was not verified — Gentoo has historically
driven all of them through a single `xdm` service rather than one per manager — and the failure is
swallowed. Output *is* produced; the effect is unconfirmed, and it is not resolved by guessing.
Gentoo also warns "desktop.display_manager is not applied by this applier", because
`check_section_fields` omits the key (`lis2gentoo.py:1217-1218`); that warning is false either way.
²¹ Void never reads `autologin`, `audio`, `bluetooth` or `printing` — each is warned twice, once
by `check_section_fields` and once by the tracker.
²² Gentoo refuses `autologin` rather than dropping it — the fourth applier to do so.
²³ Gentoo: `pipewire` writes `media-video/pipewire sound-server` into `package.use`, **but
`media-video/pipewire` is never added to `@lis`** — the USE flag lands and no audio server is
installed. `auto` and `none` emit nothing; anything else refuses.
²⁴ Gentoo adds `net-wireless/bluez` / `net-print/cups` to `@lis`. No service is enabled, and
`false` removes nothing.

## 2.15 proxy

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `proxy.http` | ✅¹ | ✅² | ❌³ | ⚙⁴ | ⛔⁵ | ✅ | ✅⁶ | ◐⁹ | ✅¹⁰ |
| `proxy.https` | ❌⁷ | ❌ | ❌ | ⚙⁴ | ⛔⁵ | ✅⁸ | ❌⁶ | ◐⁹ | ✅¹⁰ |
| `proxy.no_proxy[]` | ❌⁷ | ❌ | ❌ | ⚙⁴ | ⛔⁵ | ✅ | ❌⁶ | ◐⁹ | ✅¹⁰ |

¹ autoinstall `proxy:` — installer environment only.
² `mirror/http/proxy`, carried into the target's apt configuration.
³ **The warning is a lie**: `lis2kickstart.py:577-579` emits a comment claiming the proxy is
"applied to the installer environment". It is not applied anywhere.
⁴ Written to `/etc/sysconfig/proxy` in the target. The installer's own downloads are not proxied.
⁵ The whole `proxy` section refuses on Arch.
⁶ `PROXYOPTS` takes exactly one URL, so only `http` survives.
⁷ Not persisted into the target.
⁸ `networking.proxy.httpsProxy` is now emitted. `proxy.http` deliberately stays on
`networking.proxy.default`, which `config/networking.nix:88-133` makes httpProxy, httpsProxy,
ftpProxy, rsyncProxy and allProxy all fall back to — so one `proxy.http` covers the protocols the
document did not mention instead of leaving them direct — and an explicit `httpsProxy` still wins
over it (`:245-247`).
⁹ Void exports `http_proxy`, `https_proxy` and `no_proxy` from the answer file
(`lis2void.py:776-781`), so VAI's own `xbps-install` and the chrooted post script it spawns are
proxied — **but nothing is written into the installed system**, which comes up with no proxy
configuration.
¹⁰ **Gentoo has the most complete proxy support of the nine**: all three keys go into
`/etc/portage/make.conf` (`lis2gentoo.py:221-230`), so they persist into the target and portage
uses them there as well as during the install. Note that `make.conf` is world-readable — a proxy
URL with embedded credentials becomes a plaintext secret on the installed disk. Any other
`proxy.*` key is warned.

## 2.16 mirror

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `mirror.url` | ✅¹ | ✅ | ✅² | ⚙³ | ⛔⁴ | ◐⁵ | ✅⁶ | ✅⁹ | ◐¹⁰ |
| `mirror.country` | ❌⁷ | ✅ | ❌⁸ | ❌ | ⛔⁴ | ❌ | ❌ | ❌¹¹ | ❌¹¹ |

¹ `apt.primary`, legacy form.
² `url --url=` — the actual Anaconda install source.
³ **Added as an extra repository** (`zypper ar --priority 50 lis-mirror`); it does not replace the
install source.
⁴ Refused, although `archinstall`'s `mirror_config.custom_servers` exists.
⁵ Becomes a Nix **binary cache substituter** — a different concept — and it *replaces*
`cache.nixos.org`.
⁶ `APKREPOSOPTS`.
⁷ `apt.mirror-selection` / geoip unused.
⁸ No mirrorlist country selection.
⁹ Void: the `xbpsrepository` VAI variable — the repository `xbps-install -Sy -R … -r /mnt` pulls
`base-system` and every declared package from.
¹⁰ Gentoo: `GENTOO_MIRRORS` in `make.conf`, which governs **source distfiles only**. The stage3
tarball and the binary host stay pinned to `distfiles.gentoo.org`
(`lis2gentoo.py:80-82`, `:237-241`), so a document that names a mirror still fetches the bulk of
the install from upstream.
¹¹ Warned on both: portage has no country-based mirror selection, and VAI's knob is a single URL.

## 2.17 registration

Spec §15: an applier for an unregistered distro MUST fail on this section.

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `registration.server` | ❌¹ | ⛔² | ◐³ | ❌⁴ | ⛔² | ⛔² | ⛔² | ⛔² | ⛔² |
| `registration.token.from` | ✅⁵ | ⛔² | ✅⁶ | ⚙⁷ | ⛔² | ⛔² | ⛔² | ⛔² | ⛔² |
| `registration.email` | ❌⁸ | ⛔² | ❌⁹ | ⚙¹⁰ | ⛔² | ⛔² | ⛔² | ⛔² | ⛔² |

¹ **SILENT**: formatted into the `pro attach` template as `org=…`, but the Ubuntu template has no
`{org}` placeholder. The value is consumed (so no tracker warning) and discarded.
² No subscription service on this distro; the whole section refuses, as §15 requires. On NixOS
the section's leaves are additionally passed to `check_unread`'s ignore set, so a refused
section is not *also* reported three times as intent nobody looked at — a refusal is a decision,
and listing it again under "never read" trains the reader to skim the channel the real drops
arrive on.
³ Used as the subscription **org id**, not as a URL.
⁴ Read into a template that has no `{org}` slot; silently discarded.
⁵ Seed path, never inlined into the output.
⁶ `%post TOKEN=$(cat <seed path>)`; a non-seed source refuses.
⁷ `TOKEN=$(cat …); SUSEConnect -r $TOKEN` in the chroot stage — the seed must be readable there.
⁸ SILENT, same missing-placeholder mechanism as ¹.
⁹ SILENT: the Fedora template has no `{email}` slot.
¹⁰ Appended as `-e <email>` to `SUSEConnect`.

## 2.18 installer

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `installer.on_finish` | ◐¹ | ◐² | ◐³ | ◐⁴ | ❌⁵ | ❌⁶ | ❌⁶ | ❌¹⁷ | ❌¹⁷ |
| `installer.on_error` | ❌⁷ | ❌⁷ | ❌⁸ | ❌⁷ | ❌⁷ | ❌⁷ | ❌⁷ | ❌⁷ | ❌⁷ |
| `installer.interactive[]` | ✅⁹ | ❌¹⁰ | ❌¹¹ | ❌¹¹ | ❌¹² | ❌ | ❌ | ❌¹¹ | ❌¹¹ |
| `installer.answers.<key>` | ❌¹³ | ❌¹³ | ❌¹³ | ❌¹⁴ | ❌¹³ | ❌¹³ | ❌¹⁵ | ❌¹³ | ❌¹³ |
| `installer.unattended` | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ | ❌¹⁶ |

¹ `reboot`/`poweroff` honoured; `stay` refuses; **absent means subiquity reboots** — the opposite
of `schema.md` §16's documented default of `stay`.
² `reboot`/`poweroff` only; absent defaults to reboot.
³ `reboot`/`poweroff` only; `stay` refuses; absent means reboot.
⁴ Default is reboot (md says `stay`); `stay` refuses; `poweroff` only suppresses the reboot —
and does so SILENTLY.
⁵ archinstall `--silent` behaviour governs.
⁶ `--apply` never reboots or powers off.
⁷ Not emitted; the installer's default error behaviour applies.
⁸ `%post --erroronfail` is hardcoded.
⁹ `interactive-sections` — passed through verbatim, and the schema puts no enum on the section
names, so a typo silently disables the interactivity you asked for.
¹⁰ A preseed is fully non-interactive by construction.
¹¹ The run is always fully unattended.
¹² Always `--silent`.
¹³ No raw answer injection.
¹⁴ AutoYaST `<ask-list>` is never emitted.
¹⁵ Not merged into the answerfile.
¹⁶ **No applier consults the consent flag**, Void and Gentoo included. `installer.unattended` is
the document half of the two-key destructive-run consent described in the spec; the second key is
not implemented anywhere. Do not rely on it to gate anything.
¹⁷ Neither new applier reads `on_finish`. **Void always powers the machine off** — `end_action=func`
neither unmounts nor powers off on its own, so `end_function` ends with `poweroff -f`
(`lis2void.py:834-836`), and a document asking for `reboot` or `stay` gets a power-off regardless.
Gentoo neither reboots nor powers off: `lis-prepare.sh` unmounts and returns.

## 2.19 x-* extensions

| Field | Ubuntu | Debian | Fedora | SUSE | Arch | NixOS | Alpine | Void | Gentoo |
|---|---|---|---|---|---|---|---|---|---|
| `x-<name>` | ⛔¹ | ⛔¹ | ⛔² | ⛔³ | ⛔⁴ | ⛔⁵ | ⛔¹ | ⛔¹ | ⛔¹ |

¹ `check_unhandled` (`lis_common.py:175-181`) refuses **any** `x-*` section — including the
applier's own namespace. This directly contradicts spec §2.2/§18, which says an applier MUST
ignore extensions it does not understand. Verified unchanged on Void and Gentoo: `x-void` refuses
under `lis2void.py` and `x-gentoo` under `lis2gentoo.py`.
² Non-empty `x-*` refuses; an empty object (`{}`) passes silently.
³ `x-suse.product` is read at `lis2agama.py:237` and `:610`, and the same document is then refused
by `check_unhandled`. Without it the two SUSE outputs disagree by default (Agama targets
Tumbleweed, AutoYaST targets Leap).
⁴ Even `x-arch.packages`, which the applier itself reads at `:880`, refuses. Works only with
`--lenient`.
⁵ Even `x-nixos.*` — **the repository's own examples (`server-btrfs.lis.json`,
`server-lvm-pool.lis.json`) are refused by the NixOS applier.**

---

# 3. COMBINATION RULES

A flat matrix cannot express support that depends on two or more fields being present together.
Each rule below is stated so a generator can implement it as a pre-flight check.

## 3.1 Cross-cutting (all appliers)

| # | Rule | Consequence |
|---|---|---|
| C-1 | If any `x-*` key is present → the document is **refused by all nine appliers**. | Never emit `x-*` unless the user has explicitly accepted `--lenient`. Includes the applier's own namespace. |
| C-2 | A `keys[]` entry does something only if `storage.encryption` exists **and** `purpose` contains `disk_encryption` **and** `type` ∈ {keyfile, gpg, age} (material) or {tpm2, fido2} (enrollment). | Otherwise the entry is a fully silent no-op — no warning, because iteration counts as a read. |
| C-3 | If a C-2-qualifying `keys[]` entry exists → `storage.encryption[].key.keyfile` and `key.passphrase` are **shadowed and unread** (`lis_common.py:846-856`). | Do not emit both. On Alpine the shadowing is broken and emits `--key-file None` (rule C-30). |
| C-4 | A disk with no `match.path` refuses on Debian, Fedora, SUSE, Arch, NixOS, Alpine and Gentoo in translate-only mode; the other `match.*` selectors are resolved only under `--apply`. | Ubuntu is the exception: it emits a real match spec, but only when `path` is **absent**. **Void is the other exception, in the opposite direction**: it has no `--apply` path at all, so `match.path` is mandatory and every other selector reaches nothing under any flag. |
| C-5 | `network.ssh.password_auth` / `permit_root` are dropped entirely when `network.ssh.enabled` is `false` (Debian; Fedora needs a root user with a hash). | Emit the sub-fields only alongside `enabled: true`. **NixOS no longer belongs here**: it emits both settings regardless and, when `enabled` is false, emits `services.openssh.enable = false` beside them and warns that they have no effect. |
| C-6 | Any `network.firewall` object at all installs and enables a firewall on Ubuntu, Debian, Fedora, SUSE and Arch — **even with `enabled: false`** (`lis_common.py:1118` tests the object, not the flag). | To leave the firewall alone, omit the whole `network.firewall` block. NixOS is the only applier that honours `false`. Alpine, Void and Gentoo warn and skip: nothing installed, nothing enabled, no rules. |
| C-7 | `desktop.*` on a non-`desktop:*` role is accepted by every applier despite `schema.md` §12. | Arch drops `profile_config` so only the chroot commands reach the target; NixOS applies the whole block on a server **and now warns, naming §12**. |
| C-8 | `storage.snapshots.enabled` never checks that the root filesystem is btrfs — where it is honoured at all. | Validate it yourself; §20.9 requires it and nothing enforces it. Alpine, Void and Gentoo refuse the whole block instead. |
| C-9 | `scripts.on_success` runs unconditionally on all nine; `scripts.post_storage` runs after the install, not after storage, on all but Fedora. | Treat both as aliases of `post` and warn the user. |
| C-49 | `users[].admin: true` grants membership of `wheel`/`sudo` and, on most appliers, nothing more — **on Void and Gentoo no `%wheel` sudoers rule is written at all**. | If the account has to escalate unattended, emit `sudo: nopasswd` as well. |
| C-50 | A `users[]` entry with neither `password.hash` nor `password.locked` refuses on SUSE, Void and Gentoo, and is accepted elsewhere. | Always emit one of the two. |

## 3.2 Storage-layer combinations

| # | Rule | Applies to |
|---|---|---|
| C-10 | Encryption **over a RAID array**: refused on Arch and Debian; refused via `check_encryption_emitted` on SUSE; **silently emits no LUKS at all** on Alpine. Works on Ubuntu, Fedora and NixOS (all verified). | If the target is Arch/Debian/SUSE/Alpine, do not stack `encryption.over` on a `raid[].name`. |
| C-11 | Arch: `encryption` + `lvm` requires **every** container to wrap a PV and a total of ≤ 2 partitions. | Otherwise refuse. |
| C-12 | Arch: with `storage.lvm` present, any partition whose `fs` is not esp/boot refuses — so a partition-level `role: swap` is incompatible with LVM. | Use an LVM swap volume instead. |
| C-13 | Arch: with `storage.raid` present, subvolumes on any partition refuse, array members may not carry a mountpoint, and some boot/ESP partition **must** carry one. | |
| C-14 | Debian: `encryption` + any plain swap partition → refuse. Encrypted root without a separate plain `/boot` → refuse. Encrypted `/boot` → refuse. Two crypto containers without LVM → refuse. Mixing the LVM and bare-crypto shapes → refuse. | |
| C-15 | Debian: more than one `storage.lvm` group **without** encryption emits `new_vg_name` for group 0 and `in_vg{vg1}` for the rest — a volume group with no physical volume. SILENT. | Emit at most one LVM group for Debian. |
| C-16 | Debian: multiple disks with neither `raid` nor `lvm` spanning them → refuse. | |
| C-17 | A `raid[]` array not consumed by `lvm.devices` or `encryption.over` refuses (`lis_common.py:733`) on Ubuntu, Debian, NixOS and Alpine. | Every declared array must be consumed. |
| C-18 | Fedora: `storage.partitions[].label` is **silently discarded** when that same partition declares `subvolumes`. | Do not emit both. |
| C-19 | Ubuntu: two partitions resolving to the same mountpoint (explicitly or via `role`) emit two curtin mount actions, SILENTLY — Ubuntu never calls `resolve_mountpoints`. | Deduplicate mountpoints yourself. §20 requires exactly one `/`. |
| C-20 | `storage.wipe: false` — refuses on Debian, Fedora, NixOS and Void; emits the invalid `wipe: preserve` on Ubuntu; still partitions from 1MiB on Arch, still mkfs's on Alpine, and on Gentoo keeps the partition table but still creates and formats every declared partition (warned). | There is no working "preserve existing layout" path on any of the nine. |
| C-21 | SUSE emits **two mutually exclusive profiles**. `storage.lvm.*`, `storage.raid.*`, `users[].shell`, `users[].groups`, `users[].admin` reach only `autoyast.xml`; `users[].ssh_authorized_keys` and `system.keymap.variant` reach only `profile.json`. | Choosing the wrong output silently loses those fields. The applier warns for lvm/raid but **not** for the user fields. |
| C-22 | Alpine "simple path": with no `subvolumes`, `raid`, `lvm` or `encryption`, the entire storage layout collapses to `DISKOPTS="-m sys <disk>"`. `target.firmware`, `boot.loader`, `boot.timeout` and every `partitions[]` leaf are read (so no tracker warning) and reach nothing. | For Alpine, either accept `setup-alpine`'s default layout or force the manual path by declaring one of those four features. |
| C-23 | Alpine: an ESP plus a separate `/boot` collapse — the first partition mounted at `/boot` *or* `/boot/efi` becomes the boot device and the other is formatted and abandoned. | Declare one or the other. |
| C-24 | NixOS: `role: root|boot|swap` with **no explicit `fs`** produces a disko partition with no content while `hardware.nix` still declares the filesystem. Zero warnings, unbootable result. | Always emit an explicit `fs` for NixOS. |
| C-41 | **Void: `storage` must be exactly VAI's own scheme, or the document refuses.** One disk with a `match.path`, `wipe: true`, and exactly three partitions in the order `[role: boot` at `/boot`, `role: swap`, `role: root` at `/` sized `rest]`. `storage.encryption`, `lvm`, `raid`, `swap` and `snapshots` are refused **as a group** by one loop (`lis2void.py:161-166`) — they are not five independent gaps but one: VAI's `sfdisk` heredoc is three lines and this applier writes no partition table of its own. 8 of the 9 bundled recipes refuse on Void. | Encryption is not a feature to work around here; there is no other storage shape. |
| C-42 | Void: `target.firmware` must be `bios`. `uefi` **and `auto`** refuse. | Void is the only one of the nine that cannot install a UEFI system. |
| C-43 | **Gentoo: `target.firmware: "auto"` is not UEFI.** Only the literal `"uefi"` selects a GPT label, the ESP flag, `sys-boot/efibootmgr` and `grub-install --target=x86_64-efi`; `auto` silently takes the BIOS branch (`lis2gentoo.py:319-320`). An absent key defaults to `uefi`, so `auto` is strictly worse than omitting the field. | Write `uefi` or `bios` explicitly for Gentoo; never `auto`. |
| C-45 | **Gentoo: a partition `size` in any unit but `MiB`/`GiB`/`TiB` silently becomes zero.** `cumulative()` ignores `NN%`, `MB`/`GB`/`TB`, `KiB` and bare byte counts, emitting `mkpart primary 1MiB 1MiB` and shifting every sibling after it. | Use `MiB`/`GiB`/`TiB` or `rest` for Gentoo. |
| C-46 | **Void: a partition `size` of `NN%` raises an uncaught `ValueError`** — the applier exits 1 with a Python traceback rather than a refusal, and writes nothing. | Same advice as C-45, different failure mode. |

## 3.3 Non-storage combinations

| # | Rule | Applies to |
|---|---|---|
| C-25 | Ubuntu: `drivers.gpu: nvidia`/`nvidia-open` **silently discards `system.security.module`** — `security_packages` is only reached in the `else` branch (`lis2autoinstall.py:674-680`). | Warn if both are set. |
| C-26 | Ubuntu: `network.manager` is validated **only when `network.interfaces` is absent**. With interfaces declared, `systemd-networkd`/`iwd` are accepted and installed. | The refusal is not reliable; check the value yourself. |
| C-27 | Ubuntu: a `users[]` entry named `root` has **every field dropped silently** if any other user exists, and the document **refuses** if root is the only user. | Never emit a root entry for Ubuntu. |
| C-28 | Ubuntu: `storage.encryption[].unlock: ["keyfile"]` rewrites the crypttab keyfile field to `none` with a code comment and no warning — the machine prompts at every boot. | Warn that keyfile unlock is not achievable on Ubuntu. |
| C-29 | Debian: **any** `scripts.*[].content` or `users[].scripts.*[].content` containing a newline refuses (the preseed must be one directive per line). `files[].content` is exempt (base64). | Emit single-line script bodies for Debian, or move the payload into `files[]` and invoke it. |
| C-30 | Alpine: a container with no `key` object plus a valid `keys[]` keyfile emits `cryptsetup luksFormat … --key-file None`. Silent, fatal at apply time. | Always emit an explicit `storage.encryption[].key` for Alpine. |
| C-31 | Alpine: any root password hash, or any second user, makes `lis-post.sh` call `usermod` — which does not exist on Alpine — under `set -e`, aborting files, sudoers, uid fixups, hooks and the birth certificate. | Emit exactly one non-root user with no root hash for Alpine. |
| C-32 | Alpine: `users[]` beyond the first non-root account lose `admin`; every `ssh_authorized_keys` entry after `[0]` and all of root's keys are dropped with no warning (`declared_paths` collapses list indices, so reading them for user 0 marks them read for all). | The same index-collapsing masks multi-user drops on several appliers. |
| C-33 | Arch: `boot.loader: auto` + `target.firmware: bios` emits systemd-boot anyway. | Set `boot.loader: grub` explicitly for BIOS Arch. |
| C-34 | Arch: a `desktop:*` role combined with `display_manager` `lightdm`/`greetd`/`none` makes archinstall exit 1. | |
| C-35 | **CLOSED.** Both halves are fixed: ranges become `allowed*PortRanges` `{from;to;}` and `realtime` maps to `linuxPackages-rt`. Kept as a record of the failure mode, which is the one that matters on this applier — under `--apply`, evaluation happens *after* disko has wiped the disks, so a bad option name destroys data and then fails. | Five further instances of the same class were found and closed at the same time: grub+UEFI with no `devices` (failed assertion), `services.flatpak.enable` without `xdg.portal.*`, `desktop.audio: pulseaudio` on a desktop role, `sddm` on a Wayland role, and `nix_script` escaping `''` as `''''`. A plain `nix-instantiate` on the generated file passes all of them; only forcing `config.assertions` and `system.build.toplevel.drvPath` catches them. |
| C-36 | **CLOSED.** `nix_str` now escapes `${`, and `files[].path` outside `/etc` is written by the activation script instead of refusing. Hook bodies were *not* in fact safe: `nix_script` escaped a literal `''` as `''''`, which Nix reads back as three quotes — `x=''` became `x='''`, an unterminated shell string, and `a='''b` was an outright evaluation error. Now `'''`, verified by round-tripping 13 bodies through `nix eval --raw`. | |
| C-37 | **CLOSED on all three.** `boot.os_prober` under systemd-boot now refuses instead of being ignored (and `loader: auto` resolves to grub when the document asks for it); `system.keymap.variant` is emitted independently of `layout`; `users[].groups[]` applies to `root` like any other account. | |
| C-38 | Fedora: `desktop.autologin` writes a **GDM** config regardless of `desktop.display_manager` — verified emitting both `sddm` and `/etc/gdm/custom.conf`. | Autologin only actually works with GDM on Fedora. |
| C-39 | Fedora: `storage.raid[].spares[]` emits `--spares=N` but never adds the spare device to the member list — a RAID1 of 2 + 1 spare becomes a 2-member array with one active mirror. | |
| C-40 | Size values: `NN%` refuses on Ubuntu, Debian, Fedora and SUSE; **silently means `rest` on Arch** (two percent siblings overlap); **computes 0MiB on Alpine and on Gentoo** (C-45); **crashes Void with a traceback** (C-46); only NixOS handles it. | Emit absolute sizes or `rest`. On Gentoo the unit matters too — only `MiB`/`GiB`/`TiB` are parsed. |
| C-44 | **Gentoo: `software.role: desktop:*` installs no desktop.** The role only selects a portage profile subtree, which changes USE defaults; no metapackage is ever added to `@lis`. `desktop:xfce`, `desktop:sway` and `desktop:hyprland` are indistinguishable from one another. | Name the desktop metapackage in `software.packages[]` yourself, and set `desktop.display_manager`. |
| C-47 | **Gentoo: `boot.initramfs.include_modules[]` is warned as applied and is not.** `dracut_conf` builds `force_drivers` from a hardcoded device list plus the root filesystem and never reads the field. | A root filesystem or storage controller outside that hardcoded list will not be in the initramfs. Put the driver in `boot.kernel.modules[]` too — that one does reach `/etc/modules-load.d`, though it is too late for the initramfs. |
| C-48 | **Gentoo: `consume()` hides three groups of drops from the tracker** — `keys[].type`/`.purpose[]`, `network.hosts[]`, and every `software.apps[]` key except `name`/`package`. Marking a leaf read is what suppresses the "declared but never read" warning, so these are silent in both channels. | The access tracker cannot see these. Check them at generation time. |

---

# 4. KNOWN SILENT DROPS — the fix list

Ranked by how likely a real document is to contain the field. "Silent" in the *Diag* column means
no warning of any kind is printed; "warned" means a `warning:` line appears but the run still
succeeds. **Warnings never fail a run, not even under `--strict`.**

## Tier 1 — common fields, silent, produce a wrong machine

| # | Field / condition | Distros | Diag | What actually happens |
|---|---|---|---|---|
| 1 | Whole partition layout when no subvolumes/raid/lvm/encryption | Alpine | silent | Layout, firmware and bootloader all collapse to `setup-alpine`'s default (C-22). |
| 2 | `storage.partitions[].role` without `fs` | NixOS | silent | Partition gets no content; `fileSystems` still declared. Unbootable (C-24). |
| 3 | `keys[].source.from` with no explicit container key | Alpine | silent | `--key-file None`; install dies at LUKS format (C-30). |
| 4 | `storage.encryption[].over` naming a RAID array | Alpine | silent | No `luksFormat` emitted; `pvcreate` on a mapper node nothing opened (C-10). |
| 5 | Any `users[]` entry named `root` | Ubuntu | silent | Entire entry discarded when another user exists (C-27). |
| 6 | `system.security.module` with `drivers.gpu: nvidia*` | Ubuntu | silent | LSM package never installed (C-25). |
| 7 | `storage.encryption[].unlock: ["keyfile"]` | Ubuntu | silent | Crypttab rewritten to prompt at every boot (C-28). |
| 8 | `software.role: desktop:gnome` | Arch | silent | Emits `"Gnome"`, archinstall expects `GNOME` — no desktop installed. |
| 9 | ~~`software.exclude[]`~~ | ~~NixOS~~ | **FIXED** | The bare `pass` is gone: `perl`/`rsync`/`strace` subtract from `environment.defaultPackages`, anything else refuses (§2.10 fn 30). |
| 10 | `storage.raid[].spares[]` | NixOS | **silent** | Spares silently become active members. |
| 11 | `storage.partitions[].label` + `subvolumes` | Fedora | silent | Label dropped from the rebuilt `part` line (C-18). |
| 12 | `storage.encryption[].type: luks1` | Debian, Fedora, Arch, Alpine | warned (Alpine/Arch silent-ish) | LUKS2 created instead — a legacy-GRUB machine will not boot. |
| 13 | `storage.partitions[].size: "NN%"` | Arch, Alpine | silent | Means `rest` (Arch) or 0MiB (Alpine) (C-40). |
| 14 | `users[].password.locked` for `root` with a hash | Debian, Arch | silent | Root left unlocked. |
| 15 | `users[].ssh_authorized_keys[]` beyond `[0]`, or for root | SUSE, Alpine, Arch | silent | Keys silently discarded (C-32). |
| 16 | `system.init` other than the distro's own | all but NixOS, Void and Gentoo | warned | Spec §2.3 says MUST fail; six appliers accept and ignore it. |
| 17 | `network.firewall.enabled: false` | Ubuntu, Debian, SUSE, Arch (Fedora ◐) | warned | Firewall installed and enabled anyway (C-6). |
| 18 | `registration.server` / `registration.email` | Ubuntu, Fedora, SUSE(server) | **silent** | Formatted into a template with no matching placeholder. |
| 19 | `software.apps[].flatpak` / `.snap` / `.appimage` / `.preference[]` | Gentoo | **silent** | `consume(app)` marks every leaf read before only `package`/`name` are used. Four drops, no diagnostic in either channel (C-48). |
| 20 | `boot.initramfs.include_modules[]` | Gentoo | **warned as APPLIED** | The warning states the modules are folded into `force_drivers`; `dracut_conf` never reads the field. The only cell in this file where the diagnostic asserts the opposite of the truth (C-47). |
| 21 | `storage.partitions[].size` in `%`, `MB`/`GB`/`TB`, `KiB` or bytes | Gentoo | **silent** | Zero-length partition; every sibling after it starts at the wrong offset (C-45). |
| 22 | `target.firmware: "auto"` | Gentoo | **silent** | msdos label, no ESP, no `efibootmgr`, BIOS GRUB — while an *absent* firmware key correctly means UEFI (C-43). |
| 23 | `software.role: desktop:*` | Gentoo | **silent** | Selects a profile subtree and installs no desktop package at all (C-44). |
| 24 | `network.hosts[]` | Gentoo | **silent** | `consume(entry)`; `/etc/hosts` gets only the hostname line (C-48). |
| 25 | `keys[].type` / `.purpose[]` | Gentoo | **silent** | Read by the enrollment token map, then never used because `storage.encryption` always refuses (C-48). |
| 26 | Any `users[]` entry named `root` | Void, Gentoo | silent | `uid`, `comment`, `shell`, `groups` and `admin` on that entry are skipped; the hash and ssh keys still land. |
| 27 | `boot.kernel.variant: lts` | Gentoo | silent | Maps to `sys-kernel/gentoo-kernel-bin`, the same package `default` gets. |
| 28 | `network.manager: networkmanager` | Gentoo | silent | The package is installed; the enable verb is handed the *atom*, so `rc-update add net-misc/networkmanager` fails into `|| true` and the manager never starts. |
| 29 | `storage.partitions[].disk` / `.id` | Void | silent | Consumed and emitted nowhere — VAI's scheme is positional. Harmless only because Void refuses any document with more than one disk. |

## Tier 2 — frequently written, loudly dropped

| Field | Dropped on | Effect |
|---|---|---|
| `boot.secure_boot` | seven; **Void and Gentoo refuse** | No signing decision is made anywhere. Not proof Secure Boot fails — several distros ship a signed shim — only that the request does nothing. |
| `boot.uki` | seven; **Void and Gentoo refuse** | Arch hardcodes the opposite (`"uki": false`). |
| `boot.initramfs.generator` | seven; Void and Gentoo refuse a generator they cannot use | The distro's own generator is used regardless. |
| `boot.password_hash` | seven; Void refuses, **Gentoo implements it** | GRUB left unprotected on the seven. |
| `boot.console.serial` | seven; **Void honours it** | Alpine **forces** `ttyS0,115200` you did not ask for, and Gentoo hardcodes the same getty while passing your value to the kernel command line. |
| `boot.kernel.modules[]` / `blacklist[]` | all but NixOS, Void and Gentoo | No `modules-load.d`, no `modprobe.d`. |
| `boot.initramfs.include_modules[]` | all but NixOS and Void — and Gentoo warns that it applies it | |
| `storage.swap.zram.size` | seven; Void and Gentoo refuse the whole `storage.swap` block | zram is enabled at the package's default size, never yours. |
| `storage.swap.file.*` | Fedora, SUSE, Arch, Alpine; refused on Void and Gentoo | No swapfile created at all. |
| `network.interfaces[].*` | Fedora, SUSE, Arch (dropped); Debian, Alpine, Void, Gentoo (refused) | Static addressing silently becomes DHCP on Fedora/SUSE/Arch. Ubuntu and **NixOS** emit it. |
| `network.hosts[]` | all but NixOS and Void (Gentoo drops it silently) | `/etc/hosts` never written. |
| `system.time.servers[]` / `time.provider` | most; NixOS and Void honour the server list | NTP servers never configured. |
| `system.domain` | Ubuntu, Fedora, SUSE, Arch, Alpine, Void | Honoured on Debian, NixOS and Gentoo. |
| `system.kdump` | six; Gentoo refuses it | Native mechanisms exist on Fedora and SUSE and are unused. **NixOS now honours it** via `boot.crashDump.enable` — the only applier that does. |
| `software.apps[].snap` / `.appimage` / `.preference[]` | all nine | Source arbitration is hardcoded native-first. Silent on Gentoo (Tier 1 #19). |
| `installer.on_error` / `.answers` / `.unattended` | all nine | **`unattended` is never enforced — do not treat it as a consent gate.** |
| `installer.on_finish` | all nine except Ubuntu/Debian/Fedora/SUSE | **Void powers off unconditionally**, whatever the field says. |
| `users[].scripts.post[].*` | all but Gentoo | The user-level `post` phase is implemented only by Gentoo. Use `post_install`. |
| `scripts.*[].interpreter` / `.on_failure` | all nine, all phases | Body runs under the stage's shell; failure policy ignored. |
| `scripts.on_error[].*` | refused on eight; **Gentoo implements it** as a `bash` EXIT trap | |
| `keys[].id` / `.match` / `.pin_required` | seven, plus Gentoo; **Void refuses the whole section** | The `keys` ↔ `encryption` cross-reference of §17.2 is unimplementable in schema v0.1. |

## Tier 3 — false warnings (the reverse problem)

These fields **are** applied but are warned "not applied". They train users to ignore the warning
channel, which is what makes Tier 1 dangerous.

| Field | Distro | Note |
|---|---|---|
| `desktop.display_manager` | Fedora, SUSE, Alpine | Applied by `chroot_intents`; `check_section_fields` warns. |
| `boot.os_prober` | Debian | Native `grub-installer/with_other_os` is emitted; the warning is spurious. |
| `boot.kernel.params[]` | Alpine | Applied on the manual path; warned regardless. |
| `scripts.pre[].chroot` warning text | Debian, Fedora | The warning states the opposite of what the stage does. |
| `desktop.display_manager` | Gentoo | Installed and enabled by `chroot_intents`; `check_section_fields` warns "not applied". |
| `scripts.*[].chroot` warning text | Void, Gentoo | "always runs the script inside the target" is false for Void's `pre`/`pre_install` (installer initramfs) and for Gentoo's `pre`, `pre_install`, `pre_reboot`, `on_success` and `on_error` (installer host). |
| **`boot.initramfs.include_modules[]`** | **Gentoo** | **The reverse of the reverse problem: warned as applied, and not applied.** Listed in Tier 1 (#20) because it is a drop, not merely a bad warning. |

---

# 5. COVERAGE SUMMARY

Counts across all 233 schema leaves. Higher ✅+⚙ means more of a document survives translation;
higher ❌ means more of it is quietly discarded.

| Distro | ✅ YES | ◐ PARTIAL | ⚙ POST | ⛔ REFUSE | ❌ DROPS | – N/A | ? UNK | **✅+⚙ arrives** | **❌ share** |
|---|---|---|---|---|---|---|---|---|---|
| **NixOS** | 97 | 48 | 15 | 28 | 40 | 5 | 0 | **112** | 17% |
| **Ubuntu** | 52 | 41 | 33 | 29 | 74 | 4 | 0 | **85** | 32% |
| **Debian** | 41 | 46 | 34 | 40 | 68 | 4 | 0 | **75** | 29% |
| **Fedora** | 56 | 40 | 16 | 30 | 86 | 4 | 1 | **72** | 37% |
| **Arch** | 37 | 43 | 34 | 37 | 77 | 5 | 0 | **71** | 33% |
| **Gentoo** | 40 | 42 | 17 | 66 | 62 | 5 | 1 | **57** | 27% |
| **SUSE** | 33 | 52 | 24 | 29 | 91 | 4 | 0 | **57** | 39% |
| **Void** | 34 | 29 | 19 | 80 | 65 | 5 | 1 | **53** | 28% |
| **Alpine** | 25 | 58 | 20 | 42 | 83 | 5 | 0 | **45** | 36% |

Each row sums to 233. Sorted by ✅+⚙ — the count of leaves whose intent reaches the installed
machine by any route.

- **NixOS leads on every measure in this table** (97 ✅, 112 arriving, 17% dropped — all three the
  best of the nine), which is what the applier's shape predicts: almost anything a LIS document can
  say is expressible as a NixOS option, so most of what used to be `❌` here was never a limit of
  the distro, only a line nobody had written. The 40 remaining drops are concentrated in
  `scripts.*[].interpreter` and `.on_failure` (18 leaves between them, warned, and blocked on
  `lis_common.check_script_fields` keywords that do not exist yet) and the `installer.*` and
  `existing.*` subtrees. **The wipe-then-fail class is the thing to keep watching**: this applier is
  the one where a bad option name is an *evaluation* error rather than a silent drop, and under
  `--apply` evaluation happens after disko has already wiped the disks. Seven such paths were found
  and closed in one pass (C-35/C-36 plus grub+UEFI, flatpak-without-portal, pulseaudio-on-a-desktop-
  role, sddm-on-Wayland, and the `''` escape), every one of which a plain `nix-instantiate` on the
  generated file passes. The gate that catches them is filtering `config.assertions` for
  `!a.assertion` and forcing `system.build.toplevel.drvPath`; `builtins.seq (attrNames
  system.build)` forces neither assertions nor option values and is not sufficient.
  It still silently ignores the entire `existing.*` adoption subtree instead of refusing it.
- **Ubuntu covers the most ground of the non-NixOS columns** (85 arriving), but a third of that is `⚙` emulation
  and it carries the largest cluster of *silent* Tier-1 drops (root users, LSM-vs-nvidia,
  keyfile unlock).
- **Debian is the most conservative**: the highest ⛔ count among the mainstream three, which
  means fewer surprises — a Debian document that translates usually means what it says. Its cost
  is the one-line-per-script restriction (C-29).
- **SUSE and Alpine are the weakest targets.** SUSE's 91 drops are inflated by the two-profile
  split (C-21): fields are not missing so much as present in only one of two outputs. Alpine has
  the highest ⛔ count (42) *and* 83 drops, and its "simple path" (C-22) can discard an entire
  storage section without a single diagnostic.
- **Fedora's 86 drops** are dominated by `boot.*` and `network.interfaces[].*`; its storage and
  package handling are among the most native of the nine.
- **Void has by far the highest ⛔ count (80) and the lowest ✅+⚙ of any non-Alpine column** —
  and that is the shape of the applier, not a defect in it. 34 of those 80 refusals are the
  `storage` section alone, plus `keys[]` (6) and `target.firmware` (rule C-41/C-42): VAI expresses
  exactly one BIOS/MBR layout on one disk, and everything else in `storage`, plus UEFI, plus
  `keys[]`, is refused rather than approximated. 8 of the 9
  bundled recipes never translate. What does translate translates well: Void is the **only**
  applier besides NixOS where `boot.console.serial` reaches the installed system, and one of only three where
  `boot.kernel.modules[]`, `blacklist[]` and `system.time.servers[]` do.
- **Gentoo has the second-lowest ❌ share (27%, behind NixOS's 17%)** while sitting mid-table on
  arrival, which is the profile you want: it refuses 66 leaves loudly rather than dropping them. It
  was the first applier to implement `boot.password_hash`, `scripts.on_error` and the user-level
  `scripts.post` phase — NixOS has since joined it on all three — it is the only one where
  `drivers.gpu` is a build-time knob rather than a package name, and the only one that persists all
  three `proxy.*` keys into the target. **Its weakness is
  the opposite of Void's**: it is a young applier, and five of its drops are silent because
  `consume()` marks the leaf read before discarding it (rule C-48), plus one — 
  `boot.initramfs.include_modules[]` — that is warned as applied and is not (rule C-47). Those six
  are the highest-value fixes in this file.

**Reading these numbers.** ⛔ is not a defect — a refusal is the honest outcome for a feature the
installer cannot express, and it is strictly safer than ❌. The number to worry about is **❌**,
and within it the silent subset listed in §4 Tier 1. Note that NixOS's refusal count *fell* (36 →
28) while its drop count fell much further (68 → 40): converting a ❌ to a ⛔ is a legitimate
improvement, but most of the movement here was ❌ → ✅, i.e. options that existed all along.

**Also note the ✅/⚙ split is not comparable across columns without §1's convention.** Void's 19 ⚙
and Gentoo's 17 ⚙ are drawn by the rule stated in the preamble; a different but equally defensible
line would move perhaps a dozen cells between ✅ and ⚙ in either column. It would move nothing
between ✅+⚙ and ❌, which is the number that matters.

---

# 6. Method, and where this file is weaker than it looks

- Classifications come from reading each applier plus running it over generated documents that
  exercise every leaf, and diffing the produced installer configuration against the
  access-tracker's `check_unread` report. Drops were confirmed by the absence of any corresponding
  directive in the output, not by reading alone.
- **`?` appears three times.** `storage.partitions[].fs` for Fedora: output *is* produced for
  `f2fs` and `zfs` (`--fstype=f2fs`), with no refusal and no warning, but Anaconda was not run to
  confirm the failure mode. `system.keymap.font` for Void: the applier `sed`s an existing
  `FONT=` line in `/etc/rc.conf` and appends nothing, so it works only if the stock file carries
  that line — which was not checked. `desktop.display_manager` for Gentoo: the package is emerged
  and an enable verb emitted with `|| true`, but the OpenRC service name Gentoo's
  display-manager packages install was not verified. None of the three was resolved by guessing.
- **Void and Gentoo were classified from generated artifacts, not from a completed install.**
  Every ✅/◐/⚙ cell is grounded in a line of `lis2void.py` / `lis2gentoo.py` and confirmed present
  in the output of a document exercising that leaf; every ⛔ in the `refuse()` call that fires;
  every ❌ by generating the artifact set and grepping all of it for the value, then checking
  whether the tracker warned. What that method cannot see is a directive that is emitted, accepted
  and then does nothing on the real machine — which is exactly where the two `?` cells above sit.
- **The ✅-versus-⚙ line for these two columns is a convention, stated in §1**, not a property of
  the appliers. Cells sourced from a shared `lis_common.py` helper follow whatever the majority of
  the other seven columns already used for that helper, so the columns stay comparable; cells
  sourced from applier-specific code follow the rule in §1. Nothing in §4 or the ❌ counts depends
  on where that line falls.
- **Where classifiers disagreed about identical shared code**, this file says so rather than
  picking a winner: `scripts.*[].chroot` (§2.13 fn 5 — Arch ◐ vs everyone else ❌ for
  `lis_common.py:1299-1305`), `files[].*` (§2.12 fn 3 — SUSE/Alpine ✅ vs others ⚙ for
  `file_commands`), and `users[].dotfiles.method` (§2.8 fn 39 — Ubuntu ❌ vs others ◐ for the same
  warn-and-clone-raw code). The Void and Gentoo columns take the majority label in all three.
- **`--lenient` invalidates every ⛔ in this file.** It downgrades all refusals to warnings and
  writes the output anyway. A wizard offering that flag is offering to turn every safe refusal
  into a silent drop — and on Void, where 80 of 233 leaves refuse, that is a third of the document.
- Nothing here validates the nine §20 rules the schema cannot express (dangling handles, one
  `rest` per disk, exactly one `/`, firmware/loader coherence, no plaintext secrets, `wipe: false`
  accounting, btrfs-root-for-snapshots, desktop-role gating, autologin on an unlocked user). Schema
  validation passing tells you nothing about them; a generator must implement them itself.
