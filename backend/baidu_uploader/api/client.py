"""百度网盘 xpan 文件接口封装。

三步上传：
  precreate  -> 预创建，返回 uploadid 与仍需上传的分片序号 block_list
  superfile2 -> 逐分片上传二进制（上传域名 d.pcs.baidu.com）
  create     -> 合并分片，落盘成文件

所有 xpan 接口必须带 User-Agent: pan.baidu.com，否则返回 unsupported api。
参考：https://pan.baidu.com/union/doc/3ksg0s9r7
"""
from __future__ import annotations

import json

import httpx

from ..logger import get_logger

log = get_logger(__name__)

FILE_API = "https://pan.baidu.com/rest/2.0/xpan/file"
SHARE_API = "https://pan.baidu.com/rest/2.0/xpan/share"
SUPERFILE = "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"
HEADERS = {"User-Agent": "pan.baidu.com"}


class BaiduApiError(RuntimeError):
    def __init__(self, where: str, errno: int, payload: dict):
        super().__init__(f"{where} 失败 errno={errno}: {payload}")
        self.where = where
        self.errno = errno
        self.payload = payload


class BaiduPanClient:
    def __init__(self, access_token_provider):
        """access_token_provider: 无参可调用对象，每次返回有效 access_token。"""
        self._get_token = access_token_provider
        self._client = httpx.Client(timeout=120, headers=HEADERS)

    def close(self) -> None:
        self._client.close()

    # --- step 1: precreate ---------------------------------------------

    def precreate(
        self, remote_path: str, size: int, block_list: list[str], rtype: int = 3
    ) -> dict:
        """预创建。返回含 uploadid 和 block_list（仍需上传的分片序号）。

        rtype: 3 = 同名且 md5 不同则重命名新文件，避免覆盖。
        """
        data = {
            "path": remote_path,
            "size": str(size),
            "isdir": "0",
            "autoinit": "1",
            "rtype": str(rtype),
            "block_list": json.dumps(block_list),
        }
        resp = self._client.post(
            FILE_API,
            params={"method": "precreate", "access_token": self._get_token()},
            data=data,
        )
        payload = self._json(resp)
        if payload.get("errno", 0) != 0:
            raise BaiduApiError("precreate", payload.get("errno", -1), payload)
        return payload

    # --- step 2: superfile2 --------------------------------------------

    def upload_part(
        self, remote_path: str, uploadid: str, partseq: int, chunk: bytes
    ) -> dict:
        """上传单个分片。"""
        resp = self._client.post(
            SUPERFILE,
            params={
                "method": "upload",
                "access_token": self._get_token(),
                "type": "tmpfile",
                "path": remote_path,
                "uploadid": uploadid,
                "partseq": str(partseq),
            },
            files={"file": ("chunk", chunk, "application/octet-stream")},
        )
        payload = self._json(resp)
        # superfile2 成功返回含 md5，无 errno；出错才带 error_code
        if "md5" not in payload and payload.get("error_code"):
            raise BaiduApiError("upload_part", payload.get("error_code", -1), payload)
        return payload

    # --- step 3: create ------------------------------------------------

    def create(
        self,
        remote_path: str,
        size: int,
        block_list: list[str],
        uploadid: str,
        rtype: int = 3,
    ) -> dict:
        """合并分片，完成上传。"""
        data = {
            "path": remote_path,
            "size": str(size),
            "isdir": "0",
            "rtype": str(rtype),
            "uploadid": uploadid,
            "block_list": json.dumps(block_list),
        }
        resp = self._client.post(
            FILE_API,
            params={"method": "create", "access_token": self._get_token()},
            data=data,
        )
        payload = self._json(resp)
        if payload.get("errno", 0) != 0:
            raise BaiduApiError("create", payload.get("errno", -1), payload)
        return payload

    # --- 列目录 / 解析 fs_id -------------------------------------------

    def list_dir(self, remote_dir: str) -> list[dict]:
        """列出网盘目录下的文件，返回条目列表（含 fs_id / path / isdir）。"""
        resp = self._client.get(
            FILE_API,
            params={
                "method": "list",
                "access_token": self._get_token(),
                "dir": remote_dir,
            },
        )
        payload = self._json(resp)
        if payload.get("errno", 0) != 0:
            raise BaiduApiError("list", payload.get("errno", -1), payload)
        return payload.get("list", [])

    def get_fsid(self, remote_path: str) -> int:
        """通过列父目录解析出文件的 fs_id。"""
        remote_path = remote_path.rstrip("/")
        parent = remote_path.rsplit("/", 1)[0] or "/"
        for item in self.list_dir(parent):
            if item.get("path") == remote_path:
                return int(item["fs_id"])
        raise FileNotFoundError(f"网盘中未找到文件: {remote_path}")

    # --- 创建分享链接 ---------------------------------------------------

    def create_share(self, fid_list: list[int], pwd: str, period: int = 0) -> dict:
        """创建带提取码的分享链接。

        pwd: 提取码（4 位）。百度已强制要求提取码，无码公开分享会返回 errno 115。
        period: 有效天数，0 = 永久。
        返回含 link / shorturl / shareid。
        """
        # schannel=4 表示带提取码分享
        data = {
            "fid_list": json.dumps(fid_list),
            "channel_list": "[]",
            "period": str(period),
            "schannel": "4",
            "pwd": pwd,
        }
        resp = self._client.post(
            SHARE_API,
            params={"method": "set", "access_token": self._get_token()},
            data=data,
        )
        payload = self._json(resp)
        if payload.get("errno", 0) != 0:
            raise BaiduApiError("share.set", payload.get("errno", -1), payload)
        return payload

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        resp.raise_for_status()
        return resp.json()
