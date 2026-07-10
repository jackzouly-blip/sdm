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
from typing import Dict, List, Optional

from ..logger import get_logger
from ..pbs.cores import cores_from_nodes
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

-- 本地提交排队：作业提交请求先落地于此，由 SubmissionScheduler 按用户配额与
-- 全局核数余量决定何时真正 qsub。默认(无策略/无核数上限)配置下该表几乎立即
-- 流转到 submitted，行为与"直接 qsub"一致。
CREATE TABLE IF NOT EXISTS submission_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued/submitting/submitted/cancelled/failed
    jobname       TEXT NOT NULL,
    queue_name    TEXT NOT NULL,
    script_path   TEXT NOT NULL,
    workdir       TEXT NOT NULL,
    cores         INTEGER NOT NULL,
    extra_l       TEXT,
    jobid         TEXT,
    msg           TEXT,
    queued_at     REAL NOT NULL,
    submitted_at  REAL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subq_status ON submission_queue(status);
CREATE INDEX IF NOT EXISTS idx_subq_owner ON submission_queue(owner);

-- 用户提交策略：未配置的用户=不限并发、优先级0（与现状一致）。
CREATE TABLE IF NOT EXISTS user_policies (
    user           TEXT PRIMARY KEY,
    max_concurrent INTEGER,
    priority       INTEGER NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL
);

-- 试算任务：不进 PBS，直接在管理节点以属主身份跑一条命令。门户自己持有
-- pid/pgid 以便随时整组中断，输出重定向到 log_path 供随时增量拉取，退出码
-- 经 status_path 落盘（进程若被 init 收养也能恢复真实退出码）。
CREATE TABLE IF NOT EXISTS trial_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL,
    name        TEXT NOT NULL,
    workdir     TEXT NOT NULL,
    command     TEXT NOT NULL,
    pid         INTEGER,
    pgid        INTEGER,
    status      TEXT NOT NULL DEFAULT 'running',  -- running/finished/failed/killed/interrupted
    exit_code   INTEGER,
    log_path    TEXT NOT NULL,
    status_path TEXT NOT NULL,
    msg         TEXT,
    created_at  REAL NOT NULL,
    started_at  REAL,
    ended_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_trial_status ON trial_tasks(status);
