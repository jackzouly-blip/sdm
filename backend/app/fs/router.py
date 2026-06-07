"""文件浏览路由：列目录 / 预览 / 下载。均以登录用户身份执行。"""
from __future__ import annotations

import os
import shutil
import time
import uuid
from typing import List, Optional

from starlette.background import BackgroundTask
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..auth.session import current_user, current_user_query
from ..config import get_settings
from ..packaging.service import _common_base, _make_scratch, _validate_paths
from ..privilege.actas import (
    call_as_user,
    run_as_user,
    stream_tar_as_user,
)
from .browser import (
    FsError,
    delete_path,
    find_files,
    list_dir,
    make_dir,
    path_size,
    read_text,
    rename_path,
    stat_path,
    write_file,
)

router = APIRouter(prefix="/fs", tags=["fs"])


def _roots() -> List[str]:
    return get_settings().fs_root_list


def _fs_user(user: str) -> str:
    """文件操作的实际执行身份：管理员以 root 执行（可进所有 FS_ROOTS 下目录，
    便于配置提取规则/核对产物）；普通用户降权到自身，维持权限隔离。
    路径白名单(_roots)与身份无关，管理员同样受 FS_ROOTS 约束。"""
    return "root" if get_settings().is_admin(user) else user


def _handle(fn, *args):
    try:
        return fn(*args)
    except FsError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权访问该路径")
    except FileExistsError:
        raise HTTPException(status_code=409, detail="同名目录或文件已存在")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="路径不存在")
    except NotADirectoryError:
        raise HTTPException(status_code=400, detail="不是目录")
    except IsADirectoryError:
        raise HTTPException(status_code=400, detail="目标是目录，无法预览/下载")


class FsEntry(BaseModel):
    name: str
    is_dir: bool
    is_link: bool
    size: int
    mtime: float
    mode: str


class ListResponse(BaseModel):
    path: str
    roots: List[str]
    entries: List[FsEntry]


@router.get("/roots", response_model=List[str])
def fs_roots(user: str = Depends(current_user)) -> List[str]:
    """返回允许浏览的根白名单，供前端作为文件浏览入口。"""
    return _roots()


@router.get("/list", response_model=ListResponse)
def fs_list(
    path: str = Query(..., description="绝对路径，须在允许的根白名单内"),
    user: str = Depends(current_user),
) -> ListResponse:
    result = _handle(list_dir, _fs_user(user), path, _roots())
    return ListResponse(
        path=result["path"], roots=_roots(), entries=result["entries"]
    )


# --- 写操作：新建目录 / 上传文件（均以登录用户身份执行）-----------------

class MkdirRequest(BaseModel):
    parent: str  # 父目录绝对路径（须在白名单根内）
    name: str  # 新目录名（单段，不含 /）


class PathResponse(BaseModel):
    path: str


class RenameRequest(BaseModel):
    path: str  # 待改名的文件/目录绝对路径
    new_name: str  # 新名称（单段，不含 /，同目录内）


@router.post("/mkdir", response_model=PathResponse)
def fs_mkdir(
    req: MkdirRequest,
    user: str = Depends(current_user),
) -> PathResponse:
    result = _handle(make_dir, _fs_user(user), req.parent, req.name, _roots())
    return PathResponse(path=result["path"])


@router.post("/rename", response_model=PathResponse)
def fs_rename(
    req: RenameRequest,
    user: str = Depends(current_user),
) -> PathResponse:
    result = _handle(rename_path, _fs_user(user), req.path, req.new_name, _roots())
    return PathResponse(path=result["path"])


@router.delete("/delete")
def fs_delete(
    path: str = Query(...),
    user: str = Depends(current_user),
):
    """删除文件或目录（目录递归）。以登录身份执行，禁止删除白名单根。"""
    _handle(delete_path, _fs_user(user), path, _roots())
    return {"ok": True}


@router.get("/find-files", response_model=List[str])
def fs_find_files(
    dir: str = Query(...),
    exts: str = Query("k,key", description="逗号分隔的扩展名，如 k,key"),
    user: str = Depends(current_user),
) -> List[str]:
    """在目录下递归查找指定扩展名的文件，返回相对路径（供提交作业选输入文件）。"""
    ext_tuple = tuple(
        "." + e.strip().lstrip(".").lower() for e in exts.split(",") if e.strip()
    )
    return _handle(find_files, _fs_user(user), dir, ext_tuple, _roots())


