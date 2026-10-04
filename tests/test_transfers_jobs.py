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
