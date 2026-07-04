"""提交准入调度器：本地排队 → 按用户配额与全局核数余量决定何时真正 qsub。

设计对齐 JobPoller / NetdiskStreamer：独立后台线程 + 显式 tick()，且 tick() 可被
外部（提交接口、轮询器"任务完成"回调）主动触发，做到核数一空出来就尽快补位，
而不是干等固定周期。

准入规则（每次 tick）：
  1. 从 jobs 表读出全局已用核数(pbs_state=R 的核数之和)与各用户当前活跃
     (derived_state=active，含 Q+R)任务数——覆盖门户之外直接 qsub 的任务。
  2. 取本地队列里 status=queued 的项，按 (优先级降序, 排队时间升序) 排序。
  3. 贪心准入：用户未超配额 且 全局核数余量够用 → qsub；否则跳过继续看下一项
     （不让一个大核数任务卡住后面能塞下的小任务）。
  4. 未配置策略的用户 = 不限并发、优先级0；cluster_total_cores<=0 = 不做全局
     核数网关，只按 per-user 配额本地排队——这是功能默认的休眠状态。
"""
from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from typing import Dict, Optional, Tuple

from ..config import get_settings
from ..db.jobs_db import JobsDB
from ..logger import get_logger
from ..privilege.actas import run_as_user

log = get_logger(__name__)


class SubmissionScheduler:
    def __init__(self, db: JobsDB, interval: int = 10):
        self.db = db
        self.interval = max(3, interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 串行化 tick：避免定时线程 / 提交请求内联触发 / 轮询完成回调并发跑，
        # 对同一份"全局核数余量"重复计算导致超发。
        self._tick_lock = threading.Lock()
        self.last_ok_ts: Optional[float] = None
        self.last_error: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="submission-scheduler", daemon=True
        )
        self._thread.start()
        log.info("提交准入调度器已启动，兜底间隔 %ds", self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_ok_ts = time.time()
                self.last_error = None
            except Exception as e:  # noqa: BLE001
                self.last_error = repr(e)
                log.exception("准入调度异常")
            self._stop.wait(self.interval)

    def tick(self) -> int:
        """执行一轮准入判定，返回本轮成功提交到 PBS 的任务数。"""
        with self._tick_lock:
            return self._tick_locked()

    def _policy_for(self, user: str) -> Tuple[Optional[int], int]:
        """(max_concurrent, priority)；未配置策略 = (不限, 优先级0)。"""
        row = self.db.policy_get(user)
        if row is None:
            return None, 0
        return row["max_concurrent"], row["priority"]

    def _tick_locked(self) -> int:
        candidates = self.db.sq_list(status="queued")
        if not candidates:
            return 0

        s = get_settings()
        used_cores = self.db.running_cores_total()
        active_counts = self.db.active_counts_by_owner()
        # 本轮已"预定"但尚未反映到 jobs 表(要等下一次 qstat 轮询)的用量。
        pending_counts: Dict[str, int] = {}

        policy_cache: Dict[str, Tuple[Optional[int], int]] = {}

        def policy(user: str) -> Tuple[Optional[int], int]:
            if user not in policy_cache:
                policy_cache[user] = self._policy_for(user)
            return policy_cache[user]

        ordered = sorted(
            candidates,
            key=lambda item: (-policy(item["owner"])[1], item["queued_at"]),
        )

        cores_budget = (
            None if s.cluster_total_cores <= 0
            else max(0, s.cluster_total_cores - used_cores)
        )
        admitted = 0
        for item in ordered:
            owner = item["owner"]
            cap, _prio = policy(owner)
            cur_count = active_counts.get(owner, 0) + pending_counts.get(owner, 0)
            if cap is not None and cur_count >= cap:
                continue
            if cores_budget is not None and item["cores"] > cores_budget:
                continue
            if self._admit(item):
                admitted += 1
                pending_counts[owner] = pending_counts.get(owner, 0) + 1
                if cores_budget is not None:
                    cores_budget -= item["cores"]
        return admitted

    def _admit(self, item: sqlite3.Row) -> bool:
        """把一个排队项真正提交到 PBS。"""
        item_id = item["id"]
        if not self.db.sq_claim_for_submit(item_id):
            return False  # 已被并发流程认领（理论上不会发生，多一层保险）
        qsub = shutil.which("qsub") or "/usr/local/torque-6.1.2/bin/qsub"
        argv = [qsub, "-N", item["jobname"], "-q", item["queue_name"]]
        if item["extra_l"]:
            argv += ["-l", item["extra_l"]]
        argv.append(item["script_path"])
        try:
            proc = run_as_user(item["owner"], argv, cwd=item["workdir"], timeout=60)
        except Exception as e:  # noqa: BLE001
            self.db.sq_mark_failed(item_id, f"qsub 执行异常: {e}")
            log.exception("准入调度提交失败 item=%s", item_id)
            return False
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()[:500]
            self.db.sq_mark_failed(item_id, err or "qsub 未知错误")
            log.warning("准入调度 qsub 失败 item=%s: %s", item_id, err)
            return False
        jobid = proc.stdout.decode("utf-8", "replace").strip()
        self.db.sq_mark_submitted(item_id, jobid)
        log.info(
            "准入调度已提交 item=%s -> jobid=%s (owner=%s)", item_id, jobid, item["owner"]
        )
        return True


_scheduler: Optional[SubmissionScheduler] = None


def set_scheduler(sched: SubmissionScheduler) -> None:
    global _scheduler
    _scheduler = sched


def get_scheduler() -> Optional[SubmissionScheduler]:
    return _scheduler
