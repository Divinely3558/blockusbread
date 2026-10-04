"""SFTP（OpenSSH sshd + internal-sftp）托管：配置渲染、系统用户与密码同步、
进程守护、登录校验。

管理网页登录与 SFTP 共用同一套账号密码：
- 账号密码以容器环境变量 ADMIN_USER / ADMIN_PASSWORD 为唯一来源，
  每次启动都同步到系统账号（改密码改 .env 后重建容器即可）
- 单用户电器场景：ADMIN_USER 创建为 UID 0 的 root 别名（等价旧 Samba
  配置里的 force user = root），只读 / 读写模式均可正常工作
- ChrootDirectory 锁定到挂载根 /mnt/usb：客户端看到的 "/" 即挂载目录，
  无法浏览容器其他路径；热插拔挂载在同一 mount namespace 内立即可见
- 登录校验直接与环境变量比对（与共享服务进程解耦，sshd 异常不影响登录）
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import shutil
from pathlib import Path

from app import config

log = logging.getLogger("sftp")

_SSHD_CONF_TEMPLATE = """\
# 由 BlockUSBRead 自动生成的自包含 sshd 配置，请勿手动修改
# （覆盖发行版默认 /etc/ssh/sshd_config，不 Include sshd_config.d）

Port {port}
ListenAddress 0.0.0.0

HostKey /etc/ssh/ssh_host_rsa_key
HostKey /etc/ssh/ssh_host_ecdsa_key
HostKey /etc/ssh/ssh_host_ed25519_key

# ADMIN_USER 是 UID 0 的 root 别名，OpenSSH 对 UID 0 账号按 root 策略放行
PermitRootLogin yes
PasswordAuthentication yes
KbdInteractiveAuthentication no
PubkeyAuthentication no
PermitEmptyPasswords no
UsePAM yes
PrintMotd no
AcceptEnv LANG LC_*

# 仅允许 SFTP：关闭端口转发 / 隧道 / X11，shell 为 nologin 禁止交互式登录
X11Forwarding no
AllowTcpForwarding no
AllowAgentForwarding no
PermitTunnel no
AllowUsers {user}
Subsystem sftp internal-sftp

# 锁定在挂载根目录：SFTP 客户端看到的 "/" 实际是容器内 /mnt/usb，
# 无法浏览容器底层；Match 块必须位于配置末尾
Match User {user}
    ChrootDirectory {root}
    ForceCommand internal-sftp
