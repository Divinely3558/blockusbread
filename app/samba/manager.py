"""Samba（smbd）托管：配置渲染、系统用户与 SMB 密码同步、进程守护、登录校验。

管理网页登录与 SMB 共享共用同一套账号密码：
- 账号密码以容器环境变量 ADMIN_USER / ADMIN_PASSWORD 为唯一来源，
  每次启动都同步到 Samba（改密码改 .env 后重建容器即可）
- 登录校验通过本地 smbclient 向 IPC$ 发起认证完成
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from app import config

log = logging.getLogger("samba")

_SMB_CONF_TEMPLATE = """\
[global]
   workgroup = WORKGROUP
   server string = BlockUSBRead
   server role = standalone server
   security = user
   passdb backend = tdbsam
   map to guest = never
   disable netbios = yes
   smb ports = {smb_port}
   server min protocol = SMB2
   load printers = no
   printing = bsd
   printcap name = /dev/null
   unix charset = UTF-8
   log file = /var/log/samba/log.%m
   max log size = 1000
   logging = file
   # 单用户电器场景：共享访问统一映射为 root，规避挂载目录属主导致的权限问题
   force user = root
   force group = root

[{share}]
   comment = BlockUSBRead mounted drives
   path = {root}
   browseable = yes
   read only = no
   guest ok = no
   valid users = {user}
   create mask = 0664
   directory mask = 0775
"""


class SambaError(RuntimeError):
    """Samba 初始化或操作失败。"""


class InvalidCredentials(RuntimeError):
    """SMB 账号或密码错误。"""


class SambaManager:
    def __init__(self, settings) -> None:
        self._settings = settings
        self._proc: asyncio.subprocess.Process | None = None
        self._watch_task: asyncio.Task | None = None
        self._stopping = False

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def share_name(self) -> str:
        return config.SMB_SHARE_NAME

    # ------------------------------------------------------------------ 初始化

    async def start(self) -> None:
        await asyncio.to_thread(self._render_conf)
        await asyncio.to_thread(self._ensure_state_dirs)
        await self._ensure_user()
        await self._sync_password()
        self._stopping = False
        self._watch_task = asyncio.create_task(self._watch(), name="smbd-watch")
        # 等守护循环完成首次拉起
        for _ in range(20):
            if self.alive:
                break
            await asyncio.sleep(0.2)
        else:
            raise SambaError("smbd 启动失败，请检查容器日志")
        log.info("Samba 已启动：共享 \\\\<主机>\\%s（账号 %s）",
                 config.SMB_SHARE_NAME, self._settings.admin_user)

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
        log.info("Samba 已停止")

    # ------------------------------------------------------------------ 内部步骤

    def _render_conf(self) -> None:
        config.SMB_CONF.parent.mkdir(parents=True, exist_ok=True)
        config.SMB_CONF.write_text(
            _SMB_CONF_TEMPLATE.format(
                smb_port=config.SMB_PORT,
                share=config.SMB_SHARE_NAME,
                root=str(config.MOUNT_ROOT),
                user=self._settings.admin_user,
            ),
            encoding="utf-8",
        )

    async def _ensure_user(self) -> None:
        """创建同名系统用户（smbpasswd 需要映射到 UID）；已存在则跳过。"""
        username = self._settings.admin_user
        proc = await asyncio.create_subprocess_exec(
            "id", username,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if proc.returncode == 0:
            return
        shell = "/usr/sbin/nologin" if Path("/usr/sbin/nologin").exists() else "/bin/false"
        proc = await asyncio.create_subprocess_exec(
            "useradd", "--no-create-home", "--user-group",
            "--shell", shell, username,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise SambaError(
                f"创建系统用户 {username} 失败：{stderr.decode('utf-8', 'replace').strip()[:200]}"
            )

    async def _set_password(self, password: str) -> None:
        """以 root 身份（重）置 SMB 密码，无需旧密码；stdin 传入，不上 argv。"""
        proc = await asyncio.create_subprocess_exec(
            "smbpasswd", "-a", "-s", self._settings.admin_user,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = (password + "\n" + password + "\n").encode("utf-8")
        _, stderr = await proc.communicate(input=payload)
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[-200:]
            raise SambaError(f"设置 SMB 密码失败：{detail or '未知错误'}")

    async def _sync_password(self) -> None:
        """账号密码以容器环境变量为唯一来源：每次启动都把 SMB 密码同步为
        ADMIN_PASSWORD（smbpasswd -a 对已存在用户等同于改密）。
        需要改密码时，修改 .env 后重新创建容器即可。"""
        await self._set_password(self._settings.admin_password)
        log.info("已按环境变量同步账号 %s 的密码（管理页与 SMB 共用）",
                 self._settings.admin_user)

    @staticmethod
    def _ensure_state_dirs() -> None:
        private = Path("/var/lib/samba/private")
        private.mkdir(parents=True, exist_ok=True)
        os.chmod(private, 0o700)
        Path("/var/lib/samba/lock").mkdir(parents=True, exist_ok=True)

    async def _spawn_smbd(self) -> asyncio.subprocess.Process:
        if not shutil.which("smbd"):
            raise SambaError("镜像内缺少 smbd，构建可能不完整")
        return await asyncio.create_subprocess_exec(
            "smbd", "--foreground", "--no-process-group", "--debug-stdout",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ},
        )

    async def _watch(self) -> None:
        """启动并守护 smbd：崩溃后 3 秒自动拉起，日志泵到容器标准输出。"""
        while not self._stopping:
            self._proc = await self._spawn_smbd()
            log.info("smbd 子进程已启动（pid=%s）", self._proc.pid)
            pump = asyncio.create_task(self._pump_logs(self._proc))
            rc = await self._proc.wait()
            pump.cancel()
            if self._stopping:
                break
            log.warning("smbd 意外退出（rc=%s），3 秒后重启", rc)
            await asyncio.sleep(3)

    async def _pump_logs(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                log.debug("smbd | %s", text)

    # ------------------------------------------------------------------ 对外操作

    async def verify_password(self, username: str, password: str) -> None:
        """用 smbclient 向本地 IPC$ 发起认证；失败抛 InvalidCredentials/SambaError。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "smbclient", "//127.0.0.1/IPC$",
                "-U", username,
                f"--password={password}",
                "-m", "SMB3",
                "-c", "exit",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        except FileNotFoundError as exc:
            raise SambaError("镜像内缺少 smbclient，构建可能不完整") from exc
        except TimeoutError as exc:
            if proc.returncode is None:
                proc.kill()
            raise SambaError("登录认证超时") from exc
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace")
            if "LOGON_FAILURE" in detail or "NT_STATUS_ACCESS_DENIED" in detail:
                raise InvalidCredentials("账号或密码错误")
            raise InvalidCredentials("账号或密码错误")
