"""调用 `pbsnodes -a` 采集各计算节点状态，并解析成结构化节点列表。

pbsnodes 是只读查询，以服务自身身份（root）执行即可。输出格式（Torque 6.1）：

    node01
         state = free
         power_state = Running
         np = 64
         ntype = cluster
         status = opsys=linux,...,loadave=0.10,...,ncpus=64,physmem=...kb,availmem=...
         jobs = 0/1785.hpcmaster,1/1785.hpcmaster
         mom_service_port = 15002
         gpus = 0

折行规则：节点名顶格（无缩进），其后为缩进的 `key = value` 字段行，节点之间以
空行分隔。status 字段是逗号分隔的 `k=v` 串（含节点实时负载/内存）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from typing import Dict, List, Optional

from ..logger import get_logger

log = get_logger(__name__)

# 与 submit/scheduler.py 一致的 Torque 安装前缀兜底
_FALLBACK_BIN = "/usr/local/torque-6.1.2/bin/pbsnodes"

# 字段行：行首有缩进 + `key = value`
_FIELD_RE = re.compile(r"^\s+([A-Za-z][\w.]*)\s*=\s*(.*)$")

# 视为“不可用/异常”的状态关键字（节点需关注）
_BAD_STATES = ("down", "offline", "state-unknown", "unknown")


class PbsnodesError(RuntimeError):
    pass


def _pbsnodes_bin() -> str:
    return shutil.which("pbsnodes") or _FALLBACK_BIN


def _parse_status(value: str) -> Dict[str, str]:
    """解析 status 字段的 `k=v,k=v,...`（值一般不含逗号）。"""
    out: Dict[str, str] = {}
    for item in value.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _parse_jobs(value: str) -> tuple[int, int]:
    """从 jobs 字段解析(已用核槽数, 运行作业数)。

    每个逗号项是 `核槽/作业id`，核槽可为单个索引或范围：
      - 逐核写法：`0/1785.hpc,1/1785.hpc,2/1786.hpc`（3 核，2 作业）
      - 范围写法：`0-63/2364.hpc`（独占节点常见，64 核，1 作业）
    核槽数=各项核数之和（范围计 b-a+1）；作业数=去重后的作业 id 数。
    """
    value = value.strip()
    if not value:
        return 0, 0
    slots = 0
    jobids = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        spec, sep, jid = item.partition("/")
        if sep:
            jobids.add(jid)
        spec = spec.strip()
        if "-" in spec:
            a, _, b = spec.partition("-")
            try:
                slots += int(b) - int(a) + 1
                continue
            except ValueError:
                pass
        slots += 1
    return slots, len(jobids)


def _to_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_pbsnodes(text: str) -> List[dict]:
    """解析 `pbsnodes -a` 文本为结构化节点列表。"""
    nodes: List[dict] = []
    cur: Optional[Dict[str, str]] = None

    for raw in text.splitlines():
        if not raw.strip():
            continue
        # 顶格（非空白起始）= 新节点名
        if not raw[0].isspace():
            cur = {"__name__": raw.strip()}
            nodes.append(cur)  # type: ignore[arg-type]
            continue
        if cur is None:
            continue
        m = _FIELD_RE.match(raw)
        if m:
            cur[m.group(1)] = m.group(2)

    return [_normalize(f) for f in nodes]


def _normalize(fields: Dict[str, str]) -> dict:
    """把原始字段字典归一成前端友好的节点结构。"""
    name = fields.get("__name__", "")
    raw_state = (fields.get("state") or "").strip()
    states = [s.strip() for s in raw_state.split(",") if s.strip()]

    status = _parse_status(fields.get("status", ""))
    used_slots, running_jobs = _parse_jobs(fields.get("jobs", ""))
    np = _to_int(fields.get("np"))

    # 健康度：命中任一异常状态即视为需关注；否则正常（含 free/job-exclusive/busy）
    low = raw_state.lower()
    if any(bad in low for bad in _BAD_STATES):
        if "down" in low:
            health = "down"
        elif "offline" in low:
            health = "offline"
        else:
            health = "unknown"
    else:
        health = "up"

    return {
        "name": name,
        "state": raw_state,
        "states": states,
        "health": health,
        "np": np,
        "used_slots": used_slots,
        "running_jobs": running_jobs,
        "ntype": fields.get("ntype"),
        "power_state": fields.get("power_state"),
        # 实时负载/内存（来自 mom 上报的 status，节点 down 时可能缺失）
        "loadave": status.get("loadave"),
        "ncpus": _to_int(status.get("ncpus")),
        "physmem": status.get("physmem"),
        "availmem": status.get("availmem"),
        "totmem": status.get("totmem"),
        "gpus": _to_int(fields.get("gpus")),
        "raw": {k: v for k, v in fields.items() if k != "__name__"},
    }


def fetch_nodes(timeout: float = 30) -> List[dict]:
    """执行 `pbsnodes -a` 并解析为结构化节点列表。"""
    bin_path = _pbsnodes_bin()
    try:
        proc = subprocess.run(
            [bin_path, "-a"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise PbsnodesError("未找到 pbsnodes 命令，请确认服务运行在装有 Torque 的管理节点上") from e
    except subprocess.TimeoutExpired as e:
        raise PbsnodesError(f"pbsnodes 超时（>{timeout}s）") from e

    if proc.returncode != 0 and proc.stderr.strip():
        raise PbsnodesError(f"pbsnodes 执行失败: {proc.stderr.strip()}")

    nodes = parse_pbsnodes(proc.stdout)
    log.debug("pbsnodes 采集到 %d 个节点", len(nodes))
    return nodes
