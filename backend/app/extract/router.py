"""数据提取规则路由 + 手动触发。

规则管理限运维（HPC_ADMIN_USERS 白名单，留空则放开）。
手动触发提取要求调用者为任务属主（或运维）。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from ..auth.session import current_user
from ..config import get_settings
from ..db.jobs_db import JobsDB
from .dispatcher import Dispatcher
from .rules_db import RulesDB, row_to_dict

router = APIRouter(prefix="/extract", tags=["extract"])


class RuleIn(BaseModel):
    name: str
    command: str
    match_name: str = ""
    match_queue: str = ""
    match_workdir_prefix: str = ""
    match_owner: str = ""
    enabled: bool = True
    priority: int = 100


class RulePatch(BaseModel):
    name: Optional[str] = None
    command: Optional[str] = None
    match_name: Optional[str] = None
    match_queue: Optional[str] = None
    match_workdir_prefix: Optional[str] = None
    match_owner: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


def _rules(request: Request) -> RulesDB:
    return request.app.state.rules_db


def _dispatcher(request: Request) -> Dispatcher:
    return request.app.state.dispatcher


def _jobs(request: Request) -> JobsDB:
    return request.app.state.jobs_db


def _require_admin(user: str) -> None:
    if not get_settings().is_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权管理提取规则"
        )


@router.get("/rules")
def list_rules(request: Request, user: str = Depends(current_user)) -> List[dict]:
    # 任意登录用户可只读查看规则(了解其作业会自动跑什么)；增删改仍限管理员。
    return [row_to_dict(r) for r in _rules(request).list_all()]


@router.post("/rules")
def create_rule(
    body: RuleIn, request: Request, user: str = Depends(current_user)
) -> dict:
    _require_admin(user)
    # 提前校验命令模板可被解析（避免存入无法执行的规则）
    from .service import render_argv

    try:
        render_argv(body.command, {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return row_to_dict(_rules(request).create(body.model_dump()))


@router.put("/rules/{rule_id}")
def update_rule(
    rule_id: int,
    body: RulePatch,
    request: Request,
    user: str = Depends(current_user),
) -> dict:
    _require_admin(user)
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    if "command" in data:
        from .service import render_argv

        try:
            render_argv(data["command"], {})
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    row = _rules(request).update(rule_id, data)
    if row is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    return row_to_dict(row)


@router.delete("/rules/{rule_id}")
def delete_rule(
    rule_id: int, request: Request, user: str = Depends(current_user)
) -> dict:
    _require_admin(user)
    if not _rules(request).delete(rule_id):
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"deleted": rule_id}


@router.post("/jobs/{jobid}/run")
def run_extract(
    jobid: str, request: Request, user: str = Depends(current_user)
) -> dict:
    """手动对某任务触发提取（按当前规则重新匹配派发）。"""
    job = _jobs(request).get(jobid)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 属主或运维可触发
    if job["owner"] != user and not get_settings().is_admin(user):
        raise HTTPException(status_code=403, detail="无权操作该任务")
    count = _dispatcher(request).dispatch_one(jobid, force=True)
    return {"jobid": jobid, "dispatched": count}
