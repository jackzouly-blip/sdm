"""仿真设计路由。

属主隔离与现有 jobs 路由一致：普通用户只能看到/操作自己的仿真项目，
管理员（is_admin_request）可跨用户查看。所有下级实体（分析对象/几何/网格/
工况/作业/结果）的权限都由其所属项目的 owner 决定——单一判据，不重复实现。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from ..auth.session import (
    current_user,
    current_user_query,
    is_admin_request,
    issue_scoped_token,
    principal_is_admin,
    resolve_multi_scoped_principal,
    resolve_scoped_principal,
)
from ..config import get_settings
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
GEOM_JSON = ("source_file_json", "topo_summary_json",
             "part_inventory_json", "mesh_strategy_json")
MESH_JSON = ("mesh_params_json", "quality_json", "source_mesh_ids_json")
TEMPLATE_JSON = ("schema_json", "default_values_json",
                 "validation_rules_json", "export_mapping_json")
SUBJECT_JSON = ("config_json",)
JOB_JSON = ("submit_payload_json",)
RESULT_JSON = ("meta_json",)
REQUIREMENT_JSON = ("source_file_json", "analysis_json")
QUALITY_CARD_JSON = ("overrides_json",)
ITEM_JSON = ("metrics_json",)

# 项目没指定质量卡模板时的缺省底座(内置只读的那张碰撞通用 5mm 卡)
DEFAULT_QUALITY_TEMPLATE = "generic-crash-5mm"


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


def _owned_mesh(db: SimDB, mid: str, user: str, is_admin: bool) -> sqlite3.Row:
    row = db.get_mesh(mid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "网格版本不存在")
    _owned_geometry(db, row["sim_geometry_version_id"], user, is_admin)
    return row


def _owned_requirement(db: SimDB, rid: str, user: str, is_admin: bool) -> sqlite3.Row:
    row = db.get_requirement_doc(rid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "需求文档不存在")
    _owned_project(db, row["sim_project_id"], user, is_admin)
    return row


def _owned_quality_card(db: SimDB, qid: str, user: str, is_admin: bool) -> sqlite3.Row:
    row = db.get_quality_card(qid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "质量卡不存在")
    _owned_project(db, row["sim_project_id"], user, is_admin)
    return row


def _quality_templates_root() -> str:
    """全局用户模板目录。放在 scratch 之外的固定位置:模板是长期资产,
    不能跟着临时目录被清掉。"""
    return os.path.join(os.path.dirname(get_settings().db_path), "quality-templates")


def _quality_library(request: Request):
    """模板库句柄。用户模板落数据目录,内置模板随代码走。"""
    from .quality import QualityCardLibrary

    return QualityCardLibrary(user_dir=_quality_templates_root())


def _quality_card_detail(card) -> Dict:
    """把一张卡摊平成前端能直接渲染的结构。

    criteria 只含启用的 shells 判据——它是"这张卡在管什么"的答案,别把关掉的
    混进去让人以为那些也在管。但整份卡远不止这 11 行:allCriteria 铺出全部判据
    (含停用与 solids 域),meshGroups 铺出生成侧全部参数(按 .ansa_mpar 的分节
    分组)——否则用户会以为这张卡就只有一屏,而其余内容其实都会交给 ANSA。
    """
    from .quality import estimate_time_step

    def _criterion(c) -> Dict:
        return {
            "name": c.name,
            "domain": c.domain,
            "enabled": c.enabled,
            "calculation": c.calculation,       # 算法族:同一指标不同算法数值不同
            "weight": c.weight,
            "higherIsBetter": c.higher_is_better,
            "thresholds": c.thresholds,
        }

    criteria = [_criterion(c) for c in card.criteria.enabled_criteria("shells")]
    all_criteria = [_criterion(c) for c in card.criteria.criteria]

    # 生成侧参数按文件里的分节分组,保持文件顺序——这些段正是网格生成侧的
    # 旋钮分类。值保留原始字符串(数字/布尔/枚举/表达式混杂,解析了就毁了)。
    mesh_groups: List[Dict] = []
    by_title: Dict[str, Dict] = {}
    for key, value in card.mesh_params.values.items():
        title = card.mesh_params.sections.get(key, "")
        grp = by_title.get(title)
        if grp is None:
            grp = by_title[title] = {"title": title, "params": []}
            mesh_groups.append(grp)
        grp["params"].append({"key": key, "value": value})
    min_len = card.criteria.get("min length")
    failed_len = (min_len.thresholds.get("failed") if min_len else None) or 0.0
    return {
        "id": card.id,
        "name": card.name,
        "source": card.source,
        "revision": card.revision,
        "scope": card.scope,
        "description": card.description,
        "builtin": card.builtin,
        "ansaVersion": card.criteria.ansa_version,
        "criteria": criteria,
        "allCriteria": all_criteria,
        "meshGroups": mesh_groups,
        "meshParams": {
            "targetElementLength": card.target_element_length,
            "minTargetLength": card.mesh_params.get_float("general_min_target_len"),
            "maxTargetLength": card.mesh_params.get_float("general_max_target_len"),
            "elementType": card.mesh_params.values.get("element_type", ""),
            "featureHandling": card.mesh_params.values.get("bm_features_handling", ""),
        },
        # 判废线上的最小单元长度对应的显式时间步——碰撞里这才是机时的总闸
        "timeStepAtFailedMinLength": estimate_time_step(failed_len),
    }


def _overrides_from_request(raw_overrides: List[Dict], user: str):
    """把请求体里的覆盖项转成 Override,并强制每项都带依据(source)。

    old_value 留空:真实旧值由引擎在落盘时回填,不信调用方给的。
    """
    from .quality import Override

    out = []
    for raw in raw_overrides:
        if not str(raw.get("source") or "").strip():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"覆盖项 {raw.get('target')} 缺少依据(source)——无出处的阈值会一路流进网格验收",
            )
        out.append(Override(
            target=str(raw.get("target") or ""),
            old_value="",
            new_value=str(raw.get("new_value") or ""),
            source=str(raw.get("source") or ""),
            by=str(raw.get("by") or user),
        ))
    return out


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
    mesh_type: str                          # surface/volume/midsurface
    # manual（人工上传）或能力 ID（如 vektor3d:mesh.generate）
    mesh_engine: str = "manual"
    mesh_params: Optional[Dict] = None
    mesh_file: Optional[str] = None
    quality: Optional[Dict] = None
    # generating = 已登记、产物还没回来（能力作业动辄几十分钟，页面要能显示进度）
    status: str = "ready"
    part_filter: Optional[str] = None       # 逐零件生成时的零件名
    source_mesh_ids: Optional[List[str]] = None  # 合并回装的来源网格


class MeshUpdate(BaseModel):
    status: Optional[str] = None            # generating/ready/failed/checked-out
    quality: Optional[Dict] = None


class MeshCheckout(BaseModel):
    checkout_id: str = Field(min_length=1, description="vektor3d mesh.checkout 返回的检出标识")


class GeometryAnalysis(BaseModel):
    part_inventory: Optional[List] = None   # mesh.inventory 的 parts[]
    mesh_strategy: Optional[Dict] = None    # mesh.classify 的完整产出


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
    # 未显式指定则按系统配置派生。建项目时就定下来,而不是等到导入数模才报
    # "未设置工作目录"——那时用户已经选好文件了,再回头去配是最差的顺序。
    if not (body.workdir or "").strip():
        db.update_project(pid, workdir=derive_workdir(user, pid))
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
    target = _owned_target(db, tid, user, is_admin)
    _reject_if_bound_subjects(db.subjects_using_target(tid), "分析对象")
    # 托管目录先于删行取路径：行删了就查不到项目/工作目录了
    managed = _geometry_dir(db, target)
    db.delete_target(tid)
    _cleanup_managed_dir(managed)
    log.info("删除分析对象 tid=%s name=%s by=%s", tid, target["name"], user)


def _reject_if_bound_subjects(rows: List[sqlite3.Row], what: str) -> None:
    """有工况绑定网格版本时拒删，把该解绑的工况点名给用户。

    不做静默解绑：工况绑哪个网格是业务决策，删几何不该顺手改工况；
    也不放任直删——sim_subject 的外键会拦下它，但用户只会看到一句
    'FOREIGN KEY constraint failed'。"""
    if rows:
        names = "、".join(sorted({str(r["name"]) for r in rows}))
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"工况「{names}」正在引用该{what}下的网格版本，"
            f"请先在工况里解除网格绑定（或删除该工况）后再删。",
        )


def _cleanup_managed_dir(path: str) -> None:
    """尽力清掉一个托管几何目录（<workdir>/sdm_geometry/<tid>）。

    行已删、文件残留可由巡检兜底，所以文件系统故障不该让请求以 500 收场；
    但路径必须落在 sdm_geometry 下——这是"只删自家托管产物"的最后一道保险。"""
    if "sdm_geometry" not in os.path.normpath(path).split(os.sep):
        log.warning("拒绝清理非托管目录: %s", path)
        return
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
    except OSError as e:
        log.warning("清理托管几何目录失败（不影响删除本身）: %s (%s)", path, e)


def _cleanup_managed_files(base: str, paths: List[str]) -> None:
    """删除登记在案、且位于托管目录 base 之内的产物文件。

    from-path 登记的集群源文件在 base 之外，天然被这条边界排除——
    它们是用户的数据，登记只是引用，删除版本绝不能反过来删数据。
    删完顺手移走空掉的上传子目录（upload 的 <slot>/ 隔离层）。"""
    base_abs = os.path.abspath(base)
    for p in paths:
        if not p:
            continue
        ap = os.path.abspath(p)
        if not ap.startswith(base_abs + os.sep):
            continue
        try:
            if os.path.isfile(ap):
                os.remove(ap)
        except OSError as e:
            log.warning("清理几何产物失败（不影响删除本身）: %s (%s)", ap, e)
    try:
        if os.path.isdir(base_abs):
            for d in os.listdir(base_abs):
                full = os.path.join(base_abs, d)
                if os.path.isdir(full) and not os.listdir(full):
                    os.rmdir(full)
    except OSError:
        pass


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


def derive_workdir(owner: str, pid: str) -> str:
    """按系统配置派生项目工作目录：<HPC_SIM_WORKDIR_ROOT>/<属主>/<项目 id>。

    用项目 id 而非项目名:改名不该搬目录,重名也不该撞车。可读性由页面负责
    (详情页把完整路径显示出来),不靠路径本身。
    """
    base = get_settings().sim_workdir_base_dir
    return os.path.join(base, owner or "shared", pid)


def _project_workdir(db: SimDB, proj: sqlite3.Row) -> str:
    """取项目工作目录;没有就按系统配置派生并回写。

    惰性补齐是为了老项目——它们建于"workdir 靠人工填"的年代,大多是空的。
    与其要求做一次数据迁移、或让用户自己去猜一个集群路径,不如在真正要用到
    的这一刻按同一规则算出来并落库,此后就与新项目完全一致。
    """
    existing = str(proj["workdir"] or "").strip()
    if existing:
        return existing
    derived = derive_workdir(proj["owner"], proj["id"])
    db.update_project(proj["id"], workdir=derived)
    log.info("项目 %s 未设工作目录,已按系统配置派生: %s", proj["id"], derived)
    return derived


def _geometry_dir(db: SimDB, target: sqlite3.Row) -> str:
    """几何文件的存放目录：<项目 workdir>/sdm_geometry/<分析对象 id>。

    放在项目工作目录下而非另辟存储：仿真的输入输出本就都在集群共享盘上，
    单独再建一套存储会让备份、配额、清理各有一套口径。
    """
    proj = db.get_project(target["sim_project_id"])
    if proj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "仿真项目不存在")
    return os.path.join(_project_workdir(db, proj), "sdm_geometry", target["id"])


@router.post("/targets/{tid}/geometries/upload", status_code=status.HTTP_201_CREATED)
async def upload_geometry(
    request: Request,
    tid: str,
    file: UploadFile = File(...),
    source_type: str = Form("upload"),
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """上传 CAD 数模，建立一个几何版本。

    以项目属主身份落盘（作业要以真实用户身份读它）。每次上传落在独立子目录下，
    因此同名文件重复上传不会互相覆盖——write_file 用 O_EXCL 拒绝覆盖，
    不加隔离的话第二次上传必失败。

    CAD 原生格式（CATIA/STEP/JT…）此时只做归档；轻量化由 vektor3d 的
    geometry.convert 能力产出后经 /geometries/{gid}/lightweight 回传。
    """
    from ..fs.browser import FsError, write_file

    db = _db(request)
    target = _owned_target(db, tid, user, is_admin)
    proj = db.get_project(target["sim_project_id"])
    base = _geometry_dir(db, target)
    owner = proj["owner"]

    name = os.path.basename((file.filename or "").replace("\\", "/")) or "model.dat"
    slot = uuid.uuid4().hex[:8]
    data = await file.read()
    try:
        written = write_file(owner, os.path.join(base, slot), name,
                             data, get_settings().fs_root_list)
    except FsError as e:
        raise HTTPException(e.status, f"写入几何文件失败: {e.message}")

    gid = db.add_geometry(
        tid,
        source_type=source_type,
        source_file={"name": name, "size": len(data), "path": written["path"]},
        step_file=written["path"] if name.lower().endswith((".stp", ".step")) else None,
    )
    log.info("上传几何 target=%s gid=%s file=%s (%d 字节)", tid, gid, name, len(data))
    return _row(db.get_geometry(gid), GEOM_JSON)


class GeometryFromPath(BaseModel):
    """从集群已有文件建几何版本。

    CAD 数模走浏览器上传即可，但求解器 deck 不行：主控 .key 会牵出几百 MB
    的 include 树、散在集群目录里，浏览器传不上来。而它本就在集群上。
    """
    path: str = Field(min_length=1, description="集群上的绝对路径")
    source_type: str = "cluster"
    # LS-DYNA deck 会自动转成 GLB 供网页渲染；其余格式仅登记
    convert: bool = True
    max_triangles: Optional[int] = None


# 会被当作 LS-DYNA deck 解析的扩展名
_DECK_EXT = (".k", ".key", ".kinc", ".dyn")


@router.post("/targets/{tid}/geometries/from-path",
             status_code=status.HTTP_201_CREATED)
def add_geometry_from_path(
    request: Request,
    tid: str,
    body: GeometryFromPath,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """把集群上已有的文件登记为几何版本；deck 则顺带派发 GLB 转换。

    只登记路径、不拷贝文件——deck 的 include 树可达数百 MB 且随作业目录演进，
    复制一份只会立刻过期，还会平白占一倍空间。
    """
    from ..fs.browser import FsError, stat_path

    db = _db(request)
    target = _owned_target(db, tid, user, is_admin)
    proj = db.get_project(target["sim_project_id"])

    # 以登录用户身份校验：既确认存在，也确保他确实有权读这个路径
    try:
        info = stat_path(user, body.path, get_settings().fs_root_list)
    except FsError as e:
        raise HTTPException(e.status, f"路径不可用: {e.message}")
    if info["is_dir"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请指定文件而非目录")

    path = info["path"]
    name = os.path.basename(path)
    gid = db.add_geometry(
        tid,
        source_type=body.source_type,
        source_file={"name": name, "size": info.get("size") or 0, "path": path},
        step_file=path if name.lower().endswith((".stp", ".step")) else None,
    )

    task_id = None
    if body.convert and name.lower().endswith(_DECK_EXT):
        tm = getattr(request.app.state, "task_manager", None)
        if tm is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "任务管理器不可用")
        out = os.path.join(
            proj["workdir"] or os.path.dirname(path),
            "sdm_geometry", tid, f"{gid}.glb",
        ) if proj["workdir"] else os.path.join(os.path.dirname(path), f".sdm_{gid}.glb")
        params = {"deck_path": path, "out_path": out, "gid": gid, "run_as": user}
        if body.max_triangles is not None:
            params["max_triangles"] = body.max_triangles
        task_id = tm.submit("sim_deck_convert", owner=user, params=params)
        log.info("派发 deck 转换 gid=%s task=%s deck=%s", gid, task_id, path)

    out = _row(db.get_geometry(gid), GEOM_JSON)
    out["convert_task_id"] = task_id
    return out


# ── 几何转换票据 ──────────────────────────────────────────────────────────────
#
# vektor3d 跑在用户桌面上，只监听 localhost，集群**永远调不通它**；反过来桌面能访问
# 集群。所以调用由浏览器发起，而源文件的拉取与产物的回传是 vektor3d 直连本服务完成的
# ——它需要一份能访问这两个端点的凭据。
#
# 那份凭据不能是用户的会话 JWT（8 小时、全部接口）。这里签一个 scope=geometry.convert、
# 绑定单个 gid、十分钟过期的受限令牌：即便泄露，能做的也只有"读这一个源文件、
# 写这一个产物"。契约第 5 节列的"短期令牌待补"即指此项。

GEOMETRY_CONVERT_SCOPE = "geometry.convert"
# 需求文档分析票据。与几何票据同一套机制,但绑的是 rid ——一张票据只能读一份文档,
# 换个 rid 就 403。不共用一个 scope 是刻意的:几何票据能写产物,这张只能读。
DOC_ANALYZE_SCOPE = "doc.analyze"
# AI 会话只读票据:绑单个项目,只在枚举的只读接口上有效(见会话契约第 3 节)。
# 它会长期、高频地出现在桌面进程里,泄露半径必须一开始就钉死为
# "这一个项目的只读视图"——写永远只有提案-确认一条路。
AI_READ_SCOPE = "ai.read"
# 网格作业票据。绑单个网格版本 mid,可读该网格的输入(几何源文件 / .ansa 正本)
# 与写它的产物。网格链比几何链多两个特点,故单独一个 scope 而不复用几何票据:
# ① 产物有四类(.ansa 正本 / 求解器文件 / 预览 GLB / 质量报告),几何票据只认 glb;
# ② mesh.merge 要同时拉多个零件的 .ansa —— 用 mid 列表绑定(见 bindings.mids)。
MESH_JOB_SCOPE = "mesh.job"
# 模板解析票据。绑 kind + tid ——只能 GET 那一份模板的导出正文，别的什么都干不了。
# 用途单一到不必给写权限:解析结果由浏览器带着常规用户令牌 PATCH 回来,不走票据。
# 之所以仍要签票据而不把正文直接塞给能力:vektor3d 的 cae.template.parse 契约是
# sourceUrl 自取(与 geometry/mesh 一致),让能力侧统一按 URL 拉,少一条特例路径。
CAE_TEMPLATE_SCOPE = "cae.template"
# 气囊网格化票据。绑单个几何版本 gid:可读该版本的 .igs 平面图,可回传生成的 deck。
# 不复用 mesh.job 是因为落点完全不同 —— mesh.job 往某个网格版本挂产物,
# 这条链产出的是**一个新的几何版本**,两者的权限边界不该混在一个 scope 里。
AIRBAG_JOB_SCOPE = "airbag.job"
_bearer = HTTPBearer(auto_error=False)


def _geometry_principal(
    gid: str,
    token: Optional[str] = Query(None),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    act_as: Optional[str] = Header(None, alias="X-Act-As-User"),
) -> str:
    """几何源文件/产物端点的调用者。

    既接受常规用户令牌（页面自己下载源文件、加载产物），也接受本 gid 的转换票据
    （vektor3d 拉取与回传）。票据换到别的 gid 或别的接口一律 401/403。
    """
    raw = token or (creds.credentials if creds else None)
    return resolve_scoped_principal(
        raw, act_as, scope=GEOMETRY_CONVERT_SCOPE, bindings={"gid": gid}
    )


def _geometry_or_mesh_principal(
    gid: str,
    token: Optional[str] = Query(None),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    act_as: Optional[str] = Header(None, alias="X-Act-As-User"),
) -> str:
    """几何源文件端点的调用者(比 _geometry_principal 多接受网格作业票据)。

    网格能力同样要拉这份源文件(mesh.generate 的输入就是 CAD 原件),
    不该逼 SDM 为一次网格作业再签一张几何票据——那会把两条链的过期时间
    绑在一起,网格作业动辄几十分钟,几何票据 30 分钟根本不够。
    """
    raw = token or (creds.credentials if creds else None)
    return resolve_multi_scoped_principal(raw, act_as, allow_scopes={
        GEOMETRY_CONVERT_SCOPE: {"gid": gid},
        MESH_JOB_SCOPE: {"gid": gid},
        # 气囊网格化同样要拉这份源文件(.igs 平面图就是它的输入)
        AIRBAG_JOB_SCOPE: {"gid": gid},
    })


def _mesh_principal(
    gid: str,
    token: Optional[str] = Query(None),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    act_as: Optional[str] = Header(None, alias="X-Act-As-User"),
) -> str:
    """网格产物端点的调用者:常规用户令牌,或本 gid 的网格作业票据。"""
    raw = token or (creds.credentials if creds else None)
    return resolve_scoped_principal(
        raw, act_as, scope=MESH_JOB_SCOPE, bindings={"gid": gid}
    )


def _mesh_is_admin(
    user: str = Depends(_mesh_principal),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    portal_admin: Optional[str] = Header(None, alias="X-Hpc-Portal-Admin"),
) -> bool:
    """同 _geometry_is_admin,但主体由 _mesh_principal 解析(理由同前:
    is_admin_request 依赖 current_user,而 current_user 拒绝一切受限票据)。"""
    return principal_is_admin(user, creds.credentials if creds else None, portal_admin)


def _material_template_principal(
    tid: str,
    token: Optional[str] = Query(None),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    act_as: Optional[str] = Header(None, alias="X-Act-As-User"),
) -> str:
    """材料模板导出端点的调用者:常规用户令牌,或本模板的解析票据。

    kind 一并绑定:材料与控制卡的 id 各自独立,不绑 kind 的话一张材料票据能拿去
    读同名 id 的控制卡。两者都是组织级资产、泄露危害有限,但票据的价值就在于
    "能做的只有那一件事",这里不留缺口。
    """
    raw = token or (creds.credentials if creds else None)
    return resolve_scoped_principal(
        raw, act_as, scope=CAE_TEMPLATE_SCOPE, bindings={"tid": tid, "kind": "material"}
    )


def _control_template_principal(
    tid: str,
    token: Optional[str] = Query(None),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    act_as: Optional[str] = Header(None, alias="X-Act-As-User"),
) -> str:
    """控制卡模板导出端点的调用者。同上,kind 绑 control。"""
    raw = token or (creds.credentials if creds else None)
    return resolve_scoped_principal(
        raw, act_as, scope=CAE_TEMPLATE_SCOPE, bindings={"tid": tid, "kind": "control"}
    )


def _geometry_is_admin(
    user: str = Depends(_geometry_principal),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    portal_admin: Optional[str] = Header(None, alias="X-Hpc-Portal-Admin"),
) -> bool:
    """同 is_admin_request，但主体由 _geometry_principal 解析。

    不能直接复用 is_admin_request：它依赖 current_user，而 current_user 按设计
    拒绝一切受限票据——那样 vektor3d 拿票据来下载会在这一步就被 401。
    """
    return principal_is_admin(user, creds.credentials if creds else None, portal_admin)


class ConvertTicket(BaseModel):
    """交给浏览器、再由浏览器转交 vektor3d 的一次性转换票据。

    URL 由**前端**用 window.location.origin 拼，不在这里生成：后端看到的 base_url
    在开发期是 127.0.0.1:8000（浏览器实际访问 5173 的 vite 代理）、生产期是 nginx
    反代后的内网地址，都不等于浏览器/桌面真正能访问到的地址。浏览器最清楚自己
    是从哪个地址进来的，而 vektor3d 与浏览器在同一台机器上。
    """
    gid: str
    token: str
    expires_in: int
    source_name: str
    source_path_suffix: str = Field(description="源文件下载路径（相对 /api）")
    upload_path_suffix: str = Field(description="产物回传路径（相对 /api）")


@router.post("/geometries/{gid}/convert-ticket")
def create_convert_ticket(
    request: Request,
    gid: str,
    ttl_seconds: int = Query(1800, ge=60, le=7200),
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> ConvertTicket:
    """为一次几何转换签发受限令牌。

    只有常规用户令牌能调它——票据不能自我续签，否则十分钟有效期形同虚设
    （`current_user` 会拒绝任何带 scp 的令牌）。
    """
    db = _db(request)
    row = _owned_geometry(db, gid, user, is_admin)
    src = _json_or_none(row["source_file_json"]) or {}
    if not src.get("path"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该几何版本没有源文件，无法转换")

    token = issue_scoped_token(user, GEOMETRY_CONVERT_SCOPE, ttl_seconds, gid=gid)
    log.info("签发几何转换票据 gid=%s user=%s ttl=%ss", gid, user, ttl_seconds)
    return ConvertTicket(
        gid=gid,
        token=token,
        expires_in=ttl_seconds,
        source_name=src.get("name") or os.path.basename(src["path"]),
        source_path_suffix=f"/sim/geometries/{gid}/download",
        upload_path_suffix=f"/sim/geometries/{gid}/lightweight",
    )


def _geometry_or_mesh_is_admin(
    user: str = Depends(_geometry_or_mesh_principal),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    portal_admin: Optional[str] = Header(None, alias="X-Hpc-Portal-Admin"),
) -> bool:
    return principal_is_admin(user, creds.credentials if creds else None, portal_admin)


@router.get("/geometries/{gid}/download")
def download_geometry(
    request: Request,
    gid: str,
    user: str = Depends(_geometry_or_mesh_principal),
    is_admin: bool = Depends(_geometry_or_mesh_is_admin),
):
    """下载几何源文件。

    契约里 vektor3d 就是通过这个地址拉取源文件的（见
    docs/vektor3d-geometry-capability-contract.md）。凭据可以是常规用户令牌
    （query 或 Authorization 头——<a href> 直链下载设不了头），也可以是本 gid 的
    转换票据。
    """
    from fastapi.responses import FileResponse

    db = _db(request)
    row = _owned_geometry(db, gid, user, is_admin)
    src = _json_or_none(row["source_file_json"]) or {}
    path = src.get("path")
    if not path or not os.path.isfile(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "几何源文件不存在或已被删除")
    return FileResponse(path, filename=src.get("name") or os.path.basename(path),
                        media_type="application/octet-stream")


class AirbagTicket(BaseModel):
    gid: str
    token: str
    expires_in: int
    source_name: str
    source_path_suffix: str
    deck_path_suffix: str


@router.post("/geometries/{gid}/airbag-ticket")
def create_airbag_ticket(
    request: Request,
    gid: str,
    ttl_seconds: int = Query(3600, ge=60, le=28800),
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> AirbagTicket:
    """为气囊网格化签票据（读本版本的 .igs，回传生成的 deck）。

    1 小时：实测 5P-BAG 约 70 秒，但大图与排队都可能拖长，且票据过期会让
    回传阶段功亏一篑。只有常规用户令牌能调它——票据不能自我续签。
    """
    db = _db(request)
    row = _owned_geometry(db, gid, user, is_admin)
    src = _json_or_none(row["source_file_json"]) or {}
    if not src.get("path"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该几何版本没有源文件")
    name = src.get("name") or os.path.basename(src["path"])
    if not name.lower().endswith((".igs", ".iges")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"气囊网格化的输入必须是 IGES 平面展开图（当前 {name}）")

    token = issue_scoped_token(user, AIRBAG_JOB_SCOPE, ttl_seconds, gid=gid)
    log.info("签发气囊网格票据 gid=%s user=%s ttl=%ss", gid, user, ttl_seconds)
    return AirbagTicket(
        gid=gid, token=token, expires_in=ttl_seconds, source_name=name,
        source_path_suffix=f"/sim/geometries/{gid}/download",
        deck_path_suffix=f"/sim/geometries/{gid}/airbag-deck",
    )


def _airbag_principal(
    gid: str,
    token: Optional[str] = Query(None),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    act_as: Optional[str] = Header(None, alias="X-Act-As-User"),
) -> str:
    raw = token or (creds.credentials if creds else None)
    return resolve_scoped_principal(raw, act_as, scope=AIRBAG_JOB_SCOPE,
                                    bindings={"gid": gid})


def _airbag_is_admin(
    user: str = Depends(_airbag_principal),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    portal_admin: Optional[str] = Header(None, alias="X-Hpc-Portal-Admin"),
) -> bool:
    return principal_is_admin(user, creds.credentials if creds else None, portal_admin)


@router.post("/geometries/{gid}/airbag-deck", status_code=status.HTTP_201_CREATED)
async def upload_airbag_deck(
    request: Request,
    gid: str,
    file: UploadFile = File(...),
    meta: Optional[str] = Form(None, description="能力回传的元数据（节点/单元数、体积、非流形边）"),
    convert: bool = Query(True, description="落库后顺带触发 deck→GLB 预览转换"),
    user: str = Depends(_airbag_principal),
    is_admin: bool = Depends(_airbag_is_admin),
) -> Dict:
    """接收 vektor3d 生成的 deck，**登记成一个新的几何版本**。

    不挂在平面图那条记录下面：平面图与 deck 是两个不同的东西，各自有版本、
    各自能被引用；而且登记成几何版本后，预览渲染直接复用现成的
    deck→GLB 链路（sim_deck_convert），不必为气囊单独做一套。
    """
    from ..fs.browser import FsError, write_file

    db = _db(request)
    src_geom = _owned_geometry(db, gid, user, is_admin)
    src = _json_or_none(src_geom["source_file_json"]) or {}
    if not src.get("path"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "源几何版本没有文件，无法定位落盘目录")

    target = db.get_target(src_geom["sim_target_id"])
    proj = db.get_project(target["sim_project_id"])
    parent = os.path.join(os.path.dirname(src["path"]), "airbag")

    name = os.path.basename((file.filename or "").replace("\\", "/")) or "airbag.k"
    if not name.lower().endswith((".k", ".key", ".dyn")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"deck 的扩展名必须是 .k/.key/.dyn（收到 {name}）")
    # 文件名带上源版本号；**同一个源反复生成**时再挂序号——每次生成都登记成一个
    # 独立的几何版本，各自的 deck 必须是独立文件，不能互相覆盖（只带源版本号时
    # 第二次生成会撞名，落盘直接 500）。
    prior = sum(1 for g in db.list_geometries(src_geom["sim_target_id"])
                if g["derived_from_id"] == gid and g["source_type"] == "deck")
    seq = f"-r{prior + 1}" if prior else ""
    name = f"airbag-v{src_geom['version_no']:02d}{seq}{os.path.splitext(name)[1]}"

    data = await file.read()
    try:
        written = write_file(proj["owner"], parent, name, data, get_settings().fs_root_list)
    except FsError as e:
        raise HTTPException(e.status, f"写入 deck 失败: {e.message}")

    info = {}
    if meta:
        try:
            info = json.loads(meta) or {}
        except ValueError:
            log.warning("气囊 deck 元数据不是合法 JSON，已忽略 gid=%s", gid)

    # —— 组装结算主控：控制卡/材料卡/起爆模板 + 本次网格 —— #
    # 模板放 SDM（用户定的分工）；与几何绑定的三张卡（SET_PART 1 控制体 /
    # SET_PART 2 全部件 / 展平参考几何）由 vektor3d 写在 deck 本体里。
    # 模板 replace 覆盖写：内容随部署更新、重复生成幂等；主控与 deck 同名
    # 前缀（airbag-vNN[-rM]-main.key），一次生成一套、互不覆盖。
    # 失败只降级不阻断——deck 本体照常登记，主控可以手动补。
    master_name = None
    try:
        tdir = os.path.join(os.path.dirname(__file__), "templates", "airbag")
        for fn in ("03_Control_card.k", "MAT.K", "inflator.k"):
            with open(os.path.join(tdir, fn), "rb") as fh:
                write_file(proj["owner"], parent, fn, fh.read(),
                           get_settings().fs_root_list, replace=True)
        with open(os.path.join(tdir, "main.key.tpl"), encoding="latin-1") as fh:
            tpl = fh.read()
        master_name = f"{os.path.splitext(name)[0]}-main.key"
        write_file(proj["owner"], parent, master_name,
                   tpl.replace("{bag}", name).encode("latin-1"),
                   get_settings().fs_root_list, replace=True)
        info["master_deck"] = master_name
        log.info("结算主控已组装 gid=%s master=%s（含控制/材料/起爆模板）",
                 gid, master_name)
    except (FsError, OSError) as e:
        log.warning("结算主控组装失败（deck 本体不受影响）: %s", e)
        master_name = None

    new_gid = db.add_geometry(
        src_geom["sim_target_id"],
        source_type="deck",
        source_file={"name": name, "size": len(data), "path": written["path"]},
        topo_summary=info or None,
        derived_from_id=gid,
        derived_by="vektor3d:mesh.airbag.generate",
    )
    log.info("气囊 deck 落库 源gid=%s 新gid=%s file=%s (%d 字节)",
             gid, new_gid, name, len(data))

    task_id = None
    if convert:
        tm = getattr(request.app.state, "task_manager", None)
        if tm is None:
            log.warning("任务管理器不可用，跳过 deck→GLB 预览转换 gid=%s", new_gid)
        else:
            out = os.path.join(
                proj["workdir"] or os.path.dirname(written["path"]),
                "sdm_geometry", src_geom["sim_target_id"], f"{new_gid}.glb",
            ) if proj["workdir"] else os.path.join(
                os.path.dirname(written["path"]), f".sdm_{new_gid}.glb")
            task_id = tm.submit("sim_deck_convert", owner=user,
                                params={"deck_path": written["path"], "out_path": out,
                                        "gid": new_gid, "run_as": user})
            log.info("派发气囊 deck 预览转换 gid=%s task=%s", new_gid, task_id)

    out_row = _row(db.get_geometry(new_gid), GEOM_JSON)
    out_row["convert_task_id"] = task_id
    out_row["master_deck"] = master_name
    return out_row


@router.get("/geometries/{gid}/deck-tree")
def list_deck_tree(
    request: Request,
    gid: str,
    user: str = Depends(_geometry_or_mesh_principal),
    is_admin: bool = Depends(_geometry_or_mesh_is_admin),
) -> Dict:
    """列出该 deck 的 *INCLUDE 树。

    给 vektor3d 的 cae.deck.check 用：它要整棵树一起看才判得准跨文件引用
    （*PART 在网格文件、*MAT 在 MAT.K），逐份检查会把每条正常的跨文件引用
    都报成"目标不存在"。清单在**服务端**解析，见 deck/parser.list_deck_files。

    只枚举不解析网格，整车 deck 也是毫秒级。
    """
    from .deck.parser import list_deck_files

    db = _db(request)
    row = _owned_geometry(db, gid, user, is_admin)
    src = _json_or_none(row["source_file_json"]) or {}
    path = src.get("path")
    if not path or not os.path.isfile(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deck 主控文件不存在或已被删除")

    found, missing = list_deck_files(path)
    root_dir = os.path.dirname(os.path.abspath(path))

    def rel(p: str) -> str:
        r = os.path.relpath(p, root_dir).replace("\\", "/")
        return r

    files = [{"path": rel(p), "bytes": os.path.getsize(p)} for p in found]
    log.info("deck 树 gid=%s: %d 个文件, 缺失 %d", gid, len(files), len(missing))
    return {
        "gid": gid,
        "master": rel(os.path.abspath(path)),
        "files": files,
        # 缺失的 include 一并给出:它不是"列不全",而是 deck 本身的错,
        # 求解器会直接失败,必须让调用方看见而不是悄悄少给几个文件
        "missing": [rel(p) for p in missing],
        "total_bytes": sum(f["bytes"] for f in files),
    }


@router.get("/geometries/{gid}/deck-file")
def download_deck_file(
    request: Request,
    gid: str,
    path: str = Query(..., description="相对主控目录的路径，必须是 deck-tree 列出过的成员"),
    user: str = Depends(_geometry_or_mesh_principal),
    is_admin: bool = Depends(_geometry_or_mesh_is_admin),
):
    """下发 deck 树里的某一个文件。

    **只认 deck-tree 列出过的成员**，而不是把 `path` 拼到目录上就读。后者即便
    做了 `..` 过滤也仍然脆弱（软链接、绝对路径、大小写），而 include 树本来就
    必须由服务端解析一遍，拿它当白名单是顺手且严密的做法。
    """
    from fastapi.responses import FileResponse
    from .deck.parser import list_deck_files

    db = _db(request)
    row = _owned_geometry(db, gid, user, is_admin)
    src = _json_or_none(row["source_file_json"]) or {}
    master = src.get("path")
    if not master or not os.path.isfile(master):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deck 主控文件不存在或已被删除")

    root_dir = os.path.dirname(os.path.abspath(master))
    found, _ = list_deck_files(master)
    want = os.path.normpath(os.path.join(root_dir, path.replace("\\", "/")))
    allowed = {os.path.normpath(p) for p in found}
    if want not in allowed:
        # 不区分"不在树里"与"不存在":两者都不该让调用方拿来探测文件系统
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该路径不属于本 deck 的 include 树")

    return FileResponse(want, filename=os.path.basename(want),
                        media_type="application/octet-stream")


@router.post("/geometries/{gid}/lightweight")
async def upload_lightweight(
    request: Request,
    gid: str,
    file: UploadFile = File(...),
    meta: Optional[str] = Form(None, description="转换元数据 JSON：三角面数/包围盒/单位等"),
    user: str = Depends(_geometry_principal),
    is_admin: bool = Depends(_geometry_is_admin),
) -> Dict:
    """回传轻量化产物（glTF/GLB）。

    契约里 vektor3d 转换完成后 POST 到这里。产物落在源文件同一子目录下，
    元数据并入 topo_summary_json 供前端展示零件数、包围盒等。
    """
    from ..fs.browser import FsError, write_file

    db = _db(request)
    row = _owned_geometry(db, gid, user, is_admin)
    target = db.get_target(row["sim_target_id"])
    proj = db.get_project(target["sim_project_id"])

    src = _json_or_none(row["source_file_json"]) or {}
    if not src.get("path"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该几何版本没有源文件，无法关联产物")
    parent = os.path.dirname(src["path"])

    name = os.path.basename((file.filename or "").replace("\\", "/")) or "model.glb"
    if not name.lower().endswith((".glb", ".gltf")):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "轻量化产物必须是 glTF/GLB —— 私有格式无法在浏览器中渲染，"
            "详见 docs/vektor3d-geometry-capability-contract.md",
        )
    data = await file.read()
    try:
        # 产物是几何版本的派生物，重跑转换 / 重新识别就该换成新的一份
        written = write_file(proj["owner"], parent, name, data,
                             get_settings().fs_root_list, replace=True)
    except FsError as e:
        raise HTTPException(e.status, f"写入轻量化产物失败: {e.message}")

    summary = _json_or_none(row["topo_summary_json"]) or {}
    if meta:
        try:
            summary.update(json.loads(meta))
        except ValueError:
            log.warning("轻量化元数据不是合法 JSON，已忽略 gid=%s", gid)
    summary["lightweight_bytes"] = len(data)

    db.set_geometry_lightweight(gid, written["path"], summary)
    log.info("回传轻量化产物 gid=%s file=%s (%d 字节)", gid, name, len(data))
    return _row(db.get_geometry(gid), GEOM_JSON)


@router.get("/geometries/{gid}/lightweight")
def download_lightweight(
    request: Request,
    gid: str,
    user: str = Depends(_geometry_principal),
    is_admin: bool = Depends(_geometry_is_admin),
):
    """取轻量化产物，供前端 three.js GLTFLoader 直接加载。

    主体解析必须与 download 用同一套:GLTFLoader 是拿 URL 直接 fetch 的,设不了
    Authorization 头,令牌只能走 query。曾经这里配的是
    `current_user_query + is_admin_request`——后者内部依赖 current_user(只读请求头),
    于是 query 里的令牌还没被看一眼,就先在管理员判定那步 401 了。
    """
    from fastapi.responses import FileResponse

    db = _db(request)
    row = _owned_geometry(db, gid, user, is_admin)
    path = row["lightweight_file"]
    if not path or not os.path.isfile(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "尚无轻量化产物")
    return FileResponse(path, filename=os.path.basename(path),
                        media_type="model/gltf-binary")


@router.delete("/geometries/{gid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_geometry(
    request: Request,
    gid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> None:
    """删除一个几何版本（导错了数模/版本作废）。

    网格版本随外键级联删除；上传的源文件与轻量化/网格产物一并清理，
    但 from-path 登记的集群源文件不动（那是引用，不是拷贝）。
    已有工况绑定其网格版本时拒删（409），见 _reject_if_bound_subjects。
    """
    db = _db(request)
    row = _owned_geometry(db, gid, user, is_admin)
    _reject_if_bound_subjects(db.subjects_using_geometry(gid), "几何版本")

    target = db.get_target(row["sim_target_id"])
    base = _geometry_dir(db, target)
    src = _json_or_none(row["source_file_json"]) or {}
    candidates = [src.get("path"), row["step_file"], row["brep_file"],
                  row["lightweight_file"]]
    for m in db.list_meshes(gid):
        candidates += [m["mesh_file"], m["ansa_file"], m["solver_file"],
                       m["preview_file"], m["report_file"]]

    db.delete_geometry(gid)
    _cleanup_managed_files(base, candidates)
    log.info("删除几何版本 gid=%s v%s target=%s by=%s",
             gid, row["version_no"], row["sim_target_id"], user)


@router.get("/geometries/{gid}")
def get_geometry(
    request: Request,
    gid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """取单个几何版本。前端在网格作业进行中要反复刷它读分析结果与产物状态,
    不必为此拉整个列表。"""
    db = _db(request)
    return _row(_owned_geometry(db, gid, user, is_admin), GEOM_JSON)


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

    两种用法:mesh_engine=manual 是人工上传;由 vektor3d 网格能力产出时,
    前端先带 status='generating' 登记(能力作业动辄几十分钟,得先有一行让页面
    能展示进度),产物回传与状态回写走后面的 artifact / PATCH 两个端点。
    """
    db = _db(request)
    _owned_geometry(db, gid, user, is_admin)
    mid = db.add_mesh(
        gid, body.mesh_type, body.mesh_engine, body.mesh_params,
        body.mesh_file, body.quality,
        status=body.status, part_filter=body.part_filter,
        source_mesh_ids=body.source_mesh_ids,
    )
    return _row(db.get_mesh(mid), MESH_JSON)


