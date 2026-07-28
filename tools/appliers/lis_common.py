#!/usr/bin/env python3
"""lis_common — shared fail-closed plumbing for the LIS appliers.

SPEC §2.3 (the no-silent-drift rule) says an applier MUST refuse to apply a
document containing core intent it cannot honor. That is a *default*, not an
opt-in: every applier here refuses by default and only degrades to warnings
when the operator explicitly passes ``--lenient``.

Two report channels:

``refuse(msg)``
    Core intent (storage layout, encryption, LVM, RAID, users, network, boot)
    that the target installer cannot express. Fails the run unless ``--lenient``.

``warn(msg)``
    Intent that *is* honored, but by a different mechanism than the document
    literally implies (a preference, a re-homed hook, an approximation the
    spec explicitly allows). Never fails a run on its own.
"""

import argparse
import json
import pathlib
import shlex
import sys

WARNINGS: list[str] = []
REFUSALS: list[str] = []


def warn(msg: str) -> None:
    """Advisory drift — honored differently, never fatal."""
    WARNINGS.append(msg)
    print(f"warning: {msg}", file=sys.stderr)


def refuse(msg: str) -> None:
    """Core intent this applier cannot honor (SPEC §2.3). Fatal unless --lenient."""
    REFUSALS.append(msg)
    print(f"refused: {msg}", file=sys.stderr)


def reset() -> None:
    """Clear both channels (used by the test-suite when translating repeatedly)."""
    WARNINGS.clear()
    REFUSALS.clear()


# Role defaults, shared so every applier fills in the same blanks.
ROLE_FS = {"esp": "vfat", "boot": "ext4", "swap": "swap", "root": "btrfs"}
ROLE_MOUNT = {"esp": "/boot/efi", "boot": "/boot", "root": "/"}


def role_fs(part: dict) -> str | None:
    return part.get("fs") or ROLE_FS.get(part.get("role"))


def role_mountpoint(part: dict) -> str | None:
    return part.get("mountpoint") or ROLE_MOUNT.get(part.get("role"))


def check_firmware(doc: dict) -> None:
    """An ESP is a UEFI construct; on a BIOS target it is incoherent, not a detail.

    Installers reject it in their own words (d-i: "fat32 cannot be mounted on
    /boot"), so catching it here turns a mid-install dialog into a refusal.
    """
    firmware = (doc.get("target") or {}).get("firmware", "uefi")
    if firmware != "bios":
        return
    for i, part in enumerate((doc.get("storage") or {}).get("partitions", []) or []):
        if part.get("role") == "esp":
            refuse(f"partition {i} declares role 'esp' but target.firmware is 'bios' — "
                   "a BIOS system has no EFI system partition; use role 'boot' with a "
                   "Unix filesystem")


# Sections that describe the document itself rather than install intent, so
# there is nothing for an applier to translate.
NON_INTENT_SECTIONS = frozenset({"lis", "meta"})

# Every section of the spec that carries install intent.
ALL_SECTIONS = frozenset({
    "boot", "desktop", "drivers", "files", "installer", "keys", "mirror",
    "network", "proxy", "registration", "scripts", "software", "storage",
    "system", "target", "users",
})


def check_unhandled(doc: dict, handled: set[str] | frozenset) -> None:
    """Refuse any declared section this applier does not translate.

    Without this, adding a section to the spec silently widens the gap between
    what a document asks for and what gets installed: the applier simply never
    looks at the key, emits no output for it and says nothing, which is exactly
    the drift SPEC §2.3 forbids. Unhandled is the default here, so a new section
    is loud until someone teaches an applier about it.
    """
    for key in sorted(doc):
        if key in NON_INTENT_SECTIONS or key in handled:
            continue
        if doc[key] in (None, {}, []):
            continue  # declared but empty asks for nothing
        refuse(f"section '{key}' is declared but this applier does not "
               f"translate it — nothing about it would reach the target")


