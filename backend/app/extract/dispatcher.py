"""提取派发器：把"刚完成的任务"与提取规则撮合，派发为异步提取任务。

被轮询器在每轮采集后调用；也供路由层手动对单个任务补派发。
"""
from __future__ import annotations

from typing import List, Optional

from ..db.jobs_db import EXTRACT_DISPATCHED, EXTRACT_SKIPPED, JobsDB
from ..logger import get_logger
from ..tasks.manager import TaskManager
from .rules_db import RulesDB

# 导入以触发 register_task("job_extract")
from . import service  # noqa: F401

log = get_logger(__name__)


class Dispatcher:
    def __init__(self, jobs_db: JobsDB, rules_db: RulesDB, tm: TaskManager):
        self.jobs_db = jobs_db
        self.rules_db = rules_db
        self.tm = tm

    def dispatch_many(self, jobids: List[str]) -> int:
        """对一批刚完成的任务派发提取，返回提交的提取任务数。"""
        total = 0
        for jobid in jobids:
            total += self.dispatch_one(jobid)
        return total

    def dispatch_one(self, jobid: str, *, force: bool = False) -> int:
        """对单个任务匹配规则并派发。force=True 时忽略已派发状态（手动补跑）。"""
        job = self.jobs_db.get(jobid)
        if job is None:
            log.warning("派发提取：任务不存在 %s", jobid)
            return 0
        rules = self.rules_db.match(job)
        if not rules:
            self.jobs_db.set_extract_state(jobid, EXTRACT_SKIPPED)
            log.info("任务 %s 完成但无匹配提取规则", jobid)
            return 0

        count = 0
        for r in rules:
            params = {
                "jobid": job["jobid"],
                "short_id": job["short_id"],
                "name": job["name"] or "",
                "owner": job["owner"],
                "queue": job["queue"] or "",
                "workdir": job["workdir"] or "",
                "rule_id": r["id"],
                "rule_name": r["name"],
                "command": r["command"],
            }
            self.tm.submit("job_extract", owner=job["owner"], params=params)
            count += 1
        self.jobs_db.set_extract_state(jobid, EXTRACT_DISPATCHED)
        log.info("任务 %s 派发 %d 条提取规则", jobid, count)
        return count

    def recover_pending(self) -> int:
        """服务重启后，对仍处 pending 的已完成任务补派发一次。"""
        pending = self.jobs_db.list_pending_extract()
        if not pending:
            return 0
        log.info("发现 %d 个待提取任务，补派发", len(pending))
        return self.dispatch_many([r["jobid"] for r in pending])


_dispatcher: Optional[Dispatcher] = None


def set_dispatcher(d: Dispatcher) -> None:
    global _dispatcher
    _dispatcher = d


def get_dispatcher() -> Optional[Dispatcher]:
    return _dispatcher
