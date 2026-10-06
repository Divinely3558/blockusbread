"""挂载编排器：设备重扫、BitLocker 解锁、普通分区挂载、安全弹出、拔出清理。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from pathlib import Path

from app import secrets_store
from app.config import MOUNT_ROOT
from app.devices import scanner
from app.events import EventBus
from app.models import (
    CredentialKind,
    DiskInfo,
    MountMode,
    PartitionInfo,
    VolumeRuntime,
    VolumeState,
)
from app.mounts.commands import (
    BitlockerCredentialError,
    DeviceInUseError,
    MountError,
    dm_name_for,
    kill_fuse_daemons,
    mount_filesystem,
    probe_fstype,
    run_cryptsetup_close,
    run_cryptsetup_open,
    run_dislocker,
    umount_filesystem,
    warm_up_device,
)

log = logging.getLogger("mount.manager")


class VolumeNotFound(KeyError):
    """卷当前不在已识别的设备列表中。"""


class MountManager:
    def __init__(
        self,
        bus: EventBus,
        secrets: secrets_store.SecretsStore | None,
    ) -> None:
        self._bus = bus
        self._secrets = secrets
        self._disks: list[DiskInfo] = []
        self._volumes: dict[str, VolumeRuntime] = {}
        self._volume_locks: dict[str, asyncio.Lock] = {}
        self._scan_lock = asyncio.Lock()
        # 上次对外快照签名：内容无变化的重扫不推送，避免前端无谓重绘
        self._last_snapshot_sig: str | None = None

    # ------------------------------------------------------------------ 快照

    def snapshot(self) -> dict:
        remembered = self._secrets.all_keys() if self._secrets else set()
        return {
            "disks": [d.to_dict(self._volumes, remembered) for d in self._disks],
            "rememberEnabled": self._secrets is not None,
        }

    def _partition(self, key: str) -> PartitionInfo:
        for disk in self._disks:
            for part in disk.partitions:
                if part.key == key:
                    return part
        raise VolumeNotFound(key)

    def mounted_partition(self, key: str) -> tuple[PartitionInfo, VolumeRuntime]:
        """取已挂载的分区与运行时；未挂载/不存在抛 VolumeNotFound。"""
        part = self._partition(key)
        runtime = self._volumes.get(key)
        if runtime is None or runtime.state != VolumeState.MOUNTED:
            raise VolumeNotFound(key)
        return part, runtime

    def mounted_volumes(self):
        """枚举当前处于已挂载状态的 (PartitionInfo, VolumeRuntime)。"""
        for disk in self._disks:
            for part in disk.partitions:
                runtime = self._volumes.get(part.key)
                if runtime is not None and runtime.state == VolumeState.MOUNTED:
                    yield part, runtime

    async def usage(self) -> dict[str, dict[str, int]]:
        """各已挂载卷的容量（字节）：{key: {total, used, avail}}。

        走独立轮询接口而非 snapshot：可用空间随 SFTP 读写持续变化，
        进 snapshot 会让 SSE 去重签名不断变化，造成前端无谓重绘。
        """
        return await asyncio.to_thread(self._usage_blocking)

    def _usage_blocking(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for part, runtime in self.mounted_volumes():
            if not runtime.fs_dir:
                continue
            try:
                st = os.statvfs(runtime.fs_dir)
            except OSError:
                continue
            frsize = st.f_frsize
            total = st.f_blocks * frsize
            free_all = st.f_bfree * frsize
            avail = st.f_bavail * frsize
            result[part.key] = {
                "total": total,
                "used": max(0, total - free_all),
                "avail": avail,
            }
        return result

    def _lock_for(self, key: str) -> asyncio.Lock:
        return self._volume_locks.setdefault(key, asyncio.Lock())

    async def _notify(self, reason: str) -> None:
        await self._bus.publish("state", {"reason": reason})

    # ------------------------------------------------------------------ 扫描

    async def rescan(self, reason: str = "scan") -> None:
        """重新扫描设备：清理拔出的卷、登记新卷。

        已记住的凭据只用于手动解锁时免输密码，绝不自动解锁挂载。
        """
        async with self._scan_lock:
            try:
                disks = await asyncio.to_thread(scanner.scan)
            except Exception:
                log.exception("设备扫描失败")
                return

            self._disks = disks
            present: dict[str, PartitionInfo] = {}
            for disk in disks:
                for part in disk.partitions:
                    present[part.key] = part

            # 设备路径可能因重新插拔变化；登记新卷 / 更新路径
            for key, part in present.items():
                rt = self._volumes.get(key)
                if rt is None:
                    rt = VolumeRuntime(key=key, device=part.path, bitlocker=part.bitlocker)
                    self._volumes[key] = rt
                    log.info("发现卷 %s (%s, %s)", part.path, part.key,
                             "BitLocker" if part.bitlocker else part.fstype or "无文件系统")
                else:
                    rt.device = part.path

            # 拔出的卷：惰性清理挂载后删除运行时状态
            for key in [k for k in self._volumes if k not in present]:
                await self._cleanup_removed(self._volumes[key])

            # 只有对外快照真的变化时才推送：定时兜底扫描与无关 uevent
            # （NAS 上 md/loop/dm 的 block change）不会再引起前端重绘
            sig = json.dumps(
                self.snapshot(), sort_keys=True, ensure_ascii=False,
                separators=(",", ":"), default=str,
            )
            if sig != self._last_snapshot_sig:
                self._last_snapshot_sig = sig
                await self._notify(reason)

    # ------------------------------------------------------------------ 解锁

    async def unlock_volume(
        self,
        key: str,
        kind: str,
        secret: str,
        writable: bool,
        remember: bool,
        actor: str,
    ) -> None:
        part = self._partition(key)
        if not part.bitlocker:
            raise MountError("该分区不是 BitLocker 卷")

        # secret 留空 = 用已保存凭据解锁（免输密码）；没有已保存凭据则必须输入
        if not secret:
            if self._secrets is None:
                raise MountError("未设置 SECRET_KEY，记忆功能不可用")
            saved = self._secrets.load(key)
            if saved is None:
                raise MountError("该卷没有已保存的凭据，请输入密码或恢复密钥")
            kind, secret, _ = saved
            remember = False
        else:
            if kind not in {CredentialKind.PASSWORD.value, CredentialKind.RECOVERY.value}:
                raise MountError("凭据类型无效")
            if remember and self._secrets is None:
                raise MountError("未设置 SECRET_KEY，记忆功能不可用")

        async with self._lock_for(key):
            rt = self._volumes[key]
            if rt.state == VolumeState.MOUNTED:
                return
            try:
                await self._unlock_locked(part, kind, secret, writable, actor)
            except MountError as exc:
                rt.state = VolumeState.ERROR
                rt.error = str(exc)
                log.info("%s 解锁 %s (%s) 失败：%s", actor, part.path, key, exc)
                await self._partial_cleanup(part.mount_dir, rt)
                rt.engine = None
                await self._notify("unlock-failed")
                raise
            if remember and self._secrets is not None:
                mode = MountMode.RW.value if writable else MountMode.RO.value
                self._secrets.save(key, kind, secret, mode)
            log.info(
                "%s 解锁 %s (%s, %s) 成功",
                actor, part.path, key, "rw" if writable else "ro",
            )
            await self._notify("unlocked")

    async def _unlock_locked(
        self,
        part: PartitionInfo,
        kind: str,
        secret: str,
        writable: bool,
        actor: str,
    ) -> None:
        rt = self._volumes[part.key]
        rt.error = None

        # 部分 USB 硬盘盒在盘体休眠后，唤醒后的首个 I/O 会长时间无响应
        # （dislocker/mount 命令超时），盘体唤醒后重试即秒成功：
        # 非凭据类失败时清理半成品、整体重试一次；凭据错误立即返回。
        last_exc: MountError | None = None
        for attempt in (1, 2):
            rt.state = VolumeState.UNLOCKING
            await self._notify("unlocking")
            try:
                # 先直读唤醒可能休眠的 USB 盘体，避免 FUSE 挂载卡死
                await warm_up_device(part.path)
                await self._open_and_mount_locked(part, rt, kind, secret, writable)
                return
            except BitlockerCredentialError:
                raise
            except DeviceInUseError:
                raise
            except MountError as exc:
                last_exc = exc
                log.warning("卷 %s 第 %d 次解锁尝试失败：%s", part.path, attempt, exc)
                await self._partial_cleanup(part.mount_dir, rt)
                rt.engine = None
                if attempt == 1:
                    await asyncio.sleep(2)
        assert last_exc is not None
        raise last_exc

    async def _open_and_mount_locked(
        self,
        part: PartitionInfo,
        rt: VolumeRuntime,
        kind: str,
        secret: str,
        writable: bool,
    ) -> None:
        readonly = not writable

        # 优先 cryptsetup（内核 bitlk，秒级完成，不受部分 USB 硬盘盒下
        # dislocker/FUSE 挂死问题影响）；
        # 仅当内核不支持或无法打开时才回退 dislocker（FUSE，兼容 Win7 等旧格式）。
        # 凭据已被明确拒绝时直接上抛，不再尝试第二个引擎。
        rt.dm_name = dm_name_for(part.key)
        try:
            target = await run_cryptsetup_open(
                device=part.path,
                secret=secret,
                readonly=readonly,
                dm_name=rt.dm_name,
            )
            rt.engine = "cryptsetup"
        except BitlockerCredentialError:
            raise
        except DeviceInUseError:
            raise
        except MountError as exc:
            log.info("cryptsetup 无法打开卷 %s（%s），改用 dislocker", part.path, exc)
            rt.dm_name = None
            await run_dislocker(
                device=part.path,
                secret=secret,
                kind=kind,
                readonly=readonly,
                mount_dir=part.mount_dir,
            )
            rt.engine = "dislocker"
            target = part.dislocker_file
        rt.credential_kind = CredentialKind(kind)
        rt.mount_dir = str(part.mount_dir)

        inner = await probe_fstype(target)
        log.debug("卷 %s 内层文件系统：%s（引擎 %s）", part.path, inner or "未知", rt.engine)

        rt.state = VolumeState.MOUNTING
        await self._notify("mounting")
        await mount_filesystem(
            target=target,
            fstype=inner,
            readonly=readonly,
            mount_point=part.fs_dir,
        )
        rt.state = VolumeState.MOUNTED
        rt.mode = MountMode.RW if writable else MountMode.RO
        rt.fs_dir = str(part.fs_dir)

    # ------------------------------------------------------------------ 普通挂载

    async def mount_plain(self, key: str, writable: bool, actor: str) -> None:
        part = self._partition(key)
        if part.bitlocker:
            raise MountError("BitLocker 卷请使用解锁操作")
        if not part.supported:
            raise MountError(f"不支持的文件系统：{part.fstype or '未知'}")

        async with self._lock_for(key):
            rt = self._volumes[key]
            if rt.state == VolumeState.MOUNTED:
                return
            rt.error = None
            try:
                rt.state = VolumeState.MOUNTING
                await self._notify("mounting")
                await mount_filesystem(
                    target=Path(part.path),
                    fstype=part.fstype,
                    readonly=not writable,
                    mount_point=part.fs_dir,
                )
            except MountError as exc:
                rt.state = VolumeState.ERROR
                rt.error = str(exc)
                log.info("%s 挂载 %s (%s) 失败：%s", actor, part.path, key, exc)
                await self._partial_cleanup(part.mount_dir, rt)
                rt.engine = None
                await self._notify("mount-failed")
                raise
            rt.state = VolumeState.MOUNTED
            rt.mode = MountMode.RW if writable else MountMode.RO
            rt.fs_dir = str(part.fs_dir)
            rt.mount_dir = str(part.mount_dir)
            log.info("%s 挂载 %s (%s, %s) 成功", actor, part.path, key,
                     "rw" if writable else "ro")
            await self._notify("mounted")

    # ------------------------------------------------------------------ 弹出

    async def eject_volume(self, key: str, actor: str) -> None:
        part = self._partition(key)
        async with self._lock_for(key):
            rt = self._volumes[key]
            # 以内核挂载实况为准（状态机可能因上次失败停在中间态）
            live = await asyncio.to_thread(os.path.ismount, part.fs_dir)
            if not live:
                # 挂载已不在：清理可能残留的 FUSE 守护进程/dm 映射/目录后复位，
                # 不允许“假成功”——状态必须与内核实况一致
                if part.bitlocker:
                    with contextlib.suppress(MountError):
                        await self._teardown_bitlocker(rt, part.mount_dir, lazy=True)
                await self._remove_dirs(part.mount_dir, ignore_errors=True)
                rt.state = VolumeState.PRESENT
                rt.mode = None
                rt.fs_dir = None
                rt.mount_dir = None
                rt.credential_kind = None
                rt.error = None
                # 弹出后保持锁定；重新插拔或手动解锁后才再次可用
                log.info("%s 安全弹出卷 %s (%s)（挂载已不存在，完成残留清理）",
                         actor, part.path, key)
                await self._notify("ejected")
                return
            rt.state = VolumeState.UNMOUNTING
            rt.error = None
            await self._notify("unmounting")
            try:
                await self._teardown_locked(part, lazy=False)
            except MountError as exc:
                # 关键：失败必须回滚为已挂载，否则前端会一直停在“处理中”
                rt.state = VolumeState.MOUNTED
                rt.error = str(exc)
                log.info("%s 弹出 %s (%s) 失败：%s", actor, part.path, key, exc)
                await self._notify("eject-busy")
                raise
            rt.state = VolumeState.PRESENT
            rt.mode = None
            rt.fs_dir = None
            rt.mount_dir = None
            rt.credential_kind = None
            rt.error = None
            # 弹出后保持锁定；重新插拔或手动解锁后才再次可用
            log.info("%s 安全弹出卷 %s (%s)", actor, part.path, key)
            await self._notify("ejected")

    async def eject_disk(self, disk_id: str, actor: str) -> int:
        disk = next((d for d in self._disks if d.disk_id == disk_id), None)
        if disk is None:
            raise VolumeNotFound(disk_id)
        mounted = [
            p for p in disk.partitions
            if self._volumes.get(p.key)
            and self._volumes[p.key].state == VolumeState.MOUNTED
        ]
        for part in mounted:
            await self.eject_volume(part.key, actor)
        log.info("%s 弹出整块硬盘 %s（%d 个卷已卸载，可安全拔除）",
                 actor, disk.path, len(mounted))
        return len(mounted)

    async def forget(self, key: str) -> None:
        if self._secrets is None:
            return
        self._secrets.delete(key)
        await self._notify("forgot")

    # ------------------------------------------------------------------ 清理

    async def _teardown_locked(self, part: PartitionInfo, lazy: bool) -> None:
        rt = self._volumes[part.key]
        rt.state = VolumeState.UNMOUNTING
        await self._notify("unmounting")
        # 安全弹出（非 lazy）时先驱逐 SFTP 客户端等占用者；惰性路径直接卸载
        await umount_filesystem(part.fs_dir, lazy=lazy, evict=not lazy)
        if part.bitlocker:
            await self._teardown_bitlocker(rt, part.mount_dir, lazy)
        await self._remove_dirs(part.mount_dir)

    async def _teardown_bitlocker(
        self, rt: VolumeRuntime, mount_dir: Path, lazy: bool
    ) -> None:
        """按解锁引擎清理：dislocker 卸 FUSE 挂载点，cryptsetup 关映射设备。"""
        if rt.engine == "cryptsetup" and rt.dm_name:
            # 前台型 FUSE 守护进程卸载后可能仍持有 dm 块设备，
            # 不关进程 cryptsetup close 会 EBUSY
            await kill_fuse_daemons(rt.dm_name, str(mount_dir / "fs"))
            try:
                await run_cryptsetup_close(rt.dm_name)
            except MountError:
                if not lazy:
                    raise
                log.warning("关闭映射设备 %s 失败（设备可能已拔出）", rt.dm_name)
            rt.dm_name = None
        else:
            await umount_filesystem(mount_dir, lazy=lazy)

    async def _partial_cleanup(self, mount_dir, rt: VolumeRuntime | None = None) -> None:
        """操作失败后的半成品清理：全部惰性卸载并删除目录，忽略错误。"""
        fs_dir = mount_dir / "fs"
        for target in (fs_dir, mount_dir):
            try:
                await umount_filesystem(target, lazy=True)
            except MountError:
                pass
        if rt is not None and rt.dm_name:
            await kill_fuse_daemons(rt.dm_name, str(fs_dir), str(mount_dir))
            try:
                await run_cryptsetup_close(rt.dm_name)
            except MountError:
                pass
            rt.dm_name = None
        await self._remove_dirs(mount_dir, ignore_errors=True)

    async def _cleanup_removed(self, rt: VolumeRuntime) -> None:
        """设备已拔出：文件系统与解密层全部惰性卸载，清除状态。"""
        part = next(
            (p for d in self._disks for p in d.partitions if p.key == rt.key),
            None,
        )
        log.warning("检测到卷拔出 %s，自动清理残留挂载", rt.device)
        if part is not None:
            await self._partial_cleanup(part.mount_dir, rt)
        else:
            # 分区信息也没了，至少尝试按记录的路径清理
            if rt.fs_dir:
                await umount_filesystem(Path(rt.fs_dir), lazy=True)
            if rt.mount_dir:
                await umount_filesystem(Path(rt.mount_dir), lazy=True)
            if rt.dm_name:
                await kill_fuse_daemons(
                    rt.dm_name, rt.fs_dir or "", rt.mount_dir or ""
                )
                try:
                    await run_cryptsetup_close(rt.dm_name)
                except MountError:
                    pass
                rt.dm_name = None
        self._volumes.pop(rt.key, None)
        await self._notify("removed")

    async def unmount_all(self) -> None:
        """容器停止：卸载本容器建立的全部挂载。"""
        log.info("容器停止前，卸载全部 %d 个已挂载卷", len(self._volumes))
        for key, rt in list(self._volumes.items()):
            part = next(
                (p for d in self._disks for p in d.partitions if p.key == key), None
            )
            if part is None or rt.state != VolumeState.MOUNTED:
                continue
            try:
                await self._teardown_locked(part, lazy=True)
            except MountError:
                log.exception("停止时卸载 %s 失败", rt.device)
        # 兜底：/mnt/usb 下可能还有未跟踪的残留挂载
        await self.cleanup_orphans()

    async def cleanup_orphans(self) -> None:
        """卸载 /proc/mounts 中挂在 /mnt/usb 下、但运行时未跟踪的条目；
        并关闭上次异常退出残留的 bsbr-* cryptsetup 映射设备。

        /mnt/usb/local/*（Docker 管理，umount 后容器内映射失效直到重建）
        与 /mnt/usb/remote/*（远程存储管理器负责）不在此清理范围内。
        """
        try:
            proc_mounts = await asyncio.to_thread(_read_mount_points)
        except OSError:
            return
        prefix = str(MOUNT_ROOT)

        def _protected(mp: str) -> bool:
            local = f"{prefix}/local"
            remote = f"{prefix}/remote"
            return mp == local or mp.startswith(local + "/") \
                or mp == remote or mp.startswith(remote + "/")

        targets = sorted(
            (mp for mp in proc_mounts
             if mp.startswith(prefix + "/") and not _protected(mp)),
            key=len,
            reverse=True,
        )
        for mp in targets:
            await umount_filesystem(Path(mp), lazy=True)
        mapper = Path("/dev/mapper")
        try:
            orphans = [p.name for p in mapper.glob("bsbr-*")]
        except OSError:
            orphans = []
        for name in orphans:
            try:
                await run_cryptsetup_close(name)
                log.info("关闭残留映射设备 %s", name)
            except MountError:
                pass

    @staticmethod
    async def _remove_dirs(mount_dir, ignore_errors: bool = False) -> None:
        """删除挂载目录及其可能的空父目录（盘级目录）。"""
        await asyncio.to_thread(shutil.rmtree, mount_dir, ignore_errors=ignore_errors)
        try:
            # 尝试删除空盘目录（如 /mnt/usb/<disk_id>/）
            await asyncio.to_thread(
                lambda: os.rmdir(mount_dir.parent)
                if mount_dir.parent.exists() and not any(mount_dir.parent.iterdir()) else None
            )
        except OSError:
            pass


def _read_mount_points() -> list[str]:
    points: list[str] = []
    with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            fields = line.split()
            if len(fields) >= 2:
                points.append(fields[1].replace("\\040", " "))
    return points
