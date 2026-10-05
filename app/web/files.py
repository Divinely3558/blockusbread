"""网页文件浏览路由：列目录、Range 流式播放/下载，以及读写卷的上传/删除/重命名。

安全要点：
- 仅允许访问当前处于 MOUNTED 状态的卷；卷一旦弹出/拔出，所有文件接口立即 404。
- 所有相对路径经 safe_resolve 解析为绝对路径并校验仍在卷根内，
  挡掉 ../ 穿越与指向卷外的符号链接。
- raw 接口仅对 video/audio/image 使用 inline 展示（浏览器播放器需要），
  其余类型一律 attachment 下载，并统一 nosniff + CSP sandbox，
  防止盘内 HTML/SVG 携带脚本在管理页同源执行。
- 写操作（上传/删除/重命名）仅允许读写模式挂载的卷。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.stores.registry import VolumeNotFound, VolumeRef
from app.web.api import current_session, get_registry

router = APIRouter(prefix="/api/volumes")
log = logging.getLogger("web.files")

_COPY_BUFSIZE = 1024 * 1024
_INLINE_PREFIXES = ("video/", "audio/", "image/")

# 可作为纯文本在网页中预览的扩展名（内容一律按纯文本展示，不渲染 HTML）
_PREVIEW_EXTENSIONS = frozenset({
    "txt", "log", "ini", "inf", "conf", "cfg", "config", "properties", "prop",
    "env", "md", "markdown", "json", "xml", "csv", "tsv", "yml", "yaml", "toml",
    "sh", "bash", "zsh", "bat", "cmd", "ps1", "py", "js", "mjs", "ts", "css",
    "scss", "less", "html", "htm", "svg", "c", "h", "cpp", "cc", "hpp", "java",
    "go", "rs", "rb", "php", "pl", "lua", "sql",
})
_PREVIEW_MAX_BYTES = 1024 * 1024  # 1 MiB，超过请下载后查看


def _decode_text(data: bytes) -> tuple[str, str]:
    """按 UTF-8 → GB18030（中文 Windows 常见 ANSI）顺序解码，返回 (文本, 编码名)。"""
    try:
        return data.decode("utf-8-sig"), "UTF-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030"), "GB18030"
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "UTF-8（含无法识别字符）"


# ---------------------------------------------------------------- 路径安全


def safe_resolve(root: Path, rel: str) -> Path:
    """把卷内相对路径解析为绝对路径，并保证不逃逸出 root。

    rel 为 '' 时返回卷根。任何穿越尝试（..、卷外符号链接）抛 400。
    """
    rel = (rel or "").strip().lstrip("/")
    if "\x00" in rel:
        raise HTTPException(status_code=400, detail="非法路径")
    root_real = root.resolve()
    target = (root_real / rel).resolve() if rel else root_real
    if target != root_real and not target.is_relative_to(root_real):
        log.warning("拒绝越权路径访问：root=%s rel=%r resolved=%s", root, rel, target)
        raise HTTPException(status_code=400, detail="非法路径")
    return target


def sanitize_filename(raw: str) -> str:
    """multipart 文件名只保留最后一级（同时处理 / 与 Windows 的 \\）。"""
    cleaned = (raw or "").replace("\\", "/").split("/")[-1].strip()
    return cleaned


def _volume(request: Request, key: str) -> VolumeRef:
    """取当前可用（已挂载）的卷；不存在/未挂载统一 404。"""
    try:
        return get_registry(request).lookup(key)
    except VolumeNotFound as exc:
        raise HTTPException(status_code=404, detail="存储不存在、未挂载或已拔出") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="存储不存在、未挂载或已拔出") from exc


def _require_writable(ref: VolumeRef) -> None:
    if not ref.writable:
        raise HTTPException(status_code=409, detail="该存储为只读模式，不能写入或删除")


def _fs_root(request: Request, key: str) -> tuple[VolumeRef, Path]:
    ref = _volume(request, key)
    root = ref.fs_dir
    if not root.exists():
        raise HTTPException(status_code=404, detail="挂载点不可用")
    return ref, root


# ---------------------------------------------------------------- 列目录


@router.get("/{key}/browse", dependencies=[Depends(current_session)])
async def browse(key: str, request: Request, path: str = ""):
    ref, root = _fs_root(request, key)
    directory = safe_resolve(root, path)
    if not directory.is_dir():
        raise HTTPException(status_code=400, detail="该路径不是目录")

    entries = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                entries.append({
                    "name": entry.name,
                    "isDir": is_dir,
                    "size": 0 if is_dir else st.st_size,
                    "mtime": int(st.st_mtime),
                })
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"读取目录失败：{exc.strerror or exc}") from exc

    entries.sort(key=lambda e: (not e["isDir"], e["name"].casefold()))

    crumbs = [{"name": "根目录", "path": ""}]
    cur = ""
    for seg in [s for s in path.strip("/").split("/") if s]:
        cur = f"{cur}/{seg}".lstrip("/")
        crumbs.append({"name": seg, "path": cur})

    return {
        "path": path.strip("/"),
        "writable": ref.writable,
        "crumbs": crumbs,
        "entries": entries,
    }


# ---------------------------------------------------------------- 播放 / 下载


@router.get("/{key}/raw", dependencies=[Depends(current_session)])
async def raw_file(
    key: str,
    request: Request,
    path: str = "",
    download: int = 0,
):
    _ref, root = _fs_root(request, key)
    target = safe_resolve(root, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    ctype = (mimetypes.guess_type(target.name)[0] or "application/octet-stream").lower()
    inline = not download and ctype.startswith(_INLINE_PREFIXES)

    try:
        stat_result = os.stat(target, follow_symlinks=True)
    except OSError as exc:
        raise HTTPException(status_code=409, detail="文件暂不可读") from exc

    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
        "Referrer-Policy": "no-referrer",
    }
    return FileResponse(
        path=str(target),
        media_type=ctype,
        filename=target.name,
        stat_result=stat_result,
        content_disposition_type="inline" if inline else "attachment",
        headers=headers,
    )


# ---------------------------------------------------------------- 文本预览


@router.get("/{key}/preview", dependencies=[Depends(current_session)])
async def preview_text(
    key: str,
    request: Request,
    path: str = "",
):
    """读取小型纯文本文件内容供网页预览。

    内容以 JSON 返回、前端按纯文本（textContent）渲染，盘内的 HTML/脚本
    不会被执行；扩展名白名单 + 体积上限 + NUL 字节检测三重限制。
    """
    _ref, root = _fs_root(request, key)
    target = safe_resolve(root, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    ext = target.name.rsplit(".", 1)[-1].lower() if "." in target.name else ""
    if ext not in _PREVIEW_EXTENSIONS:
        raise HTTPException(status_code=415, detail="该文件类型不支持在线预览，请下载后查看")

    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=409, detail="文件暂不可读") from exc
    if size > _PREVIEW_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（{size // 1024} KB），预览仅支持 1 MB 以内文本，请下载后查看",
        )

    try:
        data = await asyncio.to_thread(target.read_bytes)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"读取文件失败：{exc.strerror or exc}") from exc

    if b"\x00" in data:
        raise HTTPException(status_code=415, detail="这是二进制文件，不支持文本预览，请下载后查看")

    content, encoding = await asyncio.to_thread(_decode_text, data)
    return JSONResponse({
        "name": target.name,
        "size": size,
        "encoding": encoding,
        "content": content,
    })


# ---------------------------------------------------------------- 上传


class RenameBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    newName: str = Field(min_length=1, max_length=255)


@router.post("/{key}/upload")
async def upload_entry(
    key: str,
    request: Request,
    path: str = Form(""),
    overwrite: int = Form(0),
    file: UploadFile = File(...),
    session: dict = Depends(current_session),
):
    ref, root = _fs_root(request, key)
    _require_writable(ref)
    directory = safe_resolve(root, path)
    if not directory.is_dir():
        raise HTTPException(status_code=400, detail="目标路径不是目录")

    name = sanitize_filename(file.filename or "")
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="文件名无效")

    dest = directory / name
    replacing = dest.exists() and bool(overwrite)
    if dest.exists() and not overwrite:
        raise HTTPException(status_code=409, detail="已存在同名文件，勾选覆盖后重试")

    tmp = directory / f".{name}.part-{uuid.uuid4().hex}"
    total = 0
    try:
        with open(tmp, "wb") as fh:
            while True:
                chunk = await file.read(_COPY_BUFSIZE)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        if exc.errno == 28:
            raise HTTPException(status_code=409, detail="磁盘剩余空间不足，上传中止") from exc
        raise HTTPException(status_code=409, detail=f"写入失败：{exc.strerror or exc}") from exc
    finally:
        await file.close()

    rel_display = f"{path.strip('/')}/{name}".lstrip("/")
    log.info("%s 上传文件到卷 %s：%s（%d 字节，%s）",
             session["username"], key, rel_display, total,
             "覆盖" if replacing else "新建")
    return {"ok": True, "name": name, "size": total}


# ---------------------------------------------------------------- 删除


@router.delete("/{key}/entry")
async def delete_entry(
    key: str,
    request: Request,
    path: str = "",
    recursive: int = 0,
    session: dict = Depends(current_session),
):
    ref, root = _fs_root(request, key)
    _require_writable(ref)
    if not path.strip():
        raise HTTPException(status_code=400, detail="不能删除卷根目录")
    target = safe_resolve(root, path)
    if not target.exists() and not target.is_symlink():
        raise HTTPException(status_code=404, detail="文件或目录不存在")

    display = path.strip("/")
    try:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            if not recursive:
                raise HTTPException(status_code=409, detail="目标是目录，需确认递归删除")
            shutil.rmtree(target)
        else:
            raise HTTPException(status_code=400, detail="不支持的文件类型")
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"删除失败：{exc.strerror or exc}") from exc

    log.info("%s 从卷 %s 删除：%s", session["username"], key, display)
    return {"ok": True}


# ---------------------------------------------------------------- 重命名


@router.post("/{key}/rename")
async def rename_entry(
    key: str,
    body: RenameBody,
    request: Request,
    session: dict = Depends(current_session),
):
    ref, root = _fs_root(request, key)
    _require_writable(ref)
    root_real = root.resolve()
    source = safe_resolve(root, body.path)
    if not source.exists() and not source.is_symlink():
        raise HTTPException(status_code=404, detail="文件或目录不存在")
    if source == root_real:
        raise HTTPException(status_code=400, detail="不能重命名卷根目录")

    new_name = sanitize_filename(body.newName)
    if not new_name or new_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="新名称无效")

    dest = source.parent / new_name
    if not dest.resolve().is_relative_to(root_real):
        raise HTTPException(status_code=400, detail="非法的新名称")
    if dest.exists() or dest.is_symlink():
        raise HTTPException(status_code=409, detail="已存在同名文件或目录")

    try:
        os.rename(source, dest)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"重命名失败：{exc.strerror or exc}") from exc

    log.info("%s 在卷 %s 重命名：%s -> %s",
             session["username"], key, body.path.strip("/"), new_name)
    return {"ok": True}
