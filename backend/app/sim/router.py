"""仿真设计路由。

属主隔离与现有 jobs 路由一致：普通用户只能看到/操作自己的仿真项目，
管理员（is_admin_request）可跨用户查看。所有下级实体（分析对象/几何/网格/
工况/作业/结果）的权限都由其所属项目的 owner 决定——单一判据，不重复实现。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth.session import current_user, is_admin_request
from ..logger import get_logger
from .db import SimDB

router = APIRouter(prefix="/sim", tags=["sim"])
log = get_logger(__name__)


def _db(request: Request) -> SimDB:
    return request.app.state.sim_db


def _json_or_none(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _row(r: sqlite3.Row, json_fields: tuple = ()) -> Dict:
    """行转 dict，并把指定的 *_json 列解析成对象（键名去掉 _json 后缀）。"""
    out = dict(r)
    for f in json_fields:
        if f in out:
            out[f[:-5] if f.endswith("_json") else f] = _json_or_none(out.pop(f))
    return out


PROJECT_JSON: tuple = ()
TARGET_JSON = ("source_ref_json",)
GEOM_JSON = ("source_file_json", "topo_summary_json")
MESH_JSON = ("mesh_params_json", "quality_json")
TEMPLATE_JSON = ("schema_json", "default_values_json",
                 "validation_rules_json", "export_mapping_json")
SUBJECT_JSON = ("config_json",)
JOB_JSON = ("submit_payload_json",)
RESULT_JSON = ("meta_json",)


# --- 权限 ---------------------------------------------------------------

def _owned_project(db: SimDB, pid: str, user: str, is_admin: bool) -> sqlite3.Row:
    """取项目并校验可见性；不可见一律 404（不泄露项目是否存在）。"""
    row = db.get_project(pid)
    if row is None or (not is_admin and row["owner"] != user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "仿真项目不存在")
    return row


def _owned_target(db: SimDB, tid: str, user: str, is_admin: bool) -> sqlite3.Row:
    row = db.get_target(tid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分析对象不存在")
    _owned_project(db, row["sim_project_id"], user, is_admin)
    return row


def _owned_subject(db: SimDB, sid: str, user: str, is_admin: bool) -> sqlite3.Row:
    row = db.get_subject(sid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工况不存在")
    _owned_project(db, row["sim_project_id"], user, is_admin)
    return row


def _owned_job(db: SimDB, jid: str, user: str, is_admin: bool) -> sqlite3.Row:
    row = db.get_job(jid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "仿真作业不存在")
    _owned_subject(db, row["sim_subject_id"], user, is_admin)
    return row


def _owned_geometry(db: SimDB, gid: str, user: str, is_admin: bool) -> sqlite3.Row:
    row = db.get_geometry(gid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "几何版本不存在")
    _owned_target(db, row["sim_target_id"], user, is_admin)
    return row


# --- 请求体 -------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    dbit_project_code: Optional[str] = None
    default_solver: Optional[str] = None
    unit_system: str = "SI"
    workdir: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    default_solver: Optional[str] = None
    unit_system: Optional[str] = None
    workdir: Optional[str] = None


class TargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    target_type: str = "part"
    source_ref: Optional[Dict] = None


class GeometryCreate(BaseModel):
    source_type: str = "upload"
    source_file: Optional[Dict] = None
    step_file: Optional[str] = None
    brep_file: Optional[str] = None
    lightweight_file: Optional[str] = None
    topo_summary: Optional[Dict] = None


class MeshCreate(BaseModel):
    mesh_type: str
    # 一期通常是 manual（人工上传）；vektor3d 网格能力就绪后填能力 ID
    mesh_engine: str = "manual"
    mesh_params: Optional[Dict] = None
    mesh_file: Optional[str] = None
    quality: Optional[Dict] = None


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    subject_type: str
    solver_type: str
    schema_def: Optional[Dict] = Field(default=None, alias="schema")
    default_values: Optional[Dict] = None
    validation_rules: Optional[Dict] = None
    export_mapping: Optional[Dict] = None

    model_config = {"populate_by_name": True}


class SubjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    subject_type: str
    solver_type: str
    template_id: Optional[str] = None
    sim_mesh_version_id: Optional[str] = None
    config: Optional[Dict] = None


class SubjectUpdate(BaseModel):
    name: Optional[str] = None
    template_id: Optional[str] = None
    sim_mesh_version_id: Optional[str] = None
    config: Optional[Dict] = None
    status: Optional[str] = None


class JobCreate(BaseModel):
    submit_mode: str = "pbs"
    sim_mesh_version_id: Optional[str] = None
    submit_payload: Optional[Dict] = None


class ResultCreate(BaseModel):
    result_type: str
    file_path: str
    meta: Optional[Dict] = None


# --- 项目 ---------------------------------------------------------------

@router.get("/projects")
def list_projects(
    request: Request,
    status_filter: Optional[str] = None,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    rows = db.list_projects(owner=None if is_admin else user, status=status_filter)
    out = []
    for r in rows:
        item = _row(r)
        item["stats"] = db.project_stats(r["id"])
        out.append(item)
    return out


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(
    request: Request,
    body: ProjectCreate,
    user: str = Depends(current_user),
) -> Dict:
    db = _db(request)
    pid = db.create_project(
        name=body.name,
        owner=user,
        description=body.description,
        dbit_project_code=body.dbit_project_code,
        default_solver=body.default_solver,
        unit_system=body.unit_system,
        workdir=body.workdir,
    )
    log.info("创建仿真项目 id=%s owner=%s name=%s", pid, user, body.name)
    return _row(db.get_project(pid))


@router.get("/projects/{pid}")
def get_project(
    request: Request,
    pid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    item = _row(_owned_project(db, pid, user, is_admin))
    item["stats"] = db.project_stats(pid)
    return item


@router.patch("/projects/{pid}")
def update_project(
    request: Request,
    pid: str,
    body: ProjectUpdate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    _owned_project(db, pid, user, is_admin)
    db.update_project(pid, **body.model_dump(exclude_none=True))
    return _row(db.get_project(pid))


@router.delete("/projects/{pid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    request: Request,
    pid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> None:
    db = _db(request)
    _owned_project(db, pid, user, is_admin)
    db.delete_project(pid)
    log.info("删除仿真项目 id=%s by=%s", pid, user)


# --- 分析对象与几何/网格 -------------------------------------------------

@router.get("/projects/{pid}/targets")
def list_targets(
    request: Request,
    pid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    _owned_project(db, pid, user, is_admin)
    return [_row(r, TARGET_JSON) for r in db.list_targets(pid)]


@router.post("/projects/{pid}/targets", status_code=status.HTTP_201_CREATED)
def create_target(
    request: Request,
    pid: str,
    body: TargetCreate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    _owned_project(db, pid, user, is_admin)
    tid = db.create_target(pid, body.name, body.target_type, body.source_ref)
    return _row(db.get_target(tid), TARGET_JSON)


@router.delete("/targets/{tid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_target(
    request: Request,
    tid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> None:
    db = _db(request)
    _owned_target(db, tid, user, is_admin)
    db.delete_target(tid)


@router.get("/targets/{tid}/geometries")
def list_geometries(
    request: Request,
    tid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    _owned_target(db, tid, user, is_admin)
    return [_row(r, GEOM_JSON) for r in db.list_geometries(tid)]


@router.post("/targets/{tid}/geometries", status_code=status.HTTP_201_CREATED)
def add_geometry(
    request: Request,
    tid: str,
    body: GeometryCreate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    _owned_target(db, tid, user, is_admin)
    gid = db.add_geometry(
        tid, body.source_type, body.source_file, body.step_file,
        body.brep_file, body.lightweight_file, body.topo_summary,
    )
    return _row(db.get_geometry(gid), GEOM_JSON)


@router.get("/geometries/{gid}/meshes")
def list_meshes(
    request: Request,
    gid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    _owned_geometry(db, gid, user, is_admin)
    return [_row(r, MESH_JSON) for r in db.list_meshes(gid)]


@router.post("/geometries/{gid}/meshes", status_code=status.HTTP_201_CREATED)
def add_mesh(
    request: Request,
    gid: str,
    body: MeshCreate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """登记一个网格版本。

    一期 mesh_engine=manual（人工上传网格文件）；vektor3d 的 mesh.generate
    能力就绪后由编排节点调用同一接口写入，此处无需改动。
    """
    db = _db(request)
    _owned_geometry(db, gid, user, is_admin)
    mid = db.add_mesh(
        gid, body.mesh_type, body.mesh_engine, body.mesh_params,
        body.mesh_file, body.quality,
    )
    return _row(db.get_mesh(mid), MESH_JSON)


# --- 工况模板 -----------------------------------------------------------

@router.get("/templates")
def list_templates(
    request: Request,
    subject_type: Optional[str] = None,
    solver_type: Optional[str] = None,
    user: str = Depends(current_user),
) -> List[Dict]:
    """模板是组织级资产，不做属主隔离（与现有提交模板一致）。"""
    db = _db(request)
    return [_row(r, TEMPLATE_JSON)
            for r in db.list_templates(subject_type, solver_type)]


@router.post("/templates", status_code=status.HTTP_201_CREATED)
def create_template(
    request: Request,
    body: TemplateCreate,
    user: str = Depends(current_user),
) -> Dict:
    db = _db(request)
    tid = db.create_template(
        body.name, body.subject_type, body.solver_type,
        body.schema_def, body.default_values,
        body.validation_rules, body.export_mapping,
    )
    return _row(db.get_template(tid), TEMPLATE_JSON)


@router.get("/templates/{tid}")
def get_template(
    request: Request,
    tid: str,
    user: str = Depends(current_user),
) -> Dict:
    db = _db(request)
    row = db.get_template(tid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工况模板不存在")
    return _row(row, TEMPLATE_JSON)


@router.delete("/templates/{tid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    request: Request,
    tid: str,
    user: str = Depends(current_user),
) -> None:
    db = _db(request)
    if not db.delete_template(tid):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "模板不存在或为内置模板，不可删除")


# --- 工况 ---------------------------------------------------------------

@router.get("/projects/{pid}/subjects")
def list_subjects(
    request: Request,
    pid: str,
    status_filter: Optional[str] = None,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    _owned_project(db, pid, user, is_admin)
    return [_row(r, SUBJECT_JSON) for r in db.list_subjects(pid, status_filter)]


@router.post("/projects/{pid}/subjects", status_code=status.HTTP_201_CREATED)
def create_subject(
    request: Request,
    pid: str,
    body: SubjectCreate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    _owned_project(db, pid, user, is_admin)
    sid = db.create_subject(
        pid, body.name, body.subject_type, body.solver_type,
        body.template_id, body.sim_mesh_version_id, body.config,
    )
    return _row(db.get_subject(sid), SUBJECT_JSON)


@router.get("/subjects/{sid}")
def get_subject(
    request: Request,
    sid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    return _row(_owned_subject(db, sid, user, is_admin), SUBJECT_JSON)


@router.patch("/subjects/{sid}")
def update_subject(
    request: Request,
    sid: str,
    body: SubjectUpdate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    _owned_subject(db, sid, user, is_admin)
    fields = body.model_dump(exclude_none=True)
    if "config" in fields:
        fields["config_json"] = json.dumps(fields.pop("config"), ensure_ascii=False)
    db.update_subject(sid, **fields)
    return _row(db.get_subject(sid), SUBJECT_JSON)


@router.delete("/subjects/{sid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_subject(
    request: Request,
    sid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> None:
    db = _db(request)
    _owned_subject(db, sid, user, is_admin)
    db.delete_subject(sid)


# --- 作业与结果 ---------------------------------------------------------

@router.get("/subjects/{sid}/jobs")
def list_jobs(
    request: Request,
    sid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    _owned_subject(db, sid, user, is_admin)
    return [_row(r, JOB_JSON) for r in db.list_jobs(sid)]


@router.post("/subjects/{sid}/jobs", status_code=status.HTTP_201_CREATED)
def create_job(
    request: Request,
    sid: str,
    body: JobCreate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """登记一个仿真作业（draft）。

    此处只建立编排侧记录，**不提交到 PBS**——真正的提交仍走现有 HPC 链路，
    成功后回填 hpc_jobid。两者的衔接由后续的 pipeline 编排节点负责。
    """
    db = _db(request)
    _owned_subject(db, sid, user, is_admin)
    jid = db.create_job(sid, body.submit_mode, body.sim_mesh_version_id,
                        body.submit_payload)
    return _row(db.get_job(jid), JOB_JSON)


@router.get("/projects/{pid}/jobs")
def list_project_jobs(
    request: Request,
    pid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    _owned_project(db, pid, user, is_admin)
    return [_row(r, JOB_JSON) for r in db.list_jobs_by_project(pid)]


@router.get("/jobs/{jid}")
def get_job(
    request: Request,
    jid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    return _row(_owned_job(db, jid, user, is_admin), JOB_JSON)


@router.get("/jobs/{jid}/results")
def list_results(
    request: Request,
    jid: str,
    result_type: Optional[str] = None,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    _owned_job(db, jid, user, is_admin)
    return [_row(r, RESULT_JSON) for r in db.list_results(jid, result_type)]


@router.post("/jobs/{jid}/results", status_code=status.HTTP_201_CREATED)
def add_result(
    request: Request,
    jid: str,
    body: ResultCreate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    _owned_job(db, jid, user, is_admin)
    rid = db.add_result(jid, body.result_type, body.file_path, body.meta)
    rows = [r for r in db.list_results(jid) if r["id"] == rid]
    return _row(rows[0], RESULT_JSON)


@router.get("/projects/{pid}/results")
def list_project_results(
    request: Request,
    pid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    """项目下全部结果。前端结果查看页按 result_type 分发到对应查看器插件。"""
    db = _db(request)
    _owned_project(db, pid, user, is_admin)
    return [_row(r, RESULT_JSON) for r in db.list_results_by_project(pid)]
