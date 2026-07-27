"""Main CLI Entrypoint for LIS End-To-End Automated VM Test Suite."""

import argparse
import copy
import json
import pathlib
import subprocess
import sys
from tools.e2e.colors import (
    BOLD, CYAN, GREEN, GRAY, MAGENTA, RED, WHITE,
    print_stage_header, TICK, CROSS, RESET
)
from tools.e2e.iso import download_iso_if_missing
from tools.e2e.installer import run_stage2_qemu_installer
from tools.e2e.verifier import verify_installed_disk, run_stage4_live_guest_verification


def run_single_distro_test(args) -> int:
    recipe_path = args.recipe.resolve()
    if not recipe_path.exists():
        sys.exit(f"{RED}error: recipe file '{recipe_path}' not found{RESET}")

    recipe_data = json.loads(recipe_path.read_text())

    build_dir = pathlib.Path("/home/bresilla/lis/build")
    build_dir.mkdir(exist_ok=True)
    target_disk = build_dir / f"e2e-{args.distro}-target.qcow2"
    seed_disk = build_dir / f"e2e-LIS-{args.distro}.img"

    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║     LIS END-TO-END AUTOMATED VM TEST SUITE — {args.distro.upper():<15} ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════════════════════════╝{RESET}")

    # STAGE 1: SPECIFICATION & EXPECTATIONS BREAKDOWN
    print_stage_header(1, "Recipe Intent Breakdown & Expectations")
    print(f"  {BOLD}• Recipe Name{RESET}: {CYAN}{recipe_data.get('meta', {}).get('name')}{RESET}")
    print(f"  {BOLD}• Target Architecture{RESET}: {WHITE}{recipe_data.get('target', {}).get('arch')}{RESET} ({recipe_data.get('target', {}).get('firmware')})")
    
    storage = recipe_data.get("storage", {})
    parts = storage.get("partitions", [])
    print(f"  {BOLD}• Filesystem Layout{RESET}: {len(parts)} partitions defined")
    for p in parts:
        role = p.get("role")
        fs = p.get("fs")
        size = p.get("size")
        mp = p.get("mountpoint", "")
        print(f"    {GRAY}└─[{role}]{RESET} {fs.upper():<6} {size:<6} -> {CYAN}{mp}{RESET}")

    user_info = recipe_data.get("users", [{}])[0]
    print(f"  {BOLD}• User Account{RESET}: {GREEN}{user_info.get('name')}{RESET} (shell: {user_info.get('shell')}, groups: {', '.join(user_info.get('groups', []))})")
    
    sw = recipe_data.get("software", {})
    print(f"  {BOLD}• Desktop Role{RESET}: {MAGENTA}{sw.get('role')}{RESET}")
    print(f"  {BOLD}• Apps & Packages{RESET}: {WHITE}{', '.join(sw.get('apps', []) + sw.get('packages', []))}{RESET}")

    hooks = recipe_data.get("scripts", {})
    print(f"  {BOLD}• System Script Hooks{RESET}: pre_install ({len(hooks.get('pre_install', []))}), post_install ({len(hooks.get('post_install', []))}), firstboot ({len(hooks.get('firstboot', []))})")

    if args.verify_only:
        verify_installed_disk(target_disk, recipe_data)
        return run_stage4_live_guest_verification(args, target_disk, recipe_data)

    # STAGE 2: AUTOMATED ISO RESOLUTION & SEED GENERATION
    iso_path = args.iso or download_iso_if_missing(args.distro)

    if target_disk.exists():
        target_disk.unlink()
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(target_disk), args.disk_size], check=True)
    print(f"  [{TICK}] Created {args.disk_size} target virtual disk: {BOLD}{target_disk}{RESET}")

    make_seed = pathlib.Path(__file__).resolve().parent.parent / "lis-make-seed"
    subprocess.run([sys.executable, str(make_seed), str(recipe_path), "--out", str(seed_disk), "--unattended"], check=True)
    print(f"  [{TICK}] Created FAT32 LIS seed volume image: {BOLD}{seed_disk}{RESET}")

    run_stage2_qemu_installer(args.distro, target_disk, seed_disk, iso_path, args.ram)

    # STAGE 3: POST-INSTALL VERIFICATION
    verify_installed_disk(target_disk, recipe_data)

    # STAGE 4: REBOOT TEST & LIVE GUEST SPEC VERIFICATION
    return run_stage4_live_guest_verification(args, target_disk, recipe_data)


def main() -> int:
    ap = argparse.ArgumentParser(description="Standalone Automated LIS VM Test Engine.")
    ap.add_argument("--distro", choices=["nixos", "ubuntu", "arch", "fedora", "suse", "debian", "alpine", "all"], required=True, help="Distro to test (or 'all')")
    ap.add_argument("--iso", type=pathlib.Path, help="Path to Live ISO")
    ap.add_argument("--recipe", type=pathlib.Path, default=pathlib.Path("docs/examples/test-full-install.lis.json"), help="Path to LIS recipe")
    ap.add_argument("--ram", default="4G", help="RAM (default: 4G)")
    ap.add_argument("--disk-size", default="20G", help="Target disk size (default: 20G)")
    ap.add_argument("--verify-only", action="store_true", help="Skip ISO boot and verify existing target.qcow2 disk")
    args = ap.parse_args()

    if args.distro == "all":
        distros = ["alpine", "nixos", "ubuntu", "arch", "fedora", "suse", "debian"]
        summary = {}
        for d in distros:
            subprocess.run("rm -f /home/bresilla/lis/build/*.qcow2 /home/bresilla/lis/build/*.img", shell=True)
            print(f"\n{BOLD}{CYAN}============================================================{RESET}")
            print(f"{BOLD}{CYAN}  STARTING E2E VM TEST SUITE FOR DISTRO: {d.upper()}{RESET}")
            print(f"{BOLD}{CYAN}============================================================{RESET}")
            sub_args = copy.copy(args)
            sub_args.distro = d
            res = run_single_distro_test(sub_args)
            summary[d] = res
            subprocess.run(f"rm -f /home/bresilla/lis/build/e2e-{d}-target.qcow2 /home/bresilla/lis/build/e2e-LIS-{d}.img", shell=True)
        
        print(f"\n\n{BOLD}{CYAN}╔══════════════════════════════════════════════════════════════════════╗{RESET}")
        print(f"{BOLD}{CYAN}║           LIS MULTI-DISTRO END-TO-END SUITE RESULTS MATRIX           ║{RESET}")
        print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════════════════════════╝{RESET}\n")
        for d, passed in summary.items():
            status_str = f"{GREEN}{TICK} PASSED{RESET}" if passed == 0 else f"{RED}{CROSS} FAILED{RESET}"
            print(f"  • {d.upper():<10} : {status_str}")
        print(f"\n{BOLD}{GREEN}============================================================{RESET}\n")
        return 0

    return run_single_distro_test(args)


if __name__ == "__main__":
    sys.exit(main())