@router.post("/upload", response_model=PathResponse)
async def fs_upload(
    parent: str = Form(..., description="目标目录绝对路径"),
    file: UploadFile = File(...),
    user: str = Depends(current_user),
) -> PathResponse:
    data = await file.read()
    result = _handle(write_file, _fs_user(user), parent, file.filename, data, _roots())
    return PathResponse(path=result["path"])


class ArchiveRequest(BaseModel):
    paths: List[str]
    archive: Optional[str] = None


@router.post("/archive-estimate")
def fs_archive_estimate(req: ArchiveRequest, user: str = Depends(current_user)):
    """估算选中项打包总大小与分卷数(供打包对话框预显)。"""
    import math

    eff = _fs_user(user)
    roots = _roots()
    total = 0
    for p in req.paths:
        try:
            total += path_size(eff, p, roots)
        except FsError:
            pass
    vb = get_settings().package_volume_bytes
    est = max(1, math.ceil(total / vb)) if total else 0
    return {"total_bytes": total, "volume_bytes": vb, "est_volumes": est}


@router.post("/download-archive")
def fs_download_archive(req: ArchiveRequest, user: str = Depends(current_user)):
    """把选中的文件/目录以用户身份打成标准 ZIP，直接流式回传给浏览器下载。
    适合小批量直接取走，免走网盘。"""
    roots = _roots()
    eff = _fs_user(user)
    paths = _handle(_validate_paths, eff, req.paths, roots)
    archive = req.archive or f"download_{int(time.time())}.zip"
    if "/" in archive or archive in (".", ".."):
        raise HTTPException(status_code=400, detail="非法归档文件名")
    # 兼容旧的 .tar.gz 命名 → 一律改 .zip
    for suf in (".tar.gz", ".tgz"):
        if archive.lower().endswith(suf):
            archive = archive[: -len(suf)]
            break
    if not archive.lower().endswith(".zip"):
        archive += ".zip"

    task_dir = _make_scratch(eff, "dl_" + uuid.uuid4().hex)
    archive_path = os.path.join(task_dir, archive)
    base = _common_base(paths)
    rel = [os.path.relpath(p, base) for p in paths]
    zip_bin = shutil.which("zip") or "zip"
    # cwd=base 使压缩包内为相对路径；-r 递归、-q 安静
    proc = run_as_user(eff, [zip_bin, "-r", "-q", archive_path, *rel], cwd=base, timeout=1800)
    if proc.returncode != 0 or not os.path.exists(archive_path):
        shutil.rmtree(task_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="打包失败: " + proc.stderr.decode("utf-8", "replace")[:300])
    # 传完即清理临时目录
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=archive,
        background=BackgroundTask(shutil.rmtree, task_dir, ignore_errors=True),
    )


@router.get("/download-archive-stream")
def fs_download_archive_stream(
    paths: List[str] = Query(..., description="要打包的绝对路径(可重复)"),
    name: str = Query("download", description="归档名(不含扩展名)"),
    user: str = Depends(current_user_query),  # 允许 query token，供浏览器原生下载
):
    """流式 tar 打包下载：以用户身份 `tar -cf -` 边打边传，**不在服务器落临时包**，
    下载即刻开始。适合大文件/大目录(方案 B)。store 模式无压缩，传输最快。"""
    roots = _roots()
    eff = _fs_user(user)
    vpaths = _handle(_validate_paths, eff, paths, roots)
    if not vpaths:
        raise HTTPException(status_code=400, detail="无有效路径")
    base = _common_base(vpaths)
    rel = [os.path.relpath(p, base) for p in vpaths]
    safe = (name or "download").replace("/", "_").strip() or "download"
    if not safe.lower().endswith(".tar"):
        safe += ".tar"
    headers = {"Content-Disposition": f'attachment; filename="{safe}"'}
    return StreamingResponse(
        stream_tar_as_user(eff, base, rel),
        media_type="application/x-tar",
        headers=headers,
    )


