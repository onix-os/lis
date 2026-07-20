//! The default LIS → NixOS translator.
//!
//! Produces the classic NixOS trio from a LIS document:
//! `disko.nix` (declarative partitioning for the [disko](https://github.com/nix-community/disko)
//! module), `hardware.nix` (a hardware-configuration-style module), and
//! `configuration.nix` (plain NixOS options only — no third-party module
//! system, no opinions). Opinionated flakes are expected to ship their own
//! translators; this one is the *default acting as default*.
//!
//! Translator stance on §2.3: intent that plain NixOS cannot express is
//! reported in [`NixosOutput::warnings`] rather than silently dropped;
//! callers (like the `lis2nixos` binary with `--strict`) decide whether
//! warnings are fatal.

use std::fmt::Write as _;

use crate::document::*;
use crate::size::Size;

/// The three generated files plus every §2.3 warning raised on the way.
#[derive(Debug, Clone)]
pub struct NixosOutput {
    pub disko: String,
    pub hardware: String,
    pub configuration: String,
    pub warnings: Vec<String>,
}

/// Translate a document. Errors only on inputs no NixOS system can be
/// generated from (no rooted storage, un-pathed disks); everything else
/// degrades to a warning.
pub fn translate(doc: &Document) -> Result<NixosOutput, String> {
    let mut warnings = Vec::new();
    let disko = render_disko(doc, &mut warnings)?;
    let hardware = render_hardware(doc);
    let configuration = render_configuration(doc, &mut warnings);
    Ok(NixosOutput {
        disko,
        hardware,
        configuration,
        warnings,
    })
}

fn nix_str(s: &str) -> String {
    format!("\"{}\"", s.replace('\\', "\\\\").replace('"', "\\\""))
}

fn nix_list(items: &[String]) -> String {
    if items.is_empty() {
        return "[ ]".to_string();
    }
    let mut out = String::from("[ ");
    for item in items {
        out.push_str(&nix_str(item));
        out.push(' ');
    }
    out.push(']');
    out
}

fn disko_size(size: &Size) -> String {
    match size {
        Size::MiB(n) => format!("{n}M"),
        Size::GiB(n) => format!("{n}G"),
        Size::TiB(n) => format!("{n}T"),
        Size::Percent(n) => format!("{n}%"),
        Size::Rest => "100%".to_string(),
    }
}

fn fs_name(fs: Fs) -> &'static str {
    match fs {
        Fs::Ext4 => "ext4",
        Fs::Btrfs => "btrfs",
        Fs::Xfs => "xfs",
        Fs::F2fs => "f2fs",
        Fs::Zfs => "zfs",
        Fs::Vfat => "vfat",
        Fs::Swap => "swap",
        Fs::None => "none",
    }
}

/// A filesystem/mount body shared by partitions and LVM volumes.
struct FsSpec<'a> {
    fs: Option<Fs>,
    mountpoint: Option<String>,
    mount_options: &'a [String],
    subvolumes: &'a [Subvolume],
}