"""

_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*\$?$", re.IGNORECASE)


class SftpError(RuntimeError):
    """SFTP 服务初始化或操作失败。"""


class InvalidCredentials(RuntimeError):
    """账号或密码错误。"""


class SftpManager:
    def __init__(self, settings) -> None:
        self._settings = settings
        self._proc: asyncio.subprocess.Process | None = None
        self._watch_task: asyncio.Task | None = None
        self._stopping = False

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # ------------------------------------------------------------------ 初始化

    async def start(self) -> None:
        self._validate_username()
        await asyncio.to_thread(self._render_conf)
        await asyncio.to_thread(self._ensure_state_dirs)
        await self._ensure_host_keys()
        await self._ensure_user()
        await self._sync_password()
        await self._validate_sshd_config()
        self._stopping = False
        self._watch_task = asyncio.create_task(self._watch(), name="sshd-watch")
        # 等守护循环完成首次拉起
        for _ in range(20):
            if self.alive:
                break
            await asyncio.sleep(0.2)
        else:
            raise SftpError("sshd 启动失败，请检查容器日志")
        log.info("SFTP 已启动：sftp://<主机>:%s（账号 %s，根目录为挂载目录 %s）",
                 config.SFTP_PORT, self._settings.admin_user, config.MOUNT_ROOT)

    async def stop(self) -> None:
        self._stopping = True
        if self._watch_task:
            self._watch_task.cancel()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=8)
            except asyncio.TimeoutError:
                self._proc.kill()
        log.info("SFTP 已停止")

    # ------------------------------------------------------------------ 内部步骤

    def _validate_username(self) -> None:
        if not _USERNAME_RE.fullmatch(self._settings.admin_user):
            raise SftpError(
                f"ADMIN_USER={self._settings.admin_user!r} 含非法字符，"
                "仅支持字母、数字、下划线、连字符，且以字母或下划线开头"
            )

    def _render_conf(self) -> None:
        config.SFTP_SSHD_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        config.SFTP_SSHD_CONFIG.write_text(
            _SSHD_CONF_TEMPLATE.format(
                port=config.SFTP_PORT,
                user=self._settings.admin_user,
                root=str(config.MOUNT_ROOT),
            ),
            encoding="utf-8",
        )
        os.chmod(config.SFTP_SSHD_CONFIG, 0o644)

    @staticmethod
    def _ensure_state_dirs() -> None:
        # sshd 特权分离运行目录
        Path("/run/sshd").mkdir(parents=True, exist_ok=True)
        # 挂载根（同时作为 ChrootDirectory）：
        # sshd 强制要求 chroot 目录及其各级父目录 root 属主且不可组/其他写
        config.MOUNT_ROOT.mkdir(parents=True, exist_ok=True)
        os.chown(config.MOUNT_ROOT, 0, 0)
        os.chmod(config.MOUNT_ROOT, 0o755)

    async def _ensure_host_keys(self) -> None:
        """生成缺失的主机密钥（ssh-keygen -A 幂等）。"""
        proc = await asyncio.create_subprocess_exec(
            "ssh-keygen", "-A",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            detail = out.decode("utf-8", "replace").strip()[-200:]
            raise SftpError(f"生成 SSH 主机密钥失败：{detail or '未知错误'}")

    async def _ensure_user(self) -> None:
        """创建 UID 0 的 root 别名系统用户；已存在则校正家目录与 shell。

        必须为 UID 0：保证各文件系统（NTFS/exFAT/ext4）在只读与读写模式下
        权限一致。
        shell 固定 nologin：只允许 SFTP 子系统，禁止交互式 shell 登录。
        home 固定为 "/"：配合 ChrootDirectory=/mnt/usb，chroot 后家目录
        恰好是挂载根，登录即落在卷列表目录。
        """
        username = self._settings.admin_user
        shell = "/usr/sbin/nologin" if Path("/usr/sbin/nologin").exists() else "/bin/false"
        home = "/"

        proc = await asyncio.create_subprocess_exec(
            "id", username,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if proc.returncode == 0:
            proc = await asyncio.create_subprocess_exec(
                "usermod", "--home", home, "--shell", shell, username,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise SftpError(
                    f"更新系统用户 {username} 失败："
                    f"{stderr.decode('utf-8', 'replace').strip()[:200]}"
                )
            return

        proc = await asyncio.create_subprocess_exec(
            "useradd", "--non-unique", "--uid", "0", "--gid", "0",
            "--no-user-group", "--no-create-home",
            "--home-dir", home, "--shell", shell, username,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise SftpError(
                f"创建系统用户 {username} 失败："
                f"{stderr.decode('utf-8', 'replace').strip()[:200]}"
            )

    async def _sync_password(self) -> None:
        """账号密码以容器环境变量为唯一来源：每次启动都把系统账号密码
        同步为 ADMIN_PASSWORD。需要改密码时，修改 .env 后重新创建容器即可。
        通过 stdin 传入，不上 argv。"""
        proc = await asyncio.create_subprocess_exec(
            "chpasswd",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = (
            f"{self._settings.admin_user}:{self._settings.admin_password}\n"
        ).encode("utf-8")
        _, stderr = await proc.communicate(input=payload)
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[-200:]
            raise SftpError(f"设置 SFTP 密码失败：{detail or '未知错误'}")
        log.info("已按环境变量同步账号 %s 的密码（管理页与 SFTP 共用）",
                 self._settings.admin_user)

    async def _validate_sshd_config(self) -> None:
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        proc = await asyncio.create_subprocess_exec(
            sshd, "-t", "-f", str(config.SFTP_SSHD_CONFIG),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise SftpError(
                "sshd 配置校验失败："
                + stderr.decode("utf-8", "replace").strip()[-200:]
            )

    async def _spawn_sshd(self) -> asyncio.subprocess.Process:
        sshd = shutil.which("sshd")
        if not sshd:
            raise SftpError("镜像内缺少 sshd，构建可能不完整")
        return await asyncio.create_subprocess_exec(
            sshd, "-D", "-e", "-f", str(config.SFTP_SSHD_CONFIG),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ},
        )

    async def _watch(self) -> None:
        """启动并守护 sshd：崩溃后 3 秒自动拉起，日志泵到容器标准输出。"""
        while not self._stopping:
            self._proc = await self._spawn_sshd()
            log.info("sshd 子进程已启动（pid=%s）", self._proc.pid)
            pump = asyncio.create_task(self._pump_logs(self._proc))
            rc = await self._proc.wait()
            pump.cancel()
            if self._stopping:
                break
            log.warning("sshd 意外退出（rc=%s），3 秒后重启", rc)
            await asyncio.sleep(3)

    async def _pump_logs(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            # 登录成功事件提升到 INFO：日志需明确记录谁在访问
            if "Accepted password" in text:
                log.info("sshd | %s", text)
            else:
                log.debug("sshd | %s", text)

    # ------------------------------------------------------------------ 对外操作

    async def verify_password(self, username: str, password: str) -> None:
        """与环境变量（账号密码唯一来源）直接比对；失败抛 InvalidCredentials。"""
        ok = (
            username == self._settings.admin_user
            and hmac.compare_digest(password, self._settings.admin_password)
        )
        if not ok:
            raise InvalidCredentials("账号或密码错误")
