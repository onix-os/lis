const std = @import("std");

pub const VERSION = "0.1.0";

pub const Arch = enum {
    x86_64,
    aarch64,
    riscv64,
};

pub const Firmware = enum {
    uefi,
    bios,
    auto,
};

pub const SecretRef = struct {
    from: []const u8,
};

pub const KeyObject = struct {
    id: []const u8,
    type: ?[]const u8 = null,
    purpose: ?[][]const u8 = null,
    source: ?SecretRef = null,
    pin_required: bool = false,
};

pub const TargetDisk = struct {
    id: []const u8,
    match: std.json.Value,
};

pub const Target = struct {
    arch: ?Arch = null,
    firmware: ?Firmware = null,
    disks: ?[]TargetDisk = null,
};

pub const Subvolume = struct {
    name: []const u8,
    mountpoint: []const u8,
    mount_options: ?[][]const u8 = null,
};

pub const Partition = struct {
    disk: []const u8,
    id: ?[]const u8 = null,
    role: ?[]const u8 = null,
    size: ?[]const u8 = null,
    fs: ?[]const u8 = null,
    label: ?[]const u8 = null,
    mountpoint: ?[]const u8 = null,
    mount_options: ?[][]const u8 = null,
    subvolumes: ?[]Subvolume = null,
};

pub const Encryption = struct {
    id: []const u8,
    over: []const u8,
    type: ?[]const u8 = null,
    unlock: ?[][]const u8 = null,
};

pub const Storage = struct {
    partitions: ?[]Partition = null,
    encryption: ?[]Encryption = null,
};

pub const User = struct {
    name: []const u8,
    uid: ?u32 = null,
    admin: bool = false,
    shell: ?[]const u8 = null,
    groups: ?[][]const u8 = null,
    ssh_authorized_keys: ?[][]const u8 = null,
};

pub const Software = struct {
    role: ?[]const u8 = null,
    packages: ?[][]const u8 = null,
    exclude: ?[][]const u8 = null,
    flatpak: ?[][]const u8 = null,
};

pub const FileEntry = struct {
    path: []const u8,
    content: []const u8,
    mode: ?[]const u8 = null,
    owner: ?[]const u8 = null,
};

pub const Script = struct {
    content: ?[]const u8 = null,
    source: ?SecretRef = null,
    interpreter: ?[]const u8 = null,
    chroot: ?bool = null,
    on_failure: ?[]const u8 = null,
};

pub const Scripts = struct {
    pre_install: ?[]Script = null,
    post_storage: ?[]Script = null,
    post_install: ?[]Script = null,
    pre_reboot: ?[]Script = null,
    on_success: ?[]Script = null,
    on_error: ?[]Script = null,
    firstboot: ?[]Script = null,
};

pub const Document = struct {
    lis: []const u8 = VERSION,
    meta: ?std.json.Value = null,
    keys: ?[]KeyObject = null,
    target: ?Target = null,
    storage: ?Storage = null,
    system: ?std.json.Value = null,
    users: ?[]User = null,
    software: ?Software = null,
    files: ?[]FileEntry = null,
    scripts: ?Scripts = null,
};

pub fn parseJson(allocator: std.mem.Allocator, json_bytes: []const u8) !std.json.Parsed(Document) {
    return std.json.parseFromSlice(Document, allocator, json_bytes, .{
        .ignore_unknown_fields = true,
    });
}
