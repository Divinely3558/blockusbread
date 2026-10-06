"""本地存储：Docker bind 到挂载根（/mnt/usb/<名称>）的宿主机目录。

零配置发现：解析 /proc/self/mountinfo，挂载根下一级、root 非 "/"
的挂载点即本地存储（Docker bind 的 root 是宿主机内子路径；外接卷
与远程 FUSE 均为整文件系统挂载，root 为 "/"）。注意不能用「源是否
块设备」区分——bind 在 /proc/mounts 里显示的源恰恰就是块设备名。
始终可读写、无状态机、无 API；Docker 持有 bind 引用，任何代码都
不得 umount 这些目录。未做 bind 映射的目录不显示。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.config import MOUNT_ROOT
from app.mounts.commands import read_mountinfo

log = logging.getLogger("stores.local")


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
        """发现本地存储：挂载根下一级、mountinfo root 非 "/" 的挂载点（Docker bind）。"""
        prefix = str(self._root)
        names: set[str] = set()
        try:
            mounts = read_mountinfo()
        except OSError:
            return []
        for mi in mounts:
            if not mi.mount_point.startswith(prefix + "/"):
                continue
            rel = mi.mount_point[len(prefix) + 1:]
            if not rel or "/" in rel:
                continue
            # root 为 "/" 是整文件系统挂载（外接卷 / 远程 FUSE），不是本地
            if mi.root == "/":
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
