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
from .pipeline_store import PIPELINE_SCHEMA, PipelineStoreMixin

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
    source_type       TEXT NOT NULL,     -- upload/vektor3d/dbit/airbag_flat/deck
    source_file_json  TEXT,
    step_file         TEXT,
    brep_file         TEXT,
    lightweight_file  TEXT,
    part_inventory_json TEXT,           -- mesh.inventory 产出：ANSA 产品树的零件清单
    mesh_strategy_json  TEXT,           -- mesh.classify 产出：逐零件/逐体的网格策略建议
    topo_summary_json TEXT,
    -- 派生溯源：由哪个几何版本、经哪个能力生成。气囊平面图 → deck 就是这条链。
    -- 生成物单独成一个版本而不是挂在源上：平面图与 deck 是两个不同的东西，
    -- 各自有版本、各自能被引用；渲染也因此直接复用现成的 deck→GLB 链路。
    derived_from_id   TEXT,
    derived_by        TEXT,
    status            TEXT NOT NULL DEFAULT 'ready',
    created_at        REAL NOT NULL,
    UNIQUE(sim_target_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_sim_geom_target ON sim_geometry_version(sim_target_id);

-- 网格版本。mesh_engine 记录由谁产出：manual（人工上传）或某个能力 ID
-- （如 vektor3d:mesh.generate）。
--
-- **.ansa 是网格的正本**：几何+网格+属性+厚度全量保留，手工微调、装配合并、
-- 按需导出求解器格式都以它为源；solver_file 只是派生产物（见能力契约 2.3）。
-- 因此这里不是"一个 mesh_file 走天下"，而是按角色分槽：正本 / 求解器 / 预览 / 报告。
--
-- part_filter 非空 = 这是"合并 STEP 里某个零件"的网格（逐零件生成，保持装配全局坐标）；
-- source_mesh_ids_json 非空 = 这是 mesh.merge 回装出来的装配网格。二者互斥。
CREATE TABLE IF NOT EXISTS sim_mesh_version (
    id                     TEXT PRIMARY KEY,
    sim_geometry_version_id TEXT NOT NULL REFERENCES sim_geometry_version(id) ON DELETE CASCADE,
    version_no             INTEGER NOT NULL,
    mesh_type              TEXT NOT NULL,     -- surface/volume/midsurface
    mesh_engine            TEXT NOT NULL,
    mesh_params_json       TEXT,
    mesh_file              TEXT,              -- 历史字段：人工上传的网格文件
    ansa_file              TEXT,              -- .ansa 正本
    solver_file            TEXT,              -- 求解器派生文件（.nas/.k/.inp/...）
    solver_format          TEXT,              -- nastran/lsdyna/abaqus/ansys/optistruct
    preview_file           TEXT,              -- 预览 GLB（带真实单元边线）
    report_file            TEXT,              -- 质量统计报告（HTML）
    part_filter            TEXT,              -- 逐零件生成时的零件名（取自 mesh.inventory）
    source_mesh_ids_json   TEXT,              -- 合并来源的网格版本 id 列表
    checkout_id            TEXT,              -- vektor3d 检出标识（人工微调回路）
    checkout_by            TEXT,
    checkout_at            REAL,
    quality_json           TEXT,              -- 网格检查产出
    status                 TEXT NOT NULL DEFAULT 'ready',  -- generating/ready/failed/checked-out
    created_at             REAL NOT NULL,
    updated_at             REAL,
    UNIQUE(sim_geometry_version_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_sim_mesh_geom ON sim_mesh_version(sim_geometry_version_id);

-- 客户需求文档。仿真项目的输入源头：主机厂给的 CAE 分析规范/技术协议，
-- 质量卡实例最终要从它推导出来（哪几项按客户要求改、依据是哪一句）。
-- analysis_json 存 AI 的结构化分析产出；未分析时为空，不影响文档本身的归档。
CREATE TABLE IF NOT EXISTS sim_requirement_doc (
    id             TEXT PRIMARY KEY,
    sim_project_id TEXT NOT NULL REFERENCES sim_project(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    doc_type       TEXT NOT NULL DEFAULT 'spec',   -- spec/agreement/standard/other
    source_file_json TEXT,
    analysis_json  TEXT,
    analysis_status TEXT NOT NULL DEFAULT 'pending', -- pending/analyzing/done/failed
    note           TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_req_project ON sim_requirement_doc(sim_project_id);

-- 需求条目：需求文档解析出的最小可追溯单元。
-- 每条带 source_ref（第几页/表格第几行），人能立刻回原文核对——条目的价值全在
-- 可追溯，不可追溯的条目还不如不抽。metrics_json 是量纲化的指标数组。
CREATE TABLE IF NOT EXISTS sim_requirement_item (
    id             TEXT PRIMARY KEY,
    doc_id         TEXT NOT NULL REFERENCES sim_requirement_doc(id) ON DELETE CASCADE,
    seq            TEXT NOT NULL DEFAULT '',
    category       TEXT NOT NULL,          -- subject/loading/mesh/delivery/other
    title          TEXT NOT NULL,
    raw_text       TEXT NOT NULL,          -- 原文，永远保留
    metrics_json   TEXT,
    baseline       TEXT NOT NULL DEFAULT '',  -- required=合格 / reference=参考
    project_note   TEXT,
    source_ref     TEXT NOT NULL DEFAULT '',
    needs_clarification INTEGER NOT NULL DEFAULT 0,
    clarification_hint  TEXT,
    load_points    INTEGER NOT NULL DEFAULT 0,
    indenter_diameter_mm REAL,
    extracted_by   TEXT NOT NULL DEFAULT 'rule',   -- rule/ai/manual
    status         TEXT NOT NULL DEFAULT 'draft',  -- draft/confirmed/dropped
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_req_item_doc ON sim_requirement_item(doc_id);

-- 项目的质量卡实例。它不是一份独立的卡，而是"基于哪张模板 + 改了哪几项 + 每项依据"。
-- 派生出的 .ansa_qual/.ansa_mpar 落在项目工作目录，card_dir 指向它——那两个文件
-- 本身就是合法的 ANSA 卡，可直接交回 ANSA 跑批处理，不需要二次转换。
CREATE TABLE IF NOT EXISTS sim_quality_card (
    id             TEXT PRIMARY KEY,
    sim_project_id TEXT NOT NULL REFERENCES sim_project(id) ON DELETE CASCADE,
    template_id    TEXT NOT NULL,
    name           TEXT NOT NULL,
    card_dir       TEXT,
    overrides_json TEXT,        -- [{target, old_value, new_value, source, by}]
    derived_from_doc_id TEXT,   -- 由哪份需求文档推导而来，可空（人工建卡时无）
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_qcard_project ON sim_quality_card(sim_project_id);

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

-- AI 会话：用户与 AI 在项目内的往来是主数据（要跨设备可见、要审计），落库；
-- 推理过程（工具调用轨迹、中间试错）留在 vektor3d 侧 workspace，可丢。
-- 见 docs/vektor3d-ai-session-contract.md 第 5.3 节。
CREATE TABLE IF NOT EXISTS sim_ai_session (
    id             TEXT PRIMARY KEY,
    sim_project_id TEXT NOT NULL REFERENCES sim_project(id) ON DELETE CASCADE,
    title          TEXT NOT NULL DEFAULT '',
    created_by     TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active',   -- active/archived
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_ai_session_project ON sim_ai_session(sim_project_id);

-- 消息按 seq 严格排序。proposals_json 是 assistant 消息附带的结构化提案
-- [{action, targetId, patch, reason, evidence, status, decided_by, decided_at}]：
-- status 由 pending → confirmed/rejected，是**提案卡片的 UI 状态**；真正的数据
-- 变更留痕在被改对象自己的机制里（条目的 PATCH、质量卡的 overrides），不在这里。
CREATE TABLE IF NOT EXISTS sim_ai_message (
    id             TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sim_ai_session(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    role           TEXT NOT NULL,               -- user/assistant/system
    content        TEXT NOT NULL DEFAULT '',
    proposals_json TEXT,
    citations_json TEXT,
    meta_json      TEXT,                        -- workspaceRun 等诊断信息
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_ai_message_session ON sim_ai_message(session_id, seq);

-- ── 材料库（全局资产，与工况模板/质量卡模板库同级） ──────────────────────
-- 设计见 docs/sdm-material-library.md：物理材料与模型变体分两层；数值按导入时
-- 的单位制存储不强转 SI；求解器卡保留自包含关键字原文，导出以原文为准。
CREATE TABLE IF NOT EXISTS sim_material (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,  -- 归一后的物理材料名（牌号）
    category      TEXT NOT NULL DEFAULT 'other',  -- steel/aluminum/plastic/rubber/glass/adhesive/foam/other
    standard_code TEXT,
    description   TEXT,
    source        TEXT,                  -- 数据来源（手册/试验报告/导入文件）
    revision      INTEGER NOT NULL DEFAULT 1,  -- 内容变更时 +1；引用侧（二期零件匹配）钉住它
    status        TEXT NOT NULL DEFAULT 'active',   -- active/deprecated
    created_by    TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_material_category ON sim_material(category);
CREATE INDEX IF NOT EXISTS idx_sim_material_status ON sim_material(status);

-- 标量性能：展示与检索用，导出以求解器卡为准（卡里已有同名参数，不做双写一致）。
CREATE TABLE IF NOT EXISTS sim_material_property (
    id             TEXT PRIMARY KEY,
    material_id    TEXT NOT NULL REFERENCES sim_material(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,        -- density/youngs_modulus/poisson_ratio/yield_strength…
    value          REAL NOT NULL,
    unit           TEXT NOT NULL DEFAULT '',
    condition_json TEXT,                 -- 适用条件，如 {"temperature_c": 23}
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_mat_prop ON sim_material_property(material_id);

-- 曲线。family_key 承载"同一物理量按应变率分组"的曲线族——LS-DYNA 的
-- *DEFINE_TABLE 正是这个结构；condition_json 存这条曲线的适用条件。
-- points_json 存文件原始数值；真实值 = SFA*(a+OFFA) / SFO*(o+OFFO)，因子在
-- scale_json，展示侧套用——不落地"已缩放"的点，避免与原文块两处真相。
CREATE TABLE IF NOT EXISTS sim_material_curve (
    id             TEXT PRIMARY KEY,
    material_id    TEXT NOT NULL REFERENCES sim_material(id) ON DELETE CASCADE,
    curve_type     TEXT NOT NULL DEFAULT 'generic',  -- stress_strain/strain_rate_scale/…
    title          TEXT NOT NULL DEFAULT '',
    family_key     TEXT NOT NULL DEFAULT '',
    condition_json TEXT,
    x_quantity     TEXT NOT NULL DEFAULT '',
    x_unit         TEXT NOT NULL DEFAULT '',
    y_quantity     TEXT NOT NULL DEFAULT '',
    y_unit         TEXT NOT NULL DEFAULT '',
    points_json    TEXT NOT NULL,        -- [[x,y],…]
    scale_json     TEXT,                 -- {sfa,sfo,offa,offo}
    source_lcid    INTEGER,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_mat_curve ON sim_material_curve(material_id);

-- 求解器卡：物理材料在某求解器下的一个模型变体（主卡/伴生 NULL 卡）。
-- keyword_text 是**自包含**关键字块（MAT + 其引用的 DEFINE_TABLE/DEFINE_CURVE
-- 原文），经过工程验证，导出直接用它，不重新生成。
CREATE TABLE IF NOT EXISTS sim_material_card (
    id           TEXT PRIMARY KEY,
    material_id  TEXT NOT NULL REFERENCES sim_material(id) ON DELETE CASCADE,
    solver_type  TEXT NOT NULL DEFAULT 'lsdyna',
    mat_type     TEXT NOT NULL,               -- 如 PIECEWISE_LINEAR_PLASTICITY
    title        TEXT NOT NULL DEFAULT '',    -- 原卡标题（含变体后缀，溯源用）
    variant      TEXT NOT NULL DEFAULT 'primary',  -- primary/null/alt
    unit_system  TEXT NOT NULL DEFAULT 't-mm-s',
    source_mid   INTEGER,
    params_json  TEXT,
    keyword_text TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_mat_card ON sim_material_card(material_id);

-- 材料模板文件：一组材料卡的具名选择，导出即一份可 *INCLUDE 的 MAT.K。
-- 组装式而非整份存档：材料已在库内拆解且 keyword_text 保真，再存一份整文件
-- 会产生第二份真相（改了库内材料而模板不变，或反之）。成员是引用，不是副本。
CREATE TABLE IF NOT EXISTS sim_material_template (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    description  TEXT NOT NULL DEFAULT '',
    solver_type  TEXT NOT NULL DEFAULT 'lsdyna',
    unit_system  TEXT NOT NULL,              -- 模板级约束：成员卡单位制必须与之一致
    summary_json TEXT,                       -- vektor3d 带 KB 的复核结果（字段级闭包/ID 区段）
    status       TEXT NOT NULL DEFAULT 'active',
    revision     INTEGER NOT NULL DEFAULT 1,
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_mat_tpl_status ON sim_material_template(status);

CREATE TABLE IF NOT EXISTS sim_material_template_item (
    template_id  TEXT NOT NULL REFERENCES sim_material_template(id) ON DELETE CASCADE,
    card_id      TEXT NOT NULL REFERENCES sim_material_card(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (template_id, card_id)
);
CREATE INDEX IF NOT EXISTS idx_sim_mat_tpl_item ON sim_material_template_item(template_id, seq);

-- 控制卡模板库：与材料相反，整份存档。
-- 控制卡没有 ID、彼此无引用，拆解入库没有收益；而 *CONTROL_* 之间的取值
-- 是一套互相配合的策略（时间步/接触/沙漏/输出频率），拆开反而丢了整体性。
CREATE TABLE IF NOT EXISTS sim_control_template (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    description   TEXT NOT NULL DEFAULT '',
    analysis_type TEXT NOT NULL DEFAULT '',  -- 气囊展开/整车碰撞/跌落…策略跟分析类型走
    solver_type   TEXT NOT NULL DEFAULT 'lsdyna',
    unit_system   TEXT NOT NULL,             -- 控制卡自身推不出单位制，必须声明
    keyword_text  TEXT NOT NULL,             -- 整份原文（正本）
    summary_json  TEXT,                      -- 求解策略摘要，由 vektor3d 解析回填
    source_name   TEXT NOT NULL DEFAULT '',
    source_sha256 TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'active',
    revision      INTEGER NOT NULL DEFAULT 1,
    created_by    TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_ctrl_tpl_type ON sim_control_template(analysis_type);
CREATE INDEX IF NOT EXISTS idx_sim_ctrl_tpl_status ON sim_control_template(status);

-- 模板发布版本：发布 = 把当刻渲染出的完整原文存成不可变快照。
-- 项目引用的是**快照**而不是模板本身——模板继续在线编辑不影响已引用的项目；
-- 要用新内容，再发布一版、项目改引用。这就是"发布并创建新的版本"的载体。
CREATE TABLE IF NOT EXISTS sim_template_release (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,               -- material | control
    template_id TEXT NOT NULL,
    version_no  INTEGER NOT NULL,
    name        TEXT NOT NULL,
    unit_system TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,               -- 发布时点的完整原文
    note        TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    UNIQUE(kind, template_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_sim_tpl_release ON sim_template_release(kind, template_id, version_no);

-- 项目引用的模板文件：通用列表，材料卡/控制卡起步，后续可扩展类别。
-- 两种来源：library=引用模板库的发布快照(不可变)；local=本地上传的文件原文。
-- 结算组装按类别取**最近添加**的一条。
CREATE TABLE IF NOT EXISTS sim_project_template_ref (
    id             TEXT PRIMARY KEY,
    sim_project_id TEXT NOT NULL REFERENCES sim_project(id) ON DELETE CASCADE,
    category       TEXT NOT NULL,
    name           TEXT NOT NULL,
    source         TEXT NOT NULL,
    release_id     TEXT,
    content        TEXT,
    created_by     TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_proj_tplref ON sim_project_template_ref(sim_project_id, category, created_at);
"""


def _uid() -> str:
    return uuid.uuid4().hex


class SimDB(PipelineStoreMixin):
    """仿真设计数据访问层。

    与 JobsDB 同构：请求线程与后台线程都会访问，故 check_same_thread=False + 锁。
    编排相关的表与方法由 PipelineStoreMixin 提供——拆文件只为可读性，
    共用同一连接与锁，故"建 run 时校验工况存在"这类操作仍有事务性。
    """

    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self.conn.executescript(PIPELINE_SCHEMA)
            self._migrate()
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        """对历史库做增量迁移（新增列）。

        `CREATE TABLE IF NOT EXISTS` 只对**新库**生效：已经建过表的库不会因为
        _SCHEMA 里加了一列就自动长出来，读写新列会直接 OperationalError。
        故与 jobs_db._migrate 同法逐列补齐。**加列时两处都要改**：_SCHEMA（新库）
        与这里（老库）。约束：ALTER TABLE ADD COLUMN 不能加 NOT NULL 无默认值的列。
        """
        def ensure(table: str, columns: Dict[str, str]) -> None:
            have = {
                r["name"]
                for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not have:  # 表还不存在（executescript 已建过，理论上不会走到）
                return
            for col, decl in columns.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

        # 网格版本：.ansa 正本 + 派生产物分槽 + 逐零件/合并溯源 + 检出状态
        ensure("sim_mesh_version", {
            "ansa_file": "TEXT",
            "solver_file": "TEXT",
            "solver_format": "TEXT",
            "preview_file": "TEXT",
            "report_file": "TEXT",
            "part_filter": "TEXT",
            "source_mesh_ids_json": "TEXT",
            "checkout_id": "TEXT",
            "checkout_by": "TEXT",
            "checkout_at": "REAL",
            "updated_at": "REAL",
        })
        # 几何版本：零件清单与网格策略建议
        ensure("sim_geometry_version", {
            "part_inventory_json": "TEXT",
            "mesh_strategy_json": "TEXT",
            # 派生溯源（气囊平面图 → deck）
            "derived_from_id": "TEXT",
            "derived_by": "TEXT",
        })

        # 材料模板的 vektor3d 复核结果。控制卡建表时就带了 summary_json，材料模板
        # 是后加的——库已经上线过一版，只能走 ALTER 补列。
        ensure("sim_material_template", {"summary_json": "TEXT"})
        # 仿真项目引用的模板发布版本（结算组装用）
        ensure("sim_project", {"control_release_id": "TEXT",
                               "material_release_id": "TEXT"})

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

    def subjects_using_target(self, tid: str) -> List[sqlite3.Row]:
        """引用了该分析对象名下网格版本的工况。

        sim_subject → sim_mesh_version 的外键没带级联（工况绑定是业务决策，
        不该因几何侧的删除而被悄悄清空），所以删除前必须先查引用：
        有引用时直接删会撞外键约束、以 500 收场。"""
        return self._all(
            """SELECT DISTINCT s.* FROM sim_subject s
               JOIN sim_mesh_version m ON m.id = s.sim_mesh_version_id
               JOIN sim_geometry_version g ON g.id = m.sim_geometry_version_id
               WHERE g.sim_target_id=?""",
            (tid,),
        )

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
        derived_from_id: Optional[str] = None,
        derived_by: Optional[str] = None,
    ) -> str:
        """追加一个几何版本，version_no 自动递增。

        derived_from_id/derived_by 记派生溯源（如气囊平面图经
        vektor3d:mesh.airbag.generate 生成的 deck）。
        """
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
                    derived_from_id, derived_by, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (gid, sim_target_id, row["n"], source_type,
                 json.dumps(source_file, ensure_ascii=False) if source_file else None,
                 step_file, brep_file, lightweight_file,
                 json.dumps(topo_summary, ensure_ascii=False) if topo_summary else None,
                 derived_from_id, derived_by,
                 "ready", time.time()),
            )
            self.conn.commit()
        return gid

    def get_geometry(self, gid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_geometry_version WHERE id=?", (gid,))

    def delete_geometry(self, gid: str) -> bool:
        """删除一个几何版本，网格版本随外键级联删除。"""
        return self._write("DELETE FROM sim_geometry_version WHERE id=?", (gid,)) > 0

    def subjects_using_geometry(self, gid: str) -> List[sqlite3.Row]:
        """引用了该几何版本名下网格版本的工况。理由见 subjects_using_target。"""
        return self._all(
            """SELECT DISTINCT s.* FROM sim_subject s
               JOIN sim_mesh_version m ON m.id = s.sim_mesh_version_id
               WHERE m.sim_geometry_version_id=?""",
            (gid,),
        )

    def set_geometry_lightweight(
        self, gid: str, lightweight_file: str, topo_summary: Optional[Dict] = None
    ) -> None:
        """写入轻量化产物路径与几何摘要。

        产物由 vektor3d 的 geometry.convert 能力经回传接口写入——SDM 自身不做
        CAD 转换（3D 处理归 vektor3d，见 docs/sdm-architecture.md）。
        """
        self._write(
            "UPDATE sim_geometry_version SET lightweight_file=?,"
            " topo_summary_json=COALESCE(?, topo_summary_json) WHERE id=?",
            (lightweight_file,
             json.dumps(topo_summary, ensure_ascii=False) if topo_summary else None,
             gid),
        )

    def set_geometry_analysis(
        self, gid: str, part_inventory: Optional[List] = None,
        mesh_strategy: Optional[Dict] = None,
    ) -> None:
        """写入 mesh.inventory（零件清单）与 mesh.classify（网格策略建议）的产出。

        两者都是"对同一份几何的分析结论"，故落在几何版本上而不是网格版本上：
        网格还没生成时它们就该可见——正是它们决定了每个零件该用哪种网格策略。
        """
        self._write(
            "UPDATE sim_geometry_version SET"
            " part_inventory_json=COALESCE(?, part_inventory_json),"
            " mesh_strategy_json=COALESCE(?, mesh_strategy_json) WHERE id=?",
            (json.dumps(part_inventory, ensure_ascii=False) if part_inventory is not None else None,
             json.dumps(mesh_strategy, ensure_ascii=False) if mesh_strategy is not None else None,
             gid),
        )

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
        status: str = "ready",
        part_filter: Optional[str] = None,
        source_mesh_ids: Optional[List[str]] = None,
    ) -> str:
        """追加一个网格版本，version_no 自动递增。

        mesh_engine：'manual'（人工上传）或能力 ID（如 'vektor3d:mesh.generate'）。
        status 传 'generating' 表示"已登记、产物还没回来"——vektor3d 的网格作业
        动辄几分钟到几十分钟，必须先落一行让页面能展示进度，产物回传时再补齐。
        part_filter / source_mesh_ids 见表注释（逐零件 / 合并回装,二者互斥）。
        """
        mid = _uid()
        now = time.time()
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 AS n FROM sim_mesh_version"
                " WHERE sim_geometry_version_id=?",
                (sim_geometry_version_id,),
            ).fetchone()
            self.conn.execute(
                """INSERT INTO sim_mesh_version
                   (id, sim_geometry_version_id, version_no, mesh_type, mesh_engine,
                    mesh_params_json, mesh_file, quality_json, part_filter,
                    source_mesh_ids_json, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, sim_geometry_version_id, row["n"], mesh_type, mesh_engine,
                 json.dumps(mesh_params, ensure_ascii=False) if mesh_params else None,
                 mesh_file,
                 json.dumps(quality, ensure_ascii=False) if quality else None,
                 part_filter,
                 json.dumps(source_mesh_ids, ensure_ascii=False) if source_mesh_ids else None,
                 status, now, now),
            )
            self.conn.commit()
        return mid

    def set_mesh_artifact(self, mid: str, kind: str, rel_path: str,
                          solver_format: Optional[str] = None) -> None:
        """写入网格产物文件句柄。kind ∈ ansa/solver/preview/report。

        分槽而不是一个 mesh_file：.ansa 是正本，求解器文件是派生物，
        预览与报告是给人看的——混在一列里就分不清"能不能再导出别的格式"。
        """
        column = {
            "ansa": "ansa_file",
            "solver": "solver_file",
            "preview": "preview_file",
            "report": "report_file",
        }.get(kind)
        if not column:
            raise ValueError(f"未知的网格产物类型: {kind}")
        if column == "solver_file" and solver_format:
            self._write(
                "UPDATE sim_mesh_version SET solver_file=?, solver_format=?, updated_at=?"
                " WHERE id=?",
                (rel_path, solver_format, time.time(), mid),
            )
            return
        self._write(
            f"UPDATE sim_mesh_version SET {column}=?, updated_at=? WHERE id=?",
            (rel_path, time.time(), mid),
        )

    def set_mesh_status(self, mid: str, status: str,
                        quality: Optional[Dict] = None) -> None:
        """更新网格版本状态（generating/ready/failed/checked-out）。

        失败原因写进 quality_json.summary——页面本来就在读它，
        不必为"失败信息"再开一列。
        """
        if quality is not None:
            self._write(
                "UPDATE sim_mesh_version SET status=?, quality_json=?, updated_at=?"
                " WHERE id=?",
                (status, json.dumps(quality, ensure_ascii=False), time.time(), mid),
            )
            return
        self._write(
            "UPDATE sim_mesh_version SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), mid),
        )

    def set_mesh_checkout(self, mid: str, checkout_id: Optional[str],
                          by: Optional[str] = None) -> None:
        """登记/清除检出状态。checkout_id=None 表示检入完成，释放占用。

        检出是人工微调回路的取出端：工作副本在工程师本机，SDM 这边只记
        "谁在改、凭据是什么",据此在页面上显示占用并允许其检入。
        """
        if checkout_id:
            self._write(
                "UPDATE sim_mesh_version SET checkout_id=?, checkout_by=?,"
                " checkout_at=?, status='checked-out', updated_at=? WHERE id=?",
                (checkout_id, by, time.time(), time.time(), mid),
            )
        else:
            self._write(
                "UPDATE sim_mesh_version SET checkout_id=NULL, checkout_by=NULL,"
                " checkout_at=NULL, status='ready', updated_at=? WHERE id=?",
                (time.time(), mid),
            )

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

    # --- 客户需求文档 ---------------------------------------------------

    def add_requirement_doc(
        self,
        sim_project_id: str,
        name: str,
        doc_type: str = "spec",
        source_file: Optional[Dict] = None,
        note: Optional[str] = None,
    ) -> str:
        rid = _uid()
        now = time.time()
        self._write(
            """INSERT INTO sim_requirement_doc
               (id, sim_project_id, name, doc_type, source_file_json, analysis_json,
                analysis_status, note, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (rid, sim_project_id, name, doc_type,
             json.dumps(source_file, ensure_ascii=False) if source_file else None, None,
             "pending", note, now, now),
        )
        return rid

    def list_requirement_docs(self, sim_project_id: str) -> List[sqlite3.Row]:
        return self._all(
            "SELECT * FROM sim_requirement_doc WHERE sim_project_id=? ORDER BY created_at",
            (sim_project_id,),
        )

    def get_requirement_doc(self, rid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_requirement_doc WHERE id=?", (rid,))

    def set_requirement_analysis(self, rid: str, analysis: Optional[Dict], status: str) -> None:
        self._update("sim_requirement_doc", rid, {
            "analysis_json": json.dumps(analysis, ensure_ascii=False) if analysis else None,
            "analysis_status": status,
        })

    def delete_requirement_doc(self, rid: str) -> bool:
        return self._write("DELETE FROM sim_requirement_doc WHERE id=?", (rid,)) > 0

    # --- 需求条目 -------------------------------------------------------

    def replace_requirement_items(self, doc_id: str, items: List[Dict]) -> int:
        """整体替换一份文档的条目。

        重解析是幂等的：先清后插，而不是追加——否则改一次解析规则再跑一遍，
        库里就会同时躺着新旧两版条目，谁也说不清哪条是当前口径。
        """
        now = time.time()
        with self._lock:
            self.conn.execute("DELETE FROM sim_requirement_item WHERE doc_id=?", (doc_id,))
            for it in items:
                self.conn.execute(
                    """INSERT INTO sim_requirement_item
                       (id, doc_id, seq, category, title, raw_text, metrics_json, baseline,
                        project_note, source_ref, needs_clarification, clarification_hint,
                        load_points, indenter_diameter_mm, extracted_by, status,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (_uid(), doc_id, it.get("seq", ""), it["category"], it["title"],
                     it.get("raw_text", ""),
                     json.dumps(it.get("metrics") or [], ensure_ascii=False),
                     it.get("baseline", ""), it.get("project_note"),
                     it.get("source_ref", ""), 1 if it.get("needs_clarification") else 0,
                     it.get("clarification_hint"), int(it.get("load_points") or 0),
                     it.get("indenter_diameter_mm"), it.get("extracted_by", "rule"),
                     "draft", now, now),
                )
            self.conn.commit()
        return len(items)

    def list_requirement_items(self, doc_id: str) -> List[sqlite3.Row]:
        return self._all(
            "SELECT * FROM sim_requirement_item WHERE doc_id=? ORDER BY category DESC, rowid",
            (doc_id,),
        )

    def get_requirement_item(self, iid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_requirement_item WHERE id=?", (iid,))

    def update_requirement_item(self, iid: str, **fields) -> None:
        allowed = {"title", "raw_text", "category", "baseline", "status",
                   "metrics_json", "needs_clarification", "clarification_hint",
                   "extracted_by"}
        self._update("sim_requirement_item", iid,
                     {k: v for k, v in fields.items() if k in allowed})

    # --- 质量卡实例 -----------------------------------------------------

    def add_quality_card(
        self,
        sim_project_id: str,
        template_id: str,
        name: str,
        card_dir: Optional[str] = None,
        overrides: Optional[List[Dict]] = None,
        derived_from_doc_id: Optional[str] = None,
    ) -> str:
        qid = _uid()
        now = time.time()
        self._write(
            """INSERT INTO sim_quality_card
               (id, sim_project_id, template_id, name, card_dir, overrides_json,
                derived_from_doc_id, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (qid, sim_project_id, template_id, name, card_dir,
             json.dumps(overrides or [], ensure_ascii=False),
             derived_from_doc_id, "active", now, now),
        )
        return qid

    def list_quality_cards(self, sim_project_id: str) -> List[sqlite3.Row]:
        return self._all(
            "SELECT * FROM sim_quality_card WHERE sim_project_id=? ORDER BY created_at",
            (sim_project_id,),
        )

    def get_quality_card(self, qid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_quality_card WHERE id=?", (qid,))

    def update_quality_card(self, qid: str, **fields) -> None:
        allowed = {"name", "card_dir", "overrides_json", "status", "template_id"}
        self._update("sim_quality_card", qid,
                     {k: v for k, v in fields.items() if k in allowed})

    def delete_quality_card(self, qid: str) -> bool:
        return self._write("DELETE FROM sim_quality_card WHERE id=?", (qid,)) > 0

    def count_cards_by_template(self) -> Dict[str, int]:
        """各模板被多少个项目实例引用。实例文件是派生时拷走的,删模板不影响
        既有实例——这个数只用来在删除前提示影响面。"""
        rows = self._all(
            "SELECT template_id, COUNT(*) AS n FROM sim_quality_card GROUP BY template_id"
        )
        return {r["template_id"]: r["n"] for r in rows}

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

    def clear_subject_mesh(self, sid: str) -> None:
        """解除工况的网格绑定。

        单独一个方法而不走 update_subject：_update 会滤掉 None（部分更新的
        约定），"置空"这个语义在那条路上表达不出来。"""
        self._write(
            "UPDATE sim_subject SET sim_mesh_version_id=NULL, updated_at=? WHERE id=?",
            (time.time(), sid),
        )

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

    # --- AI 会话 ---------------------------------------------------------

    def create_ai_session(self, sim_project_id: str, title: str, created_by: str) -> str:
        sid = _uid()
        now = time.time()
        self._write(
            """INSERT INTO sim_ai_session
               (id, sim_project_id, title, created_by, status, created_at, updated_at)
               VALUES (?,?,?,?, 'active', ?, ?)""",
            (sid, sim_project_id, title, created_by, now, now),
        )
        return sid

    def get_ai_session(self, sid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_ai_session WHERE id=?", (sid,))

    def list_ai_sessions(self, sim_project_id: str) -> List[sqlite3.Row]:
        return self._all(
            "SELECT * FROM sim_ai_session WHERE sim_project_id=? ORDER BY updated_at DESC",
            (sim_project_id,),
        )

    def update_ai_session(self, sid: str, **fields) -> None:
        allowed = {"title", "status"}
        self._update("sim_ai_session", sid,
                     {k: v for k, v in fields.items() if k in allowed})

    def delete_ai_session(self, sid: str) -> bool:
        return self._write("DELETE FROM sim_ai_session WHERE id=?", (sid,)) > 0

    def add_ai_message(
        self,
        session_id: str,
        role: str,
        content: str,
        proposals: Optional[List[Dict]] = None,
        citations: Optional[List[Dict]] = None,
        meta: Optional[Dict] = None,
    ) -> str:
        """追加一条消息。seq 在同一把锁里取 MAX+1，与插入构成事务，并发追加不重号。"""
        mid = _uid()
        now = time.time()
        with self._lock:
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM sim_ai_message WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            self.conn.execute(
                """INSERT INTO sim_ai_message
                   (id, session_id, seq, role, content,
                    proposals_json, citations_json, meta_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (mid, session_id, seq, role, content,
                 json.dumps(proposals, ensure_ascii=False) if proposals else None,
                 json.dumps(citations, ensure_ascii=False) if citations else None,
                 json.dumps(meta, ensure_ascii=False) if meta else None, now),
            )
            self.conn.execute(
                "UPDATE sim_ai_session SET updated_at=? WHERE id=?", (now, session_id)
            )
            self.conn.commit()
        return mid

    def get_ai_message(self, mid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_ai_message WHERE id=?", (mid,))

    def list_ai_messages(self, session_id: str) -> List[sqlite3.Row]:
        return self._all(
            "SELECT * FROM sim_ai_message WHERE session_id=? ORDER BY seq",
            (session_id,),
        )

    def set_ai_message_proposals(self, mid: str, proposals: List[Dict]) -> None:
        """整体回写某条消息的提案数组——只用于翻提案的 UI 状态
        （pending → confirmed/rejected），消息内容与其余字段不可改。"""
        self._write(
            "UPDATE sim_ai_message SET proposals_json=? WHERE id=?",
            (json.dumps(proposals, ensure_ascii=False), mid),
        )

    # --- 材料库 -----------------------------------------------------------

    def create_material(
        self,
        name: str,
        category: str = "other",
        standard_code: Optional[str] = None,
        description: Optional[str] = None,
        source: Optional[str] = None,
        created_by: str = "",
    ) -> str:
        mid = _uid()
        now = time.time()
        self._write(
            """INSERT INTO sim_material
               (id, name, category, standard_code, description, source,
                revision, status, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?, 1, 'active', ?, ?, ?)""",
            (mid, name, category, standard_code, description, source,
             created_by, now, now),
        )
        return mid

    def get_material(self, mid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_material WHERE id=?", (mid,))

    def get_material_by_name(self, name: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_material WHERE name=?", (name,))

    def list_materials(
        self,
        category: Optional[str] = None,
        q: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[sqlite3.Row]:
        """列表带卡/曲线计数——列表页要展示"这材料有多少可用资产"，
        不值得为它发 2N 个查询。"""
        sql = """SELECT m.*,
                   (SELECT COUNT(*) FROM sim_material_card c
                     WHERE c.material_id=m.id) AS card_count,
                   (SELECT COUNT(*) FROM sim_material_curve v
                     WHERE v.material_id=m.id) AS curve_count
                 FROM sim_material m WHERE 1=1"""
        args: List = []
        if category:
            sql += " AND m.category=?"
            args.append(category)
        if status:
            sql += " AND m.status=?"
            args.append(status)
        if q:
            sql += " AND m.name LIKE ?"
            args.append(f"%{q}%")
        sql += " ORDER BY m.category, m.name"
        return self._all(sql, tuple(args))

    def update_material(self, mid: str, **fields) -> None:
        allowed = {"name", "category", "standard_code", "description",
                   "source", "status"}
        self._update("sim_material", mid,
                     {k: v for k, v in fields.items() if k in allowed})

    def delete_material(self, mid: str) -> bool:
        return self._write("DELETE FROM sim_material WHERE id=?", (mid,)) > 0

    def material_properties(self, mid: str) -> List[sqlite3.Row]:
        return self._all(
            "SELECT * FROM sim_material_property WHERE material_id=? ORDER BY name",
            (mid,),
        )

    def material_curves(self, mid: str) -> List[sqlite3.Row]:
        return self._all(
            """SELECT * FROM sim_material_curve WHERE material_id=?
               ORDER BY family_key, source_lcid""",
            (mid,),
        )

    def material_cards(self, mid: str) -> List[sqlite3.Row]:
        # primary 排最前：详情页第一眼看到的应是主卡而非伴生 NULL 卡
        return self._all(
            """SELECT * FROM sim_material_card WHERE material_id=?
               ORDER BY variant='primary' DESC, title""",
            (mid,),
        )

    def get_material_card(self, card_id: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_material_card WHERE id=?", (card_id,))

    # ── 材料模板文件（组装式：模板只记选了哪几张卡，导出时拼原文）──────

    def create_material_template(self, name: str, unit_system: str,
                                 description: str = "", solver_type: str = "lsdyna",
                                 created_by: str = "") -> str:
        tid = _uid()
        now = time.time()
        self._write(
            """INSERT INTO sim_material_template
               (id, name, description, solver_type, unit_system, status, revision,
                created_by, created_at, updated_at)
               VALUES (?,?,?,?,?, 'active', 1, ?,?,?)""",
            (tid, name, description, solver_type, unit_system, created_by, now, now),
        )
        return tid

    def get_material_template(self, tid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_material_template WHERE id=?", (tid,))

    def get_material_template_by_name(self, name: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_material_template WHERE name=?", (name,))

    def list_material_templates(self, status: Optional[str] = None) -> List[sqlite3.Row]:
        sql = """SELECT t.*,
                   (SELECT COUNT(*) FROM sim_material_template_item i
                     WHERE i.template_id=t.id) AS card_count
                 FROM sim_material_template t WHERE 1=1"""
        args: List = []
        if status:
            sql += " AND t.status=?"
            args.append(status)
        return self._all(sql + " ORDER BY t.name", tuple(args))

    def update_material_template(self, tid: str, **fields) -> None:
        allowed = {"name", "description", "unit_system", "status", "solver_type",
                   "summary_json"}
        self._update("sim_material_template", tid,
                     {k: v for k, v in fields.items() if k in allowed})

    def delete_material_template(self, tid: str) -> bool:
        return self._write("DELETE FROM sim_material_template WHERE id=?", (tid,)) > 0

    def set_material_template_items(self, tid: str, card_ids: List[str]) -> None:
        """整体替换成员列表，单事务。顺序即导出顺序。

        顺带清空 summary_json：成员一变，上一次的复核结果就是**另一份文件**的了。
        留着它比没有更糟——页面会显示一份"复核通过"，而通过的不是现在这套卡。
        """
        with self._lock:
            try:
                self.conn.execute(
                    "DELETE FROM sim_material_template_item WHERE template_id=?", (tid,))
                for seq, cid in enumerate(card_ids):
                    self.conn.execute(
                        """INSERT INTO sim_material_template_item
                           (template_id, card_id, seq) VALUES (?,?,?)""",
                        (tid, cid, seq),
                    )
                self.conn.execute(
                    "UPDATE sim_material_template"
                    " SET updated_at=?, revision=revision+1, summary_json=NULL"
                    " WHERE id=?", (time.time(), tid))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def list_material_template_items(self, tid: str) -> List[sqlite3.Row]:
        return self._all(
            """SELECT i.*, c.title, c.mat_type, c.unit_system, c.source_mid,
                      c.material_id, m.name AS material_name
               FROM sim_material_template_item i
               JOIN sim_material_card c ON c.id = i.card_id
               JOIN sim_material m ON m.id = c.material_id
               WHERE i.template_id=? ORDER BY i.seq""",
            (tid,),
        )

    # ── 控制卡模板（整份存档）────────────────────────────────

    # ── 模板发布版本 ────────────────────────────────────────────
    def create_template_release(self, kind: str, template_id: str, name: str,
                                unit_system: str, content: str, note: str,
                                user: str) -> str:
        rid = _uid()
        now = time.time()
        row = self._one(
            "SELECT MAX(version_no) v FROM sim_template_release"
            " WHERE kind=? AND template_id=?", (kind, template_id))
        ver = int(row["v"] or 0) + 1
        self._write(
            """INSERT INTO sim_template_release
               (id, kind, template_id, version_no, name, unit_system, content,
                note, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (rid, kind, template_id, ver, name, unit_system, content,
             note, user, now))
        return rid

    def list_template_releases(self, kind: str, template_id: str):
        return self._all(
            "SELECT id, kind, template_id, version_no, name, unit_system,"
            " note, created_by, created_at, LENGTH(content) content_bytes"
            " FROM sim_template_release WHERE kind=? AND template_id=?"
            " ORDER BY version_no DESC", (kind, template_id))

    def get_template_release(self, rid: str):
        return self._one("SELECT * FROM sim_template_release WHERE id=?", (rid,))

    def list_project_template_refs(self, pid: str):
        return self._all(
            "SELECT id, sim_project_id, category, name, source, release_id,"
            " created_by, created_at, LENGTH(content) content_bytes"
            " FROM sim_project_template_ref WHERE sim_project_id=?"
            " ORDER BY category, created_at DESC", (pid,))

    def add_project_template_ref(self, pid: str, category: str, name: str,
                                 source: str, release_id: Optional[str],
                                 content: Optional[str], user: str) -> str:
        rid = _uid()
        self._write(
            """INSERT INTO sim_project_template_ref
               (id, sim_project_id, category, name, source, release_id,
                content, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (rid, pid, category, name, source, release_id, content,
             user, time.time()))
        return rid

    def get_project_template_ref(self, rid: str):
        return self._one("SELECT * FROM sim_project_template_ref WHERE id=?", (rid,))

    def delete_project_template_ref(self, rid: str) -> bool:
        return self._write("DELETE FROM sim_project_template_ref WHERE id=?", (rid,)) > 0

    def create_control_template(self, name: str, unit_system: str, keyword_text: str,
                                analysis_type: str = "", description: str = "",
                                solver_type: str = "lsdyna",
                                summary_json: Optional[str] = None,
                                source_name: str = "", source_sha256: str = "",
                                created_by: str = "") -> str:
        tid = _uid()
        now = time.time()
        self._write(
            """INSERT INTO sim_control_template
               (id, name, description, analysis_type, solver_type, unit_system,
                keyword_text, summary_json, source_name, source_sha256,
                status, revision, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?, 'active', 1, ?,?,?)""",
            (tid, name, description, analysis_type, solver_type, unit_system,
             keyword_text, summary_json, source_name, source_sha256,
             created_by, now, now),
        )
        return tid

    def get_control_template(self, tid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_control_template WHERE id=?", (tid,))

    def get_control_template_by_name(self, name: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM sim_control_template WHERE name=?", (name,))

    def list_control_templates(self, analysis_type: Optional[str] = None,
                               status: Optional[str] = None) -> List[sqlite3.Row]:
        # 列表不带 keyword_text：整份控制卡有几 KB，列表页不需要
        sql = """SELECT id, name, description, analysis_type, solver_type, unit_system,
                        summary_json, source_name, source_sha256, status, revision,
                        created_by, created_at, updated_at
                 FROM sim_control_template WHERE 1=1"""
        args: List = []
        if analysis_type:
            sql += " AND analysis_type=?"
            args.append(analysis_type)
        if status:
            sql += " AND status=?"
            args.append(status)
        return self._all(sql + " ORDER BY analysis_type, name", tuple(args))

    def update_control_template(self, tid: str, **fields) -> None:
        allowed = {"name", "description", "analysis_type", "unit_system",
                   "status", "solver_type", "summary_json", "keyword_text"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        # 换了正文而调用方没同时给新摘要 —— 旧摘要描述的是另一份文件，作废。
        # 单独一条 SQL：_update 会过滤掉 None，清不掉字段。
        if fields.get("keyword_text") is not None and fields.get("summary_json") is None:
            import hashlib
            self._write(
                "UPDATE sim_control_template SET summary_json=NULL, source_sha256=?"
                " WHERE id=?",
                (hashlib.sha256(fields["keyword_text"].encode("utf-8")).hexdigest()[:16], tid),
            )
        self._update("sim_control_template", tid, fields)

    def delete_control_template(self, tid: str) -> bool:
        return self._write("DELETE FROM sim_control_template WHERE id=?", (tid,)) > 0

    def replace_material_content(
        self,
        mid: str,
        properties: List[Dict],
        curves: List[Dict],
        cards: List[Dict],
        bump_revision: bool = False,
    ) -> None:
        """整体替换一个材料的性能/曲线/卡，单事务。

        导入是"新修订整体替换"语义而非逐条 diff——半新半旧的材料比过时的
        材料更危险：它看起来是新的。"""
        now = time.time()
        with self._lock:
            try:
                for t in ("sim_material_property", "sim_material_curve",
                          "sim_material_card"):
                    self.conn.execute(f"DELETE FROM {t} WHERE material_id=?", (mid,))
                for p in properties:
                    self.conn.execute(
                        """INSERT INTO sim_material_property
                           (id, material_id, name, value, unit, condition_json, created_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (_uid(), mid, p["name"], p["value"], p.get("unit", ""),
                         json.dumps(p["condition"], ensure_ascii=False)
                         if p.get("condition") else None, now),
                    )
                for c in curves:
                    self.conn.execute(
                        """INSERT INTO sim_material_curve
                           (id, material_id, curve_type, title, family_key,
                            condition_json, x_quantity, x_unit, y_quantity, y_unit,
                            points_json, scale_json, source_lcid, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (_uid(), mid, c.get("curve_type", "generic"),
                         c.get("title", ""), c.get("family_key", ""),
                         json.dumps(c["condition"], ensure_ascii=False)
                         if c.get("condition") else None,
                         c.get("x_quantity", ""), c.get("x_unit", ""),
                         c.get("y_quantity", ""), c.get("y_unit", ""),
                         json.dumps(c["points"]),
                         json.dumps(c["scale"]) if c.get("scale") else None,
                         c.get("source_lcid"), now),
                    )
                for k in cards:
                    self.conn.execute(
                        """INSERT INTO sim_material_card
                           (id, material_id, solver_type, mat_type, title, variant,
                            unit_system, source_mid, params_json, keyword_text, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (_uid(), mid, k.get("solver_type", "lsdyna"), k["mat_type"],
                         k.get("title", ""), k.get("variant", "primary"),
                         k.get("unit_system", "t-mm-s"), k.get("source_mid"),
                         json.dumps(k["params"], ensure_ascii=False)
                         if k.get("params") else None,
                         k["keyword_text"], now),
                    )
                if bump_revision:
                    self.conn.execute(
                        "UPDATE sim_material SET revision=revision+1, updated_at=? WHERE id=?",
                        (now, mid),
                    )
                else:
                    self.conn.execute(
                        "UPDATE sim_material SET updated_at=? WHERE id=?", (now, mid)
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
