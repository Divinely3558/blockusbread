"""三类存储 + 后台传输增强的单元测试。

覆盖：路径冲突预检、ETA 滑动窗口、本地存储发现、远程存储凭据加解密
与重名拒绝、统一卷注册表三类 key 查找。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.models import MountMode
from app.stores.local import LocalStoreManager
from app.stores.registry import StoreRegistry, VolumeNotFound, VolumeUnavailable
from app.stores.remote import (
    RemoteStoreError,
    RemoteStoreExists,
    RemoteStoreManager,
    validate_store_name,
)
from app.transfers.jobs import TransferConflict, TransferJob, TransferManager, paths_overlap

# ------------------------------------------------------------ 路径冲突预检


def test_paths_overlap_same_volume(tmp_path):
    a = tmp_path / "a"
    assert paths_overlap("k", a, "k", a) is True
    assert paths_overlap("k", a, "k", a / "b") is True
    assert paths_overlap("k", a / "b", "k", a) is True


def test_paths_overlap_distinct_paths(tmp_path):
    assert paths_overlap("k", tmp_path / "a", "k", tmp_path / "b") is False
    # 字符串前缀相同但不是祖先目录
    assert paths_overlap("k", tmp_path / "a" / "b", "k", tmp_path / "a" / "b2") is False


def test_paths_overlap_cross_volume(tmp_path):
    assert paths_overlap("k1", tmp_path / "a", "k2", tmp_path / "a") is False


# ------------------------------------------------------------ ETA 滑动窗口


def _job(total: int) -> TransferJob:
    return TransferJob(
        id="t1", src_key="A", src_rel="f", dst_key="B", dst_rel="",
        name="f", op="copy", src_abs="/x/f", dst_dir_abs="/y",
    )


def test_eta_computed_from_sliding_window():
    job = _job(total=20480)
    job.bytes_total = 20480
    job.bytes_done = 0
    job.record_sample(0.0)
    job.bytes_done = 10240
    job.record_sample(10.0)
    rate, eta = job.eta_fields()
    assert rate == 1024.0
    assert eta == 10  # 剩余 10240 字节 / 1024 B/s


def test_eta_none_when_rate_too_low():
    job = _job(total=10_000_000)
    job.bytes_total = 10_000_000
    job.record_sample(0.0)
    job.bytes_done = 100
    job.record_sample(10.0)
    rate, eta = job.eta_fields()
    assert rate == 10.0
    assert eta is None  # 低于 1 KiB/s，估算无意义


def test_eta_window_keeps_recent_10s():
    job = _job(total=0)
    job.record_sample(0.0)
    job.record_sample(5.0)
    job.record_sample(20.0)
    # 0s 样本已滑出窗口
    assert all(ts >= 5.0 for ts, _ in job._samples)


def test_eta_smoothed_by_ewma():
    """速率骤降时 EWMA 只向新值靠近一小步，剩余时间不再大幅跳变。"""
    job = _job(total=100_000)
    job.bytes_total = 100_000
    job.bytes_done = 0
    job.record_sample(0.0)
    job.bytes_done = 10_000
    job.record_sample(10.0)          # 窗口速率 1000 B/s
    rate1, _ = job._compute_eta()
    assert rate1 == 1000.0           # 首次以窗口速率作为基准

    job.bytes_done = 12_000
    job.record_sample(20.0)          # 窗口速率骤降至 200 B/s
    rate2, _ = job._compute_eta()
    assert 200.0 < rate2 < 1000.0    # 平滑：只移动一小步

    # 持续低速 100 秒后收敛到低速附近（能跟上真实变化）
    rate = rate2
    done = 12_000
    for i in range(1, 11):
        done += 200 * 10
        job.bytes_done = done
        job.record_sample(20.0 + i * 10.0)
        rate, _ = job._compute_eta()
    assert rate <= 260.0


def test_eta_none_without_total():
    job = _job(total=0)
    job.record_sample(0.0)
    job.bytes_done = 4096
    job.record_sample(4.0)
    rate, eta = job.eta_fields()
    assert eta is None  # 总量未知，无法估算


# ------------------------------------------------------------ 本地存储


def test_local_store_discovery(tmp_path):
    root = tmp_path / "local"
    (root / "影视").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "file.txt").write_text("x", encoding="utf-8")

    mgr = LocalStoreManager(mount_root=tmp_path)
    stores = mgr.list_stores()
    assert [s["name"] for s in stores] == ["影视"]
    assert stores[0]["key"] == "local:影视"
    assert stores[0]["mode"] == "rw"
    assert stores[0]["sftpPath"] == "local/影视"
    assert mgr.exists("影视") is True
    assert mgr.exists("不存在") is False
    assert mgr.fs_dir("影视") == root / "影视"


def test_local_store_usage(tmp_path):
    (tmp_path / "local" / "data").mkdir(parents=True)
    mgr = LocalStoreManager(mount_root=tmp_path)
    usage = mgr.usage()
    row = usage["local:data"]
    assert row["total"] > 0
    assert row["used"] >= 0


# ------------------------------------------------------------ 远程存储


def _fernet_key() -> bytes:
    return Fernet.generate_key()


@pytest.fixture
def no_sshfs(monkeypatch):
    """把真实 sshfs 命令替换为立即失败的桩：只测配置/加密/状态机。"""

    async def fake_run(args, input_data=None, timeout=30):
        return 1, "", "mock: mount disabled"

    monkeypatch.setattr("app.stores.remote.run_cmd_capture", fake_run)


class _AsyncBus:
    async def publish(self, event_type: str, data: dict | None = None) -> None:
        pass


def _make_remote(tmp_path, key=_fernet_key()) -> RemoteStoreManager:
    return RemoteStoreManager(
        bus=_AsyncBus(),
        fernet_key=key,
        data_dir=tmp_path / "data",
        mount_root=tmp_path / "mnt",
    )


def test_remote_create_encrypts_secret(no_sshfs, tmp_path):
    key = _fernet_key()
    mgr = _make_remote(tmp_path, key)
    row = asyncio.run(mgr.create(
        group="nas", host="192.168.1.10", port=22, username="root",
        remote_path="/media", auth_mode="password", secret="s3cret",
    ))
    # 挂载必然失败（桩），但不抛错，落到 error 状态
    assert row["state"] == "error"

    # 数据库中凭据为 Fernet 密文，可解密回原文
    conn = sqlite3.connect(str(tmp_path / "data" / "remote_stores.db"))
    try:
        stored, = conn.execute(
            "SELECT secret FROM remote_stores WHERE name='nas'").fetchone()
    finally:
        conn.close()
    assert isinstance(stored, bytes)
    assert b"s3cret" not in stored
    assert Fernet(key).decrypt(stored) == b"s3cret"


def test_remote_group_defaults_to_username(no_sshfs, tmp_path):
    """远程组留空时默认取用户名。"""
    mgr = _make_remote(tmp_path)
    row = asyncio.run(mgr.create(
        group="", host="h", port=22, username="alice",
        remote_path="/data", auth_mode="password", secret="p",
    ))
    assert row["name"] == "alice"
    assert row["sftpPath"] == f"remote/alice/{row['remotePath'].strip('/').replace('/', '_')}"


def test_remote_duplicate_rejected(no_sshfs, tmp_path):
    """同组同主机同路径拒绝；同组不同路径 / 不同组同路径允许。"""
    mgr = _make_remote(tmp_path)

    async def scenario():
        await mgr.create(
            group="nas", host="h", port=22, username="u",
            remote_path="/", auth_mode="password", secret="p",
        )
        with pytest.raises(RemoteStoreExists):
            await mgr.create(
                group="nas", host="h", port=22, username="u",
                remote_path="/", auth_mode="password", secret="p2",
            )
        # 同组不同路径 → 允许（远程组的多个挂载）
        await mgr.create(
            group="nas", host="h", port=22, username="u",
            remote_path="/x", auth_mode="password", secret="p2",
        )
        # 不同组同路径 → 允许
        await mgr.create(
            group="nas2", host="h", port=22, username="u",
            remote_path="/", auth_mode="password", secret="p3",
        )
        rows = mgr.list_stores()
        assert len(rows) == 3
        assert len({r["key"] for r in rows}) == 3   # key 按挂载唯一
        sftp = {r["sftpPath"] for r in rows}
        assert sftp == {"remote/nas/root", "remote/nas/x", "remote/nas2/root"}

    asyncio.run(scenario())


def test_remote_migrate_v1_schema(no_sshfs, tmp_path):
    """v1（name 唯一且兼任 id）数据库升级后数据保留、可继续解析。"""
    import sqlite3 as _sqlite3

    db_path = tmp_path / "data" / "remote_stores.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE remote_stores ("
            " id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, host TEXT NOT NULL,"
            " port INTEGER NOT NULL DEFAULT 22, username TEXT NOT NULL,"
            " auth_mode TEXT NOT NULL, secret BLOB NOT NULL,"
            " remote_path TEXT NOT NULL, created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO remote_stores VALUES"
            " ('nas', 'nas', 'h', 22, 'u', 'password', X'00', '/vol1', 1)"
        )
        conn.commit()
    finally:
        conn.close()

    key = _fernet_key()
    mgr = _make_remote(tmp_path, key)   # 初始化即触发迁移
    rows = mgr.list_stores()
    assert len(rows) == 1
    assert rows[0]["key"] == "remote:nas"          # 老记录 id == name，key 不变
    assert rows[0]["sftpPath"] == "remote/nas/vol1"
    # 迁移后同组可再挂载不同路径（唯一约束已解除）
    asyncio.run(mgr.create(
        group="nas", host="h", port=22, username="u",
        remote_path="/vol2", auth_mode="password", secret="p",
    ))
    assert len(mgr.list_stores()) == 2


def test_remote_list_and_roundtrip(no_sshfs, tmp_path):
    key = _fernet_key()
    mgr = _make_remote(tmp_path, key)

    async def scenario():
        await mgr.create(
            group="nas", host="10.0.0.2", port=2022, username="admin",
            remote_path="/vol1", auth_mode="password", secret="pw1",
        )
        rows = mgr.list_stores()
        assert len(rows) == 1
        assert rows[0]["key"].startswith("remote:")
        assert rows[0]["key"] != "remote:nas"      # 新挂载 id 为随机串
        assert rows[0]["name"] == "nas"
        assert rows[0]["host"] == "10.0.0.2"
        assert rows[0]["port"] == 2022
        assert rows[0]["sftpPath"] == "remote/nas/vol1"
        # store_snapshot 按 key 取回
        assert mgr.store_snapshot(rows[0]["key"])["remotePath"] == "/vol1"

    asyncio.run(scenario())


def test_remote_invalid_name(no_sshfs, tmp_path):
    for bad in ["", "a/b", "a\\b", ".dot", "x" * 65]:
        with pytest.raises(RemoteStoreError):
            validate_store_name(bad)
    assert validate_store_name(" 合法名 ") == "合法名"


def test_remote_mount_command_key_auth(no_sshfs, tmp_path):
    mgr = _make_remote(tmp_path)

    async def scenario():
        await mgr.create(
            group="nas", host="h", port=22, username="u",
            remote_path="/mp", auth_mode="key",
            secret="-----BEGIN OPENSSH PRIVATE KEY-----\nKEYDATA\n-----END",
        )
        store = mgr._load_all()[0]
        args, stdin = mgr._build_mount_command(store, mgr._mount_dir(store))
        assert stdin is None
        assert any("IdentityFile=" in a for a in args)
        key_file = tmp_path / "data" / "remote_keys" / store.id
        assert "KEYDATA" in key_file.read_text(encoding="utf-8")
        assert (key_file.stat().st_mode & 0o777) == 0o600

    asyncio.run(scenario())


def test_remote_mount_command_password_stdin(no_sshfs, tmp_path):
    mgr = _make_remote(tmp_path)

    async def scenario():
        await mgr.create(
            group="nas", host="h", port=22, username="u",
            remote_path="/mp", auth_mode="password", secret="pw123",
        )
        store = mgr._load_all()[0]
        args, stdin = mgr._build_mount_command(store, mgr._mount_dir(store))
        assert stdin == b"pw123\n"
        assert "password_stdin" in args

    asyncio.run(scenario())


# ------------------------------------------------------------ 统一卷注册表


def _fake_external_manager():
    part_rw = SimpleNamespace(
        key="ext-rw", label="视频盘", number=1,
        fs_dir=Path("/mnt/usb/ext-rw-p1/fs"), disk_id="EXT1",
    )
    part_ro = SimpleNamespace(
        key="ext-ro", label=None, number=2,
        fs_dir=Path("/mnt/usb/EXT1-p2/fs"), disk_id="EXT1",
    )
    runtime_rw = SimpleNamespace(mode=MountMode.RW)
    runtime_ro = SimpleNamespace(mode=MountMode.RO)

    class FakeManager:
        def snapshot(self):
            return {"disks": [], "rememberEnabled": True}

        def mounted_partition(self, key):
            if key == "ext-rw":
                return part_rw, runtime_rw
            if key == "ext-ro":
                return part_ro, runtime_ro
            raise VolumeNotFound(key)

        def mounted_volumes(self):
            return [(part_rw, runtime_rw), (part_ro, runtime_ro)]

        async def usage(self):
            return {"ext-rw": {"total": 1, "used": 0, "avail": 1}}

    return FakeManager()


def _fake_remote_manager():
    class FakeRemote:
        def list_stores(self):
            return [
                {"key": "remote:nas", "name": "nas", "host": "h", "port": 22,
                 "username": "u", "remotePath": "/", "state": "mounted",
                 "error": None, "mode": "rw", "sftpPath": "remote/nas/root"},
                {"key": "remote:bad", "name": "bad", "host": "h", "port": 22,
                 "username": "u", "remotePath": "/", "state": "error",
                 "error": "x", "mode": "rw", "sftpPath": "remote/bad/root"},
            ]

        def fs_dir(self, key):
            return Path(f"/mnt/usb/remote/{key.split(':', 1)[1]}")

        def usage_snapshot(self):
            return {"remote:nas": {"total": 2, "used": 1, "avail": 1}}

    return FakeRemote()


def _registry(tmp_path) -> StoreRegistry:
    (tmp_path / "local" / "影视").mkdir(parents=True)
    return StoreRegistry(
        _fake_external_manager(),
        LocalStoreManager(mount_root=tmp_path),
        _fake_remote_manager(),
    )


def test_registry_lookup_local(tmp_path):
    ref = _registry(tmp_path).lookup("local:影视")
    assert ref.kind == "local"
    assert ref.writable is True
    assert ref.ejectable is False
    assert ref.fs_dir == tmp_path / "local" / "影视"
    assert ref.sftp_path == "local/影视"


def test_registry_lookup_external(tmp_path):
    reg = _registry(tmp_path)
    rw = reg.lookup("ext-rw")
    assert rw.writable is True and rw.kind == "external" and rw.ejectable is True
    ro = reg.lookup("ext-ro")
    assert ro.writable is False
    with pytest.raises(VolumeNotFound):
        reg.lookup("ext-none")


def test_registry_lookup_remote(tmp_path):
    reg = _registry(tmp_path)
    ref = reg.lookup("remote:nas")
    assert ref.kind == "remote" and ref.writable is True
    assert ref.name == "nas:/"          # 展示名 = 组名:远端路径
    assert ref.fs_dir == Path("/mnt/usb/remote/nas")
    assert ref.sftp_path == "remote/nas/root"
    # 错误状态的远程卷：存在但不可用
    with pytest.raises(VolumeUnavailable):
        reg.lookup("remote:bad")
    # 完全不存在的卷
    with pytest.raises(VolumeNotFound):
        reg.lookup("remote:ghost")


def test_registry_name_of_fallback(tmp_path):
    reg = _registry(tmp_path)
    assert reg.name_of("ext-rw") == "视频盘"
    assert reg.name_of("local:影视") == "影视"
    assert reg.name_of("remote:nas") == "nas:/"
    # 不可用卷也给出可读名
    assert reg.name_of("remote:bad") == "bad:/"


def test_registry_mounted_refs_and_usage(tmp_path):
    reg = _registry(tmp_path)
    refs = reg.mounted_refs()
    keys = {r.key for r in refs}
    assert keys == {"ext-rw", "ext-ro", "local:影视", "remote:nas"}
    usage = asyncio.run(reg.usage())
    assert usage["remote:nas"]["total"] == 2
    assert usage["local:影视"]["total"] > 0


# ------------------------------------------------------------ 传输冲突预检


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def publish(self, event_type: str, data: dict | None = None) -> None:
        self.events.append((event_type, data or {}))


async def _wait_status(job, statuses, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while job.status not in statuses:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"任务一直停留在 {job.status}，未进入 {statuses}")
        await asyncio.sleep(0.05)


async def _scenario_conflict(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    big = src / "big.bin"
    with open(big, "wb") as f:
        for _ in range(200):
            f.write(b"\0" * 1024 * 1024)
    other = src / "other.txt"
    other.write_text("hi", encoding="utf-8")

    mgr = TransferManager(_FakeBus())
    await mgr.start()
    try:
        j1 = await mgr.submit(
            src_key="A", src_rel="big.bin", dst_key="B", dst_rel="",
            name="big.bin", op="copy", src_abs=big, dst_dir_abs=dst,
            volume_names={"A": "源卷", "B": "目标卷"},
        )
        await _wait_status(j1, {"running"})

        # 与进行中任务的源文件重叠 → 409 语义的 TransferConflict
        with pytest.raises(TransferConflict):
            await mgr.submit(
                src_key="A", src_rel="big.bin", dst_key="B", dst_rel="",
                name="big.bin", op="copy", src_abs=big, dst_dir_abs=dst,
            )
        # 与进行中任务的目标文件重叠 → 同样拒绝
        with pytest.raises(TransferConflict):
            await mgr.submit(
                src_key="B", src_rel="big.bin", dst_key="A", dst_rel="",
                name="big.bin", op="copy", src_abs=dst / "big.bin",
                dst_dir_abs=src,
            )
        # 不重叠的文件正常入队
        j2 = await mgr.submit(
            src_key="A", src_rel="other.txt", dst_key="B", dst_rel="",
            name="other.txt", op="copy", src_abs=other, dst_dir_abs=dst,
        )
        assert j2.status == "queued"
        await _wait_status(j1, {"done", "error"})
        await _wait_status(j2, {"done"})
    finally:
        mgr._worker.cancel()


def test_transfer_conflict_precheck(tmp_path):
    asyncio.run(_scenario_conflict(tmp_path))
