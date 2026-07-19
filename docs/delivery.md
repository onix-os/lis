# LIS delivery — the seed convention

**Status: v0.1.0-draft**, companion to [SPEC.md](../SPEC.md) §20.

A LIS document describes an installation; this document describes how an
installer **finds** one. The convention follows the two proven prior arts —
Anaconda's `OEMDRV` volume (auto-discovered kickstart) and cloud-init's
`CIDATA` NoCloud seed — and fixes their weakest point: discovery never
implies consent to destroy a machine.

## 1. The seed volume

A **LIS seed** is a filesystem labeled `LISDATA` (matched case-insensitively)
on any block device: USB stick, virtual disk, CD image. Installers MUST
support `vfat` and `iso9660` seeds and SHOULD support `ext4`.

Contents, all optional, at the volume root:

| file | meaning |
|---|---|
| `system.lis.json` | the intent — a LIS document to apply (level 2) |
| `authorized_keys` | the trust — OpenSSH public keys for remote provisioning (level 1) |
| `unattended` | the consent — empty marker file permitting a zero-prompt destructive run |
| `secrets/…` | secret material referenced from the document via `seed:` (§4) |

A seed MAY carry other conventions' files next to these (`user-data`,
`meta-data`, `ks.cfg`): one stick can drive cloud-init, Anaconda, and LIS.

## 2. Two levels: trust and intent

### Level 1 — trust seed (remote provisioning)

A seed with `authorized_keys` and **no** `system.lis.json` puts the booted
installer environment into **await mode**:

1. Bring up networking (DHCP on wired interfaces by default).
2. Start an SSH daemon and authorize the seed's keys for the installer user.
3. Announce (§5) and wait.

A remote frontend holding a matching private key connects, probes the
machine, produces a LIS document interactively, and drives the installation
over the wire. One generic stick provisions any number of machines; the
per-machine decisions happen live at the operator's side.

If the live environment needs more than DHCP (wifi, static addressing), the
seed MAY also carry a **storage-less** LIS document: a `system.lis.json`
without a `storage` section installs nothing — its `network` (and
`system.keymap`) sections are applied to the live environment instead, and
the installer proceeds to await mode.

### Level 2 — intent seed (unattended or prefilled)

A seed whose `system.lis.json` has a `storage` section is an install intent.
What happens next depends on consent (§3): a fully consented seed runs to
completion with no prompts; anything less loads the document into the
installer's interactive flow as prefilled answers.

When both `system.lis.json` (with storage) and `authorized_keys` are
present, the document applies **and** the SSH keys stay authorized in the
live environment for the duration of the run — the operator's escape hatch
to watch or abort.

## 3. Consent — the two-key rule

Discovering a document is not permission to erase a machine. An installer
MUST NOT begin a destructive, prompt-free run unless BOTH keys are present:

1. **In the document**: `installer.unattended: true` (and, for disks that
   hold data, `storage.wipe: true` as always).
2. **On the delivery channel**: the empty `unattended` marker file on the
   seed volume — or, for network delivery (§6), `lis.unattended=1` on the
   kernel command line.

The document key travels with the *intent* (which is copied and shared);
the channel key stays with the *physical object* someone deliberately
prepared and plugged into this machine. Missing either key, the installer
MUST stop at a confirmation step (or full interactive flow) with the
document's answers prefilled.

## 4. Secrets on the seed

SPEC §2.4 forbids inline secrets. The seed adds a third reference form to
`file:` and `env:`:

```json
{ "registration": { "token": { "from": "seed:secrets/scc-token" } } }
```

`seed:<relative-path>` resolves against the seed volume root. The document
stays committable and shareable; the secret material exists only on the
stick. Appliers MUST resolve `seed:` references at apply time and MUST NOT
copy the resolved values into any record they leave behind (§7).

## 5. Announcement

While in await mode (level 1), the installer SHOULD announce itself via
mDNS/DNS-SD as `_lis-installer._tcp` on the SSH port, with TXT records:

```
lisv=0.1  hostname=<transient-hostname>  arch=x86_64  serial=<dmi-serial-or-unknown>
```

so that frontends can enumerate every machine on the link currently waiting
for instructions. Implementations MAY skip announcement; operators can
always fall back to DHCP leases.

## 6. Network delivery and search order

Kernel command-line parameters, for PXE/netboot fleets:

- `lis.url=<http|https|file URL>` — fetch the document from a URL.
- `lis.device=<path | LABEL=x | UUID=x>` and optional `lis.path=<path>`
  (default `/system.lis.json`) — read it from a specific device.
- `lis.unattended=1` — the channel consent key for network delivery (§3).

An installer MUST search in this order and use the first document found:

1. `lis.url=`
2. `lis.device=`
3. a volume labeled `LISDATA` containing `/system.lis.json`
4. a volume labeled `CIDATA` containing `/system.lis.json` (piggyback)
5. a volume labeled `OEMDRV` containing `/system.lis.json` (piggyback)
6. nothing → await mode if an `authorized_keys` seed exists, else interactive.

If one tier matches more than one volume, the installer MUST fail rather
than guess — the same determinism rule as disk matching (SPEC §5).

## 7. The birth certificate

After a successful apply — regardless of how the document was delivered —
the applier SHOULD record the applied document on the installed system at:

```
/var/lib/lis/system.lis.json      (mode 0600, root-owned)
```

with `seed:`/`file:`/`env:` secret references left as references, never
resolved values. Every LIS-installed machine can then answer *"how were you
built?"* — and reinstalling or cloning it is: take the file, make a seed.

## 8. Prior art, and the deltas

| | OEMDRV (kickstart) | CIDATA (cloud-init) | LISDATA |
|---|---|---|---|
| discovery | label scan | label scan | label scan + url/device params |
| payload | ks.cfg | user-data/meta-data | system.lis.json |
| consent | none — found = executed | n/a (first boot, not install) | two-key (§3) |
| remote/attended mode | none | none | level 1: authorized_keys + await + announce |
| secrets | inline in ks.cfg | inline in user-data | `seed:` references, stick-only |
| record on target | anaconda-ks.cfg (with secrets) | /var/lib/cloud | birth certificate, secrets as refs |
| coexistence | — | — | explicitly piggybacks on both |
