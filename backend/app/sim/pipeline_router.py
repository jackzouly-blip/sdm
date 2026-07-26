"""编排路由：节点类型、定义、运行。

属主隔离与 sim 路由一致：普通用户只见自己的定义与运行，管理员跨用户可见，
越权一律 404。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth.session import current_user, is_admin_request
from ..logger import get_logger
from .db import SimDB
from .engine import DagError, PipelineEngine, validate_doc
from .nodes import list_node_types

router = APIRouter(prefix="/sim", tags=["sim-pipeline"])
log = get_logger(__name__)


def _db(request: Request) -> SimDB:
    return request.app.state.sim_db


def _engine(request: Request) -> PipelineEngine:
    return request.app.state.pipeline_engine


def _row(r: sqlite3.Row, json_fields: tuple = ()) -> Dict:
    out = dict(r)
    for f in json_fields:
        if f in out:
            raw = out.pop(f)
            key = f[:-5] if f.endswith("_json") else f
            try:
                out[key] = json.loads(raw) if raw else None
            except (ValueError, TypeError):
                out[key] = None
    return out


DEF_JSON = ("doc_json",)
RUN_JSON = ("doc_snapshot_json",)
NODE_JSON = ("inputs_json", "outputs_json")


def _owned_def(db: SimDB, pid: str, user: str, is_admin: bool) -> sqlite3.Row:
    row = db.get_pipeline_def(pid)
    if row is None or (not is_admin and row["owner"] != user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "编排定义不存在")
    return row


def _owned_run(db: SimDB, rid: str, user: str, is_admin: bool) -> sqlite3.Row:
    row = db.get_run(rid)
    if row is None or (not is_admin and row["owner"] != user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "编排运行不存在")
    return row


# --- 请求体 -------------------------------------------------------------

class PipelineDefCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    # {"nodes":[{id,type,label,params,position}], "edges":[{from,to}]}
    doc: Dict


class PipelineDefUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    doc: Optional[Dict] = None


class RunCreate(BaseModel):
    sim_project_id: Optional[str] = None
    sim_subject_id: Optional[str] = None


class NodeComplete(BaseModel):
    """外部回流：HPC 完成、浏览器代理调完能力、人工确认，都用这个。"""
    outputs: Optional[Dict] = None
    error: Optional[str] = None


class NodeClaim(BaseModel):
    external_ref: str


# --- 节点类型 -----------------------------------------------------------

@router.get("/node-types")
def get_node_types(user: str = Depends(current_user)) -> List[Dict]:
    """节点类型清单。画布的节点面板与参数表单完全由此生成——
    编辑器不认识任何具体类型，新增类型无需改前端。"""
    return [nt.to_public() for nt in list_node_types()]


# --- 定义 ---------------------------------------------------------------

@router.get("/pipelines")
def list_pipelines(
    request: Request,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    return [_row(r, DEF_JSON)
            for r in db.list_pipeline_defs(owner=None if is_admin else user)]


@router.post("/pipelines", status_code=status.HTTP_201_CREATED)
def create_pipeline(
    request: Request,
    body: PipelineDefCreate,
    user: str = Depends(current_user),
) -> Dict:
    db = _db(request)
    try:
        validate_doc(body.doc)
    except DagError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    pid = db.create_pipeline_def(body.name, user, body.doc, body.description)
    return _row(db.get_pipeline_def(pid), DEF_JSON)


@router.get("/pipelines/{pid}")
def get_pipeline(
    request: Request,
    pid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    return _row(_owned_def(_db(request), pid, user, is_admin), DEF_JSON)


@router.patch("/pipelines/{pid}")
def update_pipeline(
    request: Request,
    pid: str,
    body: PipelineDefUpdate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    _owned_def(db, pid, user, is_admin)
    if body.doc is not None:
        try:
            validate_doc(body.doc)
        except DagError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    db.update_pipeline_def(pid, body.name, body.description, body.doc)
    return _row(db.get_pipeline_def(pid), DEF_JSON)


@router.delete("/pipelines/{pid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_pipeline(
    request: Request,
    pid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> None:
    db = _db(request)
    _owned_def(db, pid, user, is_admin)
    db.delete_pipeline_def(pid)


@router.post("/pipelines/validate")
def validate_pipeline(body: Dict, user: str = Depends(current_user)) -> Dict:
    """校验 DAG 文档而不保存——画布可在编辑时实时给出环、悬空边等问题。"""
    try:
        validate_doc(body.get("doc") or {})
    except DagError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


# --- 运行 ---------------------------------------------------------------

@router.post("/pipelines/{pid}/runs", status_code=status.HTTP_201_CREATED)
def start_run(
    request: Request,
    pid: str,
    body: RunCreate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    _owned_def(db, pid, user, is_admin)
    if body.sim_subject_id and db.get_subject(body.sim_subject_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "指定的工况不存在")
    try:
        rid = _engine(request).start_run(pid, user, body.sim_project_id,
                                         body.sim_subject_id)
    except DagError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return _run_detail(db, rid)


def _run_detail(db: SimDB, rid: str) -> Dict:
    run = _row(db.get_run(rid), RUN_JSON)
    run["nodes"] = [_row(n, NODE_JSON) for n in db.list_node_runs(rid)]
    return run


@router.get("/runs")
def list_runs(
    request: Request,
    sim_subject_id: Optional[str] = None,
    active_only: bool = False,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    rows = db.list_runs(owner=None if is_admin else user,
                        sim_subject_id=sim_subject_id, active_only=active_only)
    return [_row(r, RUN_JSON) for r in rows]


@router.get("/runs/{rid}")
def get_run(
    request: Request,
    rid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    _owned_run(db, rid, user, is_admin)
    return _run_detail(db, rid)


@router.post("/runs/{rid}/cancel")
def cancel_run(
    request: Request,
    rid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    _owned_run(db, rid, user, is_admin)
    _engine(request).cancel_run(rid)
    return _run_detail(db, rid)


@router.post("/runs/{rid}/nodes/{node_id}/complete")
def complete_node(
    request: Request,
    rid: str,
    node_id: str,
    body: NodeComplete,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """外部完成一个等待中的节点。

    三类来源共用此接口：HPC 作业结束、浏览器代理调完 vektor3d 能力、人工确认。
    """
    db = _db(request)
    _owned_run(db, rid, user, is_admin)
    try:
        _engine(request).complete_node(rid, node_id, body.outputs, body.error)
    except DagError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return _run_detail(db, rid)


@router.post("/runs/{rid}/nodes/{node_id}/claim")
def claim_node(
    request: Request,
    rid: str,
    node_id: str,
    body: NodeClaim,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """给等待中的节点登记外部句柄（HPC 作业号 / 能力作业号），便于回流定位。"""
    db = _db(request)
    _owned_run(db, rid, user, is_admin)
    try:
        _engine(request).claim_node(rid, node_id, body.external_ref)
    except DagError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return _run_detail(db, rid)


@router.get("/pending-capability-nodes")
def pending_capability_nodes(
    request: Request,
    user: str = Depends(current_user),
) -> List[Dict]:
    """浏览器代理的待办队列。

    vektor3d 是桌面端、只监听 localhost，SDM 后端调不到，故由用户浏览器代劳：
    从这里取走待办 → 调本机 vektor3d → 调 complete 接口带结果回来。
    只返回本人的，避免替他人执行。
    """
    db = _db(request)
    rows = db.list_waiting_nodes(owner=user, category_prefix="capability.")
    out = []
    for r in rows:
        item = _row(r, NODE_JSON)
        run = db.get_run(r["pipeline_run_id"])
        doc = json.loads(run["doc_snapshot_json"]) if run else {}
        spec = next((n for n in doc.get("nodes", []) if n["id"] == r["node_id"]), {})
        item["params"] = spec.get("params") or {}
        item["run_id"] = r["pipeline_run_id"]
        out.append(item)
    return out