@router.get("/office-pdf")
def fs_office_pdf(path: str = Query(...), user: str = Depends(current_user)):
    """Office 文档(doc/docx/xls/xlsx/ppt/pptx)用 LibreOffice 转 PDF 后内嵌预览，结果缓存。"""
    import hashlib

    s = get_settings()
    eff = _fs_user(user)
    info = _handle(stat_path, eff, path, _roots())
    if info["is_dir"]:
        raise HTTPException(status_code=400, detail="目标是目录")
    key = hashlib.sha1(f'{info["realpath"]}|{int(info["mtime"])}|{int(info["size"])}'.encode()).hexdigest()[:16]
    cache_dir = os.path.join(s.office_cache_dir, eff, key)
    pdf_cache = os.path.join(cache_dir, "doc.pdf")
    if not os.path.exists(pdf_cache):
        soffice = s.office_soffice or shutil.which("soffice") or shutil.which("libreoffice") or "soffice"
        scratch = _make_scratch(eff, "office_" + uuid.uuid4().hex)
        cmd = [
            soffice, "--headless", "--norestore", "--convert-to", "pdf",
            "--outdir", scratch,
            "-env:UserInstallation=file://" + os.path.join(scratch, "lo_profile"),
            info["path"],
        ]
        proc = run_as_user(eff, cmd, timeout=s.office_timeout)
        stem = os.path.splitext(os.path.basename(info["path"]))[0]
        out_pdf = os.path.join(scratch, stem + ".pdf")
        if proc.returncode != 0 or not os.path.exists(out_pdf):
            shutil.rmtree(scratch, ignore_errors=True)
            raise HTTPException(status_code=500, detail="Office 转换失败（服务器需安装 LibreOffice）")
        os.makedirs(cache_dir, exist_ok=True)
        shutil.move(out_pdf, pdf_cache)
        shutil.rmtree(scratch, ignore_errors=True)
    return FileResponse(pdf_cache, media_type="application/pdf", filename="preview.pdf")


class PreviewResponse(BaseModel):
    path: str
    size: int
    truncated: bool
    binary: bool
    content: str


@router.get("/preview", response_model=PreviewResponse)
def fs_preview(
    path: str = Query(...),
    user: str = Depends(current_user),
) -> PreviewResponse:
    s = get_settings()
    result = _handle(read_text, _fs_user(user), path, _roots(), s.fs_preview_max_bytes)
    return PreviewResponse(
        path=result["path"],
        size=result["size"],
        truncated=result["truncated"],
        binary=result.get("binary", False),
        content=result["content"],
    )


# --- 下载（单次 fork 降权后流式读取，大文件高效）-----------------------

@router.get("/download")
def fs_download(
    path: str = Query(...),
    user: str = Depends(current_user_query),  # 允许 query token，供浏览器原生下载
):
    roots = _roots()
    eff = _fs_user(user)
    # 校验 + 取大小（管理员以 root，普通用户以自身）
    info = _handle(stat_path, eff, path, roots)
    if info["is_dir"]:
        raise HTTPException(status_code=400, detail="目标是目录，请使用打包功能")
    norm = info["path"]
    filename = os.path.basename(norm)

    # 与 d3plot 资源同款：用 FileResponse 连续发送（Starlette 优化、可走 sendfile），
    # 自动设置 Content-Length 并支持 HTTP Range（断点续传 / 下载器多线程）。
    # 自定义流式生成器逐块 yield 在高延迟/丢包网络上会被 chunk 间隙拖低吞吐，故弃用。
    # 权限：路径已由 stat_path(以目标用户身份) 校验在用户可见根内；服务以 root 读取文件。
    return FileResponse(
        norm,
        media_type="application/octet-stream",
        filename=filename,
    )


# --- 目录收藏（按登录账号，跨浏览器随账号）-----------------------------

class FavoriteIn(BaseModel):
    path: str


@router.get("/favorites", response_model=List[str])
def fs_favorites(request: Request, user: str = Depends(current_user)) -> List[str]:
    return request.app.state.favorites_db.list_by_owner(user)


@router.post("/favorites")
def fs_add_favorite(req: FavoriteIn, request: Request, user: str = Depends(current_user)):
    info = _handle(stat_path, _fs_user(user), req.path, _roots())  # 校验在白名单内
    if not info["is_dir"]:
        raise HTTPException(status_code=400, detail="只能收藏目录")
    request.app.state.favorites_db.add(user, info["path"])
    return {"ok": True}


@router.delete("/favorites")
def fs_remove_favorite(
    request: Request, path: str = Query(...), user: str = Depends(current_user)
):
    request.app.state.favorites_db.remove(user, path)
    return {"ok": True}
