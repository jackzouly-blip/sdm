"""目录收藏库（SQLite，按用户存储）。

每个用户收藏自己常用的目录路径，供文件浏览快速跳转。路径按用户隔离。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import List

_SCHEMA = """
CREATE TABLE IF NOT EXISTS favorites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner      TEXT NOT NULL,
    path       TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(owner, path)
);
"""


class FavoritesDB:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def list_by_owner(self, owner: str) -> List[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT path FROM favorites WHERE owner=? ORDER BY path ASC", (owner,)
            ).fetchall()
        return [r["path"] for r in rows]

    def add(self, owner: str, path: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO favorites(owner, path, created_at) VALUES(?,?,?)",
                (owner, path, time.time()),
            )
            self.conn.commit()

    def remove(self, owner: str, path: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM favorites WHERE owner=? AND path=?", (owner, path)
            )
            self.conn.commit()
