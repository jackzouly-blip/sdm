"""Torque `qstat -f` 输出解析。

折行规则（实测 Torque 6.1）：
  - 每条任务以 `Job Id: <id>` 起始。
  - 字段行：恰好 4 个空格 + `Key = value`，Key 可含点（resources_used.cput）。
  - 续行：8 个空格起，是上一字段值的延续，按列宽硬折断，可能切在任意字符
    （含路径中间），因此续行须去掉前导空白后**直接拼接、不加分隔符**。

解析结果为 dict[str, str]（保留原始字段），再由 to_job() 归一成结构化任务。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

# 字段行：行首恰好 4 空格（第 5 字符非空格），Key = value
_FIELD_RE = re.compile(r"^ {4}([A-Za-z][\w.]*) = (.*)$")
_JOBID_RE = re.compile(r"^Job Id:\s*(.+?)\s*$")

# Torque 时间格式：Mon Jun  1 19:48:57 2026
_TIME_FMT = "%a %b %d %H:%M:%S %Y"


def parse_qstat_f(text: str) -> List[Dict[str, str]]:
    """解析 qstat -f 文本，返回每个任务的原始字段字典列表。"""
    jobs: List[Dict[str, str]] = []
    cur: Optional[Dict[str, str]] = None
    cur_key: Optional[str] = None

    for raw in text.splitlines():
        m_id = _JOBID_RE.match(raw)
        if m_id:
            cur = {"Job Id": m_id.group(1)}
            jobs.append(cur)
            cur_key = None
            continue
        if cur is None:
            continue

        m_field = _FIELD_RE.match(raw)
        if m_field:
            cur_key = m_field.group(1)
            cur[cur_key] = m_field.group(2)
        elif cur_key is not None and raw.strip():
            # 续行：去前导空白后直接拼接到当前字段
            cur[cur_key] += raw.strip()

    return jobs


def parse_variable_list(value: str) -> Dict[str, str]:
    """解析 Variable_List 的 `K=V,K=V,...`（值里可能含 = 和 :，按首个 = 切）。"""
    result: Dict[str, str] = {}
    for item in value.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def parse_time(value: str) -> Optional[float]:
    """Torque 时间字符串 -> Unix 时间戳；失败返回 None。"""
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, _TIME_FMT).timestamp()
    except ValueError:
        return None


@dataclass
class Job:
    """结构化任务。"""

    jobid: str  # 完整 id，如 1785.hpcmaster
    name: str
    owner: str  # 实际属主用户名（用于权限隔离）
    state: str  # Q / R / C / E / H ...
    queue: str
    workdir: Optional[str]  # 任务工作目录
    exec_host: Optional[str]
    submit_ts: Optional[float]
    start_ts: Optional[float]
    end_ts: Optional[float]
    walltime_used: Optional[str]
    walltime_limit: Optional[str]
    nodes: Optional[str]
    exit_status: Optional[int]
    raw: Dict[str, str] = field(default_factory=dict)

    @property
    def short_id(self) -> str:
        """去掉 server 后缀的数字 id。"""
        return self.jobid.split(".", 1)[0]


def to_job(fields: Dict[str, str]) -> Job:
    """把原始字段字典归一成 Job。"""
    var_list = parse_variable_list(fields.get("Variable_List", ""))

    # 工作目录优先级：init_work_dir > PBS_O_WORKDIR
    workdir = fields.get("init_work_dir") or var_list.get("PBS_O_WORKDIR")

    # 属主：euser（实际执行用户）优先，回退 Job_Owner 的 @ 前
    owner = fields.get("euser")
    if not owner:
        job_owner = fields.get("Job_Owner", "")
        owner = job_owner.split("@", 1)[0] if job_owner else ""

    exit_status = None
    if "exit_status" in fields:
        try:
            exit_status = int(fields["exit_status"])
        except ValueError:
            pass

    return Job(
        jobid=fields.get("Job Id", ""),
        name=fields.get("Job_Name", ""),
        owner=owner,
        state=fields.get("job_state", ""),
        queue=fields.get("queue", ""),
        workdir=workdir,
        exec_host=fields.get("exec_host"),
        submit_ts=parse_time(fields.get("qtime", "")) or parse_time(fields.get("ctime", "")),
        start_ts=parse_time(fields.get("start_time", "")),
        end_ts=parse_time(fields.get("comp_time", "")) or parse_time(fields.get("mtime", "")) if fields.get("job_state") == "C" else None,
        walltime_used=fields.get("resources_used.walltime"),
        walltime_limit=fields.get("Resource_List.walltime"),
        nodes=fields.get("Resource_List.nodes"),
        exit_status=exit_status,
        raw=fields,
    )


def parse_jobs(text: str) -> List[Job]:
    """解析 qstat -f 文本为结构化 Job 列表。"""
    return [to_job(f) for f in parse_qstat_f(text)]
