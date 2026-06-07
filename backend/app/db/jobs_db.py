"""任务数据库（SQLite）。

存放 qstat 轮询采集到的任务快照。任务从 qstat 消失即标记为已完成（done），
从而保留历史，弥补 Torque 完成任务很快移出队列的问题。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional

from ..pbs.parser import Job

# 我们自己维护的"派生状态"，区别于 PBS 原始 state
# active: qstat 中仍存在（Q/R/E...）；done: 已从 qstat 消失
ACTIVE = "active"
DONE = "done"

# 数据提取状态：任务完成后是否已评估/执行提取规则
# none: 尚未评估（仍在运行或刚入库）；pending: 已完成待评估；
# dispatched: 已匹配规则并派发；skipped: 完成但无规则匹配
EXTRACT_NONE = "none"
EXTRACT_PENDING = "pending"
EXTRACT_DISPATCHED = "dispatched"
EXTRACT_SKIPPED = "skipped"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    jobid          TEXT PRIMARY KEY,
    short_id       TEXT NOT NULL,
    name           TEXT,
    owner          TEXT NOT NULL,
    pbs_state      TEXT,            -- PBS 原始 job_state: Q/R/C/E/H
    derived_state  TEXT NOT NULL,   -- active / done
    queue          TEXT,
    workdir        TEXT,
    exec_host      TEXT,
    submit_ts      REAL,
    start_ts       REAL,
    end_ts         REAL,
    walltime_used  TEXT,
    walltime_limit TEXT,
    nodes          TEXT,
    exit_status    INTEGER,
    raw            TEXT,            -- 原始字段 JSON
    extract_state  TEXT NOT NULL DEFAULT 'none',  -- none/pending/dispatched/skipped
    first_seen     REAL NOT NULL,
    last_seen      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(derived_state);
"""


class JobsDB:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 轮询线程与请求线程都会访问，开 check_same_thread=False + 锁
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self) -> None:
        """对历史库做增量迁移（新增列等）。"""
        cols = {
            r["name"]
            for r in self.conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "extract_state" not in cols:
            self.conn.execute(
                "ALTER TABLE jobs ADD COLUMN extract_state TEXT NOT NULL DEFAULT 'none'"
            )

    def close(self) -> None:
        self.conn.close()

    # --- 轮询写入 -------------------------------------------------------

    def upsert_active(self, jobs: List[Job]) -> List[str]:
        """写入本轮 qstat 采集到的活跃任务，并把不在本轮的任务标记为 done。

        返回本轮"刚由 active 转为 done"的 jobid 列表，供完成后提取派发使用。
        这些任务的 extract_state 同时被置为 pending。
        """
        now = time.time()
        seen_ids = [j.jobid for j in jobs]
        with self._lock:
            for j in jobs:
                self._upsert_one(j, now)
            # 先查出本轮即将转 done 的 active 任务（用于返回 + 标记待提取）
            if seen_ids:
                placeholders = ",".join("?" * len(seen_ids))
                finished = self.conn.execute(
                    f"""SELECT jobid FROM jobs
                        WHERE derived_state=? AND jobid NOT IN ({placeholders})""",
                    [ACTIVE, *seen_ids],
                ).fetchall()
                self.conn.execute(
                    f"""UPDATE jobs SET derived_state=?, end_ts=COALESCE(end_ts, ?),
                           extract_state=?, last_seen=last_seen
                        WHERE derived_state=? AND jobid NOT IN ({placeholders})""",
                    [DONE, now, EXTRACT_PENDING, ACTIVE, *seen_ids],
                )
            else:
                # 本轮无任何活跃任务，全部置 done
                finished = self.conn.execute(
                    "SELECT jobid FROM jobs WHERE derived_state=?", [ACTIVE]
                ).fetchall()
                self.conn.execute(
                    "UPDATE jobs SET derived_state=?, end_ts=COALESCE(end_ts, ?), "
                    "extract_state=? WHERE derived_state=?",
                    [DONE, now, EXTRACT_PENDING, ACTIVE],
                )
            self.conn.commit()
        return [r["jobid"] for r in finished]

    def _upsert_one(self, j: Job, now: float) -> None:
        existing = self.conn.execute(
            "SELECT first_seen FROM jobs WHERE jobid=?", (j.jobid,)
        ).fetchone()
        first_seen = existing["first_seen"] if existing else now
        self.conn.execute(
            """
            INSERT INTO jobs
                (jobid, short_id, name, owner, pbs_state, derived_state, queue,
                 workdir, exec_host, submit_ts, start_ts, end_ts, walltime_used,
                 walltime_limit, nodes, exit_status, raw, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(jobid) DO UPDATE SET
                name=excluded.name,
                pbs_state=excluded.pbs_state,
                derived_state=excluded.derived_state,
                queue=excluded.queue,
                workdir=COALESCE(excluded.workdir, jobs.workdir),
                exec_host=excluded.exec_host,
                start_ts=COALESCE(excluded.start_ts, jobs.start_ts),
                walltime_used=excluded.walltime_used,
                exit_status=COALESCE(excluded.exit_status, jobs.exit_status),
                raw=excluded.raw,
                last_seen=excluded.last_seen
            """,
            (
                j.jobid,
                j.short_id,
                j.name,
                j.owner,
                j.state,
                ACTIVE,
                j.queue,
                j.workdir,
                j.exec_host,
                j.submit_ts,
                j.start_ts,
                j.end_ts,
                j.walltime_used,
                j.walltime_limit,
                j.nodes,
                j.exit_status,
                json.dumps(j.raw, ensure_ascii=False),
                first_seen,
                now,
            ),
        )

    # --- 查询 -----------------------------------------------------------

    def list_by_owner(
        self, owner: Optional[str], state: Optional[str] = None, limit: int = 500
    ) -> List[sqlite3.Row]:
        # owner=None 表示不限属主（管理员查看全部）
        if owner is None:
            sql = "SELECT * FROM jobs WHERE 1=1"
            params: list = []
        else:
            sql = "SELECT * FROM jobs WHERE owner=?"
            params = [owner]
        if state in (ACTIVE, DONE):
            sql += " AND derived_state=?"
            params.append(state)
        sql += " ORDER BY COALESCE(submit_ts, first_seen) DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def get(self, jobid: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM jobs WHERE jobid=?", (jobid,)
            ).fetchone()

    # --- 数据提取状态 ---------------------------------------------------

    def set_extract_state(self, jobid: str, state: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE jobs SET extract_state=? WHERE jobid=?", (state, jobid)
            )
            self.conn.commit()

    def list_pending_extract(self) -> List[sqlite3.Row]:
        """已完成但提取仍处 pending 的任务（用于服务重启后补派发）。"""
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM jobs WHERE derived_state=? AND extract_state=?",
                (DONE, EXTRACT_PENDING),
            ).fetchall()
