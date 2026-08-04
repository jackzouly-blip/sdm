"""网盘数据管理路由：分享源 CRUD + 手动同步 + 文件清单。

权限模型：分享源按属主隔离，普通用户只能看/改自己的；管理员可见全部
（便于排障与中转区清理）。所有集群落点都必须落在 fs_roots 白名单内，
由 normalize_under_roots 强制——这条不能靠前端把关。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from ..auth.session import current_user
from ..config import get_settings
from ..fs.browser import FsError, normalize_under_roots
from ..logger import get_logger
from .sync_db import NetdiskSyncDB, file_to_dict, share_to_dict

log = get_logger(__name__)

router = APIRouter(prefix="/netdisk", tags=["netdisk"])


def _db(request: Request) -> NetdiskSyncDB:
    db = getattr(request.app.state, "netdisk_sync_db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="网盘同步库未初始化")
    return db


def _own(request: Request, share_id: int, user: str):
    """取分享源并校验访问权；管理员可访问全部。"""
    row = _db(request).get(share_id)
    if row is None:
        raise HTTPException(status_code=404, detail="分享源不存在")
    if row["owner"] != user and not get_settings().is_admin(user):
        raise HTTPException(status_code=403, detail="无权访问该分享源")
    return row


def _validate_local_dir(path: str) -> str:
    try:
        return normalize_under_roots(path, get_settings().fs_root_list)
    except FsError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


# --- 模型 ---------------------------------------------------------------

class ShareIn(BaseModel):
    name: str
    share_url: str
    pwd: str = ""
    sub_dir: str = ""
    # 留空则派生 <inbox 根>/<属主>/<源名>，用户不必知道集群绝对路径
    local_dir: str = ""
    enabled: bool = True
    poll_interval: int = 0  # 秒；0 = 仅手动


class SharePatch(BaseModel):
    name: Optional[str] = None
    share_url: Optional[str] = None
    pwd: Optional[str] = None
    sub_dir: Optional[str] = None
    local_dir: Optional[str] = None
    enabled: Optional[bool] = None
    poll_interval: Optional[int] = None


class PreviewIn(BaseModel):
    share_url: str
    pwd: str = ""
    sub_dir: str = ""


# --- 功能可用性 ---------------------------------------------------------

@router.get("/status")
def netdisk_status(request: Request, user: str = Depends(current_user)) -> dict:
    """入站同步是否可用 + 落点根，供前端决定是否显示模块与提示配置缺失。"""
    s = get_settings()
    cred = _db(request).credentials_status()
    return {
        "ready": cred["configured"],
        "inbox_base": s.netdisk_inbox_base_dir,
        "poll_enabled": s.netdisk_pull_enabled,
        "default_interval": s.netdisk_pull_interval,
        "is_admin": s.is_admin(user),
        "credentials": cred,  # 不含凭据本身，只有状态
    }


# --- 平台凭据（仅管理员）------------------------------------------------

class CredentialsIn(BaseModel):
    bduss: str
    stoken: str = ""


def _require_admin(user: str) -> None:
    if not get_settings().is_admin(user):
        raise HTTPException(status_code=403, detail="无权管理网盘凭据")


@router.put("/credentials")
def set_credentials(
    req: CredentialsIn,
    request: Request,
    user: str = Depends(current_user),
) -> dict:
    """配置平台账号的网页 cookie。

    存库而非 env：BDUSS 会过期需定期轮换，改 env 要 ssh 上生产 + 重启服务，
    重启会打断正在跑的同步任务与在线终端会话。存库改完立即生效。
    """
    _require_admin(user)
    bduss = req.bduss.strip()
    if not bduss:
        raise HTTPException(status_code=400, detail="BDUSS 不能为空")
    db = _db(request)
    db.set_credentials(bduss, req.stoken.strip(), user)
    log.info("管理员 %s 更新了网盘平台凭据", user)  # 只记事件，绝不记凭据本身
    return db.credentials_status()


@router.delete("/credentials")
def clear_credentials(request: Request, user: str = Depends(current_user)) -> dict:
    _require_admin(user)
    db = _db(request)
    db.clear_credentials(user)
    log.info("管理员 %s 清除了网盘平台凭据", user)
    return db.credentials_status()


@router.post("/credentials/test")
def test_credentials(request: Request, user: str = Depends(current_user)) -> dict:
    """当场验证凭据是否有效，并把结论回写状态。"""
    _require_admin(user)
    from .share_client import BaiduShareClient, ShareError

    db = _db(request)
    bduss, stoken = db.resolve_credentials()
    if not bduss:
        raise HTTPException(status_code=400, detail="尚未配置凭据")
    try:
        with BaiduShareClient(bduss, stoken) as cli:
            info = cli.check()
    except ShareError as e:
        db.mark_credential_state("auth_failed")
        return {"ok": False, "detail": f"凭据无效或已过期（errno={e.errno}）",
                **db.credentials_status()}
    except Exception as e:  # noqa: BLE001
        log.exception("网盘凭据自检异常")
        return {"ok": False, "detail": f"自检失败: {e}"[:200],
                **db.credentials_status()}
    db.mark_credential_state("ok", info.get("username", ""))
    return {"ok": True, "detail": "凭据有效", **db.credentials_status()}


# --- 新建向导：先验链接再落库 -------------------------------------------

@router.post("/preview")
def netdisk_preview(
    req: PreviewIn, request: Request, user: str = Depends(current_user)
) -> dict:
    """校验分享链接与提取码，返回该层目录内容，供用户挑子目录。

    这一步的价值是**让用户在落库前就知道链接对不对**——否则错误的链接会变成
    一个永远同步失败的源，用户还得回头猜是哪儿错了。
    """
    from .share_client import BaiduShareClient, ShareError

    db = _db(request)
    bduss, stoken = db.resolve_credentials()
    if not bduss:
        raise HTTPException(status_code=503, detail="平台未配置网盘凭据，请联系运维")
    try:
        with BaiduShareClient(bduss, stoken) as cli:
            sess = cli.open_share(req.share_url, req.pwd)
            entries = cli.list_share(sess, req.sub_dir)
    except ShareError as e:
        if e.is_auth_failure:
            db.mark_credential_state("auth_failed")
            raise HTTPException(status_code=503, detail="平台网盘凭据已失效，请联系运维")
        if e.is_link_invalid:
            raise HTTPException(status_code=400, detail="链接或提取码无效，或分享已取消")
        raise HTTPException(status_code=502, detail=f"网盘接口错误 errno={e.errno}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    from .share_client import is_dir

    # isdir 必须用 is_dir() 归一：根目录返回字符串 "0"/"1"，子目录返回整数，
    # 直接取真值会把根目录下的文件显示成目录（见 share_client.is_dir 注释）
    items = [
        {
            "fs_id": str(e.get("fs_id")),
            "name": e.get("server_filename") or "",
            "path": e.get("path") or "",
            "isdir": is_dir(e),
            "size": int(e.get("size", 0) or 0),
        }
        for e in entries
    ]
    return {
        "sub_dir": req.sub_dir,
        "items": items,
        "file_count": sum(1 for i in items if not i["isdir"]),
        "total_bytes": sum(i["size"] for i in items if not i["isdir"]),
    }


# --- 分享源 CRUD --------------------------------------------------------

@router.get("/shares")
def list_shares(request: Request, user: str = Depends(current_user)) -> List[dict]:
    db = _db(request)
    rows = db.list_all() if get_settings().is_admin(user) else db.list_by_owner(user)
    return [share_to_dict(r, db.counts(r["id"])) for r in rows]


@router.post("/shares", status_code=status.HTTP_201_CREATED)
def create_share(req: ShareIn, request: Request, user: str = Depends(current_user)) -> dict:
    from .puller import local_dir_for

    if not req.name.strip():
        raise HTTPException(status_code=400, detail="请填写名称")
    if not req.share_url.strip():
        raise HTTPException(status_code=400, detail="请填写分享链接")

    local_dir = req.local_dir.strip() or local_dir_for(user, req.name.strip())
    local_dir = _validate_local_dir(local_dir)

    db = _db(request)
    row = db.create(user, {
        "name": req.name.strip(),
        "share_url": req.share_url.strip(),
        "pwd": req.pwd.strip(),
        "sub_dir": req.sub_dir.strip(),
        "local_dir": local_dir,
        "enabled": req.enabled,
        "poll_interval": max(0, req.poll_interval),
    })
    log.info("用户 %s 新建网盘分享源 %s → %s", user, row["id"], local_dir)
    return share_to_dict(row, {})


@router.patch("/shares/{share_id}")
def patch_share(
    share_id: int,
    req: SharePatch,
    request: Request,
    user: str = Depends(current_user),
) -> dict:
    _own(request, share_id, user)
    data = req.model_dump(exclude_unset=True)
    if "local_dir" in data and data["local_dir"]:
        data["local_dir"] = _validate_local_dir(data["local_dir"].strip())
    if "poll_interval" in data and data["poll_interval"] is not None:
        data["poll_interval"] = max(0, int(data["poll_interval"]))
    db = _db(request)
    row = db.update(share_id, data)
    return share_to_dict(row, db.counts(share_id))


@router.delete("/shares/{share_id}")
def delete_share(share_id: int, request: Request, user: str = Depends(current_user)) -> dict:
    _own(request, share_id, user)
    _db(request).delete(share_id)
    # 已下载到集群的文件保留——删的是同步配置，不是用户数据
    return {"ok": True}


# --- 同步 ---------------------------------------------------------------

@router.post("/shares/{share_id}/sync")
def sync_share(share_id: int, request: Request, user: str = Depends(current_user)) -> dict:
    """手动触发一次同步，返回 task_id（进度走 /ws/tasks，与其他任务同构）。"""
    row = _own(request, share_id, user)
    db = _db(request)
    if not db.resolve_credentials()[0]:
        raise HTTPException(status_code=503, detail="平台未配置网盘凭据，请联系运维")

    if not db.try_begin_sync(share_id):
        raise HTTPException(status_code=409, detail="该分享源正在同步中")

    tm = getattr(request.app.state, "task_manager", None)
    if tm is None:
        db.end_sync(share_id, "failed", "任务管理器不可用")
        raise HTTPException(status_code=503, detail="任务管理器不可用")
    try:
        task_id = tm.submit("netdisk_pull", owner=row["owner"],
                            params={"share_id": share_id})
    except Exception as e:  # noqa: BLE001
        db.end_sync(share_id, "failed", f"派发失败: {e}"[:300])
        raise HTTPException(status_code=500, detail="同步任务派发失败")
    db.set_task(share_id, task_id)
    return {"task_id": task_id}


@router.post("/shares/{share_id}/files/{fs_id}/resync")
def resync_file(
    share_id: int,
    fs_id: str,
    request: Request,
    user: str = Depends(current_user),
) -> dict:
    """重新拉取单个文件并覆盖本地，返回 task_id。

    比整源同步快得多——只列该文件所在的一层目录，不做全量递归遍历。
    会按文件名重新定位：客户在网盘上换了新版本时 fs_id 会变，这里能跟上。
    """
    row = _own(request, share_id, user)
    db = _db(request)
    if not db.resolve_credentials()[0]:
        raise HTTPException(status_code=503, detail="平台未配置网盘凭据，请联系运维")
    if db.get_file(share_id, fs_id) is None:
        raise HTTPException(status_code=404, detail="该文件不在同步清单中")

    tm = getattr(request.app.state, "task_manager", None)
    if tm is None:
        raise HTTPException(status_code=503, detail="任务管理器不可用")
    task_id = tm.submit("netdisk_resync_one", owner=row["owner"],
                        params={"share_id": share_id, "fs_id": fs_id})
    return {"task_id": task_id}


@router.get("/shares/{share_id}/files")
def list_share_files(
    share_id: int,
    request: Request,
    state: Optional[str] = Query(None),
    user: str = Depends(current_user),
) -> List[dict]:
    _own(request, share_id, user)
    return [file_to_dict(r) for r in _db(request).list_files(share_id, state)]


@router.get("/shares/{share_id}/batches")
def list_batches(share_id: int, request: Request, user: str = Depends(current_user)) -> List[dict]:
    """中转区批次汇总，供人工清理决策（哪些批次已全部下载完成、占多少）。"""
    from .puller import remote_batch_dir

    row = _own(request, share_id, user)
    out = []
    for b in _db(request).batches(share_id):
        out.append({
            "batch_id": b["batch_id"],
            "files": b["files"],
            "bytes": b["bytes"] or 0,
            "done": b["done"],
            "complete": b["done"] == b["files"],
            "remote_dir": remote_batch_dir(row["owner"], share_id, b["batch_id"]),
        })
    return out
