"""统一卷注册表：把本地 / 外接 / 远程三类存储适配为 VolumeRef。

文件浏览、传输、容量、速率只依赖本模块：
- lookup(key) 给文件类 API 提供「当前可用（已挂载）」的卷根；
- mounted_refs() 给速率统计 / 容量汇总提供全部已挂载卷；
- snapshot() 给 /api/volumes 提供三类存储的完整状态。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from app.models import MountMode
from app.mounts.manager import MountManager, VolumeNotFound
from app.stores.local import LocalStoreManager
from app.stores.remote import RemoteStoreManager

__all__ = ["StoreRegistry", "VolumeNotFound", "VolumeRef", "VolumeUnavailable"]


class VolumeUnavailable(RuntimeError):
    """卷存在但当前不可用（未挂载 / 错误状态）。"""


@dataclass(frozen=True)
class VolumeRef:
    key: str            # 外接：PARTUUID/UUID/盘ID-pN；本地 "local:<名称>"；远程 "remote:<挂载id>"
    kind: str           # "external" | "local" | "remote"
    name: str           # 展示名
    fs_dir: Path        # 容器内真实文件系统根（所有文件 API 的根）
    sftp_path: str      # SFTP chroot 内相对子路径
    writable: bool
    ejectable: bool     # 仅外接 True
    device: str | None = None  # 底层块设备节点（外接卷的分区），FUSE 挂载回退统计用


class StoreRegistry:
    def __init__(
        self,
        manager: MountManager,
        local: LocalStoreManager,
        remote: RemoteStoreManager,
    ) -> None:
        self._manager = manager
        self._local = local
        self._remote = remote

    # ------------------------------------------------------------ 快照

    def snapshot(self) -> dict:
        disks_snap = self._manager.snapshot()
        return {
            "local": self._local.list_stores(),
            "disks": disks_snap["disks"],
            "remote": self._remote.list_stores(),
            "rememberEnabled": disks_snap["rememberEnabled"],
        }

    # ------------------------------------------------------------ 查找

    def _local_ref(self, name: str) -> VolumeRef:
        if not self._local.exists(name):
            raise VolumeNotFound(f"local:{name}")
        return VolumeRef(
            key=f"local:{name}",
            kind="local",
            name=name,
            fs_dir=self._local.fs_dir(name),
            sftp_path=name,
            writable=True,
            ejectable=False,
        )

    def _remote_ref(self, key: str) -> VolumeRef:
        """key 形如 remote:<挂载id>；展示名 = 组名:远端路径。"""
        store = self.store_dict(key)
        if store["state"] != "mounted":
            raise VolumeUnavailable(
                f"远程存储「{store['name']}」当前未挂载（{store['state']}）"
            )
        return VolumeRef(
            key=store["key"],
            kind="remote",
            name=f"{store['name']}:{store['remotePath']}",
            fs_dir=self._remote.fs_dir(key),
            sftp_path=store["sftpPath"],
            writable=True,
            ejectable=False,
        )

    def _external_ref(self, key: str) -> VolumeRef:
        part, runtime = self._manager.mounted_partition(key)
        writable = runtime.mode == MountMode.RW
        label = part.label or f"分区 {part.number}"
        return VolumeRef(
            key=part.key,
            kind="external",
            name=f"{runtime.drive}:{label}" if runtime.drive else label,
            fs_dir=Path(runtime.fs_dir),
            sftp_path=runtime.sftp_path or "",
            writable=writable,
            ejectable=True,
            device=runtime.device,
        )

    def lookup(self, key: str) -> VolumeRef:
        """按 key 取当前可用（已挂载）的卷；找不到抛 VolumeNotFound。

        外接卷沿用 MountManager 的挂载态判定；远程卷要求 state=mounted；
        本地卷恒可用。
        """
        if key.startswith("local:"):
            return self._local_ref(key[len("local:"):])
        if key.startswith("remote:"):
            return self._remote_ref(key)
        return self._external_ref(key)

    def name_of(self, key: str) -> str:
        """卷展示名：lookup 失败时尽力回退（远程任意状态、本地存在性宽松）。"""
        try:
            return self.lookup(key).name
        except (VolumeNotFound, VolumeUnavailable):
            pass
        if key.startswith("local:"):
            return key[len("local:"):]
        if key.startswith("remote:"):
            try:
                store = self.store_dict(key)
            except VolumeNotFound:
                return key
            return f"{store['name']}:{store['remotePath']}"
        return key

    def store_dict(self, key: str) -> dict:
        """远程存储的完整快照行（任意状态）。"""
        for row in self._remote.list_stores():
            if row["key"] == key:
                return row
        raise VolumeNotFound(key)

    # ------------------------------------------------------------ 枚举

    def mounted_refs(self) -> list[VolumeRef]:
        """三类存储当前可用的全部卷（速率统计、容量汇总用）。"""
        refs: list[VolumeRef] = []
        for part, runtime in self._manager.mounted_volumes():
            label = part.label or f"分区 {part.number}"
            refs.append(VolumeRef(
                key=part.key,
                kind="external",
                name=f"{runtime.drive}:{label}" if runtime.drive else label,
                fs_dir=Path(runtime.fs_dir),
                sftp_path=runtime.sftp_path or "",
                writable=runtime.mode == MountMode.RW,
                ejectable=True,
                device=runtime.device,
            ))
        refs.extend(self._local_ref(row["name"]) for row in self._local.list_stores())
        for row in self._remote.list_stores():
            if row["state"] == "mounted":
                try:
                    refs.append(self._remote_ref(row["key"]))
                except (VolumeNotFound, VolumeUnavailable):
                    pass
        return refs

    # ------------------------------------------------------------ 容量

    async def usage(self) -> dict[str, dict[str, int]]:
        """三类已挂载卷的容量：{key: {total, used, avail}}。

        - 外接：statvfs 各挂载点（线程内）；
        - 本地：statvfs 宿主机目录（快）；
        - 远程：取健康探测的缓存（sshfs statvfs 可能因断网卡住，
          不在请求路径上直接碰远端）。
        """
        result: dict[str, dict[str, int]] = {}
        external, local = await asyncio.gather(
            self._manager.usage(),
            asyncio.to_thread(self._local.usage),
        )
        result.update(external)
        result.update(local)
        result.update(self._remote.usage_snapshot())
        return result

    async def avail_bytes(self, ref: VolumeRef) -> int | None:
        """目标卷当前可用字节数（传输前空间预检用）。

        外接 / 本地直接 statvfs 挂载点；远程取健康探测的缓存，
        缓存缺失（刚挂载、探测未跑、网络断开）时返回 None，
        由调用方跳过预检——不在请求路径上碰远端，也避免误拦。
        """
        if ref.kind == "remote":
            row = self._remote.usage_snapshot().get(ref.key)
            return row["avail"] if row else None
        try:
            st = await asyncio.to_thread(os.statvfs, ref.fs_dir)
        except OSError:
            return None
        return st.f_bavail * st.f_frsize
