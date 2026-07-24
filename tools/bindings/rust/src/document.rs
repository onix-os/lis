//! The typed LIS document model — one struct per schema.md section.
//!
//! Every field is optional (or an empty collection) exactly as the spec
//! demands: an omitted section means "applier default". Unknown top-level
//! keys land in [`Document::extensions`]; validation flags any that do not
//! carry the `x-` prefix.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::size::Size;

/// The spec version this crate implements.
pub const VERSION: &str = "0.1.0";

fn skip_false(b: &bool) -> bool {
    !*b
}

// ── top level ────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Document {
    /// Spec version, e.g. `"0.1.0"`. Required.
    pub lis: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub meta: Option<Meta>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub keys: Vec<KeyObject>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<Target>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub storage: Option<Storage>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub boot: Option<Boot>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub system: Option<System>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub users: Vec<User>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub network: Option<Network>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub software: Option<Software>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub desktop: Option<Desktop>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub drivers: Option<Drivers>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub proxy: Option<Proxy>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mirror: Option<Mirror>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub registration: Option<Registration>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub files: Vec<FileEntry>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scripts: Option<Scripts>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub installer: Option<Installer>,
    /// `x-*` extension namespaces (and, until validated, any stray keys).
    #[serde(flatten)]
    pub extensions: BTreeMap<String, serde_json::Value>,
}

