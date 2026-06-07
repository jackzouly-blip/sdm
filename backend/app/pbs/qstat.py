"""调用 qstat 采集当前任务快照。

qstat 是只读查询，对所有用户可见全集群任务，因此以服务自身身份（root）执行
即可，无需降权。后续按登录用户过滤在 API 层完成。

注意：`qstat -f` 默认只返回 Q/R/E 等活跃任务；完成（C）任务会很快从 qstat
消失（取决于服务端 keep_completed）。完整历史需结合 accounting 日志（后续实现）。
"""
from __future__ import annotations

import shutil
import subprocess
from typing import List

from ..logger import get_logger
from .parser import Job, parse_jobs

log = get_logger(__name__)


class QstatError(RuntimeError):
    pass


def _qstat_bin() -> str:
    path = shutil.which("qstat")
    if not path:
        raise QstatError("未找到 qstat 命令，请确认服务运行在装有 Torque 客户端的节点上")
    return path


def fetch_jobs(timeout: float = 30) -> List[Job]:
    """执行 `qstat -f` 并解析为结构化任务列表。"""
    bin_path = _qstat_bin()
    try:
        proc = subprocess.run(
            [bin_path, "-f"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise QstatError(f"qstat 超时（>{timeout}s）") from e

    # 无任务时 qstat -f 返回空输出、退出码 0；有错误才非 0
    if proc.returncode != 0 and proc.stderr.strip():
        raise QstatError(f"qstat 执行失败: {proc.stderr.strip()}")

    jobs = parse_jobs(proc.stdout)
    log.debug("qstat 采集到 %d 个活跃任务", len(jobs))
    return jobs


def fetch_job(jobid: str, timeout: float = 30) -> Job | None:
    """执行 `qstat -f <jobid>` 获取单个任务详情。"""
    bin_path = _qstat_bin()
    proc = subprocess.run(
        [bin_path, "-f", jobid],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        # 任务不存在 / 已完成移出队列
        return None
    jobs = parse_jobs(proc.stdout)
    return jobs[0] if jobs else None
