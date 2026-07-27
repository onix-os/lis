"""CLI entrypoint for the LIS end-to-end VM test suite."""

import argparse
import copy
import datetime
import json
import pathlib
import subprocess
import sys

from tools.e2e.colors import (
    BOLD, CYAN, GREEN, GRAY, MAGENTA, RED, WHITE, YELLOW,
    print_stage_header, TICK, CROSS, WARN_ICON, RESET
)
from tools.e2e.iso import download_iso_if_missing
from tools.e2e.installer import (DocumentRefused, InstallFailed,
                                 run_stage2_qemu_installer)
from tools.e2e.verifier import verify_installed_disk, run_stage4_live_guest_verification

DISTROS = ["alpine", "nixos", "ubuntu", "arch", "fedora", "suse", "debian"]
REFUSED = 2  # exit status meaning "the applier refused the document", not "it broke"
BUILD_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "build"


def print_intent(recipe: dict) -> None:
    print_stage_header(1, "Recipe intent and expectations")
    meta = recipe.get("meta", {}) or {}
    target = recipe.get("target", {}) or {}
    print(f"  {BOLD}• Recipe{RESET}: {CYAN}{meta.get('name')}{RESET}")
    print(f"  {BOLD}• Target{RESET}: {WHITE}{target.get('arch')}{RESET} "
          f"({target.get('firmware')})")

    storage = recipe.get("storage", {}) or {}
    parts = storage.get("partitions", []) or []
    print(f"  {BOLD}• Storage{RESET}: {len(parts)} partition(s)"
          + (f", {len(storage['lvm'])} volume group(s)" if storage.get("lvm") else "")
          + (f", {len(storage['raid'])} array(s)" if storage.get("raid") else "")
          + (f", {len(storage['encryption'])} LUKS container(s)"
             if storage.get("encryption") else ""))
    for part in parts:
        fs = (part.get("fs") or "-").upper()
        print(f"    {GRAY}└─[{part.get('role')}]{RESET} {fs:<6} "
              f"{part.get('size', 'rest'):<8} -> {CYAN}{part.get('mountpoint', '')}{RESET}")

    for user in recipe.get("users", []) or []:
        print(f"  {BOLD}• User{RESET}: {GREEN}{user['name']}{RESET} "
              f"(shell: {user.get('shell', 'default')}, "
              f"groups: {', '.join(user.get('groups', [])) or '-'})")

    software = recipe.get("software", {}) or {}
    print(f"  {BOLD}• Role{RESET}: {MAGENTA}{software.get('role', '-')}{RESET}")
    listed = software.get("apps", []) + software.get("packages", [])
    print(f"  {BOLD}• Software{RESET}: {WHITE}{', '.join(map(str, listed)) or '-'}{RESET}")

    hooks = recipe.get("scripts", {}) or {}
    summary = ", ".join(f"{stage} ({len(items or [])})" for stage, items in hooks.items())
    print(f"  {BOLD}• Script hooks{RESET}: {summary or '-'}")


