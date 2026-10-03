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
# 未显式设置 SECRET_KEY 时，自动生成并持久化到该文件（权限 600）
SECRET_KEY_FILE = DATA_DIR / ".secret_key"

# Samba 共享配置
SMB_CONF = Path("/etc/samba/smb.conf")
SMB_SHARE_NAME = os.getenv("SMB_SHARE_NAME", "usb")
SMB_PORT = int(os.getenv("SMB_PORT", "445"))

# BitLocker 普通分区允许的文件系统
SUPPORTED_FSTYPES = {"ntfs", "exfat", "vfat", "fat", "ext2", "ext3", "ext4"}


class ConfigError(RuntimeError):
    """启动配置错误。"""


@dataclass(frozen=True)
class Settings:
    admin_user: str
    admin_password: str
    secret_key: str
    log_level: str
    session_signing_key: bytes = field(repr=False)
    remember_enabled: bool = True
    # 密钥来源：env（环境变量）/ file（已生成的持久化文件）/ generated（本次新生成）
    key_source: str = "env"

    @property
    def fernet_key(self) -> bytes:
        """由 SECRET_KEY 派生 Fernet 密钥（32 字节 -> urlsafe base64）。"""
        digest = hashlib.sha256(self.secret_key.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)


def _resolve_secret_key() -> tuple[str, str]:
    """解析凭据加密密钥：环境变量 SECRET_KEY 优先；
    未设置则读取 data/.secret_key，文件也不存在时自动生成持久化随机密钥（零配置）。"""
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key:
        return env_key, "env"
    try:
        saved = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        saved = ""
    if saved:
        return saved, "file"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(48)
    SECRET_KEY_FILE.write_text(generated, encoding="utf-8")
    SECRET_KEY_FILE.chmod(0o600)
    return generated, "generated"


def load_settings() -> Settings:
    admin_user = os.getenv("ADMIN_USER", "admin").strip() or "admin"
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    secret_key, key_source = _resolve_secret_key()
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    # 本地直接运行（非容器）时允许缺省密码，方便开发；容器内 /app/.dockerenv 存在则强制
    in_docker = Path("/.dockerenv").exists()
    if not admin_password and in_docker:
        raise ConfigError(
            "必须设置环境变量 ADMIN_PASSWORD（首次启动的管理页/SMB 共享密码）"
        )
    if not admin_password:
        admin_password = "dev-admin-password"

    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        log_level = "INFO"

    # 会话签名密钥复用持久化的 secret_key，容器重启后登录态不再全部失效
    signing_key = hashlib.sha256(secret_key.encode("utf-8")).digest()

    return Settings(
        admin_user=admin_user,
        admin_password=admin_password,
        secret_key=secret_key,
        log_level=log_level,
        session_signing_key=signing_key,
        remember_enabled=True,
        key_source=key_source,
    )
