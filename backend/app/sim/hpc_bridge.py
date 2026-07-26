"""编排与现有 HPC 链路的桥接。

编排层**不重新实现提交与轮询**——那两条链路已经成熟（本地排队、用户配额、
全局核数网关、qstat 轮询、完成检测）。这里只做两件事：

  提交侧：把上游节点产出的输入卡落盘，写提交脚本，投进现有的 submission_queue，
          再触发一次准入调度。之后一切照旧走既有流程。
  回流侧：作业结束时由轮询器调 on_jobs_finished，据外部句柄找到等待中的编排
          节点并推进。

外部句柄有两种形态，因为提交是两段式的：

    sq:<队列项 id>   已入本地排队、尚未 qsub（受配额或核数余量限制）
    <PBS 作业号>     已 qsub

回流时先把 sq: 句柄解析成真实作业号，再判完成——这样"排队中"这个中间态不会
让节点失去追踪。
"""
from __future__ import annotations

import os
import re
import uuid
from typing import List, Optional, Tuple

from ..logger import get_logger

log = get_logger(__name__)

# 本地排队阶段的句柄前缀
SQ_PREFIX = "sq:"

_NODES_RE = re.compile(r"(nodes=)([^\s:,]+(?::ppn=\d+)?)", re.I)


def _resolve_workdir(db, run) -> Tuple[Optional[str], Optional[str]]:
    """确定作业的工作目录。返回 (workdir, 错误信息)。

    取自仿真项目的 workdir——项目是资源边界，工况共享同一个根目录。
    """
    pid = run["sim_project_id"]
    if not pid:
        return None, "该运行未绑定仿真项目，无法确定工作目录"
    proj = db.get_project(pid)
    if proj is None:
        return None, "仿真项目不存在"
    if not proj["workdir"]:
        return None, "仿真项目未设置工作目录（workdir），无法提交"
    return proj["workdir"], None


def submit_from_node(ctx, jobs_db, templates_db, settings) -> Tuple[Optional[str], Optional[str]]:
    """由 hpc.submit 节点发起一次提交。

    返回 (external_ref, 错误信息)：成功给句柄，失败给可读原因。

    步骤与 /jobs/submit 一致，只是输入来自编排上下文而非请求体：
    落盘输入卡 → 取脚本 → 注入核数 → 入排队 → 触发准入调度。
    """
    db = ctx.db
    run = db.get_run(ctx.run_id)
    params = ctx.params or {}

    # 先做廉价校验再导入重模块：配置类错误不该依赖文件系统/提交链路能否加载，
    # 报错也更快更准。
    workdir, err = _resolve_workdir(db, run)
    if err:
        return None, err
    if not params.get("script") and not params.get("template_id"):
        return None, "未提供 script，也未指定 template_id，无法生成提交脚本"

    from ..fs.browser import FsError, write_file
    from ..submit.router import inject_ppn, render_template
    from ..submit.scheduler import get_scheduler

    exec_user = ctx.owner
    roots = [r.strip().rstrip("/") for r in settings.fs_roots.split(":") if r.strip()]

    # 1) 上游 export_deck 的产出落盘。没有 deck_text 也允许——
    #    有些流程直接用工作目录里已有的输入文件。
    #
    # 文件名附带运行号后缀（input.k → input_a1b2c3d4.k）。write_file 用 O_EXCL
    # 拒绝覆盖（有意的防误写设计，不绕过），不加后缀则重跑必然失败。仍写在
    # workdir 根下而非各自子目录：求解器输入卡常按相对路径引用网格与 include，
    # 挪进子目录会把这些引用打断。
    deck_text = ctx.inputs.get("deck_text")
    deck_name = (params.get("deck_filename") or "").strip() or "input.k"
    if deck_text:
        stem, ext = os.path.splitext(deck_name)
        deck_name = f"{stem}_{ctx.run_id[:8]}{ext}"
        try:
            write_file(exec_user, workdir, deck_name,
                       str(deck_text).encode("utf-8"), roots)
        except FsError as e:
            return None, f"写入输入卡失败: {e.message}"

    # 2) 取脚本：节点参数里的脚本优先，否则用提交模板渲染
    script = params.get("script")
    if not script:
        tid = params.get("template_id")
        try:
            t = templates_db.get(int(tid))
        except (TypeError, ValueError):
            return None, f"template_id 不是合法整数: {tid!r}"
        if not t:
            return None, f"提交模板不存在: {tid}"
        script = render_template(t["content"], workdir, deck_name)

    # 3) 注入核数
    cores = int(params.get("cores") or 0)
    extra_l = None
    if cores > 0:
        if _NODES_RE.search(script):
            script = inject_ppn(script, cores)
        else:
            extra_l = f"nodes=1:ppn={cores}"

    # 4) 以执行身份写提交脚本
    jobname = re.sub(r"[^A-Za-z0-9_-]", "_", f"sdm_{ctx.node_id}")[:60] or "sdm_job"
    if not jobname[0].isalpha():
        jobname = "j" + jobname
    fname = f".sdm_{jobname}_{uuid.uuid4().hex[:8]}.pbs"
    try:
        write_file(exec_user, workdir, fname, script.encode("utf-8"), roots)
    except FsError as e:
        return None, f"写入提交脚本失败: {e.message}"
    script_path = os.path.join(workdir, fname)

    # 5) 核数记账：与 /jobs/submit 同口径
    if cores > 0:
        est_cores = cores
    else:
        from ..pbs.cores import cores_from_nodes

        m = _NODES_RE.search(script)
        est_cores = cores_from_nodes(m.group(2)) if m else 1

    queue_name = (params.get("queue") or "").strip() or settings.submit_default_queue or "batch"
    qid = jobs_db.sq_enqueue(exec_user, jobname, queue_name, script_path,
                             workdir, est_cores, extra_l)

    # 6) 立即触发一次准入调度：配额与核数充裕时会在本次调用内直接 qsub 完成
    sched = get_scheduler()
    if sched is not None:
        try:
            sched.tick()
        except Exception:  # noqa: BLE001
            log.exception("编排提交后触发准入调度失败")

    row = jobs_db.sq_get(qid)
    if row["status"] == "failed":
        return None, f"qsub 失败: {row['msg'] or '未知错误'}"
    if row["status"] == "submitted" and row["jobid"]:
        return row["jobid"], None
    # 仍在本地排队（受配额或核数余量限制）：先用队列句柄追踪
    return f"{SQ_PREFIX}{qid}", None


