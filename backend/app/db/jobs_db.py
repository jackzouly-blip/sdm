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

from ..logger import get_logger
from ..pbs.parser import Job

log = get_logger(__name__)

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

-- 网盘流式上传队列：扫描器把"已写完稳定"的结果文件入队，上传器异步消费。
-- 入队与上传解耦，扫描永不被上传阻塞；(jobid,fname) 唯一，天然去重。
CREATE TABLE IF NOT EXISTS netdisk_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    jobid       TEXT NOT NULL,
    owner       TEXT NOT NULL,
    fname       TEXT NOT NULL,
    local_path  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued/uploading/done/failed
    enqueued_at REAL NOT NULL,
    updated_at  REAL NOT NULL,
    msg         TEXT,
    UNIQUE(jobid, fname)
);
CREATE INDEX IF NOT EXISTS idx_ndq_status ON netdisk_queue(status);
CREATE INDEX IF NOT EXISTS idx_ndq_job ON netdisk_queue(jobid);
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
            self._reconcile_stale_netdisk()
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
        # 结果文件自动上传百度网盘并分享：状态机与分享信息
        netdisk_cols = {
            "netdisk_state": "TEXT NOT NULL DEFAULT 'none'",  # none/pending/uploading/done/failed/skipped
            "netdisk_dir": "TEXT",          # 网盘上的任务目录
            "netdisk_share_url": "TEXT",    # 分享链接
            "netdisk_share_pwd": "TEXT",    # 提取码
            "netdisk_expire_at": "REAL",    # 分享过期时间戳（0/NULL=永久）
            "netdisk_files": "TEXT",        # 已上传文件名 JSON 数组
            "netdisk_msg": "TEXT",          # 失败原因/备注
            "netdisk_updated": "REAL",      # 最近更新时间
        }
        for col, decl in netdisk_cols.items():
            if col not in cols:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")

    def _reconcile_stale_netdisk(self) -> None:
        """启动自愈：网盘上传任务跑在本进程内，进程重启即被杀死，
        但其状态可能停留在 pending/uploading。启动时这些必为僵尸，
        统一重置为 failed，前端方可恢复并允许重试。"""
        cols = {
            r["name"]
            for r in self.conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "netdisk_state" not in cols:
            return
        cur = self.conn.execute(
            "UPDATE jobs SET netdisk_state='failed', "
            "netdisk_msg='服务重启导致上传中断，请重新上传', netdisk_updated=? "
            "WHERE netdisk_state IN ('pending','uploading')",
            (time.time(),),
        )
        if cur.rowcount:
            log.warning("启动自愈：重置 %d 个中断的网盘上传任务为 failed", cur.rowcount)
        # 上传队列：进程重启时 'uploading' 必为僵尸，退回 'queued' 以便重新消费。
        if "netdisk_queue" in {
            r["name"]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }:
            q = self.conn.execute(
                "UPDATE netdisk_queue SET status='queued', updated_at=? "
                "WHERE status='uploading'",
                (time.time(),),
            )
            if q.rowcount:
                log.warning("启动自愈：%d 个中断的上传队列项退回 queued", q.rowcount)

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

    # --- 结果网盘自动分享 -----------------------------------------------

    _NETDISK_FIELDS = (
        "netdisk_state", "netdisk_dir", "netdisk_share_url", "netdisk_share_pwd",
        "netdisk_expire_at", "netdisk_files", "netdisk_msg", "netdisk_updated",
    )

    def set_netdisk(self, jobid: str, **fields) -> None:
        """更新任务的网盘分享相关字段；自动写入 netdisk_updated。"""
        import time as _t
        fields.setdefault("netdisk_updated", _t.time())
        cols = [f for f in fields if f in self._NETDISK_FIELDS]
        if not cols:
            return
        sets = ", ".join(f"{c}=?" for c in cols)
        vals = [fields[c] for c in cols] + [jobid]
        with self._lock:
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE jobid=?", vals)
            self.conn.commit()

    def try_begin_netdisk(self, jobid: str) -> bool:
        """原子地把网盘状态从 none/partial 置为 pending，成功返回 True。

        用于最终补传的去重与接管：
          - 同一任务匹配多条提取规则各自完成时都会尝试触发，仅第一个成功；
          - 运行中流式上传会把状态置为 partial，最终补传从 partial 接管，
            之后流式上传的条件写入自动让位（见 set_netdisk_if_streamable）。
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE jobs SET netdisk_state='pending', netdisk_msg=NULL, "
                "netdisk_updated=? WHERE jobid=? AND "
                "(netdisk_state IS NULL OR netdisk_state IN ('none','partial'))",
                (time.time(), jobid),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def try_retry_netdisk(self, jobid: str) -> bool:
        """原子地把网盘状态从 failed 置为 pending，成功返回 True。
        供自动重试看门狗认领失败任务，避免与手动重试/并发看门狗重复派发。"""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE jobs SET netdisk_state='pending', netdisk_msg=NULL, "
                "netdisk_updated=? WHERE jobid=? AND netdisk_state='failed'",
                (time.time(), jobid),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def set_netdisk_if_streamable(self, jobid: str, **fields) -> None:
        """条件写入网盘字段：仅当当前状态仍为 none/partial 时才更新。

        供运行中流式上传使用——一旦最终补传接管（pending/uploading/done），
        本次写入自动 no-op，避免把已完成的 done 覆盖回 partial。
        """
        fields.setdefault("netdisk_updated", time.time())
        cols = [f for f in fields if f in self._NETDISK_FIELDS]
        if not cols:
            return
        sets = ", ".join(f"{c}=?" for c in cols)
        vals = [fields[c] for c in cols] + [jobid]
        with self._lock:
            self.conn.execute(
                f"UPDATE jobs SET {sets} WHERE jobid=? AND "
                "(netdisk_state IS NULL OR netdisk_state IN ('none','partial'))",
                vals,
            )
            self.conn.commit()

    # --- 网盘上传队列 ---------------------------------------------------

    def nq_enqueue(self, jobid: str, owner: str, fname: str, local_path: str) -> bool:
        """把一个结果文件加入上传队列；(jobid,fname) 已存在则不重复入队。
        若该项此前为 failed，则重置为 queued 以便重试。返回是否新入队/重置。"""
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO netdisk_queue "
                "(jobid, owner, fname, local_path, status, enqueued_at, updated_at) "
                "VALUES (?,?,?,?, 'queued', ?, ?)",
                (jobid, owner, fname, local_path, now, now),
            )
            if cur.rowcount == 0:
                # 已存在：仅当为 failed 时重置重试
                cur = self.conn.execute(
                    "UPDATE netdisk_queue SET status='queued', msg=NULL, "
                    "local_path=?, updated_at=? WHERE jobid=? AND fname=? "
                    "AND status='failed'",
                    (local_path, now, jobid, fname),
                )
            self.conn.commit()
            return cur.rowcount > 0

    def nq_claim_next(self) -> Optional[sqlite3.Row]:
        """原子取出最早的一条 queued，置为 uploading 并返回；无则返回 None。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM netdisk_queue WHERE status='queued' "
                "ORDER BY enqueued_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE netdisk_queue SET status='uploading', updated_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
            self.conn.commit()
            return row

    def nq_mark(self, item_id: int, status: str, msg: Optional[str] = None) -> None:
        """更新队列项状态（done/failed/queued）。"""
        with self._lock:
            self.conn.execute(
                "UPDATE netdisk_queue SET status=?, msg=?, updated_at=? WHERE id=?",
                (status, msg, time.time(), item_id),
            )
            self.conn.commit()

    def nq_counts(self) -> dict:
        """各状态的队列计数，便于观测。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) n FROM netdisk_queue GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def nq_list(
        self, jobid: Optional[str] = None, status: Optional[str] = None, limit: int = 500
    ) -> List[sqlite3.Row]:
        """列出队列项（可按任务/状态过滤）。"""
        sql = "SELECT * FROM netdisk_queue WHERE 1=1"
        params: list = []
        if jobid:
            sql += " AND jobid=?"
            params.append(jobid)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY enqueued_at LIMIT ?"
        params.append(limit)
        with self._lock:
            return self.conn.execute(sql, params).fetchall()
