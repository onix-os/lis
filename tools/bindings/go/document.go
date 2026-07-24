package lis

import (
	"encoding/json"
)

// Document is the top-level LIS document struct.
type Document struct {
	LIS          string                 `json:"lis"`
	Meta         *Meta                  `json:"meta,omitempty"`
	Keys         []KeyObject            `json:"keys,omitempty"`
	Target       *Target                `json:"target,omitempty"`
	Storage      *Storage               `json:"storage,omitempty"`
	Boot         *Boot                  `json:"boot,omitempty"`
	System       *System                `json:"system,omitempty"`
	Users        []User                 `json:"users,omitempty"`
	Network      *Network               `json:"network,omitempty"`
	Software     *Software              `json:"software,omitempty"`
	Desktop      *Desktop               `json:"desktop,omitempty"`
	Drivers      *Drivers               `json:"drivers,omitempty"`
	Proxy        *Proxy                 `json:"proxy,omitempty"`
	Mirror       *Mirror                `json:"mirror,omitempty"`
	Registration *Registration          `json:"registration,omitempty"`
	Files        []FileEntry            `json:"files,omitempty"`
	Scripts      *Scripts               `json:"scripts,omitempty"`
	Installer    *Installer             `json:"installer,omitempty"`
	Extensions   map[string]interface{} `json:"-"`
}

type Meta struct {
	Name        string `json:"name,omitempty"`
	Description string `json:"description,omitempty"`
	Generator   string `json:"generator,omitempty"`
	Created     string `json:"created,omitempty"`
}

type SecretRef struct {
	From string `json:"from"`
}

type KeyObject struct {
	ID          string                 `json:"id"`
	Type        string                 `json:"type,omitempty"`
	Purpose     []string               `json:"purpose,omitempty"`
	Match       map[string]interface{} `json:"match,omitempty"`
	Source      *SecretRef             `json:"source,omitempty"`
	PinRequired bool                   `json:"pin_required,omitempty"`
}

type Target struct {
	Arch     string       `json:"arch,omitempty"`
	Firmware string       `json:"firmware,omitempty"`
	Disks    []TargetDisk `json:"disks,omitempty"`
}

type TargetDisk struct {
	ID    string                 `json:"id"`
	Match map[string]interface{} `json:"match"`
}

type Storage struct {
	Partitions  []Partition          `json:"partitions,omitempty"`
	Encryption  []Encryption         `json:"encryption,omitempty"`
	Aggregates  *Aggregates          `json:"aggregates,omitempty"`
	Swap        *SwapConfig          `json:"swap,omitempty"`
	Snapshots   *SnapshotConfig      `json:"snapshots,omitempty"`
}

type Partition struct {
	Disk         string                 `json:"disk"`
	ID           string                 `json:"id,omitempty"`
	Role         string                 `json:"role,omitempty"`
	Size         string                 `json:"size,omitempty"`
	Fs           string                 `json:"fs,omitempty"`
	Label        string                 `json:"label,omitempty"`
	Mountpoint   string                 `json:"mountpoint,omitempty"`
	MountOptions []string               `json:"mount_options,omitempty"`
	Subvolumes   []Subvolume            `json:"subvolumes,omitempty"`
	Existing     map[string]interface{} `json:"existing,omitempty"`
}

type Subvolume struct {
	Name         string   `json:"name"`
	Mountpoint   string   `json:"mountpoint"`
	MountOptions []string `json:"mount_options,omitempty"`
}

type Encryption struct {
	ID     string      `json:"id"`
	Over   string      `json:"over"`
	Type   string      `json:"type,omitempty"`
	Key    interface{} `json:"key,omitempty"`
	Unlock []string    `json:"unlock,omitempty"`
}

type Aggregates struct {
	LVM  []LvmGroup  `json:"lvm,omitempty"`
	RAID []RaidGroup `json:"raid,omitempty"`
}

type LvmGroup struct {
	Name    string      `json:"name"`
	Devices []string    `json:"devices"`
	Volumes []LvmVolume `json:"volumes"`
}

type LvmVolume struct {
	Name         string      `json:"name"`
	Size         string      `json:"size"`
	Fs           string      `json:"fs,omitempty"`
	Mountpoint   string      `json:"mountpoint,omitempty"`
	Subvolumes   []Subvolume `json:"subvolumes,omitempty"`
}