CREATE INDEX IF NOT EXISTS idx_trial_owner ON trial_tasks(owner);
"""

# trial_tasks 状态
TRIAL_RUNNING = "running"
TRIAL_FINISHED = "finished"
TRIAL_FAILED = "failed"
TRIAL_KILLED = "killed"
TRIAL_INTERRUPTED = "interrupted"
TRIAL_TERMINAL = (TRIAL_FINISHED, TRIAL_FAILED, TRIAL_KILLED, TRIAL_INTERRUPTED)

# submission_queue 状态
SQ_QUEUED = "queued"
SQ_SUBMITTING = "submitting"
SQ_SUBMITTED = "submitted"
SQ_CANCELLED = "cancelled"
SQ_FAILED = "failed"


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
            self._reconcile_stale_submissions()
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

    def _reconcile_stale_submissions(self) -> None:
        """启动自愈：'submitting' 是"正在调用 qsub"的瞬时状态，进程若在此时
        崩溃，重启后无法确认 qsub 是否已经成功——为避免重复提交，一律置 failed，
        由用户/管理员确认后手动重新提交。"""
        if "submission_queue" not in {
            r["name"]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }:
            return
        cur = self.conn.execute(
            "UPDATE submission_queue SET status=?, msg=?, updated_at=? "
            "WHERE status=?",
            (SQ_FAILED, "服务重启导致提交中断，请确认是否已提交后重试", time.time(), SQ_SUBMITTING),
        )
        if cur.rowcount:
            log.warning("启动自愈：重置 %d 个中断的本地排队提交为 failed", cur.rowcount)

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

    # --- 本地提交排队（准入调度）-----------------------------------------

    def sq_enqueue(
        self,
        owner: str,
        jobname: str,
        queue_name: str,
        script_path: str,
        workdir: str,
        cores: int,
        extra_l: Optional[str] = None,
    ) -> int:
        """把一次提交请求写入本地排队队列（真正 qsub 前的落地），返回队列项 id。"""
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO submission_queue
                    (owner, status, jobname, queue_name, script_path, workdir,
                     cores, extra_l, queued_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (owner, SQ_QUEUED, jobname, queue_name, script_path, workdir,
                 cores, extra_l, now, now),
            )
            self.conn.commit()
            return cur.lastrowid

    def sq_get(self, item_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM submission_queue WHERE id=?", (item_id,)
            ).fetchone()

    def sq_list(
        self, owner: Optional[str] = None, status: Optional[str] = None, limit: int = 500
    ) -> List[sqlite3.Row]:
        """列出本地排队项（可按属主/状态过滤），供列表页展示与调度器扫描使用。"""
        sql = "SELECT * FROM submission_queue WHERE 1=1"
        params: list = []
        if owner is not None:
            sql += " AND owner=?"
            params.append(owner)
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY queued_at LIMIT ?"
        params.append(limit)
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def sq_claim_for_submit(self, item_id: int) -> bool:
        """原子地把队列项从 queued 置为 submitting(准备调用 qsub)，成功返回 True。

        供调度器认领——避免同一项被并发的两次 tick(定时线程 + 提交请求内联触发
        + 轮询完成回调)重复提交。"""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE submission_queue SET status=?, updated_at=? "
                "WHERE id=? AND status=?",
                (SQ_SUBMITTING, time.time(), item_id, SQ_QUEUED),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def sq_mark_submitted(self, item_id: int, jobid: str) -> None:
        now = time.time()
        with self._lock:
            self.conn.execute(
                "UPDATE submission_queue SET status=?, jobid=?, "
                "submitted_at=?, updated_at=? WHERE id=?",
                (SQ_SUBMITTED, jobid, now, now, item_id),
            )
            self.conn.commit()

    def sq_mark_failed(self, item_id: int, msg: str) -> None:
        """qsub 调用失败：置为终态 failed，不自动重试（脚本/环境问题需人工确认）。"""
        with self._lock:
            self.conn.execute(
                "UPDATE submission_queue SET status=?, msg=?, updated_at=? WHERE id=?",
                (SQ_FAILED, msg, time.time(), item_id),
            )
            self.conn.commit()

    def sq_cancel(self, item_id: int, owner: Optional[str] = None) -> bool:
        """撤回一个尚未提交到 PBS 的排队项（仅 queued 状态可撤回）。

        owner 非空时要求属主匹配，供非管理员用户的越权校验。"""
        sql = "UPDATE submission_queue SET status=?, updated_at=? WHERE id=? AND status=?"
        params: list = [SQ_CANCELLED, time.time(), item_id, SQ_QUEUED]
        if owner is not None:
            sql += " AND owner=?"
            params.append(owner)
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.rowcount > 0

    def sq_delete_failed(self, item_id: int, owner: Optional[str] = None) -> bool:
        """删除一个提交失败的本地排队记录（仅 failed 状态可删；未占任何 PBS 资源）。

        owner 非空时要求属主匹配，供非管理员用户的越权校验。"""
        sql = "DELETE FROM submission_queue WHERE id=? AND status=?"
        params: list = [item_id, SQ_FAILED]
        if owner is not None:
            sql += " AND owner=?"
            params.append(owner)
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.rowcount > 0

    # --- 准入调度所需的用量统计 -------------------------------------------

    def active_counts_by_owner(self) -> Dict[str, int]:
        """各用户当前活跃（Q+R，即 derived_state=active）任务数。

        覆盖门户之外直接 qsub 的任务，用作 per-user 并发配额判定的分母。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT owner, COUNT(*) n FROM jobs WHERE derived_state=? GROUP BY owner",
                (ACTIVE,),
            ).fetchall()
        return {r["owner"]: r["n"] for r in rows}

    def running_cores_total(self) -> int:
        """当前集群已用核数 = 所有 pbs_state='R' 活跃任务的核数之和。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT nodes FROM jobs WHERE derived_state=? AND pbs_state='R'",
                (ACTIVE,),
            ).fetchall()
        return sum(cores_from_nodes(r["nodes"]) for r in rows)

    # --- 用户提交策略 -------------------------------------------------------

    def policy_get(self, user: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM user_policies WHERE user=?", (user,)
            ).fetchone()

    def policy_list(self) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM user_policies ORDER BY user"
            ).fetchall()

    def policy_upsert(
        self, user: str, max_concurrent: Optional[int], priority: int = 0
    ) -> sqlite3.Row:
        now = time.time()
        with self._lock:
            self.conn.execute(
                """INSERT INTO user_policies (user, max_concurrent, priority, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(user) DO UPDATE SET
                       max_concurrent=excluded.max_concurrent,
                       priority=excluded.priority,
                       updated_at=excluded.updated_at""",
                (user, max_concurrent, priority, now),
            )
            self.conn.commit()
            return self.conn.execute(
                "SELECT * FROM user_policies WHERE user=?", (user,)
            ).fetchone()

    def policy_delete(self, user: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM user_policies WHERE user=?", (user,))
            self.conn.commit()
            return cur.rowcount > 0

    # --- 试算任务 ---------------------------------------------------------

    def trial_create(
        self, owner: str, name: str, workdir: str, command: str,
        log_path: str, status_path: str,
    ) -> int:
        """登记一条试算任务（pid 稍后由 trial_set_started 回填），返回 id。"""
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO trial_tasks
                    (owner, name, workdir, command, status, log_path, status_path,
                     created_at, started_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (owner, name, workdir, command, TRIAL_RUNNING, log_path,
                 status_path, now, now),
            )
            self.conn.commit()
            return cur.lastrowid

    def trial_set_started(self, trial_id: int, pid: int, pgid: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE trial_tasks SET pid=?, pgid=? WHERE id=?",
                (pid, pgid, trial_id),
            )
            self.conn.commit()

    def trial_finish(
        self, trial_id: int, status: str,
        exit_code: Optional[int] = None, msg: Optional[str] = None,
    ) -> None:
        """置为终态并记结束时间/退出码。"""
        with self._lock:
            self.conn.execute(
                "UPDATE trial_tasks SET status=?, exit_code=?, msg=?, ended_at=? "
                "WHERE id=?",
                (status, exit_code, msg, time.time(), trial_id),
            )
            self.conn.commit()

    def trial_get(self, trial_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM trial_tasks WHERE id=?", (trial_id,)
            ).fetchone()

    def trial_list(
        self, owner: Optional[str] = None, limit: int = 500
    ) -> List[sqlite3.Row]:
        """列出试算任务（owner=None 不限属主，供管理员查看全部）。"""
        if owner is None:
            sql, params = "SELECT * FROM trial_tasks WHERE 1=1", []
        else:
            sql, params = "SELECT * FROM trial_tasks WHERE owner=?", [owner]
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def trial_list_running(self) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM trial_tasks WHERE status=?", (TRIAL_RUNNING,)
            ).fetchall()

    def trial_count_running(self) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) n FROM trial_tasks WHERE status=?", (TRIAL_RUNNING,)
            ).fetchone()
        return row["n"] if row else 0
