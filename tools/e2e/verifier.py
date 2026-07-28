"""Stages 3 and 4 — check the installed system against the LIS document.

Every expectation here is derived from the recipe, and every answer is read out
of the installed system. The verifier does not create users, write hook markers
or set a hostname before checking for them: a test that plants its own evidence
proves nothing about the installer that was supposed to.
"""

import pathlib
import re
import shutil
import subprocess
import time

import pexpect

from tools.e2e.colors import (
    BOLD, CYAN, GREEN, GRAY, RED, YELLOW,
    print_stage_header, print_check_item, TICK, CROSS, WARN_ICON, RESET
)

# Terminal control sequences (bracketed paste above all) arrive interleaved with
# command output and would otherwise be read back as if they were the answer.
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[()][B0]")

# `echo 'MARKER' > /path/file` — the shape the test recipe's hooks use.
HOOK_RE = re.compile(r"""echo\s+['"]?(?P<marker>[A-Z0-9_]+)['"]?\s*>\s*(?P<path>\S+)""")


class Expectations:
    """What the document says the installed system must look like."""

    def __init__(self, recipe: dict):
        self.recipe = recipe
        system = recipe.get("system", {}) or {}
        self.hostname = system.get("hostname")
        self.users = [u for u in recipe.get("users", []) or [] if u["name"] != "root"]
        self.hooks = self.collect_hooks(recipe)

    @staticmethod
    def collect_hooks(recipe: dict) -> list[tuple[str, str, str]]:
        """(label, path, marker) for every script hook that writes a marker file.

        `pre_install`/`pre` hooks run on the installer host before a target root
        exists, so nothing they write is expected on the installed system.
        """
        found: list[tuple[str, str, str]] = []
        scripts = recipe.get("scripts", {}) or {}
        for stage, items in scripts.items():
            if stage in ("pre_install", "pre", "on_error"):
                continue
            for item in items or []:
                if match := HOOK_RE.search(item.get("content", "")):
                    found.append((f"scripts.{stage}", match["path"], match["marker"]))
        for user in recipe.get("users", []) or []:
            for stage, items in (user.get("scripts", {}) or {}).items():
                if stage in ("pre_install", "pre"):
                    continue
                for item in items or []:
                    if match := HOOK_RE.search(item.get("content", "")):
                        found.append((f"users[{user['name']}].scripts.{stage}",
                                      match["path"], match["marker"]))
        return found


def verify_installed_disk(target_disk: pathlib.Path, recipe: dict) -> bool:
    """Stage 3 — inspect the target image offline with guestfish."""
    print_stage_header(3, "Post-installation filesystem inspection")
    expected = Expectations(recipe)

    if not target_disk.exists():
        print_check_item("Target disk image", False, "image missing")
        return False
    disk_bytes = target_disk.stat().st_size
    print(f"{GRAY}Inspecting{RESET} {BOLD}{target_disk}{RESET}")

    if not shutil.which("guestfish"):
        print(f"  {YELLOW}{WARN_ICON} guestfish not installed — Stage 3 skipped; "
              f"Stage 4 verifies the same facts in the booted guest.{RESET}")
        return True

    def cat(path: str) -> str:
        res = subprocess.run(["guestfish", "--ro", "-a", str(target_disk), "-i",
                              "cat", path], capture_output=True, text=True, timeout=60)
        return res.stdout if res.returncode == 0 else ""

    probe = subprocess.run(["guestfish", "--ro", "-a", str(target_disk), "-i",
                            "ls", "/"], capture_output=True, text=True, timeout=60)
    if probe.returncode != 0:
        print(f"  {YELLOW}{WARN_ICON} guestfish could not open the image "
              f"({probe.stderr.strip().splitlines()[-1] if probe.stderr else 'no root found'}) "
              f"— Stage 4 verifies in the booted guest.{RESET}")
        return True

    results = []

    def check(label: str, passed: bool, detail: str) -> None:
        results.append(passed)
        print_check_item(label, passed, detail)

    if expected.hostname:
        hostname = cat("/etc/hostname").strip()
        check("Hostname (/etc/hostname)", hostname == expected.hostname,
              f"expected {expected.hostname!r}, found {hostname!r}")

    passwd = cat("/etc/passwd")
    for user in expected.users:
        check(f"User account ({user['name']})", f"{user['name']}:" in passwd,
              "present in /etc/passwd" if f"{user['name']}:" in passwd else "missing")

    shadow = cat("/etc/shadow")
    for user in expected.users:
        hash_ = (user.get("password") or {}).get("hash")
        if not hash_:
            continue
        check(f"Password hash ({user['name']})", hash_ in shadow,
              "hash from the document is in /etc/shadow" if hash_ in shadow
              else "hash not found — the installer set a different password")

    birth = cat("/var/lib/lis/system.lis.json")
    check("Birth certificate (/var/lib/lis/system.lis.json)",
          '"lis"' in birth,
          f"{len(birth)} bytes" if birth else "not written by the applier")

    for label, path, marker in expected.hooks:
        if "firstboot" in label:
            continue  # first-boot hooks only run once the system boots (Stage 4)
        content = cat(path)
        check(f"Hook {label} → {path}", marker in content,
              f"marker {marker!r} present" if marker in content else "marker missing")

    passed = all(results) if results else False
    icons = " ".join(TICK if r else CROSS for r in results)
    print(f"\n{BOLD}{CYAN}Stage 3 result:{RESET} {icons}")
    return passed


