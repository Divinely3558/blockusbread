"""dislocker / cryptsetup / mount / ntfs-3g 的底层封装：参数数组调用，凭据只走 stdin。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger("mount.cmd")


class MountInfo(NamedTuple):
    """/proc/self/mountinfo 单行：root 是挂载源在文件系统内的路径。

    Docker bind 的本地存储 root 为宿主机内子路径（非 "/"），
    外接卷与远程 FUSE 均为整文件系统挂载（root 为 "/"）——这是
    区分两者的可靠依据（/proc/mounts 里 bind 的源显示的就是块设备名）。
    """

    mount_point: str
    root: str
    source: str
    fstype: str


_MOUNT_ESCAPES = (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\"))


def _unescape_mount_field(value: str) -> str:
    for pat, ch in _MOUNT_ESCAPES:
        value = value.replace(pat, ch)
    return value


def read_mountinfo() -> list[MountInfo]:
    """解析 /proc/self/mountinfo，返回 [(挂载点, root, 源, 文件系统类型), ...]。"""
    rows: list[MountInfo] = []
    with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            fields = line.split()
            try:
                sep = fields.index("-")
            except ValueError:
                continue
            if sep < 5 or len(fields) < sep + 3:
                continue
            rows.append(MountInfo(
                mount_point=_unescape_mount_field(fields[4]),
                root=_unescape_mount_field(fields[3]),
                fstype=fields[sep + 1],
                source=_unescape_mount_field(fields[sep + 2]),
            ))
    return rows


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


class BitlockerCredentialError(MountError):
    """密码/恢复密钥已被引擎明确拒绝，调用方不应再尝试其他解锁引擎。"""


class DeviceInUseError(MountError):
    """分区已被其他内核映射/挂载占用（常见于容器重启后的 dm 残留），
    清理失败时不应再尝试 dislocker。"""


async def run_cmd(
    args: list[str],
    *,
    input_data: bytes | None = None,
    timeout: float = 60,
) -> tuple[bytes, bytes]:
    """参数数组执行命令（禁止 shell 拼接）；失败抛 MountError。

    子进程在独立进程组中运行：mount/ntfs-3g/dislocker 会派生守护进程，
    超时时必须整组杀掉，否则 helper 残留会继续占住设备。
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_data is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_safe_env(),
            start_new_session=True,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_data), timeout=timeout
        )
    except TimeoutError as exc:
        await _kill_process_group(proc)
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
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_data is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_safe_env(),
            start_new_session=True,
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
        await _kill_process_group(proc)
        raise MountError(f"命令超时（{timeout:.0f}s）：{args[0]}") from exc


async def _kill_process_group(proc: asyncio.subprocess.Process | None) -> None:
    """杀掉整个子进程组并收尸，避免 mount helper / FUSE 守护进程残留。"""
    if proc is None or proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        proc.kill()
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


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
        return "设备正忙：仍有文件被占用，请先关闭 SFTP 客户端中打开的文件后重试"
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


async def probe_label(target: Path) -> str:
    """探测目标（解密映射设备 / dislocker 虚拟文件）上的文件系统卷标。

    BitLocker 把卷标连同元数据一起加密，原分区上永远读不到；解锁后的
    明文目标上才有真实卷标。读不到（探测失败/无卷标）返回空串。
    """
    try:
        stdout, _ = await run_cmd(
            ["blkid", "-p", "-s", "LABEL", "-o", "value", str(target)], timeout=20
        )
    except MountError:
        return ""
    return stdout.decode("utf-8", "replace").strip()


async def warm_up_device(device: str) -> None:
    """直读设备头部，强制 USB 硬盘盒唤醒休眠中的盘体。

    部分 USB 硬盘盒在盘体休眠后，首个元数据 I/O 会在内核里阻塞数十秒，
    此时挂载 FUSE 文件系统（ntfs-3g/dislocker）会直接卡死到超时。
    用 O_DIRECT 小读把盘叫醒，后续解密/挂载即可秒级完成；
    纯只读操作，对 BitLocker 加密卷安全。
    """
    try:
        await run_cmd(
            [
                "dd", f"if={device}", "of=/dev/null",
                "bs=1M", "count=8", "iflag=direct",
            ],
            timeout=120,
        )
        log.debug("设备预热完成：%s", device)
    except MountError as exc:
        log.warning("设备预热读取失败（忽略，继续尝试解锁）：%s", exc)


