#!/usr/bin/env python3
"""lis2nixos — the default LIS → NixOS translator.

Usage: lis2nixos.py FILE.lis.json [--out DIR] [--strict]

Writes the classic NixOS trio into DIR (default '.'):
  disko.nix           — declarative partitioning for the disko module
  hardware.nix        — hardware-configuration-style module
  configuration.nix   — plain NixOS options only, no third-party modules

This is the *default acting as default*: opinionated flakes are expected to
ship their own translators. Core intent plain NixOS cannot express is
reported as a warning; with --strict any dropped intent exits non-zero
(SPEC §2.3).
"""

import argparse
import json
import pathlib
import sys

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"warning: {msg}", file=sys.stderr)


def nix_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def nix_list(items: list[str]) -> str:
    if not items:
        return "[ ]"
    return "[ " + " ".join(nix_str(i) for i in items) + " ]"


def disko_size(size: str) -> str:
    if size == "rest":
        return "100%"
    for unit, letter in (("MiB", "M"), ("GiB", "G"), ("TiB", "T")):
        if size.endswith(unit):
            return size[: -len(unit)] + letter
    if size.endswith("%"):
        return size
    raise ValueError(f"unparseable size: {size}")


# ── disko.nix ────────────────────────────────────────────────────

def fs_content(lines, pad, fs, mountpoint, mount_options, subvolumes):
    if fs in (None, "none"):
        return
    if fs == "swap":
        lines += [f"{pad}content = {{", f"{pad}  type = \"swap\";", f"{pad}}};"]
        return
    if fs == "zfs":
        warn("fs zfs is not supported by the default translator")
        return
    if fs == "btrfs" and subvolumes:
        lines += [f"{pad}content = {{",
                  f"{pad}  type = \"btrfs\";",
                  f"{pad}  extraArgs = [ \"-f\" ];",
                  f"{pad}  subvolumes = {{"]
        covered = any(s["mountpoint"] == mountpoint for s in subvolumes)
        if mountpoint and not covered:
            lines.append(f"{pad}    \"@\" = {{ mountpoint = {nix_str(mountpoint)}; }};")
        for sub in subvolumes:
            name = sub["name"] if sub["name"].startswith("@") else "@" + sub["name"]
            entry = f"{pad}    {nix_str(name)} = {{ mountpoint = {nix_str(sub['mountpoint'])};"
            if sub.get("mount_options"):
                entry += f" mountOptions = {nix_list(sub['mount_options'])};"
            lines.append(entry + " };")
        lines += [f"{pad}  }};", f"{pad}}};"]
        return
    lines += [f"{pad}content = {{",
              f"{pad}  type = \"filesystem\";",
              f"{pad}  format = {nix_str('fat32' if fs == 'vfat' else fs)};"]
    if mountpoint:
        lines.append(f"{pad}  mountpoint = {nix_str(mountpoint)};")
    if mount_options:
        lines.append(f"{pad}  mountOptions = {nix_list(mount_options)};")
    lines.append(f"{pad}}};")