def run_single_distro_test(args) -> int:
    recipe_path = args.recipe.resolve()
    if not recipe_path.exists():
        sys.exit(f"{RED}error: recipe file '{recipe_path}' not found{RESET}")
    recipe = json.loads(recipe_path.read_text())

    BUILD_DIR.mkdir(exist_ok=True)
    target_disk = BUILD_DIR / f"e2e-{args.distro}-target.qcow2"
    seed_disk = BUILD_DIR / f"e2e-LIS-{args.distro}.img"

    # Stamp the run: a log without one is impossible to tell apart from the
    # previous run's leftovers when a suite takes half an hour per distro.
    started = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{BOLD}{CYAN}╔{'═' * 68}╗{RESET}")
    print(f"{BOLD}{CYAN}║  LIS END-TO-END VM TEST SUITE — {args.distro.upper():<34}║{RESET}")
    print(f"{BOLD}{CYAN}╚{'═' * 68}╝{RESET}")
    print(f"  {GRAY}run started {started}{RESET}")

    print_intent(recipe)

    if not args.verify_only:
        iso_path = args.iso or download_iso_if_missing(args.distro)

        if target_disk.exists():
            target_disk.unlink()
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(target_disk),
                        args.disk_size], check=True, capture_output=True)
        print(f"  [{TICK}] Created {args.disk_size} blank target disk: "
              f"{BOLD}{target_disk}{RESET}")

        make_seed = pathlib.Path(__file__).resolve().parent.parent / "lis-make-seed"
        subprocess.run([sys.executable, str(make_seed), str(recipe_path),
                        "--out", str(seed_disk)], check=True)

        try:
            run_stage2_qemu_installer(args.distro, target_disk, seed_disk, iso_path,
                                      args.ram, recipe_path)
        except DocumentRefused as err:
            # Not a malfunction: the recipe asks for something this distro's
            # installer cannot express, and the applier said so before touching
            # the disk. Report it as its own outcome.
            print(f"\n  {YELLOW}{WARN_ICON} {err.applier} refused the document "
                  f"(SPEC §2.3, no silent drift):{RESET}")
            for reason in err.reasons:
                print(f"      • {reason}")
            print(f"\n{BOLD}{YELLOW}{'=' * 60}{RESET}")
            print(f"{BOLD}{YELLOW}  {args.distro.upper()}: REFUSED — nothing was "
                  f"installed{RESET}")
            print(f"{BOLD}{YELLOW}{'=' * 60}{RESET}\n")
            return REFUSED
        except InstallFailed as err:
            print(f"\n  {RED}{CROSS} Installation failed: {err}{RESET}")
            print(f"\n{BOLD}{RED}{'=' * 60}{RESET}")
            print(f"{BOLD}{RED}  {CROSS} {args.distro.upper()}: install stage failed{RESET}")
            print(f"{BOLD}{RED}{'=' * 60}{RESET}\n")
            return 1

    disk_ok = verify_installed_disk(target_disk, recipe)
    live = run_stage4_live_guest_verification(args, target_disk, recipe)
    return 0 if (disk_ok and live == 0) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="LIS end-to-end VM test engine.")
    ap.add_argument("--distro", choices=[*DISTROS, "all"], required=True,
                    help="distro to test (or 'all')")
    ap.add_argument("--iso", type=pathlib.Path, help="path to a live ISO")
    ap.add_argument("--recipe", type=pathlib.Path,
                    default=pathlib.Path("docs/examples/test-full-install.lis.json"),
                    help="path to the LIS recipe")
    ap.add_argument("--ram", default="4G", help="VM memory (default: 4G)")
    ap.add_argument("--disk-size", default="20G", help="target disk size (default: 20G)")
    ap.add_argument("--verify-only", action="store_true",
                    help="skip the install and verify an existing target image")
    ap.add_argument("--keep", action="store_true",
                    help="keep build artifacts after each distro in an 'all' run")
    args = ap.parse_args()

    if args.distro != "all":
        return run_single_distro_test(args)

    summary: dict[str, int] = {}
    for distro in DISTROS:
        print(f"\n{BOLD}{CYAN}{'=' * 60}{RESET}")
        print(f"{BOLD}{CYAN}  STARTING E2E TEST: {distro.upper()}{RESET}")
        print(f"{BOLD}{CYAN}{'=' * 60}{RESET}")
        sub_args = copy.copy(args)
        sub_args.distro = distro
        summary[distro] = run_single_distro_test(sub_args)
        if not args.keep:
            for artifact in (BUILD_DIR / f"e2e-{distro}-target.qcow2",
                             BUILD_DIR / f"e2e-LIS-{distro}.img"):
                artifact.unlink(missing_ok=True)

    print(f"\n\n{BOLD}{CYAN}╔{'═' * 68}╗{RESET}")
    print(f"{BOLD}{CYAN}║  LIS MULTI-DISTRO END-TO-END RESULTS{' ' * 32}║{RESET}")
    print(f"{BOLD}{CYAN}╚{'═' * 68}╝{RESET}\n")
    for distro, status in summary.items():
        label = {0: f"{GREEN}{TICK} PASSED{RESET}",
                 REFUSED: f"{YELLOW}{WARN_ICON} REFUSED (recipe not honorable "
                          f"on this distro){RESET}"}.get(
                     status, f"{RED}{CROSS} FAILED{RESET}")
        print(f"  • {distro.upper():<10} : {label}")
    failed = [d for d, s in summary.items() if s != 0]
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
