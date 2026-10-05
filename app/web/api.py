"""管理后端 REST API 与 SSE 事件流。"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import config
from app.devices.smart import query_smart
from app.mounts.commands import MountError
from app.mounts.manager import VolumeNotFound
from app.sftp.manager import InvalidCredentials
from app.stores.remote import (
    RemoteStoreError,
    RemoteStoreExists,
    RemoteStoreNotFound,
)
from app.web.ratelimit import client_ip
from app.web.sessions import COOKIE_MAX_AGE, COOKIE_NAME

router = APIRouter(prefix="/api")
log = logging.getLogger("web.api")

# ---------------------------------------------------------------- 依赖


def get_sessions(request: Request):
    return request.app.state.sessions


def get_manager(request: Request):
    return request.app.state.manager


def get_registry(request: Request):
    return request.app.state.registry


def get_remote(request: Request):
    return request.app.state.remote


def get_sftp(request: Request):
    return request.app.state.sftp


async def current_session(request: Request) -> dict:
    session = request.app.state.sessions.resolve(request)
    if session is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return session


# ---------------------------------------------------------------- 请求体


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UnlockBody(BaseModel):
    kind: str = Field(default="password", pattern="^(password|recovery)$")
    secret: str = Field(min_length=1, max_length=512)
    writable: bool = False
    remember: bool = False


class MountBody(BaseModel):
    writable: bool = False


class RemoteStoreBody(BaseModel):
    group: str = Field(default="", max_length=64)   # 远程组名，留空默认取用户名
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    remotePath: str = Field(default="/", max_length=1024)
    authMode: str = Field(pattern="^(password|key)$")
    password: str = Field(default="", max_length=2048)
    privateKey: str = Field(default="", max_length=65536)


# ---------------------------------------------------------------- 认证


@router.post("/auth/login")
async def login(body: LoginBody, request: Request, response: Response):
    sftp = get_sftp(request)
    ip = client_ip(request)
    limiter = request.app.state.login_limiter
    retry_after = limiter.blocked_for(ip)
    if retry_after:
        log.warning("登录限流：%s 尝试过于频繁，要求 %d 秒后再试", ip, retry_after)
        raise HTTPException(
            status_code=429,
            detail=f"尝试过于频繁，请 {retry_after} 秒后再试",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        await sftp.verify_password(body.username, body.password)
    except InvalidCredentials as exc:
        limiter.failure(ip)
        log.warning("登录失败：%s 尝试账号 %s 被拒", ip, body.username)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    limiter.reset(ip)
    signed = get_sessions(request).create(body.username)
    # 经 HTTPS 反代（X-Forwarded-Proto: https）访问时下发 Secure cookie
    secure = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    response.set_cookie(
        COOKIE_NAME, signed,
        max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", path="/",
        secure=secure,
    )
    return {"username": body.username}


@router.post("/auth/logout")
async def logout(
    request: Request,
    response: Response,
    session: dict = Depends(current_session),
):
    get_sessions(request).drop(request)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/auth/me")
async def me(session: dict = Depends(current_session)):
    return {"username": session["username"]}


@router.get("/share")
async def share_info(request: Request, _: dict = Depends(current_session)):
    """SFTP 连接信息（主机名取用户实际访问管理页所用的地址）。

    账号密码与网页登录相同，随接口返回仅为方便用户在客户端填写；
    该接口本身要求已登录会话才能访问。
    """
    host = request.headers.get("host", "").split(":")[0] or "localhost"
    settings = request.app.state.settings
    port = config.SFTP_HOST_PORT
    return {
        "scheme": "sftp",
        "host": host,
        "port": port,
        "username": settings.admin_user,
        "password": settings.admin_password,
        "uri": f"sftp://{settings.admin_user}@{host}:{port}/",
    }


# ---------------------------------------------------------------- 存储卷


def _require_external_key(key: str) -> None:
    """解锁/挂载/弹出/凭据/SMART 等操作仅服务外接卷。"""
    if key.startswith(("local:", "remote:")):
        raise HTTPException(status_code=400, detail="该存储不支持此操作")


@router.get("/volumes")
async def list_volumes(request: Request, _: dict = Depends(current_session)):
    """三类存储快照：local / disks / remote + rememberEnabled。"""
    return get_registry(request).snapshot()


@router.get("/speeds")
async def volume_speeds(request: Request, _: dict = Depends(current_session)):
    """各已挂载卷底层硬盘的实时吞吐（字节/秒）：rx=读取，tx=写入。"""
    return request.app.state.speeds.rates()


@router.get("/usage")
async def volume_usage(request: Request, _: dict = Depends(current_session)):
    """各已挂载卷容量（字节）：total/used/avail。"""
    return await get_registry(request).usage()


@router.get("/sessions")
async def sftp_sessions(request: Request, _: dict = Depends(current_session)):
    """当前 SFTP 连接数、登录用户与各卷打开中的文件。"""
    return request.app.state.speeds.session_info()


def _busy_detail(request: Request, keys: list[str], message: str) -> str:
    """卸载失败（busy）时把占用文件拼进错误文案。"""
    names: list[str] = []
    seen: set[str] = set()
    for key in keys:
        for row in request.app.state.speeds.open_files_for(key):
            label = f"{row['user']}:{row['path'] or '/'}"
            if label not in seen:
                seen.add(label)
                names.append(label)
    if names:
        shown = "、".join(names[:8])
        if len(names) > 8:
            shown += f" 等 {len(names)} 个"
        return f"{message}（仍被占用：{shown}）"
    return message


@router.post("/rescan")
async def rescan(request: Request, _: dict = Depends(current_session)):
    asyncio.create_task(get_manager(request).rescan("manual"))
    return {"ok": True}


@router.post("/volumes/{key}/unlock")
async def unlock_volume(
    key: str,
    body: UnlockBody,
    request: Request,
    session: dict = Depends(current_session),
):
    _require_external_key(key)
    manager = get_manager(request)
    try:
        await manager.unlock_volume(
            key=key,
            kind=body.kind,
            secret=body.secret,
            writable=body.writable,
            remember=body.remember,
            actor=session["username"],
        )
    except VolumeNotFound as exc:
        raise HTTPException(status_code=404, detail="卷不存在或已拔出") from exc
    except MountError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/volumes/{key}/mount")
async def mount_volume(
    key: str,
    body: MountBody,
    request: Request,
    session: dict = Depends(current_session),
):
    _require_external_key(key)
    manager = get_manager(request)
    try:
        await manager.mount_plain(key, writable=body.writable, actor=session["username"])
    except VolumeNotFound as exc:
        raise HTTPException(status_code=404, detail="卷不存在或已拔出") from exc
    except MountError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/volumes/{key}/eject")
async def eject_volume(
    key: str,
    request: Request,
    session: dict = Depends(current_session),
):
    _require_external_key(key)
    manager = get_manager(request)
    from app.transfers.api import _ensure_not_transferring
    _ensure_not_transferring(request, key)
    try:
        await manager.eject_volume(key, actor=session["username"])
    except VolumeNotFound as exc:
        raise HTTPException(status_code=404, detail="卷不存在或已拔出") from exc
    except MountError as exc:
        raise HTTPException(
            status_code=409, detail=_busy_detail(request, [key], str(exc))
        ) from exc
    return {"ok": True, "safeToRemove": True}


@router.post("/disks/{disk_id}/eject")
async def eject_disk(
    disk_id: str,
    request: Request,
    session: dict = Depends(current_session),
):
    manager = get_manager(request)
    from app.transfers.api import _ensure_not_transferring
    keys = [
        part["key"]
        for disk in manager.snapshot().get("disks", [])
        if disk.get("id") == disk_id
        for part in disk.get("partitions", [])
    ]
    for key in keys:
        _ensure_not_transferring(request, key)
    try:
        count = await manager.eject_disk(disk_id, actor=session["username"])
    except VolumeNotFound as exc:
        raise HTTPException(status_code=404, detail="磁盘不存在或已拔出") from exc
    except MountError as exc:
        raise HTTPException(
            status_code=409, detail=_busy_detail(request, keys, str(exc))
        ) from exc
    return {"ok": True, "unmounted": count, "safeToRemove": True}


@router.delete("/volumes/{key}/credential")
async def forget_credential(
    key: str,
    request: Request,
    _: dict = Depends(current_session),
):
    _require_external_key(key)
    await get_manager(request).forget(key)
    return {"ok": True}


@router.get("/disks/{disk_id}/smart")
async def disk_smart(
    disk_id: str,
    request: Request,
    force: bool = False,
    _: dict = Depends(current_session),
):
    """整盘 SMART 健康信息（10 分钟缓存；force=1 立即重查）。

    USB 硬盘盒不支持 SAT 透传时返回 status=unavailable，前端不展示。
    """
    for disk in get_manager(request).snapshot().get("disks", []):
        if disk.get("id") == disk_id:
            return await query_smart(disk["path"], force=force)
    raise HTTPException(status_code=404, detail="磁盘不存在或已拔出")


# ---------------------------------------------------------------- 远程存储


def _remote_http_error(exc: RemoteStoreError) -> HTTPException:
    if isinstance(exc, RemoteStoreExists):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RemoteStoreNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/remote-stores")
async def create_remote_store(
    body: RemoteStoreBody,
    request: Request,
    session: dict = Depends(current_session),
):
    """新增远程 SFTP 存储并立即挂载；凭据加密保存，重启后自动重连。"""
    remote = get_remote(request)
    secret = body.password if body.authMode == "password" else body.privateKey
    try:
        store = await remote.create(
            group=body.group,
            host=body.host,
            port=body.port,
            username=body.username,
            remote_path=body.remotePath,
            auth_mode=body.authMode,
            secret=secret,
        )
    except RemoteStoreError as exc:
        raise _remote_http_error(exc) from exc
    log.info("%s 添加远程存储 %s", session["username"], store.get("name"))
    return {"ok": True, "store": store}


@router.post("/remote-stores/{key}/reconnect")
async def reconnect_remote_store(
    key: str,
    request: Request,
    session: dict = Depends(current_session),
):
    remote = get_remote(request)
    try:
        store = await remote.reconnect(key)
    except RemoteStoreError as exc:
        raise _remote_http_error(exc) from exc
    log.info("%s 重新连接远程存储 %s", session["username"], store.get("name"))
    return {"ok": True, "store": store}


@router.delete("/remote-stores/{key}")
async def delete_remote_store(
    key: str,
    request: Request,
    session: dict = Depends(current_session),
):
    from app.transfers.api import _ensure_not_transferring
    _ensure_not_transferring(request, key)
    remote = get_remote(request)
    try:
        await remote.delete(key)
    except RemoteStoreError as exc:
        raise _remote_http_error(exc) from exc
    log.info("%s 删除远程存储 %s", session["username"], key)
    return {"ok": True}


# ---------------------------------------------------------------- SSE


@router.get("/events")
async def events(request: Request, _: dict = Depends(current_session)):
    bus = request.app.state.bus
    queue = bus.subscribe()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=20)
                    yield f"event: {message['type']}\ndata: {json.dumps(message['data'])}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
