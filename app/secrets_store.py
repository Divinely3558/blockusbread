"""已记住的卷凭据：SQLite + Fernet 加密，与卷稳定标识（UUID/PARTUUID）绑定。"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("secrets")


class SecretsStore:
    def __init__(self, db_path: Path, fernet_key: bytes) -> None:
        self._fernet = Fernet(fernet_key)
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS volume_secrets (
                volume_key TEXT PRIMARY KEY,
                kind       TEXT NOT NULL,
                secret     BLOB NOT NULL,
                mode       TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self._conn.commit()

    def save(self, volume_key: str, kind: str, secret: str, mode: str) -> None:
        token = self._fernet.encrypt(secret.encode("utf-8"))
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO volume_secrets VALUES (?, ?, ?, ?, ?)",
                (volume_key, kind, token, mode, int(time.time())),
            )
            self._conn.commit()
        log.info("已保存卷 %s 的解锁凭据（加密）", volume_key)

    def load(self, volume_key: str) -> tuple[str, str, str] | None:
        """返回 (kind, secret, mode)；密钥被更换导致解不开时返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT kind, secret, mode FROM volume_secrets WHERE volume_key = ?",
                (volume_key,),
            ).fetchone()
        if row is None:
            return None
        kind, token, mode = row
        try:
            secret = self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken:
            log.warning("卷 %s 的凭据无法解密（SECRET_KEY 已更换？）", volume_key)
            return None
        return kind, secret, mode

    def delete(self, volume_key: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM volume_secrets WHERE volume_key = ?", (volume_key,)
            )
            self._conn.commit()
        log.info("已删除卷 %s 的已保存凭据", volume_key)

    def all_keys(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT volume_key FROM volume_secrets").fetchall()
        return {row[0] for row in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
