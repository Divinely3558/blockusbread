"""dislocker / cryptsetup / mount / ntfs-3g 的底层封装：参数数组调用，凭据只走 stdin。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("mount.cmd")

# blkid 报的类型 -> 挂载驱动
_DRIVER_MAP = {
    "ntfs": "ntfs-3g",
    "vfat": "vfat",
    "fat": "vfat",
    "ext2": "ext4",
    "ext3": "ext4",
    "ext4": "ext4",
    "exfat": "exfat",
}


class MountError(RuntimeError):
    """挂载/卸载失败，message 可直接展示给用户。"""


class DislockerUnsupported(MountError):
    """dislocker 无法解析该 BitLocker 卷（如含 VIRTUALIZATION_INFO 的新格式），
    调用方应改用 cryptsetup。"""


async def run_cmd(
    args: list[str],
    *,
    input_data: bytes | None = None,
    timeout: float = 60,
) -> tuple[bytes, bytes]:
    """参数数组执行命令（禁止 shell 拼接）；失败抛 MountError。"""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_safe_env(),
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_data), timeout=timeout
        )
    except TimeoutError as exc:
        if proc is not None and proc.returncode is None:
            proc.kill()
        raise MountError(f"命令超时（{timeout:.0f}s）：{args[0]}") from exc
    except FileNotFoundError as exc:
        raise MountError(f"系统缺少命令 {args[0]}，镜像可能构建不完整") from exc

    if proc.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()[-400:]
        log.debug("命令失败 %s rc=%s stderr=%s", args[0], proc.returncode, detail)
        raise MountError(_friendly_error(args[0], detail))
    return stdout, stderr


async def run_cmd_capture(
    args: list[str],
    *,
    input_data: bytes | None = None,
    timeout: float = 60,
) -> tuple[int, str, str]:
    """不抛错的执行：返回 (returncode, stdout_text, stderr_text)，供需自行判错的调用方。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_safe_env(),
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_data), timeout=timeout
        )
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )
    except FileNotFoundError as exc:
        raise MountError(f"系统缺少命令 {args[0]}，镜像可能构建不完整") from exc
    except TimeoutError as exc:
        raise MountError(f"命令超时（{timeout:.0f}s）：{args[0]}") from exc


def _safe_env() -> dict[str, str]:
    # 最小环境，避免凭据经由环境变量泄漏到子进程环境
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        # 容器内没有 udevd（/run/udev 仅借宿主机数据库），
        # 不禁止的话 libdevmapper 会等待 udev cookie 同步而永久挂死
        "DM_DISABLE_UDEV": "1",
    }


def _friendly_error(cmd: str, detail: str) -> str:
    low = detail.lower()
    if "target is busy" in low or "device or resource busy" in low:
        return "设备正忙：仍有文件被占用，请先关闭 SMB 客户端中打开的文件后重试"
    if cmd == "dislocker":
        if any(w in low for w in ("password", "recovery", "decrypt", "verification")):
            return "BitLocker 凭据错误或卷数据损坏，解锁失败"
        return f"BitLocker 解锁失败：{detail or '未知错误'}"
    return f"{cmd} 执行失败：{detail or '未知错误'}"


async def probe_fstype(target: Path) -> str:
    """探测 dislocker-file / 分区的真实文件系统类型。"""
    stdout, _ = await run_cmd(
        ["blkid", "-p", "-s", "TYPE", "-o", "value", str(target)], timeout=20
    )
    return stdout.decode("ascii", "ignore").strip().lower()


