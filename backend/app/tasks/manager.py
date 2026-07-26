"""异步任务管理器：进程内线程池执行长任务 + SQLite 持久化 + 进度推送。

v1 不引入 Redis，用 ThreadPoolExecutor + SQLite。任务在后台线程跑（打包、
上传都是阻塞 IO），进度通过回调写库并广播给订阅的 WebSocket。

任务状态：queued -> running -> success / failed
进程重启后，未完成（queued/running）的任务标记为 interrupted（不自动重启，
由用户决定是否重新发起，避免重复上传带来的副作用）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..logger import get_logger

log = get_logger(__name__)

QUEUED = "queued"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"
INTERRUPTED = "interrupted"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS async_tasks (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    owner       TEXT NOT NULL,
    params      TEXT NOT NULL,    -- JSON 入参
    status      TEXT NOT NULL,
    phase       TEXT,             -- 当前阶段文字（打包中/上传中/分享中）
    progress    REAL NOT NULL,    -- 0~100
    result      TEXT,             -- JSON 结果（含分享链接）
    error       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON async_tasks(owner);
"""


class TaskHandle:
    """传给任务函数的句柄，用于更新阶段/进度。"""

    def __init__(self, manager: "TaskManager", task_id: str, owner: str):
        self.manager = manager
        self.id = task_id
        self.owner = owner

    def update(self, phase: Optional[str] = None, progress: Optional[float] = None) -> None:
        self.manager._update_progress(self.id, phase, progress)


# 任务类型 -> 执行函数的注册表。函数签名: fn(handle, params) -> dict(result)
_REGISTRY: Dict[str, Callable] = {}


def register_task(task_type: str):
    def deco(fn: Callable) -> Callable:
        _REGISTRY[task_type] = fn
        return fn

    return deco


class TaskManager:
    # 独占线程池的任务类型 -> worker 数。
    # 网盘上传单文件耗时以十分钟计（GB 级），与提取/打包共用默认池时会把两个
    # worker 长期占满，导致其它任务全堵在队列里。给它单独一个池彼此隔离。
    _DEDICATED_POOLS = {"netdisk_autoshare": 2}

    def __init__(self, db_path: str, max_workers: int = 2):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="task")
        self._pools = {
            t: ThreadPoolExecutor(max_workers=n, thread_name_prefix=f"task-{t}")
            for t, n in self._DEDICATED_POOLS.items()
        }
        # 进度订阅者：task_id -> set(回调)。回调签名 cb(snapshot: dict)
        self._subscribers: Dict[str, set] = {}
        self._reap_interrupted()

    def close(self) -> None:
        self.executor.shutdown(wait=False)
        for ex in self._pools.values():
            ex.shutdown(wait=False)
        self.conn.close()

    # --- 提交 / 执行 ---------------------------------------------------

    def submit(self, task_type: str, owner: str, params: dict) -> str:
        if task_type not in _REGISTRY:
            raise ValueError(f"未注册的任务类型: {task_type}")
        task_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self.conn.execute(
                """INSERT INTO async_tasks
                   (id, type, owner, params, status, phase, progress, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (task_id, task_type, owner, json.dumps(params), QUEUED, "排队中", 0.0, now, now),
            )
            self.conn.commit()
        self._pools.get(task_type, self.executor).submit(
            self._run, task_id, task_type, owner, params
        )
        log.info("任务已提交 id=%s type=%s owner=%s", task_id, task_type, owner)
        return task_id

    def _run(self, task_id: str, task_type: str, owner: str, params: dict) -> None:
        self._set_status(task_id, RUNNING, phase="开始")
        handle = TaskHandle(self, task_id, owner)
        try:
            result = _REGISTRY[task_type](handle, params)
            self._finish(task_id, SUCCESS, result=result)
            log.info("任务完成 id=%s", task_id)
        except Exception as e:  # noqa: BLE001
            self._finish(task_id, FAILED, error=str(e))
            log.exception("任务失败 id=%s", task_id)

    # --- 状态写入 + 广播 -----------------------------------------------

    def _set_status(self, task_id: str, status: str, phase: Optional[str] = None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE async_tasks SET status=?, phase=COALESCE(?,phase), updated_at=? WHERE id=?",
                (status, phase, time.time(), task_id),
            )
            self.conn.commit()
        self._broadcast(task_id)

    def _update_progress(self, task_id: str, phase: Optional[str], progress: Optional[float]) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE async_tasks SET phase=COALESCE(?,phase), progress=COALESCE(?,progress), updated_at=? WHERE id=?",
                (phase, progress, time.time(), task_id),
            )
            self.conn.commit()
        self._broadcast(task_id)

    def _finish(self, task_id: str, status: str, result: Optional[dict] = None, error: Optional[str] = None) -> None:
        with self._lock:
            self.conn.execute(
                """UPDATE async_tasks SET status=?, progress=?, result=?, error=?, updated_at=?
                   WHERE id=?""",
                (
                    status,
                    100.0 if status == SUCCESS else self._get_progress(task_id),
                    json.dumps(result, ensure_ascii=False) if result else None,
                    error,
                    time.time(),
                    task_id,
                ),
            )
            self.conn.commit()
        self._broadcast(task_id)

    def _get_progress(self, task_id: str) -> float:
        row = self.conn.execute("SELECT progress FROM async_tasks WHERE id=?", (task_id,)).fetchone()
        return row["progress"] if row else 0.0

    def _reap_interrupted(self) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE async_tasks SET status=?, error=? WHERE status IN (?,?)",
                (INTERRUPTED, "服务重启导致中断", QUEUED, RUNNING),
            )
            self.conn.commit()

    # --- 查询 -----------------------------------------------------------

    def get(self, task_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute("SELECT * FROM async_tasks WHERE id=?", (task_id,)).fetchone()

    def snapshot(self, task_id: str) -> Optional[dict]:
        row = self.get(task_id)
        if not row:
            return None
        return {
            "id": row["id"],
            "type": row["type"],
            "owner": row["owner"],
            "status": row["status"],
            "phase": row["phase"],
            "progress": row["progress"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_by_owner(self, owner: str, limit: int = 100) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM async_tasks WHERE owner=? ORDER BY created_at DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
        return [self.snapshot(r["id"]) for r in rows]

    # --- 进度订阅（供 WebSocket）---------------------------------------

    def subscribe(self, task_id: str, callback: Callable[[dict], None]) -> None:
        with self._lock:
            self._subscribers.setdefault(task_id, set()).add(callback)

    def unsubscribe(self, task_id: str, callback: Callable[[dict], None]) -> None:
        with self._lock:
            subs = self._subscribers.get(task_id)
            if subs:
                subs.discard(callback)

    def _broadcast(self, task_id: str) -> None:
        snap = self.snapshot(task_id)
        if snap is None:
            return
        with self._lock:
            subs = list(self._subscribers.get(task_id, ()))
        for cb in subs:
            try:
                cb(snap)
            except Exception:  # noqa: BLE001
                log.debug("进度回调失败", exc_info=True)
