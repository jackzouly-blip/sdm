"""分片 MD5 计算。

百度 precreate/create 的 block_list 需要每个分片内容的 MD5（小写十六进制）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def slice_md5_list(path: Path, chunk_size: int) -> tuple[list[str], int]:
    """逐分片读取并计算 MD5，返回 (md5列表, 文件总大小)。"""
    md5s: list[str] = []
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            md5s.append(hashlib.md5(chunk).hexdigest())
    if not md5s:  # 空文件也要有一个分片
        md5s.append(hashlib.md5(b"").hexdigest())
    return md5s, size


def file_md5(path: Path, buf_size: int = 1024 * 1024) -> str:
    """整文件内容 MD5，用于增量去重判断文件是否变更。"""
    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            chunk = f.read(buf_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def read_chunk(path: Path, partseq: int, chunk_size: int) -> bytes:
    """读取指定序号的分片内容。"""
    with path.open("rb") as f:
        f.seek(partseq * chunk_size)
        return f.read(chunk_size)