fn render_fs_content(spec: &FsSpec, pad: &str, out: &mut String, warnings: &mut Vec<String>) {
    match spec.fs {
        None | Some(Fs::None) => {}
        Some(Fs::Swap) => {
            let _ = writeln!(out, "{pad}content = {{");
            let _ = writeln!(out, "{pad}  type = \"swap\";");
            let _ = writeln!(out, "{pad}}};");
        }
        Some(Fs::Btrfs) if !spec.subvolumes.is_empty() => {
            let _ = writeln!(out, "{pad}content = {{");
            let _ = writeln!(out, "{pad}  type = \"btrfs\";");
            let _ = writeln!(out, "{pad}  extraArgs = [ \"-f\" ];");
            let _ = writeln!(out, "{pad}  subvolumes = {{");
            let covered_self = spec
                .subvolumes
                .iter()
                .any(|s| Some(&s.mountpoint) == spec.mountpoint.as_ref());
            if !covered_self {
                if let Some(mp) = &spec.mountpoint {
                    let _ = writeln!(out, "{pad}    \"@\" = {{ mountpoint = {}; }};", nix_str(mp));
                }
            }
            for sub in spec.subvolumes {
                let name = if sub.name.starts_with('@') {
                    sub.name.clone()
                } else {
                    format!("@{}", sub.name)
                };
                let _ = write!(out, "{pad}    {} = {{ mountpoint = {};", nix_str(&name), nix_str(&sub.mountpoint));
                if !sub.mount_options.is_empty() {
                    let _ = write!(out, " mountOptions = {};", nix_list(&sub.mount_options));
                }
                let _ = writeln!(out, " }};");
            }
            let _ = writeln!(out, "{pad}  }};");
            let _ = writeln!(out, "{pad}}};");
        }
        Some(Fs::Zfs) => {
            warnings.push("fs zfs is not supported by the default translator".to_string());
        }
        Some(fs) => {
            let _ = writeln!(out, "{pad}content = {{");
            let _ = writeln!(out, "{pad}  type = \"filesystem\";");
            let _ = writeln!(out, "{pad}  format = {};", nix_str(fs_name(fs)));
            if let Some(mp) = &spec.mountpoint {
                let _ = writeln!(out, "{pad}  mountpoint = {};", nix_str(mp));
            }
            if !spec.mount_options.is_empty() {
                let _ = writeln!(out, "{pad}  mountOptions = {};", nix_list(spec.mount_options));
            }
            let _ = writeln!(out, "{pad}}};");
        }
    }
}

