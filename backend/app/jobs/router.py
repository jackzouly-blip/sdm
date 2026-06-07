"""任务路由：列表 / 详情。强制只返回登录用户自己的任务。"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from ..auth.session import current_user
from ..config import get_settings
from ..db.jobs_db import JobsDB
from ..logger import get_logger
from ..privilege.actas import call_as_user, run_as_user

router = APIRouter(prefix="/jobs", tags=["jobs"])
log = get_logger(__name__)


class JobSummary(BaseModel):
    jobid: str
    short_id: str
    name: str
    owner: str
    pbs_state: str
    derived_state: str
    queue: Optional[str]
    workdir: Optional[str]
    submit_ts: Optional[float]
    start_ts: Optional[float]
    end_ts: Optional[float]
    walltime_used: Optional[str]
    nodes: Optional[str]
    exec_host: Optional[str]


class JobDetail(JobSummary):
    exec_host: Optional[str]
    walltime_limit: Optional[str]
    exit_status: Optional[int]
    extract_state: str
    raw: dict


def _db(request: Request) -> JobsDB:
    return request.app.state.jobs_db


def _row_to_summary(r: sqlite3.Row) -> JobSummary:
    return JobSummary(
        jobid=r["jobid"],
        short_id=r["short_id"],
        name=r["name"] or "",
        owner=r["owner"],
        pbs_state=r["pbs_state"] or "",
        derived_state=r["derived_state"],
        queue=r["queue"],
        workdir=get_settings().map_path(r["workdir"]),  # 展示用计算节点路径(/data→/caedata)
        submit_ts=r["submit_ts"],
        start_ts=r["start_ts"],
        end_ts=r["end_ts"],
        walltime_used=r["walltime_used"],
        nodes=r["nodes"],
        exec_host=r["exec_host"],
    )


@router.get("", response_model=List[JobSummary])
def list_jobs(
    request: Request,
    user: str = Depends(current_user),
    state: Optional[str] = Query(None, description="active / done，留空为全部"),
) -> List[JobSummary]:
    # 管理员查看所有用户的任务；普通用户只看自己的
    owner = None if get_settings().is_admin(user) else user
    rows = _db(request).list_by_owner(owner, state=state)
    return [_row_to_summary(r) for r in rows]


@router.get("/{jobid}", response_model=JobDetail)
def get_job(
    jobid: str,
    request: Request,
    user: str = Depends(current_user),
) -> JobDetail:
    row = _db(request).get(jobid)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    # 权限隔离：只能看自己的任务；管理员可访问所有任务
    if row["owner"] != user and not get_settings().is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该任务")
    summary = _row_to_summary(row)
    return JobDetail(
        **summary.model_dump(),
        walltime_limit=row["walltime_limit"],
        exit_status=row["exit_status"],
        extract_state=row["extract_state"] if "extract_state" in row.keys() else "none",
        raw=json.loads(row["raw"]) if row["raw"] else {},
    )


# 任务工作目录可清理的临时/中间文件通配类别（LS-DYNA 的磁盘/消息/暂存文件）
CLEANUP_PATTERNS = ("disk*", "mes*", "scr*")


def _cleanup_workdir(workdir: str, patterns) -> dict:
    """删除工作目录“顶层”匹配 patterns 的普通文件，返回清理结果。

    经 call_as_user 以任务属主身份在子进程中执行，受 OS 权限约束；
    安全约束：只删普通文件与符号链接、跳过目录、不递归子目录。
    """
    deleted: list = []
    freed = 0
    errors: list = []
    try:
        entries = list(os.scandir(workdir))
    except OSError as e:
        return {"deleted": [], "count": 0, "freed_bytes": 0, "errors": [str(e)]}
    for ent in entries:
        if not any(fnmatch.fnmatch(ent.name, p) for p in patterns):
            continue
        try:
            if ent.is_dir(follow_symlinks=False):
                continue  # 不删目录，避免误删
            try:
                freed += ent.stat(follow_symlinks=False).st_size
            except OSError:
                pass
            os.unlink(ent.path)
            deleted.append(ent.name)
        except OSError as ex:
            errors.append(f"{ent.name}: {ex}")
    return {"deleted": deleted, "count": len(deleted), "freed_bytes": freed, "errors": errors}


@router.post("/{jobid}/cleanup")
def cleanup_job_files(
    jobid: str,
    request: Request,
    user: str = Depends(current_user),
) -> dict:
    """清理任务工作目录下的 disk* / mes* / scr* 临时文件（以任务属主身份执行）。"""
    row = _db(request).get(jobid)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    # 权限隔离：仅任务属主或管理员可清理
    if row["owner"] != user and not get_settings().is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权操作该任务")
    workdir = row["workdir"] or ""
    if not workdir or not os.path.isdir(workdir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="任务无有效工作目录"
        )
    try:
        result = call_as_user(row["owner"], _cleanup_workdir, workdir, CLEANUP_PATTERNS)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"清理失败: {e}")
    return result


@router.post("/{jobid}/cancel")
def cancel_job(
    jobid: str,
    request: Request,
    user: str = Depends(current_user),
) -> dict:
    """终止运行/排队中的任务（qdel，以任务属主身份执行）。"""
    row = _db(request).get(jobid)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    # 权限隔离：仅任务属主或管理员可终止
    if row["owner"] != user and not get_settings().is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权操作该任务")
    # 只有活跃（运行/排队）任务才能终止
    if row["derived_state"] != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="任务已结束，无法终止"
        )
    qdel = shutil.which("qdel")
    if not qdel:
        raise HTTPException(status_code=500, detail="未找到 qdel 命令")
    # 以属主身份执行：用户只能终止自己的作业（Torque 强制），管理员经 root 可终止任意
    proc = run_as_user(row["owner"], [qdel, jobid], timeout=30)
    if proc.returncode != 0:
        msg = (proc.stderr or b"").decode("utf-8", "replace").strip()
        low = msg.lower()
        # 常见竞态：本地 DB 还显示活跃，但 Torque 里任务已结束/出队，
        # qdel 报 Unknown Job Id / Request invalid for state of job。
        # 这不是错误，任务本就已停止——返回友好提示而非 500。
        if (
            not msg
            or "unknown job" in low
            or "invalid for state" in low
            or "job has finished" in low
            or "qhist" in low
        ):
            log.info("终止任务 %s：任务已结束（qdel: %s）", jobid, msg or "无输出")
            return {
                "jobid": jobid,
                "cancelled": False,
                "message": "任务可能已结束，无需终止",
            }
        # 其它失败：记日志，返回 409 友好错误（不再抛 500）
        log.warning("终止任务 %s 失败（rc=%s）：%s", jobid, proc.returncode, msg)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"终止失败: {msg[:300]}"
        )
    log.info("已终止任务 %s（属主 %s，操作人 %s）", jobid, row["owner"], user)
    return {"jobid": jobid, "cancelled": True}
