import json
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any, Union

@dataclass
class SecretRef:
    from_ref: str

    @classmethod
    def from_dict(cls, data: dict) -> "SecretRef":
        return cls(from_ref=data.get("from", ""))

    def to_dict(self) -> dict:
        return {"from": self.from_ref}

@dataclass
class KeyObject:
    id: str
    type: Optional[str] = None
    purpose: List[str] = field(default_factory=list)
    match: Dict[str, Any] = field(default_factory=dict)
    source: Optional[SecretRef] = None
    pin_required: bool = False

@dataclass
class Meta:
    name: Optional[str] = None
    description: Optional[str] = None
    generator: Optional[str] = None
    created: Optional[str] = None

@dataclass
class TargetDisk:
    id: str
    match: Dict[str, Any] = field(default_factory=dict)

@dataclass
class Target:
    arch: Optional[str] = None
    firmware: Optional[str] = None
    disks: List[TargetDisk] = field(default_factory=list)

@dataclass
class Subvolume:
    name: str
    mountpoint: str
    mount_options: List[str] = field(default_factory=list)

@dataclass
class Partition:
    disk: str
    id: Optional[str] = None
    role: Optional[str] = None
    size: Optional[str] = None
    fs: Optional[str] = None
    label: Optional[str] = None
    mountpoint: Optional[str] = None
    mount_options: List[str] = field(default_factory=list)
    subvolumes: List[Subvolume] = field(default_factory=list)
    existing: Dict[str, Any] = field(default_factory=dict)

@dataclass
class Encryption:
    id: str
    over: str
    type: Optional[str] = None
    key: Optional[Any] = None
    unlock: List[str] = field(default_factory=list)

@dataclass
class LvmVolume:
    name: str
    size: str
    fs: Optional[str] = None
    mountpoint: Optional[str] = None
    subvolumes: List[Subvolume] = field(default_factory=list)

@dataclass
class LvmGroup:
    name: str
    devices: List[str] = field(default_factory=list)
    volumes: List[LvmVolume] = field(default_factory=list)

@dataclass
class RaidGroup:
    name: str
    level: int = 1
    devices: List[str] = field(default_factory=list)
    spares: List[str] = field(default_factory=list)

@dataclass
class Aggregates:
    lvm: List[LvmGroup] = field(default_factory=list)
    raid: List[RaidGroup] = field(default_factory=list)

@dataclass
class SwapConfig:
    zram: Optional[Dict[str, Any]] = None
    file: Optional[Dict[str, Any]] = None

@dataclass
class SnapshotConfig:
    enabled: bool = False
    tool: Optional[str] = None
    boot_menu: Optional[bool] = None

@dataclass
class Storage:
    partitions: List[Partition] = field(default_factory=list)
    encryption: List[Encryption] = field(default_factory=list)
    aggregates: Optional[Aggregates] = None
    swap: Optional[SwapConfig] = None
    snapshots: Optional[SnapshotConfig] = None

@dataclass
class Boot:
    loader: Optional[str] = None
    timeout: Optional[int] = None
    os_prober: Optional[bool] = None
    password_hash: Optional[str] = None
    console: Optional[Dict[str, Any]] = None
    secure_boot: Optional[str] = None
    uki: Optional[bool] = None
    kernel: Optional[Dict[str, Any]] = None
    initramfs: Optional[Dict[str, Any]] = None

@dataclass
class System:
    hostname: Optional[str] = None
    domain: Optional[str] = None
    timezone: Optional[str] = None
    hwclock: Optional[str] = None
    time: Optional[Dict[str, Any]] = None
    locale: Optional[str] = None
    extra_locales: List[str] = field(default_factory=list)
    locale_overrides: Dict[str, str] = field(default_factory=dict)
    keymap: Optional[Dict[str, Any]] = None
    init: Optional[str] = None
    security: Optional[Dict[str, Any]] = None
    kdump: bool = False
    telemetry: Optional[str] = None

