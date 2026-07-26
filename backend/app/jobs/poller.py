"""任务轮询器：后台周期性 qstat 采集并写入数据库。

任务从 qstat 消失即由 JobsDB 标记为 done，从而保留完成任务的历史。
"""
from __future__ import annotations

import threading
import time

from ..db.jobs_db import JobsDB
from ..logger import get_logger
from ..pbs.qstat import QstatError, fetch_jobs

log = get_logger(__name__)


class JobPoller:
    def __init__(self, db: JobsDB, interval: int = 15):
        self.db = db
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_ok_ts: float | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="job-poller", daemon=True
        )
        self._thread.start()
        log.info("任务轮询器已启动，间隔 %ds", self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def poll_once(self) -> int:
        """执行一次采集，返回活跃任务数。

        采集后把"本轮刚完成"的任务交给提取派发器（若已装配）。
        """
        jobs = fetch_jobs()
        finished = self.db.upsert_active(jobs)
        if finished:
            from ..extract.dispatcher import get_dispatcher

            d = get_dispatcher()
            if d is not None:
                try:
                    d.dispatch_many(finished)
                except Exception:  # noqa: BLE001
                    log.exception("提取派发失败")
        # 编排回流：作业结束推进对应的 hpc.submit 节点。
        # 即便本轮无完成作业也要调一次——排队中的节点需要把 sq: 句柄升级成
        # 真实作业号，否则"提交后很快结束"的作业会错过匹配。
        self._advance_pipelines(finished)

        # 任务结束会释放核数/用户配额名额，立刻触发一次准入调度补位，
        # 不必等调度器自己的兜底轮询间隔。
        from ..submit.scheduler import get_scheduler

        sched = get_scheduler()
        if sched is not None:
            try:
                sched.tick()
            except Exception:  # noqa: BLE001
                log.exception("准入调度触发失败")
        self.last_ok_ts = time.time()
        self.last_error = None
        return len(jobs)

    def _advance_pipelines(self, finished: list) -> None:
        """把作业完成事件转给编排引擎。

        编排是可选子系统：未装配或出错时只记日志，绝不影响作业轮询本身。
        """
        try:
            from ..sim.engine import get_engine
            from ..sim.hpc_bridge import on_jobs_finished, tick_queue_refs

            eng = get_engine()
            if eng is None:
                return
            if finished:
                on_jobs_finished(eng.db, self.db, finished)
            else:
                tick_queue_refs(eng.db, self.db)
        except Exception:  # noqa: BLE001
            log.exception("编排回流失败")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                n = self.poll_once()
                log.debug("轮询完成，活跃任务 %d", n)
            except QstatError as e:
                self.last_error = str(e)
                log.warning("轮询失败: %s", e)
            except Exception as e:  # noqa: BLE001
                self.last_error = repr(e)
                log.exception("轮询异常")
            self._stop.wait(self.interval)
