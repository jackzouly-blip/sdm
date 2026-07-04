"""作业提交：模板 CRUD（管理员）+ 提交作业（本地排队 → 准入调度器 qsub）。

提交流程：取模板内容 → 替换 ###init_dir_path###/###input_file_path### → 把 #PBS -l
nodes 行注入 ppn=<核数>(保留原有节点特性) → 以"初始目录属主"身份把脚本写入初始目录
→ 写入本地排队队列(submission_queue) → 同步触发一次准入调度：无策略限制且核数
充裕时会在本次请求内直接 qsub 完成(与旧行为一致)，否则留在队列里等待
SubmissionScheduler 后续补位（见 app/submit/scheduler.py）。
"""
from __future__ import annotations

import os
import pwd
import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth.session import current_user, is_admin_request
from ..config import get_settings
from ..db.jobs_db import SQ_FAILED, SQ_QUEUED, SQ_SUBMITTED
from ..fs.browser import FsError, stat_path, write_file
from ..logger import get_logger
from ..pbs.cores import cores_from_nodes
from ..privilege.actas import run_as_user
from .db import row_to_dict
from .scheduler import get_scheduler

router = APIRouter(tags=["submit"])
log = get_logger(__name__)


def _db(request: Request):
    return request.app.state.templates_db


def _roots():
    return get_settings().fs_root_list


def _require_admin(is_admin: bool) -> None:
    if not is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")


# ===== 模板 CRUD（管理员）=====
class TemplateIn(BaseModel):
    name: str
    content: str


@router.get("/templates")
def list_templates(request: Request, user: str = Depends(current_user)):
    return [row_to_dict(r) for r in _db(request).list_all()]


@router.post("/templates")
def create_template(body: TemplateIn, request: Request, user: str = Depends(current_user), is_admin: bool = Depends(is_admin_request)):
    _require_admin(is_admin)
    return row_to_dict(_db(request).create(body.name, body.content))


@router.put("/templates/{tid}")
def update_template(tid: int, body: TemplateIn, request: Request, user: str = Depends(current_user), is_admin: bool = Depends(is_admin_request)):
    _require_admin(is_admin)
    r = _db(request).update(tid, {"name": body.name, "content": body.content})
    if not r:
        raise HTTPException(status_code=404, detail="模板不存在")
    return row_to_dict(r)


@router.delete("/templates/{tid}")
def delete_template(tid: int, request: Request, user: str = Depends(current_user), is_admin: bool = Depends(is_admin_request)):
    _require_admin(is_admin)
    if not _db(request).delete(tid):
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"ok": True}


# ===== 用户提交策略（管理员）=====
# 未配置的用户 = 不限并发、优先级0（与不开启本功能时行为一致）。
class UserPolicyIn(BaseModel):
    max_concurrent: Optional[int] = None  # 留空=不限并发（仍受全局核数约束）
    priority: int = 0                     # 越大越优先抢占空出来的核数/名额


def _policy_to_dict(row) -> dict:
    return {
        "user": row["user"],
        "max_concurrent": row["max_concurrent"],
        "priority": row["priority"],
        "updated_at": row["updated_at"],
    }


@router.get("/admin/user-policies")
def list_user_policies(request: Request, user: str = Depends(current_user), is_admin: bool = Depends(is_admin_request)):
    _require_admin(is_admin)
    return [_policy_to_dict(r) for r in request.app.state.jobs_db.policy_list()]


@router.put("/admin/user-policies/{target_user}")
def upsert_user_policy(
    target_user: str, body: UserPolicyIn, request: Request,
    user: str = Depends(current_user), is_admin: bool = Depends(is_admin_request),
):
    _require_admin(is_admin)
    if body.max_concurrent is not None and body.max_concurrent < 1:
        raise HTTPException(status_code=400, detail="max_concurrent 需为正整数或留空(不限)")
    row = request.app.state.jobs_db.policy_upsert(target_user, body.max_concurrent, body.priority)
    return _policy_to_dict(row)


@router.delete("/admin/user-policies/{target_user}")
def delete_user_policy(
    target_user: str, request: Request,
    user: str = Depends(current_user), is_admin: bool = Depends(is_admin_request),
):
    _require_admin(is_admin)
    if not request.app.state.jobs_db.policy_delete(target_user):
        raise HTTPException(status_code=404, detail="策略不存在")
    return {"ok": True}


# ===== 作业提交 =====
_NODES_RE = re.compile(r"(?mi)^(\s*#PBS\s+-l\s+nodes=)(\S+)")


def inject_ppn(script: str, cores: int) -> str:
    """把 #PBS -l nodes=<spec> 行注入 ppn=<cores>，保留其余节点特性(如 :first)。"""
    def repl(m):
        parts = [p for p in m.group(2).split(":") if not p.lower().startswith("ppn=")]
        parts = [parts[0], f"ppn={cores}"] + parts[1:]
        return m.group(1) + ":".join(parts)
    return _NODES_RE.sub(repl, script)