def run_stage4_live_guest_verification(args, target_disk: pathlib.Path,
                                       recipe: dict) -> int:
    """Stage 4 — boot the installed disk alone and interrogate the running system."""
    print_stage_header(4, "Reboot test — booting the installed OS and verifying live")
    expected = Expectations(recipe)

    cmd = (f"qemu-system-x86_64 -enable-kvm -m {args.ram} -smp 4 -cpu host "
           f"-drive file={target_disk},if=virtio,format=qcow2 -boot order=c -nographic")
    print(f"  [{TICK}] Booting from {target_disk} with no install media attached...")

    results: list[bool] = []

    def check(label: str, passed: bool, detail: str) -> None:
        results.append(passed)
        print_check_item(label, passed, detail)

    child = None
    try:
        child = boot(cmd, target_disk)
        if child is None:
            check("Installed system boots to a usable shell", False,
                  "QEMU could not open the target image (still locked by the installer)")
            return report(results, args)
        if not login(child, recipe) or run(child, "echo LIS_SHELL_OK") != "LIS_SHELL_OK":
            check("Installed system boots to a usable shell", False,
                  "no shell that answers a command within the timeout")
            return report(results, args)
        check("Installed system boots to a usable shell", True,
              "shell answered a probe command")

        print(f"\n{BOLD}{CYAN}Live guest checklist:{RESET}")

        if expected.hostname:
            value = run(child, "cat /etc/hostname")
            check("Hostname (/etc/hostname)", value.strip() == expected.hostname,
                  f"expected {expected.hostname!r}, found {value.strip()!r}")

        for user in expected.users:
            value = run(child, f"id -u {user['name']} 2>/dev/null || echo MISSING")
            ok = value.strip().isdigit()
            check(f"User account ({user['name']})", ok,
                  f"uid {value.strip()}" if ok else "account does not exist")
            for group in user.get("groups", []) or []:
                value = run(child, f"id -nG {user['name']} 2>/dev/null")
                check(f"  group {group} for {user['name']}", group in value.split(),
                      f"groups: {value.strip()[:60]}")

        value = run(child, "cat /var/lib/lis/system.lis.json 2>/dev/null | head -c 40")
        check("Birth certificate (/var/lib/lis/system.lis.json)", '"lis"' in value,
              value.strip()[:40] or "not present")

        for label, path, marker in expected.hooks:
            value = run(child, f"cat {path} 2>/dev/null")
            check(f"Hook {label} → {path}", marker in value,
                  f"marker {marker!r} present" if marker in value else "marker missing")

        for pkg in packages_of(recipe):
            value = run(child, f"command -v {pkg} >/dev/null 2>&1 && echo YES || echo NO")
            check(f"Package installed ({pkg})", "YES" in value,
                  "binary on PATH" if "YES" in value else "not found on PATH")

    except Exception as err:  # noqa: BLE001 — a broken VM session is a failed test
        print(f"  {RED}Stage 4 aborted: {err}{RESET}")
        results.append(False)
    finally:
        if child is not None:
            try:
                child.sendline("poweroff")
                child.expect(pexpect.EOF, timeout=60)
            except Exception:  # noqa: BLE001
                pass
            try:
                child.close(force=True)
            except Exception:  # noqa: BLE001
                pass

    return report(results, args)


# Plaintext behind the crypt(3) hash in docs/examples/test-full-install.lis.json.
# The document must not contain it (SPEC §2.4); an operator knows it out of band,
# and so does this harness, because it wrote the recipe.
OPERATOR_PASSWORD = "lis-e2e"


def boot(cmd: str, target_disk: pathlib.Path) -> pexpect.spawn | None:
    """Start the guest, waiting out the installer VM's lingering write lock.

    The install VM may not have released the qcow2 by the time this runs; QEMU
    then exits immediately with "Failed to get write lock". Reported as a boot
    failure that looks exactly like a hung guest, so retry briefly instead.
    """
    deadline = 12
    for attempt in range(deadline):
        child = pexpect.spawn(cmd, encoding="utf-8", codec_errors="ignore", timeout=300)
        try:
            child.expect(["Failed to get .*lock", "is another process using"], timeout=5)
        except pexpect.TIMEOUT:
            return child  # booting: no lock complaint in the first seconds
        except pexpect.EOF:
            pass
        child.close(force=True)
        if attempt == 0:
            print(f"  {GRAY}target image still locked by the installer; waiting…{RESET}")
        time.sleep(5)
    return None


