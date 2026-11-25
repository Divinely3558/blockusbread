"""后台传输任务回归测试：取消任务不能杀死常驻 worker。"""

from __future__ import annotations

import asyncio

from app.transfers.jobs import TransferManager


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


async def _scenario_cancel(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    # 足够大的源文件，保证取消时复制仍在进行
    big = src / "big.bin"
    with open(big, "wb") as f:
        for _ in range(200):
            f.write(b"\0" * 1024 * 1024)

    mgr = TransferManager(_FakeBus())
    await mgr.start()
    try:
        j1 = await mgr.submit(
            src_key="A", src_rel="big.bin", dst_key="B", dst_rel="",
            name="big.bin", op="copy", src_abs=big, dst_dir_abs=dst,
        )
        # 等复制确实开始（已在传字节）后再取消，命中 running 取消路径
        deadline = asyncio.get_event_loop().time() + 5
        while not (j1.status == "running" and 0 < j1.bytes_done < j1.bytes_total):
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError("复制任务未如预期进入传输中状态")
            await asyncio.sleep(0.01)
        assert mgr.cancel(j1.id) is True
        await _wait_status(j1, {"canceled"})

        # 回归点：取消一个任务后 worker 必须存活，后续任务照常执行
        assert not mgr._worker.done(), "传输 worker 在取消后死亡，后续任务将永久排队"
        assert not (dst / "big.bin").exists(), "取消后目标半成品应被回滚删除"

        small = src / "small.txt"
        small.write_text("hello", encoding="utf-8")
        j2 = await mgr.submit(
            src_key="A", src_rel="small.txt", dst_key="B", dst_rel="",
            name="small.txt", op="copy", src_abs=small, dst_dir_abs=dst,
        )
        await _wait_status(j2, {"done"})
        assert (dst / "small.txt").read_text(encoding="utf-8") == "hello"
    finally:
        mgr._worker.cancel()


def test_cancel_does_not_kill_worker(tmp_path):
    asyncio.run(_scenario_cancel(tmp_path))


async def _scenario_copy_dir(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("a", encoding="utf-8")
    (src / "sub" / "b.txt").write_text("bb", encoding="utf-8")
    dst.mkdir()

    mgr = TransferManager(_FakeBus())
    await mgr.start()
    try:
        job = await mgr.submit(
            src_key="A", src_rel="src", dst_key="B", dst_rel="",
            name="src", op="copy", src_abs=src, dst_dir_abs=dst,
        )
        await _wait_status(job, {"done"})
        assert (dst / "src" / "a.txt").read_text(encoding="utf-8") == "a"
        assert (dst / "src" / "sub" / "b.txt").read_text(encoding="utf-8") == "bb"
        assert (src / "a.txt").exists(), "复制不应删除源文件"
    finally:
        mgr._worker.cancel()


def test_copy_directory(tmp_path):
    asyncio.run(_scenario_copy_dir(tmp_path))


def test_sftp_activity_detection(monkeypatch):
    """SFTP 会话有 /proc io 增量视为活跃，停止 I/O 超过空闲阈值后恢复。"""
    from app.transfers import jobs

    clock = [1000.0]
    counter = {"io": 1000}
    monkeypatch.setattr(jobs.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(jobs, "_sshd_session_pids", lambda: [42])
    monkeypatch.setattr(jobs, "_proc_io_bytes", lambda _pid: counter["io"])

    activity = jobs._SftpActivity(idle_seconds=3.0, sample_seconds=0.5)
    assert activity.active() is False
    clock[0] += 0.5
    counter["io"] += 4096
    assert activity.active() is True
    clock[0] += 10.0
    assert activity.active() is False


def test_rate_limiter_prefers_busy_rate_when_sftp_active():
    from app.transfers import jobs

    mb = 1024 * 1024
    limiter = jobs._RateLimiter(30 * mb, 8 * mb)
    limiter._activity.active = lambda: True
    assert limiter._current_rate() == 8 * mb
    limiter._activity.active = lambda: False
    assert limiter._current_rate() == 30 * mb


def test_rate_limiter_skips_when_turbo_enabled():
    """极速模式置位后不再等待：16 MiB 应立即写完，而不是按 8 MiB/s 拖 2 秒。"""
    import threading
    import time

    from app.transfers import jobs

    mb = 1024 * 1024
    turbo = threading.Event()
    turbo.set()
    limiter = jobs._RateLimiter(30 * mb, 8 * mb, turbo)
    limiter._activity.active = lambda: True
    start = time.monotonic()
    limiter.consume(16 * mb, threading.Event())
    assert time.monotonic() - start < 0.05


async def _scenario_turbo(tmp_path):
    from app.transfers import jobs

    mgr = TransferManager(_FakeBus())
    await mgr.start()
    old_idle = jobs._TURBO_IDLE_SECONDS
    jobs._TURBO_IDLE_SECONDS = 0.05
    try:
        assert mgr.turbo_enabled is False
        assert await mgr.set_turbo(True) is True
        assert mgr.turbo_enabled is True
        assert mgr._bus.events[-1][1]["turbo"] is True
        # 幂等：重复开启仍是开启
        assert await mgr.set_turbo(True) is True
        # 队列空闲时到点自动关闭
        await asyncio.sleep(0.15)
        assert mgr.turbo_enabled is False, "队列空闲超过阈值后应自动关闭极速模式"
        assert await mgr.set_turbo(False) is False
    finally:
        jobs._TURBO_IDLE_SECONDS = old_idle
        mgr._cancel_turbo_idle_timer()
        mgr._worker.cancel()


def test_turbo_toggle_and_auto_off(tmp_path):
    asyncio.run(_scenario_turbo(tmp_path))


def test_turbo_api_toggles_and_reports_state():
    """接口切换后 /api/jobs 与 SSE 快照都带 turbo 字段。"""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import app.main as main

    mgr = TransferManager(_FakeBus())
    main.app.state.sessions = SimpleNamespace(
        resolve=lambda request: {"username": "tester"}
    )
    main.app.state.transfers = mgr
    client = TestClient(main.app)

    resp = client.post("/api/transfers/turbo", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["turbo"] is True
    assert mgr.turbo_enabled is True
    assert client.get("/api/jobs").json()["turbo"] is True

    resp = client.post("/api/transfers/turbo", json={"enabled": False})
    assert resp.json()["turbo"] is False
    assert client.get("/api/jobs").json()["turbo"] is False