@dataclass
class Script:
    content: Optional[str] = None
    source: Optional[SecretRef] = None
    interpreter: Optional[str] = None
    chroot: Optional[bool] = None
    on_failure: Optional[str] = None

@dataclass
class Scripts:
    pre_install: List[Script] = field(default_factory=list)
    post_storage: List[Script] = field(default_factory=list)
    post_install: List[Script] = field(default_factory=list)
    pre_reboot: List[Script] = field(default_factory=list)
    on_success: List[Script] = field(default_factory=list)
    on_error: List[Script] = field(default_factory=list)
    firstboot: List[Script] = field(default_factory=list)

@dataclass
class User:
    name: str
    uid: Optional[int] = None
    comment: Optional[str] = None
    admin: bool = False
    sudo: Optional[str] = None
    shell: Optional[str] = None
    groups: List[str] = field(default_factory=list)
    password: Optional[Dict[str, Any]] = None
    ssh_authorized_keys: List[str] = field(default_factory=list)
    dotfiles: Optional[Dict[str, Any]] = None
    scripts: Optional[Scripts] = None

@dataclass
class Network:
    manager: Optional[str] = None
    interfaces: List[Dict[str, Any]] = field(default_factory=list)
    wifi: List[Dict[str, Any]] = field(default_factory=list)
    ssh: Optional[Dict[str, Any]] = None
    hosts: List[Dict[str, Any]] = field(default_factory=list)

@dataclass
class Software:
    role: Optional[str] = None
    apps: List[Any] = field(default_factory=list)
    packages: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    services: Optional[Dict[str, Any]] = None
    flatpak: List[str] = field(default_factory=list)
    snap: List[Dict[str, Any]] = field(default_factory=list)

@dataclass
class Desktop:
    display_manager: Optional[str] = None
    autologin: Optional[str] = None
    audio: Optional[str] = None
    bluetooth: bool = False
    printing: bool = False

@dataclass
class Drivers:
    gpu: Optional[str] = None
    microcode: Optional[str] = None
    firmware: Optional[str] = None

@dataclass
class Proxy:
    http: Optional[str] = None
    https: Optional[str] = None
    no_proxy: List[str] = field(default_factory=list)

@dataclass
class Mirror:
    url: Optional[str] = None
    country: Optional[str] = None

@dataclass
class Registration:
    server: Optional[str] = None
    token: Optional[SecretRef] = None
    email: Optional[str] = None

@dataclass
class FileEntry:
    path: str
    content: str
    mode: Optional[str] = None
    owner: Optional[str] = None
    encoding: Optional[str] = None

@dataclass
class Installer:
    on_finish: Optional[str] = None
    on_error: Optional[str] = None
    unattended: bool = False
    interactive: List[str] = field(default_factory=list)
    answers: Dict[str, Any] = field(default_factory=dict)

@dataclass
class Document:
    lis: str = "0.1.0"
    meta: Optional[Meta] = None
    keys: List[KeyObject] = field(default_factory=list)
    target: Optional[Target] = None
    storage: Optional[Storage] = None
    boot: Optional[Boot] = None
    system: Optional[System] = None
    users: List[User] = field(default_factory=list)
    network: Optional[Network] = None
    software: Optional[Software] = None
    desktop: Optional[Desktop] = None
    drivers: Optional[Drivers] = None
    proxy: Optional[Proxy] = None
    mirror: Optional[Mirror] = None
    registration: Optional[Registration] = None
    files: List[FileEntry] = field(default_factory=list)
    scripts: Optional[Scripts] = None
    installer: Optional[Installer] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)

def parse_json(json_str_or_dict: Union[str, dict]) -> Document:
    data = json.loads(json_str_or_dict) if isinstance(json_str_or_dict, str) else json_str_or_dict
    doc = Document(lis=data.get("lis", "0.1.0"), raw_data=data)
    return doc

def to_json(doc: Document) -> str:
    return json.dumps(doc.raw_data or asdict(doc), indent=2)
