"""BlockUSBRead 应用入口：装配 SFTP、设备监听、挂载编排器与管理 Web 层。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, config
from app.devices.monitor import UdevMonitor
from app.events import EventBus
from app.log import setup_logging
from app.mounts.manager import MountManager
from app.mounts.stats import SpeedMonitor
from app.secrets_store import SecretsStore
from app.sftp.manager import SftpManager
from app.stores.local import LocalStoreManager
from app.stores.registry import StoreRegistry
from app.stores.remote import RemoteStoreManager
from app.transfers.api import router as transfers_router
from app.transfers.jobs import TransferManager
from app.web.api import router as api_router
from app.web.files import router as files_router
from app.web.ratelimit import LoginRateLimiter
from app.web.sessions import SessionStore

log = logging.getLogger("main")
STATIC_DIR = Path(__file__).parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = config.load_settings()
    setup_logging(settings.log_level, config.LOG_DIR)
    log.info("BlockUSBRead v%s 启动", __version__)

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.MOUNT_ROOT.mkdir(parents=True, exist_ok=True)

    # 凭据存储（SECRET_KEY 未设置则记忆功能禁用）
    secrets = None
    if settings.remember_enabled:
        secrets = SecretsStore(config.SECRETS_DATABASE, settings.fernet_key)
        log.info("已启用「记住此卷」加密存储 %s", config.SECRETS_DATABASE)
    else:
        log.warning("未设置 SECRET_KEY，「记住此卷」功能禁用")

    bus = EventBus()
    sessions = SessionStore(settings.session_signing_key)
    login_limiter = LoginRateLimiter()
    manager = MountManager(bus=bus, secrets=secrets)
    local_stores = LocalStoreManager()
    remote_stores = RemoteStoreManager(
        bus=bus,
        fernet_key=settings.fernet_key if settings.remember_enabled else None,
    )
    registry = StoreRegistry(manager=manager, local=local_stores, remote=remote_stores)
    speeds = SpeedMonitor(registry)
    sftp = SftpManager(settings)
    transfers = TransferManager(bus=bus)

    # 本地/远程存储挂载根目录（Dockerfile 已有兜底）
    local_stores.ensure_root()
    remote_stores.ensure_dirs()

    # SFTP 服务先行（挂载卷通过它对外共享）
    await sftp.start()
    await transfers.start()

    # 对外暴露给路由层
    app.state.bus = bus
    app.state.sessions = sessions
    app.state.login_limiter = login_limiter
    app.state.manager = manager
    app.state.registry = registry
    app.state.remote = remote_stores
    app.state.sftp = sftp
    app.state.speeds = speeds
    app.state.transfers = transfers
    app.state.settings = settings

    # 传输速率采样（按已挂载卷，2 秒一期）
    await speeds.start()

    # 清理上次异常退出可能残留的挂载与 cryptsetup 映射设备
    # （/mnt/usb/local 与 /mnt/usb/remote 子树由 Docker / 远程管理器负责，不清理）
    await manager.cleanup_orphans()

    # 启动前先做一次全量扫描与状态重建（记住的凭据不自动挂载，解锁需手动点击）
    await manager.rescan("startup")

    # 远程存储：按保存的配置自动重连 + 健康探测
    await remote_stores.start()

    # uevent 监听线程 -> 调度到事件循环
    loop = asyncio.get_running_loop()

    def schedule_rescan() -> None:
        with contextlib.suppress(RuntimeError):
            asyncio.run_coroutine_threadsafe(manager.rescan("uevent"), loop)

    monitor = UdevMonitor(on_event=schedule_rescan)
    monitor.start()

    # 定时兜底扫描（15 秒），防止漏掉事件
    periodic = asyncio.create_task(_periodic_rescan(manager), name="periodic-rescan")

    try:
        yield
    finally:
        log.info("BlockUSBRead 关闭中……")
        periodic.cancel()
        monitor.stop()
        monitor.join(timeout=3)
        await remote_stores.stop()
        await remote_stores.unmount_all()
        await manager.unmount_all()
        await speeds.stop()
        if secrets is not None:
            secrets.close()
        remote_stores.close()
        await sftp.stop()
        log.info("已退出")


async def _periodic_rescan(manager: MountManager) -> None:
    while True:
        await asyncio.sleep(15)
        try:
            await manager.rescan("periodic")
        except Exception:
            log.exception("兜底扫描异常")


app = FastAPI(
    title="BlockUSBRead",
    version=__version__,
    description="解密挂载 BitLocker USB 硬盘并通过 SFTP 共享读取",
    lifespan=lifespan,
)

app.include_router(api_router)
app.include_router(files_router)
app.include_router(transfers_router)


class _NoCacheStatic(StaticFiles):
    """静态资源随版本迭代较快，禁止启发式缓存，避免更新后页面仍旧。"""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _NoCacheStatic(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", include_in_schema=False)
async def health():
    sftp_alive = False
    if hasattr(app.state, "sftp"):
        sftp_alive = app.state.sftp.alive
    return JSONResponse({"status": "ok", "version": __version__, "sftp": sftp_alive})