# drivers.* named per distro family. A value mapped to None is not in that
# distro's base repositories (nvidia needs RPM Fusion on Fedora, the NVIDIA repo
# on openSUSE, and has no Alpine package at all): emitting a name that will not
# resolve turns a driver request into a failed or silently skipped install, so
# those refuse instead.
DRIVER_PACKAGES: dict[str, dict] = {
    "arch": {
        "microcode": {"intel": "intel-ucode", "amd": "amd-ucode"},
        "gpu": {"nvidia": "nvidia", "nvidia-open": "nvidia-open",
                "amdgpu": "xf86-video-amdgpu", "intel": "mesa"},
        "firmware": "linux-firmware",
    },
    "alpine": {
        "microcode": {"intel": "intel-ucode", "amd": "amd-ucode"},
        "gpu": {"nvidia": None, "nvidia-open": None,
                "amdgpu": "mesa-dri-gallium", "intel": "mesa-dri-gallium"},
        "firmware": "linux-firmware",
    },
    "debian": {
        "microcode": {"intel": "intel-microcode", "amd": "amd64-microcode"},
        "gpu": {"nvidia": "nvidia-driver", "nvidia-open": None,
                "amdgpu": "firmware-amd-graphics",
                "intel": "xserver-xorg-video-intel"},
        "firmware": "firmware-linux",
    },
    # Ubuntu is not Debian here: firmware-linux and the firmware-* graphics
    # packages are Debian non-free names with no Ubuntu counterpart.
    "ubuntu": {
        "microcode": {"intel": "intel-microcode", "amd": "amd64-microcode"},
        "gpu": {"nvidia": None, "nvidia-open": None,
                "amdgpu": "xserver-xorg-video-amdgpu",
                "intel": "xserver-xorg-video-intel"},
        "firmware": "linux-firmware",
    },
    "fedora": {
        "microcode": {"intel": "microcode_ctl", "amd": "microcode_ctl"},
        "gpu": {"nvidia": None, "nvidia-open": None,
                "amdgpu": "xorg-x11-drv-amdgpu", "intel": "xorg-x11-drv-intel"},
        "firmware": "linux-firmware",
    },
    "suse": {
        "microcode": {"intel": "ucode-intel", "amd": "ucode-amd"},
        "gpu": {"nvidia": None, "nvidia-open": None,
                "amdgpu": "xf86-video-amdgpu", "intel": "xf86-video-intel"},
        "firmware": "kernel-firmware",
    },
}


def driver_packages(doc: dict, family: str,
                    skip: frozenset[str] = frozenset()) -> list[str]:
    """Packages carrying drivers.* intent, refusing what the family cannot supply.

    'auto' and 'none' add nothing: the first defers to the installer's own
    detection, the second asks for it to be left alone. On Debian and Ubuntu the
    names live in contrib/non-free, so a caller using the "debian" family must
    also enable those components or the packages will not resolve.
    """
    drivers = doc.get("drivers") or {}
    table = DRIVER_PACKAGES[family]
    out: list[str] = []

    gpu = drivers.get("gpu")
    if "gpu" not in skip and gpu not in (None, "auto", "none"):
        if gpu not in table["gpu"]:
            refuse(f"drivers.gpu {gpu!r} has no mapping for this distro")
        elif table["gpu"][gpu] is None:
            refuse(f"drivers.gpu {gpu!r} is not in this distro's base repositories "
                   "— add the vendor repository and name the package in "
                   "software.packages")
        else:
            out.append(table["gpu"][gpu])

    microcode = drivers.get("microcode")
    if microcode not in (None, "auto", "none"):
        if microcode not in table["microcode"]:
            refuse(f"drivers.microcode {microcode!r} has no mapping for this distro")
        else:
            out.append(table["microcode"][microcode])

    firmware = drivers.get("firmware")
    if firmware == "all":
        out.append(table["firmware"])
    elif firmware not in (None, "auto", "none"):
        refuse(f"drivers.firmware {firmware!r} has no mapping for this distro")

    return out


# Regenerating grub's config is spelled differently per distro.
GRUB_REFRESH = {
    "arch": "grub-mkconfig -o /boot/grub/grub.cfg",
    "alpine": "grub-mkconfig -o /boot/grub/grub.cfg",
    "debian": "update-grub",
    "ubuntu": "update-grub",
    "fedora": "grub2-mkconfig -o /boot/grub2/grub.cfg",
    "suse": "grub2-mkconfig -o /boot/grub2/grub.cfg",
}