def _promote_queue_refs(db, jobs_db) -> int:
    """把已经 qsub 成功的 sq: 句柄替换成真实作业号。

    准入调度是异步的，节点挂起时可能还在排队；这一步让它们在真正提交后
    重新可追踪。返回本轮升级的节点数。
    """
    promoted = 0
    for node in db.list_waiting_nodes(category_prefix="hpc."):
        ref = node["external_ref"] or ""
        if not ref.startswith(SQ_PREFIX):
            continue
        try:
            qid = int(ref[len(SQ_PREFIX):])
        except ValueError:
            continue
        row = jobs_db.sq_get(qid)
        if row is None:
            continue
        if row["status"] == "submitted" and row["jobid"]:
            db.set_node_waiting(node["pipeline_run_id"], node["node_id"],
                                row["jobid"], node["wait_hint"] or "")
            promoted += 1
        elif row["status"] in ("failed", "cancelled"):
            from .engine import get_engine

            eng = get_engine()
            if eng is not None:
                try:
                    eng.complete_node(
                        node["pipeline_run_id"], node["node_id"],
                        error=f"提交未成功({row['status']}): {row['msg'] or '无说明'}",
                    )
                except Exception:  # noqa: BLE001
                    log.exception("排队失败回流异常 node=%s", node["node_id"])
    return promoted


def on_jobs_finished(db, jobs_db, finished_jobids: List[str]) -> int:
    """作业结束时推进对应的编排节点。由作业轮询器在每轮采集后调用。

    退出码非 0 视为失败——让整条流水线停在这里，而不是拿着失败的结果继续往下走。
    返回推进的节点数。
    """
    from .engine import get_engine

    eng = get_engine()
    if eng is None:
        return 0

    # 先把排队态句柄升级，避免刚 qsub 就完成的作业错过匹配
    try:
        _promote_queue_refs(db, jobs_db)
    except Exception:  # noqa: BLE001
        log.exception("升级排队句柄失败")

    advanced = 0
    for jobid in finished_jobids or []:
        node = db.find_waiting_node_by_ref(jobid)
        if node is None:
            continue
        job = jobs_db.get(jobid)
        exit_status = job["exit_status"] if job is not None else None
        try:
            if exit_status not in (None, 0):
                eng.complete_node(node["pipeline_run_id"], node["node_id"],
                                  error=f"作业 {jobid} 非正常结束，退出码 {exit_status}")
            else:
                eng.complete_node(node["pipeline_run_id"], node["node_id"],
                                  outputs={"hpc_jobid": jobid,
                                           "workdir": job["workdir"] if job else None})
            advanced += 1
            log.info("编排节点随作业完成推进 job=%s node=%s", jobid, node["node_id"])
        except Exception:  # noqa: BLE001
            log.exception("作业完成回流失败 job=%s node=%s", jobid, node["node_id"])
    return advanced


def tick_queue_refs(db, jobs_db) -> int:
    """兜底：单独升级排队句柄。

    作业可能在两次轮询之间既被 qsub 又已结束，此时 finished 列表里的作业号
    还没来得及匹配上节点。由轮询器每轮调用一次，与 on_jobs_finished 互补。
    """
    try:
        return _promote_queue_refs(db, jobs_db)
    except Exception:  # noqa: BLE001
        log.exception("兜底升级排队句柄失败")
        return 0
