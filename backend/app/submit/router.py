"""作业提交：模板 CRUD（管理员）+ 提交作业（qsub）。

提交流程：取模板内容 → 替换 ###init_dir_path###/###input_file_path### → 把 #PBS -l
nodes 行注入 ppn=<核数>(保留原有节点特性) → 以"初始目录属主"身份把脚本写入初始目录
并 qsub 到默认队列 → 返回作业号(轮询自动收录到作业管理)。
"""
from __future__ import annotations

import os
import pwd
import re
import shutil
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth.session import current_user
from ..config import get_settings
from ..fs.browser import FsError, stat_path, write_file
from ..privilege.actas import run_as_user
from .db import row_to_dict

router = APIRouter(tags=["submit"])


def _db(request: Request):
    return request.app.state.templates_db


def _roots():
    return get_settings().fs_root_list


def _require_admin(user: str) -> None:
    if not get_settings().is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")


# ===== 模板 CRUD（管理员）=====
class TemplateIn(BaseModel):
    name: str
    content: str


@router.get("/templates")
def list_templates(request: Request, user: str = Depends(current_user)):
    return [row_to_dict(r) for r in _db(request).list_all()]


@router.post("/templates")
def create_template(body: TemplateIn, request: Request, user: str = Depends(current_user)):
    _require_admin(user)
    return row_to_dict(_db(request).create(body.name, body.content))


@router.put("/templates/{tid}")
def update_template(tid: int, body: TemplateIn, request: Request, user: str = Depends(current_user)):
    _require_admin(user)
    r = _db(request).update(tid, {"name": body.name, "content": body.content})
    if not r:
        raise HTTPException(status_code=404, detail="模板不存在")
    return row_to_dict(r)


@router.delete("/templates/{tid}")
def delete_template(tid: int, request: Request, user: str = Depends(current_user)):
    _require_admin(user)
    if not _db(request).delete(tid):
        raise HTTPException(status_code=404, detail="模板不存在")
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
def submit_job(body: SubmitIn, request: Request, user: str = Depends(current_user)):
    s = get_settings()
    is_admin = s.is_admin(user)
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

    # 7) qsub（用绝对路径，因降权后 PATH 不含 Torque bin）
    qsub = shutil.which("qsub") or "/usr/local/torque-6.1.2/bin/qsub"
    queue = (body.queue or "").strip() or s.submit_default_queue or "batch"
    argv = [qsub, "-N", jobname, "-q", queue]
    if extra_l:
        argv += ["-l", extra_l]
    argv.append(script_path)
    proc = run_as_user(exec_user, argv, cwd=init_dir, timeout=60)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()[:500]
        raise HTTPException(status_code=500, detail=f"qsub 失败: {err or '未知错误'}")
    jobid = proc.stdout.decode("utf-8", "replace").strip()
    return {"jobid": jobid, "exec_user": exec_user, "name": jobname, "script": script_path}
