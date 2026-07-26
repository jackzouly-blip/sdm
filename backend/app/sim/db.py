"""仿真设计数据库（SQLite）。

SDM 的领域模型：从"要算什么"到"算出了什么"的完整链路。

    sim_project              仿真项目
    ├─ sim_analysis_target   分析对象
    │  └─ sim_geometry_version   几何版本
    │     └─ sim_mesh_version    网格版本
    └─ sim_subject           工况
       └─ sim_job            作业
          └─ sim_result      结果

设计要点：

1. **执行不在这里实现**。`sim_job.hpc_jobid` 软引用 portal.db 的 `jobs.jobid`，
   真正的提交、轮询、提取仍由现有 HPC 链路负责。现有 jobs 保留"裸作业"语义
   （手工提交的照常可用），sim_job 只是其上的编排层——现有功能零回归。
   跨库故无法建外键，引用完整性由应用层保证（与 netdisk_queue 引用 jobid 同例）。

2. **sim_template 的三个 json 是 AI 的约束边界**：schema_json 定义可填什么、
   validation_rules_json 定义什么算对、export_mapping_json 定义如何导出求解器
   输入卡。Agent 不自由生成输入卡，而是填一个受约束的结构。

3. 沿用本项目"一个子系统一个 db 文件"的既有约定（portal.db / tasks.db /
   templates.db / favorites.db 各自独立）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from ..logger import get_logger

log = get_logger(__name__)

# 项目状态
PROJECT_ACTIVE = "active"
PROJECT_ARCHIVED = "archived"

# 工况状态：草稿 → 就绪(可提交) → 运行中 → 完成/失败
SUBJECT_DRAFT = "draft"
SUBJECT_READY = "ready"
SUBJECT_RUNNING = "running"
SUBJECT_DONE = "done"
SUBJECT_FAILED = "failed"

# 作业状态。与 PBS 状态解耦：这里记录 SDM 视角的生命周期，
# PBS 细节留在 portal.db 的 jobs 行里。
JOB_DRAFT = "draft"
JOB_SUBMITTED = "submitted"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"

_SCHEMA = """
PRAGMA foreign_keys = ON;