fn render_disko(doc: &Document, warnings: &mut Vec<String>) -> Result<String, String> {
    let Some(storage) = &doc.storage else {
        return Err("document has no storage section — nothing to generate".to_string());
    };
    let empty_target = Target::default();
    let target = doc.target.as_ref().unwrap_or(&empty_target);
    let mut disk_paths = std::collections::BTreeMap::new();
    for disk in &target.disks {
        match &disk.matcher.path {
            Some(path) => {
                disk_paths.insert(disk.id.as_str(), path.as_str());
            }
            None => warnings.push(format!(
                "disk '{}' matches by rule, not path; disko needs a concrete device — resolve the match before translating",
                disk.id
            )),
        }
    }

    // Encryption containers by the partition they sit over.
    let mut luks_over: std::collections::BTreeMap<&str, &Encryption> = Default::default();
    for crypt in &storage.encryption {
        luks_over.insert(crypt.over.as_str(), crypt);
    }
    // Which LVM vg (if any) claims each partition id (directly or via LUKS).
    let mut vg_of: std::collections::BTreeMap<&str, &str> = Default::default();
    for group in &storage.lvm {
        for dev in &group.devices {
            let part = storage
                .encryption
                .iter()
                .find(|c| c.id == *dev)
                .map(|c| c.over.as_str())
                .unwrap_or(dev.as_str());
            vg_of.insert(part, group.name.as_str());
        }
    }

    let mut out = String::new();
    out.push_str("# Generated from a LIS document by lis2nixos (default translator).\n");
    out.push_str("{\n  disko.devices = {\n    disk = {\n");

    for disk in &target.disks {
        let Some(path) = disk_paths.get(disk.id.as_str()) else { continue };
        let _ = writeln!(out, "      {} = {{", nix_str(&disk.id));
        let _ = writeln!(out, "        type = \"disk\";");
        let _ = writeln!(out, "        device = {};", nix_str(path));
        out.push_str("        content = {\n          type = \"gpt\";\n          partitions = {\n");
        let mut index = 0;
        for part in storage.partitions.iter().filter(|p| p.disk == disk.id) {
            if part.existing.is_some() {
                warnings.push(format!(
                    "partition adoption ('existing') on disk '{}' is not supported by the default translator",
                    disk.id
                ));
                continue;
            }
            index += 1;
            let name = part
                .id
                .clone()
                .unwrap_or_else(|| format!("{}{}", part.role.map(role_name).unwrap_or("part"), index));
            let _ = writeln!(out, "            {} = {{", nix_str(&name));
            if let Some(size) = &part.size {
                let _ = writeln!(out, "              size = {};", nix_str(&disko_size(size)));
            }
            match part.role {
                Some(Role::Esp) => {
                    let mp = part.mountpoint.clone().unwrap_or_else(|| "/boot".to_string());
                    out.push_str("              type = \"EF00\";\n");
                    out.push_str("              content = {\n");
                    out.push_str("                type = \"filesystem\";\n");
                    out.push_str("                format = \"vfat\";\n");
                    let _ = writeln!(out, "                mountpoint = {};", nix_str(&mp));
                    out.push_str("                mountOptions = [ \"umask=0077\" ];\n");
                    out.push_str("              };\n");
                }
                _ => {
                    let part_id = part.id.as_deref().unwrap_or("");
                    let vg = vg_of.get(part_id).copied();
                    let luks = luks_over.get(part_id).copied();
                    let inner_pad;
                    if let Some(crypt) = luks {
                        let _ = writeln!(out, "              content = {{");
                        let _ = writeln!(out, "                type = \"luks\";");
                        let _ = writeln!(out, "                name = {};", nix_str(&crypt.id));
                        out.push_str("                settings.allowDiscards = true;\n");
                        inner_pad = "                ";
                    } else {
                        inner_pad = "              ";
                    }
                    if let Some(vg) = vg {
                        let _ = writeln!(out, "{inner_pad}content = {{");
                        let _ = writeln!(out, "{inner_pad}  type = \"lvm_pv\";");
                        let _ = writeln!(out, "{inner_pad}  vg = {};", nix_str(vg));
                        let _ = writeln!(out, "{inner_pad}}};");
                    } else {
                        let mp = part.mountpoint.clone().or_else(|| {
                            (part.role == Some(Role::Root)).then(|| "/".to_string())
                        });
                        render_fs_content(
                            &FsSpec {
                                fs: part.fs.or(match part.role {
                                    Some(Role::Swap) => Some(Fs::Swap),
                                    _ => None,
                                }),
                                mountpoint: mp,
                                mount_options: &part.mount_options,
                                subvolumes: &part.subvolumes,
                            },
                            inner_pad,
                            &mut out,
                            warnings,
                        );
                    }
                    if luks.is_some() {
                        out.push_str("              };\n");
                    }
                }
            }
            out.push_str("            };\n");
        }
        out.push_str("          };\n        };\n      };\n");
    }
    out.push_str("    };\n");

    if !storage.lvm.is_empty() {
        out.push_str("    lvm_vg = {\n");
        for group in &storage.lvm {
            let _ = writeln!(out, "      {} = {{", nix_str(&group.name));
            out.push_str("        type = \"lvm_vg\";\n        lvs = {\n");
            for vol in &group.volumes {
                let _ = writeln!(out, "          {} = {{", nix_str(&vol.name));
                let size = vol.size.unwrap_or(Size::Rest);
                let _ = writeln!(out, "            size = {};", nix_str(&disko_size(&size)));
                render_fs_content(
                    &FsSpec {
                        fs: vol.fs,
                        mountpoint: vol.mountpoint.clone(),
                        mount_options: &vol.mount_options,
                        subvolumes: &vol.subvolumes,
                    },
                    "            ",
                    &mut out,
                    warnings,
                );
                out.push_str("          };\n");
            }
            out.push_str("        };\n      };\n");
        }
        out.push_str("    };\n");
    }
    if !storage.raid.is_empty() {
        warnings.push("raid arrays are not supported by the default translator yet".to_string());
    }
    out.push_str("  };\n}\n");
    Ok(out)
}

