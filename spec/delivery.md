# LIS delivery — the boot manifest & seed convention

**Status: v0.1.0-draft**, companion to [schema.md](schema.md) §21.

A LIS document describes an installation intent (the recipe); this document describes how an installer **locates, fetches, and authorizes** that intent using a boot manifest (`lis.json`) delivered via physical media (`LIS` seed volume), network parameters, or dynamic remote hooks.

---

## 1. The LIS volume & `lis.json` boot manifest

A **LIS seed** is a filesystem labeled `LIS` (or `LISDATA`, matched case-insensitively) on any block device: USB stick, virtual disk, CD image, or NVMe partition. Installers MUST support `vfat` and `iso9660` seeds and SHOULD support `ext4`.

Instead of requiring a static system configuration file on the volume, the volume root contains a lightweight **boot manifest** named `lis.json` (with `system.lis.json` supported for legacy/direct intent):

```
LIS/ (Volume Root)
├── lis.json           the manifest — HOW to get the recipe & keys
├── authorized_keys    the trust    — OpenSSH public keys for remote provisioning
├── unattended         the consent  — empty marker file permitting zero-prompt runs
├── keys/…             local key files (GPG, binary keyfiles)
└── secrets/…          secret material referenced via { "from": "seed:secrets/…" }
```

---

## 2. Multi-source recipe resolution (`source`)

The `lis.json` manifest specifies where the installer should fetch the system recipe via the `source` field. `source` can be a single source object or a **fallback priority chain** (array).

### 2.1 Supported source types

| Source type | Description | Example / Schema |
|---|---|---|
| `file` / `path` | Local filesystem path | `"source": { "type": "file", "path": "/recipes/server.lis.json" }` |
| `disk` | Specific disk partition or label | `"source": { "type": "disk", "match": { "label": "DATA" }, "path": "/server.lis.json" }` |
| `http` / `https` | Remote web server or CMDB API | `"source": { "type": "https", "url": "https://cmdb.internal/api/lis" }` |
| `nfs` | Remote Network Attached Storage | `"source": { "type": "nfs", "server": "nas.local", "export": "/deploy", "path": "/web.lis.json" }` |
| `smb` / `cifs` | Windows / Samba share | `"source": { "type": "smb", "share": "//nas.local/configs", "path": "node1.lis.json" }` |
| `git` | Git repository branch/tag | `"source": { "type": "git", "url": "https://github.com/org/infra.git", "path": "nodes/web.lis.json" }` |
| `s3` | S3 / MinIO object storage | `"source": { "type": "s3", "endpoint": "https://minio.local:9000", "bucket": "recipes", "key": "node.lis.json" }` |
| `await` | Remote SSH / Web hook | `"source": { "type": "await", "protocol": "ssh", "timeout": 600 }` |
| `exec` | Local generator script | `"source": { "type": "exec", "command": "/lis/scripts/detect_and_generate.sh" }` |
| `interactive` | Local TUI / GUI wizard | `"source": { "type": "interactive" }` |

### 2.2 Fallback priority chains

If `source` is an array, the installer attempts each source sequentially:

```json
{
  "lis": "0.1.0",
  "source": [
    {
      "type": "nfs",
      "server": "nas.internal.net",
      "export": "/deployments",
      "path": "server-01.lis.json"
    },
    {
      "type": "disk",
      "match": { "label": "LIS_BACKUP" },
      "path": "/system.lis.json"
    },
    {
      "type": "await",
      "protocol": "ssh",
      "timeout": 300
    },
    {
      "type": "interactive"
    }
  ]
}
```

---

## 3. Explicit key objects (`keys`)

The manifest or LIS document can declare an explicit `keys` section for hardware tokens (YubiKey), cryptographic keys, or keyfiles used for disk encryption (`LUKS`) or document decryption:

```json
"keys": [
  {
    "id": "admin-yubikey",
    "type": "yubikey_fido2",
    "purpose": "disk_encryption",
    "match": { "serial": "12345678" },
    "pin_required": true
  },
  {
    "id": "luks-keyfile",
    "type": "keyfile",
    "purpose": "disk_encryption",
    "source": { "from": "seed:keys/luks-root.key" }
  }
]
```

### Supported key types:
- `yubikey_fido2`: Enrolled directly via `systemd-cryptenroll --fido2-device=auto`.
- `yubikey_challenge`: HMAC-SHA1 challenge-response key.
- `tpm2`: Bound to motherboard TPM2 PCR registers.
- `gpg` / `age`: Used for decrypting encrypted recipe payloads.
- `keyfile`: Binary keyfile for LUKS or volume unlocking.
- `ssh`: Public key for remote session authorization.

---

## 4. Remote await mode & dynamic hooks (`source: "await"`)

When `source` resolves to `"await"` (or a seed contains `authorized_keys` with no static recipe):

1. The live installer brings up networking (DHCP by default).
2. It starts an SSH daemon (or ephemeral HTTPS web server) authorizing the `authorized_keys`.
3. It announces itself on the LAN via mDNS (`_lis-installer._tcp`) with TXT records:
   ```
   lisv=0.1  hostname=<transient-hostname>  arch=x86_64  serial=<dmi-serial-or-unknown>
   ```
4. A remote operator or deployment wizard connects over SSH, probes the system, generates the recipe interactively or programmatically, and **pushes the LIS JSON across the hook**.
5. The installer receives the recipe, binds any local hardware keys on the target machine, and executes the installation.

---

## 5. Consent — the two-key rule

Discovering a document or receiving a recipe over a network hook is not permission to erase a machine. An installer MUST NOT begin a destructive, prompt-free run unless BOTH consent keys are present:

1. **In the document**: `installer.unattended: true` (and `storage.wipe: true` for target disks).
2. **On the delivery channel**: the empty `unattended` marker file on the `LIS` seed volume — or `lis.unattended=1` on the kernel command line.

Missing either key, the installer MUST stop at a confirmation step (or full interactive flow) with the document's answers prefilled.

---

## 6. Secrets on the seed

SPEC §2.4 forbids inline secrets. The seed allows secret references:

```json
{ "registration": { "token": { "from": "seed:secrets/scc-token" } } }
```

`seed:<relative-path>` resolves against the `LIS` seed volume root. The document stays committable and shareable; secret material exists only on physical media or secure stores. Appliers MUST resolve `seed:` references at apply time and MUST NOT copy resolved secrets into target log files or birth certificates.

---

## 7. Network delivery and search order

Kernel command-line parameters for PXE/netboot fleets:

- `lis.url=<http|https|nfs|git URL>` — fetch the document/manifest directly.
- `lis.device=<path | LABEL=x | UUID=x>` and optional `lis.path=<path>` (default `/lis.json`).
- `lis.unattended=1` — channel consent key for network delivery.

Installer search order:
1. `lis.url=`
2. `lis.device=`
3. Volume labeled `LIS` containing `/lis.json` or `/system.lis.json`
4. Volume labeled `LISDATA` containing `/lis.json` or `/system.lis.json`
5. Volume labeled `CIDATA` containing `/system.lis.json` (piggyback)
6. Volume labeled `OEMDRV` containing `/system.lis.json` (piggyback)
7. Fallback to `await` mode if `authorized_keys` exists, else local `interactive` TUI.

---

## 8. The birth certificate

After a successful apply — regardless of how the document was delivered — the applier SHOULD record the applied document on the installed system at:

```
/var/lib/lis/system.lis.json      (mode 0600, root-owned)
```

with `seed:`/`file:`/`env:` secret references left as references, never resolved values. Every LIS-installed machine can then answer *"how were you built?"* — and reinstalling or cloning it is: take the file, make a seed.