def render_disko(doc: dict) -> str:
    storage = doc.get("storage")
    if not storage:
        raise SystemExit("error: document has no storage section — nothing to generate")
    target = doc.get("target", {}) or {}
    partitions = storage.get("partitions", [])
    encryption = storage.get("encryption", []) or []
    lvm = storage.get("lvm", []) or []

    disk_paths = {}
    for disk in target.get("disks", []):
        path = (disk.get("match", {}) or {}).get("path")
        if path:
            disk_paths[disk["id"]] = path
        else:
            warn(f"disk '{disk['id']}' matches by rule, not path; disko needs a "
                 "concrete device — resolve the match before translating")

    luks_over = {c["over"]: c for c in encryption}
    vg_of = {}
    for group in lvm:
        for dev in group.get("devices", []):
            part = next((c["over"] for c in encryption if c["id"] == dev), dev)
            vg_of[part] = group["name"]

    out = ["# Generated from a LIS document by lis2nixos (default translator).",
           "{", "  disko.devices = {", "    disk = {"]
    for disk in target.get("disks", []):
        path = disk_paths.get(disk["id"])
        if not path:
            continue
        out += [f"      {nix_str(disk['id'])} = {{",
                "        type = \"disk\";",
                f"        device = {nix_str(path)};",
                "        content = {", "          type = \"gpt\";",
                "          partitions = {"]
        index = 0
        for part in [p for p in partitions if p["disk"] == disk["id"]]:
            if part.get("existing"):
                warn(f"partition adoption ('existing') on disk '{disk['id']}' "
                     "is not supported by the default translator")
                continue
            index += 1
            name = part.get("id") or f"{part.get('role', 'part')}{index}"
            out.append(f"            {nix_str(name)} = {{")
            if part.get("size"):
                out.append(f"              size = {nix_str(disko_size(part['size']))};")
            if part.get("role") == "esp":
                mp = part.get("mountpoint", "/boot")
                out += ["              type = \"EF00\";",
                        "              content = {",
                        "                type = \"filesystem\";",
                        "                format = \"vfat\";",
                        f"                mountpoint = {nix_str(mp)};",
                        "                mountOptions = [ \"umask=0077\" ];",
                        "              };"]
            else:
                part_id = part.get("id", "")
                crypt = luks_over.get(part_id)
                pad = "              "
                if crypt:
                    out += [f"{pad}content = {{",
                            f"{pad}  type = \"luks\";",
                            f"{pad}  name = {nix_str(crypt['id'])};",
                            f"{pad}  settings.allowDiscards = true;"]
                    pad += "  "
                if part_id in vg_of:
                    out += [f"{pad}content = {{",
                            f"{pad}  type = \"lvm_pv\";",
                            f"{pad}  vg = {nix_str(vg_of[part_id])};",
                            f"{pad}}};"]
                else:
                    mp = part.get("mountpoint") or ("/" if part.get("role") == "root" else None)
                    fs = part.get("fs") or ("swap" if part.get("role") == "swap" else None)
                    fs_content(out, pad, fs, mp, part.get("mount_options", []),
                               part.get("subvolumes", []))
                if crypt:
                    out.append("              };")
            out.append("            };")
        out += ["          };", "        };", "      };"]
    out.append("    };")

    if lvm:
        out.append("    lvm_vg = {")
        for group in lvm:
            out += [f"      {nix_str(group['name'])} = {{",
                    "        type = \"lvm_vg\";", "        lvs = {"]
            for vol in group.get("volumes", []):
                out.append(f"          {nix_str(vol['name'])} = {{")
                out.append(f"            size = {nix_str(disko_size(vol.get('size', 'rest')))};")
                fs_content(out, "            ", vol.get("fs"), vol.get("mountpoint"),
                           vol.get("mount_options", []), vol.get("subvolumes", []))
                out.append("          };")
            out += ["        };", "      };"]
        out.append("    };")
    if storage.get("raid"):
        warn("raid arrays are not supported by the default translator yet")
    out += ["  };", "}"]
    return "\n".join(out) + "\n"


# ── hardware.nix ─────────────────────────────────────────────────

def render_hardware(doc: dict) -> str:
    boot = doc.get("boot", {}) or {}
    kernel = boot.get("kernel", {}) or {}
    initramfs = boot.get("initramfs", {}) or {}
    drivers = doc.get("drivers", {}) or {}
    arch = (doc.get("target", {}) or {}).get("arch", "x86_64")

    initrd = ["ahci", "xhci_pci", "nvme", "usb_storage", "sd_mod"]
    for module in initramfs.get("include_modules", []):
        if module not in initrd:
            initrd.append(module)

    out = ["# Generated from a LIS document by lis2nixos (default translator).",
           "{ config, lib, pkgs, modulesPath, ... }:", "", "{",
           "  imports = [ (modulesPath + \"/installer/scan/not-detected.nix\") ];", "",
           f"  boot.initrd.availableKernelModules = {nix_list(initrd)};",
           f"  boot.kernelModules = {nix_list(kernel.get('modules', []))};"]
    if kernel.get("blacklist"):
        out.append(f"  boot.blacklistedKernelModules = {nix_list(kernel['blacklist'])};")
    if kernel.get("params"):
        out.append(f"  boot.kernelParams = {nix_list(kernel['params'])};")
    if drivers.get("microcode") in ("intel", "amd"):
        out.append(f"  hardware.cpu.{drivers['microcode']}.updateMicrocode = true;")
    firmware_on = drivers.get("firmware") != "none"
    out.append(f"  hardware.enableRedistributableFirmware = {str(firmware_on).lower()};")
    platform = {"x86_64": "x86_64-linux", "aarch64": "aarch64-linux",
                "riscv64": "riscv64-linux"}[arch]
    out += [f"  nixpkgs.hostPlatform = lib.mkDefault {nix_str(platform)};", "}"]
    return "\n".join(out) + "\n"


