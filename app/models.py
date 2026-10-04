"""设备/卷的数据模型与运行时状态。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path

from app.config import MOUNT_ROOT


class VolumeState(str, enum.Enum):
    PRESENT = "present"            # 已识别，未处理
    UNLOCKING = "unlocking"        # BitLocker 解密中
    MOUNTING = "mounting"          # 挂载文件系统中
    MOUNTED = "mounted"            # 已挂载（ro/rw 由 mode 表示）
    UNMOUNTING = "unmounting"      # 卸载中
    ERROR = "error"                # 上次操作失败


class MountMode(str, enum.Enum):
    RO = "ro"
    RW = "rw"


# 普通（非 BitLocker）分区允许挂载的文件系统
SUPPORTED_FSTYPES = {"ntfs", "exfat", "vfat", "fat", "ext2", "ext3", "ext4"}


class CredentialKind(str, enum.Enum):
    PASSWORD = "password"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class PartitionInfo:
    disk_id: str
    number: int                     # 分区序号（1 起）
    name: str                       # sdb1
    path: str                       # /dev/sdb1
    size: int                       # 字节
    fstype: str                     # blkid 探测：BitLocker / ntfs / exfat ...
    label: str
    uuid: str                       # 文件系统/卷 UUID
    partuuid: str
    bitlocker: bool

    @property
    def key(self) -> str:
        """卷的稳定标识：优先 PARTUUID，其次 UUID，最后 盘ID+分区号。"""
        return self.partuuid or self.uuid or f"{self.disk_id}-p{self.number}"

    @property
    def supported(self) -> bool:
        if self.bitlocker:
            return True
        return self.fstype.lower() in SUPPORTED_FSTYPES

    @property
    def mount_dir(self) -> Path:
        return MOUNT_ROOT / self.disk_id / f"part{self.number}"

    @property
    def dislocker_file(self) -> Path:
        return self.mount_dir / "dislocker-file"

    @property
    def fs_dir(self) -> Path:
        return self.mount_dir / "fs"

    def to_dict(self, runtime: "VolumeRuntime | None" = None, remembered: bool = False) -> dict:
        data = {
            "key": self.key,
            "diskId": self.disk_id,
            "number": self.number,
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "fstype": self.fstype,
            "label": self.label,
            "uuid": self.uuid,
            "bitlocker": self.bitlocker,
            "supported": self.supported,
            "remembered": remembered,
            "state": VolumeState.PRESENT.value,
            "mode": None,
            "error": None,
            "mountedPath": None,
        }
        if runtime is not None:
            data.update(runtime.to_dict())
        return data


@dataclass(frozen=True)
class DiskInfo:
    disk_id: str                    # 序列号优先，缺失回退内核设备名
    name: str                       # sdb
    path: str                       # /dev/sdb
    vendor: str
    model: str
    serial: str
    tran: str                       # usb / sata ...
    size: int
    removable: bool
    partitions: list[PartitionInfo] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        label = " ".join(p for p in (self.vendor, self.model) if p).strip()
        return label or self.name

    def to_dict(self, runtimes: dict, remembered_keys: set[str]) -> dict:
        return {
            "id": self.disk_id,
            "name": self.name,
            "path": self.path,
            "vendor": self.vendor,
            "model": self.model,
            "displayName": self.display_name,
            "serial": self.serial,
            "tran": self.tran,
            "size": self.size,
            "removable": self.removable,
            "partitions": [
                p.to_dict(runtimes.get(p.key), remembered=p.key in remembered_keys)
                for p in self.partitions
            ],
        }


@dataclass
class VolumeRuntime:
    key: str
    device: str                     # 当前 /dev/sdXn（热插拔后可能变化）
    bitlocker: bool
    state: VolumeState = VolumeState.PRESENT
    mode: MountMode | None = None
    error: str | None = None
    credential_kind: CredentialKind | None = None  # 当前挂载所用凭据类型（内存，不落明文）
    auto_unlock_tried: bool = False
    mount_dir: str | None = None
    fs_dir: str | None = None
    engine: str | None = None         # BitLocker 解锁引擎：dislocker / cryptsetup
    dm_name: str | None = None        # cryptsetup 映射设备名（/dev/mapper/<dm_name>）

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "mode": self.mode.value if self.mode else None,
            "error": self.error,
            "mountedPath": self.fs_dir if self.state == VolumeState.MOUNTED else None,
        }
