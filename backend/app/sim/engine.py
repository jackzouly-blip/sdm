"""DAG 编排引擎。

设计取舍见 docs/sdm-architecture.md 第 4 节。一句话：SDM 是**编排者不是执行者**，
绝大多数节点的实际执行都在进程外（PBS 集群、用户桌面的 vektor3d），所以引擎的
核心不是"跑任务"，而是"依赖满足时派发、外部完成时推进"。

推进方式与现有 submit.scheduler 一致：**事件驱动 + 兜底轮询**。事件有三类——
建 run、节点完成、外部回流；兜底 tick 只防漏，不承担主要推进职责。

节点执行在调用线程内同步完成（内置节点都是纯计算，毫秒级），需要等外部的节点
立即返回 Waiting 而不占线程。因此引擎不需要自己的线程池。
"""
from __future__ import annotations

import json
import threading
from typing import Dict, List, Optional, Set, Tuple

from ..logger import get_logger
from .nodes import Done, Failed, NodeContext, Waiting, get_node_type
from .pipeline_store import (
    NODE_DONE,
    NODE_FAILED,
    NODE_PENDING,
    NODE_SKIPPED,
    NODE_TERMINAL,
    NODE_WAITING,
    RUN_DONE,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_TERMINAL,
    RUN_WAITING,
)

log = get_logger(__name__)


class DagError(ValueError):
    """DAG 文档不合法。消息面向用户，直接透传到接口。"""


# --- 文档校验 -----------------------------------------------------------

def validate_doc(doc: Dict) -> None:
    """校验 DAG 文档：节点类型已注册、id 唯一、边端点存在、无环。

    在保存定义与启动运行时都要调用——保存时拦下明显错误，启动时防止
    "定义保存后节点类型被下线"这类漂移。
    """
    if not isinstance(doc, dict):
        raise DagError("DAG 文档必须是对象")
    nodes = doc.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise DagError("DAG 至少要有一个节点")
    edges = doc.get("edges") or []
    if not isinstance(edges, list):
        raise DagError("edges 必须是数组")

    seen: Set[str] = set()
    for n in nodes:
        if not isinstance(n, dict) or not n.get("id") or not n.get("type"):
            raise DagError("每个节点必须有 id 与 type")
        if n["id"] in seen:
            raise DagError(f"节点 id 重复: {n['id']}")
        seen.add(n["id"])
        if get_node_type(n["type"]) is None:
            raise DagError(f"未注册的节点类型: {n['type']}")

    for e in edges:
        if not isinstance(e, dict) or "from" not in e or "to" not in e:
            raise DagError("每条边必须有 from 与 to")
        if e["from"] not in seen:
            raise DagError(f"边的起点不存在: {e['from']}")
        if e["to"] not in seen:
            raise DagError(f"边的终点不存在: {e['to']}")
        if e["from"] == e["to"]:
            raise DagError(f"节点不能指向自己: {e['from']}")

    _ensure_acyclic(seen, edges)


def _ensure_acyclic(node_ids: Set[str], edges: List[Dict]) -> None:
    """Kahn 拓扑排序判环；有环则报出参与成环的节点，便于在画布上定位。"""
    indeg = {n: 0 for n in node_ids}
    adj: Dict[str, List[str]] = {n: [] for n in node_ids}
    for e in edges:
        adj[e["from"]].append(e["to"])
        indeg[e["to"]] += 1

    queue = [n for n, d in indeg.items() if d == 0]
    visited = 0
    while queue:
        cur = queue.pop()
        visited += 1
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)

    if visited != len(node_ids):
        stuck = sorted(n for n, d in indeg.items() if d > 0)
        raise DagError(f"DAG 存在环，涉及节点: {', '.join(stuck)}")


def _deps_of(doc: Dict) -> Dict[str, List[str]]:
    deps: Dict[str, List[str]] = {n["id"]: [] for n in doc.get("nodes", [])}
    for e in doc.get("edges", []) or []:
        deps[e["to"]].append(e["from"])
    return deps


