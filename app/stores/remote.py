"""远程存储：sshfs (FUSE) 挂载远程 SFTP 目录为统一卷。

- 按「远程组」组织：同组（组名留空默认取用户名）下可挂载多个远端路径；
  挂载点平铺在挂载根下，以 Windows 风格盘符命名（C~Z 用完接 Aa~Zz，
  见 app.mounts.letters），SFTP 暴露路径 sftpPath 即该盘符，删除挂载时
  盘符释放回收。
- 每个挂载以唯一 id 标识，key 形如 remote:<id>；组名仅作展示与分组。
- 凭据（密码 / SSH 私钥 PEM）经 SECRET_KEY 派生的 Fernet 加密存 SQLite；
  私钥挂载时解密写入 data/remote_keys/<id>（0600）。
- 挂载命令参数数组执行，密码只走子进程 stdin，不进 argv、不进日志。
- 主机指纹持久化到 data/ssh/known_hosts_remote（accept-new，变更即拒连）。
- 状态机：mounting → mounted | error；后台每 10s 健康探测（短超时 statvfs），
  连续 2 次失败置 error 并推 SSE；sshfs -o reconnect 恢复后自动回 mounted。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import DATA_DIR, MOUNT_ROOT
from app.events import EventBus
from app.mounts.commands import (
    MountError,
    _kill_process_group,
    _safe_env,
    run_cmd_capture,
)
from app.mounts.letters import DriveLetterRegistry

log = logging.getLogger("stores.remote")

PROBE_INTERVAL = 10.0
PROBE_TIMEOUT = 15.0
PROBE_FAILURES_TO_ERROR = 2
MOUNT_TIMEOUT = 45.0


async def _run_sshfs_mount(
    args: list[str],
    input_data: bytes | None,
    timeout: float,
) -> tuple[int, str]:
    """执行 sshfs 挂载：stdout/stderr 重定向到临时文件而非管道。

    sshfs 建立连接后会 daemonize，其 fork 的 ssh 子进程长期持有继承来的
    stdio；若走 PIPE，管道永远等不到 EOF，调用方会一直等到超时——尽管
    挂载其实已经成功。写入临时文件则主进程退出即返回，仍可读 stderr 做
    错误分类。返回 (returncode, stderr_text)。
    """
    tf = tempfile.NamedTemporaryFile(prefix="sshfs-", suffix=".log", delete=False)
    err_path = Path(tf.name)
    tf.close()
    proc: asyncio.subprocess.Process | None = None
    try:
        with err_path.open("wb") as err_f:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if input_data is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=err_f,
                env=_safe_env(),
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(
                    proc.communicate(input=input_data), timeout=timeout
                )
            except TimeoutError:
                await _kill_process_group(proc)
                raise MountError(f"命令超时（{timeout:.0f}s）：{args[0]}") from None
        stderr = err_path.read_text("utf-8", "replace")
        return proc.returncode or 0, stderr
    except FileNotFoundError as exc:
        raise MountError(f"系统缺少命令 {args[0]}，镜像可能构建不完整") from exc
    finally:
        with contextlib.suppress(OSError):
            err_path.unlink(missing_ok=True)


class RemoteStoreError(RuntimeError):
    """可展示给用户的远程存储操作错误（API 层映射 400）。"""


class RemoteStoreExists(RemoteStoreError):
    """同组同主机同路径的远程挂载已存在（API 层映射 409）。"""


class RemoteStoreNotFound(RemoteStoreError):
    """远程挂载不存在（API 层映射 404）。"""


def validate_store_name(name: str) -> str:
    """校验远程组名（合法字符与长度约束）。"""
    cleaned = (name or "").strip()
    if not cleaned:
        raise RemoteStoreError("组名不能为空")
    if len(cleaned) > 64:
        raise RemoteStoreError("组名过长（最多 64 个字符）")
    if "/" in cleaned or "\\" in cleaned:
        raise RemoteStoreError("组名不能包含 / 或 \\")
    if cleaned.startswith("."):
        raise RemoteStoreError("组名不能以 . 开头")
    if any(ord(c) < 32 for c in cleaned):
        raise RemoteStoreError("组名不能包含控制字符")
    return cleaned


@dataclass(frozen=True)
class RemoteStoreConfig:
    id: str          # 挂载唯一标识（key = remote:<id>）
    name: str        # 远程组名
    host: str
    port: int
    username: str
    auth_mode: str   # "password" | "key"
    remote_path: str
    created_at: int

    @property
    def key(self) -> str:
        return f"remote:{self.id}"


class RemoteStoreManager:
    def __init__(
        self,
        bus: EventBus,
        fernet_key: bytes | None,
        letters: DriveLetterRegistry,
        data_dir: Path = DATA_DIR,
        mount_root: Path = MOUNT_ROOT,
    ) -> None:
        self._bus = bus
        self._fernet = Fernet(fernet_key) if fernet_key else None
        self._letters = letters
        self._db_path = data_dir / "remote_stores.db"
        self._key_dir = data_dir / "remote_keys"
        self._known_hosts = data_dir / "ssh" / "known_hosts_remote"
        self._root = mount_root
        self._db_lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._ops_lock = asyncio.Lock()
        # 挂载 id -> {"state": mounting|mounted|error, "error": str|None, "failures": int}
        self._states: dict[str, dict] = {}
        self._usage: dict[str, dict[str, int]] = {}   # key -> {total, used, avail}
        self._probe_task: asyncio.Task | None = None
        self._stopping = False
        if self._fernet is not None:
            self._init_db()
        else:
            log.warning("未设置 SECRET_KEY，远程存储功能禁用")

    # ------------------------------------------------------------ 基础

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    @property
    def root(self) -> Path:
        return self._root

    def _mount_dir(self, store: RemoteStoreConfig) -> Path:
        """解析挂载点：根目录下以持久记忆的盘符命名（/mnt/usb/<盘符>）。

        盘符在首次挂载时分配并永久记忆，之后稳定不变。
        """
        letter = self._letters.allocate(store.key)
        return self._root / letter

    def fs_dir(self, key: str) -> Path:
        """按 key（remote:<id>）解析真实挂载目录（registry / 文件 API 使用）。"""
        store = self._load_one(key[len("remote:"):])
        return self._mount_dir(store)

    def ensure_dirs(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._key_dir.mkdir(parents=True, exist_ok=True)
        self._known_hosts.parent.mkdir(parents=True, exist_ok=True)

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        try:
            os.chmod(self._db_path, 0o600)
        except OSError:
            log.warning("无法设置 %s 权限为 600", self._db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remote_stores (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                host        TEXT NOT NULL,
                port        INTEGER NOT NULL DEFAULT 22,
                username    TEXT NOT NULL,
                auth_mode   TEXT NOT NULL,
                secret      BLOB NOT NULL,
                remote_path TEXT NOT NULL,
                created_at  INTEGER NOT NULL
            )
            """
        )
        self._migrate_v1()
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_remote_unique
            ON remote_stores(name, host, port, remote_path)
            """
        )
        self._conn.commit()

    def _migrate_v1(self) -> None:
        """v1：name 唯一且兼任挂载 id；v2：组名可重复，id 独立。

        老表的 name 列带 UNIQUE 约束（id == name），需重建表解除约束，
        数据原样保留（id == name 的老记录 key 不变，继续可解析）。
        """
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'remote_stores'"
        ).fetchone()
        if row is None or "UNIQUE" not in (row[0] or "").upper():
            return
        log.info("迁移远程存储表：解除组名唯一约束（v1 -> v2）")
        self._conn.executescript(
            """
            CREATE TABLE remote_stores_v2 (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                host        TEXT NOT NULL,
                port        INTEGER NOT NULL DEFAULT 22,
                username    TEXT NOT NULL,
                auth_mode   TEXT NOT NULL,
                secret      BLOB NOT NULL,
                remote_path TEXT NOT NULL,
                created_at  INTEGER NOT NULL
            );
            INSERT INTO remote_stores_v2
                (id, name, host, port, username, auth_mode, secret,
                 remote_path, created_at)
            SELECT id, name, host, port, username, auth_mode, secret,
                   remote_path, created_at
            FROM remote_stores;
            DROP TABLE remote_stores;
            ALTER TABLE remote_stores_v2 RENAME TO remote_stores;
            """
        )

    def close(self) -> None:
        with self._db_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------ 数据库

    def _load_all(self) -> list[RemoteStoreConfig]:
        if self._conn is None:
            return []
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT id, name, host, port, username, auth_mode, remote_path,"
                " created_at"
                " FROM remote_stores ORDER BY created_at, name"
            ).fetchall()
        return [
            RemoteStoreConfig(
                id=r[0], name=r[1], host=r[2], port=int(r[3]), username=r[4],
                auth_mode=r[5], remote_path=r[6], created_at=int(r[7]),
            )
            for r in rows
        ]

    def _load_one(self, store_id: str) -> RemoteStoreConfig:
        if self._conn is not None:
            with self._db_lock:
                row = self._conn.execute(
                    "SELECT id, name, host, port, username, auth_mode, remote_path,"
                    " created_at"
                    " FROM remote_stores WHERE id = ?",
                    (store_id,),
                ).fetchone()
            if row is not None:
                return RemoteStoreConfig(
                    id=row[0], name=row[1], host=row[2], port=int(row[3]),
                    username=row[4], auth_mode=row[5], remote_path=row[6],
                    created_at=int(row[7]),
                )
        raise RemoteStoreNotFound("远程挂载不存在或已被删除")

    def _duplicate_exists(
        self, name: str, host: str, port: int, remote_path: str
    ) -> bool:
        """同组下相同主机与远端路径视为重复挂载。"""
        if self._conn is None:
            return False
        with self._db_lock:
            row = self._conn.execute(
                "SELECT 1 FROM remote_stores"
                " WHERE name = ? AND host = ? AND port = ? AND remote_path = ?",
                (name, host, port, remote_path),
            ).fetchone()
        return row is not None

    def _insert(self, store: RemoteStoreConfig, secret: str) -> None:
        assert self._fernet is not None
        token = self._fernet.encrypt(secret.encode("utf-8"))
        with self._db_lock:
            assert self._conn is not None
            self._conn.execute(
                "INSERT INTO remote_stores"
                " (id, name, host, port, username, auth_mode, secret, remote_path, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (store.id, store.name, store.host, store.port, store.username,
                 store.auth_mode, token, store.remote_path, store.created_at),
            )
            self._conn.commit()

    def _delete_row(self, store_id: str) -> None:
        with self._db_lock:
            if self._conn is not None:
                self._conn.execute(
                    "DELETE FROM remote_stores WHERE id = ?", (store_id,)
                )
                self._conn.commit()

    def _secret_for(self, store: RemoteStoreConfig) -> str:
        """解密凭据；SECRET_KEY 被更换导致解不开时给出明确错误。"""
        assert self._fernet is not None and self._conn is not None
        with self._db_lock:
            row = self._conn.execute(
                "SELECT secret FROM remote_stores WHERE id = ?", (store.id,)
            ).fetchone()
        if row is None:
            raise RemoteStoreNotFound("远程挂载不存在或已被删除")
        try:
            return self._fernet.decrypt(row[0]).decode("utf-8")
        except InvalidToken as exc:
            raise RemoteStoreError(
                f"「{store.name}」的凭据无法解密（SECRET_KEY 已更换？），请删除后重新添加"
            ) from exc

    # ------------------------------------------------------------ 状态

    async def _publish(self, reason: str) -> None:
        await self._bus.publish("state", {"reason": reason})

    def _set_state(self, name: str, state: str, error: str | None = None) -> bool:
        """更新内存状态；返回是否发生变化（变化才需要推 SSE）。"""
        info = self._states.get(name, {})
        changed = (
            info.get("state") != state
            or (state == "error" and info.get("error") != error)
        )
        if state == "mounted":
            self._states[name] = {"state": state, "error": None, "failures": 0}
        else:
            failures = info.get("failures", 0) if state != "mounting" else 0
            self._states[name] = {
                "state": state, "error": error, "failures": failures,
            }
        return changed

    def store_snapshot(self, key: str) -> dict:
        for row in self.list_stores():
            if row["key"] == key:
                return row
        raise RemoteStoreNotFound("远程挂载不存在或已被删除")

    def list_stores(self) -> list[dict]:
        if not self.enabled:
            return []
        stores = self._load_all()
        out = []
        for store in stores:
            state = self._states.get(store.id, {"state": "mounting", "error": None})
            # 盘符在首次挂载时分配；未挂载过的新配置无盘符
            drive = self._letters.letter_of(store.key)
            out.append({
                "key": store.key,
                "name": store.name,
                "host": store.host,
                "port": store.port,
                "username": store.username,
                "remotePath": store.remote_path,
                "state": state.get("state", "mounting"),
                "error": state.get("error"),
                "mode": "rw",
                "drive": drive,
                "sftpPath": drive or "",
            })
        return out

    def usage_snapshot(self) -> dict[str, dict[str, int]]:
        return dict(self._usage)

    # ------------------------------------------------------------ 挂载

    def _build_mount_command(
        self, store: RemoteStoreConfig, mp: Path
    ) -> tuple[list[str], bytes | None]:
        """构造 sshfs 参数数组；密码认证时一并返回 stdin 数据。"""
        self.ensure_dirs()
        mp.mkdir(parents=True, exist_ok=True)
        secret = self._secret_for(store)
        args = [
            "sshfs",
            f"{store.username}@{store.host}:{store.remote_path or '/'}",
            str(mp),
            "-p", str(store.port),
            "-o", "reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,"
                  "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self._known_hosts}",
        ]
        if store.auth_mode == "password":
            args += ["-o", "password_stdin"]
            return args, (secret + "\n").encode("utf-8")
        key_file = self._key_dir / store.id
        key_file.write_text(secret, encoding="utf-8")
        os.chmod(key_file, 0o600)
        args += ["-o", f"IdentityFile={key_file},IdentitiesOnly=yes"]
        return args, None

    @staticmethod
    def _friendly_mount_error(stderr: str) -> str:
        low = (stderr or "").lower()
        if ("host key" in low and "changed" in low) or "host key verification failed" in low:
            return ("远程主机指纹已变化，连接被拒绝（防中间人）。"
                    "确认服务器未被盗用后，删除 data/ssh/known_hosts_remote 中对应行可重置")
        if "permission denied" in low or "authentications that can continue" in low:
            return "认证失败：用户名、密码或私钥不正确"
        if "connection refused" in low:
            return "连接被拒绝：端口错误或远端 SSH 服务未运行"
        if "connection timed out" in low or "no route to host" in low or "unreachable" in low:
            return "主机不可达：请检查主机地址与网络"
        if "name or service not known" in low or "resolve" in low:
            return "主机名无法解析：请检查主机地址"
        if "no such file or directory" in low:
            return "远端路径不存在"
        return f"挂载失败：{(stderr or '').strip()[-200:] or '未知错误'}"

    async def _mount_store(self, store: RemoteStoreConfig) -> None:
        """挂载单个远程挂载；一切失败都落到 error 状态，不向上抛。"""
        mp = self._mount_dir(store)
        try:
            args, input_data = self._build_mount_command(store, mp)
            log.info("挂载远程存储 %s:%s（%s@%s:%s，凭据已脱敏）",
                     store.name, store.remote_path, store.username,
                     store.host, store.port)
            rc, err = await _run_sshfs_mount(args, input_data, MOUNT_TIMEOUT)
            if rc != 0:
                raise MountError(self._friendly_mount_error(err))
            # sshfs 挂载成功后自行 daemonize；轮询确认挂载点已生效
            for _ in range(20):
                if await asyncio.to_thread(os.path.ismount, mp):
                    break
                await asyncio.sleep(0.25)
            else:
                raise MountError("sshfs 已退出但挂载点未生效")
        except RemoteStoreError as exc:
            self._set_state(store.id, "error", str(exc))
            log.warning("远程存储 %s:%s 挂载失败：%s",
                        store.name, store.remote_path, exc)
        except MountError as exc:
            self._set_state(store.id, "error", str(exc))
            log.warning("远程存储 %s:%s 挂载失败：%s",
                        store.name, store.remote_path, exc)
        except Exception as exc:  # noqa: BLE001 - 任何异常都落到状态而非崩溃
            self._set_state(store.id, "error", f"挂载失败：{exc}")
            log.exception("远程存储 %s:%s 挂载异常", store.name, store.remote_path)
        else:
            self._set_state(store.id, "mounted")
            log.info("远程存储 %s:%s 挂载成功", store.name, store.remote_path)
            await self._refresh_usage(store)
            await self._publish("remote")
            return
        # 挂载失败：清掉未生效的挂载点目录，避免空目录出现在 SFTP 根下
        if not await asyncio.to_thread(os.path.ismount, mp):
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.rmdir, mp)
        await self._publish("remote")

    async def _lazy_unmount(self, store: RemoteStoreConfig) -> None:
        """惰性卸载挂载点（重连/删除/停止前的清理），幂等。"""
        mp = self._mount_dir(store)
        if await asyncio.to_thread(os.path.ismount, mp):
            rc, _, _ = await run_cmd_capture(
                ["fusermount3", "-u", str(mp)], timeout=20
            )
            if rc != 0:
                await run_cmd_capture(["fusermount3", "-uz", str(mp)], timeout=20)
        with contextlib.suppress(OSError):
            await asyncio.to_thread(os.rmdir, mp)

    async def _refresh_usage(self, store: RemoteStoreConfig) -> None:
        mp = self._mount_dir(store)
        try:
            st = await asyncio.wait_for(
                asyncio.to_thread(os.statvfs, mp), PROBE_TIMEOUT
            )
        except Exception:  # noqa: BLE001 - 拿不到容量不影响挂载状态
            self._usage.pop(store.key, None)
            return
        frsize = st.f_frsize
        total = st.f_blocks * frsize
        free_all = st.f_bfree * frsize
        self._usage[store.key] = {
            "total": total,
            "used": max(0, total - free_all),
            "avail": st.f_bavail * frsize,
        }

    # ------------------------------------------------------------ 公开操作

    async def start(self) -> None:
        """启动：挂载已保存的远程存储并开启健康探测。"""
        if not self.enabled:
            return
        self.ensure_dirs()
        stores = self._load_all()
        if stores:
            log.info("恢复 %d 个远程存储的挂载", len(stores))
            for store in stores:
                self._set_state(store.id, "mounting")
            await self._publish("remote")
            await asyncio.gather(*(self._mount_store(s) for s in stores))
        self._probe_task = asyncio.create_task(self._probe_loop(), name="remote-probe")

    async def stop(self) -> None:
        self._stopping = True
        if self._probe_task is not None:
            self._probe_task.cancel()
            self._probe_task = None

    async def create(
        self,
        *,
        group: str,
        host: str,
        port: int,
        username: str,
        remote_path: str,
        auth_mode: str,
        secret: str,
    ) -> dict:
        if not self.enabled:
            raise RemoteStoreError(
                "未设置 SECRET_KEY，无法添加远程存储（凭据需加密保存，请先配置 SECRET_KEY）"
            )
        host = (host or "").strip()
        username = (username or "").strip()
        remote_path = (remote_path or "/").strip() or "/"
        if not host:
            raise RemoteStoreError("主机地址不能为空")
        if not username:
            raise RemoteStoreError("用户名不能为空")
        if not remote_path.startswith("/"):
            raise RemoteStoreError("远程路径必须是以 / 开头的绝对路径")
        if auth_mode not in ("password", "key"):
            raise RemoteStoreError("认证方式无效")
        if not (secret or "").strip():
            raise RemoteStoreError(
                "密码认证请填写密码；私钥认证请粘贴 PEM 私钥文本"
            )
        if auth_mode == "key" and "PRIVATE KEY" not in secret:
            raise RemoteStoreError(
                "私钥格式无效：需粘贴 PEM 文本（-----BEGIN ... PRIVATE KEY-----）"
            )
        # 远程组名留空时默认取用户名
        name = validate_store_name(group) if (group or "").strip() else username

        async with self._ops_lock:
            if self._duplicate_exists(name, host, port, remote_path):
                raise RemoteStoreExists(
                    f"远程组「{name}」下已存在相同主机与路径的挂载 {remote_path}"
                )
            store = RemoteStoreConfig(
                id=uuid.uuid4().hex[:12], name=name, host=host, port=port,
                username=username, auth_mode=auth_mode, remote_path=remote_path,
                created_at=int(time.time()),
            )
            self._insert(store, secret)
            log.info("新增远程挂载 %s:%s（%s@%s:%s）",
                     name, remote_path, username, host, port)
        self._set_state(store.id, "mounting")
        await self._publish("remote")
        await self._mount_store(store)
        return self.store_snapshot(store.key)

    async def reconnect(self, key: str) -> dict:
        """重新连接：先惰性卸载残留，再按保存的配置重新挂载（凭据不出库）。"""
        if not key.startswith("remote:"):
            raise RemoteStoreNotFound("远程挂载不存在")
        store_id = key[len("remote:"):]
        async with self._ops_lock:
            store = self._load_one(store_id)   # 不存在抛 RemoteStoreNotFound
            self._set_state(store.id, "mounting")
            await self._publish("remote")
            await self._lazy_unmount(store)
            await self._mount_store(store)
        return self.store_snapshot(key)

    async def delete(self, key: str) -> None:
        """卸载并删除（仅删除挂载配置，不删除远端文件）。"""
        if not key.startswith("remote:"):
            raise RemoteStoreNotFound("远程挂载不存在")
        store_id = key[len("remote:"):]
        async with self._ops_lock:
            store = self._load_one(store_id)
            await self._lazy_unmount(store)
            self._delete_row(store.id)
            with contextlib.suppress(OSError):
                (self._key_dir / store.id).unlink(missing_ok=True)
            self._states.pop(store.id, None)
            self._usage.pop(store.key, None)
            # 盘符随挂载配置一起删除，回收复用
            self._letters.release(store.key)
        log.info("已删除远程挂载 %s:%s（远端文件不受影响）",
                 store.name, store.remote_path)
        await self._publish("remote")

    async def unmount_all(self) -> None:
        """容器关闭：惰性卸载全部远程挂载点（在 MountManager.unmount_all 之前调用）。"""
        for store in self._load_all():
            try:
                await self._lazy_unmount(store)
            except Exception:  # noqa: BLE001
                log.warning("停止时卸载远程存储 %s:%s 失败",
                            store.name, store.remote_path, exc_info=True)

    # ------------------------------------------------------------ 健康探测

    @staticmethod
    def _friendly_probe_error(exc: Exception) -> str:
        if isinstance(exc, asyncio.TimeoutError):
            return "健康探测超时：远程存储无响应"
        if isinstance(exc, OSError):
            return f"连接异常：{exc.strerror or exc}"
        return f"连接异常：{exc}"

    async def _probe_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(PROBE_INTERVAL)
            try:
                await self._probe_once()
            except Exception:  # noqa: BLE001
                log.exception("远程存储健康探测异常")

    async def _probe_once(self) -> None:
        for store in self._load_all():
            state = self._states.get(store.id, {}).get("state")
            if state not in ("mounted", "error"):
                continue   # mounting 由挂载流程自行收敛
            mp = self._mount_dir(store)

            def _probe_path() -> None:
                # 必须先确认确实是挂载点：挂载失败后残留的普通目录
                # statvfs 也会成功，会导致 error 被误判为“恢复连接”
                if not os.path.ismount(mp):
                    raise OSError(f"{mp} 不是挂载点")
                os.statvfs(mp)

            try:
                await asyncio.wait_for(
                    asyncio.to_thread(_probe_path), PROBE_TIMEOUT
                )
            except Exception as exc:  # noqa: BLE001
                info = self._states.setdefault(
                    store.id, {"state": state, "error": None, "failures": 0}
                )
                info["failures"] = info.get("failures", 0) + 1
                self._usage.pop(store.key, None)
                if info["failures"] >= PROBE_FAILURES_TO_ERROR:
                    msg = self._friendly_probe_error(exc)
                    if self._set_state(store.id, "error", msg):
                        log.warning("远程存储 %s:%s 连接异常：%s",
                                    store.name, store.remote_path, msg)
                        await self._publish("remote")
            else:
                if state == "error":
                    log.info("远程存储 %s:%s 恢复连接",
                             store.name, store.remote_path)
                if self._set_state(store.id, "mounted"):
                    await self._publish("remote")
                await self._refresh_usage(store)
