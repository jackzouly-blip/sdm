"""数据提取任务：任务完成后在其工作目录里运行配置好的第三方提取命令。

权限与安全：
  - 以任务属主身份降权执行（run_as_user），读写受 OS 权限约束。
  - 命令模板用 shlex 拆成 argv，绝不走 shell，避免命令注入。
  - 工作目录 cwd=workdir；超时由 HPC_EXTRACT_TIMEOUT 控制。
"""
from __future__ import annotations

import os
import shlex
from typing import Dict, List

from ..config import get_settings
from ..logger import get_logger
from ..privilege.actas import run_as_user
from ..tasks.manager import TaskHandle, register_task

log = get_logger(__name__)

# 命令模板支持的占位符 -> 取自任务上下文的 key
_PLACEHOLDERS = ("workdir", "jobid", "short_id", "name", "owner", "queue")


def render_argv(command: str, ctx: dict) -> List[str]:
    """把命令模板拆成 argv 并替换占位符。

    先 shlex 分词（按 shell 词法但不执行 shell），再对每个片段做占位符替换，
    这样占位符值即便含空格也只会落在单个 argv 元素里，不会被二次分词。
    """
    if not command or not command.strip():
        raise ValueError("提取命令为空")
    tokens = shlex.split(command)
    if not tokens:
        raise ValueError("提取命令为空")
    out: List[str] = []
    for tok in tokens:
        for ph in _PLACEHOLDERS:
            tok = tok.replace("{" + ph + "}", str(ctx.get(ph, "")))
        out.append(tok)
    return out


@register_task("job_extract")
def job_extract(handle: TaskHandle, params: dict) -> Dict:
    """提取任务主体。

    params:
      jobid/short_id/name/owner/queue/workdir: 任务上下文（占位符来源）
      rule_id/rule_name: 触发的规则
      command: 命令模板字符串
    """
    s = get_settings()
    workdir = params.get("workdir") or ""
    owner = params.get("owner") or handle.owner
    rule_name = params.get("rule_name") or "提取规则"

    if not workdir:
        raise ValueError("任务无工作目录，无法执行提取")
    if not os.path.isdir(workdir):
        raise ValueError(f"工作目录不存在: {workdir}")

    argv = render_argv(params["command"], params)

    handle.update(phase=f"执行提取：{rule_name}", progress=10)
    log.info(
        "提取任务 job=%s rule=%s owner=%s cwd=%s argv=%s",
        params.get("jobid"),
        rule_name,
        owner,
        workdir,
        argv,
    )
    proc = run_as_user(owner, argv, cwd=workdir, timeout=s.extract_timeout)
    stdout = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    stderr = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""

    if proc.returncode != 0:
        raise RuntimeError(
            f"提取命令退出码 {proc.returncode}：{stderr[:800] or stdout[:800]}"
        )

    handle.update(phase="完成", progress=100)
    return {
        "jobid": params.get("jobid"),
        "rule_id": params.get("rule_id"),
        "rule_name": rule_name,
        "command": " ".join(argv),
        "returncode": proc.returncode,
        "stdout": stdout[-4000:],
        "stderr": stderr[-2000:],
    }
