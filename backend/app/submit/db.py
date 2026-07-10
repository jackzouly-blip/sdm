"""作业提交模板库（SQLite）。

模板就是一段 PBS bash 脚本，含占位符 ###init_dir_path### / ###input_file_path###，
提交时替换；核数通过改写 #PBS -l nodes 行注入 ppn；作业名走 qsub -N。
模板由管理员维护。

kind 区分模板用途：
  - pbs（默认）：提交到 PBS 队列的作业脚本（现状）。
  - trial：试算模板，内容是一条在管理节点直接执行的命令行（同样支持占位符），
    不进 PBS，见 app/trial。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional

KIND_PBS = "pbs"
KIND_TRIAL = "trial"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    content    TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'pbs',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


class TemplatesDB:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self) -> None:
        """历史库增量迁移：补 kind 列（默认 pbs，与旧行为一致）。"""
        cols = {
            r["name"] for r in self.conn.execute("PRAGMA table_info(templates)").fetchall()
        }
        if "kind" not in cols:
            self.conn.execute(
                "ALTER TABLE templates ADD COLUMN kind TEXT NOT NULL DEFAULT 'pbs'"
            )

    def close(self) -> None:
        self.conn.close()

    def create(self, name: str, content: str, kind: str = KIND_PBS) -> sqlite3.Row:
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO templates(name, content, kind, created_at, updated_at) "
                "VALUES(?,?,?,?,?)",
                (name, content, kind, now, now),
            )
            self.conn.commit()
            tid = cur.lastrowid
        return self.get(tid)

    def update(self, tid: int, data: dict) -> Optional[sqlite3.Row]:
        sets, params = [], []
        if "name" in data:
            sets.append("name=?"); params.append(data["name"])
        if "content" in data:
            sets.append("content=?"); params.append(data["content"])
        if "kind" in data:
            sets.append("kind=?"); params.append(data["kind"])
        if not sets:
            return self.get(tid)
        sets.append("updated_at=?"); params.append(time.time()); params.append(tid)
        with self._lock:
            self.conn.execute(f"UPDATE templates SET {', '.join(sets)} WHERE id=?", params)
            self.conn.commit()
        return self.get(tid)

    def delete(self, tid: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM templates WHERE id=?", (tid,))
            self.conn.commit()
            return cur.rowcount > 0

    def get(self, tid: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()

    def list_all(self) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute("SELECT * FROM templates ORDER BY name ASC").fetchall()


def row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "content": r["content"],
        "kind": r["kind"] if "kind" in r.keys() else KIND_PBS,
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }
