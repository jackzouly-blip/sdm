"""结果收集与在线查看的编排节点。

两个节点补齐"算完之后"这一段：

    internal.collect_results   扫描工作目录，把结果文件登记成 sim_result
    viewer.prepare             为可视化结果生成查看器产物（复用现有 d3plot 链路）

result_type 是查看器插件的分发键。目前只有碰撞（d3plot）有查看器实现，其余类型
先登记、可下载；将来 CFD / NVH / 疲劳各自注册解析器与展示组件，这里不用改。

**不做提取节点**：任务完成时作业轮询器已经自动派发过提取规则
（poller → dispatcher.dispatch_many），再加一个编排节点只会重复执行同一批命令。
若将来确实需要把提取作为流水线里的显式步骤（比如按工况用不同规则），
再基于 task: 句柄机制补，不在这里预先造。
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from ..logger import get_logger
from .nodes import Done, Failed, NodeContext, Waiting

log = get_logger(__name__)

# TaskManager 任务句柄前缀。与 hpc_bridge 的 sq: 同思路：
# 句柄落库故服务重启后仍可续，比内存里的订阅可靠。
TASK_PREFIX = "task:"

# 结果文件识别。与 netdisk.autoshare 的口径保持一致，避免两处对"什么算结果"
# 各说各话。
_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("d3plot", re.compile(r"^d3plot(\d+|[a-z]+)?$", re.I)),
    ("binout", re.compile(r"^binout", re.I)),
    ("d3hsp", re.compile(r"^d3hsp", re.I)),
    ("h3d", re.compile(r"\.h3d$", re.I)),
]


def classify(name: str) -> Optional[str]:
    """按文件名判定 result_type；不是结果文件则返回 None。"""
    for rtype, pat in _PATTERNS:
        if pat.match(name) if pat.pattern.startswith("^") else pat.search(name):
            return rtype
    return None


def _exec_collect_results(ctx: NodeContext):
    """扫描工作目录，把结果文件登记成 sim_result。

    幂等：同一 (job, 路径) 不重复登记，故重跑或补跑都安全。
    """
    db = ctx.db
    run = db.get_run(ctx.run_id)
    pid = run["sim_project_id"]
    if not pid:
        return Failed("该运行未绑定仿真项目，无法确定工作目录")
    proj = db.get_project(pid)
    if proj is None or not proj["workdir"]:
        return Failed("仿真项目未设置工作目录（workdir）")

    # 优先用上游 hpc 节点回传的实际 workdir（作业可能在映射后的路径下跑）
    workdir = ctx.inputs.get("workdir") or proj["workdir"]

    # 结果挂在哪个 sim_job 下：优先上游作业号，其次工况的最后一个作业
    sim_job_id = None
    hpc_jobid = ctx.inputs.get("hpc_jobid")
    if hpc_jobid:
        row = db.find_job_by_hpc(str(hpc_jobid))
        if row is not None:
            sim_job_id = row["id"]
    if sim_job_id is None and ctx.sim_subject_id:
        jobs = db.list_jobs(ctx.sim_subject_id)
        if jobs:
            sim_job_id = jobs[-1]["id"]
    if sim_job_id is None:
        if not ctx.sim_subject_id:
            return Failed("该运行未绑定工况，结果无处归属")
        sim_job_id = db.create_job(ctx.sim_subject_id, submit_mode="pbs")
        if hpc_jobid:
            db.mark_job_submitted(sim_job_id, str(hpc_jobid))

    only = ctx.params.get("result_types")
    wanted = set(only) if isinstance(only, list) and only else None

    try:
        entries = list(os.scandir(workdir))
    except OSError as e:
        return Failed(f"扫描工作目录失败: {e}")

    existing = {r["file_path"] for r in db.list_results(sim_job_id)}
    registered: Dict[str, int] = {}
    for e in entries:
        try:
            if not e.is_file():
                continue
        except OSError:
            continue
        rtype = classify(e.name)
        if rtype is None or (wanted and rtype not in wanted):
            continue
        if e.path in existing:
            continue
        try:
            size = e.stat(follow_symlinks=False).st_size
        except OSError:
            size = 0
        db.add_result(sim_job_id, rtype, e.path, {"name": e.name, "size": size})
        registered[rtype] = registered.get(rtype, 0) + 1

    total = sum(registered.values())
    log.info("编排收集结果 run=%s job=%s 新增 %d 个", ctx.run_id, sim_job_id, total)
    return Done({
        "sim_job_id": sim_job_id,
        "workdir": workdir,
        "registered": registered,
        "registered_total": total,
    })


def _exec_viewer_prepare(ctx: NodeContext):
    """为结果生成在线查看产物。

    目前只有碰撞（d3plot）有查看器实现，故复用现有的 d3plot_view 任务链路：
    投进 TaskManager，节点挂起在 task: 句柄上，任务完成后回流。

    其余 result_type 暂无查看器，此节点会明确说明而不是假装成功——
    等对应查看器插件注册后，这里按 result_type 分发即可。
    """
    db = ctx.db
    tm = ctx.task_manager
    if tm is None:
        return Failed("任务管理器未装配，无法生成查看产物")

    sim_job_id = ctx.inputs.get("sim_job_id")
    if not sim_job_id:
        return Failed("上游未提供 sim_job_id（应先经 internal.collect_results）")

    results = db.list_results(str(sim_job_id), result_type="d3plot")
    if not results:
        return Failed("该作业没有 d3plot 结果，无法生成查看产物")

    # d3plot 家族里主文件（无数字后缀）是查看入口
    main = next((r for r in results
                 if os.path.basename(r["file_path"]).lower() == "d3plot"), results[0])
    d3path = main["file_path"]

    # 缓存键必须与查看器完全一致，否则本节点生成的产物查看器根本找不到，
    # 点开还会重新解析一遍——这个节点的意义就没了。故直接复用查看器的
    # 家族签名与键计算，不在这里另写一套公式。
    from ..d3plot.router import _family_sig
    from ..d3plot.service import cache_key

    try:
        st = os.stat(d3path)
    except OSError as e:
        return Failed(f"读取 d3plot 失败: {e}")
    key = cache_key(d3path, st.st_mtime, st.st_size, _family_sig(ctx.owner, d3path))

    params = {"d3plot": d3path, "key": key, "run_as": ctx.owner}
    for k in ("max_states", "max_tris"):
        if ctx.params.get(k) is not None:
            params[k] = ctx.params[k]

    try:
        task_id = tm.submit("d3plot_view", owner=ctx.owner, params=params)
    except Exception as e:  # noqa: BLE001
        return Failed(f"提交 d3plot 解析任务失败: {e}")

    return Waiting(ref=f"{TASK_PREFIX}{task_id}", hint=f"正在解析 d3plot（{os.path.basename(d3path)}）")


def tick_task_refs(db, task_manager) -> int:
    """把已完成的 TaskManager 任务回流到挂起的节点。

    与 hpc_bridge 的 sq: 句柄同一思路：句柄落库、轮询解析，故服务重启后
    仍能续上——比内存订阅可靠。由作业轮询器每轮调用。返回推进的节点数。
    """
    from .engine import get_engine

    eng = get_engine()
    if eng is None or task_manager is None:
        return 0

    advanced = 0
    for node in db.list_waiting_nodes():
        ref = node["external_ref"] or ""
        if not ref.startswith(TASK_PREFIX):
            continue
        snap = task_manager.snapshot(ref[len(TASK_PREFIX):])
        if snap is None or snap["status"] in ("queued", "running"):
            continue
        rid, nid = node["pipeline_run_id"], node["node_id"]
        try:
            if snap["status"] == "success":
                eng.complete_node(rid, nid, outputs={"viewer_key": (snap["result"] or {}).get("key"),
                                                     **(snap["result"] or {})})
            else:
                eng.complete_node(rid, nid,
                                  error=snap["error"] or f"任务状态 {snap['status']}")
            advanced += 1
        except Exception:  # noqa: BLE001
            log.exception("任务回流失败 node=%s ref=%s", nid, ref)
    return advanced
