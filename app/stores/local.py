"""本地存储：docker-compose bind 到挂载根（/mnt/usb/<名称>）的宿主机目录。

零配置发现：解析 /proc/mounts，挂载根下源不是块设备、文件系统不是 FUSE
的一级挂载点即本地存储（Docker bind 的源为宿主机路径），目录名即存储名。
始终可读写、无状态机、无 API；Docker 持有 bind 引用，任何代码都不得
umount 这些目录。未做 bind 映射的目录不显示。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.config import MOUNT_ROOT

log = logging.getLogger("stores.local")


def _proc_mounts() -> list[tuple[str, str, str]]:
    """解析 /proc/mounts：[(挂载点, 源, 文件系统类型), ...]。"""
    rows: list[tuple[str, str, str]] = []
    with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            fields = line.split()
            if len(fields) >= 3:
                rows.append((
                    fields[1].replace("\\040", " "),
                    fields[0].replace("\\040", " "),
                    fields[2],
                ))
    return rows


class LocalStoreManager:
    def __init__(self, mount_root: Path = MOUNT_ROOT) -> None:
        self._root = mount_root

    @property
    def root(self) -> Path:
        return self._root

    def ensure_root(self) -> None:
        """启动时建好挂载根（Dockerfile 也有一层兜底）。"""
        self._root.mkdir(parents=True, exist_ok=True)

    def fs_dir(self, name: str) -> Path:
        return self._root / name

    def exists(self, name: str) -> bool:
        return self.fs_dir(name).is_dir()

    def _discover(self) -> list[str]:
        """发现本地存储：挂载根下源不是块设备、文件系统不是 FUSE 的一级挂载点。"""
        prefix = str(self._root)
        names: set[str] = set()
        try:
            mounts = _proc_mounts()
        except OSError:
            return []
        for mp, src, fstype in mounts:
            if not mp.startswith(prefix + "/"):
                continue
            rel = mp[len(prefix) + 1:]
            if not rel or "/" in rel:
                continue
            # 外接卷挂载源是块设备（/dev/*），远程存储是 FUSE；都不是本地
            if src.startswith("/dev/") or fstype.startswith("fuse"):
                continue
            names.add(rel)
        return sorted(names)

    def list_stores(self) -> list[dict]:
        """快照：仅列出真实存在的目录型 bind 存储。"""
        out = []
        for name in self._discover():
            if name.startswith("."):
                continue
            if not (self._root / name).is_dir():
                continue
            out.append({
                "key": f"local:{name}",
                "name": name,
                "state": "mounted",
                "mode": "rw",
                "sftpPath": name,
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