async def run_dislocker(
    device: str,
    secret: str,
    kind: str,
    readonly: bool,
    mount_dir: Path,
) -> None:
    """运行 dislocker：密码/恢复密钥通过 stdin 传入，不出现在 argv 与 ps 输出中。

    dislocker 是 cryptsetup 失败后的兜底引擎；它无法解析含
    VIRTUALIZATION_INFO 的卷（Win10/11 新版加密）时，以 MountError 返回，
    此时已没有其他引擎可尝试。
    """
    mount_dir.mkdir(parents=True, exist_ok=True)
    args = ["dislocker", "-s", "-V", device]
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
            raise MountError(
                "dislocker 无法解析该卷的元数据（可能为新版 BitLocker 格式）"
            )
        log.debug("dislocker 失败 rc=%s 输出=%s", rc, combined[-400:])
        msg = _friendly_error("dislocker", combined)
        if any(w in low for w in ("password", "recovery", "decrypt", "verification")):
            raise BitlockerCredentialError(msg)
        raise MountError(msg)
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
            raise BitlockerCredentialError("BitLocker 密码或恢复密钥错误，解锁失败")
        if (
            "in use" in low
            or "already mapped or mounted" in low
            or "already exists" in low
        ):
            # 容器异常退出/重启后，同名 dm 映射可能残留在内核里（内核设备
            # 不随容器销毁），先尝试关掉同名残留再重试一次
            log.info("设备 %s 被映射占用，尝试清理同名残留 %s", device, dm_name)
            with contextlib.suppress(MountError):
                await run_cryptsetup_close(dm_name)
            rc2, out2, err2 = await run_cmd_capture(
                args, input_data=(secret + "\n").encode("utf-8"), timeout=90
            )
            if rc2 == 0 and (Path("/dev/mapper") / dm_name).exists():
                return Path("/dev/mapper") / dm_name
            raise DeviceInUseError(
                "该分区正被其他内核映射或挂载占用（可能是容器异常退出后的残留），"
                "请在宿主机清理对应映射后重试"
            )
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
    """挂载文件系统；target 为 dislocker-file 时走 loop。

    FUSE 类驱动（ntfs-3g/dislocker）的 mount helper 间歇不会 daemonize：
    挂载生效后 helper 进程本身作为 FUSE 守护进程常驻、永远不退出。
    因此不能等进程结束，而是轮询挂载点是否生效；生效即视为成功，
    常驻的守护进程在独立会话中脱离、由 PID 1（tini）托管，umount 时自行退出。
    """
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

    # std 全部 DEVNULL：常驻 FUSE 守护进程不得持有应用管道
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=_safe_env(),
        start_new_session=True,
    )

    # 轮询挂载点：内核挂载毫秒级生效；FUSE 通常 1~3 秒；
    # 冷盘唤醒可能较久，给 90 秒
    deadline = 90.0
    step = 0.25
    elapsed = 0.0
    while True:
        if await asyncio.to_thread(os.path.ismount, mount_point):
            # 挂载已生效。helper 若仍在运行说明它是前台 FUSE 守护进程，
            # 保持运行，随独立会话脱离由 PID 1 托管。
            log.debug("挂载点已生效：%s（helper 状态=%s）",
                      mount_point, "运行中" if proc.returncode is None else "已退出")
            return
        if proc.returncode is not None:
            raise MountError(_friendly_error(driver, "挂载进程提前退出，挂载未生效"))
        if elapsed >= deadline:
            break
        await asyncio.sleep(step)
        elapsed += step

    await _kill_process_group(proc)
    raise MountError(f"挂载超时（{deadline:.0f}s），挂载点未生效：{mount_point}")


async def _umount_once(target: Path, lazy: bool, timeout: float) -> tuple[bool, str]:
    """单次 umount，返回 (是否成功, 小写错误输出)。已卸载/路径不存在视为成功。"""
    args = ["umount"]
    if lazy:
        args.append("-l")
    args.append(str(target))
    try:
        rc, _out, err = await run_cmd_capture(args, timeout=timeout)
    except MountError:
        # 仅可能是超时：lazy 卸载不该发生，交由上层按失败处理
        return False, "timeout"
    detail = (err or "").strip().lower()
    if rc == 0:
        return True, ""
    # 幂等：目标本来就没挂载（目录可能已被清理）
    if "not mounted" in detail or "no mount point specified" in detail or "no such file" in detail:
        return True, detail
    return False, detail


