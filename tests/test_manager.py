"""MountManager 行为：记住凭据 = 手动解锁免输密码，绝不自动挂载。

用户语义：「记住此卷」只记住密码（下次手动解锁不用再输），
插入硬盘 / 重启软件后挂载必须手动点击，后端不得自动解锁。
"""

from __future__ import annotations

import asyncio

import pytest

from app.devices import scanner
from app.events import EventBus
from app.models import DiskInfo, PartitionInfo, VolumeRuntime, VolumeState
from app.mounts.commands import MountError
from app.mounts.manager import MountManager


def _bitlocker_disk() -> DiskInfo:
    return DiskInfo(
        disk_id="USB-DISK-001",
        name="sdb",
        path="/dev/sdb",
        vendor="Kingston",
        model="Portable",
        serial="USB-DISK-001",
        tran="usb",
        size=1_000_204_880_384,
        removable=True,
        partitions=[
            PartitionInfo(
                disk_id="USB-DISK-001",
                number=1,
                name="sdb1",
                path="/dev/sdb1",
                size=1_000_203_856_896,
                fstype="BitLocker",
                label="",
                uuid="",
                partuuid="pu-sdb1",
                bitlocker=True,
            )
        ],
    )


class FakeSecrets:
    """SecretsStore 内存替身。"""

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, str, str]] = {}

    def save(self, key: str, kind: str, secret: str, mode: str) -> None:
        self._data[key] = (kind, secret, mode)

    def load(self, key: str):
        return self._data.get(key)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def all_keys(self) -> set[str]:
        return set(self._data)


def _make_manager(secrets=None) -> MountManager:
    mgr = MountManager(EventBus(), secrets)
    disk = _bitlocker_disk()
    mgr._disks = [disk]
    part = disk.partitions[0]
    mgr._volumes[part.key] = VolumeRuntime(
        key=part.key, device=part.path, bitlocker=True
    )
    return mgr


def test_unlock_with_empty_secret_uses_saved_credential(monkeypatch):
    """secret 留空 = 用已保存凭据解锁（免输密码），读写模式跟随本次请求。"""
    mgr = _make_manager(FakeSecrets())
    part = mgr._disks[0].partitions[0]
    mgr._secrets.save(part.key, "password", "我的密码", "ro")

    calls: list[tuple[str, str, str, bool]] = []

    async def fake_unlock_locked(p, kind, secret, writable, actor):
        calls.append((p.key, kind, secret, writable))

    monkeypatch.setattr(mgr, "_unlock_locked", fake_unlock_locked)
    asyncio.run(mgr.unlock_volume(
        key=part.key, kind="password", secret="",
        writable=True, remember=False, actor="tester",
    ))
    assert calls == [(part.key, "password", "我的密码", True)]


def test_unlock_with_empty_secret_and_no_saved_credential_errors():
    """没有已保存凭据时 secret 留空必须报错，不能静默失败。"""
    mgr = _make_manager(FakeSecrets())
    part = mgr._disks[0].partitions[0]
    with pytest.raises(MountError, match="没有已保存的凭据"):
        asyncio.run(mgr.unlock_volume(
            key=part.key, kind="password", secret="",
            writable=False, remember=False, actor="tester",
        ))


def test_rescan_never_auto_unlocks_saved_volumes(monkeypatch):
    """插入已记住凭据的硬盘：rescan 只登记卷，绝不自动解锁挂载。"""
    mgr = _make_manager(FakeSecrets())
    part = mgr._disks[0].partitions[0]
    mgr._secrets.save(part.key, "password", "我的密码", "ro")

    calls: list = []

    async def fake_unlock_locked(p, kind, secret, writable, actor):
        calls.append(p.key)

    monkeypatch.setattr(scanner, "scan", lambda: [_bitlocker_disk()])
    monkeypatch.setattr(mgr, "_unlock_locked", fake_unlock_locked)

    asyncio.run(mgr.rescan("scan"))

    rt = mgr._volumes[part.key]
    assert rt.state == VolumeState.PRESENT
    assert calls == []
