"""试算路由：提交（管理节点直接跑）/ 增量看输出 / 中断。

与 PBS 提交并列的一条轻量链路：校验初始目录+输入文件 → 解析属主 → 用
试算模板(kind=trial)渲染命令 → 交给 TrialManager 在管理节点以属主身份直接跑。
与作业提交不同：命令在**管理节点的真实 head 路径**下执行，故 ###init_dir_path###
不做 /data→/caedata 的计算节点映射。
"""
from __future__ import annotations

import os
import pwd

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from ..auth.session import current_user, is_admin_request
from ..config import get_settings
from ..db.jobs_db import JobsDB
from ..fs.browser import FsError, stat_path
from ..logger import get_logger
from ..submit.db import KIND_TRIAL
from ..submit.router import _safe_jobname
from .manager import TrialError

router = APIRouter(prefix="/trials", tags=["trial"])
log = get_logger(__name__)


def _db(request: Request) -> JobsDB:
    return request.app.state.jobs_db


def _roots():
    return get_settings().fs_root_list


def _render_trial(content: str, init_dir: str, input_rel: str) -> str:
    """渲染试算命令：占位符用**真实 head 路径**替换（命令在管理节点执行）。"""
    idir = init_dir if init_dir.endswith("/") else init_dir + "/"
    return (content.replace("###init_dir_path###", idir)
                   .replace("###input_file_path###", input_rel.lstrip("/")))


class TrialSubmitIn(BaseModel):
    name: str
    init_dir: str
    input_file: str      # 相对 init_dir
    template_id: int     # 必须是 kind=trial 的模板


@router.post("/submit")
def submit_trial(
    body: TrialSubmitIn, request: Request,
    user: str = Depends(current_user), is_admin: bool = Depends(is_admin_request),
):
    eff = "root" if is_admin else user  # 读取/校验身份

    # 1) 校验初始目录
    try:
        info = stat_path(eff, body.init_dir, _roots())
    except FsError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    if not info["is_dir"]:
        raise HTTPException(status_code=400, detail="初始路径不是目录")
    init_dir = info["path"]

    # 2) 校验输入文件存在
    rel = body.input_file.lstrip("/")
    if not rel:
        raise HTTPException(status_code=400, detail="请指定输入文件")
    try:
        fi = stat_path(eff, os.path.join(init_dir, rel), _roots())
    except FsError as e:
        raise HTTPException(status_code=400, detail="输入文件不存在: " + e.message)
    if fi["is_dir"]:
        raise HTTPException(status_code=400, detail="输入文件不能是目录")

    # 3) 执行身份：管理员→按初始目录属主；普通用户→自己
    if is_admin:
        try:
            exec_user = pwd.getpwuid(os.stat(init_dir).st_uid).pw_name
        except (KeyError, OSError):
            raise HTTPException(status_code=400, detail="无法确定初始目录属主，无法提交")
    else:
        exec_user = user

    # 4) 取试算模板并渲染命令
    t = request.app.state.templates_db.get(body.template_id)
    if not t:
        raise HTTPException(status_code=404, detail="模板不存在")
    kind = t["kind"] if "kind" in t.keys() else "pbs"
    if kind != KIND_TRIAL:
        raise HTTPException(status_code=400, detail="所选模板不是试算模板")
    command = _render_trial(t["content"], init_dir, rel)

    # 5) 交给试算管理器在管理节点直接跑
    tm = request.app.state.trial_manager
    jobname = _safe_jobname(body.name)
    try:
        tid = tm.start_trial(owner=exec_user, name=jobname, workdir=init_dir, command=command)
    except TrialError as e:
        raise HTTPException(status_code=409, detail=str(e))
    log.info("试算提交 id=%s name=%s exec_user=%s 操作人=%s", tid, jobname, exec_user, user)
    return {"id": tid, "jobid": f"trial:{tid}", "exec_user": exec_user, "name": jobname}


def _get_owned_trial(request: Request, trial_id: int, user: str, is_admin: bool):
    row = _db(request).trial_get(trial_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="试算不存在")
    if row["owner"] != user and not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该试算")
    return row


@router.get("/{trial_id}/output")
def trial_output(
    trial_id: int, request: Request,
    offset: int = Query(0, ge=0, description="已读到的字节偏移，用于增量拉取"),
    user: str = Depends(current_user), is_admin: bool = Depends(is_admin_request),
) -> dict:
    """增量读取试算命令输出（前端轮询 tail）。"""
    row = _get_owned_trial(request, trial_id, user, is_admin)
    tm = request.app.state.trial_manager
    out = tm.read_output(row, offset)
    return {
        "id": trial_id,
        "status": row["status"],
        "exit_code": row["exit_code"],
        "msg": row["msg"],
        "data": out["data"],
        "offset": out["offset"],
        "size": out["size"],
    }


@router.post("/{trial_id}/cancel")
def cancel_trial(
    trial_id: int, request: Request,
    user: str = Depends(current_user), is_admin: bool = Depends(is_admin_request),
) -> dict:
    """中断运行中的试算（killpg 整组，覆盖派生子进程）。"""
    _get_owned_trial(request, trial_id, user, is_admin)
    tm = request.app.state.trial_manager
    try:
        tm.cancel(trial_id)
    except TrialError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    log.info("试算中断 id=%s 操作人=%s", trial_id, user)
    return {"id": trial_id, "cancelled": True}