# ---------------------------------------------------------- 进程占用 / FUSE 守护

def _iter_pids() -> list[int]:
    with contextlib.suppress(OSError):
        return [int(n) for n in os.listdir("/proc") if n.isdigit()]
    return []


def _path_within(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def _find_mount_users(mount_point: Path) -> list[int]:
    """查找 cwd/root/打开的 fd 落在挂载点内的进程（排除自身与 PID 1）。"""
    mp = str(mount_point)
    self_pid = os.getpid()
    users: list[int] = []
    for pid in _iter_pids():
        if pid in (1, self_pid):
            continue
        hit = False
        for tgt in ("cwd", "root"):
            with contextlib.suppress(OSError):
                if _path_within(os.readlink(f"/proc/{pid}/{tgt}"), mp):
                    hit = True
                    break
        if not hit:
            with contextlib.suppress(OSError):
                for fd in os.listdir(f"/proc/{pid}/fd"):
                    with contextlib.suppress(OSError):
                        if _path_within(os.readlink(f"/proc/{pid}/fd/{fd}"), mp):
                            hit = True
                            break
        if hit:
            users.append(pid)
    return users


async def evict_mount_users(mount_point: Path) -> list[int]:
    """安全弹出时结束占用挂载点的进程（如 SFTP 客户端）：先 TERM 后 KILL，全程限 3 秒。"""
    users = await asyncio.to_thread(_find_mount_users, mount_point)
    if not users:
        return []
    log.info("卸载 %s 前结束占用进程：%s", mount_point, users)
    for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
        for pid in users:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)
        await asyncio.sleep(grace)
        users = [p for p in users if os.path.exists(f"/proc/{p}")]
        if not users:
            break
    return users


def _find_fuse_daemons(*needles: str) -> list[int]:
    """按命令行特征查找服务指定挂载点/映射设备的 ntfs-3g / dislocker 守护进程。"""
    hits: list[int] = []
    for pid in _iter_pids():
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        low = cmd.lower()
        if "ntfs-3g" not in low and "dislocker" not in low:
            continue
        if needles and not any(n and n in cmd for n in needles):
            continue
        hits.append(pid)
    return hits


async def kill_fuse_daemons(*needles: str, grace: float = 3.0) -> None:
    """卸载后清理未自行退出的前台型 FUSE 守护进程（否则 dm 映射无法关闭）。

    正常 umount 后守护进程会在 1 秒内自行退出；超时仍存活才 TERM/KILL。
    """
    pids = await asyncio.to_thread(_find_fuse_daemons, *needles)
    if not pids:
        return
    waited = 0.0
    while pids and waited < grace:
        await asyncio.sleep(0.2)
        waited += 0.2
        pids = [p for p in pids if os.path.exists(f"/proc/{p}")]
    if not pids:
        return
    log.warning("FUSE 守护进程卸载后未退出，强制清理：%s（特征 %s）", pids, needles)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)
        await asyncio.sleep(0.5)
        pids = [p for p in pids if os.path.exists(f"/proc/{p}")]
        if not pids:
            break


async def umount_filesystem(
    target: Path, lazy: bool = False, evict: bool = False
) -> None:
    """卸载挂载点，全程限时、幂等。

    - 目标未挂载/目录不存在：直接返回。
    - lazy=True（停机/拔线清理）：惰性 umount 后强杀残留 FUSE 守护进程。
    - evict=True（安全弹出）：遇占用先结束占用进程（SFTP 客户端等）再重试；
      仍失败则惰性卸载兜底，保证调用返回且设备可释放。
    """
    is_mount = await asyncio.to_thread(os.path.ismount, target)
    if not is_mount:
        await kill_fuse_daemons(str(target))
        return

    if lazy:
        await _umount_once(target, lazy=True, timeout=15)
        await kill_fuse_daemons(str(target))
        return

    ok, detail = await _umount_once(target, lazy=False, timeout=15)
    if not ok and ("busy" in detail) and evict:
        await evict_mount_users(target)
        ok, detail = await _umount_once(target, lazy=False, timeout=15)
    if not ok and "busy" in detail:
        # 兜底：惰性卸载让挂载立即从命名空间消失，避免长时间卡住用户
        log.warning("%s 仍被占用，改用惰性卸载", target)
        ok, detail = await _umount_once(target, lazy=True, timeout=15)
    if not ok:
        raise MountError(_friendly_error("umount", detail))

    await kill_fuse_daemons(str(target))
