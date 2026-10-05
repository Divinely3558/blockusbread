"""后台传输 REST API：创建移动任务、查看任务列表、取消任务。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.stores.registry import VolumeNotFound, VolumeUnavailable
from app.transfers.jobs import TransferConflict
from app.web.api import current_session, get_registry
from app.web.files import safe_resolve

router = APIRouter(prefix="/api", dependencies=[Depends(current_session)])
log = logging.getLogger("web.transfers")


def get_transfers(request: Request):
    return request.app.state.transfers


class MoveBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    destKey: str = Field(min_length=1, max_length=200)
    destPath: str = Field(default="", max_length=4096)
    # move=移动（复制后删除源，仅读写源可用）；copy=复制（始终保留源）
    mode: str = Field(default="move", pattern="^(move|copy)$")


def _ensure_not_transferring(request: Request, key: str) -> None:
    """卷弹出/删除前调用：有进行中的传输任务则拒绝，避免传输中途挂载消失。"""
    active = get_transfers(request).active_for_key(key)
    if active:
        names = "、".join(j["name"] for j in active[:3])
        more = f" 等 {len(active)} 个" if len(active) > 3 else ""
        raise HTTPException(
            status_code=409,
            detail=f"有文件传输任务正在使用该存储（{names}{more}），请等待完成或取消后再试",
        )


def _lookup(request: Request, key: str, detail: str):
    try:
        return get_registry(request).lookup(key)
    except VolumeNotFound as exc:
        raise HTTPException(status_code=404, detail=detail) from exc
    except VolumeUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/volumes/{key}/move")
async def create_move(key: str, body: MoveBody, request: Request):
    # 源卷：已挂载即可（只读卷也能读出）
    src_ref = _lookup(request, key, "源存储未挂载或已拔出")

    # 目标卷：必须可写
    dst_ref = _lookup(request, body.destKey, "目标存储未挂载或已拔出")
    if not dst_ref.writable:
        raise HTTPException(
            status_code=409,
            detail="目标存储为只读模式，不能写入",
        )

    src_abs = safe_resolve(src_ref.fs_dir, body.path)
    dst_dir = safe_resolve(dst_ref.fs_dir, body.destPath)
    if not src_abs.exists() and not src_abs.is_symlink():
        raise HTTPException(status_code=404, detail="源文件或目录不存在")
    if not dst_dir.is_dir():
        raise HTTPException(status_code=400, detail="目标位置不是有效目录")
    name = src_abs.name
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="源路径无效")

    # 同卷：禁止把目录移入自身或其子孙目录
    if key == body.destKey:
        if src_abs == dst_dir:
            raise HTTPException(status_code=400, detail="目标位置不能与源相同")
        if dst_dir.is_relative_to(src_abs) and src_abs.is_dir():
            raise HTTPException(status_code=400, detail="不能放到该文件夹自己的子文件夹里")

    # 源只读 -> 只能复制（无法删除源）；读写源按用户选择 move/copy
    if not src_ref.writable:
        if body.mode == "move":
            # 前端不应出现此组合，双重保险
            raise HTTPException(
                status_code=409,
                detail="源存储为只读挂载，不能移动（删除源），请选择复制",
            )
        op = "copy"
    else:
        op = body.mode

    try:
        job = await get_transfers(request).submit(
            src_key=key,
            src_rel=body.path.strip("/"),
            dst_key=body.destKey,
            dst_rel=body.destPath.strip("/"),
            name=name,
            op=op,
            src_abs=src_abs,
            dst_dir_abs=dst_dir,
            volume_names={key: src_ref.name, body.destKey: dst_ref.name},
        )
    except TransferConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    log.info("%s 创建%s任务 %s：%s/%s -> %s/%s",
             "后台", "复制" if op == "copy" else "移动", job.id,
             key, body.path, body.destKey, body.destPath or "/")
    return {"ok": True, "job": job.snapshot(), "op": op}


@router.get("/jobs")
async def list_jobs(request: Request):
    return {"jobs": get_transfers(request).snapshot()}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    ok = get_transfers(request).cancel(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已结束，无法取消")
    return {"ok": True}