@router.patch("/geometries/{gid}/meshes/{mid}")
def update_mesh(
    request: Request,
    gid: str,
    mid: str,
    body: MeshUpdate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """回写网格作业的状态与质量结论(浏览器代理拿到能力结果后调用)。

    失败时把原因放进 quality.summary —— 页面本就在读它,不必另开一列。
    """
    db = _db(request)
    _owned_mesh(db, mid, user, is_admin)
    if body.status:
        db.set_mesh_status(mid, body.status, body.quality)
    elif body.quality is not None:
        db.set_mesh_quality(mid, body.quality)
    return _row(db.get_mesh(mid), MESH_JSON)


# --- 网格能力的文件通道 --------------------------------------------------
#
# 与几何链同构(票据 → vektor3d 直连拉取/回传),但网格产物有四类角色:
#   ansa    .ansa 原生库 —— **正本**,手工微调/装配合并/换格式导出都以它为源
#   solver  求解器文件   —— 派生物(.nas/.k/.inp/...),交给求解器算的就是它
#   preview 预览 GLB     —— 带真实单元边线,复用几何那套 three.js 查看器
#   report  质量报告     —— ANSA 出的 HTML 统计
# 分角色而不是"一个 mesh_file 走天下":混在一列里就分不清能不能再导出别的格式。

MESH_ARTIFACT_KINDS = {
    "ansa": (".ansa", "application/octet-stream"),
    "solver": (None, "application/octet-stream"),   # 扩展名由 solver_format 决定
    "preview": ((".glb", ".gltf"), "model/gltf-binary"),
    "report": ((".html", ".htm"), "text/html; charset=utf-8"),
}


class MeshTicket(BaseModel):
    """网格作业票据。URL 由前端用 window.location.origin 拼(理由同 ConvertTicket)。"""
    gid: str
    token: str
    expires_in: int
    source_name: str
    source_path_suffix: str = Field(description="几何源文件下载路径（相对 /api）")
    mesh_path_prefix: str = Field(description="网格产物路径前缀（相对 /api），后接 /{mid}/artifact/{kind}")


@router.post("/geometries/{gid}/mesh-ticket")
def create_mesh_ticket(
    request: Request,
    gid: str,
    ttl_seconds: int = Query(7200, ge=60, le=28800),
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> MeshTicket:
    """为网格作业签发受限令牌(绑本 gid)。

    默认 2 小时:网格作业比几何转换慢一个量级(大件几十分钟,还可能排队),
    票据在作业跑完前过期会让回传阶段功亏一篑。上限 8 小时。
    只有常规用户令牌能调它——票据不能自我续签。
    """
    db = _db(request)
    row = _owned_geometry(db, gid, user, is_admin)
    src = _json_or_none(row["source_file_json"]) or {}
    if not src.get("path"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该几何版本没有源文件，无法生成网格")

    token = issue_scoped_token(user, MESH_JOB_SCOPE, ttl_seconds, gid=gid)
    log.info("签发网格作业票据 gid=%s user=%s ttl=%ss", gid, user, ttl_seconds)
    return MeshTicket(
        gid=gid,
        token=token,
        expires_in=ttl_seconds,
        source_name=src.get("name") or os.path.basename(src["path"]),
        source_path_suffix=f"/sim/geometries/{gid}/download",
        mesh_path_prefix=f"/sim/geometries/{gid}/meshes",
    )


@router.post("/geometries/{gid}/meshes/{mid}/artifact/{kind}")
async def upload_mesh_artifact(
    request: Request,
    gid: str,
    mid: str,
    kind: str,
    file: UploadFile = File(...),
    meta: Optional[str] = Form(None, description="能力回传的元数据 JSON（单元数/违例等）"),
    user: str = Depends(_mesh_principal),
    is_admin: bool = Depends(_mesh_is_admin),
) -> Dict:
    """接收 vektor3d 回传的网格产物(multipart，字段 file + 可选 meta)。

    kind ∈ ansa/solver/preview/report。产物落在几何源文件同一目录下的 mesh/ 子目录,
    以项目属主身份写盘(与轻量化产物同例)。
    """
    from ..fs.browser import FsError, write_file

    spec = MESH_ARTIFACT_KINDS.get(kind)
    if not spec:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"未知的网格产物类型 {kind}（可选 {'/'.join(MESH_ARTIFACT_KINDS)}）",
        )
    db = _db(request)
    mesh_row = _owned_mesh(db, mid, user, is_admin)
    if mesh_row["sim_geometry_version_id"] != gid:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该网格版本不属于此几何版本")
    geom = db.get_geometry(gid)
    target = db.get_target(geom["sim_target_id"])
    proj = db.get_project(target["sim_project_id"])

    src = _json_or_none(geom["source_file_json"]) or {}
    if not src.get("path"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该几何版本没有源文件，无法关联产物")
    parent = os.path.join(os.path.dirname(src["path"]), "mesh")

    name = os.path.basename((file.filename or "").replace("\\", "/")) or f"mesh-{kind}"
    allowed_ext = spec[0]
    if allowed_ext and not name.lower().endswith(allowed_ext):
        want = allowed_ext if isinstance(allowed_ext, str) else "/".join(allowed_ext)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{kind} 产物的扩展名必须是 {want}（收到 {name}）",
        )
    # 同一网格版本的同类产物固定文件名，避免反复检入检出堆出一堆同名变体
    name = f"m{mesh_row['version_no']:02d}-{kind}{os.path.splitext(name)[1]}"

    data = await file.read()
    try:
        # 固定文件名的用意就是同一版本重复检入时换掉旧的（见上），必须允许覆盖
        written = write_file(proj["owner"], parent, name, data,
                             get_settings().fs_root_list, replace=True)
    except FsError as e:
        raise HTTPException(e.status, f"写入网格产物失败: {e.message}")

    info = {}
    if meta:
        try:
            info = json.loads(meta) or {}
        except ValueError:
            log.warning("网格产物元数据不是合法 JSON，已忽略 mid=%s kind=%s", mid, kind)
    db.set_mesh_artifact(mid, kind, written["path"],
                         solver_format=info.get("solverFormat"))
    # 主产物(正本/求解器)到位即视为可用;质量结论随 meta 一并落库供页面展示
    if kind in ("ansa", "solver"):
        quality = _json_or_none(mesh_row["quality_json"]) or {}
        quality.update({k: v for k, v in info.items() if k != "uploaded"})
        db.set_mesh_status(mid, "ready", quality)
    log.info("回传网格产物 mid=%s kind=%s file=%s (%d 字节)", mid, kind, name, len(data))
    return _row(db.get_mesh(mid), MESH_JSON)


@router.get("/geometries/{gid}/meshes/{mid}/artifact/{kind}")
def download_mesh_artifact(
    request: Request,
    gid: str,
    mid: str,
    kind: str,
    user: str = Depends(_mesh_principal),
    is_admin: bool = Depends(_mesh_is_admin),
):
    """取网格产物。

    三类消费方共用它:浏览器 three.js 加载 preview(GLTFLoader 设不了 header,
    故令牌走 query)、vektor3d 拉 .ansa 做导出/检查/合并/检出、用户直接下载求解器文件。
    """
    from fastapi.responses import FileResponse

    spec = MESH_ARTIFACT_KINDS.get(kind)
    if not spec:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知的网格产物类型 {kind}")
    db = _db(request)
    row = _owned_mesh(db, mid, user, is_admin)
    if row["sim_geometry_version_id"] != gid:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该网格版本不属于此几何版本")
    path = row[{"ansa": "ansa_file", "solver": "solver_file",
                "preview": "preview_file", "report": "report_file"}[kind]]
    if not path or not os.path.isfile(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"尚无 {kind} 产物")
    return FileResponse(path, filename=os.path.basename(path), media_type=spec[1])


@router.post("/geometries/{gid}/meshes/{mid}/checkout")
def checkout_mesh(
    request: Request,
    gid: str,
    mid: str,
    body: MeshCheckout,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """登记检出(工程师把 .ansa 取到本机 ANSA 里改)。

    SDM 这边只记"谁在改、凭据是什么":工作副本在对方机器上,正本仍在这里。
    已被别人检出时拒绝——两个人各改各的,谁后检入谁覆盖,静默丢工作。
    """
    db = _db(request)
    row = _owned_mesh(db, mid, user, is_admin)
    if row["checkout_id"] and row["checkout_by"] and row["checkout_by"] != user:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"该网格已被 {row['checkout_by']} 检出，请等待其提交或联系其释放",
        )
    if not row["ansa_file"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该网格版本没有 .ansa 正本，无法检出")
    db.set_mesh_checkout(mid, body.checkout_id, user)
    log.info("网格检出 mid=%s by=%s checkout=%s", mid, user, body.checkout_id)
    return _row(db.get_mesh(mid), MESH_JSON)


@router.post("/geometries/{gid}/meshes/{mid}/checkin")
def checkin_mesh(
    request: Request,
    gid: str,
    mid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """释放检出占用。新版本文件本身经 artifact 端点回传，这里只翻状态。"""
    db = _db(request)
    _owned_mesh(db, mid, user, is_admin)
    db.set_mesh_checkout(mid, None)
    log.info("网格检入 mid=%s by=%s", mid, user)
    return _row(db.get_mesh(mid), MESH_JSON)


@router.put("/geometries/{gid}/analysis")
def set_geometry_analysis(
    request: Request,
    gid: str,
    body: GeometryAnalysis,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """回写 mesh.inventory(零件清单)与 mesh.classify(网格策略建议)的产出。

    这两项是对几何的分析结论、网格生成之前就该可见——正是它们决定了
    每个零件用哪种网格策略(见能力契约 2.7/2.8)。
    """
    db = _db(request)
    _owned_geometry(db, gid, user, is_admin)
    db.set_geometry_analysis(gid, body.part_inventory, body.mesh_strategy)
    return _row(db.get_geometry(gid), GEOM_JSON)


# --- 客户需求文档 -------------------------------------------------------
#
# 仿真项目的输入源头:主机厂给的 CAE 分析规范/技术协议。整条链是
#   建项目 → 传需求文档 → 分析需求并选质量卡模板 → 派生质量卡实例 → 按需调参
# 其中"分析"这一步的 AI 尚未接入(缺真实需求文档样本,输入形态未知),因此这里
# 先把文档归档与实例的**手工派生**做通——AI 到位后是替人填"依据",不是推倒重来。

class RequirementDocCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    doc_type: str = "spec"          # spec/agreement/standard/other
    note: Optional[str] = None


@router.get("/projects/{pid}/requirements")
def list_requirement_docs(
    request: Request,
    pid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    _owned_project(db, pid, user, is_admin)
    return [_row(r, REQUIREMENT_JSON) for r in db.list_requirement_docs(pid)]


@router.post("/projects/{pid}/requirements/upload", status_code=status.HTTP_201_CREATED)
async def upload_requirement_doc(
    request: Request,
    pid: str,
    file: UploadFile = File(...),
    doc_type: str = Form("spec"),
    note: Optional[str] = Form(None),
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """上传客户需求文档并归档。

    与几何同一套落盘规则:项目工作目录下独立子目录,同名重复上传不互相覆盖。
    """
    from ..fs.browser import FsError, write_file

    db = _db(request)
    proj = _owned_project(db, pid, user, is_admin)
    base = os.path.join(_project_workdir(db, proj), "sdm_requirements")

    raw_name = (file.filename or "").replace("\\", "/")
    name = os.path.basename(raw_name) or "requirement.bin"
    data = await file.read()
    try:
        written = write_file(proj["owner"], os.path.join(base, uuid.uuid4().hex[:8]),
                             name, data, get_settings().fs_root_list)
    except FsError as e:
        raise HTTPException(e.status, f"写入需求文档失败: {e.message}")

    rid = db.add_requirement_doc(
        pid, name=name, doc_type=doc_type,
        source_file={"name": name, "size": len(data), "path": written["path"]},
        note=note,
    )
    log.info("上传需求文档 project=%s rid=%s file=%s (%d 字节)", pid, rid, name, len(data))
    return _row(db.get_requirement_doc(rid), REQUIREMENT_JSON)


def _requirement_principal(
    request: Request,
    rid: str,
    token: Optional[str] = Query(None),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    act_as: Optional[str] = Header(None, alias="X-Act-As-User"),
) -> str:
    """需求文档下载的调用者:常规用户令牌、本 rid 的分析票据,或本项目的
    ai.read 只读票据(AI 会话中 vektor3d 深读原文档用,见会话契约第 3 节)。

    ai.read 绑的是项目,先查文档属于哪个项目再核对——查库放在依赖里而不是
    handler 里,绑定核对必须发生在主体解析这一步,不给"忘了核对"留机会。
    """
    raw = token or (creds.credentials if creds else None)
    row = _db(request).get_requirement_doc(rid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "需求文档不存在")
    return resolve_multi_scoped_principal(
        raw, act_as, allow_scopes={
            DOC_ANALYZE_SCOPE: {"rid": rid},
            AI_READ_SCOPE: {"pid": row["sim_project_id"]},
        },
    )


def _requirement_is_admin(
    user: str = Depends(_requirement_principal),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    portal_admin: Optional[str] = Header(None, alias="X-Hpc-Portal-Admin"),
) -> bool:
    """同 is_admin_request,但主体由 _requirement_principal 解析。

    不能直接复用 is_admin_request:它依赖 current_user,而后者按设计拒绝一切
    受限票据——那样 vektor3d 拿票据来拉文档会在这一步就 401。
    """
    return principal_is_admin(user, creds.credentials if creds else None, portal_admin)


class AnalyzeTicket(BaseModel):
    """交给浏览器、再转交 vektor3d 的一次性文档分析票据。

    只读、只对这一份文档、以分钟计过期。与几何票据一样,URL 由前端用
    window.location.origin 拼——后端看到的 base_url 不是浏览器真正访问的地址。
    """
    rid: str
    token: str
    expires_in: int
    source_name: str
    source_path_suffix: str = Field(description="文档下载路径（相对 /api）")


@router.post("/requirements/{rid}/analyze-ticket")
def create_analyze_ticket(
    request: Request,
    rid: str,
    ttl_seconds: int = Query(1800, ge=60, le=7200),
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> AnalyzeTicket:
    """为一次 AI 解析签发只读票据。只有常规用户令牌能调——票据不能自我续签。"""
    db = _db(request)
    row = _owned_requirement(db, rid, user, is_admin)
    src = _json_or_none(row["source_file_json"]) or {}
    if not src.get("path"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该需求文档没有源文件")
    token = issue_scoped_token(user, DOC_ANALYZE_SCOPE, ttl_seconds, rid=rid)
    log.info("签发文档分析票据 rid=%s user=%s ttl=%ss", rid, user, ttl_seconds)
    return AnalyzeTicket(
        rid=rid,
        token=token,
        expires_in=ttl_seconds,
        source_name=src.get("name") or os.path.basename(src["path"]),
        source_path_suffix=f"/sim/requirements/{rid}/download",
    )


@router.get("/requirements/{rid}/download")
def download_requirement_doc(
    request: Request,
    rid: str,
    user: str = Depends(_requirement_principal),
    is_admin: bool = Depends(_requirement_is_admin),
):
    from fastapi.responses import FileResponse

    db = _db(request)
    row = _owned_requirement(db, rid, user, is_admin)
    src = _json_or_none(row["source_file_json"]) or {}
    path = src.get("path")
    if not path or not os.path.isfile(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "需求文档不存在或已被删除")
    # ETag = 内容 sha256:vektor3d workspace 的 manifest 靠它判缓存是否过期
    # (会话契约 2.2)。AI 拿三天前的文档回答今天的问题,比"不可用"危险得多。
    return FileResponse(path, filename=src.get("name") or os.path.basename(path),
                        media_type="application/octet-stream",
                        headers={"ETag": f'"{_file_sha256(path)}"'})


@router.delete("/requirements/{rid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_requirement_doc(
    request: Request,
    rid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> None:
    db = _db(request)
    _owned_requirement(db, rid, user, is_admin)
    db.delete_requirement_doc(rid)


@router.post("/requirements/{rid}/extract")
def extract_requirement_items(
    request: Request,
    rid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """解析需求文档,产出需求条目。

    走**确定性规则**,不上 AI:真实文档里分析项躺在规整表格里,规则抽得准,而且
    抽错了能一眼看出是哪条规则的问题。重解析是整体替换而非追加——否则改一次
    规则再跑一遍,库里就同时躺着新旧两版条目,谁也说不清哪条是当前口径。
    """
    from .requirements import extract_from_file, items_to_json

    db = _db(request)
    row = _owned_requirement(db, rid, user, is_admin)
    src = _json_or_none(row["source_file_json"]) or {}
    path = src.get("path")
    if not path or not os.path.isfile(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "需求文档不存在或已被删除")

    try:
        items = extract_from_file(path)
    except ValueError as e:
        db.set_requirement_analysis(rid, None, "failed")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    payload = items_to_json(items)
    count = db.replace_requirement_items(rid, payload)
    summary = {
        "itemCount": count,
        "subjectCount": sum(1 for i in payload if i["category"] == "subject"),
        "loadingCount": sum(1 for i in payload if i["category"] == "loading"),
        "needsClarification": sum(1 for i in payload if i["needs_clarification"]),
        "requiredCount": sum(1 for i in payload if i["baseline"] == "required"),
        "loadPointTotal": sum(i["load_points"] for i in payload),
        # 点位坐标在图上,文字层抽不到——把边界写进产出,免得下游误以为拿到了位置
        "loadPointCoordsAvailable": False,
        "extractor": "rule",
    }
    db.set_requirement_analysis(rid, summary, "done")
    log.info("解析需求文档 rid=%s → %d 条条目(%d 待澄清)", rid, count,
             summary["needsClarification"])
    return {"summary": summary, "items": [_row(r, ITEM_JSON) for r in db.list_requirement_items(rid)]}


class RequirementItemsIn(BaseModel):
    """由 vektor3d 的 doc.analyze 产出、经浏览器回写的条目。

    走浏览器回写而不是 vektor3d 直连写:写库是有副作用的操作,必须经过 SDM 自己的
    鉴权与属主校验。让桌面端持一张能写库的票据,风险面比读大得多。
    """
    items: List[Dict]
    summary: Optional[Dict] = None
    extractor: str = "ai"


@router.put("/requirements/{rid}/items")
def replace_requirement_items(
    request: Request,
    rid: str,
    body: RequirementItemsIn,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """整体替换条目。与规则解析同一个入口语义——重解析不追加,避免新旧两版并存。"""
    db = _db(request)
    row = _owned_requirement(db, rid, user, is_admin)

    items = []
    for raw in body.items:
        if not str(raw.get("title") or "").strip():
            continue
        items.append({**raw, "extracted_by": raw.get("extracted_by") or body.extractor})
    if not items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有可写入的条目")

    # AI 回写时服务端再独立跑一遍规则抽取做交叉核对:一致互为佐证、冲突转待澄清、
    # 仅规则抽到的兜底保留(视觉失败页的表格内容规则侧本来就有)。规则结果不进 AI
    # 的提示词——两条线独立才有交叉验证的价值,合并与裁决全在 merge.py 的纯代码里。
    merge_stats = None
    if body.extractor == "ai":
        from .requirements import extract_from_file, items_to_json
        from .requirements.merge import merge_rule_and_ai

        src = _json_or_none(row["source_file_json"]) or {}
        path = src.get("path")
        if path and os.path.isfile(path):
            try:
                rule_items = items_to_json(extract_from_file(path))
            except ValueError:
                rule_items = None      # 格式规则抽不了(如 pdf),合并不适用
            except Exception:
                # 规则侧崩溃不能拖垮 AI 回写:AI 结果照常落库,只是少了交叉核对
                log.exception("规则抽取失败,跳过交叉核对 rid=%s", rid)
                rule_items = None
            if rule_items is not None:
                items, merge_stats = merge_rule_and_ai(rule_items, items)
                log.info("规则/AI 交叉核对 rid=%s 一致 %d 冲突 %d 规则兜底 %d 仅AI %d",
                         rid, merge_stats["agreed"], merge_stats["conflicts"],
                         merge_stats["ruleOnly"], merge_stats["aiOnly"])

    count = db.replace_requirement_items(rid, items)
    # body.summary 里的自由字段(如 AI 的 notes)原样保留,下面只覆盖统计口径——
    # notes 记的是"AI 在哪里拿不准",丢了就等于把最该人工复核的线索扔了
    summary = dict(body.summary or {})
    summary.update({
        "itemCount": count,
        "subjectCount": sum(1 for i in items if i.get("category") == "subject"),
        "loadingCount": sum(1 for i in items if i.get("category") == "loading"),
        "needsClarification": sum(1 for i in items if i.get("needs_clarification")),
        "requiredCount": sum(1 for i in items if i.get("baseline") == "required"),
        "loadPointTotal": sum(int(i.get("load_points") or 0) for i in items),
        # 无论谁抽的,这条边界不变:点位坐标在图上,文字层拿不到
        "loadPointCoordsAvailable": False,
        "extractor": body.extractor,
    })
    if merge_stats is not None:
        summary["merge"] = merge_stats
    db.set_requirement_analysis(rid, summary, "done")
    log.info("回写需求条目 rid=%s %d 条 extractor=%s", rid, count, body.extractor)
    return {"summary": summary, "items": [_row(r, ITEM_JSON) for r in db.list_requirement_items(rid)]}


@router.get("/requirements/{rid}/items")
def list_requirement_items(
    request: Request,
    rid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    _owned_requirement(db, rid, user, is_admin)
    return [_row(r, ITEM_JSON) for r in db.list_requirement_items(rid)]


class RequirementItemUpdate(BaseModel):
    """人工修正一条条目。

    改动一律记为 extracted_by=manual:规则抽的和人改过的必须分得开,
    否则下次重解析会把人的修正无声冲掉而没人察觉。
    """
    title: Optional[str] = None
    raw_text: Optional[str] = None
    category: Optional[str] = None
    baseline: Optional[str] = None
    status: Optional[str] = None
    needs_clarification: Optional[bool] = None
    clarification_hint: Optional[str] = None


@router.patch("/requirement-items/{iid}")
def update_requirement_item(
    request: Request,
    iid: str,
    body: RequirementItemUpdate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    item = db.get_requirement_item(iid)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "需求条目不存在")
    _owned_requirement(db, item["doc_id"], user, is_admin)

    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "needs_clarification" in fields:
        fields["needs_clarification"] = 1 if fields["needs_clarification"] else 0
    if fields:
        fields["extracted_by"] = "manual"
        db.update_requirement_item(iid, **fields)
    return _row(db.get_requirement_item(iid), ITEM_JSON)


# --- AI 会话:只读票据、项目快照、会话与消息 -----------------------------
#
# 契约见 docs/vektor3d-ai-session-contract.md。要点:
#   - SDM 持主数据与消息流;推理过程留在 vektor3d 侧 workspace,可丢;
#   - ai.read 票据绑单项目、只在枚举的只读接口上有效;
#   - AI 只能"提案",确认后由浏览器带**用户自己的凭据**调既有接口执行——
#     本节没有、也不应该有任何新的写主数据接口。

# 提案动作枚举(契约 5.2)。不在枚举里的 action 一律 400——新场景通过修订
# 契约加入,不通过"先发了再说"加入。
AI_PROPOSAL_ACTIONS = {
    "requirement_item.update",
    "requirement_item.resolve_clarification",
    "quality_template.edit_content",
    "quality_template.derive",
}

_file_hash_cache: Dict[str, tuple] = {}   # path -> (size, mtime, sha256)


def _file_sha256(path: str) -> str:
    """文件内容 sha256,按 (size, mtime) 缓存——需求文档是 MB 级、请求是高频的。"""
    import hashlib

    st = os.stat(path)
    cached = _file_hash_cache.get(path)
    if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
        return cached[2]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    _file_hash_cache[path] = (st.st_size, st.st_mtime, digest)
    return digest


def _json_sha256(obj) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _ai_read_principal(
    pid: str,
    token: Optional[str] = Query(None),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    act_as: Optional[str] = Header(None, alias="X-Act-As-User"),
) -> str:
    """项目级只读端点的调用者:常规用户令牌,或本项目的 ai.read 票据。"""
    raw = token or (creds.credentials if creds else None)
    return resolve_scoped_principal(raw, act_as, scope=AI_READ_SCOPE, bindings={"pid": pid})


def _ai_read_is_admin(
    user: str = Depends(_ai_read_principal),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    portal_admin: Optional[str] = Header(None, alias="X-Hpc-Portal-Admin"),
) -> bool:
    """同 is_admin_request,但主体由 _ai_read_principal 解析(后者接受受限票据)。"""
    return principal_is_admin(user, creds.credentials if creds else None, portal_admin)


class AiReadTicket(BaseModel):
    """交给浏览器、再转交 vektor3d 的项目级只读票据(会话契约第 3 节)。"""
    pid: str
    token: str
    expires_in: int
    context_path_suffix: str = Field(description="项目快照路径(相对 /api)")


@router.post("/projects/{pid}/ai-read-ticket")
def create_ai_read_ticket(
    request: Request,
    pid: str,
    ttl_seconds: int = Query(1800, ge=60, le=7200),
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> AiReadTicket:
    """签发 AI 会话只读票据。只有常规用户令牌能调——票据不能自我续签。"""
    db = _db(request)
    _owned_project(db, pid, user, is_admin)
    token = issue_scoped_token(user, AI_READ_SCOPE, ttl_seconds, pid=pid)
    log.info("签发 AI 只读票据 pid=%s user=%s ttl=%ss", pid, user, ttl_seconds)
    return AiReadTicket(
        pid=pid, token=token, expires_in=ttl_seconds,
        context_path_suffix=f"/sim/projects/{pid}/ai-context",
    )


@router.get("/projects/{pid}/ai-context")
def get_ai_context(
    request: Request,
    pid: str,
    user: str = Depends(_ai_read_principal),
    is_admin: bool = Depends(_ai_read_is_admin),
) -> Dict:
    """项目上下文快照:AI 会话推理的推荐起点(契约 4.1)。

    每节带内容 hash,供 vektor3d workspace 的 manifest 判缓存过期。它是推荐
    起点而非强制边界——vektor3d 也可以凭票据走枚举的只读接口深读原件。
    """
    db = _db(request)
    proj = _owned_project(db, pid, user, is_admin)

    docs = []
    items = []
    for d in db.list_requirement_docs(pid):
        src = _json_or_none(d["source_file_json"]) or {}
        path = src.get("path")
        docs.append({
            "id": d["id"], "name": d["name"], "docType": d["doc_type"],
            "analysisStatus": d["analysis_status"],
            "hash": _file_sha256(path) if path and os.path.isfile(path) else None,
            "downloadPathSuffix": f"/sim/requirements/{d['id']}/download",
        })
        items.extend(
            {**_row(r, ITEM_JSON), "doc_name": d["name"]}
            for r in db.list_requirement_items(d["id"])
        )

    cards = []
    for r in db.list_quality_cards(pid):
        card = _row(r, QUALITY_CARD_JSON)
        card_dir = r["card_dir"]
        if card_dir and os.path.isdir(card_dir):
            from .quality import QualityCardLibrary

            lib = QualityCardLibrary(user_dir=os.path.dirname(card_dir))
            try:
                card["card"] = _quality_card_detail(lib.load(os.path.basename(card_dir)))
            except Exception:
                log.exception("ai-context 载入质量卡失败 qid=%s", r["id"])
        cards.append(card)

    subjects = [_row(r, SUBJECT_JSON) for r in db.list_subjects(pid, None)]

    return {
        "project": {
            "id": proj["id"], "name": proj["name"],
            "description": proj["description"],
            "unitSystem": proj["unit_system"],
        },
        "requirementDocs": docs,
        "requirementItems": {"hash": _json_sha256(items), "items": items},
        "qualityCards": {"hash": _json_sha256(cards), "cards": cards},
        "subjects": {"hash": _json_sha256(subjects), "subjects": subjects},
        "generatedAt": time.time(),
    }


def _owned_ai_session(db: SimDB, sid: str, user: str, is_admin: bool):
    row = db.get_ai_session(sid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    _owned_project(db, row["sim_project_id"], user, is_admin)
    return row


AI_MESSAGE_JSON = ("proposals_json", "citations_json", "meta_json")


class AiSessionCreate(BaseModel):
    title: str = Field("", max_length=200)


@router.post("/projects/{pid}/ai-sessions", status_code=status.HTTP_201_CREATED)
def create_ai_session(
    request: Request,
    pid: str,
    body: AiSessionCreate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    _owned_project(db, pid, user, is_admin)
    sid = db.create_ai_session(pid, body.title.strip() or "新会话", user)
    return _row(db.get_ai_session(sid))


@router.get("/projects/{pid}/ai-sessions")
def list_ai_sessions(
    request: Request,
    pid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    _owned_project(db, pid, user, is_admin)
    return [_row(r) for r in db.list_ai_sessions(pid)]


@router.get("/ai-sessions/{sid}")
def get_ai_session(
    request: Request,
    sid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    row = _owned_ai_session(db, sid, user, is_admin)
    return {
        **_row(row),
        "messages": [_row(m, AI_MESSAGE_JSON) for m in db.list_ai_messages(sid)],
    }


@router.delete("/ai-sessions/{sid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ai_session(
    request: Request,
    sid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> None:
    db = _db(request)
    _owned_ai_session(db, sid, user, is_admin)
    db.delete_ai_session(sid)


class AiMessageIn(BaseModel):
    """一条会话消息。user 消息由用户输入;assistant 消息是浏览器从 vektor3d
    `llm.chat` 取回后落库(消息流是主数据,契约 5.3);system 消息记提案执行
    结果等注记。"""
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str = ""
    proposals: Optional[List[Dict]] = None
    citations: Optional[List[Dict]] = None
    meta: Optional[Dict] = None


@router.post("/ai-sessions/{sid}/messages", status_code=status.HTTP_201_CREATED)
def add_ai_message(
    request: Request,
    sid: str,
    body: AiMessageIn,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    _owned_ai_session(db, sid, user, is_admin)

    proposals = None
    if body.proposals:
        proposals = []
        for raw in body.proposals:
            action = str(raw.get("action") or "")
            if action not in AI_PROPOSAL_ACTIONS:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"未知的提案动作 {action}(允许:{sorted(AI_PROPOSAL_ACTIONS)})。"
                    "新动作通过修订会话契约加入,不通过先发了再说加入",
                )
            if not str(raw.get("reason") or "").strip():
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"提案 {action} 缺少 reason——没有出处的改动就是编的",
                )
            # status 由服务端置 pending,不信调用方——提案的裁决只能来自确认接口
            proposals.append({**raw, "status": "pending",
                              "decided_by": "", "decided_at": None})

    if not body.content.strip() and not proposals:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "消息为空")
    mid = db.add_ai_message(sid, body.role, body.content,
                            proposals=proposals, citations=body.citations,
                            meta=body.meta)
    return _row(db.get_ai_message(mid), AI_MESSAGE_JSON)


class ProposalDecision(BaseModel):
    """对一条提案的裁决。**只翻 UI 状态**:真正的数据变更由前端带用户凭据调
    对应的既有接口完成(那里有属主校验与留痕),这里只记"谁在何时决定了什么"。"""
    index: int = Field(ge=0)
    decision: str = Field(pattern="^(confirmed|rejected)$")
    note: str = ""


@router.post("/ai-sessions/{sid}/messages/{mid}/proposal-decision")
def decide_ai_proposal(
    request: Request,
    sid: str,
    mid: str,
    body: ProposalDecision,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    db = _db(request)
    _owned_ai_session(db, sid, user, is_admin)
    msg = db.get_ai_message(mid)
    if msg is None or msg["session_id"] != sid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "消息不存在")
    proposals = _json_or_none(msg["proposals_json"]) or []
    if body.index >= len(proposals):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提案不存在")
    if proposals[body.index].get("status") != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "该提案已裁决,不能重复裁决")
    proposals[body.index].update(
        status=body.decision, decided_by=user, decided_at=time.time(),
        note=body.note,
    )
    db.set_ai_message_proposals(mid, proposals)
    log.info("AI 提案裁决 sid=%s mid=%s #%d %s by=%s",
             sid, mid, body.index, body.decision, user)
    return _row(db.get_ai_message(mid), AI_MESSAGE_JSON)


# --- 质量卡模板库与项目实例 ---------------------------------------------

@router.get("/quality-templates")
def list_quality_templates(
    request: Request,
    user: str = Depends(current_user),
) -> List[Dict]:
    """模板库:内置只读 + 用户自建。内置那张是所有项目的缺省底座。

    used_by 是被项目实例引用的次数。实例文件派生时已拷走,删模板不破坏既有
    实例——这个数只用来让删除者知道影响面。
    """
    from dataclasses import asdict

    counts = _db(request).count_cards_by_template()
    return [
        {**asdict(t), "used_by": counts.get(t.id, 0)}
        for t in _quality_library(request).list_templates()
    ]


@router.get("/quality-templates/{template_id}")
def get_quality_template(
    request: Request,
    template_id: str,
    user: str = Depends(current_user),
) -> Dict:
    try:
        card = _quality_library(request).load(template_id)
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return _quality_card_detail(card)


class QualityCardCreate(BaseModel):
    """从模板派生项目质量卡实例。

    overrides 的每一项都要求 source(依据出处)。这不是形式要求:AI 从需求文档
    生成实例时,没有出处的数值就是编的,而这个数字会一路流进网格验收。
    """
    template_id: str = DEFAULT_QUALITY_TEMPLATE
    name: str = Field(min_length=1, max_length=200)
    overrides: List[Dict] = Field(default_factory=list)
    requirement_doc_id: Optional[str] = None


@router.get("/projects/{pid}/quality-cards")
def list_project_quality_cards(
    request: Request,
    pid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> List[Dict]:
    db = _db(request)
    _owned_project(db, pid, user, is_admin)
    return [_row(r, QUALITY_CARD_JSON) for r in db.list_quality_cards(pid)]


@router.post("/projects/{pid}/quality-cards", status_code=status.HTTP_201_CREATED)
def create_project_quality_card(
    request: Request,
    pid: str,
    body: QualityCardCreate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """派生实例。产物是**合法的 ANSA 卡**,可直接交回 ANSA 跑批处理。"""
    from dataclasses import asdict

    from .quality import QualityCardLibrary

    db = _db(request)
    proj = _owned_project(db, pid, user, is_admin)
    card_root = os.path.join(_project_workdir(db, proj), "sdm_quality_cards")
    # 底座可以来自全局模板库(含导入的客户模板),产物落项目目录
    lib = QualityCardLibrary(user_dir=card_root, extra_dirs=[_quality_templates_root()])

    overrides = _overrides_from_request(body.overrides, user)

    instance_id = f"{pid[:8]}-{uuid.uuid4().hex[:6]}"
    try:
        meta = lib.derive(body.template_id, instance_id, body.name, overrides,
                          source=proj["name"], scope="项目实例")
    except (KeyError, ValueError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    qid = db.add_quality_card(
        pid, template_id=body.template_id, name=body.name,
        card_dir=os.path.join(card_root, instance_id),
        overrides=[asdict(o) for o in meta.overrides],
        derived_from_doc_id=body.requirement_doc_id,
    )
    log.info("派生质量卡实例 project=%s card=%s 基于 %s,覆盖 %d 项",
             pid, qid, body.template_id, len(meta.overrides))
    return _row(db.get_quality_card(qid), QUALITY_CARD_JSON)


@router.get("/quality-cards/{qid}")
def get_project_quality_card(
    request: Request,
    qid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """实例详情:元数据 + 解析后的判据与网格参数。"""
    from .quality import QualityCardLibrary

    db = _db(request)
    row = _owned_quality_card(db, qid, user, is_admin)
    out = _row(row, QUALITY_CARD_JSON)
    card_dir = row["card_dir"]
    if card_dir and os.path.isdir(card_dir):
        lib = QualityCardLibrary(user_dir=os.path.dirname(card_dir))
        out["card"] = _quality_card_detail(lib.load(os.path.basename(card_dir)))
    return out


@router.post("/quality-templates/import", status_code=status.HTTP_201_CREATED)
async def import_quality_template(
    request: Request,
    template_id: str = Form(..., description="模板 id,英文数字与连字符"),
    name: str = Form(...),
    qual_file: UploadFile = File(..., description=".ansa_qual 判定侧"),
    mpar_file: Optional[UploadFile] = File(None, description=".ansa_mpar 生成侧,可省略"),
    source: str = Form(""),
    revision: str = Form(""),
    scope: str = Form(""),
    description: str = Form(""),
    user: str = Depends(current_user),
) -> Dict:
    """导入客户自己的质量卡,成为模板库里的一张模板。

    mpar 允许缺省:有些客户只给判定准则、不给网格参数。缺省时沿用内置模板的
    生成侧参数,并在 description 里注明——比塞一份空文件强,后者会让人以为
    客户规定了这些参数。
    """
    import shutil

    from .quality import QualityCardLibrary, ansa_mpar, ansa_qual
    from .quality.library import BUILTIN_DIR, CRITERIA_FILE, MESH_FILE, META_FILE

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", template_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "模板 id 只能用字母数字与 - _,长度 2~64")

    lib = _quality_library(request)
    dest = os.path.join(lib.user_dir, template_id)
    if os.path.exists(dest):
        raise HTTPException(status.HTTP_409_CONFLICT, f"模板已存在: {template_id}")

    qual_text = (await qual_file.read()).decode("utf-8", errors="replace")
    try:
        parsed = ansa_qual.loads(qual_text)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"解析 .ansa_qual 失败: {e}")
    if not parsed.data.criteria:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "这份 .ansa_qual 里没有解析出任何判据,请确认文件是否正确")

    mpar_text = ""
    inherited = False
    if mpar_file is not None:
        mpar_text = (await mpar_file.read()).decode("utf-8", errors="replace")
        try:
            ansa_mpar.loads(mpar_text)
        except Exception as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"解析 .ansa_mpar 失败: {e}")
    else:
        with open(os.path.join(BUILTIN_DIR, DEFAULT_QUALITY_TEMPLATE, MESH_FILE),
                  encoding="utf-8", newline="") as f:
            mpar_text = f.read()
        inherited = True

    os.makedirs(dest)
    try:
        with open(os.path.join(dest, CRITERIA_FILE), "w", encoding="utf-8", newline="") as f:
            f.write(qual_text)
        with open(os.path.join(dest, MESH_FILE), "w", encoding="utf-8", newline="") as f:
            f.write(mpar_text)
        note = description
        if inherited:
            note = (note + " " if note else "") + \
                   f"（未提供 .ansa_mpar，生成侧参数沿用内置模板 {DEFAULT_QUALITY_TEMPLATE}）"
        with open(os.path.join(dest, META_FILE), "w", encoding="utf-8") as f:
            json.dump({
                "id": template_id, "name": name, "source": source, "revision": revision,
                "scope": scope, "description": note, "builtin": False,
                "based_on": "", "created_by": user, "overrides": [],
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise

    log.info("导入质量卡模板 %s by=%s 判据 %d 条 mpar=%s",
             template_id, user, len(parsed.data.criteria),
             "继承内置" if inherited else "随包上传")
    return _quality_card_detail(lib.load(template_id))


def _manageable_template(request: Request, template_id: str, user: str, is_admin: bool):
    """删/改模板前的权限闸:内置不可动;用户模板只有导入者或管理员可动。

    老模板 created_by 为空(加字段前导入的),视为仅管理员可管——宁可收紧,
    模板是全局共享资产,谁都能删等于谁都不敢用。
    """
    lib = _quality_library(request)
    try:
        meta = lib.get_meta(template_id)
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    if meta.builtin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "内置模板不可修改或删除")
    if not is_admin and (not meta.created_by or meta.created_by != user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有导入者或管理员可以管理此模板")
    return lib, meta


class QualityTemplateMetaUpdate(BaseModel):
    """只改元数据,不碰阈值——阈值改动必须走派生留痕,这里不能成为绕过口子。"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    source: Optional[str] = None
    revision: Optional[str] = None
    scope: Optional[str] = None
    description: Optional[str] = None


@router.patch("/quality-templates/{template_id}")
def update_quality_template(
    request: Request,
    template_id: str,
    body: QualityTemplateMetaUpdate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    from dataclasses import asdict

    lib, _ = _manageable_template(request, template_id, user, is_admin)
    try:
        meta = lib.update_meta(template_id, **body.model_dump(exclude_unset=True))
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    log.info("更新质量卡模板元数据 %s by=%s", template_id, user)
    return asdict(meta)


@router.delete("/quality-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_quality_template(
    request: Request,
    template_id: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> None:
    """删用户模板。既有项目实例不受影响——实例文件在派生时已拷进项目目录。"""
    lib, _ = _manageable_template(request, template_id, user, is_admin)
    try:
        lib.delete(template_id)
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    log.info("删除质量卡模板 %s by=%s", template_id, user)


class QualityTemplateContentEdit(BaseModel):
    """在线修改用户模板的内容(阈值/网格参数)。

    与实例编辑同一条规矩:每项改动必须带依据(source)。改动追加进模板的
    overrides 留痕,原样落进 .ansa_qual/.ansa_mpar——模板改完仍是可直接交回
    ANSA 的合法卡。只影响之后从它派生的实例;既有实例的文件早已拷走。
    """
    overrides: List[Dict] = Field(min_length=1)


@router.patch("/quality-templates/{template_id}/content")
def edit_quality_template_content(
    request: Request,
    template_id: str,
    body: QualityTemplateContentEdit,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    from dataclasses import asdict

    lib, _ = _manageable_template(request, template_id, user, is_admin)
    overrides = _overrides_from_request(body.overrides, user)
    try:
        meta = lib.edit_content(template_id, overrides)
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except (KeyError, ValueError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    log.info("在线编辑质量卡模板 %s 新增 %d 项覆盖 by=%s", template_id, len(overrides), user)
    out = _quality_card_detail(lib.load(template_id))
    out["overrides"] = [asdict(o) for o in meta.overrides]
    return out


class QualityTemplateDerive(BaseModel):
    """从既有模板派生一张新的用户模板。

    内置模板的内容不可直接改——想改它,就以它为底座派生一张自己的,改动逐项
    留痕。这也是"从零填一张"之外唯一的建卡方式:从零填必然漏项,漏掉的项会
    静默变成"不检查"。
    """
    new_id: str
    name: str = Field(min_length=1, max_length=200)
    overrides: List[Dict] = Field(default_factory=list)
    source: str = ""
    revision: str = ""
    scope: str = ""
    description: str = ""


@router.post("/quality-templates/{template_id}/derive", status_code=status.HTTP_201_CREATED)
def derive_quality_template(
    request: Request,
    template_id: str,
    body: QualityTemplateDerive,
    user: str = Depends(current_user),
) -> Dict:
    from dataclasses import asdict

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", body.new_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "模板 id 只能用字母数字与 - _,长度 2~64")
    lib = _quality_library(request)
    try:
        lib.get_meta(template_id)
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    overrides = _overrides_from_request(body.overrides, user)
    try:
        meta = lib.derive(template_id, body.new_id, body.name, overrides,
                          source=body.source, revision=body.revision, scope=body.scope,
                          description=body.description, created_by=user)
    except FileExistsError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    except (KeyError, ValueError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    log.info("派生质量卡模板 %s ← %s 覆盖 %d 项 by=%s",
             body.new_id, template_id, len(meta.overrides), user)
    return asdict(meta)


class QualityCardEdit(BaseModel):
    """在线编辑一张已派生的质量卡实例。

    与派生时同一条规矩:每项改动必须带依据。改动直接落到 .ansa_qual/.ansa_mpar
    上,因此编辑完的卡仍然是一份可直接交回 ANSA 的合法卡。
    """
    overrides: List[Dict] = Field(default_factory=list)


@router.patch("/quality-cards/{qid}")
def edit_quality_card(
    request: Request,
    qid: str,
    body: QualityCardEdit,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """在线改阈值/网格参数。改动累加进 overrides,原样落进 ANSA 卡文件。"""
    from dataclasses import asdict

    from .quality.library import _apply_override
    from .quality import ansa_mpar, ansa_qual
    from .quality.library import CRITERIA_FILE, MESH_FILE

    db = _db(request)
    row = _owned_quality_card(db, qid, user, is_admin)
    card_dir = row["card_dir"]
    if not card_dir or not os.path.isdir(card_dir):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "质量卡文件不存在")

    qual = ansa_qual.load(os.path.join(card_dir, CRITERIA_FILE))
    mpar = ansa_mpar.load(os.path.join(card_dir, MESH_FILE))
    applied = []
    for ov in _overrides_from_request(body.overrides, user):
        try:
            applied.append(_apply_override(qual, mpar, ov))
        except (KeyError, ValueError) as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    with open(os.path.join(card_dir, CRITERIA_FILE), "w", encoding="utf-8", newline="") as f:
        f.write(qual.dumps())
    with open(os.path.join(card_dir, MESH_FILE), "w", encoding="utf-8", newline="") as f:
        f.write(mpar.dumps())

    history = _json_or_none(row["overrides_json"]) or []
    history.extend(asdict(o) for o in applied)
    db.update_quality_card(qid, overrides_json=json.dumps(history, ensure_ascii=False))
    log.info("在线编辑质量卡 %s 新增 %d 项覆盖 by=%s", qid, len(applied), user)
    return get_project_quality_card(request, qid, user, is_admin)


@router.get("/quality-cards/{qid}/export")
def export_quality_card(
    request: Request,
    qid: str,
    kind: str = Query("qual", description="qual=判定侧 .ansa_qual, mpar=生成侧 .ansa_mpar"),
    user: str = Depends(current_user_query),
    is_admin: bool = Depends(is_admin_request),
):
    """导出给 ANSA 用。文件是派生时就写好的,这里原样交付——不做二次生成,
    避免"平台里看到的"与"ANSA 收到的"变成两份真相。"""
    from fastapi.responses import FileResponse

    from .quality.library import CRITERIA_FILE, MESH_FILE

    db = _db(request)
    row = _owned_quality_card(db, qid, user, is_admin)
    card_dir = row["card_dir"]
    if not card_dir or not os.path.isdir(card_dir):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "质量卡文件不存在")
    fname = CRITERIA_FILE if kind == "qual" else MESH_FILE
    path = os.path.join(card_dir, fname)
    if not os.path.isfile(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"缺少 {fname}")
    return FileResponse(path, filename=f"{row['name']}{os.path.splitext(fname)[1]}",
                        media_type="application/octet-stream")


@router.delete("/quality-cards/{qid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project_quality_card(
    request: Request,
    qid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> None:
    import shutil

    db = _db(request)
    row = _owned_quality_card(db, qid, user, is_admin)
    if row["card_dir"] and os.path.isdir(row["card_dir"]):
        shutil.rmtree(row["card_dir"], ignore_errors=True)
    db.delete_quality_card(qid)


# --- 材料库 ---------------------------------------------------------------
# 全局资产：读对所有登录用户开放，写仅管理员——错误的材料数据会污染其后
# 所有引用它的计算。设计见 docs/sdm-material-library.md。

MATERIAL_CURVE_JSON = ("condition_json", "points_json", "scale_json")
MATERIAL_CARD_JSON = ("params_json",)

# 关键字文本上限。种子库 76 张卡才 400KB，50MB 足够容纳任何真实材料库；
# 再大的多半是整车 deck 传错了文件。
_MATERIAL_FILE_MAX = 50 * 1024 * 1024


def _require_admin(is_admin: bool) -> None:
    if not is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "材料库仅管理员可维护")


@router.get("/materials")
def list_materials(
    request: Request,
    category: Optional[str] = Query(None),
    q: Optional[str] = Query(None, max_length=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    user: str = Depends(current_user),
) -> List[Dict]:
    return [_row(r) for r in _db(request).list_materials(category, q, status_filter)]


@router.get("/materials/{mid}")
def get_material(
    request: Request,
    mid: str,
    user: str = Depends(current_user),
) -> Dict:
    db = _db(request)
    row = db.get_material(mid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "材料不存在")
    return {
        **_row(row),
        "properties": [_row(p, ("condition_json",)) for p in db.material_properties(mid)],
        "curves": [_row(c, MATERIAL_CURVE_JSON) for c in db.material_curves(mid)],
        "cards": [_row(k, MATERIAL_CARD_JSON) for k in db.material_cards(mid)],
    }


@router.post("/materials/import", status_code=status.HTTP_201_CREATED)
async def import_materials(
    request: Request,
    file: UploadFile = File(..., description="LS-DYNA 关键字文件（.k/.key）"),
    unit_system: str = Form("t-mm-s"),
    solver_type: str = Form("lsdyna"),
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    """导入关键字文件里的材料段。幂等：内容未变的材料跳过，变了的整体替换
    并 revision+1——半新半旧的材料比过时的更危险。"""
    from .materials import import_material_text

    _require_admin(is_admin)
    data = await file.read()
    if len(data) > _MATERIAL_FILE_MAX:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            "文件过大，确认这是材料库而不是整车 deck")
    report = import_material_text(
        _db(request),
        data.decode("utf-8", errors="replace"),
        unit_system=unit_system,
        solver_type=solver_type,
        source=file.filename or "",
        actor=user,
    )
    log.info("导入材料库 %s by=%s: +%d ~%d =%d",
             file.filename, user, report["materials_created"],
             report["materials_updated"], report["materials_unchanged"])
    return report


class MaterialUpdate(BaseModel):
    """元数据编辑。性能/曲线/卡不在此列——那些只能整体走导入（新修订），
    单点改数会让卡原文与结构化参数两处真相分叉。"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    category: Optional[str] = Field(None, max_length=40)
    standard_code: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=2000)
    source: Optional[str] = Field(None, max_length=500)
    status: Optional[str] = Field(None, pattern="^(active|deprecated)$")


@router.patch("/materials/{mid}")
def update_material(
    request: Request,
    mid: str,
    body: MaterialUpdate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    _require_admin(is_admin)
    db = _db(request)
    if db.get_material(mid) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "材料不存在")
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields:
        other = db.get_material_by_name(fields["name"])
        if other is not None and other["id"] != mid:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"已存在同名材料: {fields['name']}")
    db.update_material(mid, **fields)
    log.info("更新材料 %s by=%s: %s", mid, user, list(fields))
    return _row(db.get_material(mid))


@router.delete("/materials/{mid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_material(
    request: Request,
    mid: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> None:
    _require_admin(is_admin)
    db = _db(request)
    row = db.get_material(mid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "材料不存在")
    db.delete_material(mid)
    log.info("删除材料 %s(%s) by=%s", row["name"], mid, user)


# --- 材料模板文件（组装式）-----------------------------------------------
# 路径用独立前缀而非 /materials/templates：后者会被 /materials/{mid} 抢先匹配。


class MaterialTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    unit_system: str = Field(..., max_length=40)
    description: str = Field("", max_length=2000)
    solver_type: str = Field("lsdyna", max_length=40)
    card_ids: List[str] = Field(default_factory=list)


class MaterialTemplateUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    unit_system: Optional[str] = Field(None, max_length=40)
    status: Optional[str] = Field(None, pattern="^(active|deprecated)$")
    summary_json: Optional[str] = None


class MaterialTemplateItems(BaseModel):
    card_ids: List[str]


@router.get("/material-templates")
def list_material_templates(
    request: Request,
    status_filter: Optional[str] = Query(None, alias="status"),
    user: str = Depends(current_user),
) -> List[Dict]:
    return [_row(r) for r in _db(request).list_material_templates(status_filter)]


@router.post("/material-templates/check")
def check_material_template_api(
    request: Request,
    body: MaterialTemplateItems,
    unit_system: Optional[str] = Query(None),
    user: str = Depends(current_user),
) -> Dict:
    """入库前先看能不能组装。单位制不一致、MID/LCID 撞车都在这里拦下——
    这几类问题进了 deck 求解器不会报错，但结果是错的。"""
    from .materials.templates import check_material_template
    return check_material_template(_db(request), body.card_ids, unit_system)


@router.post("/material-templates", status_code=status.HTTP_201_CREATED)
def create_material_template(
    request: Request,
    body: MaterialTemplateCreate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    _require_admin(is_admin)
    from .materials.templates import check_material_template
    db = _db(request)
    if db.get_material_template_by_name(body.name) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"已存在同名模板: {body.name}")
    if body.card_ids:
        chk = check_material_template(db, body.card_ids, body.unit_system)
        if not chk["ok"]:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                {"message": "成员卡无法组装", "problems": chk["problems"]})
    tid = db.create_material_template(body.name, body.unit_system, body.description,
                                      body.solver_type, created_by=user)
    if body.card_ids:
        db.set_material_template_items(tid, body.card_ids)
    log.info("新建材料模板 %s(%s) 成员 %d by=%s", body.name, tid, len(body.card_ids), user)
    return _row(db.get_material_template(tid))


@router.get("/material-templates/{tid}")
def get_material_template(request: Request, tid: str,
                          user: str = Depends(current_user)) -> Dict:
    db = _db(request)
    row = db.get_material_template(tid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    out = _row(row)
    out["items"] = [_row(r) for r in db.list_material_template_items(tid)]
    return out


@router.put("/material-templates/{tid}/items")
def set_material_template_items(
    request: Request,
    tid: str,
    body: MaterialTemplateItems,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    _require_admin(is_admin)
    from .materials.templates import check_material_template
    db = _db(request)
    tpl = db.get_material_template(tid)
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    chk = check_material_template(db, body.card_ids, tpl["unit_system"])
    if not chk["ok"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            {"message": "成员卡无法组装", "problems": chk["problems"]})
    db.set_material_template_items(tid, body.card_ids)
    log.info("模板 %s 成员更新为 %d 张 by=%s", tid, len(body.card_ids), user)
    return _row(db.get_material_template(tid))


@router.patch("/material-templates/{tid}")
def update_material_template(
    request: Request,
    tid: str,
    body: MaterialTemplateUpdate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    _require_admin(is_admin)
    db = _db(request)
    if db.get_material_template(tid) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields:
        other = db.get_material_template_by_name(fields["name"])
        if other is not None and other["id"] != tid:
            raise HTTPException(status.HTTP_409_CONFLICT, f"已存在同名模板: {fields['name']}")
    db.update_material_template(tid, **fields)
    return _row(db.get_material_template(tid))


@router.delete("/material-templates/{tid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_material_template(
    request: Request, tid: str,
    user: str = Depends(current_user), is_admin: bool = Depends(is_admin_request),
) -> None:
    _require_admin(is_admin)
    db = _db(request)
    if db.get_material_template(tid) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    db.delete_material_template(tid)
    log.info("删除材料模板 %s by=%s", tid, user)


class TemplateParseTicket(BaseModel):
    tid: str
    kind: str
    token: str
    expires_in: int
    source_name: str
    source_path_suffix: str
    unit_system: str
    version: str
    expected_sha256: str = ""


@router.post("/material-templates/{tid}/parse-ticket")
def create_material_parse_ticket(
    request: Request,
    tid: str,
    ttl_seconds: int = Query(900, ge=60, le=3600),
    user: str = Depends(current_user),
) -> TemplateParseTicket:
    """为 vektor3d cae.template.parse 签票据（只读本模板导出正文）。

    15 分钟就够：解析是纯计算、估时 20 秒，不像网格作业要跑几十分钟。
    票据越短越好——它要交到浏览器再转给桌面进程。
    """
    db = _db(request)
    row = db.get_material_template(tid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    token = issue_scoped_token(user, CAE_TEMPLATE_SCOPE, ttl_seconds,
                               tid=tid, kind="material")
    log.info("签发材料模板解析票据 tid=%s user=%s", tid, user)
    return TemplateParseTicket(
        tid=tid, kind="material", token=token, expires_in=ttl_seconds,
        source_name=f"MAT_{row['name']}.k",
        source_path_suffix=f"/sim/material-templates/{tid}/export",
        unit_system=row["unit_system"],
        # 版本号带 revision:成员卡换了但 revision 不变时,缓存该失效——
        # 故拼上成员数与 updated_at,任一变化都是新版本。
        version=f"r{row['revision']}-{int(row['updated_at'])}",
    )


@router.post("/control-templates/{tid}/parse-ticket")
def create_control_parse_ticket(
    request: Request,
    tid: str,
    ttl_seconds: int = Query(900, ge=60, le=3600),
    user: str = Depends(current_user),
) -> TemplateParseTicket:
    """同上，控制卡侧。expected_sha256 用入库时记的指纹，供能力侧判缓存是否已脏。"""
    db = _db(request)
    row = db.get_control_template(tid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "控制卡模板不存在")
    token = issue_scoped_token(user, CAE_TEMPLATE_SCOPE, ttl_seconds,
                               tid=tid, kind="control")
    log.info("签发控制卡解析票据 tid=%s user=%s", tid, user)
    return TemplateParseTicket(
        tid=tid, kind="control", token=token, expires_in=ttl_seconds,
        source_name=row["source_name"] or f"{row['name']}.k",
        source_path_suffix=f"/sim/control-templates/{tid}/export",
        unit_system=row["unit_system"],
        version=f"r{row['revision']}-{int(row['updated_at'])}",
        expected_sha256=row["source_sha256"] or "",
    )


@router.get("/material-templates/{tid}/export")
def export_material_template(request: Request, tid: str,
                             user: str = Depends(_material_template_principal)) -> Response:
    """导出一份可 *INCLUDE 的 MAT.K：各卡原文原样拼接，不重新生成。

    也是 vektor3d cae.template.parse 的 sourceUrl —— 故收解析票据，
    否则桌面端能力拉不到正文（它拿不到用户的会话令牌）。
    """
    from .materials.templates import render_material_template
    db = _db(request)
    if db.get_material_template(tid) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    text = render_material_template(db, tid)
    return Response(
        content=text, media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="MAT_{tid[:8]}.k"'},
    )


# --- 控制卡模板库（整份存档）---------------------------------------------


class ControlTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    unit_system: str = Field(..., max_length=40)
    keyword_text: str = Field(..., min_length=1)
    analysis_type: str = Field("", max_length=80)
    description: str = Field("", max_length=2000)
    solver_type: str = Field("lsdyna", max_length=40)
    source_name: str = Field("", max_length=200)


class ControlTemplateUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    analysis_type: Optional[str] = Field(None, max_length=80)
    unit_system: Optional[str] = Field(None, max_length=40)
    status: Optional[str] = Field(None, pattern="^(active|deprecated)$")
    keyword_text: Optional[str] = None
    summary_json: Optional[str] = None


@router.get("/control-templates")
def list_control_templates(
    request: Request,
    analysis_type: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    user: str = Depends(current_user),
) -> List[Dict]:
    return [_row(r) for r in _db(request).list_control_templates(analysis_type, status_filter)]


@router.post("/control-templates", status_code=status.HTTP_201_CREATED)
def create_control_template(
    request: Request,
    body: ControlTemplateCreate,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> Dict:
    _require_admin(is_admin)
    import hashlib
    db = _db(request)
    if db.get_control_template_by_name(body.name) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"已存在同名控制卡模板: {body.name}")
    sha = hashlib.sha256(body.keyword_text.encode("utf-8")).hexdigest()[:16]
    tid = db.create_control_template(
        body.name, body.unit_system, body.keyword_text, body.analysis_type,
        body.description, body.solver_type, source_name=body.source_name,
        source_sha256=sha, created_by=user)
    log.info("新建控制卡模板 %s(%s) 类型=%s by=%s", body.name, tid, body.analysis_type, user)
    return _row(db.get_control_template(tid))


@router.get("/control-templates/{tid}")
def get_control_template(request: Request, tid: str,
                         user: str = Depends(current_user)) -> Dict:
    row = _db(request).get_control_template(tid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "控制卡模板不存在")
    return _row(row)


@router.patch("/control-templates/{tid}")
def update_control_template(
    request: Request, tid: str, body: ControlTemplateUpdate,
    user: str = Depends(current_user), is_admin: bool = Depends(is_admin_request),
) -> Dict:
    _require_admin(is_admin)
    db = _db(request)
    if db.get_control_template(tid) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "控制卡模板不存在")
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields:
        other = db.get_control_template_by_name(fields["name"])
        if other is not None and other["id"] != tid:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"已存在同名控制卡模板: {fields['name']}")
    db.update_control_template(tid, **fields)
    return _row(db.get_control_template(tid))


@router.delete("/control-templates/{tid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_control_template(
    request: Request, tid: str,
    user: str = Depends(current_user), is_admin: bool = Depends(is_admin_request),
) -> None:
    _require_admin(is_admin)
    db = _db(request)
    if db.get_control_template(tid) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "控制卡模板不存在")
    db.delete_control_template(tid)
    log.info("删除控制卡模板 %s by=%s", tid, user)


@router.get("/control-templates/{tid}/export")
def export_control_template(request: Request, tid: str,
                            user: str = Depends(_control_template_principal)) -> Response:
    """整份原样导出。同材料模板，兼作 cae.template.parse 的 sourceUrl。"""
    from .materials.templates import render_control_template
    db = _db(request)
    if db.get_control_template(tid) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "控制卡模板不存在")
    text = render_control_template(db, tid)
    return Response(
        content=text, media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="control_{tid[:8]}.k"'},
    )


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
    # 显式传 null = 解除网格绑定。exclude_none 的部分更新约定表达不了这个
    # 语义，单独识别：没有它，删几何时 409 里"先解绑"的指引就是一句空话。
    if "sim_mesh_version_id" in body.model_fields_set and body.sim_mesh_version_id is None:
        db.clear_subject_mesh(sid)
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
