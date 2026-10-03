"""BlockUSBRead 应用入口：装配 Samba、设备监听、挂载编排器与管理 Web 层。"""

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
from app.samba.manager import SambaManager
from app.secrets_store import SecretsStore
from app.web.api import router as api_router
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

    # 凭据存储：SECRET_KEY 环境变量优先，未设置时已自动生成持久化密钥
    if settings.key_source == "env":
        log.info("凭据加密密钥来自环境变量 SECRET_KEY")
    else:
        log.info("未设置 SECRET_KEY，使用自动生成的持久化密钥 %s（请勿删除该文件）",
                 config.SECRET_KEY_FILE)
    secrets = SecretsStore(config.SECRETS_DATABASE, settings.fernet_key)
    log.info("已启用「记住此卷」加密存储 %s", config.SECRETS_DATABASE)

    bus = EventBus()
    sessions = SessionStore(settings.session_signing_key)
    manager = MountManager(bus=bus, secrets=secrets)
    samba = SambaManager(settings)

    # Samba 先行（管理页登录校验依赖它）
    await samba.start()

    # 对外暴露给路由层
    app.state.bus = bus
    app.state.sessions = sessions
    app.state.manager = manager
    app.state.samba = samba
    app.state.settings = settings

    # 清理上次异常退出可能残留的挂载与 cryptsetup 映射设备
    await manager.cleanup_orphans()

    # 启动前先做一次全量扫描与状态重建（含已记住凭据卷的自动解锁）
    await manager.rescan("startup")

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
        await manager.unmount_all()
        if secrets is not None:
            secrets.close()
        await samba.stop()
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
    description="解密挂载 BitLocker USB 硬盘并通过 Samba 共享读取",
    lifespan=lifespan,
)

app.include_router(api_router)


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
    samba_alive = False
    if hasattr(app.state, "samba"):
        samba_alive = app.state.samba.alive
    return JSONResponse({"status": "ok", "version": __version__, "samba": samba_alive})
