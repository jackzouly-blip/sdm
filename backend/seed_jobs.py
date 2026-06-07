"""一次性脚本：往 jobs 表插入若干虚拟任务，便于本地查看任务管理效果。

仅用于演示/联调，不参与生产。运行：
    HPC_FS_ROOTS=/tmp/hpc-sandbox .venv/bin/python seed_jobs.py
"""
from __future__ import annotations

import json
import time

from app.config import get_settings
from app.db.jobs_db import ACTIVE, DONE, JobsDB

now = time.time()
H = 3600
SB = "/tmp/hpc-sandbox"
OWNER = "leiyou"

# (jobid, name, pbs_state, derived, queue, workdir, exec_host, submit, start,
#  end, wt_used, wt_limit, nodes, exit_status)
ROWS = [
    (
        "1803.head", "cfd_solver", "R", ACTIVE, "batch", f"{SB}/case1",
        "node03/0-15", now - 2 * H, now - 2 * H + 30, None, "02:00:15",
        "24:00:00", "1:ppn=16", None,
    ),
    (
        "1804.head", "mesh_refine", "R", ACTIVE, "batch", f"{SB}/case2",
        "node05/0-7", now - 40 * 60, now - 40 * 60 + 12, None, "00:39:50",
        "12:00:00", "1:ppn=8", None,
    ),
    (
        "1805.head", "param_sweep", "Q", ACTIVE, "batch", f"{SB}/case2",
        None, now - 8 * 60, None, None, None, "06:00:00", "2:ppn=16", None,
    ),
    (
        "1806.head", "post_process", "Q", ACTIVE, "debug", f"{SB}/case1/results",
        None, now - 3 * 60, None, None, None, "01:00:00", "1:ppn=4", None,
    ),
    (
        "1799.head", "viz_render", "C", DONE, "batch", f"{SB}/case1/results",
        "node02/0-3", now - 26 * H, now - 26 * H + 20, now - 25 * H,
        "00:58:40", "02:00:00", "1:ppn=4", 0,
    ),
    (
        "1795.head", "solver_restart", "C", DONE, "batch", f"{SB}/case2",
        "node07/0-15", now - 50 * H, now - 50 * H + 15, now - 48 * H,
        "01:47:22", "24:00:00", "1:ppn=16", 1,
    ),
    (
        "1788.head", "geometry_import", "C", DONE, "debug", f"{SB}/case1",
        "node01/0", now - 72 * H, now - 72 * H + 5, now - 72 * H + 600,
        "00:09:55", "00:30:00", "1:ppn=1", 0,
    ),
]


def main() -> None:
    s = get_settings()
    db = JobsDB(s.db_path)
    for r in ROWS:
        (
            jobid, name, pbs_state, derived, queue, workdir, exec_host,
            submit, start, end, wt_used, wt_limit, nodes, exit_status,
        ) = r
        raw = {
            "Job_Name": name,
            "job_state": pbs_state,
            "queue": queue,
            "Resource_List.nodes": nodes or "",
            "Resource_List.walltime": wt_limit or "",
        }
        db.conn.execute(
            """
            INSERT INTO jobs
                (jobid, short_id, name, owner, pbs_state, derived_state, queue,
                 workdir, exec_host, submit_ts, start_ts, end_ts, walltime_used,
                 walltime_limit, nodes, exit_status, raw, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(jobid) DO UPDATE SET
                name=excluded.name, pbs_state=excluded.pbs_state,
                derived_state=excluded.derived_state, queue=excluded.queue,
                workdir=excluded.workdir, exec_host=excluded.exec_host,
                start_ts=excluded.start_ts, end_ts=excluded.end_ts,
                walltime_used=excluded.walltime_used, exit_status=excluded.exit_status,
                raw=excluded.raw, last_seen=excluded.last_seen
            """,
            (
                jobid, jobid.split(".")[0], name, OWNER, pbs_state, derived,
                queue, workdir, exec_host, submit, start, end, wt_used,
                wt_limit, nodes, exit_status,
                json.dumps(raw, ensure_ascii=False), now, now,
            ),
        )
    db.conn.commit()
    n = db.conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE owner=?", (OWNER,)
    ).fetchone()["c"]
    db.close()
    print(f"已写入 {len(ROWS)} 条虚拟任务，当前 owner={OWNER} 共 {n} 条")


if __name__ == "__main__":
    main()