fn role_name(role: Role) -> &'static str {
    match role {
        Role::Esp => "esp",
        Role::Boot => "boot",
        Role::Root => "root",
        Role::Swap => "swap",
        Role::Data => "data",
        Role::Raw => "raw",
    }
}

fn render_hardware(doc: &Document) -> String {
    let boot = doc.boot.clone().unwrap_or_default();
    let kernel = boot.kernel.clone().unwrap_or_default();
    let initramfs = boot.initramfs.clone().unwrap_or_default();
    let drivers = doc.drivers.clone().unwrap_or_default();
    let arch = doc
        .target
        .as_ref()
        .and_then(|t| t.arch)
        .unwrap_or(Arch::X86_64);

    let mut initrd = vec![
        "ahci".to_string(),
        "xhci_pci".to_string(),
        "nvme".to_string(),
        "usb_storage".to_string(),
        "sd_mod".to_string(),
    ];
    for module in &initramfs.include_modules {
        if !initrd.contains(module) {
            initrd.push(module.clone());
        }
    }

    let mut out = String::new();
    out.push_str("# Generated from a LIS document by lis2nixos (default translator).\n");
    out.push_str("{ config, lib, pkgs, modulesPath, ... }:\n\n{\n");
    out.push_str("  imports = [ (modulesPath + \"/installer/scan/not-detected.nix\") ];\n\n");
    let _ = writeln!(out, "  boot.initrd.availableKernelModules = {};", nix_list(&initrd));
    let _ = writeln!(out, "  boot.kernelModules = {};", nix_list(&kernel.modules));
    if !kernel.blacklist.is_empty() {
        let _ = writeln!(out, "  boot.blacklistedKernelModules = {};", nix_list(&kernel.blacklist));
    }
    if !kernel.params.is_empty() {
        let _ = writeln!(out, "  boot.kernelParams = {};", nix_list(&kernel.params));
    }
    match drivers.microcode {
        Some(Microcode::Intel) => {
            out.push_str("  hardware.cpu.intel.updateMicrocode = true;\n");
        }
        Some(Microcode::Amd) => {
            out.push_str("  hardware.cpu.amd.updateMicrocode = true;\n");
        }
        _ => {}
    }
    let firmware_on = !matches!(drivers.firmware, Some(FirmwarePolicy::None));
    let _ = writeln!(out, "  hardware.enableRedistributableFirmware = {firmware_on};");
    let platform = match arch {
        Arch::X86_64 => "x86_64-linux",
        Arch::Aarch64 => "aarch64-linux",
        Arch::Riscv64 => "riscv64-linux",
    };
    let _ = writeln!(out, "  nixpkgs.hostPlatform = lib.mkDefault {};", nix_str(platform));
    out.push_str("}\n");
    out
}

