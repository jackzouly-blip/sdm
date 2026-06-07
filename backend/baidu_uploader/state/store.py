"""上传状态持久化（SQLite）。

两个用途：
  1. 断点续传：记录每个文件的 uploadid、分片 MD5 列表、已上传分片序号，
     进程重启后复用同一 uploadid 只补传缺失分片。
  2. 增量去重：记录已成功上传文件的 内容MD5 / 大小 / mtime，
     再次同步时跳过未变更的文件。

以 (remote_path) 为主键标识一次上传任务。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ..logger import get_logger

log = get_logger(__name__)

# 上传任务状态
PENDING = "pending"
UPLOADING = "uploading"
DONE = "done"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_task (
    remote_path   TEXT PRIMARY KEY,
    local_path    TEXT NOT NULL,
    content_md5   TEXT NOT NULL,   -- 整文件内容 MD5，用于增量去重
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,   -- 本地文件修改时间，快速预判变更
    chunk_size    INTEGER NOT NULL,
    block_list    TEXT NOT NULL,   -- JSON: 各分片 MD5
    uploadid      TEXT,            -- precreate 返回，断点续传复用
    uploaded      TEXT NOT NULL,   -- JSON: 已上传分片序号列表
    status        TEXT NOT NULL,
    updated_at    REAL NOT NULL
);
"""


class StateStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- 查询 -----------------------------------------------------------

    def get(self, remote_path: str) -> sqlite3.Row | None:
        cur = self.conn.execute(
            "SELECT * FROM upload_task WHERE remote_path = ?", (remote_path,)
        )
        return cur.fetchone()

    def is_unchanged(self, remote_path: str, content_md5: str, size: int) -> bool:
        """该远端路径是否已成功上传且内容未变（增量去重判断）。"""
        row = self.get(remote_path)
        return bool(
            row
            and row["status"] == DONE
            and row["content_md5"] == content_md5
            and row["size"] == size
        )

    # --- 写入 -----------------------------------------------------------

    def upsert_task(
        self,
        remote_path: str,
        local_path: str,
        content_md5: str,
        size: int,
        mtime: float,
        chunk_size: int,
        block_list: list[str],
    ) -> sqlite3.Row:
        """创建或重置一个上传任务。

        若已有记录但内容 MD5 变了（文件被修改），重置 uploadid/uploaded 重新上传。
        """
        row = self.get(remote_path)
        if row and row["content_md5"] == content_md5 and row["status"] != DONE:
            # 同一文件未完成，保留 uploadid 与已传分片以便续传
            return row

        self.conn.execute(
            """
            INSERT INTO upload_task
                (remote_path, local_path, content_md5, size, mtime, chunk_size,
                 block_list, uploadid, uploaded, status, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(remote_path) DO UPDATE SET
                local_path=excluded.local_path,
                content_md5=excluded.content_md5,
                size=excluded.size,
                mtime=excluded.mtime,
                chunk_size=excluded.chunk_size,
                block_list=excluded.block_list,
                uploadid=NULL,
                uploaded='[]',
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                remote_path,
                local_path,
                content_md5,
                size,
                mtime,
                chunk_size,
                json.dumps(block_list),
                None,
                "[]",
                PENDING,
                time.time(),
            ),
        )
        self.conn.commit()
        return self.get(remote_path)  # type: ignore[return-value]

    def set_uploadid(self, remote_path: str, uploadid: str) -> None:
        self.conn.execute(
            "UPDATE upload_task SET uploadid=?, status=?, updated_at=? WHERE remote_path=?",
            (uploadid, UPLOADING, time.time(), remote_path),
        )
        self.conn.commit()

    def mark_part_done(self, remote_path: str, partseq: int) -> None:
        row = self.get(remote_path)
        if not row:
            return
        uploaded = set(json.loads(row["uploaded"]))
        uploaded.add(partseq)
        self.conn.execute(
            "UPDATE upload_task SET uploaded=?, updated_at=? WHERE remote_path=?",
            (json.dumps(sorted(uploaded)), time.time(), remote_path),
        )
        self.conn.commit()

    def get_uploaded(self, remote_path: str) -> set[int]:
        row = self.get(remote_path)
        return set(json.loads(row["uploaded"])) if row else set()

    def mark_done(self, remote_path: str) -> None:
        self.conn.execute(
            "UPDATE upload_task SET status=?, updated_at=? WHERE remote_path=?",
            (DONE, time.time(), remote_path),
        )
        self.conn.commit()
