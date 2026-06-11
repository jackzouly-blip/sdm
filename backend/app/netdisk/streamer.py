"""网盘流式上传器：运行中任务的 d3plot 边算边传。

设计：扫描与上传**解耦**，各自独立线程，互不阻塞。
  扫描线程（每 interval 秒）：遍历白名单用户的 active 任务工作目录，
    用"两次扫描尺寸不变 + 排除活跃成员"判出可传的 d3plot，写入【上传队列表】。
    这一步只记账、很快，永远不会被上传卡住——这正是把入队和上传分开的目的。
  上传线程：不断从队列取最早一条，确保该任务网盘目录已建+已共享（首个文件落地后
    建链接，后续复用），然后上传该文件。顺序/速度非首要，单线程消费即可。

为何不走 TaskManager：其仅 2 个工作线程，供提取/手动上传共用；流式上传耗时
（分钟级），独立线程与任务池隔离，避免互相阻塞。任务结束后的最终补传由提取完成
钩子 maybe_auto_share 负责，从 partial 接管并置 done，本器的条件写入自动让位。
"""
from __future__ import annotations

import os
import threading

from ..db.jobs_db import JobsDB
from ..logger import get_logger

log = get_logger(__name__)


class NetdiskStreamer:
    def __init__(self, jobs_db: JobsDB, interval: int = 60, stable_seconds: int = 120):
        self.db = jobs_db
        self.interval = max(15, interval)
        self.stable_seconds = max(30, stable_seconds)  # 保留参数（语义兼容）
        self._stop = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._upload_thread: threading.Thread | None = None
        # {jobid: {abs_path: size}} 上一轮 d3plot 尺寸快照，用于"两次相同"判定
        self._sizes: dict[str, dict[str, int]] = {}
        # {jobid: 自动重试次数} 失败最终上传的看门狗计数
        self._retry_attempts: dict[str, int] = {}

    def start(self) -> None:
        if self._scan_thread and self._scan_thread.is_alive():
            return
        self._stop.clear()
        self._scan_thread = threading.Thread(
            target=self._scan_loop, name="netdisk-scan", daemon=True
        )
        self._upload_thread = threading.Thread(
            target=self._upload_loop, name="netdisk-upload", daemon=True
        )
        self._scan_thread.start()
        self._upload_thread.start()
        log.info("网盘流式上传器已启动：扫描间隔 %ds（入队/上传双线程解耦）", self.interval)

    def stop(self) -> None:
        self._stop.set()
        for t in (self._scan_thread, self._upload_thread):
            if t:
                t.join(timeout=5)

    # ---- 扫描线程：发现可传文件 → 入队 -------------------------------

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick_scan()
            except Exception:  # noqa: BLE001
                log.exception("网盘扫描轮询异常")
            try:
                self.tick_retry()
            except Exception:  # noqa: BLE001
                log.exception("网盘失败重试轮询异常")
            self._stop.wait(self.interval)

    def tick_retry(self) -> int:
        """看门狗一轮：自动重试 failed 状态的最终上传。返回派发的重试数。"""
        from ..config import get_settings
        from .autoshare import retry_failed_uploads

        s = get_settings()
        if not s.netdisk_stream_enabled or not s.netdisk_auto_user_list:
            return 0
        return retry_failed_uploads(self._retry_attempts)

    def tick_scan(self) -> int:
        """扫描一轮白名单用户的 active 任务，返回本轮新入队的文件数。"""
        from ..config import get_settings
        from .autoshare import scan_uploadable_d3plot

        s = get_settings()
        if not s.netdisk_stream_enabled:
            return 0
        owners = s.netdisk_auto_user_list
        if not owners:
            return 0

        enqueued = 0
        for owner in owners:
            try:
                rows = self.db.list_by_owner(owner, state="active")
            except Exception:  # noqa: BLE001
                log.exception("列出 %s 的活跃任务失败", owner)
                continue
            for row in rows:
                if self._stop.is_set():
                    return enqueued
                jobid = row["jobid"]
                state = (row["netdisk_state"] if "netdisk_state" in row.keys() else None) or "none"
                # 已被最终补传接管（pending/uploading/done）的任务不再流式入队
                if state not in ("none", "partial"):
                    continue
                workdir = s.map_path(row["workdir"]) if row["workdir"] else ""
                if not workdir or not os.path.isdir(workdir):
                    continue
                prev = self._sizes.setdefault(jobid, {})
                try:
                    eligible = scan_uploadable_d3plot(workdir, prev)
                except Exception:  # noqa: BLE001
                    log.exception("扫描可传 d3plot 失败 jobid=%s", jobid)
                    continue
                if not eligible:
                    continue
                already = set(self._loaded_names(row))
                cnt = 0
                for p in eligible:
                    fn = os.path.basename(p)
                    if fn in already:
                        continue
                    if self.db.nq_enqueue(jobid, owner, fn, p):
                        cnt += 1
                if cnt:
                    enqueued += cnt
                    log.info("入队 job=%s owner=%s 新增 d3plot %d 个（待上传）",
                             jobid, owner, cnt)
        return enqueued

    @staticmethod
    def _loaded_names(row) -> list[str]:
        import json
        if "netdisk_files" in row.keys() and row["netdisk_files"]:
            try:
                return list(json.loads(row["netdisk_files"]))
            except Exception:  # noqa: BLE001
                return []
        return []

    # ---- 上传线程：消费队列 → 逐个上传 -------------------------------

    def _upload_loop(self) -> None:
        idle = min(self.interval, 15)
        while not self._stop.is_set():
            try:
                item = self.db.nq_claim_next()
            except Exception:  # noqa: BLE001
                log.exception("取上传队列失败")
                item = None
            if item is None:
                self._stop.wait(idle)  # 队列空，歇一会
                continue
            try:
                self._upload_item(item)
            except Exception:  # noqa: BLE001
                log.exception("上传队列项异常 id=%s", item["id"])
                try:
                    self.db.nq_mark(item["id"], "failed", "未捕获异常")
                except Exception:  # noqa: BLE001
                    pass

    def _upload_item(self, item) -> None:
        from ..config import get_settings
        from .autoshare import run_upload

        s = get_settings()
        jobid = item["jobid"]
        row = self.db.get(jobid)
        if row is None:
            self.db.nq_mark(item["id"], "failed", "任务不存在")
            return
        state = (row["netdisk_state"] if "netdisk_state" in row.keys() else None) or "none"
        # 已被最终补传接管/完成：该文件由最终全量上传覆盖，队列项直接收尾
        if state in ("pending", "uploading", "done"):
            self.db.nq_mark(item["id"], "done", "最终补传接管，跳过")
            return
        local = item["local_path"]
        if not os.path.isfile(local):
            self.db.nq_mark(item["id"], "failed", "本地文件已不存在")
            return
        workdir = s.map_path(row["workdir"]) if row["workdir"] else ""
        short_id = row["short_id"] or jobid
        task_name = row["name"] or short_id
        try:
            run_upload(
                jobid,
                workdir,
                row["owner"],
                task_name,
                short_id,
                s.netdisk_share_period,
                files=[local],       # 单文件：内部会确保目录建好+分享链接已生成
                finalize=False,
                handle=None,
            )
        except Exception as e:  # noqa: BLE001
            self.db.nq_mark(item["id"], "failed", str(e)[:300])
            log.exception("流式上传文件失败 job=%s file=%s", jobid, item["fname"])
            return
        self.db.nq_mark(item["id"], "done")
        log.info("已上传 job=%s file=%s", jobid, item["fname"])


_streamer: NetdiskStreamer | None = None


def set_streamer(st: NetdiskStreamer) -> None:
    global _streamer
    _streamer = st


def get_streamer() -> NetdiskStreamer | None:
    return _streamer
