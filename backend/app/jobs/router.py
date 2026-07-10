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

from ..auth.session import current_user, is_admin_request
from ..config import get_settings
from ..db.jobs_db import JobsDB
from ..logger import get_logger
from ..privilege.actas import call_as_user, run_as_user

router = APIRouter(prefix="/jobs", tags=["jobs"])
log = get_logger(__name__)

# 容错导入网盘自动分享任务（触发 netdisk_autoshare 注册）。
# 网盘为可选子系统，缺失/异常时仅禁用该功能，绝不拖垮门户启动。
try:
    from ..netdisk import autoshare  # noqa: F401
    _NETDISK_AVAILABLE = True
except Exception as _e:  # noqa: BLE001
    _NETDISK_AVAILABLE = False
    log.warning("网盘自动分享模块不可用，相关功能已禁用: %s", _e)


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
    # 本地排队中的提交请求(尚未/未能进入 PBS)才会有值；真实 PBS 任务恒为 None。
    queue_id: Optional[int] = None
    msg: Optional[str] = None


class JobDetail(JobSummary):
    exec_host: Optional[str]
    walltime_limit: Optional[str]
    exit_status: Optional[int]
    extract_state: str
    raw: dict
    # 结果网盘自动分享
    netdisk_state: str = "none"
    netdisk_share_url: Optional[str] = None
    netdisk_share_pwd: Optional[str] = None
    netdisk_expire_at: Optional[float] = None
    netdisk_files: Optional[List[str]] = None
    netdisk_msg: Optional[str] = None
    netdisk_updated: Optional[float] = None


def _db(request: Request) -> JobsDB:
    return request.app.state.jobs_db


def _netdisk_fields(r: sqlite3.Row) -> dict:
    """从任务行安全提取网盘分享字段（兼容历史库缺列）。"""
    keys = r.keys()

    def g(k):
        return r[k] if k in keys else None

    files = None
    if "netdisk_files" in keys and r["netdisk_files"]:
        try:
            files = json.loads(r["netdisk_files"])
        except Exception:  # noqa: BLE001
            files = None
    return {
        "netdisk_state": (g("netdisk_state") or "none"),
        "netdisk_share_url": g("netdisk_share_url"),
        "netdisk_share_pwd": g("netdisk_share_pwd"),
        "netdisk_expire_at": g("netdisk_expire_at"),
        "netdisk_files": files,
        "netdisk_msg": g("netdisk_msg"),
        "netdisk_updated": g("netdisk_updated"),
    }


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


def _sq_row_to_summary(r: sqlite3.Row) -> JobSummary:
    """把一条本地排队记录(尚未/未能进入 PBS)转成 JobSummary 形态，混入任务列表。"""
    is_failed = r["status"] == "failed"
    return JobSummary(
        jobid=f"local:{r['id']}",
        short_id=f"Q{r['id']}",
        name=r["jobname"] or "",
        owner=r["owner"],
        pbs_state="F" if is_failed else "L",
        derived_state="queue_failed" if is_failed else "queued_local",
        queue=r["queue_name"],
        workdir=get_settings().map_path(r["workdir"]),
        submit_ts=r["queued_at"],
        start_ts=None,
        end_ts=None,
        walltime_used=None,
        nodes=None,
        exec_host=None,
        queue_id=r["id"],
        msg=r["msg"],
    )


# 试算状态 → 混入任务列表时的派生状态（前端据此显示徽标/操作）
_TRIAL_DERIVED = {
    "running": "trial_running",
    "finished": "trial_done",
    "failed": "trial_failed",
    "killed": "trial_killed",
    "interrupted": "trial_interrupted",
}


def _trial_row_to_summary(r: sqlite3.Row) -> JobSummary:
    """把一条试算记录转成 JobSummary 形态，混入任务列表。

    jobid 以 trial: 前缀标识，前端据此走"试算输出"面板而非 PBS 详情页；
    workdir 用真实 head 路径（试算就在管理节点该路径下执行，不做计算节点映射）。"""
    return JobSummary(
        jobid=f"trial:{r['id']}",
        short_id=f"T{r['id']}",
        name=r["name"] or "",
        owner=r["owner"],
        pbs_state="",
        derived_state=_TRIAL_DERIVED.get(r["status"], "trial_done"),
        queue="试算",
        workdir=r["workdir"],
        submit_ts=r["created_at"],
        start_ts=r["started_at"],
        end_ts=r["ended_at"],
        walltime_used=None,
        nodes=None,
        exec_host="管理节点",
        queue_id=None,
        msg=r["msg"],
    )


@router.get("", response_model=List[JobSummary])
def list_jobs(
    request: Request,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
    state: Optional[str] = Query(None, description="active / done，留空为全部"),
) -> List[JobSummary]:
    # 管理员查看所有用户的任务；普通用户只看自己的
    owner = None if is_admin else user
    rows = _db(request).list_by_owner(owner, state=state)
    out = [_row_to_summary(r) for r in rows]
    if state in (None, "active"):
        # 本地排队中/提交失败的记录也算"活跃"的一部分，一并展示；
        # submitted/cancelled 不重复展示(submitted 很快会被轮询采集为真实任务行)。
        for r in _db(request).sq_list(owner=owner):
            if r["status"] in ("queued", "submitting", "failed"):
                out.append(_sq_row_to_summary(r))
    # 试算任务（非 PBS，管理节点直跑）：running 计入 active，终态计入 done。
    for r in _db(request).trial_list(owner=owner):
        running = r["status"] == "running"
        if state == "active" and not running:
            continue
        if state == "done" and running:
            continue
        out.append(_trial_row_to_summary(r))
    return out