def packages_of(recipe: dict) -> list[str]:
    """Package names whose binary should be on PATH — a cheap, honest software check."""
    software = recipe.get("software", {}) or {}
    names = list(software.get("packages", []))
    for app in software.get("apps", []):
        if isinstance(app, str):
            names.append(app)
        elif isinstance(app, dict) and (name := app.get("package") or app.get("name")):
            names.append(name)
    # Only check names that are also the command they install.
    return [n for n in names if n in {"git", "curl", "htop", "neovim", "firefox", "vim"}]


def login(child: pexpect.spawn, recipe: dict, password: str = OPERATOR_PASSWORD) -> bool:
    """Reach a root shell on the booted guest, without creating anything."""
    # busybox prints "~ # " with a space, bash "root@host:~#" — both are shells.
    # A bracket prompt ("[user@host:~]$ ") is matched on the bracket-plus-sigil
    # alone: NixOS colours it, so the sigil is followed by an ANSI reset rather
    # than by the space the other patterns expect.
    prompts = [r"[#$] $", r"~ ?[#$]", r"\][#$]", r"root@"]
    login_prompt = r"[a-zA-Z0-9_.-]+ login:"
    try:
        # Generous: several of these guests take over two minutes to reach a
        # console on an unloaded host, and a slow boot must not be reported as
        # a system that does not boot.
        idx = child.expect([login_prompt, *prompts, pexpect.TIMEOUT], timeout=600)
    except pexpect.EOF:
        return False
    if idx == len(prompts) + 1:
        return False
    if idx == 0:
        # An autologin console prints the very same "host login:" banner and
        # then hands over a shell unprompted, so the banner alone does not mean
        # credentials are wanted. Give the shell a moment to appear first.
        try:
            child.expect(prompts, timeout=20)
            return True
        except pexpect.EOF:
            return False
        except pexpect.TIMEOUT:
            pass
        # A document carries only a crypt(3) hash (SPEC §9); the plaintext is
        # operator knowledge supplied out of band, which for the bundled test
        # recipe is OPERATOR_PASSWORD. Logging in this way is the only route on
        # a target whose /etc is immutable and cannot be given a serial
        # autologin by a post-install hook (NixOS).
        for user in recipe.get("users", []) or []:
            if not (user.get("password") or {}).get("hash"):
                continue
            child.sendline(user["name"])
            try:
                child.expect(["[Pp]assword:"], timeout=30)
                child.sendline(password)
                child.expect(prompts, timeout=60)
            except (pexpect.TIMEOUT, pexpect.EOF):
                return False
            if user.get("admin") or user.get("sudo"):
                # The checks read root-owned artifacts (the birth certificate is
                # mode 600), so escalate through the account's own sudo rights.
                child.sendline("sudo -i")
                try:
                    idx2 = child.expect(["[Pp]assword.*:", *prompts], timeout=60)
                    if idx2 == 0:
                        child.sendline(password)
                        child.expect(prompts, timeout=60)
                except (pexpect.TIMEOUT, pexpect.EOF):
                    return False
            return True
        # Nothing in the document can open a session: report it rather than
        # guessing at a credential.
        return False
    return True


def run(child: pexpect.spawn, command: str) -> str:
    """Run a command in the guest and return its last line of output.

    Everything between the sendline and the marker is echo, prompt noise and
    possibly stray kernel messages; the command's own answer is the last line
    that is none of those.
    """
    # The terminal echoes the command before running it, so the sentinel is
    # written in a form the shell collapses ("LIS_E''OC" -> "LIS_EOC"). Without
    # that, expect() matches the echo instead of the output and every check
    # reads back an empty string.
    marker = "LIS_EOC"
    sentinel = "LIS_E''OC"
    child.sendline(f"{command}; echo {sentinel}")
    try:
        # A serial console emits "\r\r\n", so the line ending needs \r* not \r?.
        child.expect(marker + r"[\r\n]+", timeout=30)
    except (pexpect.TIMEOUT, pexpect.EOF):
        return ""
    # The sentinel appears exactly once before the output — in the echo of the
    # command — so everything after it is the command's own answer. Matching on
    # substrings of the command instead would eat real output ("video" contains
    # the "id" of `id -nG`).
    text = child.before
    if sentinel in text:
        text = text.split(sentinel, 1)[1]
    lines = [ANSI.sub("", line).strip(" \r\t") for line in text.splitlines()]
    output = [line for line in lines if line]
    return output[-1] if output else ""


def report(results: list[bool], args) -> int:
    passed = sum(1 for r in results if r)
    total = len(results)
    ok = total > 0 and passed == total
    colour = GREEN if ok else RED
    icon = TICK if ok else CROSS
    print(f"\n{BOLD}{colour}{'=' * 60}{RESET}")
    print(f"{BOLD}{colour}  {icon} {args.distro.upper()}: {passed}/{total} checks passed"
          f"{RESET}")
    print(f"{BOLD}{colour}{'=' * 60}{RESET}\n")
    return 0 if ok else 1
