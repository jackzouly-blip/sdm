"""入站同步的定时轮询线程。

按各分享源自己的 poll_interval 到期派发同步任务。轮询本身很轻（只查库），
真正的同步交给 TaskManager，所以这个线程永远不会被某个大文件卡住。

**节奏要克制**：转存走的是网页私有接口，高频调用有被风控的风险。因此
  - 间隔下限 60 秒，即便有人把 poll_interval 配成 5；
  - 每轮加随机抖动，避免多个源整点齐发形成尖峰。
"""
from __future__ import annotations

import random
import threading

from ..logger import get_logger

log = get_logger(__name__)


class NetdiskScanner:
    def __init__(self, sync_db, task_manager, interval: int = 60, jitter: int = 60):
        self.db = sync_db
        self.tm = task_manager
        self.interval = max(30, interval)
        self.jitter = max(0, jitter)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="netdisk-pull-scan", daemon=True
        )
        self._thread.start()
        log.info("网盘入站轮询已启动：检查间隔 %ds（抖动 ≤%ds）", self.interval, self.jitter)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("网盘入站轮询异常")
            self._stop.wait(self.interval + random.uniform(0, self.jitter))

    def tick(self) -> int:
        """派发一轮到期的同步任务，返回派发数。"""
        from ..config import get_settings

        s = get_settings()
        # 凭据取自库（管理页可随时改），env 仅作回退——不能只看 env
        if not s.netdisk_pull_enabled or not self.db.resolve_credentials()[0]:
            return 0

        count = 0
        for row in self.db.due_shares():
            if self._stop.is_set():
                break
            share_id = row["id"]
            # cookie 失效是全局故障，继续轮询只会刷屏——等运维换了凭据再说
            if row["link_state"] == "auth_failed":
                continue
            # 链接失效需要用户重新提交，自动重试没有意义
            if row["link_state"] == "invalid":
                continue
            if not self.db.try_begin_sync(share_id):
                continue  # 手动同步已抢先认领
            try:
                task_id = self.tm.submit(
                    "netdisk_pull", owner=row["owner"],
                    params={"share_id": share_id},
                )
            except Exception as e:  # noqa: BLE001
                self.db.end_sync(share_id, "failed", f"派发失败: {e}"[:300])
                log.exception("派发定时同步失败 share=%s", share_id)
                continue
            self.db.set_task(share_id, task_id)
            count += 1
            log.info("定时同步已派发: share=%s owner=%s", share_id, row["owner"])
        return count


_scanner: NetdiskScanner | None = None


def set_scanner(sc: NetdiskScanner) -> None:
    global _scanner
    _scanner = sc


def get_scanner() -> NetdiskScanner | None:
    return _scanner
