"""容器内后台文件传输：跨卷移动 / 同卷重命名，独立于网页请求运行。

设计要点：
- 任务由 asyncio 调度、在线程中执行阻塞式文件 I/O，网页关闭甚至无人
  浏览时任务照常继续；容器重启则清空（不承诺跨重启续传）。
- 同卷移动走 os.rename（原子、秒成）；跨卷移动 = 分块复制成功后再删源，
  任何失败 / 取消都只清理目标半成品，源文件绝不先删，避免数据丢失。
- 源卷以只读挂载时无法删除源，自动降级为“复制”（源保留），任务中注明。
- 进度（字节 / 文件数）直接由工作线程写入任务对象，事件循环每秒推送一次。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("transfers")

_COPY_BUFSIZE = 1024 * 1024  # 1 MiB
_HISTORY_LIMIT = 50


class _JobCanceled(Exception):
    """工作线程内的用户取消信号（不可使用 asyncio.CancelledError：
    那会在事件循环侧被当成任务取消并杀死常驻 worker）。"""


@dataclass
class TransferJob:
    id: str
    src_key: str
    src_rel: str
    dst_key: str
    dst_rel: str
    name: str
    op: str  # "move" | "copy"
    # 绝对路径在提交时由 API 层校验后填入
    src_abs: str = ""
    dst_dir_abs: str = ""
    status: str = "queued"  # queued | running | done | error | canceled
    bytes_total: int = 0
    bytes_done: int = 0
    files_total: int = 0
    files_done: int = 0
    current: str = ""
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "srcKey": self.src_key,
            "srcPath": self.src_rel,
            "dstKey": self.dst_key,
            "dstPath": self.dst_rel,
            "name": self.name,
            "op": self.op,
            "status": self.status,
            "bytesTotal": self.bytes_total,
            "bytesDone": self.bytes_done,
            "filesTotal": self.files_total,
            "filesDone": self.files_done,
            "current": self.current,
            "error": self.error,
            "createdAt": int(self.created_at),
            "startedAt": int(self.started_at) if self.started_at else None,
            "finishedAt": int(self.finished_at) if self.finished_at else None,
        }


def _split_name(name: str) -> tuple[str, str]:
    """a.txt -> ('a', '.txt')；目录/无扩展名 -> (name, '')。"""
    if "." in name:
        base, dot, ext = name.rpartition(".")
        if base:
            return base, f".{ext}"
    return name, ""


def _unique_destination(dst_dir: Path, name: str) -> Path:
    """目标已存在时追加 “(1)/(2)” 序号，绝不覆盖已有文件。"""
    candidate = dst_dir / name
    if not candidate.exists() and not candidate.is_symlink():
        return candidate
    base, ext = _split_name(name)
    for i in range(1, 10_000):
        candidate = dst_dir / f"{base} ({i}){ext}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise OSError(17, "无法生成不冲突的目标文件名", name)


def _scan_tree(root: Path) -> tuple[int, int]:
    """统计待复制的总字节与文件数。"""
    if root.is_file() or root.is_symlink():
        try:
            return root.stat().st_size, 1
        except OSError:
            return 0, 1
    total_bytes = 0
    total_files = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            try:
                total_bytes += (Path(dirpath) / fn).stat().st_size
                total_files += 1
            except OSError:
                total_files += 1
    return total_bytes, total_files


def _copy_file(src: Path, dst: Path, job: TransferJob) -> None:
    """分块复制单文件：支持取消、字节进度、保留元数据。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            if job.cancel_event.is_set():
                raise _JobCanceled
            chunk = fin.read(_COPY_BUFSIZE)
            if not chunk:
                break
            fout.write(chunk)
            job.bytes_done += len(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    with contextlib.suppress(OSError):
        shutil.copystat(src, dst, follow_symlinks=False)
    job.files_done += 1


def _do_transfer(job: TransferJob) -> None:
    """阻塞式执行（工作线程内）。成功返回；取消抛 _JobCanceled；其他错误抛 OSError。"""
    src = Path(job.src_abs)
    dst_dir = Path(job.dst_dir_abs)
    same_volume = job.src_key == job.dst_key

    if same_volume and job.op == "move":
        # 同卷移动：原子重命名，瞬间完成（同卷复制走下面的真实拷贝）
        dst = _unique_destination(dst_dir, job.name)
        os.rename(src, dst)
        job.bytes_done = job.bytes_total = max(job.bytes_total, 1)
        job.files_done = job.files_total = 1
        return

    # 跨卷移动/复制、同卷复制：先统计总量
    job.current = "统计文件数…"
    total_bytes, total_files = _scan_tree(src)
    job.bytes_total = total_bytes
    job.files_total = max(total_files, 1)

    dst = _unique_destination(dst_dir, job.name)

    def rollback() -> None:
        try:
            if dst.is_symlink() or dst.is_file():
                dst.unlink()
            elif dst.is_dir():
                shutil.rmtree(dst)
        except OSError:
            log.warning("回滚目标半成品失败：%s", dst)

    try:
        if src.is_dir() and not src.is_symlink():
            dst.mkdir(parents=True, exist_ok=True)
            for dirpath, dirnames, filenames in os.walk(src):
                if job.cancel_event.is_set():
                    raise _JobCanceled
                rel = Path(dirpath).relative_to(src)
                target_dir = dst / rel
                target_dir.mkdir(parents=True, exist_ok=True)
                for dn in dirnames:
                    (target_dir / dn).mkdir(exist_ok=True)
                for fn in filenames:
                    if job.cancel_event.is_set():
                        raise _JobCanceled
                    job.current = str(rel / fn) if str(rel) != "." else fn
                    _copy_file(Path(dirpath) / fn, target_dir / fn, job)
        else:
            job.current = job.name
            _copy_file(src, dst, job)

        if job.cancel_event.is_set():
            raise _JobCanceled

        # 复制完整成功后，move 才删除源；copy / 只读源保留
        if job.op == "move":
            if src.is_symlink() or src.is_file():
                src.unlink()
            else:
                shutil.rmtree(src)
    except BaseException:
        rollback()
        raise


class TransferManager:
    """串行执行传输任务（避免多 USB 盘并发抢占），状态经 SSE 推送。"""

    def __init__(self, bus) -> None:
        self._bus = bus
        self._jobs: dict[str, TransferJob] = {}
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._consume(), name="transfer-worker")

    async def _publish(self) -> None:
        await self._bus.publish("jobs", {"jobs": self.snapshot()})

    async def submit(
        self,
        *,
        src_key: str,
        src_rel: str,
        dst_key: str,
        dst_rel: str,
        name: str,
        op: str,
        src_abs: Path,
        dst_dir_abs: Path,
    ) -> TransferJob:
        job = TransferJob(
            id=uuid.uuid4().hex[:12],
            src_key=src_key,
            src_rel=src_rel,
            dst_key=dst_key,
            dst_rel=dst_rel,
            name=name,
            op=op,
            src_abs=str(src_abs),
            dst_dir_abs=str(dst_dir_abs),
        )
        async with self._lock:
            self._jobs[job.id] = job
            self._trim_history()
        await self._publish()
        self._wake.set()
        return job

    def _trim_history(self) -> None:
        finished = [
            j for j in self._jobs.values()
            if j.status in ("done", "error", "canceled")
        ]
        finished.sort(key=lambda j: j.finished_at or 0)
        for job in finished[:-_HISTORY_LIMIT] if len(finished) > _HISTORY_LIMIT else []:
            self._jobs.pop(job.id, None)

    async def _consume(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while True:
                job = next(
                    (j for j in self._jobs.values() if j.status == "queued"), None
                )
                if job is None:
                    break
                await self._run(job)

    async def _run(self, job: TransferJob) -> None:
        job.status = "running"
        job.started_at = time.time()
        await self._publish()

        ticker = asyncio.create_task(self._tick_loop())
        try:
            await asyncio.to_thread(_do_transfer, job)
            job.status = "done"
            log.info("传输完成：%s %s -> %s（%s）",
                     job.op, job.src_rel, job.dst_key, job.name)
        except _JobCanceled:
            # 注意：不能让取消异常逃出 _consume，否则常驻 worker 协程死亡，
            # 之后提交的所有任务都会永远停在 queued
            job.status = "canceled"
            log.info("传输已取消：%s", job.name)
        except Exception as exc:  # noqa: BLE001 - 任何 I/O 错误都落到任务状态
            job.status = "error"
            job.error = str(exc) or exc.__class__.__name__
            log.warning("传输失败：%s：%s", job.name, job.error)
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
            job.current = ""
            job.finished_at = time.time()
            async with self._lock:
                self._trim_history()
            await self._publish()
            self._wake.set()

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self._publish()

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job and job.status in ("queued", "running"):
            job.cancel_event.set()
            # 排队中的任务由 worker 取出时直接转取消
            if job.status == "queued":
                job.status = "canceled"
                job.finished_at = time.time()
                self._wake.set()
            return True
        return False

    def snapshot(self) -> list[dict]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [j.snapshot() for j in jobs]

    def active_for_key(self, key: str) -> list[dict]:
        """正在排队 / 传输且涉及指定卷的任务（弹出卷前拦截用）。"""
        return [
            j.snapshot() for j in self._jobs.values()
            if j.status in ("queued", "running")
            and (j.src_key == key or j.dst_key == key)
        ]
