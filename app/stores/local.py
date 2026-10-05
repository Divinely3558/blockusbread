"""本地存储：docker-compose 映射到 /mnt/usb/local/<名称> 的宿主机目录。

零配置发现：一级子目录即一个本地存储，目录名即存储名；不做 ismount 限制
（兼容「直接把一个父目录 bind 到 /mnt/usb/local」的用法）。始终可读写、
无状态机、无 API；Docker 持有 bind 引用，任何代码都不得 umount 这些目录。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.config import MOUNT_ROOT

log = logging.getLogger("stores.local")


class LocalStoreManager:
    def __init__(self, mount_root: Path = MOUNT_ROOT) -> None:
        self._root = mount_root / "local"

    @property
    def root(self) -> Path:
        return self._root

    def ensure_root(self) -> None:
        """启动时建好 /mnt/usb/local（Dockerfile 也有一层兜底）。"""
        self._root.mkdir(parents=True, exist_ok=True)

    def fs_dir(self, name: str) -> Path:
        return self._root / name

    def exists(self, name: str) -> bool:
        return self.fs_dir(name).is_dir()

    def list_stores(self) -> list[dict]:
        """快照：跳过隐藏条目与非目录；目录名即存储名。"""
        try:
            names = sorted(os.listdir(self._root))
        except OSError:
            return []
        out = []
        for name in names:
            if name.startswith("."):
                continue
            if not (self._root / name).is_dir():
                continue
            out.append({
                "key": f"local:{name}",
                "name": name,
                "state": "mounted",
                "mode": "rw",
                "sftpPath": f"local/{name}",
            })
        return out

    def usage(self) -> dict[str, dict[str, int]]:
        """各本地存储容量（字节）：{key: {total, used, avail}}。"""
        result: dict[str, dict[str, int]] = {}
        for store in self.list_stores():
            try:
                st = os.statvfs(self.fs_dir(store["name"]))
            except OSError:
                continue
            frsize = st.f_frsize
            total = st.f_blocks * frsize
            free_all = st.f_bfree * frsize
            avail = st.f_bavail * frsize
            result[store["key"]] = {
                "total": total,
                "used": max(0, total - free_all),
                "avail": avail,
            }
        return result
