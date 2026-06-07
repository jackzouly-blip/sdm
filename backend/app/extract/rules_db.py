"""数据提取规则库（SQLite）。

规则描述"任务完成后在其工作目录里运行什么命令"，以及命中哪些任务。
匹配条件留空表示不限制；多条规则可同时命中同一任务，按优先级排序后逐条派发。

命令模板在执行时以 shlex 拆分为 argv（绝不走 shell），再对每个片段做占位符
替换：{workdir} {jobid} {short_id} {name} {owner} {queue}。
"""
from __future__ import annotations

import fnmatch
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS extract_rules (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,           -- 规则名称（展示用）
    command              TEXT NOT NULL,           -- 命令模板（含占位符）
    match_name           TEXT NOT NULL DEFAULT '',-- 任务名通配，空=不限
    match_queue          TEXT NOT NULL DEFAULT '',-- 队列名通配，空=不限
    match_workdir_prefix TEXT NOT NULL DEFAULT '',-- 工作目录前缀，空=不限
    match_owner          TEXT NOT NULL DEFAULT '',-- 属主精确匹配，空=不限
    enabled              INTEGER NOT NULL DEFAULT 1,
    priority             INTEGER NOT NULL DEFAULT 100, -- 越小越先执行
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL
);
"""

_FIELDS = (
    "name",
    "command",
    "match_name",
    "match_queue",
    "match_workdir_prefix",
    "match_owner",
    "enabled",
    "priority",
)


class RulesDB:
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

    # --- CRUD -----------------------------------------------------------

    def create(self, data: dict) -> sqlite3.Row:
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO extract_rules
                   (name, command, match_name, match_queue, match_workdir_prefix,
                    match_owner, enabled, priority, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    data["name"],
                    data["command"],
                    data.get("match_name", "") or "",
                    data.get("match_queue", "") or "",
                    data.get("match_workdir_prefix", "") or "",
                    data.get("match_owner", "") or "",
                    1 if data.get("enabled", True) else 0,
                    int(data.get("priority", 100)),
                    now,
                    now,
                ),
            )
            self.conn.commit()
            rid = cur.lastrowid
        return self.get(rid)

    def update(self, rule_id: int, data: dict) -> Optional[sqlite3.Row]:
        sets = []
        params: list = []
        for f in _FIELDS:
            if f in data:
                if f == "enabled":
                    sets.append("enabled=?")
                    params.append(1 if data[f] else 0)
                elif f == "priority":
                    sets.append("priority=?")
                    params.append(int(data[f]))
                else:
                    sets.append(f"{f}=?")
                    params.append(data[f] or "")
        if not sets:
            return self.get(rule_id)
        sets.append("updated_at=?")
        params.append(time.time())
        params.append(rule_id)
        with self._lock:
            self.conn.execute(
                f"UPDATE extract_rules SET {', '.join(sets)} WHERE id=?", params
            )
            self.conn.commit()
        return self.get(rule_id)

    def delete(self, rule_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM extract_rules WHERE id=?", (rule_id,)
            )
            self.conn.commit()
            return cur.rowcount > 0

    def get(self, rule_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM extract_rules WHERE id=?", (rule_id,)
            ).fetchone()

    def list_all(self) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM extract_rules ORDER BY priority ASC, id ASC"
            ).fetchall()

    # --- 匹配 -----------------------------------------------------------

    def match(self, job: sqlite3.Row) -> List[sqlite3.Row]:
        """返回命中该任务的全部启用规则，按优先级升序。"""
        out: List[sqlite3.Row] = []
        for r in self.list_all():
            if not r["enabled"]:
                continue
            if _rule_matches(r, job):
                out.append(r)
        return out


def _rule_matches(rule: sqlite3.Row, job: sqlite3.Row) -> bool:
    name = job["name"] or ""
    queue = job["queue"] or ""
    workdir = job["workdir"] or ""
    owner = job["owner"] or ""

    if rule["match_name"] and not fnmatch.fnmatch(name, rule["match_name"]):
        return False
    if rule["match_queue"] and not fnmatch.fnmatch(queue, rule["match_queue"]):
        return False
    if rule["match_workdir_prefix"] and not workdir.startswith(
        rule["match_workdir_prefix"]
    ):
        return False
    if rule["match_owner"] and owner != rule["match_owner"]:
        return False
    return True


def row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "command": r["command"],
        "match_name": r["match_name"],
        "match_queue": r["match_queue"],
        "match_workdir_prefix": r["match_workdir_prefix"],
        "match_owner": r["match_owner"],
        "enabled": bool(r["enabled"]),
        "priority": r["priority"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }
