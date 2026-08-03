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
import time

import httpx

from ..logger import get_logger

log = get_logger(__name__)

FILE_API = "https://pan.baidu.com/rest/2.0/xpan/file"
SHARE_API = "https://pan.baidu.com/rest/2.0/xpan/share"
MULTIMEDIA_API = "https://pan.baidu.com/rest/2.0/xpan/multimedia"
SUPERFILE = "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"
HEADERS = {"User-Agent": "pan.baidu.com"}


class BaiduApiError(RuntimeError):
    def __init__(self, where: str, errno: int, payload: dict):
        super().__init__(f"{where} 失败 errno={errno}: {payload}")
        self.where = where
        self.errno = errno
        self.payload = payload


class BaiduHttpError(RuntimeError):
    """HTTP 非 2xx：携带百度返回体，避免真实 errno 被状态码吞掉。"""

    def __init__(self, status: int, url: str, body: object):
        super().__init__(f"HTTP {status} {url} 返回: {body}")
        self.status = status
        self.url = url
        self.body = body


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
        """上传单个分片。

        superfile2 会偶发返回空体 HTTP 5xx，也可能连接中断——都是暂时性故障。
        单片重传是幂等的（同一 uploadid + partseq 覆盖写），故就地退避重试。
        不重试 4xx：那是参数/权限一类的确定性错误，重试只是浪费。

        没有这层重试时，一片偶发 500 就会让整个文件失败，GB 级文件因分片多、
        耗时长，撞上的概率相当可观。
        """
        attempts = 4  # 首次 + 3 次重试，退避 2/4/8s
        last_err: Exception | None = None
        for attempt in range(attempts):
            try:
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
                if resp.status_code < 500:
                    payload = self._json(resp)  # 4xx 带响应体抛出；2xx 正常解析
                    # superfile2 成功返回含 md5，无 errno；出错才带 error_code
                    if "md5" not in payload and payload.get("error_code"):
                        raise BaiduApiError(
                            "upload_part", payload.get("error_code", -1), payload
                        )
                    return payload
                body = (resp.text or "")[:200]
                last_err = BaiduHttpError(
                    resp.status_code, SUPERFILE, body or "<空响应体>"
                )
            except httpx.TransportError as e:  # 连接/读写中断
                last_err = e

            if attempt < attempts - 1:
                wait = 2 ** (attempt + 1)
                log.warning(
                    "分片 %d 上传失败（%s），%ss 后重试 (%d/%d): %s",
                    partseq, last_err, wait, attempt + 1, attempts - 1, remote_path,
                )
                time.sleep(wait)
        raise last_err  # type: ignore[misc]

    # --- step 3: create ------------------------------------------------

    def create(
        self,
        remote_path: str,
        size: int,
        block_list: list[str],
        uploadid: str,
        rtype: int = 3,
    ) -> dict:
        """合并分片，完成上传。

        百度 superfile2 的分片是异步提交的，紧接着调用 create 合并时，
        大文件可能因部分分片在服务端尚未就绪而返回空体 HTTP 5xx。
        这是该接口的已知最终一致性行为，故对 5xx 做指数退避重试。
        """
        data = {
            "path": remote_path,
            "size": str(size),
            "isdir": "0",
            "rtype": str(rtype),
            "uploadid": uploadid,
            "block_list": json.dumps(block_list),
        }
        attempts = 5  # 首次 + 4 次重试，退避 2/4/8/16s
        last_err: BaiduHttpError | None = None
        for attempt in range(attempts):
            resp = self._client.post(
                FILE_API,
                params={"method": "create", "access_token": self._get_token()},
                data=data,
            )
            if resp.status_code < 500:
                payload = self._json(resp)  # 4xx 会带响应体抛出；2xx 正常解析
                if payload.get("errno", 0) != 0:
                    raise BaiduApiError("create", payload.get("errno", -1), payload)
                return payload
            # 5xx：分片合并未就绪，退避后重试
            body = (resp.text or "")[:300]
            last_err = BaiduHttpError(resp.status_code, FILE_API, body or "<空响应体>")
            if attempt < attempts - 1:
                wait = 2 ** (attempt + 1)
                log.warning(
                    "create 返回 HTTP %s（分片可能未就绪），%ss 后重试 (%d/%d): %s",
                    resp.status_code, wait, attempt + 1, attempts - 1, remote_path,
                )
                time.sleep(wait)
        raise last_err  # type: ignore[misc]

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

    def list_all(self, remote_dir: str, limit: int = 1000) -> list[dict]:
        """递归列出目录下所有条目（listall，自动翻页）。

        比自己递归调 list 省大量请求。普通权限应用对该接口的可用性需实测，
        不可用时调用方应回退到 list_dir 自行递归。
        """
        out: list[dict] = []
        start = 0
        while True:
            resp = self._client.get(
                FILE_API,
                params={
                    "method": "listall",
                    "access_token": self._get_token(),
                    "path": remote_dir,
                    "recursion": "1",
                    "start": str(start),
                    "limit": str(limit),
                },
            )
            payload = self._json(resp)
            if payload.get("errno", 0) != 0:
                raise BaiduApiError("listall", payload.get("errno", -1), payload)
            batch = payload.get("list", [])
            out.extend(batch)
            # has_more 为 0/缺失即到底；另防接口不返回 has_more 时靠空批次收敛
            if not payload.get("has_more") or not batch:
                return out
            start += len(batch)

    # --- 取下载直链 -----------------------------------------------------

    def filemetas(self, fs_ids: list[int], dlink: bool = True) -> list[dict]:
        """批量取文件元信息；dlink=True 时返回下载直链。

        dlink 有效期约 8 小时，且使用时必须：
          1) 手工拼接 `&access_token=`——不能走 httpx params，否则已签名的
             query 会被重新编码导致 errno 31023 sign error；
          2) 带 User-Agent: pan.baidu.com；
          3) 跟随 302 跳转。
        见 download.py::stream_dlink。
        """
        if not fs_ids:
            return []
        resp = self._client.get(
            MULTIMEDIA_API,
            params={
                "method": "filemetas",
                "access_token": self._get_token(),
                "fsids": json.dumps(fs_ids),
                "dlink": "1" if dlink else "0",
            },
        )
        payload = self._json(resp)
        if payload.get("errno", 0) != 0:
            raise BaiduApiError("filemetas", payload.get("errno", -1), payload)
        return payload.get("list", [])

    # --- 删除 -----------------------------------------------------------

    def delete(self, remote_paths: list[str]) -> dict:
        """删除网盘文件/目录（filemanager opera=delete）。

        用于中转区人工清理。async=1 走异步任务，返回 taskid；小批量通常立即完成。
        """
        if not remote_paths:
            return {}
        resp = self._client.post(
            FILE_API,
            params={
                "method": "filemanager",
                "access_token": self._get_token(),
                "opera": "delete",
            },
            data={
                "async": "1",
                "filelist": json.dumps(remote_paths),
                "ondup": "fail",
            },
        )
        payload = self._json(resp)
        if payload.get("errno", 0) != 0:
            raise BaiduApiError("filemanager.delete", payload.get("errno", -1), payload)
        return payload

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
        # 不直接 raise_for_status：百度即便返回 4xx/5xx，响应体里通常仍带
        # errno 与提示，直接抛 HTTP 状态会把真实原因吞掉，难以定位。
        if resp.status_code >= 400:
            body: object
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = (resp.text or "")[:800]
            raise BaiduHttpError(resp.status_code, str(resp.url).split("?")[0], body)
        return resp.json()