def boot_timeout_commands(doc: dict, family: str, loader: str) -> list[str]:
    """Shell that applies boot.timeout to the bootloader the applier installs.

    No installer answer file carries a menu timeout, so this edits the target's
    own bootloader configuration from the post-install stage. GRUB_TIMEOUT is
    appended when absent rather than sed-replaced, because a missing line would
    otherwise make the edit a silent no-op.
    """
    timeout = (doc.get("boot") or {}).get("timeout")
    if timeout is None:
        return []
    timeout = int(timeout)
    if loader == "systemd-boot":
        conf = "/boot/loader/loader.conf"
        return [f"touch {conf}",
                f"sed -i '/^timeout /d' {conf}",
                f"printf 'timeout %s\\n' {timeout} >> {conf}"]
    refresh = GRUB_REFRESH.get(family)
    if refresh is None:
        warn(f"boot.timeout: no grub refresh command known for {family!r}")
        return []
    return [f"if grep -q '^GRUB_TIMEOUT=' /etc/default/grub 2>/dev/null; then "
            f"sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT={timeout}/' /etc/default/grub; "
            f"else echo 'GRUB_TIMEOUT={timeout}' >> /etc/default/grub; fi",
            refresh]


def kernel_params_commands(doc: dict, family: str) -> list[str]:
    """Shell that puts boot.kernel.params on the target's GRUB command line."""
    params = ((doc.get("boot") or {}).get("kernel") or {}).get("params")
    if not params:
        return []
    refresh = GRUB_REFRESH.get(family)
    if refresh is None:
        warn(f"boot.kernel.params: no grub refresh command known for {family!r}")
        return []
    joined = " ".join(params)
    return [f"sed -i 's|^GRUB_CMDLINE_LINUX=.*|GRUB_CMDLINE_LINUX=\"{joined}\"|' "
            "/etc/default/grub", refresh]


def sudoers_commands(doc: dict) -> list[str]:
    """Shell that grants passwordless sudo where the document asks for it.

    `admin: true` only earns membership of wheel/sudo, which still prompts. The
    document asking for `sudo: nopasswd` is a separate, security-relevant fact
    and needs its own sudoers drop-in; every applier has a post-install stage
    that can write one.
    """
    out: list[str] = []
    for user in doc.get("users", []) or []:
        if user.get("sudo") != "nopasswd":
            continue
        name = user["name"]
        path = f"/etc/sudoers.d/99-lis-{name}"
        out += [f"install -d -m755 /etc/sudoers.d",
                f"printf '%s ALL=(ALL) NOPASSWD: ALL\\n' {shlex.quote(name)} > {path}",
                f"chmod 0440 {path}"]
    return out


def check_user_sudo(doc: dict) -> None:
    """`sudo: nopasswd` is part of who may take root, so dropping it is core drift."""
    for user in doc.get("users", []) or []:
        if user.get("sudo") == "nopasswd":
            refuse(f"users['{user['name']}'].sudo: 'nopasswd' is not applied by this "
                   "applier — the account would still be prompted for a password")


def check_boot_extras(doc: dict, used: set[str] | frozenset) -> None:
    """Report boot.* keys this applier leaves to the installer's own defaults."""
    boot = doc.get("boot") or {}
    # boot.kernel's sub-keys are checked whether or not "kernel" itself is
    # listed: treating the parent as handled would hide every field inside it.
    for sub in sorted(set(boot.get("kernel") or {}) - set(used)):
        warn(f"boot.kernel.{sub} is not applied by this applier")
    for key in sorted(set(boot) - set(used) - {"kernel"}):
        warn(f"boot.{key} is not applied by this applier — the bootloader keeps "
             "its own default")


def check_section_fields(doc: dict, section: str,
                         used: set[str] | frozenset) -> None:
    """Report sub-keys of a section this applier translates only in part.

    A section is rarely all-or-nothing: an applier may honor
    desktop.display_manager and quietly ignore desktop.bluetooth. Refusing the
    whole section would block documents it can mostly satisfy, so the unhandled
    keys are named one by one instead.
    """
    for key in sorted(set(doc.get(section) or {}) - set(used)):
        warn(f"{section}.{key} is not applied by this applier")


def check_mirror(doc: dict, used: set[str] | frozenset) -> None:
    """Report mirror.* keys this applier cannot express."""
    mirror = doc.get("mirror") or {}
    for key in sorted(set(mirror) - set(used)):
        warn(f"mirror.{key} is not applied by this applier")


def check_kernel_variant(doc: dict, mapping: dict, label: str) -> str | None:
    """Resolve boot.kernel.variant against a distro's kernel packages."""
    variant = ((doc.get("boot") or {}).get("kernel") or {}).get("variant")
    if variant in (None, "default"):
        return None
    if variant not in mapping:
        refuse(f"boot.kernel.variant {variant!r} has no {label} kernel package")
        return None
    return mapping[variant]