async def run_dislocker(
    device: str,
    secret: str,
    kind: str,
    readonly: bool,
    mount_dir: Path,
) -> None:
    """运行 dislocker：密码/恢复密钥通过 stdin 传入，不出现在 argv 与 ps 输出中。

    dislocker 无法解析含 VIRTUALIZATION_INFO 的卷（Win10/11 新版加密）时，
    抛 DislockerUnsupported 让调用方 fallback 到 cryptsetup。
    """
    mount_dir.mkdir(parents=True, exist_ok=True)
    args = ["dislocker", "-V", device]
    if readonly:
        args.append("-r")
    # 裸 -u / -p：dislocker 从 stdin 交互式读取凭据
    args.append("-p" if kind == "recovery" else "-u")
    args += ["--", str(mount_dir)]
    log.debug("执行 dislocker（device=%s ro=%s kind=%s，凭据已脱敏）", device, readonly, kind)
    # dislocker 的诊断信息默认打到 stdout 而非 stderr，两边都要看
    rc, out, err = await run_cmd_capture(
        args, input_data=(secret + "\n").encode("utf-8"), timeout=90
    )
    combined = (out + err).strip()
    if rc != 0:
        low = combined.lower()
        if "virtualization" in low or "unable to compute regions" in low:
            raise DislockerUnsupported(
                "dislocker 无法解析该卷的元数据（可能为新版 BitLocker 格式）"
            )
        log.debug("dislocker 失败 rc=%s 输出=%s", rc, combined[-400:])
        raise MountError(_friendly_error("dislocker", combined))
    if not (mount_dir / "dislocker-file").exists():
        raise MountError("dislocker 未生成 dislocker-file，解锁失败")


def dm_name_for(key: str) -> str:
    """cryptsetup 映射设备名：bsbr- 前缀 + 卷 key（清洗为 dm 合法字符）。"""
    return "bsbr-" + re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:100]


async def run_cryptsetup_open(
    device: str,
    secret: str,
    readonly: bool,
    dm_name: str,
) -> Path:
    """cryptsetup 解锁 BitLocker 卷（内核 dm-crypt，兼容新旧格式）。

    bitlk 类型会遍历卷上所有 key protector，用户密码与 48 位恢复密钥
    均直接作为 passphrase 传入即可自动识别。
    """
    args = ["cryptsetup", "open", "--type", "bitlk"]
    if readonly:
        args.append("--readonly")
    args += [device, dm_name]
    log.debug("执行 cryptsetup open（device=%s ro=%s，凭据已脱敏）", device, readonly)
    rc, out, err = await run_cmd_capture(
        args, input_data=(secret + "\n").encode("utf-8"), timeout=90
    )
    if rc != 0:
        combined = (out + err).strip()
        low = combined.lower()
        log.debug("cryptsetup 失败 rc=%s 输出=%s", rc, combined[-400:])
        if "no key available" in low or "no usable" in low:
            raise MountError("BitLocker 凭据错误或卷数据损坏，解锁失败")
        raise MountError(f"BitLocker 解锁失败：{combined[-200:] or '未知错误'}")
    mapper = Path("/dev/mapper") / dm_name
    if not mapper.exists():
        raise MountError("cryptsetup 未生成映射设备，解锁失败")
    return mapper


async def run_cryptsetup_close(dm_name: str) -> None:
    """关闭 cryptsetup 映射设备；设备已被拔出等场景允许失败。"""
    await run_cmd(["cryptsetup", "close", dm_name], timeout=45)


async def mount_filesystem(
    target: Path, fstype: str, readonly: bool, mount_point: Path
) -> None:
    """挂载文件系统；target 为 dislocker-file 时走 loop。"""
    fstype = fstype.lower()
    driver = _DRIVER_MAP.get(fstype)
    if driver is None:
        raise MountError(f"不支持的文件系统类型：{fstype}")

    mount_point.mkdir(parents=True, exist_ok=True)
    opts = ["ro"] if readonly else []
    if target.is_file():
        opts.append("loop")
    # 中文等非 ASCII 文件名：vfat 默认按 iso8859-1 转码会变成 ??，
    # 必须显式指定 UTF-8；ntfs-3g 依赖进程 locale，显式指定更稳
    if driver == "vfat":
        opts.append("iocharset=utf8")
    elif driver == "ntfs-3g":
        opts.append("locale=C.UTF-8")
    opt_arg = ",".join(opts)

    args = ["mount", "-t", driver]
    if opt_arg:
        args += ["-o", opt_arg]
    args += [str(target), str(mount_point)]
    log.debug("挂载：%s (%s) -> %s", target, fstype, mount_point)
    await run_cmd(args, timeout=60)

    if not os.path.ismount(mount_point):
        raise MountError("挂载命令返回成功但挂载点未生效")


async def umount_filesystem(target: Path, lazy: bool = False) -> None:
    args = ["umount"]
    if lazy:
        args.append("-l")
    args.append(str(target))
    try:
        await run_cmd(args, timeout=45)
    except MountError:
        if not lazy:
            raise
