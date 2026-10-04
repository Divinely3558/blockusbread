"""运行期配置：全部来自环境变量，集中校验。"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

MOUNT_ROOT = Path(os.getenv("MOUNT_ROOT", "/mnt/usb"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
LOG_DIR = DATA_DIR / "logs"
SECRETS_DATABASE = DATA_DIR / "secrets.db"
# SSH 主机密钥持久化目录：容器重建后指纹不变，避免客户端反复收到密钥变更警告
SSH_KEY_DIR = DATA_DIR / "ssh"

# SFTP 服务配置（容器内 sshd 固定监听 22；SFTP_HOST_PORT 为宿主机映射端口，
# 仅用于管理页向客户端展示连接地址）
# 使用自包含的完整配置覆盖发行版默认 sshd_config，避免默认 Subsystem 等
# 指令与本项目所需配置冲突（如重复声明 Subsystem sftp）
SFTP_SSHD_CONFIG = Path("/etc/ssh/sshd_config")
SFTP_PORT = int(os.getenv("SFTP_PORT", "22"))
SFTP_HOST_PORT = int(os.getenv("SFTP_HOST_PORT", "2222"))


class ConfigError(RuntimeError):
    """启动配置错误。"""


@dataclass(frozen=True)
class Settings:
    admin_user: str
    admin_password: str
    secret_key: str
    log_level: str
    session_signing_key: bytes = field(repr=False)
    remember_enabled: bool = False

    @property
    def fernet_key(self) -> bytes:
        """由 SECRET_KEY 派生 Fernet 密钥（32 字节 -> urlsafe base64）。"""
        digest = hashlib.sha256(self.secret_key.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)


def load_settings() -> Settings:
    admin_user = os.getenv("ADMIN_USER", "admin").strip() or "admin"
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    secret_key = os.getenv("SECRET_KEY", "").strip()
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    # 本地直接运行（非容器）时允许缺省密码，方便开发；容器内 /app/.dockerenv 存在则强制
    in_docker = Path("/.dockerenv").exists()
    if not admin_password and in_docker:
        raise ConfigError(
            "必须设置环境变量 ADMIN_PASSWORD（首次启动的管理页/SFTP 密码）"
        )
    if not admin_password:
        admin_password = "dev-admin-password"

    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        log_level = "INFO"

    # 会话签名密钥：优先 SECRET_KEY；未设置时进程随机（重启后全部会话失效）
    signing_secret = secret_key or secrets.token_hex(32)
    signing_key = hashlib.sha256(signing_secret.encode("utf-8")).digest()

    return Settings(
        admin_user=admin_user,
        admin_password=admin_password,
        secret_key=secret_key,
        log_level=log_level,
        session_signing_key=signing_key,
        remember_enabled=bool(secret_key),
    )