fn render_configuration(doc: &Document, warnings: &mut Vec<String>) -> String {
    let system = doc.system.clone().unwrap_or_default();
    let boot = doc.boot.clone().unwrap_or_default();
    let network = doc.network.clone().unwrap_or_default();
    let software = doc.software.clone().unwrap_or_default();
    let desktop = doc.desktop.clone();

    let mut out = String::new();
    out.push_str("# Generated from a LIS document by lis2nixos (default translator).\n");
    out.push_str("# Pair with disko.nix (via the disko module) and hardware.nix.\n");
    out.push_str("{ config, lib, pkgs, ... }:\n\n{\n");
    out.push_str("  imports = [ ./hardware.nix ];\n\n");

    // Boot loader.
    match boot.loader.unwrap_or_default() {
        Loader::Grub => {
            out.push_str("  boot.loader.grub.enable = true;\n");
            out.push_str("  boot.loader.grub.efiSupport = true;\n");
            out.push_str("  boot.loader.grub.device = \"nodev\";\n");
        }
        _ => {
            out.push_str("  boot.loader.systemd-boot.enable = true;\n");
            out.push_str("  boot.loader.efi.canTouchEfiVariables = true;\n");
        }
    }
    if let Some(timeout) = boot.timeout {
        let _ = writeln!(out, "  boot.loader.timeout = {timeout};");
    }
    out.push('\n');

    // Identity, time, locale.
    if let Some(hostname) = &system.hostname {
        let _ = writeln!(out, "  networking.hostName = {};", nix_str(hostname));
    }
    if let Some(domain) = &system.domain {
        let _ = writeln!(out, "  networking.domain = {};", nix_str(domain));
    }
    if let Some(tz) = &system.timezone {
        let _ = writeln!(out, "  time.timeZone = {};", nix_str(tz));
    }
    if system.hwclock == Some(HwClock::Localtime) {
        out.push_str("  time.hardwareClockInLocalTime = true;\n");
    }
    if let Some(locale) = &system.locale {
        let _ = writeln!(out, "  i18n.defaultLocale = {};", nix_str(locale));
    }
    if !system.locale_overrides.is_empty() {
        out.push_str("  i18n.extraLocaleSettings = {\n");
        for (key, value) in &system.locale_overrides {
            let _ = writeln!(out, "    {key} = {};", nix_str(value));
        }
        out.push_str("  };\n");
    }
    if let Some(keymap) = &system.keymap {
        if let Some(console) = &keymap.console {
            let _ = writeln!(out, "  console.keyMap = {};", nix_str(console));
        }
        if let Some(font) = &keymap.font {
            let _ = writeln!(out, "  console.font = {};", nix_str(font));
        }
        if let Some(layout) = &keymap.layout {
            let _ = writeln!(out, "  services.xserver.xkb.layout = {};", nix_str(layout));
            if let Some(variant) = &keymap.variant {
                if !variant.is_empty() {
                    let _ = writeln!(out, "  services.xserver.xkb.variant = {};", nix_str(variant));
                }
            }
        }
    }
    if let Some(time) = &system.time {
        if !time.servers.is_empty() {
            let _ = writeln!(out, "  networking.timeServers = {};", nix_list(&time.servers));
        }
        match time.provider {
            Some(TimeProvider::Chrony) => out.push_str("  services.chrony.enable = true;\n"),
            Some(TimeProvider::Openntpd) => out.push_str("  services.openntpd.enable = true;\n"),
            _ => {}
        }
        if time.ntp == Some(false) {
            out.push_str("  services.timesyncd.enable = false;\n");
        }
    }
    if system.init.is_some() && system.init != Some(Init::Systemd) && system.init != Some(Init::Auto)
    {
        warnings.push("system.init: NixOS is systemd-only (no-silent-drift: refused)".to_string());
    }
    out.push('\n');

    // Network.
    let manager = network.manager.unwrap_or_default();
    match manager {
        NetworkManager::Auto | NetworkManager::Networkmanager => {
            out.push_str("  networking.networkmanager.enable = true;\n");
        }
        NetworkManager::SystemdNetworkd => {
            out.push_str("  networking.useNetworkd = true;\n");
        }
        NetworkManager::Iwd => {
            out.push_str("  networking.wireless.iwd.enable = true;\n");
        }
    }
    if !network.interfaces.is_empty() {
        warnings.push("static interface configuration is emitted as a comment — review networking.* options".to_string());
        out.push_str("  # LIS network.interfaces were declared; static addressing must be\n");
        out.push_str("  # mapped to networking.interfaces.<name> options for your NIC names.\n");
    }
    if !network.wifi.is_empty() {
        warnings.push("wifi networks are not emitted (NetworkManager profiles are stateful)".to_string());
    }
    if !network.hosts.is_empty() {
        out.push_str("  networking.hosts = {\n");
        for entry in &network.hosts {
            let _ = writeln!(out, "    {} = {};", nix_str(&entry.ip), nix_list(&entry.names));
        }
        out.push_str("  };\n");
    }
    if let Some(firewall) = &network.firewall {
        if let Some(enabled) = firewall.enabled {
            let _ = writeln!(out, "  networking.firewall.enable = {enabled};");
        }
        let (mut tcp, mut udp) = (Vec::new(), Vec::new());
        for port in &firewall.allow_ports {
            if let Some((num, proto)) = port.split_once('/') {
                match proto {
                    "tcp" => tcp.push(num.to_string()),
                    "udp" => udp.push(num.to_string()),
                    _ => {}
                }
            }
        }
        if !tcp.is_empty() {
            let _ = writeln!(out, "  networking.firewall.allowedTCPPorts = [ {} ];", tcp.join(" "));
        }
        if !udp.is_empty() {
            let _ = writeln!(out, "  networking.firewall.allowedUDPPorts = [ {} ];", udp.join(" "));
        }
    }
    if let Some(ssh) = &network.ssh {
        if ssh.enabled == Some(true) {
            out.push_str("  services.openssh.enable = true;\n");
            if let Some(pw) = ssh.password_auth {
                let _ = writeln!(out, "  services.openssh.settings.PasswordAuthentication = {pw};");
            }
            if let Some(permit) = ssh.permit_root {
                let value = match permit {
                    PermitRoot::No => "no",
                    PermitRoot::ProhibitPassword => "prohibit-password",
                    PermitRoot::Yes => "yes",
                };
                let _ = writeln!(out, "  services.openssh.settings.PermitRootLogin = {};", nix_str(value));
            }
        }
    }
    if let Some(proxy) = &doc.proxy {
        if let Some(http) = &proxy.http {
            let _ = writeln!(out, "  networking.proxy.default = {};", nix_str(http));
        }
        if !proxy.no_proxy.is_empty() {
            let _ = writeln!(out, "  networking.proxy.noProxy = {};", nix_str(&proxy.no_proxy.join(",")));
        }
    }
    out.push('\n');

    // Users.
    let mut wheel_nopasswd = false;
    for user in &doc.users {
        let _ = writeln!(out, "  users.users.{} = {{", user.name);
        if user.name != "root" {
            out.push_str("    isNormalUser = true;\n");
        }
        if let Some(uid) = user.uid {
            let _ = writeln!(out, "    uid = {uid};");
        }
        if let Some(comment) = &user.comment {
            let _ = writeln!(out, "    description = {};", nix_str(comment));
        }
        let mut groups = user.groups.clone();
        if user.admin == Some(true) && !groups.iter().any(|g| g == "wheel") {
            groups.insert(0, "wheel".to_string());
        }
        if user.name != "root" && !groups.is_empty() {
            let _ = writeln!(out, "    extraGroups = {};", nix_list(&groups));
        }
        match &user.password {
            Some(p) if p.locked == Some(true) => {
                out.push_str("    hashedPassword = \"!\";\n");
            }
            Some(p) => {
                if let Some(hash) = &p.hash {
                    let _ = writeln!(out, "    hashedPassword = {};", nix_str(hash));
                }
            }
            None => {}
        }
        if !user.ssh_authorized_keys.is_empty() {
            let _ = writeln!(out, "    openssh.authorizedKeys.keys = {};", nix_list(&user.ssh_authorized_keys));
        }
        if let Some(shell) = &user.shell {
            match shell.as_str() {
                "bash" => {}
                "zsh" | "fish" => {
                    let _ = writeln!(out, "    shell = pkgs.{shell};");
                }
                path if path.starts_with('/') => {
                    let _ = writeln!(out, "    shell = {};", nix_str(path));
                }
                other => warnings.push(format!("unknown shell intent {other:?} for user {}", user.name)),
            }
        }
        if user.dotfiles.is_some() {
            warnings.push(format!(
                "users[{}].dotfiles is not applied by the default translator",
                user.name
            ));
        }
        out.push_str("  };\n");
        if user.sudo == Some(SudoPolicy::Nopasswd) {
            wheel_nopasswd = true;
        }
        if user.shell.as_deref() == Some("zsh") {
            out.push_str("  programs.zsh.enable = true;\n");
        }
        if user.shell.as_deref() == Some("fish") {
            out.push_str("  programs.fish.enable = true;\n");
        }
    }
    if wheel_nopasswd {
        out.push_str("  security.sudo.wheelNeedsPassword = false;\n");
    }
    out.push('\n');

    // Software.
    let role = software.role.clone().unwrap_or_default();
    match role.as_str() {
        "desktop:gnome" => {
            out.push_str("  services.xserver.enable = true;\n");
            out.push_str("  services.xserver.displayManager.gdm.enable = true;\n");
            out.push_str("  services.xserver.desktopManager.gnome.enable = true;\n");
        }
        "desktop:kde" => {
            out.push_str("  services.xserver.enable = true;\n");
            out.push_str("  services.displayManager.sddm.enable = true;\n");
            out.push_str("  services.desktopManager.plasma6.enable = true;\n");
        }
        "desktop:xfce" => {
            out.push_str("  services.xserver.enable = true;\n");
            out.push_str("  services.xserver.desktopManager.xfce.enable = true;\n");
        }
        "desktop:sway" => out.push_str("  programs.sway.enable = true;\n"),
        "desktop:hyprland" => out.push_str("  programs.hyprland.enable = true;\n"),
        "" | "minimal" | "server" => {}
        other => warnings.push(format!("role {other:?} has no default-translator mapping")),
    }
    if !software.packages.is_empty() {
        out.push_str("  # Package names pass through verbatim; unresolvable names fail the build\n");
        out.push_str("  # (no-silent-drift).\n");
        let _ = writeln!(
            out,
            "  environment.systemPackages = with pkgs; [ {} ];",
            software.packages.join(" ")
        );
    }
    if !software.exclude.is_empty() {
        warnings.push("software.exclude has no NixOS equivalent (roles are additive)".to_string());
    }
    if let Some(services) = &software.services {
        for unit in &services.enable {
            match unit.as_str() {
                "sshd" => {} // covered by network.ssh
                "tailscaled" => out.push_str("  services.tailscale.enable = true;\n"),
                "docker" => out.push_str("  virtualisation.docker.enable = true;\n"),
                other => warnings.push(format!(
                    "services.enable {other:?} has no default mapping — add the module option yourself"
                )),
            }
        }
        for unit in &services.disable {
            warnings.push(format!("services.disable {unit:?} is not mapped by the default translator"));
        }
    }
    if !software.flatpak.is_empty() {
        out.push_str("  services.flatpak.enable = true;\n");
        warnings.push("flatpak app installation happens at runtime, not in configuration".to_string());
    }
    if !software.snap.is_empty() {
        warnings.push("snaps are not supported on NixOS (no-silent-drift: refused)".to_string());
    }

    // Desktop plumbing.
    if let Some(desktop) = &desktop {
        match desktop.audio {
            Some(Audio::Pipewire) | Some(Audio::Auto) => {
                out.push_str("  services.pipewire = { enable = true; alsa.enable = true; pulse.enable = true; };\n");
            }
            Some(Audio::Pulseaudio) => {
                out.push_str("  services.pulseaudio.enable = true;\n");
            }
            _ => {}
        }
        if desktop.bluetooth == Some(true) {
            out.push_str("  hardware.bluetooth.enable = true;\n");
        }
        if desktop.printing == Some(true) {
            out.push_str("  services.printing.enable = true;\n");
        }
        if let Some(user) = &desktop.autologin {
            out.push_str("  services.displayManager.autoLogin.enable = true;\n");
            let _ = writeln!(out, "  services.displayManager.autoLogin.user = {};", nix_str(user));
        }
    }

    // GPU intent.
    if let Some(drivers) = &doc.drivers {
        match drivers.gpu {
            Some(Gpu::Nvidia) | Some(Gpu::NvidiaOpen) => {
                out.push_str("  services.xserver.videoDrivers = [ \"nvidia\" ];\n");
                let open = drivers.gpu == Some(Gpu::NvidiaOpen);
                let _ = writeln!(out, "  hardware.nvidia.open = {open};");
            }
            _ => {}
        }
    }

    // Files into /etc; anything else is out of configuration's reach.
    for file in &doc.files {
        if let Some(rest) = file.path.strip_prefix("/etc/") {
            let _ = writeln!(out, "  environment.etc.{}.text = {};", nix_str(rest), nix_str(&file.content));
            if let Some(mode) = &file.mode {
                let _ = writeln!(out, "  environment.etc.{}.mode = {};", nix_str(rest), nix_str(mode));
            }
        } else {
            warnings.push(format!(
                "files[{}] outside /etc is not expressible in configuration.nix",
                file.path
            ));
        }
    }
    if doc.scripts.is_some() {
        warnings.push("scripts are an installer concern; the default translator emits none".to_string());
    }
    if doc.registration.is_some() {
        warnings.push("registration does not apply to NixOS (no-silent-drift: refused)".to_string());
    }
    if let Some(storage) = &doc.storage {
        if storage.snapshots.as_ref().map(|s| s.enabled).unwrap_or(false) {
            out.push_str("  services.snapper.configs.root = { SUBVOLUME = \"/\"; TIMELINE_CREATE = true; TIMELINE_CLEANUP = true; };\n");
        }
        if let Some(swap) = &storage.swap {
            if swap.zram.is_some() {
                out.push_str("  zramSwap.enable = true;\n");
            }
            if let Some(file) = &swap.file {
                let gib = file.size.as_gib().unwrap_or(4);
                let _ = writeln!(
                    out,
                    "  swapDevices = [ {{ device = {}; size = {}; }} ];",
                    nix_str(&file.path),
                    gib * 1024
                );
            }
        }
    }
    if system.kdump == Some(true) {
        warnings.push("kdump has no simple NixOS default mapping".to_string());
    }

    out.push('\n');
    out.push_str("  # Pin to the release the generator targeted; do not blindly bump.\n");
    out.push_str("  system.stateVersion = \"25.05\";\n");
    out.push_str("}\n");
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn example(name: &str) -> Document {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../examples/");
        Document::from_json(&std::fs::read_to_string(format!("{path}{name}")).unwrap()).unwrap()
    }

    #[test]
    fn translates_lvm_pool_document() {
        let doc = example("server-lvm-pool.lis.json");
        let out = translate(&doc).unwrap();
        assert!(out.disko.contains("type = \"lvm_pv\";"));
        assert!(out.disko.contains("\"pool\" = {"));
        assert!(out.disko.contains("type = \"EF00\";"));
        assert!(out.disko.contains("\"@home\" = { mountpoint = \"/home\";"));
        assert!(out.configuration.contains("networking.hostName = \"tron\";"));
        assert!(out.configuration.contains("services.openssh.enable = true;"));
        assert!(out.configuration.contains("hashedPassword = \"$6$"));
        assert!(out.hardware.contains("nixpkgs.hostPlatform"));
    }

    #[test]
    fn plain_partition_document_warns_on_ruleful_disk_match() {
        let doc = example("server-btrfs.lis.json");
        let out = translate(&doc).unwrap();
        // match: { largest: true } — no concrete device for disko.
        assert!(out
            .warnings
            .iter()
            .any(|w| w.contains("matches by rule")));
    }

    #[test]
    fn storage_less_documents_are_refused() {
        let doc = Document::new();
        assert!(translate(&doc).unwrap_err().contains("no storage"));
    }
}
