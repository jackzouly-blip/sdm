"""按起止日期统计每个用户的 CPU 机时（仅管理员）。

口径：机时 = Σ(作业落在统计区间内的运行时长 × 核数)，按用户汇总。
  - 跨区间的作业只算落在 [起, 止] 内的那段（重叠时长）。
  - 核数：已完成取 accounting 的 total_execution_slots；运行中按 Resource_List.nodes 的 nodect×ppn。

数据源（两者不重复）：
  - 已完成作业：Torque accounting 日志 /var/spool/torque/server_priv/accounting/YYYYMMDD 的 E 记录。
  - 运行中作业：实时 qstat -f（job_state=R），结束时间用“现在”。
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth.session import current_user, is_admin_request
from ..config import get_settings
from ..logger import get_logger
from ..pbs.cores import cores_from_nodes as _cores_from_nodes
from ..pbs.parser import parse_qstat_f
from ..pbs.qstat import _qstat_bin

log = get_logger(__name__)
router = APIRouter(prefix="/stats", tags=["stats"])

# accounting 日志目录（可用环境变量 HPC_ACCOUNTING_DIR 覆盖）
ACCT_DIR = os.environ.get(
    "HPC_ACCOUNTING_DIR", "/var/spool/torque/server_priv/accounting"
)
TORQUE_TIME_FMT = "%a %b %d %H:%M:%S %Y"

# accounting E 记录的 key=value 串解析：值可能含空格（如 group=domain users），
# 故按“下一个 key= 边界”切分，key 仅由字母/数字/点/下划线组成。
_KV_RE = re.compile(r"([\w.]+)=(.*?)(?=\s+[\w.]+=|\s*$)")


def _parse_acct_fields(rest: str) -> Dict[str, str]:
    return {m.group(1): m.group(2) for m in _KV_RE.finditer(rest)}


@router.get("/cpu-hours")
def cpu_hours(
    start: str = Query(..., description="开始日期 YYYY-MM-DD（本地时间，含当天）"),
    end: str = Query(..., description="结束日期 YYYY-MM-DD（本地时间，含当天）"),
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
):
    """按用户统计 [start, end] 区间内的 CPU 机时。仅管理员可用。"""
    if not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权查看统计")
    try:
        d0 = datetime.strptime(start, "%Y-%m-%d")
        d1 = datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD")

    now = time.time()
    range_start = d0.timestamp()  # 起始当天 00:00（本地）
    range_end = min((d1 + timedelta(days=1)).timestamp(), now)  # 结束当天 24:00，封顶到现在
    if range_end <= range_start:
        raise HTTPException(status_code=400, detail="结束日期需不早于开始日期")

    agg: Dict[str, dict] = {}

    def add(u: str, seconds: float, cores: int):
        a = agg.setdefault(u, {"user": u, "cpu_hours": 0.0, "job_count": 0})
        a["cpu_hours"] += seconds * cores / 3600.0
        a["job_count"] += 1

    # ---- 1) 已完成作业：读 accounting 日志 ----
    # 重叠区间的作业其 end >= range_start，E 记录按结束日写入；
    # 为兼顾跨界长作业（最长 walltime 数天），多读结束日之后几天。
    day = d0.date()
    last = min(d1.date() + timedelta(days=5), date.today())
    while day <= last:
        path = os.path.join(ACCT_DIR, day.strftime("%Y%m%d"))
        day += timedelta(days=1)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    parts = line.split(";")
                    if len(parts) < 4 or parts[1] != "E":
                        continue
                    f = _parse_acct_fields(parts[3])
                    try:
                        js = int(f["start"])
                        je = int(f["end"])
                    except (KeyError, ValueError):
                        continue
                    cores = 0
                    try:
                        cores = int(f.get("total_execution_slots") or 0)
                    except ValueError:
                        cores = 0
                    if cores <= 0:
                        cores = _cores_from_nodes(
                            f.get("Resource_List.nodes"), f.get("Resource_List.nodect")
                        )
                    ov = min(je, range_end) - max(js, range_start)
                    if ov <= 0 or cores <= 0:
                        continue
                    add(f.get("user") or "unknown", ov, cores)
        except OSError as e:
            log.warning("读取 accounting 文件失败 %s: %s", path, e)

    # ---- 2) 运行中作业：实时 qstat ----
    try:
        proc = subprocess.run(
            [_qstat_bin(), "-f"], capture_output=True, text=True, timeout=30, check=False
        )
        for rec in parse_qstat_f(proc.stdout):
            if rec.get("job_state") != "R":
                continue
            st = rec.get("start_time")
            if not st:
                continue
            try:
                js = datetime.strptime(st.strip(), TORQUE_TIME_FMT).timestamp()
            except ValueError:
                continue
            cores = _cores_from_nodes(
                rec.get("Resource_List.nodes"), rec.get("Resource_List.nodect")
            )
            owner = (rec.get("Job_Owner") or "").split("@")[0] or rec.get("euser") or "unknown"
            ov = min(now, range_end) - max(js, range_start)
            if ov <= 0 or cores <= 0:
                continue
            add(owner, ov, cores)
    except Exception as e:  # noqa: BLE001
        log.warning("统计运行中作业失败: %s", e)

    rows = sorted(agg.values(), key=lambda x: x["cpu_hours"], reverse=True)
    for r in rows:
        r["cpu_hours"] = round(r["cpu_hours"], 2)
    return {
        "start": start,
        "end": end,
        "rows": rows,
        "total_cpu_hours": round(sum(r["cpu_hours"] for r in rows), 2),
    }


@router.get("/jobs")
def jobs_list(
    start: str = Query(..., description="开始日期 YYYY-MM-DD"),
    end: str = Query(..., description="结束日期 YYYY-MM-DD"),
    only_user: str | None = Query(None, alias="user_filter", description="仅统计该用户，留空为全部"),
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
):
    """导出任务清单：区间内每个作业的明细（含开始/结束时间、核数、机时）。仅管理员。

    user_filter 非空时只返回该用户的作业。
    """
    if not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权查看统计")
    try:
        d0 = datetime.strptime(start, "%Y-%m-%d")
        d1 = datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD")

    now = time.time()
    range_start = d0.timestamp()
    range_end = min((d1 + timedelta(days=1)).timestamp(), now)
    if range_end <= range_start:
        raise HTTPException(status_code=400, detail="结束日期需不早于开始日期")

    out: list[dict] = []

    # ---- 已完成作业（accounting）----
    day = d0.date()
    last = min(d1.date() + timedelta(days=5), date.today())
    while day <= last:
        path = os.path.join(ACCT_DIR, day.strftime("%Y%m%d"))
        day += timedelta(days=1)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    parts = line.split(";")
                    if len(parts) < 4 or parts[1] != "E":
                        continue
                    jobid = parts[2].strip()
                    f = _parse_acct_fields(parts[3])
                    try:
                        js = int(f["start"])
                        je = int(f["end"])
                    except (KeyError, ValueError):
                        continue
                    if min(je, range_end) - max(js, range_start) <= 0:  # 不重叠则跳过
                        continue
                    try:
                        cores = int(f.get("total_execution_slots") or 0)
                    except ValueError:
                        cores = 0
                    if cores <= 0:
                        cores = _cores_from_nodes(
                            f.get("Resource_List.nodes"), f.get("Resource_List.nodect")
                        )
                    juser = f.get("user") or "unknown"
                    if only_user and juser != only_user:
                        continue
                    hours = (je - js) / 3600.0
                    out.append({
                        "jobid": jobid,
                        "short_id": jobid.split(".")[0],
                        "user": juser,
                        "queue": f.get("queue") or "",
                        "name": f.get("jobname") or "",
                        "cores": cores,
                        "start_ts": js,
                        "end_ts": je,
                        "hours": round(hours, 2),
                        "cpu_hours": round(hours * cores, 2),
                        "state": "完成",
                        "exit_status": f.get("Exit_status"),
                    })
        except OSError as e:
            log.warning("读取 accounting 文件失败 %s: %s", path, e)

    # ---- 运行中作业（qstat）----
    try:
        proc = subprocess.run(
            [_qstat_bin(), "-f"], capture_output=True, text=True, timeout=30, check=False
        )
        for rec in parse_qstat_f(proc.stdout):
            if rec.get("job_state") != "R":
                continue
            st = rec.get("start_time")
            if not st:
                continue
            try:
                js = datetime.strptime(st.strip(), TORQUE_TIME_FMT).timestamp()
            except ValueError:
                continue
            if min(now, range_end) - max(js, range_start) <= 0:
                continue
            cores = _cores_from_nodes(
                rec.get("Resource_List.nodes"), rec.get("Resource_List.nodect")
            )
            juser = (rec.get("Job_Owner") or "").split("@")[0] or "unknown"
            if only_user and juser != only_user:
                continue
            hours = (now - js) / 3600.0
            jobid = rec.get("Job Id") or ""
            out.append({
                "jobid": jobid,
                "short_id": jobid.split(".")[0],
                "user": juser,
                "queue": rec.get("queue") or "",
                "name": rec.get("Job_Name") or "",
                "cores": cores,
                "start_ts": int(js),
                "end_ts": None,
                "hours": round(hours, 2),
                "cpu_hours": round(hours * cores, 2),
                "state": "运行中",
                "exit_status": None,
            })
    except Exception as e:  # noqa: BLE001
        log.warning("统计运行中作业失败: %s", e)

    out.sort(key=lambda x: x["start_ts"], reverse=True)
    return {"start": start, "end": end, "count": len(out), "rows": out}
