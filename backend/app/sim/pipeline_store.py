"""编排的持久化：定义、运行、节点运行态。

拆成 mixin 而非独立类，是为了与 sim_* 共用同一个连接与锁——运行实例要引用工况，
分成两个库会让"建 run 时校验工况存在"这类操作失去事务性。

三张表的分工：

    pipeline_def       声明式 DAG 文档，版本化
    pipeline_run       一次运行；**快照运行时的文档**，定义后续被改不影响已跑的 run
    pipeline_node_run  节点级状态，落库故服务重启可续跑

节点状态机：

    pending → running → done
                     ↘ waiting → done / failed     （等外部：HPC / 能力 / 人工）
                     ↘ failed
    上游失败时未启动的节点置 skipped。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Dict, List, Optional

# 运行状态
RUN_RUNNING = "running"
RUN_WAITING = "waiting"      # 全部可推进节点都在等外部
RUN_DONE = "done"
RUN_FAILED = "failed"
RUN_CANCELED = "canceled"
RUN_TERMINAL = (RUN_DONE, RUN_FAILED, RUN_CANCELED)

# 节点状态
NODE_PENDING = "pending"
NODE_RUNNING = "running"
NODE_WAITING = "waiting"
NODE_DONE = "done"
NODE_FAILED = "failed"
NODE_SKIPPED = "skipped"
NODE_TERMINAL = (NODE_DONE, NODE_FAILED, NODE_SKIPPED)

PIPELINE_SCHEMA = """
-- 声明式 DAG 文档。doc_json 形如：
--   {"nodes":[{"id":"n1","type":"internal.validate_config","label":"校验",
--              "params":{},"position":{"x":0,"y":0}}],
--    "edges":[{"from":"n1","to":"n2"}]}
-- position 只供画布用，引擎完全忽略。
CREATE TABLE IF NOT EXISTS pipeline_def (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    version     INTEGER NOT NULL DEFAULT 1,
    doc_json    TEXT NOT NULL,
    owner       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pdef_owner ON pipeline_def(owner);

-- 一次运行。doc_snapshot_json 是运行开始时的文档副本：定义之后被改也不影响
-- 已经在跑或已跑完的 run，保证可复现。
CREATE TABLE IF NOT EXISTS pipeline_run (
    id                TEXT PRIMARY KEY,
    pipeline_def_id   TEXT NOT NULL,
    def_version       INTEGER NOT NULL,
    doc_snapshot_json TEXT NOT NULL,
    sim_project_id    TEXT,
    sim_subject_id    TEXT,
    owner             TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'running',
    error_message     TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    finished_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_prun_def ON pipeline_run(pipeline_def_id);
CREATE INDEX IF NOT EXISTS idx_prun_status ON pipeline_run(status);
CREATE INDEX IF NOT EXISTS idx_prun_owner ON pipeline_run(owner);
CREATE INDEX IF NOT EXISTS idx_prun_subject ON pipeline_run(sim_subject_id);

CREATE TABLE IF NOT EXISTS pipeline_node_run (
    id              TEXT PRIMARY KEY,
    pipeline_run_id TEXT NOT NULL REFERENCES pipeline_run(id) ON DELETE CASCADE,
    node_id         TEXT NOT NULL,
    node_type       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    -- 外部句柄：HPC 作业号 / 能力作业号 / 人工确认者，供回流时定位
    external_ref    TEXT,
    wait_hint       TEXT,
    inputs_json     TEXT,
    outputs_json    TEXT,
    error_message   TEXT,
    started_at      REAL,
    finished_at     REAL,
    updated_at      REAL NOT NULL,
    UNIQUE(pipeline_run_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_pnode_run ON pipeline_node_run(pipeline_run_id);
CREATE INDEX IF NOT EXISTS idx_pnode_status ON pipeline_node_run(status);
CREATE INDEX IF NOT EXISTS idx_pnode_ref ON pipeline_node_run(external_ref);
"""


def _uid() -> str:
    return uuid.uuid4().hex


class PipelineStoreMixin:
    """编排持久化方法。由 SimDB 混入，复用其 conn 与 _lock。"""

    # --- 定义 -----------------------------------------------------------

    def create_pipeline_def(
        self, name: str, owner: str, doc: Dict, description: Optional[str] = None
    ) -> str:
        pid = _uid()
        now = time.time()
        self._write(
            """INSERT INTO pipeline_def
               (id, name, description, version, doc_json, owner, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pid, name, description, 1,
             json.dumps(doc, ensure_ascii=False), owner, now, now),
        )
        return pid

    def get_pipeline_def(self, pid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM pipeline_def WHERE id=?", (pid,))

    def list_pipeline_defs(self, owner: Optional[str] = None) -> List[sqlite3.Row]:
        if owner:
            return self._all(
                "SELECT * FROM pipeline_def WHERE owner=? ORDER BY updated_at DESC",
                (owner,),
            )
        return self._all("SELECT * FROM pipeline_def ORDER BY updated_at DESC")

    def update_pipeline_def(
        self,
        pid: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        doc: Optional[Dict] = None,
    ) -> None:
        """更新定义。文档变更会让 version 自增——已跑的 run 持有快照，不受影响。"""
        fields: Dict = {}
        if name is not None:
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        if doc is not None:
            fields["doc_json"] = json.dumps(doc, ensure_ascii=False)
        if not fields:
            return
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        bump = ", version = version + 1" if doc is not None else ""
        self._write(
            f"UPDATE pipeline_def SET {sets}{bump} WHERE id=?",
            (*fields.values(), pid),
        )

    def delete_pipeline_def(self, pid: str) -> bool:
        return self._write("DELETE FROM pipeline_def WHERE id=?", (pid,)) > 0

    # --- 运行 -----------------------------------------------------------

    def create_run(
        self,
        pipeline_def_id: str,
        def_version: int,
        doc: Dict,
        owner: str,
        sim_project_id: Optional[str] = None,
        sim_subject_id: Optional[str] = None,
    ) -> str:
        """建运行并按文档铺开全部节点行（一次写入，之后只改状态）。"""
        rid = _uid()
        now = time.time()
        with self._lock:
            self.conn.execute(
                """INSERT INTO pipeline_run
                   (id, pipeline_def_id, def_version, doc_snapshot_json,
                    sim_project_id, sim_subject_id, owner, status,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (rid, pipeline_def_id, def_version,
                 json.dumps(doc, ensure_ascii=False),
                 sim_project_id, sim_subject_id, owner, RUN_RUNNING, now, now),
            )
            for n in doc.get("nodes", []):
                self.conn.execute(
                    """INSERT INTO pipeline_node_run
                       (id, pipeline_run_id, node_id, node_type, status, updated_at)
                       VALUES (?,?,?,?,?,?)""",
                    (_uid(), rid, n["id"], n["type"], NODE_PENDING, now),
                )
            self.conn.commit()
        return rid

    def get_run(self, rid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM pipeline_run WHERE id=?", (rid,))

    def list_runs(
        self,
        owner: Optional[str] = None,
        sim_subject_id: Optional[str] = None,
        active_only: bool = False,
    ) -> List[sqlite3.Row]:
        sql = "SELECT * FROM pipeline_run WHERE 1=1"
        args: list = []
        if owner:
            sql += " AND owner=?"
            args.append(owner)
        if sim_subject_id:
            sql += " AND sim_subject_id=?"
            args.append(sim_subject_id)
        if active_only:
            sql += f" AND status NOT IN ({','.join('?' * len(RUN_TERMINAL))})"
            args.extend(RUN_TERMINAL)
        return self._all(sql + " ORDER BY created_at DESC", tuple(args))

    def set_run_status(
        self, rid: str, status: str, error_message: Optional[str] = None
    ) -> None:
        finished = time.time() if status in RUN_TERMINAL else None
        self._write(
            "UPDATE pipeline_run SET status=?, error_message=?, updated_at=?,"
            " finished_at=COALESCE(?, finished_at) WHERE id=?",
            (status, error_message, time.time(), finished, rid),
        )

    # --- 节点运行态 -----------------------------------------------------

    def list_node_runs(self, rid: str) -> List[sqlite3.Row]:
        return self._all(
            "SELECT * FROM pipeline_node_run WHERE pipeline_run_id=? ORDER BY rowid",
            (rid,),
        )

    def get_node_run(self, rid: str, node_id: str) -> Optional[sqlite3.Row]:
        return self._one(
            "SELECT * FROM pipeline_node_run WHERE pipeline_run_id=? AND node_id=?",
            (rid, node_id),
        )

    def set_node_running(self, rid: str, node_id: str, inputs: Dict) -> None:
        now = time.time()
        self._write(
            "UPDATE pipeline_node_run SET status=?, inputs_json=?, started_at=?,"
            " updated_at=? WHERE pipeline_run_id=? AND node_id=?",
            (NODE_RUNNING, json.dumps(inputs, ensure_ascii=False), now, now,
             rid, node_id),
        )

    def set_node_waiting(
        self, rid: str, node_id: str, ref: Optional[str], hint: str
    ) -> None:
        self._write(
            "UPDATE pipeline_node_run SET status=?, external_ref=?, wait_hint=?,"
            " updated_at=? WHERE pipeline_run_id=? AND node_id=?",
            (NODE_WAITING, ref, hint, time.time(), rid, node_id),
        )

    def set_node_done(self, rid: str, node_id: str, outputs: Dict) -> None:
        now = time.time()
        self._write(
            "UPDATE pipeline_node_run SET status=?, outputs_json=?, finished_at=?,"
            " updated_at=?, error_message=NULL WHERE pipeline_run_id=? AND node_id=?",
            (NODE_DONE, json.dumps(outputs, ensure_ascii=False), now, now,
             rid, node_id),
        )

    def set_node_failed(self, rid: str, node_id: str, message: str) -> None:
        now = time.time()
        self._write(
            "UPDATE pipeline_node_run SET status=?, error_message=?, finished_at=?,"
            " updated_at=? WHERE pipeline_run_id=? AND node_id=?",
            (NODE_FAILED, message[:1000], now, now, rid, node_id),
        )

    def skip_pending_nodes(self, rid: str) -> int:
        """上游失败后，把尚未启动的节点置 skipped，避免它们永远悬在 pending。"""
        return self._write(
            "UPDATE pipeline_node_run SET status=?, updated_at=?"
            " WHERE pipeline_run_id=? AND status=?",
            (NODE_SKIPPED, time.time(), rid, NODE_PENDING),
        )

    def find_waiting_node_by_ref(self, external_ref: str) -> Optional[sqlite3.Row]:
        """按外部句柄反查等待中的节点，供 HPC 作业状态回流时定位。"""
        return self._one(
            "SELECT * FROM pipeline_node_run WHERE external_ref=? AND status=?",
            (external_ref, NODE_WAITING),
        )

    def list_waiting_nodes(
        self, owner: Optional[str] = None, category_prefix: Optional[str] = None
    ) -> List[sqlite3.Row]:
        """列出等待中的节点，供浏览器代理拉取待办。

        category_prefix 用于只取某类节点（如 'capability.' 只给浏览器代理看
        需要它调 vektor3d 的那些）。
        """
        sql = """SELECT n.*, r.owner, r.sim_project_id, r.sim_subject_id
                 FROM pipeline_node_run n
                 JOIN pipeline_run r ON r.id = n.pipeline_run_id
                 WHERE n.status=?"""
        args: list = [NODE_WAITING]
        if owner:
            sql += " AND r.owner=?"
            args.append(owner)
        if category_prefix:
            sql += " AND n.node_type LIKE ?"
            args.append(category_prefix + "%")
        return self._all(sql + " ORDER BY n.updated_at", tuple(args))