# ── configuration.nix ────────────────────────────────────────────

def render_configuration(doc: dict) -> str:
    system = doc.get("system", {}) or {}
    boot = doc.get("boot", {}) or {}
    network = doc.get("network", {}) or {}
    software = doc.get("software", {}) or {}
    desktop = doc.get("desktop")
    storage = doc.get("storage", {}) or {}

    out = ["# Generated from a LIS document by lis2nixos (default translator).",
           "# Pair with disko.nix (via the disko module) and hardware.nix.",
           "{ config, lib, pkgs, ... }:", "", "{",
           "  imports = [ ./hardware.nix ];", ""]

    if boot.get("loader") == "grub":
        out += ["  boot.loader.grub.enable = true;",
                "  boot.loader.grub.efiSupport = true;",
                "  boot.loader.grub.device = \"nodev\";"]
    else:
        out += ["  boot.loader.systemd-boot.enable = true;",
                "  boot.loader.efi.canTouchEfiVariables = true;"]
    if boot.get("timeout") is not None:
        out.append(f"  boot.loader.timeout = {boot['timeout']};")
    out.append("")

    if system.get("hostname"):
        out.append(f"  networking.hostName = {nix_str(system['hostname'])};")
    if system.get("domain"):
        out.append(f"  networking.domain = {nix_str(system['domain'])};")
    if system.get("timezone"):
        out.append(f"  time.timeZone = {nix_str(system['timezone'])};")
    if system.get("hwclock") == "localtime":
        out.append("  time.hardwareClockInLocalTime = true;")
    if system.get("locale"):
        out.append(f"  i18n.defaultLocale = {nix_str(system['locale'])};")
    for key, value in (system.get("locale_overrides", {}) or {}).items():
        out.append(f"  i18n.extraLocaleSettings.{key} = {nix_str(value)};")
    keymap = system.get("keymap", {}) or {}
    if keymap.get("console"):
        out.append(f"  console.keyMap = {nix_str(keymap['console'])};")
    if keymap.get("font"):
        out.append(f"  console.font = {nix_str(keymap['font'])};")
    if keymap.get("layout"):
        out.append(f"  services.xserver.xkb.layout = {nix_str(keymap['layout'])};")
        if keymap.get("variant"):
            out.append(f"  services.xserver.xkb.variant = {nix_str(keymap['variant'])};")
    time_cfg = system.get("time", {}) or {}
    if time_cfg.get("servers"):
        out.append(f"  networking.timeServers = {nix_list(time_cfg['servers'])};")
    if time_cfg.get("provider") == "chrony":
        out.append("  services.chrony.enable = true;")
    elif time_cfg.get("provider") == "openntpd":
        out.append("  services.openntpd.enable = true;")
    if time_cfg.get("ntp") is False:
        out.append("  services.timesyncd.enable = false;")
    if system.get("init") not in (None, "systemd", "auto"):
        warn("system.init: NixOS is systemd-only (no-silent-drift: refused)")
    out.append("")

    manager = network.get("manager", "auto")
    if manager in ("auto", "networkmanager"):
        out.append("  networking.networkmanager.enable = true;")
    elif manager == "systemd-networkd":
        out.append("  networking.useNetworkd = true;")
    elif manager == "iwd":
        out.append("  networking.wireless.iwd.enable = true;")
    if network.get("interfaces"):
        warn("static interface configuration is emitted as a comment — review networking.* options")
        out.append("  # LIS network.interfaces were declared; map them to networking.interfaces.<name>.")
    if network.get("wifi"):
        warn("wifi networks are not emitted (NetworkManager profiles are stateful)")
    for entry in network.get("hosts", []) or []:
        out.append(f"  networking.hosts.{nix_str(entry['ip'])} = {nix_list(entry['names'])};")
    firewall = network.get("firewall")
    if firewall:
        if "enabled" in firewall:
            out.append(f"  networking.firewall.enable = {str(firewall['enabled']).lower()};")
        tcp = [p.split("/")[0] for p in firewall.get("allow_ports", []) if p.endswith("/tcp")]
        udp = [p.split("/")[0] for p in firewall.get("allow_ports", []) if p.endswith("/udp")]
        if tcp:
            out.append(f"  networking.firewall.allowedTCPPorts = [ {' '.join(tcp)} ];")
        if udp:
            out.append(f"  networking.firewall.allowedUDPPorts = [ {' '.join(udp)} ];")
    ssh = network.get("ssh", {}) or {}
    if ssh.get("enabled"):
        out.append("  services.openssh.enable = true;")
        if "password_auth" in ssh:
            out.append(f"  services.openssh.settings.PasswordAuthentication = {str(ssh['password_auth']).lower()};")
        if ssh.get("permit_root"):
            out.append(f"  services.openssh.settings.PermitRootLogin = {nix_str(ssh['permit_root'])};")
    proxy = doc.get("proxy", {}) or {}
    if proxy.get("http"):
        out.append(f"  networking.proxy.default = {nix_str(proxy['http'])};")
    if proxy.get("no_proxy"):
        out.append(f"  networking.proxy.noProxy = {nix_str(','.join(proxy['no_proxy']))};")
    out.append("")

    wheel_nopasswd = False
    for user in doc.get("users", []):
        out.append(f"  users.users.{user['name']} = {{")
        if user["name"] != "root":
            out.append("    isNormalUser = true;")
        if user.get("uid") is not None:
            out.append(f"    uid = {user['uid']};")
        if user.get("comment"):
            out.append(f"    description = {nix_str(user['comment'])};")
        groups = list(user.get("groups", []))
        if user.get("admin") and "wheel" not in groups:
            groups.insert(0, "wheel")
        if user["name"] != "root" and groups:
            out.append(f"    extraGroups = {nix_list(groups)};")
        password = user.get("password") or {}
        if password.get("locked"):
            out.append("    hashedPassword = \"!\";")
        elif password.get("hash"):
            out.append(f"    hashedPassword = {nix_str(password['hash'])};")
        if user.get("ssh_authorized_keys"):
            out.append(f"    openssh.authorizedKeys.keys = {nix_list(user['ssh_authorized_keys'])};")
        shell = user.get("shell")
        if shell in ("zsh", "fish"):
            out.append(f"    shell = pkgs.{shell};")
        elif shell and shell.startswith("/"):
            out.append(f"    shell = {nix_str(shell)};")
        elif shell and shell != "bash":
            warn(f"unknown shell intent {shell!r} for user {user['name']}")
        if user.get("dotfiles"):
            warn(f"users[{user['name']}].dotfiles is not applied by the default translator")
        out.append("  };")
        if user.get("sudo") == "nopasswd":
            wheel_nopasswd = True
        if shell in ("zsh", "fish"):
            out.append(f"  programs.{shell}.enable = true;")
    if wheel_nopasswd:
        out.append("  security.sudo.wheelNeedsPassword = false;")
    out.append("")

    role = software.get("role", "")
    role_map = {
        "desktop:gnome": ["  services.xserver.enable = true;",
                          "  services.xserver.displayManager.gdm.enable = true;",
                          "  services.xserver.desktopManager.gnome.enable = true;"],
        "desktop:kde": ["  services.xserver.enable = true;",
                        "  services.displayManager.sddm.enable = true;",
                        "  services.desktopManager.plasma6.enable = true;"],
        "desktop:xfce": ["  services.xserver.enable = true;",
                         "  services.xserver.desktopManager.xfce.enable = true;"],
        "desktop:sway": ["  programs.sway.enable = true;"],
        "desktop:hyprland": ["  programs.hyprland.enable = true;"],
    }
    if role in role_map:
        out += role_map[role]
    elif role not in ("", "minimal", "server"):
        warn(f"role {role!r} has no default-translator mapping")
    if software.get("packages"):
        out += ["  # Package names pass through verbatim; unresolvable names fail the build.",
                f"  environment.systemPackages = with pkgs; [ {' '.join(software['packages'])} ];"]
    if software.get("exclude"):
        warn("software.exclude has no NixOS equivalent (roles are additive)")
    services = software.get("services", {}) or {}
    for unit in services.get("enable", []):
        mapped = {"sshd": None, "tailscaled": "  services.tailscale.enable = true;",
                  "docker": "  virtualisation.docker.enable = true;"}.get(unit, "?")
        if mapped == "?":
            warn(f"services.enable {unit!r} has no default mapping — add the module option yourself")
        elif mapped:
            out.append(mapped)
    for unit in services.get("disable", []):
        warn(f"services.disable {unit!r} is not mapped by the default translator")
    if software.get("flatpak"):
        out.append("  services.flatpak.enable = true;")
        warn("flatpak app installation happens at runtime, not in configuration")
    if software.get("snap"):
        warn("snaps are not supported on NixOS (no-silent-drift: refused)")

    if desktop:
        audio = desktop.get("audio", "auto")
        if audio in ("auto", "pipewire"):
            out.append("  services.pipewire = { enable = true; alsa.enable = true; pulse.enable = true; };")
        elif audio == "pulseaudio":
            out.append("  services.pulseaudio.enable = true;")
        if desktop.get("bluetooth"):
            out.append("  hardware.bluetooth.enable = true;")
        if desktop.get("printing"):
            out.append("  services.printing.enable = true;")
        if desktop.get("autologin"):
            out += ["  services.displayManager.autoLogin.enable = true;",
                    f"  services.displayManager.autoLogin.user = {nix_str(desktop['autologin'])};"]

    drivers = doc.get("drivers", {}) or {}
    if drivers.get("gpu") in ("nvidia", "nvidia-open"):
        out += ["  services.xserver.videoDrivers = [ \"nvidia\" ];",
                f"  hardware.nvidia.open = {str(drivers['gpu'] == 'nvidia-open').lower()};"]

    for entry in doc.get("files", []):
        if entry["path"].startswith("/etc/"):
            rest = entry["path"][len("/etc/"):]
            out.append(f"  environment.etc.{nix_str(rest)}.text = {nix_str(entry['content'])};")
            if entry.get("mode"):
                out.append(f"  environment.etc.{nix_str(rest)}.mode = {nix_str(entry['mode'])};")
        else:
            warn(f"files[{entry['path']}] outside /etc is not expressible in configuration.nix")
    if doc.get("scripts"):
        warn("scripts are an installer concern; the default translator emits none")
    if doc.get("registration"):
        warn("registration does not apply to NixOS (no-silent-drift: refused)")
    if (storage.get("snapshots", {}) or {}).get("enabled"):
        out.append("  services.snapper.configs.root = { SUBVOLUME = \"/\"; TIMELINE_CREATE = true; TIMELINE_CLEANUP = true; };")
    swap = storage.get("swap", {}) or {}
    if swap.get("zram"):
        out.append("  zramSwap.enable = true;")
    if swap.get("file"):
        size = swap["file"]["size"]
        gib = int(size[:-3]) if size.endswith("GiB") else 4
        out.append(f"  swapDevices = [ {{ device = {nix_str(swap['file']['path'])}; size = {gib * 1024}; }} ];")
    if system.get("kdump"):
        warn("kdump has no simple NixOS default mapping")

    out += ["", "  # Pin to the release the generator targeted; do not blindly bump.",
            "  system.stateVersion = \"25.05\";", "}"]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("."))
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any core intent was dropped")
    args = ap.parse_args()

    doc = json.loads(args.file.read_text())
    if not str(doc.get("lis", "")).startswith("0.1."):
        sys.exit(f"unsupported LIS version: {doc.get('lis')!r}")

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "disko.nix").write_text(render_disko(doc))
    (args.out / "hardware.nix").write_text(render_hardware(doc))
    (args.out / "configuration.nix").write_text(render_configuration(doc))
    print(f"wrote {args.out}/disko.nix, hardware.nix, configuration.nix "
          f"({len(WARNINGS)} warning(s))")
    if args.strict and WARNINGS:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
