"""d3plot 网页可视化路由：查找 / 准备解析 / 资产服务。"""
from __future__ import annotations

import os
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..auth.session import current_user
from ..config import get_settings
from ..fs.browser import FsError, list_dir, stat_path
from .service import asset_dir, cache_key

# 导入 service 触发任务注册
from . import service  # noqa: F401

router = APIRouter(prefix="/d3plot", tags=["d3plot"])


def _roots() -> List[str]:
    return get_settings().fs_root_list


def _fs_user(user: str) -> str:
    """管理员的解析/读取以 root 执行（可处理任意用户作业的 d3plot）；普通用户降权到自身。
    缓存命名空间、任务属主、资产服务三处须用同一身份以保持一致。"""
    return "root" if get_settings().is_admin(user) else user


# d3plot 家族文件：主文件 d3plot 及状态文件 d3plot01.. / d3plotaa..
_FAMILY_RE = re.compile(r"^d3plot([0-9]+|[a-z]+)?$")


def _family_sig(user: str, d3path: str) -> str:
    """统计 d3plot 家族(主文件+各 state 文件)的签名：数量+总大小+最新 mtime。
    仿真推进新增 state 文件会改变该签名→缓存键变化→自动重解析。出错返回空串(回退旧行为)。"""
    try:
        res = list_dir(user, os.path.dirname(d3path), _roots())
    except Exception:  # noqa: BLE001
        return ""
    n = 0
    total = 0
    mx = 0.0
    for e in res["entries"]:
        if not e["is_dir"] and _FAMILY_RE.match(e["name"]):
            n += 1
            total += int(e["size"])
            mx = max(mx, float(e["mtime"]))
    return f"{n}:{total}:{int(mx)}"


def _handle(fn, *args):
    try:
        return fn(*args)
    except FsError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="路径不存在")
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权访问")


@router.get("/find")
def d3plot_find(
    dir: str = Query(..., description="任务工作目录绝对路径"),
    user: str = Depends(current_user),
) -> dict:
    """在目录中查找 d3plot 主文件（lasso 会自动读取 d3plot01.. 家族）。"""
    result = _handle(list_dir, _fs_user(user), dir, _roots())
    base = result["path"].rstrip("/")
    files = {e["name"]: e for e in result["entries"] if not e["is_dir"]}
    found = []
    if "d3plot" in files:
        found.append({"path": f"{base}/d3plot", "size": files["d3plot"]["size"]})
    n_family = sum(1 for n in files if n.startswith("d3plot") and n != "d3plot")
    return {"dir": base, "found": found, "n_state_files": n_family}


class PrepareIn(BaseModel):
    path: str  # d3plot 主文件绝对路径
    max_states: Optional[int] = None  # 读取帧数上限(0=全部)；留空用服务端默认
    max_tris: Optional[int] = None  # 三角面预算(0=不减面/高精度)；留空用服务端默认


@router.post("/prepare")
def d3plot_prepare(
    body: PrepareIn,
    request: Request,
    user: str = Depends(current_user),
) -> dict:
    """校验 d3plot，命中缓存则直接就绪，否则提交解析任务。"""
    eff = _fs_user(user)
    info = _handle(stat_path, eff, body.path, _roots())
    if info["is_dir"]:
        raise HTTPException(status_code=400, detail="请选择 d3plot 文件而非目录")
    # 读取帧数 / 精度(三角面预算)：用户指定优先，否则用服务端默认
    s = get_settings()
    ms = body.max_states if body.max_states is not None else s.d3plot_max_states
    mt = body.max_tris if body.max_tris is not None else s.d3plot_max_tris
    # 纳入家族签名 + 帧数 + 精度：新增 state 文件 / 换帧数 / 换精度都使键变化(各自独立缓存)
    sig = f"{_family_sig(eff, info['path'])}|ms{ms}|mt{mt}"
    key = cache_key(info["realpath"], info["mtime"], info["size"], sig)
    if (asset_dir(eff, key) / "model.json").exists():
        return {"ready": True, "key": key}
    tm = request.app.state.task_manager
    # 任务属主=真实登录用户(供进度 WS 鉴权/任务列表)；执行身份用 run_as 单独传(管理员=root)
    task_id = tm.submit("d3plot_view", user, {"d3plot": info["path"], "key": key, "run_as": eff, "max_states": ms, "max_tris": mt})
    return {"ready": False, "key": key, "task_id": task_id}


@router.get("/task/{task_id}")
def d3plot_task(
    task_id: str,
    request: Request,
    user: str = Depends(current_user),
) -> dict:
    """查询解析任务进度（仅任务属主可见）。供前端轮询显示解析进度。"""
    tm = request.app.state.task_manager
    snap = tm.snapshot(task_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if snap["owner"] != user:
        raise HTTPException(status_code=403, detail="无权查看该任务")
    return {k: snap.get(k) for k in ("status", "phase", "progress", "error")}


@router.get("/asset/{key}/{name}")
def d3plot_asset(
    key: str,
    name: str,
    user: str = Depends(current_user),
):
    """提供缓存的解析产物（仅本人命名空间）。"""
    import re
    if name not in ("model.json", "model.bin") and not re.fullmatch(r"field_\d+\.bin", name):
        raise HTTPException(status_code=404, detail="未知资产")
    if "/" in key or ".." in key:
        raise HTTPException(status_code=400, detail="非法 key")
    fp = asset_dir(_fs_user(user), key) / name
    if not fp.exists():
        raise HTTPException(status_code=404, detail="资产不存在，请先解析")
    media = "application/json" if name.endswith(".json") else "application/octet-stream"
    return FileResponse(str(fp), media_type=media)