def _node_spec(doc: Dict, node_id: str) -> Optional[Dict]:
    for n in doc.get("nodes", []):
        if n["id"] == node_id:
            return n
    return None


# --- 引擎 ---------------------------------------------------------------

class PipelineEngine:
    """编排引擎。无自有线程：推进由事件触发，兜底轮询由 streamer 式的调用方驱动。"""

    def __init__(self, db, jobs_db=None, templates_db=None, settings=None,
                 task_manager=None):
        self.db = db
        # 需要触达现有链路的节点用这几项；测试里可为 None，
        # 相关节点会报明确错误而非崩溃。
        self.jobs_db = jobs_db
        self.templates_db = templates_db
        self.settings = settings
        self.task_manager = task_manager
        # 同一 run 的推进串行化，避免并发事件把同一节点派发两次
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _run_lock(self, run_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(run_id, threading.Lock())

    # --- 启动 -----------------------------------------------------------

    def start_run(
        self,
        pipeline_def_id: str,
        owner: str,
        sim_project_id: Optional[str] = None,
        sim_subject_id: Optional[str] = None,
    ) -> str:
        d = self.db.get_pipeline_def(pipeline_def_id)
        if d is None:
            raise DagError("编排定义不存在")
        doc = json.loads(d["doc_json"])
        validate_doc(doc)  # 防定义保存后节点类型下线导致的漂移
        rid = self.db.create_run(
            pipeline_def_id, d["version"], doc, owner,
            sim_project_id, sim_subject_id,
        )
        log.info("编排运行已启动 run=%s def=%s owner=%s", rid, pipeline_def_id, owner)
        self.advance(rid)
        return rid

    # --- 推进 -----------------------------------------------------------

    def advance(self, run_id: str) -> None:
        """推进一个 run：把依赖已满足的 pending 节点派发出去，并结算 run 状态。

        循环直到没有可派发的节点——内置节点是同步完成的，一次调用可能连推数级。
        """
        with self._run_lock(run_id):
            run = self.db.get_run(run_id)
            if run is None or run["status"] in RUN_TERMINAL:
                return
            doc = json.loads(run["doc_snapshot_json"])
            deps = _deps_of(doc)

            while True:
                states = {n["node_id"]: n for n in self.db.list_node_runs(run_id)}

                # 任一节点失败：跳过未启动的，整个 run 失败
                failed = [n for n in states.values() if n["status"] == NODE_FAILED]
                if failed:
                    self.db.skip_pending_nodes(run_id)
                    self.db.set_run_status(
                        run_id, RUN_FAILED,
                        f"节点 {failed[0]['node_id']} 失败: {failed[0]['error_message']}",
                    )
                    log.info("编排运行失败 run=%s node=%s", run_id, failed[0]["node_id"])
                    return

                ready = [
                    nid for nid, st in states.items()
                    if st["status"] == NODE_PENDING
                    and all(states[d]["status"] == NODE_DONE for d in deps.get(nid, []))
                ]
                if not ready:
                    break
                for nid in ready:
                    self._dispatch(run, doc, deps, states, nid)

            self._settle(run_id)

    def _dispatch(self, run, doc: Dict, deps, states, node_id: str) -> None:
        """派发单个节点：算入参 → 执行 → 落状态。"""
        run_id = run["id"]
        spec = _node_spec(doc, node_id)
        if spec is None:
            self.db.set_node_failed(run_id, node_id, "节点在文档快照中不存在")
            return
        nt = get_node_type(spec["type"])
        if nt is None:
            self.db.set_node_failed(run_id, node_id, f"未注册的节点类型: {spec['type']}")
            return

        # 入参 = 全部上游节点 outputs 的合并（后者覆盖前者，按依赖声明顺序）
        inputs: Dict = {}
        for dep in deps.get(node_id, []):
            raw = states[dep]["outputs_json"]
            if raw:
                try:
                    inputs.update(json.loads(raw))
                except ValueError:
                    pass

        self.db.set_node_running(run_id, node_id, inputs)
        ctx = NodeContext(
            run_id=run_id,
            node_id=node_id,
            params=spec.get("params") or {},
            inputs=inputs,
            owner=run["owner"],
            sim_project_id=run["sim_project_id"],
            sim_subject_id=run["sim_subject_id"],
            db=self.db,
            jobs_db=self.jobs_db,
            templates_db=self.templates_db,
            settings=self.settings,
            task_manager=self.task_manager,
        )
        try:
            outcome = nt.executor(ctx)
        except Exception as e:  # noqa: BLE001
            log.exception("节点执行抛异常 run=%s node=%s", run_id, node_id)
            self.db.set_node_failed(run_id, node_id, f"执行异常: {e}")
            return

        if isinstance(outcome, Done):
            self.db.set_node_done(run_id, node_id, outcome.outputs or {})
        elif isinstance(outcome, Waiting):
            self.db.set_node_waiting(run_id, node_id, outcome.ref, outcome.hint)
        elif isinstance(outcome, Failed):
            self.db.set_node_failed(run_id, node_id, outcome.message)
        else:
            self.db.set_node_failed(
                run_id, node_id, f"执行器返回了未知结果类型: {type(outcome).__name__}"
            )

    def _settle(self, run_id: str) -> None:
        """结算 run 状态：全部终态则 done；有等待中的则 waiting；否则仍 running。"""
        states = self.db.list_node_runs(run_id)
        if not states:
            return
        if all(s["status"] in NODE_TERMINAL for s in states):
            if any(s["status"] == NODE_FAILED for s in states):
                return  # 失败已在 advance 里处理
            self.db.set_run_status(run_id, RUN_DONE)
            log.info("编排运行完成 run=%s", run_id)
            return
        if any(s["status"] == NODE_WAITING for s in states):
            self.db.set_run_status(run_id, RUN_WAITING)
        else:
            self.db.set_run_status(run_id, RUN_RUNNING)

    # --- 外部回流 -------------------------------------------------------

    def complete_node(
        self,
        run_id: str,
        node_id: str,
        outputs: Optional[Dict] = None,
        error: Optional[str] = None,
    ) -> None:
        """外部完成一个等待中的节点。

        HPC 作业结束、浏览器代理调完 vektor3d 能力、人工确认——三者都走这里。
        只接受 waiting 状态的节点，避免重复回流把已完成的节点改回去。
        """
        node = self.db.get_node_run(run_id, node_id)
        if node is None:
            raise DagError("节点不存在")
        if node["status"] != NODE_WAITING:
            raise DagError(f"节点当前状态为 {node['status']}，不是等待中，无法回流")
        if error:
            self.db.set_node_failed(run_id, node_id, error)
        else:
            self.db.set_node_done(run_id, node_id, outputs or {})
        self.advance(run_id)

    def claim_node(self, run_id: str, node_id: str, external_ref: str) -> None:
        """给等待中的节点登记外部句柄（如 HPC 作业号、能力作业号）。"""
        node = self.db.get_node_run(run_id, node_id)
        if node is None or node["status"] != NODE_WAITING:
            raise DagError("节点不存在或不在等待中")
        self.db.set_node_waiting(run_id, node_id, external_ref, node["wait_hint"] or "")

    def cancel_run(self, run_id: str) -> None:
        run = self.db.get_run(run_id)
        if run is None or run["status"] in RUN_TERMINAL:
            return
        self.db.skip_pending_nodes(run_id)
        self.db.set_run_status(run_id, "canceled", "用户取消")

    # --- 兜底轮询 -------------------------------------------------------

    def tick(self) -> int:
        """兜底：把所有未终结的 run 推一遍，防止事件漏掉。返回推进的 run 数。"""
        runs = self.db.list_runs(active_only=True)
        for r in runs:
            try:
                self.advance(r["id"])
            except Exception:  # noqa: BLE001
                log.exception("兜底推进失败 run=%s", r["id"])
        return len(runs)


_engine: Optional[PipelineEngine] = None


def set_engine(e: PipelineEngine) -> None:
    global _engine
    _engine = e


def get_engine() -> Optional[PipelineEngine]:
    return _engine