-- 仿真项目。dbit_project_code 是来源侧的软引用（可空：SDM 内部直接发起的项目没有）。
CREATE TABLE IF NOT EXISTS sim_project (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    description       TEXT,
    owner             TEXT NOT NULL,     -- 系统账号(PAM)，作业以其身份执行
    dbit_project_code TEXT,              -- 来源 dbit 项目编码
    status            TEXT NOT NULL DEFAULT 'active',
    default_solver    TEXT,
    unit_system       TEXT NOT NULL DEFAULT 'SI',
    workdir           TEXT,              -- 项目在集群上的根目录
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_project_owner ON sim_project(owner);
CREATE INDEX IF NOT EXISTS idx_sim_project_status ON sim_project(status);
CREATE INDEX IF NOT EXISTS idx_sim_project_dbit ON sim_project(dbit_project_code);

-- 分析对象。source_ref_json 记录外部来源（dbit 产品数据 / vektor3d BOM 对象），
-- 不建外键：来源系统在库外。
CREATE TABLE IF NOT EXISTS sim_analysis_target (
    id              TEXT PRIMARY KEY,
    sim_project_id  TEXT NOT NULL REFERENCES sim_project(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    target_type     TEXT NOT NULL,       -- part/assembly/system
    source_ref_json TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_target_project ON sim_analysis_target(sim_project_id);

-- 几何版本。同一分析对象下 version_no 递增且唯一。
CREATE TABLE IF NOT EXISTS sim_geometry_version (
    id                TEXT PRIMARY KEY,
    sim_target_id     TEXT NOT NULL REFERENCES sim_analysis_target(id) ON DELETE CASCADE,
    version_no        INTEGER NOT NULL,
    source_type       TEXT NOT NULL,     -- upload/vektor3d/dbit
    source_file_json  TEXT,
    step_file         TEXT,
    brep_file         TEXT,
    lightweight_file  TEXT,
    topo_summary_json TEXT,
    status            TEXT NOT NULL DEFAULT 'ready',
    created_at        REAL NOT NULL,
    UNIQUE(sim_target_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_sim_geom_target ON sim_geometry_version(sim_target_id);

-- 网格版本。mesh_engine 记录由谁产出：manual（人工上传）或某个能力 ID
-- （如 vektor3d:mesh.generate）——一期用 manual，vektor3d 能力就绪后切换。
CREATE TABLE IF NOT EXISTS sim_mesh_version (
    id                     TEXT PRIMARY KEY,
    sim_geometry_version_id TEXT NOT NULL REFERENCES sim_geometry_version(id) ON DELETE CASCADE,
    version_no             INTEGER NOT NULL,
    mesh_type              TEXT NOT NULL,
    mesh_engine            TEXT NOT NULL,
    mesh_params_json       TEXT,
    mesh_file              TEXT,
    quality_json           TEXT,         -- 网格检查产出
    status                 TEXT NOT NULL DEFAULT 'ready',
    created_at             REAL NOT NULL,
    UNIQUE(sim_geometry_version_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_sim_mesh_geom ON sim_mesh_version(sim_geometry_version_id);

-- 工况模板：一类工况的方法论载体，三个 json 共同构成 AI 的约束边界。
CREATE TABLE IF NOT EXISTS sim_template (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    subject_type          TEXT NOT NULL,
    solver_type           TEXT NOT NULL,
    schema_json           TEXT NOT NULL DEFAULT '{}',
    default_values_json   TEXT,
    validation_rules_json TEXT,
    export_mapping_json   TEXT,
    is_builtin            INTEGER NOT NULL DEFAULT 0,
    created_at            REAL NOT NULL,
    updated_at            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_template_type ON sim_template(subject_type, solver_type);

-- 工况：一次具体的计算设定。config_json 受 template.schema_json 约束。
CREATE TABLE IF NOT EXISTS sim_subject (
    id                  TEXT PRIMARY KEY,
    sim_project_id      TEXT NOT NULL REFERENCES sim_project(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    subject_type        TEXT NOT NULL,
    solver_type         TEXT NOT NULL,
    template_id         TEXT REFERENCES sim_template(id),
    sim_mesh_version_id TEXT REFERENCES sim_mesh_version(id),
    config_json         TEXT NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'draft',
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_subject_project ON sim_subject(sim_project_id);
CREATE INDEX IF NOT EXISTS idx_sim_subject_status ON sim_subject(status);

-- 作业：把工况投递到 HPC 的一次尝试。hpc_jobid 跨库软引用 portal.db 的 jobs.jobid。
CREATE TABLE IF NOT EXISTS sim_job (
    id                  TEXT PRIMARY KEY,
    sim_subject_id      TEXT NOT NULL REFERENCES sim_subject(id) ON DELETE CASCADE,
    sim_mesh_version_id TEXT,
    hpc_jobid           TEXT,
    submit_mode         TEXT NOT NULL DEFAULT 'pbs',   -- pbs/trial
    submit_payload_json TEXT,
    status              TEXT NOT NULL DEFAULT 'draft',
    error_message       TEXT,
    created_at          REAL NOT NULL,
    submitted_at        REAL,
    finished_at         REAL
);
CREATE INDEX IF NOT EXISTS idx_sim_job_subject ON sim_job(sim_subject_id);
CREATE INDEX IF NOT EXISTS idx_sim_job_status ON sim_job(status);
CREATE INDEX IF NOT EXISTS idx_sim_job_hpc ON sim_job(hpc_jobid);

-- 结果：result_type 是查看器插件的分发键（碰撞/CFD/NVH/疲劳各自注册）。
CREATE TABLE IF NOT EXISTS sim_result (
    id          TEXT PRIMARY KEY,
    sim_job_id  TEXT NOT NULL REFERENCES sim_job(id) ON DELETE CASCADE,
    result_type TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    meta_json   TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_result_job ON sim_result(sim_job_id);
CREATE INDEX IF NOT EXISTS idx_sim_result_type ON sim_result(result_type);
"""


def _uid() -> str:
    return uuid.uuid4().hex


class SimDB:
    """仿真设计数据访问层。

    与 JobsDB 同构：请求线程与后台线程都会访问，故 check_same_thread=False + 锁。
    """

    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- 内部工具 -------------------------------------------------------

    def _one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def _write(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount

    def _update(self, table: str, pk: str, fields: Dict, touch: bool = True) -> None:
        """按传入字段做部分更新；空字典则不动。touch=True 时顺带刷新 updated_at。"""
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return
        if touch:
            fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        self._write(
            f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), pk)
        )

    # --- 项目 -----------------------------------------------------------

    def create_project(
        self,
        name: str,
        owner: str,
        description: Optional[str] = None,
        dbit_project_code: Optional[str] = None,
        default_solver: Optional[str] = None,
        unit_system: str = "SI",
        workdir: Optional[str] = None,
    ) -> str:
        pid = _uid()
        now = time.time()
        self._write(
            """INSERT INTO sim_project
               (id, name, description, owner, dbit_project_code, status,
                default_solver, unit_system, workdir, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, name, description, owner, dbit_project_code, PROJECT_ACTIVE,
             default_solver, unit_system, workdir, now, now),
        )
        return pid

    def get_project(self, pid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_project WHERE id=?", (pid,))

    def list_projects(
        self, owner: Optional[str] = None, status: Optional[str] = None
    ) -> List[sqlite3.Row]:
        """列项目。owner=None 表示不限属主（仅管理员视角应如此调用）。"""
        sql = "SELECT * FROM sim_project WHERE 1=1"
        args: list = []
        if owner:
            sql += " AND owner=?"
            args.append(owner)
        if status:
            sql += " AND status=?"
            args.append(status)
        return self._all(sql + " ORDER BY updated_at DESC", tuple(args))

    def update_project(self, pid: str, **fields) -> None:
        allowed = {"name", "description", "status", "default_solver",
                   "unit_system", "workdir"}
        self._update("sim_project", pid, {k: v for k, v in fields.items() if k in allowed})

    def delete_project(self, pid: str) -> bool:
        """删除项目。外键级联清掉分析对象/几何/网格/工况/作业/结果。"""
        return self._write("DELETE FROM sim_project WHERE id=?", (pid,)) > 0

    def project_stats(self, pid: str) -> Dict[str, int]:
        """项目概览计数，供列表页一次性展示，避免前端 N+1 请求。"""
        row = self._one(
            """SELECT
                 (SELECT COUNT(*) FROM sim_analysis_target WHERE sim_project_id=?) AS targets,
                 (SELECT COUNT(*) FROM sim_subject WHERE sim_project_id=?) AS subjects,
                 (SELECT COUNT(*) FROM sim_job j JOIN sim_subject s ON s.id=j.sim_subject_id
                   WHERE s.sim_project_id=?) AS jobs""",
            (pid, pid, pid),
        )
        return {"targets": row["targets"], "subjects": row["subjects"], "jobs": row["jobs"]}

    # --- 分析对象 -------------------------------------------------------

    def create_target(
        self,
        sim_project_id: str,
        name: str,
        target_type: str,
        source_ref: Optional[Dict] = None,
    ) -> str:
        tid = _uid()
        self._write(
            """INSERT INTO sim_analysis_target
               (id, sim_project_id, name, target_type, source_ref_json, created_at)
               VALUES (?,?,?,?,?,?)""",
            (tid, sim_project_id, name, target_type,
             json.dumps(source_ref, ensure_ascii=False) if source_ref else None,
             time.time()),
        )
        return tid

    def get_target(self, tid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_analysis_target WHERE id=?", (tid,))

    def list_targets(self, sim_project_id: str) -> List[sqlite3.Row]:
        return self._all(
            "SELECT * FROM sim_analysis_target WHERE sim_project_id=? ORDER BY created_at",
            (sim_project_id,),
        )

    def delete_target(self, tid: str) -> bool:
        return self._write("DELETE FROM sim_analysis_target WHERE id=?", (tid,)) > 0

    # --- 几何版本 -------------------------------------------------------

    def add_geometry(
        self,
        sim_target_id: str,
        source_type: str,
        source_file: Optional[Dict] = None,
        step_file: Optional[str] = None,
        brep_file: Optional[str] = None,
        lightweight_file: Optional[str] = None,
        topo_summary: Optional[Dict] = None,
    ) -> str:
        """追加一个几何版本，version_no 自动递增。"""
        gid = _uid()
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 AS n FROM sim_geometry_version"
                " WHERE sim_target_id=?",
                (sim_target_id,),
            ).fetchone()
            self.conn.execute(
                """INSERT INTO sim_geometry_version
                   (id, sim_target_id, version_no, source_type, source_file_json,
                    step_file, brep_file, lightweight_file, topo_summary_json,
                    status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (gid, sim_target_id, row["n"], source_type,
                 json.dumps(source_file, ensure_ascii=False) if source_file else None,
                 step_file, brep_file, lightweight_file,
                 json.dumps(topo_summary, ensure_ascii=False) if topo_summary else None,
                 "ready", time.time()),
            )
            self.conn.commit()
        return gid

    def get_geometry(self, gid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_geometry_version WHERE id=?", (gid,))

    def list_geometries(self, sim_target_id: str) -> List[sqlite3.Row]:
        return self._all(
            "SELECT * FROM sim_geometry_version WHERE sim_target_id=? ORDER BY version_no",
            (sim_target_id,),
        )

    # --- 网格版本 -------------------------------------------------------

    def add_mesh(
        self,
        sim_geometry_version_id: str,
        mesh_type: str,
        mesh_engine: str,
        mesh_params: Optional[Dict] = None,
        mesh_file: Optional[str] = None,
        quality: Optional[Dict] = None,
    ) -> str:
        """追加一个网格版本，version_no 自动递增。

        mesh_engine 一期通常是 'manual'（人工上传）；vektor3d 网格能力就绪后
        改为能力 ID，此处无需变更。
        """
        mid = _uid()
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 AS n FROM sim_mesh_version"
                " WHERE sim_geometry_version_id=?",
                (sim_geometry_version_id,),
            ).fetchone()
            self.conn.execute(
                """INSERT INTO sim_mesh_version
                   (id, sim_geometry_version_id, version_no, mesh_type, mesh_engine,
                    mesh_params_json, mesh_file, quality_json, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (mid, sim_geometry_version_id, row["n"], mesh_type, mesh_engine,
                 json.dumps(mesh_params, ensure_ascii=False) if mesh_params else None,
                 mesh_file,
                 json.dumps(quality, ensure_ascii=False) if quality else None,
                 "ready", time.time()),
            )
            self.conn.commit()
        return mid

    def get_mesh(self, mid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_mesh_version WHERE id=?", (mid,))

    def list_meshes(self, sim_geometry_version_id: str) -> List[sqlite3.Row]:
        return self._all(
            "SELECT * FROM sim_mesh_version WHERE sim_geometry_version_id=?"
            " ORDER BY version_no",
            (sim_geometry_version_id,),
        )

    def set_mesh_quality(self, mid: str, quality: Dict) -> None:
        """写入网格检查结果。一期由人工/外部工具填，后续由 mesh.check 能力产出。"""
        self._write(
            "UPDATE sim_mesh_version SET quality_json=? WHERE id=?",
            (json.dumps(quality, ensure_ascii=False), mid),
        )

    # --- 工况模板 -------------------------------------------------------

    def create_template(
        self,
        name: str,
        subject_type: str,
        solver_type: str,
        schema: Optional[Dict] = None,
        default_values: Optional[Dict] = None,
        validation_rules: Optional[Dict] = None,
        export_mapping: Optional[Dict] = None,
        is_builtin: bool = False,
    ) -> str:
        tid = _uid()
        now = time.time()
        self._write(
            """INSERT INTO sim_template
               (id, name, subject_type, solver_type, schema_json, default_values_json,
                validation_rules_json, export_mapping_json, is_builtin,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, name, subject_type, solver_type,
             json.dumps(schema or {}, ensure_ascii=False),
             json.dumps(default_values, ensure_ascii=False) if default_values else None,
             json.dumps(validation_rules, ensure_ascii=False) if validation_rules else None,
             json.dumps(export_mapping, ensure_ascii=False) if export_mapping else None,
             1 if is_builtin else 0, now, now),
        )
        return tid

    def get_template(self, tid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_template WHERE id=?", (tid,))

    def list_templates(
        self, subject_type: Optional[str] = None, solver_type: Optional[str] = None
    ) -> List[sqlite3.Row]:
        sql = "SELECT * FROM sim_template WHERE 1=1"
        args: list = []
        if subject_type:
            sql += " AND subject_type=?"
            args.append(subject_type)
        if solver_type:
            sql += " AND solver_type=?"
            args.append(solver_type)
        return self._all(sql + " ORDER BY name", tuple(args))

    def update_template(self, tid: str, **fields) -> None:
        allowed = {"name", "schema_json", "default_values_json",
                   "validation_rules_json", "export_mapping_json"}
        self._update("sim_template", tid, {k: v for k, v in fields.items() if k in allowed})

    def delete_template(self, tid: str) -> bool:
        """删除模板。内置模板不允许删除。"""
        row = self.get_template(tid)
        if row is None or row["is_builtin"]:
            return False
        return self._write("DELETE FROM sim_template WHERE id=?", (tid,)) > 0

    # --- 工况 -----------------------------------------------------------

    def create_subject(
        self,
        sim_project_id: str,
        name: str,
        subject_type: str,
        solver_type: str,
        template_id: Optional[str] = None,
        sim_mesh_version_id: Optional[str] = None,
        config: Optional[Dict] = None,
    ) -> str:
        sid = _uid()
        now = time.time()
        self._write(
            """INSERT INTO sim_subject
               (id, sim_project_id, name, subject_type, solver_type, template_id,
                sim_mesh_version_id, config_json, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, sim_project_id, name, subject_type, solver_type, template_id,
             sim_mesh_version_id,
             json.dumps(config or {}, ensure_ascii=False),
             SUBJECT_DRAFT, now, now),
        )
        return sid

    def get_subject(self, sid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_subject WHERE id=?", (sid,))

    def list_subjects(
        self, sim_project_id: str, status: Optional[str] = None
    ) -> List[sqlite3.Row]:
        sql = "SELECT * FROM sim_subject WHERE sim_project_id=?"
        args: list = [sim_project_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        return self._all(sql + " ORDER BY created_at", tuple(args))

    def update_subject(self, sid: str, **fields) -> None:
        allowed = {"name", "template_id", "sim_mesh_version_id",
                   "config_json", "status"}
        self._update("sim_subject", sid, {k: v for k, v in fields.items() if k in allowed})

    def delete_subject(self, sid: str) -> bool:
        return self._write("DELETE FROM sim_subject WHERE id=?", (sid,)) > 0

    # --- 作业 -----------------------------------------------------------

    def create_job(
        self,
        sim_subject_id: str,
        submit_mode: str = "pbs",
        sim_mesh_version_id: Optional[str] = None,
        submit_payload: Optional[Dict] = None,
    ) -> str:
        jid = _uid()
        self._write(
            """INSERT INTO sim_job
               (id, sim_subject_id, sim_mesh_version_id, hpc_jobid, submit_mode,
                submit_payload_json, status, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (jid, sim_subject_id, sim_mesh_version_id, None, submit_mode,
             json.dumps(submit_payload, ensure_ascii=False) if submit_payload else None,
             JOB_DRAFT, time.time()),
        )
        return jid

    def get_job(self, jid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_job WHERE id=?", (jid,))

    def list_jobs(self, sim_subject_id: str) -> List[sqlite3.Row]:
        return self._all(
            "SELECT * FROM sim_job WHERE sim_subject_id=? ORDER BY created_at",
            (sim_subject_id,),
        )

    def list_jobs_by_project(self, sim_project_id: str) -> List[sqlite3.Row]:
        return self._all(
            """SELECT j.*, s.name AS subject_name FROM sim_job j
               JOIN sim_subject s ON s.id = j.sim_subject_id
               WHERE s.sim_project_id=? ORDER BY j.created_at DESC""",
            (sim_project_id,),
        )

    def find_job_by_hpc(self, hpc_jobid: str) -> Optional[sqlite3.Row]:
        """按 HPC 作业号反查，供作业状态回流时定位 sim_job。"""
        return self._one("SELECT * FROM sim_job WHERE hpc_jobid=?", (hpc_jobid,))

    def mark_job_submitted(self, jid: str, hpc_jobid: str) -> None:
        """记录已投递到 HPC。hpc_jobid 由现有提交链路返回。"""
        self._write(
            "UPDATE sim_job SET hpc_jobid=?, status=?, submitted_at=?,"
            " error_message=NULL WHERE id=?",
            (hpc_jobid, JOB_SUBMITTED, time.time(), jid),
        )

    def set_job_status(
        self, jid: str, status: str, error_message: Optional[str] = None
    ) -> None:
        finished = time.time() if status in (JOB_DONE, JOB_FAILED) else None
        self._write(
            "UPDATE sim_job SET status=?, error_message=?,"
            " finished_at=COALESCE(?, finished_at) WHERE id=?",
            (status, error_message, finished, jid),
        )

    # --- 结果 -----------------------------------------------------------

    def add_result(
        self,
        sim_job_id: str,
        result_type: str,
        file_path: str,
        meta: Optional[Dict] = None,
    ) -> str:
        rid = _uid()
        self._write(
            """INSERT INTO sim_result
               (id, sim_job_id, result_type, file_path, meta_json, created_at)
               VALUES (?,?,?,?,?,?)""",
            (rid, sim_job_id, result_type, file_path,
             json.dumps(meta, ensure_ascii=False) if meta else None, time.time()),
        )
        return rid

    def list_results(
        self, sim_job_id: str, result_type: Optional[str] = None
    ) -> List[sqlite3.Row]:
        sql = "SELECT * FROM sim_result WHERE sim_job_id=?"
        args: list = [sim_job_id]
        if result_type:
            sql += " AND result_type=?"
            args.append(result_type)
        return self._all(sql + " ORDER BY created_at", tuple(args))

    def list_results_by_project(self, sim_project_id: str) -> List[sqlite3.Row]:
        """项目下全部结果，供结果查看页按 result_type 分发到各查看器插件。"""
        return self._all(
            """SELECT r.*, j.sim_subject_id, s.name AS subject_name
               FROM sim_result r
               JOIN sim_job j ON j.id = r.sim_job_id
               JOIN sim_subject s ON s.id = j.sim_subject_id
               WHERE s.sim_project_id=? ORDER BY r.created_at DESC""",
            (sim_project_id,),
        )
