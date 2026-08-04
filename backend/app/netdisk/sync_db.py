"""网盘入站同步库（SQLite）：分享源 + 文件级清单。

两张表各司其职：
  netdisk_shares       一个客户分享链接 ↔ 一个集群落点，附同步策略
  netdisk_share_files  文件级清单，同时充当三种角色——
                       增量判据（fs_id 见过没）、进度展示、中转区清理依据

**fs_id 一律以 TEXT 存取**。百度的 fs_id 是 64 位整数，超出 JS Number 的安全
整数范围（2^53），一旦以数字形式进出前端就会被静默改写成邻近值，增量判据随之
失效——文件要么重复转存、要么永远同步不到。存 TEXT 让它从库到前端全程保真。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS netdisk_shares (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner         TEXT NOT NULL,             -- 属主（POSIX 用户名）
    name          TEXT NOT NULL,             -- 展示名，用户自己起
    share_url     TEXT NOT NULL,
    pwd           TEXT NOT NULL DEFAULT '',  -- 提取码
    sub_dir       TEXT NOT NULL DEFAULT '',  -- 只同步分享内的某个子目录，''=根
    local_dir     TEXT NOT NULL,             -- 集群落点（绝对路径，须在 fs_roots 内）
    enabled       INTEGER NOT NULL DEFAULT 1,
    poll_interval INTEGER NOT NULL DEFAULT 0,-- 秒；0=仅手动同步
    link_state    TEXT NOT NULL DEFAULT 'unknown', -- unknown/ok/invalid/auth_failed
    last_poll_at  REAL NOT NULL DEFAULT 0,
    last_status   TEXT NOT NULL DEFAULT '',  -- idle/syncing/done/failed
    last_error    TEXT NOT NULL DEFAULT '',
    last_task_id  TEXT NOT NULL DEFAULT '',  -- 最近一次同步任务，供前端订阅进度
    syncing       INTEGER NOT NULL DEFAULT 0,-- CAS 占位，防手动与定时重复派发
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS netdisk_share_files (
    share_id   INTEGER NOT NULL,
    fs_id      TEXT NOT NULL,               -- 见模块 docstring：必须 TEXT
    share_path TEXT NOT NULL DEFAULT '',    -- 在分享内的路径
    filename   TEXT NOT NULL,
    size       INTEGER NOT NULL DEFAULT 0,
    md5        TEXT NOT NULL DEFAULT '',
    batch_id   TEXT NOT NULL DEFAULT '',    -- 所属转存批次（中转区子目录名）
    state      TEXT NOT NULL DEFAULT 'seen',-- seen/transferred/done/failed
    local_path TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (share_id, fs_id)
);

CREATE INDEX IF NOT EXISTS idx_share_files_state
    ON netdisk_share_files(share_id, state);
CREATE INDEX IF NOT EXISTS idx_shares_owner ON netdisk_shares(owner);

-- 平台账号的网页 cookie（单行）。转存没有开放接口，只能靠它。
-- 放库里而非 env：BDUSS 会过期、需定期轮换，改 env 得 ssh 上生产 + 重启服务，
-- 而重启会打断正在跑的同步任务与在线终端会话。存库则改完立即生效。
-- 与 baidu.token.json 一样以明文落盘，靠服务器文件权限保护（与现有姿态一致）。
CREATE TABLE IF NOT EXISTS netdisk_credentials (
    id              INTEGER PRIMARY KEY CHECK (id = 1),  -- 恒为单行
    bduss           TEXT NOT NULL DEFAULT '',
    stoken          TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT 'unknown',  -- unknown/ok/auth_failed
    account         TEXT NOT NULL DEFAULT '',         -- 自检时拿到的账号名，便于确认配对了号
    updated_at      REAL NOT NULL DEFAULT 0,
    updated_by      TEXT NOT NULL DEFAULT '',
    last_checked_at REAL NOT NULL DEFAULT 0
);
"""

# 可被 update() 修改的字段（白名单，避免前端塞进任意列）
_MUTABLE = (
    "name", "share_url", "pwd", "sub_dir", "local_dir", "enabled", "poll_interval",
)


