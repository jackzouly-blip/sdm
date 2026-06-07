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
        self.last_ok_ts = time.time()
        self.last_error = None
        return len(jobs)

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
