"""从平台账号网盘下载文件到集群：官方 OAuth dlink + Range 断点续传。

入站同步的第二段（第一段见 share_client.py）。这一段全部走官方开放接口，
是整条链路里最稳的部分。

dlink 的三个坑（POC 已踩过，勿改）：
  1. access_token 必须**手工拼接**到 URL，不能走 httpx params——dlink 自带已签名
     的 query，httpx 会重新编码从而破坏签名，返回 errno 31023 sign error；
  2. 必须带 User-Agent: pan.baidu.com；
  3. 会 302 跳转到实际存储节点，必须 follow_redirects。

落盘策略：先写 `<目标目录>/.hpc-part/<文件名>.part`，校验通过后 rename 到正式名。
文件始终以**目标用户身份**创建（write_stream_as_user），root 不越过目录权限写入。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import httpx

from ..logger import get_logger
from ..privilege.actas import call_as_user, stream_file_as_user, write_stream_as_user

log = get_logger(__name__)

PAN_UA = {"User-Agent": "pan.baidu.com"}
PART_DIR = ".hpc-part"


class DownloadError(RuntimeError):
    pass


@dataclass
class RemoteFile:
    """待下载文件的元信息（来自 filemetas / share list）。"""

    fs_id: int
    filename: str
    size: int
    dlink: str
    md5: str = ""


def resolve_dlinks(client, fs_ids: List[int], batch: int = 100) -> Dict[int, RemoteFile]:
    """批量把 fs_id 解析成带 dlink 的 RemoteFile。

    dlink 有效期约 8 小时，因此**用之前才解析**，不要提前批量缓存过久。
    """
    out: Dict[int, RemoteFile] = {}
    for i in range(0, len(fs_ids), batch):
        for meta in client.filemetas(fs_ids[i:i + batch], dlink=True):
            fs_id = int(meta.get("fs_id", 0))
            dlink = meta.get("dlink") or ""
            if not fs_id or not dlink:
                log.warning("filemetas 未返回 dlink: %s", meta)
                continue
            out[fs_id] = RemoteFile(
                fs_id=fs_id,
                filename=meta.get("filename") or meta.get("server_filename") or str(fs_id),
                size=int(meta.get("size", 0)),
                dlink=dlink,
                md5=(meta.get("md5") or ""),
            )
    return out


def _dl_url(dlink: str, token: str) -> str:
    """手工拼接 access_token（见模块 docstring 坑 1）。"""
    sep = "&" if "?" in dlink else "?"
    return f"{dlink}{sep}access_token={token}"


def download_file(
    rf: RemoteFile,
    token: str,
    dest_dir: str,
    username: str,
    *,
    resume_from: int = 0,
    verify_md5: bool = True,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    timeout: int = 300,
) -> str:
    """下载单个文件到 dest_dir，返回最终落盘路径。

    resume_from > 0 时发 Range 续传；若服务端不支持 Range（返回 200 而非 206），
    自动退回从 0 重传，不会静默拼出损坏文件。
    progress_cb(done_bytes, total_bytes) 用于任务进度。
    """
    safe_name = _safe_filename(rf.filename)
    part_dir = os.path.join(dest_dir, PART_DIR)
    part_path = os.path.join(part_dir, safe_name + ".part")
    final_path = os.path.join(dest_dir, safe_name)

    call_as_user(username, _ensure_dir, part_dir)

    headers = dict(PAN_UA)
    if resume_from > 0:
        headers["Range"] = f"bytes={resume_from}-"

    done = resume_from
    with httpx.stream(
        "GET",
        _dl_url(rf.dlink, token),
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    ) as resp:
        if resp.status_code not in (200, 206):
            body = ""
            try:
                body = resp.read()[:300].decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            raise DownloadError(
                f"下载 {rf.filename} 失败 HTTP {resp.status_code}: {body}"
            )
        append = True
        if resume_from > 0 and resp.status_code == 200:
            # 服务端忽略了 Range，整个文件从头返回：必须重来，否则会拼接出脏数据
            log.warning("dlink 未响应 Range（返回 200），%s 放弃续传从头下载", rf.filename)
            append = False
            done = 0

        def _chunks():
            nonlocal done
            for b in resp.iter_bytes(1 << 20):
                done += len(b)
                if progress_cb is not None:
                    progress_cb(done, rf.size)
                yield b

        write_stream_as_user(username, part_path, _chunks(), append=append)

    # 校验：size 是硬判据，md5 是防"拿到旧版本"的第二道闸
    actual = call_as_user(username, _file_size, part_path)
    if rf.size and actual != rf.size:
        raise DownloadError(
            f"{rf.filename} 大小不符：期望 {rf.size} 实得 {actual}（已保留 .part 供续传）"
        )
    if verify_md5 and rf.md5:
        got = md5_as_user(username, part_path)
        if got.lower() != rf.md5.lower():
            raise DownloadError(
                f"{rf.filename} md5 不符：期望 {rf.md5} 实得 {got}"
                "（可能取到了中转区的旧版本，检查批次目录隔离）"
            )

    call_as_user(username, _rename, part_path, final_path)
    return final_path


def md5_as_user(username: str, path: str, chunk: int = 1 << 20) -> str:
    """以目标用户身份读文件算 md5。

    复用 stream_file_as_user：只 fork 一次边读边算，不把整个文件读进内存。
    """
    h = hashlib.md5()
    for b in stream_file_as_user(username, path, chunk):
        h.update(b)
    return h.hexdigest()


def part_size(username: str, dest_dir: str, filename: str) -> int:
    """已下载的 .part 字节数；不存在返回 0。用于决定 Range 起点。"""
    part_path = os.path.join(dest_dir, PART_DIR, _safe_filename(filename) + ".part")
    try:
        return call_as_user(username, _file_size, part_path)
    except Exception:  # noqa: BLE001
        return 0


def _safe_filename(name: str) -> str:
    """清理网盘侧文件名：去路径分隔符与控制字符，防路径穿越。"""
    import re

    name = (name or "").strip().replace("/", "_").replace("\\", "_")
    name = re.sub(r'[\x00-\x1f<>:"|?*]+', "_", name)
    name = name.lstrip(".") or "unnamed"  # 防 ".." 与隐藏文件
    return name[:200]


# --- 以下为 call_as_user 的目标函数，必须是可 pickle 的顶层函数 ---

def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _rename(src: str, dst: str) -> str:
    os.replace(src, dst)
    return dst
