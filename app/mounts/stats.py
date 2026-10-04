"""按已挂载卷统计 SFTP 实时传输速率。

块设备 /proc/diskstats 受页缓存影响很大（写入先入缓存、刷盘是零散突发，
读取命中缓存时统计为 0），不能反映 SFTP 客户端实际感受到的传输速度。

本模块直接采样 SFTP 会话进程（sshd 的 per-connection 子进程，本容器只允许
SFTP）打开的文件描述符：
- /proc/<pid>/fd/<fd> 符号链接 -> /mnt/usb/<磁盘ID>/part<序号>/fs/...
- /proc/<pid>/fdinfo/<fd> 中的 pos（文件偏移）增量即该 fd 上实际
  读/写的字节数，与页缓存无关；flags 低位区分读 / 写方向
    O_ACCMODE=0（RDONLY）-> rx 下载，1（WRONLY）-> tx 上传，2（RDWR）各半
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from app.config import MOUNT_ROOT

__all__ = ["SpeedMonitor"]

_PROC = "/proc"
_O_ACCMODE = 3

log = logging.getLogger("speed")


def _is_sshd_session(pid: str) -> bool:
    """会话子进程 cmdline 形如 "sshd: admin@notty\\0"；监听进程是 "sshd -D -e"。"""
    try:
        raw = os.readlink(f"{_PROC}/{pid}/exe")
    except OSError:
        return False
    if os.path.basename(raw) != "sshd":
        return False
    try:
        with open(f"{_PROC}/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return cmdline.startswith("sshd:")


def _read_fdinfo(pid: str, fd: str) -> tuple[int, int] | None:
    """返回 (pos, flags)；读不到返回 None。"""
    pos: int | None = None
    flags: int | None = None
    try:
        with open(f"{_PROC}/{pid}/fdinfo/{fd}", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("pos:"):
                    pos = int(line.split()[1])
                elif line.startswith("flags:"):
                    flags = int(line.split()[1], 8)
    except OSError:
        return None
    if pos is None or flags is None:
        return None
    return pos, flags


class SpeedMonitor:
    """周期采样每个已挂载卷的 SFTP 读/写速率（字节/秒）。"""

    def __init__(self, manager, interval: float = 2.0) -> None:
        self._manager = manager
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._stopping = False
        # (pid, fd) -> (monotonic 时间戳, pos)
        self._last: dict[tuple[str, str], tuple[float, int]] = {}
        self._rates: dict[str, dict[str, float]] = {}

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="speed-monitor")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()

    def rates(self) -> dict[str, dict[str, float]]:
        return {key: dict(v) for key, v in self._rates.items()}

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await asyncio.to_thread(self._sample)
            except Exception:
                log.exception("速率采样异常")
            await asyncio.sleep(self._interval)

    def _volume_index(self) -> dict[str, str]:
        """{'<磁盘ID>/part<序号>': 卷 key}，只含当前已挂载的卷。"""
        index: dict[str, str] = {}
        for part, _runtime in self._manager.mounted_volumes():
            index[f"{part.disk_id}/part{part.number}"] = part.key
        return index

    def _sample(self) -> None:
        now = time.monotonic()
        volumes = self._volume_index()
        root_prefix = str(MOUNT_ROOT).rstrip("/") + "/"

        # 卷 key -> 本周期累计字节
        rx_bytes: dict[str, int] = {}
        tx_bytes: dict[str, int] = {}
        seen: set[tuple[str, str]] = set()

        try:
            pids = [p for p in os.listdir(_PROC) if p.isdigit()]
        except OSError:
            return

        for pid in pids:
            if not _is_sshd_session(pid):
                continue
            fd_dir = f"{_PROC}/{pid}/fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                if not target.startswith(root_prefix):
                    continue
                # /mnt/usb/<磁盘ID>/part<序号>/fs/... -> 定位卷
                tail = target[len(root_prefix):].split("/", 3)
                if len(tail) < 3 or tail[2] != "fs":
                    continue
                volume_key = volumes.get(f"{tail[0]}/{tail[1]}")
                if volume_key is None:
                    continue
                # 只统计常规文件（目录句柄 readdir 偏移无意义）
                try:
                    if not os.path.isfile(target):
                        continue
                except OSError:
                    continue
                info = _read_fdinfo(pid, fd)
                if info is None:
                    continue
                pos, flags = info
                ident = (pid, fd)
                seen.add(ident)

                prev = self._last.get(ident)
                if prev is not None:
                    delta = pos - prev[1]
                    if delta > 0:
                        mode = flags & _O_ACCMODE
                        if mode == 0:       # O_RDONLY -> 下载
                            rx_bytes[volume_key] = rx_bytes.get(volume_key, 0) + delta
                        elif mode == 1:     # O_WRONLY -> 上传
                            tx_bytes[volume_key] = tx_bytes.get(volume_key, 0) + delta
                        else:               # O_RDWR：罕见，均摊
                            half = delta // 2
                            rx_bytes[volume_key] = rx_bytes.get(volume_key, 0) + half
                            tx_bytes[volume_key] = tx_bytes.get(volume_key, 0) + delta - half
                # 新出现的 fd 以当前 pos 为基线（文件通常顺序读写，pos 即本窗口内流量）
                self._last[ident] = (now, pos)

        # 已关闭的 fd / 消失的会话清理
        for ident in [k for k in self._last if k not in seen]:
            self._last.pop(ident, None)

        # 汇总速率（本周期有 fd 活动的卷；无流量的卷速率归零）
        active_keys = set(rx_bytes) | set(tx_bytes)
        for key in volumes.values():
            if key in active_keys:
                self._rates[key] = {
                    "rx": round(rx_bytes.get(key, 0) / self._interval, 1),
                    "tx": round(tx_bytes.get(key, 0) / self._interval, 1),
                }
            else:
                self._rates[key] = {"rx": 0.0, "tx": 0.0}
        # 卸载/拔出的卷移除
        for key in [k for k in self._rates if k not in volumes.values()]:
            self._rates.pop(key, None)