def check_keymap(doc: dict, used: set[str] | frozenset) -> None:
    """Report system.keymap sub-keys this applier cannot express."""
    keymap = (doc.get("system") or {}).get("keymap") or {}
    extra = sorted(set(keymap) - set(used))
    if extra:
        warn(f"system.keymap.{', '.join(extra)} not applied by this applier "
             f"(it reads only {', '.join(sorted(used))})")


def add_common_args(ap: argparse.ArgumentParser) -> None:
    """Register the fail-closed flags shared by every applier."""
    ap.add_argument("file", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("."))
    ap.add_argument("--strict", dest="strict", action="store_true", default=True,
                    help="refuse (exit 1) when core intent cannot be honored — the default")
    ap.add_argument("--lenient", "--allow-drift", dest="strict", action="store_false",
                    help="downgrade refusals to warnings and continue (opts out of SPEC §2.3)")


def load_doc(path: pathlib.Path) -> dict:
    """Read a LIS document. Canonical form is JSON; YAML authoring is also accepted.

    Appliers run on minimal live ISOs where PyYAML is usually absent, so YAML
    falls back to the small block/flow subset parser below.
    """
    text = path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        import yaml
        doc = yaml.safe_load(text)
    except ImportError:
        try:
            doc = parse_yaml(text)
        except ValueError as err:
            sys.exit(f"error: cannot parse {path} as JSON or YAML: {err}")
    except Exception as err:  # noqa: BLE001 — surfaced verbatim to the operator
        sys.exit(f"error: cannot parse {path} as YAML: {err}")
    if not isinstance(doc, dict):
        sys.exit(f"error: {path} does not contain a LIS document mapping")
    return doc


# ── minimal YAML reader ──────────────────────────────────────────
#
# Covers exactly what LIS documents use for authoring: block mappings, block
# sequences, flow mappings/sequences (including multi-line ones), quoted and
# bare scalars, and `#` comments. Anchors, tags, multi-line scalars and merge
# keys are not LIS vocabulary and raise instead of being silently mis-read.

