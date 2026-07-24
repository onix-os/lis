/* 
 * LIS — Linux Installation Specification
 * Single-header C struct definitions and enums for LIS v0.1.0
 */

#ifndef LIS_H
#define LIS_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>

#define LIS_SPEC_VERSION "0.1.0"

/* Target Architecture */
typedef enum {
    LIS_ARCH_UNKNOWN = 0,
    LIS_ARCH_X86_64,
    LIS_ARCH_AARCH64,
    LIS_ARCH_RISCV64
} lis_arch_t;

/* Target Firmware */
typedef enum {
    LIS_FIRMWARE_AUTO = 0,
    LIS_FIRMWARE_UEFI,
    LIS_FIRMWARE_BIOS
} lis_firmware_t;

/* Bootloader Choice */
typedef enum {
    LIS_BOOTLOADER_AUTO = 0,
    LIS_BOOTLOADER_SYSTEMD_BOOT,
    LIS_BOOTLOADER_GRUB
} lis_bootloader_t;

/* Partition Role */
typedef enum {
    LIS_PART_ROLE_RAW = 0,
    LIS_PART_ROLE_ESP,
    LIS_PART_ROLE_BOOT,
    LIS_PART_ROLE_ROOT,
    LIS_PART_ROLE_SWAP,
    LIS_PART_ROLE_DATA
} lis_part_role_t;

/* Filesystem Type */
typedef enum {
    LIS_FS_NONE = 0,
    LIS_FS_EXT4,
    LIS_FS_BTRFS,
    LIS_FS_XFS,
    LIS_FS_F2FS,
    LIS_FS_ZFS,
    LIS_FS_VFAT,
    LIS_FS_SWAP
} lis_fs_t;

/* Secret Reference */
typedef struct {
    const char *from; /* e.g. "seed:secrets/scc-token", "key:admin-key", "file:/path", "env:VAR" */
} lis_secret_ref_t;

/* Key Object */
typedef struct {
    const char *id;
    const char *type;      /* "yubikey_fido2", "tpm2", "gpg", "keyfile", "passphrase", "ssh" */
    const char **purpose;  /* array of purpose strings */
    size_t purpose_count;
    lis_secret_ref_t *source;
    bool pin_required;
} lis_key_object_t;

/* Target Disk Matcher */
typedef struct {
    const char *id;
    bool largest;
    const char *wwn;
    const char *serial;
    const char *path;
} lis_target_disk_t;

/* Subvolume */
typedef struct {
    const char *name;
    const char *mountpoint;
    const char **mount_options;
    size_t mount_options_count;
} lis_subvolume_t;

/* Partition */
typedef struct {
    const char *disk_id;
    const char *id;
    lis_part_role_t role;
    const char *size;
    lis_fs_t fs;
    const char *label;
    const char *mountpoint;
    const char **mount_options;
    size_t mount_options_count;
    lis_subvolume_t *subvolumes;
    size_t subvolume_count;
} lis_partition_t;

/* Encryption Container */
typedef struct {
    const char *id;
    const char *over_id;
    const char *type; /* "luks2" */
    const char **unlock_order;
    size_t unlock_order_count;
} lis_encryption_t;

/* User Account */
typedef struct {
    const char *name;
    uint32_t uid;
    bool admin;
    const char *shell;
    const char **groups;
    size_t group_count;
    const char *password_hash;
    const char **ssh_keys;
    size_t ssh_key_count;
} lis_user_t;

/* File Entry */
typedef struct {
    const char *path;
    const char *content;
    const char *mode;
    const char *owner;
} lis_file_entry_t;

/* Script Entry */
typedef struct {
    const char *content;
    lis_secret_ref_t *source;
    const char *interpreter;
    bool chroot;
    const char *on_failure; /* "fail", "continue" */
} lis_script_t;

/* Script Lifecycle Hooks */
typedef struct {
    lis_script_t *pre_install;   size_t pre_install_count;
    lis_script_t *post_storage;  size_t post_storage_count;
    lis_script_t *post_install;  size_t post_install_count;
    lis_script_t *pre_reboot;    size_t pre_reboot_count;
    lis_script_t *on_success;    size_t on_success_count;
    lis_script_t *on_error;      size_t on_error_count;
    lis_script_t *firstboot;     size_t firstboot_count;
} lis_scripts_t;

/* Top-level LIS Document */
typedef struct {
    const char *version;
    const char *hostname;
    const char *timezone;
    const char *locale;
    lis_arch_t arch;
    lis_firmware_t firmware;
    lis_bootloader_t bootloader;
    
    lis_key_object_t *keys;
    size_t key_count;

    lis_target_disk_t *disks;
    size_t disk_count;

    lis_partition_t *partitions;
    size_t partition_count;

    lis_encryption_t *encryption;
    size_t encryption_count;

    lis_user_t *users;
    size_t user_count;

    lis_file_entry_t *files;
    size_t file_count;

    lis_scripts_t *scripts;
} lis_document_t;

#ifdef __cplusplus
}
#endif

#endif /* LIS_H */
