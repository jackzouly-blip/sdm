"""deck → GLB 的异步转换任务。

与 d3plot 解析同款：以目标用户身份（`run_as_user`）执行转换脚本，
因此读取 deck 及其 include 树用的是该用户的权限，而不是后端的 root。

实测参考（真实座椅整车模型）：主控 .key + 54 个 include，152 万节点 /
148 万壳 / 116 万实体 → 465 万三角面、714 个零件、72 MB GLB，耗时约 40 秒。
故必须异步执行，且节点侧走 `task:` 句柄等待（见 sim/results.py）。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict

from ...config import BACKEND_DIR, get_settings
from ...logger import get_logger
from ...privilege.actas import run_as_user
from ...tasks.manager import TaskHandle, register_task

log = get_logger(__name__)

# 三角面预算，0 表示不限。
#
# 定得高是有意的：整车 deck 实测 465 万面 / 72 MB，现代显卡渲染无压力，
# 局域网下载也就几秒。而预算一旦卡住就要丢零件——丢零件对"提交前看一眼模型
# 对不对"是致命的（缺了什么恰恰是要看的）。所以宁可多传几十 MB，
# 也让典型模型完整通过；这个预算只作极端模型的安全阀。
DEFAULT_MAX_TRIANGLES = 8_000_000


def _python() -> str:
    """转换脚本用的解释器。沿用 d3plot 的配置项，避免再引入一个。"""
    return get_settings().d3plot_python or sys.executable


@register_task("sim_deck_convert")
def sim_deck_convert(handle: TaskHandle, params: dict) -> Dict:
    """把 LS-DYNA deck 转成 GLB，并写回几何版本的 lightweight_file。

    params: deck_path / out_path / gid / run_as / max_triangles
    """
    deck_path = params["deck_path"]
    out_path = params["out_path"]
    gid = params.get("gid")
    user = params.get("run_as") or handle.owner
    max_tris = int(params.get("max_triangles", DEFAULT_MAX_TRIANGLES))

    handle.update(phase="解析 deck 与 include 树", progress=10)

    argv = [_python(), "-m", "app.sim.deck.convert", deck_path, out_path, str(max_tris)]
    proc = run_as_user(user, argv, cwd=str(BACKEND_DIR),
                       timeout=get_settings().d3plot_timeout)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[-1200:]
        raise RuntimeError(f"deck 转换失败: {err}")

    handle.update(phase="写回几何版本", progress=90)
    try:
        summary = json.loads(proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        raise RuntimeError(f"转换脚本未返回可解析的摘要: {e}")

    # 写回：产物路径 + 摘要。摘要里的 missingIncludes 尤其重要——
    # include 缺失意味着模型不完整，用户看到的可能只是半个模型。
    if gid:
        # 走引擎的全局访问器取库，不 import main（那会形成循环导入）
        from ..engine import get_engine

        eng = get_engine()
        if eng is None:
            raise RuntimeError("编排引擎未装配，无法写回几何版本")
        eng.db.set_geometry_lightweight(gid, out_path, summary)

    handle.update(phase="完成", progress=100)
    log.info("deck 转换完成 gid=%s 零件=%s 三角面=%s 缺失include=%s",
             gid, summary.get("partCount"), summary.get("triangleCount"),
             summary.get("missingIncludeCount"))
    return summary