impl Document {
    /// A fresh document at the current spec version.
    pub fn new() -> Self {
        Document {
            lis: VERSION.to_string(),
            ..Default::default()
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Meta {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub generator: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub created: Option<String>,
}

// ── keys ─────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct KeyObject {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub r#type: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub purpose: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub matcher: Option<BTreeMap<String, serde_json::Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<SecretRef>,
    #[serde(default, skip_serializing_if = "skip_false")]
    pub pin_required: bool,
}

// ── target ───────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Target {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub arch: Option<Arch>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub firmware: Option<Firmware>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub disks: Vec<TargetDisk>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Arch {
    X86_64,
    Aarch64,
    Riscv64,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum Firmware {
    Uefi,
    Bios,
    #[default]
    Auto,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct TargetDisk {
    pub id: String,
    #[serde(rename = "match")]
    pub matcher: DiskMatch,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct DiskMatch {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub serial: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wwn: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min_size: Option<Size>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_size: Option<Size>,
    #[serde(rename = "type", default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<DiskKind>,
    #[serde(default, skip_serializing_if = "skip_false")]
    pub smallest: bool,
    #[serde(default, skip_serializing_if = "skip_false")]
    pub largest: bool,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum DiskKind {
    Ssd,
    Hdd,
    Nvme,
}

// ── storage ──────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Storage {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wipe: Option<bool>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub partitions: Vec<Partition>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub encryption: Vec<Encryption>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub lvm: Vec<LvmGroup>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub raid: Vec<RaidArray>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub swap: Option<Swap>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub snapshots: Option<Snapshots>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Partition {
    /// A `target.disks[].id` handle.
    pub disk: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<Role>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub size: Option<Size>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fs: Option<Fs>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mountpoint: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub mount_options: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub subvolumes: Vec<Subvolume>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub existing: Option<Existing>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Esp,
    Boot,
    Root,
    Swap,
    Data,
    Raw,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Fs {
    Ext4,
    Btrfs,
    Xfs,
    F2fs,
    Zfs,
    Vfat,
    Swap,
    None,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Subvolume {
    pub name: String,
    pub mountpoint: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub mount_options: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Existing {
    #[serde(rename = "match")]
    pub matcher: ExistingMatch,
    #[serde(default, skip_serializing_if = "skip_false")]
    pub format: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resize: Option<Size>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct ExistingMatch {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub partition: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub uuid: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fs: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Encryption {
    pub id: String,
    /// Partition (or aggregate volume) id this container sits over.
    pub over: String,
    #[serde(rename = "type", default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<LuksKind>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub key: Option<KeySource>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub unlock: Vec<Unlock>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum LuksKind {
    #[default]
    Luks2,
    Luks1,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct KeySource {
    #[serde(default, skip_serializing_if = "skip_false")]
    pub passphrase: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub keyfile: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Unlock {
    Passphrase,
    Keyfile,
    Tpm2,
    Fido2,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct LvmGroup {
    pub name: String,
    pub devices: Vec<String>,
    pub volumes: Vec<LvmVolume>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct LvmVolume {
    pub name: String,
    pub size: Option<Size>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fs: Option<Fs>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mountpoint: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub mount_options: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub subvolumes: Vec<Subvolume>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct RaidArray {
    pub name: String,
    pub level: u8,
    pub devices: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub spares: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Swap {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub zram: Option<ZramSwap>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub file: Option<FileSwap>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct ZramSwap {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub size: Option<Size>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct FileSwap {
    pub path: String,
    pub size: Size,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Snapshots {
    #[serde(default, skip_serializing_if = "skip_false")]
    pub enabled: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool: Option<SnapshotTool>,
    #[serde(default, skip_serializing_if = "skip_false")]
    pub boot_menu: bool,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum SnapshotTool {
    #[default]
    Auto,
    Snapper,
    Timeshift,
}

// ── boot ─────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Boot {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub loader: Option<Loader>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timeout: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub os_prober: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub password_hash: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub console: Option<BootConsole>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub secure_boot: Option<SecureBoot>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub uki: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kernel: Option<Kernel>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub initramfs: Option<Initramfs>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "kebab-case")]
pub enum Loader {
    SystemdBoot,
    Grub,
    #[default]
    Auto,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct BootConsole {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub serial: Option<String>,
}

/// `"auto" | true | false` — a tri-state the JSON writes heterogeneously.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum SecureBoot {
    #[default]
    Auto,
    On,
    Off,
}

impl Serialize for SecureBoot {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            SecureBoot::Auto => serializer.serialize_str("auto"),
            SecureBoot::On => serializer.serialize_bool(true),
            SecureBoot::Off => serializer.serialize_bool(false),
        }
    }
}

impl<'de> Deserialize<'de> for SecureBoot {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        use serde::de::Error;
        match serde_json::Value::deserialize(deserializer)? {
            serde_json::Value::String(s) if s == "auto" => Ok(SecureBoot::Auto),
            serde_json::Value::Bool(true) => Ok(SecureBoot::On),
            serde_json::Value::Bool(false) => Ok(SecureBoot::Off),
            other => Err(D::Error::custom(format!(
                "secure_boot must be \"auto\", true, or false (got {other})"
            ))),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Kernel {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub variant: Option<KernelVariant>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub params: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub modules: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub blacklist: Vec<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum KernelVariant {
    #[default]
    Default,
    Lts,
    Hardened,
    Realtime,
    Zen,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Initramfs {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub generator: Option<InitramfsGenerator>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub include_modules: Vec<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum InitramfsGenerator {
    #[default]
    Auto,
    Dracut,
    Mkinitcpio,
    Booster,
}

// ── system ───────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct System {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hostname: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub domain: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timezone: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hwclock: Option<HwClock>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub time: Option<Time>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub locale: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub extra_locales: Vec<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub locale_overrides: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub keymap: Option<Keymap>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub init: Option<Init>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub security: Option<Security>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kdump: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub telemetry: Option<Telemetry>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum HwClock {
    #[default]
    Utc,
    Localtime,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Time {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ntp: Option<bool>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub servers: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider: Option<TimeProvider>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "kebab-case")]
pub enum TimeProvider {
    #[default]
    Auto,
    Chrony,
    SystemdTimesyncd,
    Openntpd,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Keymap {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub console: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub font: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub layout: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub variant: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum Init {
    Systemd,
    Openrc,
    Runit,
    S6,
    #[default]
    Auto,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Security {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub module: Option<SecurityModule>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum SecurityModule {
    #[default]
    Auto,
    Selinux,
    Apparmor,
    None,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum Telemetry {
    Off,
    #[default]
    Default,
}

// ── users ────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct User {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub uid: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comment: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub admin: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sudo: Option<SudoPolicy>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub shell: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub groups: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub password: Option<Password>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub ssh_authorized_keys: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dotfiles: Option<Dotfiles>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scripts: Option<Scripts>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum SudoPolicy {
    #[default]
    Default,
    Nopasswd,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Password {
    /// crypt(3) hash (`$6$…`, `$y$…`). Never plaintext.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hash: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub locked: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Dotfiles {
    pub repo: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub method: Option<DotfilesMethod>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum DotfilesMethod {
    #[default]
    Raw,
    Stow,
    Chezmoi,
}

// ── network ──────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Network {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub manager: Option<NetworkManager>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub interfaces: Vec<Interface>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub wifi: Vec<Wifi>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub hosts: Vec<HostEntry>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub firewall: Option<Firewall>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ssh: Option<Ssh>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "kebab-case")]
pub enum NetworkManager {
    #[default]
    Auto,
    Networkmanager,
    SystemdNetworkd,
    Iwd,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Interface {
    #[serde(rename = "match")]
    pub matcher: InterfaceMatch,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dhcp4: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dhcp6: Option<bool>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub addresses: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gateway: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub dns: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct InterfaceMatch {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mac: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Wifi {
    pub ssid: String,
    /// WPA-PSK hex hash (wpa_passphrase output), never the passphrase.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub psk_hash: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hidden: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct HostEntry {
    pub ip: String,
    pub names: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Firewall {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub enabled: Option<bool>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub allow_services: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub allow_ports: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Ssh {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub enabled: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub password_auth: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub permit_root: Option<PermitRoot>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
pub enum PermitRoot {
    #[serde(rename = "no")]
    #[default]
    No,
    #[serde(rename = "prohibit-password")]
    ProhibitPassword,
    #[serde(rename = "yes")]
    Yes,
}

// ── software / desktop / drivers ─────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Software {
    /// Role intent, e.g. `server`, `minimal`, `desktop:gnome`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub apps: Vec<AppEntry>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub packages: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub exclude: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub services: Option<Services>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub flatpak: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub snap: Vec<Snap>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(untagged)]
pub enum AppEntry {
    Simple(String),
    Detailed(AppDetail),
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct AppDetail {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub package: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub flatpak: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub snap: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub appimage: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub preference: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Services {
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub enable: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub disable: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Snap {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub channel: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub classic: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Desktop {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub display_manager: Option<DisplayManager>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub autologin: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub audio: Option<Audio>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bluetooth: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub printing: Option<bool>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum DisplayManager {
    #[default]
    Auto,
    Gdm,
    Sddm,
    Lightdm,
    Greetd,
    Ly,
    None,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum Audio {
    #[default]
    Auto,
    Pipewire,
    Pulseaudio,
    None,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Drivers {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gpu: Option<Gpu>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub microcode: Option<Microcode>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub firmware: Option<FirmwarePolicy>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "kebab-case")]
pub enum Gpu {
    #[default]
    Auto,
    Nvidia,
    NvidiaOpen,
    Amdgpu,
    Intel,
    None,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum Microcode {
    #[default]
    Auto,
    Intel,
    Amd,
    None,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum FirmwarePolicy {
    #[default]
    Auto,
    All,
    None,
}

// ── proxy / mirror / registration ────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Proxy {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub http: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub https: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub no_proxy: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Mirror {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub country: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Registration {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub server: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token: Option<SecretRef>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub email: Option<String>,
}

/// A reference to secret material: `file:<path>` or `env:<var>`. Never inline.
#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct SecretRef {
    pub from: String,
}

// ── files / scripts / installer ──────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct FileEntry {
    pub path: String,
    pub content: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mode: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub owner: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub encoding: Option<Encoding>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum Encoding {
    #[default]
    Plain,
    Base64,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Scripts {
    #[serde(default, alias = "pre", skip_serializing_if = "Vec::is_empty")]
    pub pre_install: Vec<Script>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub post_storage: Vec<Script>,
    #[serde(default, alias = "post", skip_serializing_if = "Vec::is_empty")]
    pub post_install: Vec<Script>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub pre_reboot: Vec<Script>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub on_success: Vec<Script>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub on_error: Vec<Script>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub firstboot: Vec<Script>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Script {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub content: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<SecretRef>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub interpreter: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub chroot: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub on_failure: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Installer {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub on_finish: Option<OnFinish>,
    /// The document half of the delivery two-key consent rule (delivery §3).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unattended: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub on_error: Option<OnError>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub interactive: Vec<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub answers: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum OnFinish {
    Reboot,
    Poweroff,
    #[default]
    Stay,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum OnError {
    #[default]
    Fail,
    Prompt,
}