@router.get("/netdisk/queue")
def netdisk_queue(
    request: Request,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
    jobid: Optional[str] = Query(None, description="按任务过滤"),
    status_filter: Optional[str] = Query(None, alias="status",
                                         description="queued/uploading/done/failed"),
) -> dict:
    """网盘流式上传队列总览：各状态计数 + 队列项明细。供观测"文件是否已入队"。"""
    db = _db(request)
    counts = db.nq_counts()
    items = db.nq_list(jobid=jobid, status=status_filter)
    # 普通用户只看自己的队列项
    out = []
    for r in items:
        if not is_admin and r["owner"] != user:
            continue
        out.append({
            "id": r["id"], "jobid": r["jobid"], "owner": r["owner"],
            "fname": r["fname"], "status": r["status"],
            "enqueued_at": r["enqueued_at"], "updated_at": r["updated_at"],
            "msg": r["msg"],
        })
    return {"counts": counts, "items": out}


@router.get("/{jobid}", response_model=JobDetail)
def get_job(
    jobid: str,
    request: Request,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> JobDetail:
    row = _db(request).get(jobid)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    # 权限隔离：只能看自己的任务；管理员可访问所有任务
    if row["owner"] != user and not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该任务")
    summary = _row_to_summary(row)
    return JobDetail(
        **summary.model_dump(),
        walltime_limit=row["walltime_limit"],
        exit_status=row["exit_status"],
        extract_state=row["extract_state"] if "extract_state" in row.keys() else "none",
        raw=json.loads(row["raw"]) if row["raw"] else {},
        **_netdisk_fields(row),
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
    is_admin: bool = Depends(is_admin_request),
) -> dict:
    """清理任务工作目录下的 disk* / mes* / scr* 临时文件（以任务属主身份执行）。"""
    row = _db(request).get(jobid)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    # 权限隔离：仅任务属主或管理员可清理
    if row["owner"] != user and not is_admin:
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
    is_admin: bool = Depends(is_admin_request),
) -> dict:
    """终止运行/排队中的任务（qdel，以任务属主身份执行）。"""
    row = _db(request).get(jobid)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    # 权限隔离：仅任务属主或管理员可终止
    if row["owner"] != user and not is_admin:
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


@router.post("/queue/{item_id}/cancel")
def cancel_queued_submission(
    item_id: int,
    request: Request,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> dict:
    """撤回一个尚未提交到 PBS 的本地排队项（未占用任何 PBS 资源，无需 qdel）。"""
    db = _db(request)
    row = db.sq_get(item_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="排队项不存在")
    if row["owner"] != user and not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权操作该排队项")
    if not db.sq_cancel(item_id, owner=None if is_admin else user):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="已提交或已撤回，无法取消"
        )
    log.info("已撤回本地排队项 %s（属主 %s，操作人 %s）", item_id, row["owner"], user)
    return {"id": item_id, "cancelled": True}


@router.delete("/queue/{item_id}")
def delete_failed_submission(
    item_id: int,
    request: Request,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> dict:
    """删除一条提交失败的本地排队记录（仅 failed 状态；未占任何 PBS 资源）。"""
    db = _db(request)
    row = db.sq_get(item_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="排队项不存在")
    if row["owner"] != user and not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权操作该排队项")
    if not db.sq_delete_failed(item_id, owner=None if is_admin else user):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="仅可删除“提交失败”的记录"
        )
    log.info("已删除失败本地排队项 %s（属主 %s，操作人 %s）", item_id, row["owner"], user)
    return {"id": item_id, "deleted": True}


@router.get("/{jobid}/netdisk-preview")
def netdisk_preview(
    jobid: str,
    request: Request,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> dict:
    """预览将要上传到网盘的结果文件清单与总大小（不触发上传）。"""
    row = _db(request).get(jobid)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if row["owner"] != user and not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该任务")
    workdir = get_settings().map_path(row["workdir"]) or ""
    if not _NETDISK_AVAILABLE or not workdir or not os.path.isdir(workdir):
        return {"count": 0, "total_bytes": 0, "files": []}
    entries = autoshare.scan_result_entries(workdir)
    files = [{"name": os.path.basename(p), "size": s} for p, s in entries]
    return {
        "count": len(files),
        "total_bytes": sum(f["size"] for f in files),
        "files": files,
    }


@router.post("/{jobid}/netdisk-share")
def netdisk_share_job(
    jobid: str,
    request: Request,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> dict:
    """将任务结果（h3d / d3plot / binout）上传到百度网盘并生成分享链接。"""
    if not _NETDISK_AVAILABLE:
        raise HTTPException(status_code=503, detail="网盘分享功能未启用")
    row = _db(request).get(jobid)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if row["owner"] != user and not get_settings().is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权操作该任务")
    if row["derived_state"] != "done":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="任务尚未完成，暂不能上传分享"
        )
    cur = row["netdisk_state"] if "netdisk_state" in row.keys() else "none"
    if cur in ("pending", "uploading"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="已在上传中，请稍候")
    tm = request.app.state.task_manager
    task_id = tm.submit(
        "netdisk_autoshare",
        owner=row["owner"],
        params={
            "jobid": jobid,
            # 后端可读的工作目录（/data→/caedata 映射）
            "workdir": get_settings().map_path(row["workdir"]),
            "owner": row["owner"],
            "task_name": row["name"] or row["short_id"],
            "short_id": row["short_id"],
            "period": get_settings().netdisk_share_period,
        },
    )
    _db(request).set_netdisk(jobid, netdisk_state="pending", netdisk_msg=None)
    log.info("已提交网盘分享任务 %s（job=%s, 属主=%s, 操作人=%s）", task_id, jobid, row["owner"], user)
    return {"task_id": task_id, "jobid": jobid}