def render_template(content: str, init_dir: str, input_rel: str) -> str:
    # 初始路径映射到计算节点路径(脚本在计算节点执行)，并确保结尾带 /
    idir = get_settings().map_path(init_dir)
    if not idir.endswith("/"):
        idir += "/"
    return (content.replace("###init_dir_path###", idir)
                   .replace("###input_file_path###", input_rel.lstrip("/")))


def _safe_jobname(name: str) -> str:
    """PBS 作业名:字母开头、仅字母数字下划线连字符、限长。"""
    s = re.sub(r"[^A-Za-z0-9_-]", "_", name.strip()) or "job"
    if not s[0].isalpha():
        s = "j" + s
    return s[:60]


class SubmitIn(BaseModel):
    name: str
    cores: int
    init_dir: str
    input_file: str            # 相对 init_dir 的 K 文件
    queue: Optional[str] = None   # 队列名，留空用服务端默认(batch)
    template_id: Optional[int] = None
    script: Optional[str] = None  # 前端可传编辑后的脚本(优先于模板)


@router.post("/jobs/submit")
def submit_job(body: SubmitIn, request: Request, user: str = Depends(current_user), is_admin: bool = Depends(is_admin_request)):
    s = get_settings()
    eff = "root" if is_admin else user  # 校验/读取身份

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

    # 3) 执行身份：管理员→按初始目录属主提交(替用户提交)；普通用户→自己
    if is_admin:
        try:
            exec_user = pwd.getpwuid(os.stat(init_dir).st_uid).pw_name
        except (KeyError, OSError):
            raise HTTPException(status_code=400, detail="无法确定初始目录属主，无法提交")
    else:
        exec_user = user

    # 4) 取脚本：前端传的优先，否则用模板渲染
    if body.script:
        script = body.script
    else:
        if not body.template_id:
            raise HTTPException(status_code=400, detail="缺少模板")
        t = _db(request).get(body.template_id)
        if not t:
            raise HTTPException(status_code=404, detail="模板不存在")
        script = render_template(t["content"], init_dir, rel)

    # 5) 注入核数(ppn)
    cores = int(body.cores or 0)
    extra_l = None
    if cores > 0:
        if _NODES_RE.search(script):
            script = inject_ppn(script, cores)
        else:
            extra_l = f"nodes=1:ppn={cores}"  # 模板无 nodes 行则命令行兜底

    # 6) 以执行身份写脚本到初始目录
    jobname = _safe_jobname(body.name)
    fname = f".portal_{jobname}_{uuid.uuid4().hex[:8]}.pbs"
    try:
        write_file(exec_user, init_dir, fname, script.encode("utf-8"), _roots())
    except FsError as e:
        raise HTTPException(status_code=e.status, detail="写入提交脚本失败: " + e.message)
    script_path = os.path.join(init_dir, fname)

    # 7) 写入本地排队队列，交给准入调度器判定何时真正 qsub。
    #    核数记账：优先用请求核数(会被注入脚本)，否则从脚本自带的 nodes 行解析，
    #    都拿不到则保守按 1 核算。
    if cores > 0:
        est_cores = cores
    else:
        m = _NODES_RE.search(script)
        est_cores = cores_from_nodes(m.group(2)) if m else 1
    queue_name = (body.queue or "").strip() or s.submit_default_queue or "batch"
    jobs_db = request.app.state.jobs_db
    qid = jobs_db.sq_enqueue(
        exec_user, jobname, queue_name, script_path, init_dir, est_cores, extra_l
    )

    # 同步触发一次准入调度：无策略限制且核数充裕时会在本次请求内直接提交完成，
    # 对没配置策略的默认用户而言与"直接 qsub"体验一致，不会有排队感。
    sched = get_scheduler()
    if sched is not None:
        try:
            sched.tick()
        except Exception:  # noqa: BLE001
            log.exception("提交后触发准入调度失败")

    row = jobs_db.sq_get(qid)
    if row["status"] == SQ_SUBMITTED:
        return {
            "status": "submitted",
            "jobid": row["jobid"],
            "exec_user": exec_user,
            "name": jobname,
            "script": script_path,
        }
    if row["status"] == SQ_FAILED:
        raise HTTPException(status_code=500, detail=f"qsub 失败: {row['msg'] or '未知错误'}")
    # 仍在本地排队：受用户并发配额或全局核数余量限制，等待调度器后续补位
    queued_total = len(jobs_db.sq_list(status=SQ_QUEUED))
    return {
        "status": "queued",
        "queue_id": qid,
        "queued_total": queued_total,
        "exec_user": exec_user,
        "name": jobname,
        "script": script_path,
    }