class NetdiskSyncDB:
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

    # --- 分享源 CRUD ----------------------------------------------------

    def create(self, owner: str, data: dict) -> sqlite3.Row:
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO netdisk_shares
                   (owner, name, share_url, pwd, sub_dir, local_dir, enabled,
                    poll_interval, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    owner,
                    data["name"],
                    data["share_url"],
                    data.get("pwd", "") or "",
                    data.get("sub_dir", "") or "",
                    data["local_dir"],
                    1 if data.get("enabled", True) else 0,
                    int(data.get("poll_interval", 0) or 0),
                    now,
                    now,
                ),
            )
            self.conn.commit()
            sid = cur.lastrowid
        return self.get(sid)

    def update(self, share_id: int, data: dict) -> Optional[sqlite3.Row]:
        sets, params = [], []
        for f in _MUTABLE:
            if f not in data:
                continue
            if f == "enabled":
                sets.append("enabled=?")
                params.append(1 if data[f] else 0)
            elif f == "poll_interval":
                sets.append("poll_interval=?")
                params.append(int(data[f] or 0))
            else:
                sets.append(f"{f}=?")
                params.append(data[f] or "")
        if not sets:
            return self.get(share_id)
        sets.append("updated_at=?")
        params.extend([time.time(), share_id])
        with self._lock:
            self.conn.execute(
                f"UPDATE netdisk_shares SET {', '.join(sets)} WHERE id=?", params
            )
            self.conn.commit()
        return self.get(share_id)

    def delete(self, share_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM netdisk_shares WHERE id=?", (share_id,)
            )
            self.conn.execute(
                "DELETE FROM netdisk_share_files WHERE share_id=?", (share_id,)
            )
            self.conn.commit()
            return cur.rowcount > 0

    def get(self, share_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM netdisk_shares WHERE id=?", (share_id,)
            ).fetchone()

    def list_by_owner(self, owner: str) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM netdisk_shares WHERE owner=? ORDER BY id DESC",
                (owner,),
            ).fetchall()

    def list_all(self) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM netdisk_shares ORDER BY id DESC"
            ).fetchall()

    # --- 同步状态 -------------------------------------------------------

    def try_begin_sync(self, share_id: int) -> bool:
        """CAS 认领：syncing 0→1。手动按钮与定时轮询并发时只有一个能进。"""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE netdisk_shares SET syncing=1, last_status='syncing',"
                " last_error='', updated_at=? WHERE id=? AND syncing=0",
                (time.time(), share_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def end_sync(
        self,
        share_id: int,
        status: str,
        error: str = "",
        link_state: Optional[str] = None,
    ) -> None:
        now = time.time()
        sets = ["syncing=0", "last_status=?", "last_error=?", "last_poll_at=?",
                "updated_at=?"]
        params: list = [status, error[:500], now, now]
        if link_state:
            sets.insert(0, "link_state=?")
            params.insert(0, link_state)
        params.append(share_id)
        with self._lock:
            self.conn.execute(
                f"UPDATE netdisk_shares SET {', '.join(sets)} WHERE id=?", params
            )
            self.conn.commit()

    def set_task(self, share_id: int, task_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE netdisk_shares SET last_task_id=?, updated_at=? WHERE id=?",
                (task_id, time.time(), share_id),
            )
            self.conn.commit()

    def reset_stuck(self) -> int:
        """服务重启后把残留的 syncing 占位清掉，否则该源再也无法同步。"""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE netdisk_shares SET syncing=0, last_status='failed',"
                " last_error='服务重启，同步中断' WHERE syncing=1"
            )
            self.conn.commit()
            return cur.rowcount

    def due_shares(self, now: Optional[float] = None) -> List[sqlite3.Row]:
        """到期该轮询的源：启用、设了间隔、未在同步、且距上次轮询已够久。"""
        now = time.time() if now is None else now
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM netdisk_shares WHERE enabled=1 AND poll_interval>0"
                " AND syncing=0 AND (? - last_poll_at) >= poll_interval",
                (now,),
            ).fetchall()

    # --- 文件清单 -------------------------------------------------------

    def known_fs_ids(self, share_id: int) -> set:
        with self._lock:
            rows = self.conn.execute(
                "SELECT fs_id FROM netdisk_share_files WHERE share_id=?", (share_id,)
            ).fetchall()
        return {r["fs_id"] for r in rows}

    def add_seen(self, share_id: int, files: List[dict]) -> int:
        """把新发现的文件登记为 seen。已存在的 fs_id 原样保留（不覆盖其状态）。

        返回真正新增的条数——这就是"用户这轮新加了几个文件"。
        """
        if not files:
            return 0
        now = time.time()
        added = 0
        with self._lock:
            for f in files:
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO netdisk_share_files
                       (share_id, fs_id, share_path, filename, size, md5,
                        state, updated_at)
                       VALUES (?,?,?,?,?,?, 'seen', ?)""",
                    (
                        share_id,
                        str(f["fs_id"]),
                        f.get("share_path", "") or "",
                        f.get("filename", "") or "",
                        int(f.get("size", 0) or 0),
                        f.get("md5", "") or "",
                        now,
                    ),
                )
                added += cur.rowcount
            self.conn.commit()
        return added

    def mark(self, share_id: int, fs_ids: List[str], **fields) -> None:
        """批量更新一组文件的状态字段。"""
        if not fs_ids or not fields:
            return
        allowed = ("state", "batch_id", "local_path", "error")
        sets, params = [], []
        for k in allowed:
            if k in fields:
                sets.append(f"{k}=?")
                params.append(str(fields[k] or "")[:500])
        if not sets:
            return
        sets.append("updated_at=?")
        params.append(time.time())
        with self._lock:
            for fid in fs_ids:
                self.conn.execute(
                    f"UPDATE netdisk_share_files SET {', '.join(sets)}"
                    " WHERE share_id=? AND fs_id=?",
                    (*params, share_id, str(fid)),
                )
            self.conn.commit()

    def get_file(self, share_id: int, fs_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM netdisk_share_files WHERE share_id=? AND fs_id=?",
                (share_id, str(fs_id)),
            ).fetchone()

    def drop_file(self, share_id: int, fs_id: str) -> None:
        """删除一条清单记录。

        用于客户在网盘上替换了文件的情形：新版本是新的 fs_id，旧记录既不再对应
        分享里的任何文件、也不该继续占着列表，删掉后由 add_seen 重新登记新 fs_id。
        （fs_id 是主键，"改 id"只能删了重登。）
        """
        with self._lock:
            self.conn.execute(
                "DELETE FROM netdisk_share_files WHERE share_id=? AND fs_id=?",
                (share_id, str(fs_id)),
            )
            self.conn.commit()

    def list_files(
        self, share_id: int, state: Optional[str] = None, limit: int = 2000
    ) -> List[sqlite3.Row]:
        sql = "SELECT * FROM netdisk_share_files WHERE share_id=?"
        params: list = [share_id]
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY updated_at DESC, filename ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def pending_files(self, share_id: int) -> List[sqlite3.Row]:
        """待处理：新发现的（seen）与上次失败的（failed）——失败的天然会重试。"""
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM netdisk_share_files WHERE share_id=?"
                " AND state IN ('seen','failed','transferred') ORDER BY size ASC",
                (share_id,),
            ).fetchall()

    def counts(self, share_id: int) -> Dict[str, int]:
        """各状态计数，供列表页显示「12 已完成 / 2 失败」。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT state, COUNT(*) n FROM netdisk_share_files"
                " WHERE share_id=? GROUP BY state",
                (share_id,),
            ).fetchall()
        return {r["state"]: r["n"] for r in rows}

    # --- 平台凭据 ------------------------------------------------------

    def get_credentials(self) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM netdisk_credentials WHERE id=1"
            ).fetchone()

    def set_credentials(self, bduss: str, stoken: str, by: str) -> None:
        """写入/更新平台凭据。写入后状态重置为 unknown，等自检或首次同步确认。"""
        now = time.time()
        with self._lock:
            self.conn.execute(
                """INSERT INTO netdisk_credentials
                   (id, bduss, stoken, state, account, updated_at, updated_by)
                   VALUES (1,?,?, 'unknown', '', ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     bduss=excluded.bduss, stoken=excluded.stoken,
                     state='unknown', account='',
                     updated_at=excluded.updated_at,
                     updated_by=excluded.updated_by""",
                (bduss, stoken, now, by),
            )
            self.conn.commit()

    def clear_credentials(self, by: str) -> None:
        self.set_credentials("", "", by)

    def mark_credential_state(self, state: str, account: str = "") -> None:
        """自检或同步失败时回写状态，让管理页能显眼地提示"该换 cookie 了"。"""
        now = time.time()
        with self._lock:
            self.conn.execute(
                "UPDATE netdisk_credentials SET state=?, last_checked_at=?"
                + (", account=?" if account else "")
                + " WHERE id=1",
                ((state, now, account) if account else (state, now)),
            )
            self.conn.commit()

    def resolve_credentials(self) -> tuple:
        """有效凭据 (bduss, stoken)：库内配置优先，回退到环境变量。

        保留 env 回退是为了兼容既有部署与首次引导——库里没配时，
        运维仍可先用 env 把功能跑起来，之后再改到页面上管理。
        """
        from ..config import get_settings

        row = self.get_credentials()
        if row is not None and row["bduss"]:
            return row["bduss"], row["stoken"]
        s = get_settings()
        return s.netdisk_bduss, s.netdisk_stoken

    def credentials_status(self) -> Dict:
        """给前端看的凭据状态——**绝不包含凭据本身**。"""
        from ..config import get_settings

        row = self.get_credentials()
        in_db = bool(row is not None and row["bduss"])
        bduss, stoken = self.resolve_credentials()
        return {
            "configured": bool(bduss),
            "source": "db" if in_db else ("env" if bduss else "none"),
            "has_stoken": bool(stoken),
            "state": (row["state"] if in_db else ("unknown" if bduss else "none")),
            "account": (row["account"] if in_db else ""),
            "updated_at": (row["updated_at"] if in_db else 0),
            "updated_by": (row["updated_by"] if in_db else ""),
            "last_checked_at": (row["last_checked_at"] if in_db else 0),
        }

    def batches(self, share_id: int) -> List[sqlite3.Row]:
        """中转区批次汇总：体积与完成度，供人工清理决策。"""
        with self._lock:
            return self.conn.execute(
                "SELECT batch_id, COUNT(*) files, SUM(size) bytes,"
                " SUM(CASE WHEN state='done' THEN 1 ELSE 0 END) done"
                " FROM netdisk_share_files WHERE share_id=? AND batch_id<>''"
                " GROUP BY batch_id ORDER BY batch_id DESC",
                (share_id,),
            ).fetchall()


def share_to_dict(r: sqlite3.Row, counts: Optional[Dict[str, int]] = None) -> dict:
    d = {
        "id": r["id"],
        "owner": r["owner"],
        "name": r["name"],
        "share_url": r["share_url"],
        "pwd": r["pwd"],
        "sub_dir": r["sub_dir"],
        "local_dir": r["local_dir"],
        "enabled": bool(r["enabled"]),
        "poll_interval": r["poll_interval"],
        "link_state": r["link_state"],
        "last_poll_at": r["last_poll_at"],
        "last_status": r["last_status"],
        "last_error": r["last_error"],
        "last_task_id": r["last_task_id"],
        "syncing": bool(r["syncing"]),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }
    if counts is not None:
        d["counts"] = counts
    return d


def file_to_dict(r: sqlite3.Row) -> dict:
    return {
        "fs_id": r["fs_id"],  # 字符串：勿在前端转 Number（见模块 docstring）
        "share_path": r["share_path"],
        "filename": r["filename"],
        "size": r["size"],
        "md5": r["md5"],
        "batch_id": r["batch_id"],
        "state": r["state"],
        "local_path": r["local_path"],
        "error": r["error"],
        "updated_at": r["updated_at"],
    }