type RaidGroup struct {
	Name    string   `json:"name"`
	Level   int      `json:"level"`
	Devices []string `json:"devices"`
	Spares  []string `json:"spares,omitempty"`
}

type SwapConfig struct {
	Zram *SwapZram `json:"zram,omitempty"`
	File *SwapFile `json:"file,omitempty"`
}

type SwapZram struct {
	Size string `json:"size"`
}

type SwapFile struct {
	Path string `json:"path"`
	Size string `json:"size"`
}

type SnapshotConfig struct {
	Enabled  bool   `json:"enabled"`
	Tool     string `json:"tool,omitempty"`
	BootMenu bool   `json:"boot_menu,omitempty"`
}

type Boot struct {
	Loader       string            `json:"loader,omitempty"`
	Timeout      int               `json:"timeout,omitempty"`
	OSProber     *bool             `json:"os_prober,omitempty"`
	PasswordHash string            `json:"password_hash,omitempty"`
	Console      *Console          `json:"console,omitempty"`
	SecureBoot   string            `json:"secure_boot,omitempty"`
	UKI          *bool             `json:"uki,omitempty"`
	Kernel       *Kernel           `json:"kernel,omitempty"`
	Initramfs    *Initramfs        `json:"initramfs,omitempty"`
}

type Console struct {
	Serial string `json:"serial,omitempty"`
}

type Kernel struct {
	Variant   string   `json:"variant,omitempty"`
	Params    []string `json:"params,omitempty"`
	Modules   []string `json:"modules,omitempty"`
	Blacklist []string `json:"blacklist,omitempty"`
}

type Initramfs struct {
	Generator      string   `json:"generator,omitempty"`
	IncludeModules []string `json:"include_modules,omitempty"`
}

type System struct {
	Hostname        string            `json:"hostname,omitempty"`
	Domain          string            `json:"domain,omitempty"`
	Timezone        string            `json:"timezone,omitempty"`
	Hwclock         string            `json:"hwclock,omitempty"`
	Time            *TimeConfig       `json:"time,omitempty"`
	Locale          string            `json:"locale,omitempty"`
	ExtraLocales    []string          `json:"extra_locales,omitempty"`
	LocaleOverrides map[string]string `json:"locale_overrides,omitempty"`
	Keymap          *Keymap           `json:"keymap,omitempty"`
	Init            string            `json:"init,omitempty"`
	Security        *Security         `json:"security,omitempty"`
	Kdump           bool              `json:"kdump,omitempty"`
	Telemetry       string            `json:"telemetry,omitempty"`
}

type TimeConfig struct {
	NTP      bool     `json:"ntp"`
	Servers  []string `json:"servers,omitempty"`
	Provider string   `json:"provider,omitempty"`
}

type Keymap struct {
	Console string `json:"console,omitempty"`
	Font    string `json:"font,omitempty"`
	Layout  string `json:"layout,omitempty"`
	Variant string `json:"variant,omitempty"`
}

type Security struct {
	Module string `json:"module,omitempty"`
}

type User struct {
	Name              string                 `json:"name"`
	UID               *uint32                `json:"uid,omitempty"`
	Comment           string                 `json:"comment,omitempty"`
	Admin             bool                   `json:"admin,omitempty"`
	Sudo              string                 `json:"sudo,omitempty"`
	Shell             string                 `json:"shell,omitempty"`
	Groups            []string               `json:"groups,omitempty"`
	Password          map[string]interface{} `json:"password,omitempty"`
	SSHAuthorizedKeys []string               `json:"ssh_authorized_keys,omitempty"`
	Dotfiles          map[string]interface{} `json:"dotfiles,omitempty"`
	Scripts           *Scripts               `json:"scripts,omitempty"`
}

type Network struct {
	Manager    string      `json:"manager,omitempty"`
	Interfaces []Interface `json:"interfaces,omitempty"`
	Wifi       []Wifi      `json:"wifi,omitempty"`
	SSH        *SSHConfig  `json:"ssh,omitempty"`
	Hosts      []HostEntry `json:"hosts,omitempty"`
}

type Interface struct {
	Name    string                 `json:"name"`
	DHCP    bool                   `json:"dhcp,omitempty"`
	Address string                 `json:"address,omitempty"`
	Gateway string                 `json:"gateway,omitempty"`
	DNS     []string               `json:"dns,omitempty"`
	Match   map[string]interface{} `json:"match,omitempty"`
}

type Wifi struct {
	SSID    string `json:"ssid"`
	PSKHash string `json:"psk_hash"`
}

type SSHConfig struct {
	Enabled      bool   `json:"enabled,omitempty"`
	PasswordAuth bool   `json:"password_auth,omitempty"`
	PermitRoot   string `json:"permit_root,omitempty"`
}

type HostEntry struct {
	IP    string   `json:"ip"`
	Names []string `json:"names"`
}

type Software struct {
	Role     string        `json:"role,omitempty"`
	Apps     []interface{} `json:"apps,omitempty"`
	Packages []string      `json:"packages,omitempty"`
	Exclude  []string      `json:"exclude,omitempty"`
	Services *Services     `json:"services,omitempty"`
	Flatpak  []string      `json:"flatpak,omitempty"`
	Snap     []Snap        `json:"snap,omitempty"`
}

type Services struct {
	Enable  []string `json:"enable,omitempty"`
	Disable []string `json:"disable,omitempty"`
}

type Snap struct {
	Name    string `json:"name"`
	Channel string `json:"channel,omitempty"`
	Classic bool   `json:"classic,omitempty"`
}

type Desktop struct {
	DisplayManager string `json:"display_manager,omitempty"`
	Autologin      string `json:"autologin,omitempty"`
	Audio          string `json:"audio,omitempty"`
	Bluetooth      bool   `json:"bluetooth,omitempty"`
	Printing       bool   `json:"printing,omitempty"`
}

type Drivers struct {
	GPU       string `json:"gpu,omitempty"`
	Microcode string `json:"microcode,omitempty"`
	Firmware  string `json:"firmware,omitempty"`
}

type Proxy struct {
	HTTP    string   `json:"http,omitempty"`
	HTTPS   string   `json:"https,omitempty"`
	NoProxy []string `json:"no_proxy,omitempty"`
}

type Mirror struct {
	URL     string `json:"url,omitempty"`
	Country string `json:"country,omitempty"`
}

type Registration struct {
	Server string     `json:"server,omitempty"`
	Token  *SecretRef `json:"token,omitempty"`
	Email  string     `json:"email,omitempty"`
}

type FileEntry struct {
	Path     string `json:"path"`
	Content  string `json:"content"`
	Mode     string `json:"mode,omitempty"`
	Owner    string `json:"owner,omitempty"`
	Encoding string `json:"encoding,omitempty"`
}

type Scripts struct {
	PreInstall  []Script `json:"pre_install,omitempty"`
	PostStorage []Script `json:"post_storage,omitempty"`
	PostInstall []Script `json:"post_install,omitempty"`
	PreReboot   []Script `json:"pre_reboot,omitempty"`
	OnSuccess   []Script `json:"on_success,omitempty"`
	OnError     []Script `json:"on_error,omitempty"`
	Firstboot   []Script `json:"firstboot,omitempty"`
}

type Script struct {
	Content     string     `json:"content,omitempty"`
	Source      *SecretRef `json:"source,omitempty"`
	Interpreter string     `json:"interpreter,omitempty"`
	Chroot      *bool      `json:"chroot,omitempty"`
	OnFailure   string     `json:"on_failure,omitempty"`
}

type Installer struct {
	OnFinish    string                 `json:"on_finish,omitempty"`
	OnError     string                 `json:"on_error,omitempty"`
	Unattended  bool                   `json:"unattended,omitempty"`
	Interactive []string               `json:"interactive,omitempty"`
	Answers     map[string]interface{} `json:"answers,omitempty"`
}

// ParseJSON parses a LIS document from raw JSON bytes.
func ParseJSON(data []byte) (*Document, error) {
	var doc Document
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	return &doc, nil
}

// ToJSON serializes the LIS document back to JSON bytes.
func (d *Document) ToJSON() ([]byte, error) {
	return json.MarshalIndent(d, "", "  ")
}
