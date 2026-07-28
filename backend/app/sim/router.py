"""仿真设计路由。

属主隔离与现有 jobs 路由一致：普通用户只能看到/操作自己的仿真项目，
管理员（is_admin_request）可跨用户查看。所有下级实体（分析对象/几何/网格/
工况/作业/结果）的权限都由其所属项目的 owner 决定——单一判据，不重复实现。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
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
GEOM_JSON = ("source_file_json", "topo_summary_json")
MESH_JSON = ("mesh_params_json", "quality_json")
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


def _quality_library(request: Request):
    """模板库句柄。用户模板落数据目录,内置模板随代码走。

    用户目录放在 scratch 之外的固定位置:模板是长期资产,不能跟着临时目录被清掉。
    """
    from .quality import QualityCardLibrary

    root = os.path.join(os.path.dirname(get_settings().db_path), "quality-templates")
    return QualityCardLibrary(user_dir=root)


def _quality_card_detail(card) -> Dict:
    """把一张卡摊平成前端能直接渲染的结构。

    只回启用的判据:那张 5mm 卡 28 条 shells 判据里只开了 11 条,把关掉的一起
    铺给用户看,等于让他以为那些也在管。
    """
    from .quality import estimate_time_step

    criteria = [
        {
            "name": c.name,
            "domain": c.domain,
            "calculation": c.calculation,       # 算法族:同一指标不同算法数值不同
            "weight": c.weight,
            "higherIsBetter": c.higher_is_better,
            "thresholds": c.thresholds,
        }
        for c in card.criteria.enabled_criteria("shells")
    ]
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


@router.get("/geometries/{gid}/download")
def download_geometry(
    request: Request,
    gid: str,
    user: str = Depends(_geometry_principal),
    is_admin: bool = Depends(_geometry_is_admin),
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
        written = write_file(proj["owner"], parent, name, data,
                             get_settings().fs_root_list)
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
    rid: str,
    token: Optional[str] = Query(None),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    act_as: Optional[str] = Header(None, alias="X-Act-As-User"),
) -> str:
    """需求文档下载的调用者:常规用户令牌,或本 rid 的分析票据(vektor3d 用)。"""
    raw = token or (creds.credentials if creds else None)
    return resolve_scoped_principal(
        raw, act_as, scope=DOC_ANALYZE_SCOPE, bindings={"rid": rid}
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
    return FileResponse(path, filename=src.get("name") or os.path.basename(path),
                        media_type="application/octet-stream")


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
    _owned_requirement(db, rid, user, is_admin)

    items = []
    for raw in body.items:
        if not str(raw.get("title") or "").strip():
            continue
        items.append({**raw, "extracted_by": raw.get("extracted_by") or body.extractor})
    if not items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有可写入的条目")

    count = db.replace_requirement_items(rid, items)
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


# --- 质量卡模板库与项目实例 ---------------------------------------------

@router.get("/quality-templates")
def list_quality_templates(
    request: Request,
    user: str = Depends(current_user),
) -> List[Dict]:
    """模板库:内置只读 + 用户自建。内置那张是所有项目的缺省底座。"""
    from dataclasses import asdict

    return [asdict(t) for t in _quality_library(request).list_templates()]


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

    from .quality import Override, QualityCardLibrary

    db = _db(request)
    proj = _owned_project(db, pid, user, is_admin)
    card_root = os.path.join(_project_workdir(db, proj), "sdm_quality_cards")
    lib = QualityCardLibrary(user_dir=card_root)

    overrides = []
    for raw in body.overrides:
        if not str(raw.get("source") or "").strip():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"覆盖项 {raw.get('target')} 缺少依据(source)——无出处的阈值会一路流进网格验收",
            )
        overrides.append(Override(
            target=str(raw.get("target") or ""),
            old_value="",
            new_value=str(raw.get("new_value") or ""),
            source=str(raw.get("source") or ""),
            by=str(raw.get("by") or user),
        ))

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
                "based_on": "", "overrides": [],
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise

    log.info("导入质量卡模板 %s by=%s 判据 %d 条 mpar=%s",
             template_id, user, len(parsed.data.criteria),
             "继承内置" if inherited else "随包上传")
    return _quality_card_detail(lib.load(template_id))


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

    from .quality import Override
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
    for raw in body.overrides:
        if not str(raw.get("source") or "").strip():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"覆盖项 {raw.get('target')} 缺少依据(source)——无出处的阈值会一路流进网格验收",
            )
        ov = Override(target=str(raw.get("target") or ""), old_value="",
                      new_value=str(raw.get("new_value") or ""),
                      source=str(raw.get("source") or ""), by=str(raw.get("by") or user))
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