def _strip_comment(line: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote and line[i - 1] != "\\":
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (not out or out[-1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _balance(text: str) -> int:
    depth, quote = 0, None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote and text[i - 1] != "\\":
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
    return depth


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Comment-free, blank-free (indent, content) pairs with flow lines joined."""
    raw: list[tuple[int, str]] = []
    pending: str | None = None
    pending_indent = 0
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        stripped = _strip_comment(line)
        if not stripped.strip():
            continue
        if pending is not None:
            pending += " " + stripped.strip()
            if _balance(pending) <= 0:
                raw.append((pending_indent, pending.strip()))
                pending = None
            continue
        indent = len(stripped) - len(stripped.lstrip())
        content = stripped.strip()
        if _balance(content) > 0:
            pending, pending_indent = content, indent
            continue
        raw.append((indent, content))
    if pending is not None:
        raise ValueError("unterminated flow collection")

    # Split "- item" markers so a sequence entry and its value are separate lines.
    out: list[tuple[int, str]] = []
    for indent, content in raw:
        while content == "-" or content.startswith("- "):
            out.append((indent, "\x00seq"))
            if content == "-":
                content = ""
                break
            content = content[2:]
            indent += 2
        if content:
            out.append((indent, content))
    return out


def _split_key(text: str) -> tuple[str, str] | None:
    quote, depth = None, 0
    for i, ch in enumerate(text):
        if quote:
            if ch == quote and text[i - 1] != "\\":
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            if i + 1 == len(text) or text[i + 1] in " \t":
                return text[:i].strip().strip("\"'"), text[i + 1:].strip()
    return None


def _scalar(text: str):
    text = text.strip()
    if not text:
        return None
    if text[0] in "\"'" and text[-1] == text[0] and len(text) > 1:
        return text[1:-1]
    if text in ("null", "~"):
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _parse_flow(text: str, pos: int):
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    if pos >= len(text):
        return None, pos
    if text[pos] == "{":
        result, pos = {}, pos + 1
        while True:
            while pos < len(text) and text[pos] in " \t,":
                pos += 1
            if pos < len(text) and text[pos] == "}":
                return result, pos + 1
            key_end = pos
            quote = None
            while key_end < len(text):
                ch = text[key_end]
                if quote:
                    quote = None if ch == quote else quote
                elif ch in "\"'":
                    quote = ch
                elif ch == ":":
                    break
                key_end += 1
            key = text[pos:key_end].strip().strip("\"'")
            value, pos = _parse_flow(text, key_end + 1)
            result[key] = value
    if text[pos] == "[":
        result, pos = [], pos + 1
        while True:
            while pos < len(text) and text[pos] in " \t,":
                pos += 1
            if pos < len(text) and text[pos] == "]":
                return result, pos + 1
            value, pos = _parse_flow(text, pos)
            result.append(value)
    # bare or quoted scalar, terminated by , } ] at depth 0
    start, quote = pos, None
    while pos < len(text):
        ch = text[pos]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in ",}]":
            break
        pos += 1
    return _scalar(text[start:pos]), pos


def _parse_value(text: str):
    if text.startswith(("{", "[")):
        value, _ = _parse_flow(text, 0)
        return value
    return _scalar(text)


def _parse_node(lines: list[tuple[int, str]], idx: int, indent: int):
    if idx >= len(lines):
        return None, idx
    content = lines[idx][1]
    if content == "\x00seq":
        return _parse_seq(lines, idx, indent)
    if _split_key(content) is None:
        return _parse_value(content), idx + 1  # flow collection or bare scalar
    return _parse_map(lines, idx, indent)


def _parse_seq(lines, idx, indent):
    items = []
    while idx < len(lines) and lines[idx][0] == indent and lines[idx][1] == "\x00seq":
        idx += 1
        if idx < len(lines) and lines[idx][0] > indent:
            value, idx = _parse_node(lines, idx, lines[idx][0])
        else:
            value = None
        items.append(value)
    return items, idx


def _parse_map(lines, idx, indent):
    result: dict = {}
    while idx < len(lines) and lines[idx][0] == indent and lines[idx][1] != "\x00seq":
        content = lines[idx][1]
        split = _split_key(content)
        if split is None:
            raise ValueError(f"expected 'key: value', got {content!r}")
        key, rest = split
        idx += 1
        if rest:
            result[key] = _parse_value(rest)
        elif idx < len(lines) and lines[idx][0] > indent:
            result[key], idx = _parse_node(lines, idx, lines[idx][0])
        else:
            result[key] = None
    return result, idx


def parse_yaml(text: str):
    """Parse the YAML subset LIS documents are authored in. Raises ValueError."""
    lines = _logical_lines(text)
    if not lines:
        return None
    value, idx = _parse_node(lines, 0, lines[0][0])
    if idx != len(lines):
        raise ValueError(f"unexpected indentation at {lines[idx][1]!r}")
    return value


def check_version(doc: dict, path: pathlib.Path) -> None:
    if not str(doc.get("lis", "")).startswith("0.1."):
        sys.exit(f"error: unsupported LIS version in {path}: {doc.get('lis')!r}")


def enforce(strict: bool) -> int:
    """Fail-closed gate. Returns the exit status; call before applying anything."""
    if REFUSALS and strict:
        print(f"\nerror: refusing to apply — {len(REFUSALS)} core intent item(s) "
              "cannot be honored by this applier (SPEC §2.3, no silent drift):",
              file=sys.stderr)
        for item in REFUSALS:
            print(f"  • {item}", file=sys.stderr)
        print("\nFix the document, use an applier that supports this intent, or pass "
              "--lenient to accept the drift explicitly.", file=sys.stderr)
        return 1
    if REFUSALS:
        print(f"note: --lenient accepted {len(REFUSALS)} drifted core intent item(s)",
              file=sys.stderr)
    return 0


def report(*written: pathlib.Path) -> None:
    files = ", ".join(str(p) for p in written)
    print(f"wrote {files} ({len(WARNINGS)} warning(s), {len(REFUSALS)} refusal(s))")


def secret_ref(value) -> str | None:
    """Resolve a LIS secret reference to the path an applier should read at apply time.

    Secrets never live in the document (SPEC §2.4) — only references such as
    ``{"from": "seed:secrets/luks-root.key"}``. The seed volume is mounted at
    /run/lis/seed by the installer, so ``seed:`` maps into that tree.
    """
    if isinstance(value, dict):
        value = value.get("from")
    if not isinstance(value, str):
        return None
    if value.startswith("seed:"):
        return "/run/lis/seed/" + value[len("seed:"):].lstrip("/")
    if value.startswith("file:"):
        return value[len("file:"):]
    return None
