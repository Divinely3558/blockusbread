"""后台传输 REST API：创建移动任务、查看任务列表、取消任务。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.models import MountMode
from app.web.api import current_session, get_manager
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
    """卷弹出前调用：有进行中的传输任务则拒绝，避免传输中途挂载消失。"""
    active = get_transfers(request).active_for_key(key)
    if active:
        names = "、".join(j["name"] for j in active[:3])
        more = f" 等 {len(active)} 个" if len(active) > 3 else ""
        raise HTTPException(
            status_code=409,
            detail=f"有文件传输任务正在使用该卷（{names}{more}），请等待完成或取消后再弹出",
        )


@router.post("/volumes/{key}/move")
async def create_move(key: str, body: MoveBody, request: Request):
    manager = get_manager(request)

    # 源卷：已挂载即可（只读卷也能读出）
    try:
        src_part, src_rt = manager.mounted_partition(key)
    except Exception as exc:  # VolumeNotFound
        raise HTTPException(status_code=404, detail="源卷未挂载或已拔出") from exc

    # 目标卷：必须可写
    try:
        dst_part, dst_rt = manager.mounted_partition(body.destKey)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="目标卷未挂载或已拔出") from exc
    if dst_rt.mode != MountMode.RW:
        raise HTTPException(
            status_code=409,
            detail="目标卷以只读模式挂载，不能写入，请用读写模式解锁目标卷",
        )

    _ensure_not_transferring(request, key)
    _ensure_not_transferring(request, body.destKey)

    src_root = Path(src_part.fs_dir)
    dst_root = Path(dst_part.fs_dir)
    src_abs = safe_resolve(src_root, body.path)
    dst_dir = safe_resolve(dst_root, body.destPath)

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

    # 源卷只读 -> 只能复制（无法删除源）；读写源按用户选择 move/copy
    if src_rt.mode == MountMode.RO:
        if body.mode == "move":
            # 前端不应出现此组合，双重保险
            raise HTTPException(
                status_code=409,
                detail="源硬盘为只读挂载，不能移动（删除源），请选择复制",
            )
        op = "copy"
    else:
        op = body.mode

    job = await get_transfers(request).submit(
        src_key=key,
        src_rel=body.path.strip("/"),
        dst_key=body.destKey,
        dst_rel=body.destPath.strip("/"),
        name=name,
        op=op,
        src_abs=src_abs,
        dst_dir_abs=dst_dir,
    )
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
