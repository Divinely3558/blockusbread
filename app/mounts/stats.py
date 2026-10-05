"""各卷实时速率：硬盘块设备的真实读/写吞吐。

速率直接读块设备统计（/sys/dev/block/<maj:min>/stat 的扇区计数差分，
每扇区 512 字节），反映硬盘实际发生的读/写——这正是速度计想表达的
“硬盘现在多快”，而不是某个客户端链路的网速。卷到设备的解析：
- 挂载点 st_dev：外接分区、本地 bind 目录、cryptsetup 映射（dm-*）
- FUSE（dislocker 解锁的 BitLocker）：st_dev 是匿名设备，回退到卷的
  底层分区设备节点（VolumeRef.device）
- 远程 sshfs：无块设备，速率恒 0

SFTP 会话快照（连接数 / 用户 / 各卷打开中的文件）仍通过扫描 sshd
会话进程的文件描述符获得，供 /api/sessions 与「占用中」提示使用。
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
_SECTOR_SIZE = 512

log = logging.getLogger("speed")


def _session_user(pid: str) -> str | None:
    """会话子进程 cmdline 形如 "sshd: admin@notty\\0"；返回登录用户名。

    监听进程是 "sshd -D -e"，返回 None；非 sshd 进程同样返回 None。
    """
    try:
        raw = os.readlink(f"{_PROC}/{pid}/exe")
    except OSError:
        return None
    if os.path.basename(raw) != "sshd":
        return None
    try:
        with open(f"{_PROC}/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return None
    if not cmdline.startswith("sshd:"):
        return None
    # "sshd: admin@notty " -> admin；无 @ 的是未认证/监听子进程，不算会话
    body = cmdline[len("sshd:"):].strip()
    if "@" not in body:
        return None
    return body.split("@", 1)[0].strip() or "?"


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


def _parse_block_stat(text: str) -> tuple[int, int] | None:
    """解析块设备 stat 行：返回（累计读扇区, 累计写扇区）；无效返回 None。"""
    parts = text.split()
    if len(parts) <= 6:
        return None
    try:
        return int(parts[2]), int(parts[6])
    except ValueError:
        return None


def _read_block_stat(dev: str) -> tuple[int, int] | None:
    """读取块设备累计扇区数：dev 形如 "8:0"（对应 /sys/dev/block/8:0/stat）。"""
    try:
        with open(f"/sys/dev/block/{dev}/stat", encoding="utf-8") as fh:
            return _parse_block_stat(fh.read())
    except OSError:
        return None


class SpeedMonitor:
    """周期采样：每卷底层硬盘的读/写速率 + SFTP 会话快照。"""

    def __init__(self, registry, interval: float = 2.0) -> None:
        self._registry = registry
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._stopping = False
        # dev "maj:min" -> (采样时刻, 累计读扇区, 累计写扇区)
        self._dev_last: dict[str, tuple[float, int, int]] = {}
        self._rates: dict[str, dict[str, float]] = {}
        # 最近一次采样的 SFTP 会话快照（连接数 / 用户 / 打开文件）
        self._session_info: dict = {"connections": 0, "users": [], "openFiles": [], "volumes": {}}

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="speed-monitor")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()

    def rates(self) -> dict[str, dict[str, float]]:
        return {key: dict(v) for key, v in self._rates.items()}

    def session_info(self) -> dict:
        return {
            "connections": self._session_info["connections"],
            "users": list(self._session_info["users"]),
            "openFiles": [dict(r) for r in self._session_info["openFiles"]],
            "volumes": {k: [dict(r) for r in v]
                        for k, v in self._session_info["volumes"].items()},
        }

    def open_files_for(self, volume_key: str) -> list[dict]:
        return [dict(r) for r in self._session_info["volumes"].get(volume_key, [])]

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await asyncio.to_thread(self._sample)
            except Exception:
                log.exception("速率采样异常")
            await asyncio.sleep(self._interval)

    # ------------------------------------------------------------ 硬盘速率

    def _resolve_device(self, ref) -> str | None:
        """卷 → 块设备 "maj:min"；无块设备（远程 / 无底层信息的 FUSE）返回 None。"""
        try:
            st = os.stat(ref.fs_dir)
        except OSError:
            return None
        dev = f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
        if os.path.exists(f"/sys/dev/block/{dev}/stat"):
            return dev
        # FUSE（dislocker 解锁的 BitLocker）：st_dev 是匿名设备，
        # 回退到卷的底层分区 / 映射设备节点
        if ref.device:
            try:
                st = os.stat(ref.device)
            except OSError:
                return None
            return f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
        return None

    def _sample_disk_rates(self, refs, now: float) -> None:
        """各卷底层硬盘的读/写速率：扇区计数差分 ÷ 采样间隔。"""
        zero = {"rx": 0.0, "tx": 0.0}
        dev_seen: set[str] = set()
        for ref in refs:
            dev = self._resolve_device(ref)
            if dev is None:
                self._rates[ref.key] = zero
                continue
            counts = _read_block_stat(dev)
            if counts is None:
                self._rates[ref.key] = zero
                continue
            dev_seen.add(dev)
            prev = self._dev_last.get(dev)
            self._dev_last[dev] = (now, counts[0], counts[1])
            if prev is None or now <= prev[0]:
                self._rates[ref.key] = zero   # 首个周期只建基线
                continue
            dt = now - prev[0]
            rx = max(0, counts[0] - prev[1]) * _SECTOR_SIZE / dt
            tx = max(0, counts[1] - prev[2]) * _SECTOR_SIZE / dt
            self._rates[ref.key] = {"rx": round(rx, 1), "tx": round(tx, 1)}
        # 卸载/拔出的卷移除；不再活动的设备基线清理
        known = {ref.key for ref in refs}
        for key in [k for k in self._rates if k not in known]:
            self._rates.pop(key, None)
        self._dev_last = {d: v for d, v in self._dev_last.items() if d in dev_seen}

    # ------------------------------------------------------------ SFTP 会话快照

    def _sample_sessions(self, refs) -> None:
        """扫描 sshd 会话进程的文件描述符：连接数 / 用户 / 各卷打开中的文件。"""
        volumes = []
        for ref in refs:
            prefix = str(ref.fs_dir)
            if not prefix.endswith("/"):
                prefix += "/"
            volumes.append((prefix, ref.key, ref.name))
        volumes.sort(key=lambda r: len(r[0]), reverse=True)
        root_prefix = str(MOUNT_ROOT).rstrip("/") + "/"

        connected_users: set[str] = set()
        connection_count = 0
        open_rows: set[tuple[str, str, str, str, bool]] = set()

        try:
            pids = [p for p in os.listdir(_PROC) if p.isdigit()]
        except OSError:
            pids = []

        for pid in pids:
            user = _session_user(pid)
            if user is None:
                continue
            connection_count += 1
            connected_users.add(user)
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
                # fd 目标按注册表 fs_dir 最长前缀归属卷
                match = next(
                    ((p, k, n) for p, k, n in volumes if target.startswith(p)),
                    None,
                )
                if match is None:
                    continue
                prefix, volume_key, volume_name = match
                rel_path = target[len(prefix):]
                # 只统计常规文件（目录句柄 readdir 偏移无意义）
                try:
                    if not os.path.isfile(target):
                        continue
                except OSError:
                    continue
                info = _read_fdinfo(pid, fd)
                if info is None:
                    continue
                _pos, flags = info
                writable = bool(flags & _O_ACCMODE)   # WRONLY=1 / RDWR=2
                open_rows.add((user, volume_key, volume_name, rel_path, writable))

        # 会话快照：打开文件按卷归组，供面板与"占用中"提示使用
        files = [
            {"user": user, "volume": key, "volumeName": name,
             "path": path, "writable": writable}
            for user, key, name, path, writable in sorted(open_rows)
        ]
        by_volume: dict[str, list[dict]] = {}
        for row in files:
            by_volume.setdefault(row["volume"], []).append(row)
        self._session_info = {
            "connections": connection_count,
            "users": sorted(connected_users),
            "openFiles": files,
            "volumes": by_volume,
        }

    def _sample(self) -> None:
        now = time.monotonic()
        refs = self._registry.mounted_refs()
        self._sample_disk_rates(refs, now)
        self._sample_sessions(refs)
