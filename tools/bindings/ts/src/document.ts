export const VERSION = "0.1.0";

export interface SecretRef {
  from: string;
}

export interface KeyObject {
  id: string;
  type?: "yubikey_fido2" | "yubikey_challenge" | "tpm2" | "gpg" | "age" | "keyfile" | "passphrase" | "ssh" | string;
  purpose?: ("payload_decryption" | "disk_encryption" | "secret_decryption" | "user_ssh_key" | "user_pam_auth" | "remote_auth" | string)[];
  match?: Record<string, unknown>;
  source?: SecretRef;
  pin_required?: boolean;
}

export interface Meta {
  name?: string;
  description?: string;
  generator?: string;
  created?: string;
}

export interface TargetDisk {
  id: string;
  match: Record<string, unknown>;
}

export interface Target {
  arch?: "x86_64" | "aarch64" | "riscv64";
  firmware?: "uefi" | "bios" | "auto";
  disks?: TargetDisk[];
}

export interface Subvolume {
  name: string;
  mountpoint: string;
  mount_options?: string[];
}

export interface Partition {
  disk: string;
  id?: string;
  role?: "esp" | "boot" | "root" | "swap" | "data" | "raw" | string;
  size?: string;
  fs?: "ext4" | "btrfs" | "xfs" | "f2fs" | "zfs" | "vfat" | "swap" | "none" | string;
  label?: string;
  mountpoint?: string;
  mount_options?: string[];
  subvolumes?: Subvolume[];
  existing?: Record<string, unknown>;
}

export interface Encryption {
  id: string;
  over: string;
  type?: string;
  key?: { passphrase?: boolean; keyfile?: string; ref?: string } | Record<string, unknown>;
  unlock?: string[];
}

export interface LvmVolume {
  name: string;
  size: string;
  fs?: string;
  mountpoint?: string;
  subvolumes?: Subvolume[];
}

export interface LvmGroup {
  name: string;
  devices: string[];
  volumes: LvmVolume[];
}

export interface RaidGroup {
  name: string;
  level: number;
  devices: string[];
  spares?: string[];
}

export interface Aggregates {
  lvm?: LvmGroup[];
  raid?: RaidGroup[];
}

export interface SwapConfig {
  zram?: { size: string };
  file?: { path: string; size: string };
}

export interface SnapshotConfig {
  enabled: boolean;
  tool?: "auto" | "snapper" | "timeshift";
  boot_menu?: boolean;
}

export interface Storage {
  partitions?: Partition[];
  encryption?: Encryption[];
  aggregates?: Aggregates;
  swap?: SwapConfig;
  snapshots?: SnapshotConfig;
}

export interface Boot {
  loader?: "systemd-boot" | "grub" | "auto" | string;
  timeout?: number;
  os_prober?: boolean;
  password_hash?: string;
  console?: { serial?: string };
  secure_boot?: "auto" | "true" | "false" | boolean;
  uki?: boolean;
  kernel?: { variant?: string; params?: string[]; modules?: string[]; blacklist?: string[] };
  initramfs?: { generator?: string; include_modules?: string[] };
}

export interface System {
  hostname?: string;
  domain?: string;
  timezone?: string;
  hwclock?: "utc" | "localtime";
  time?: { ntp?: boolean; servers?: string[]; provider?: string };
  locale?: string;
  extra_locales?: string[];
  locale_overrides?: Record<string, string>;
  keymap?: { console?: string; font?: string; layout?: string; variant?: string };
  init?: "systemd" | "openrc" | "runit" | "s6" | "auto" | string;
  security?: { module?: "auto" | "selinux" | "apparmor" | "none" | string };
  kdump?: boolean;
  telemetry?: "off" | "default";
}

export interface Script {
  content?: string;
  source?: SecretRef;
  interpreter?: string;
  chroot?: boolean;
  on_failure?: "fail" | "continue";
}

export interface Scripts {
  pre_install?: Script[];
  post_storage?: Script[];
  post_install?: Script[];
  pre_reboot?: Script[];
  on_success?: Script[];
  on_error?: Script[];
  firstboot?: Script[];
}

export interface User {
  name: string;
  uid?: number;
  comment?: string;
  admin?: boolean;
  sudo?: "default" | "nopasswd";
  shell?: string;
  groups?: string[];
  password?: { hash?: string; locked?: boolean };
  ssh_authorized_keys?: string[];
  dotfiles?: { repo: string; method?: "raw" | "stow" | "chezmoi" };
  scripts?: Scripts;
}

export interface Network {
  manager?: "auto" | "networkmanager" | "systemd-networkd" | "iwd";
  interfaces?: Record<string, unknown>[];
  wifi?: { ssid: string; psk_hash: string }[];
  ssh?: { enabled?: boolean; password_auth?: boolean; permit_root?: string };
  hosts?: { ip: string; names: string[] }[];
}

export interface AppDetail {
  name: string;
  package?: string;
  flatpak?: string;
  snap?: string;
  appimage?: string;
  preference?: string[];
}

export type AppEntry = string | AppDetail;

export interface Software {
  role?: "minimal" | "server" | "desktop:gnome" | "desktop:kde" | "desktop:hyprland" | "desktop:sway" | "desktop:xfce" | string;
  apps?: AppEntry[];
  packages?: string[];
  exclude?: string[];
  services?: { enable?: string[]; disable?: string[] };
  flatpak?: string[];
  snap?: { name: string; channel?: string; classic?: boolean }[];
}

export interface Desktop {
  display_manager?: "auto" | "gdm" | "sddm" | "lightdm" | "greetd" | "ly" | "none";
  autologin?: string;
  audio?: "auto" | "pipewire" | "pulseaudio" | "none";
  bluetooth?: boolean;
  printing?: boolean;
}

export interface Drivers {
  gpu?: "auto" | "nvidia" | "nvidia-open" | "amdgpu" | "intel" | "none";
  microcode?: "auto" | "intel" | "amd" | "none";
  firmware?: "auto" | "all" | "none";
}

export interface Proxy {
  http?: string;
  https?: string;
  no_proxy?: string[];
}

export interface Mirror {
  url?: string;
  country?: string;
}

export interface Registration {
  server?: string;
  token?: SecretRef;
  email?: string;
}

export interface FileEntry {
  path: string;
  content: string;
  mode?: string;
  owner?: string;
  encoding?: "plain" | "base64";
}

export interface Installer {
  on_finish?: "reboot" | "poweroff" | "stay";
  on_error?: "fail" | "prompt";
  unattended?: boolean;
  interactive?: string[];
  answers?: Record<string, string>;
}

export interface Document {
  lis: string;
  meta?: Meta;
  keys?: KeyObject[];
  target?: Target;
  storage?: Storage;
  boot?: Boot;
  system?: System;
  users?: User[];
  network?: Network;
  software?: Software;
  desktop?: Desktop;
  drivers?: Drivers;
  proxy?: Proxy;
  mirror?: Mirror;
  registration?: Registration;
  files?: FileEntry[];
  scripts?: Scripts;
  installer?: Installer;
  [key: `x-${string}`]: unknown;
}

export function parseDocument(jsonText: string): Document {
  const doc = JSON.parse(jsonText) as Document;
  if (!doc.lis) {
    throw new Error("Missing required 'lis' version field");
  }
  return doc;
}

export function stringifyDocument(doc: Document): string {
  return JSON.stringify(doc, null, 2);
}
