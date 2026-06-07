"""网盘上传引擎适配层：封装 baidu_uploader。

把已就绪的 baidu_uploader（OAuth/分片/断点续传/分享）包装成门户内部统一接口。
按用户隔离上传目录：/apps/HPC/<username>/...
"""
from __future__ import annotations

import random
import string
from pathlib import Path
from typing import Callable, Dict, Optional

from baidu_uploader.api.client import BaiduPanClient
from baidu_uploader.auth.oauth import BaiduOAuth
from baidu_uploader.config import load_config
from baidu_uploader.core.uploader import Uploader
from baidu_uploader.state.store import StateStore

from ..config import get_settings
from ..logger import get_logger

log = get_logger(__name__)


def _gen_pwd(k: int = 4) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=k))


class NetdiskEngine:
    """单次会话的网盘操作封装。用完即关闭。"""

    def __init__(self):
        self.cfg = load_config(get_settings().baidu_config)
        self.oauth = BaiduOAuth(self.cfg)
        self.client = BaiduPanClient(self.oauth.get_access_token)
        self.store = StateStore(self.cfg.state.db_path)

    def close(self) -> None:
        self.client.close()
        self.store.close()

    def remote_path_for(self, username: str, filename: str) -> str:
        """用户隔离的网盘目标路径：<remote_dir>/<username>/<filename>。"""
        base = self.cfg.upload.remote_dir.rstrip("/")
        return f"{base}/{username}/{filename}"

    def upload(
        self,
        local_path: str,
        remote_path: str,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> Dict:
        """上传文件。progress_cb(done_parts, total_parts) 可选进度回调。"""
        uploader = Uploader(
            self.client,
            self.cfg.upload.chunk_size,
            self.cfg.upload.concurrency,
            store=self.store,
        )
        if progress_cb is not None:
            uploader.progress_cb = progress_cb  # uploader 内部按存在与否调用
        return uploader.upload_file(local_path, remote_path)

    def share(self, remote_path: str, pwd: Optional[str] = None, period: int = 7) -> Dict:
        """为单个网盘文件创建带提取码的分享链接，返回 {link, pwd, period}。"""
        return self.share_many([remote_path], pwd=pwd, period=period)

    def share_many(
        self,
        remote_paths: list[str],
        pwd: Optional[str] = None,
        period: int = 7,
    ) -> Dict:
        """为一组网盘文件创建一个带提取码的分享链接（一个链接含全部文件）。"""
        if not remote_paths:
            raise ValueError("没有可分享的文件")
        if not pwd:
            pwd = _gen_pwd()
        fs_ids = [self.client.get_fsid(p) for p in remote_paths]
        res = self.client.create_share(fs_ids, pwd=pwd, period=period)
        return {
            "link": res.get("link"),
            "shorturl": res.get("shorturl"),
            "pwd": pwd,
            "period": period,
        }
