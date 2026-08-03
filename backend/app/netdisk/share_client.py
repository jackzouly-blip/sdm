"""百度网盘「分享链接」网页私有接口封装。

入站同步的第一段：打开用户的分享链接 → 列目录 → 把选中的文件转存到平台账号。
这一段**没有官方开放接口**，只能走网页端私有接口，因此：
  - 需要平台账号的网页 cookie（BDUSS / STOKEN），不是 OAuth token；
  - 接口随时可能漂移，故每一步都记录 errno 与原始返回，便于定位；
  - 所有私有接口调用集中在本文件，漂移时只需改这一处。

第二段（把转存到平台账号的文件下载到服务器）走官方 OAuth dlink，见 download.py。

原型：backend/_poc_share_download.py（本文件由其提炼而来）。
"""
from __future__ import annotations

import json
import re
import time
from typing import Dict, List, Optional

import httpx

from ..logger import get_logger

log = get_logger(__name__)

PAN = "https://pan.baidu.com"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
WEB_APP_ID = "250528"  # 网页端固定 app_id


class ShareError(RuntimeError):
    """分享接口调用失败。errno 语义见百度网页端（非开放平台文档）。

    已知：-9 提取码错误 / -12 分享已失效 / -33 转存数量超限 /
          12 部分文件转存失败 / -6 鉴权失败(cookie 失效) / 2 参数错误
    """

    def __init__(self, where: str, errno: int, payload: object):
        super().__init__(f"{where} 失败 errno={errno}: {payload}")
        self.where = where
        self.errno = errno
        self.payload = payload

    @property
    def is_auth_failure(self) -> bool:
        """cookie 失效——需要运维更换 BDUSS/STOKEN，不是用户能自助解决的。"""
        return self.errno in (-6,)

    @property
    def is_link_invalid(self) -> bool:
        """链接本身不可用——用户取消了分享、改了提取码或删除了文件夹。"""
        return self.errno in (-9, -12, -21, 105)

    @property
    def is_quota_exceeded(self) -> bool:
        """转存配额触顶——退避后重试或减小批量。"""
        return self.errno in (-33, -32, 12)


class ShareSession:
    """一个已通过提取码校验的分享会话。

    surl/shareid/uk 是后续 list 与 transfer 都要带的三件套；bdstoken 绑定 cookie。
    BDCLND（提取码凭证）存活在 client 的 cookiejar 里，有效期有限，
    故本对象**不要长期缓存**，每轮同步重新 open 一次。
    """

    def __init__(self, surl: str, shareid: str, uk: str, bdstoken: str):
        self.surl = surl
        self.shareid = shareid
        self.uk = uk
        self.bdstoken = bdstoken

    @property
    def page_url(self) -> str:
        return f"{PAN}/s/1{self.surl}"

    def __repr__(self) -> str:  # 便于日志定位
        return f"<ShareSession surl={self.surl} shareid={self.shareid} uk={self.uk}>"


def parse_surl(url: str) -> str:
    """从分享链接提取 surl（/s/1 后面的部分，不含前导 1）。"""
    m = re.search(r"/s/1([\w-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"surl=([\w-]+)", url)
    if m:
        return m.group(1)
    raise ValueError(f"无法从链接解析 surl: {url}")


class BaiduShareClient:
    """平台账号的网页会话。用完即 close。"""

    def __init__(self, bduss: str, stoken: Optional[str] = None, timeout: int = 60):
        if not bduss:
            raise ValueError("缺少 BDUSS：入站同步需要平台账号的网页 cookie")
        cookies = {"BDUSS": bduss}
        if stoken:
            cookies["STOKEN"] = stoken
        self._client = httpx.Client(
            headers={"User-Agent": BROWSER_UA},
            cookies=cookies,
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BaiduShareClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 会话建立 -----------------------------------------------------

    def _bdstoken(self) -> str:
        r = self._client.get(
            f"{PAN}/api/gettemplatevariable",
            params={
                "clienttype": "0",
                "app_id": WEB_APP_ID,
                "web": "1",
                "fields": '["bdstoken"]',
            },
        )
        j = _as_json(r)
        token = (j.get("result") or {}).get("bdstoken")
        if not token:
            # 拿不到 bdstoken 基本等同于 cookie 已失效
            raise ShareError("gettemplatevariable", j.get("errno", -6), j)
        return token

    def check(self) -> dict:
        """凭据自检：不需要分享链接，只验证 cookie 还能不能换到 bdstoken。

        供管理员配置页的「测试连接」按钮用——填错或过期时当场知道，
        而不是等到某次同步失败才发现。返回账号昵称/容量（拿得到的话）。
        """
        self._client.headers["Referer"] = f"{PAN}/disk/home"
        self._bdstoken()  # 拿不到即视为 cookie 无效，内部会抛 ShareError
        info: dict = {"ok": True}
        try:  # 附带账号信息，便于确认配的是不是预期的那个账号
            r = self._client.get(f"{PAN}/rest/2.0/xpan/nas",
                                 params={"method": "uinfo"})
            j = r.json()
            if j.get("errno") == 0:
                info["username"] = j.get("baidu_name") or j.get("netdisk_name") or ""
        except Exception:  # noqa: BLE001
            pass  # 附加信息拿不到不影响自检结论
        return info

    def open_share(self, share_url: str, pwd: str) -> ShareSession:
        """校验提取码并解析 shareid/uk，返回可用于 list/transfer 的会话。"""
        surl = parse_surl(share_url)
        page_url = f"{PAN}/s/1{surl}"
        self._client.headers["Referer"] = page_url
        bdstoken = self._bdstoken()

        # 校验提取码 → 拿 BDCLND 写回 cookiejar
        r = self._client.post(
            f"{PAN}/share/verify",
            params={
                "surl": surl,
                "t": str(int(time.time() * 1000)),
                "channel": "chunlei",
                "web": "1",
                "app_id": WEB_APP_ID,
                "bdstoken": bdstoken,
                "clienttype": "0",
            },
            data={"pwd": pwd, "vcode": "", "vcode_str": ""},
        )
        j = _as_json(r)
        if j.get("errno") != 0:
            raise ShareError("share/verify", j.get("errno", -1), j)
        randsk = j.get("randsk")
        if randsk:
            self._client.cookies.set("BDCLND", randsk, domain=".baidu.com")

        # 从分享页 HTML 解析 shareid / uk
        html = self._client.get(page_url).text
        shareid = _first(html, r'"shareid"\s*:\s*(\d+)')
        uk = _first(html, r'"share_uk"\s*:\s*"?(\d+)') or _first(html, r'"uk"\s*:\s*(\d+)')
        if not shareid or not uk:
            raise ShareError("share/page", -12, "分享页未解析出 shareid/uk（链接可能已失效）")
        return ShareSession(surl, shareid, uk, bdstoken)

    # ---- 列目录 -------------------------------------------------------

    def list_share(
        self, sess: ShareSession, sub_dir: str = "", page_size: int = 100
    ) -> List[Dict]:
        """列出分享内某个目录的直接子项（自动翻页，不递归）。

        sub_dir 为空 = 分享根目录（用 root=1），否则用 dir=<绝对路径>。
        返回条目含 fs_id / server_filename / isdir / size / md5 / path。
        """
        out: List[Dict] = []
        page = 1
        while True:
            params = {
                "shorturl": sess.surl,
                "web": "1",
                "app_id": WEB_APP_ID,
                "channel": "chunlei",
                "clienttype": "0",
                "page": str(page),
                "num": str(page_size),
                "order": "name",
                "desc": "0",
                "bdstoken": sess.bdstoken,
            }
            if sub_dir:
                params["dir"] = sub_dir
            else:
                params["root"] = "1"
            r = self._client.get(f"{PAN}/share/list", params=params)
            j = _as_json(r)
            if j.get("errno") != 0:
                raise ShareError("share/list", j.get("errno", -1), j)
            batch = j.get("list") or []
            out.extend(batch)
            if len(batch) < page_size:
                return out
            page += 1

    def walk_share(
        self, sess: ShareSession, sub_dir: str = "", max_entries: int = 20000
    ) -> List[Dict]:
        """递归遍历分享目录，返回**文件**条目（目录本身不返回）。

        分享侧没有 listall，只能自己递归。max_entries 是防炸保险：
        客户分享一个几万文件的目录时，先失败得明明白白，好过把内存和接口配额吃光。
        """
        files: List[Dict] = []
        stack = [sub_dir]
        seen_dirs = set()
        while stack:
            cur = stack.pop()
            if cur in seen_dirs:
                continue
            seen_dirs.add(cur)
            for item in self.list_share(sess, cur):
                if item.get("isdir"):
                    stack.append(item.get("path") or "")
                else:
                    files.append(item)
                    if len(files) > max_entries:
                        raise ShareError(
                            "share/walk", -1,
                            f"分享内文件数超过上限 {max_entries}，请让客户按子目录分批分享",
                        )
        return files

    # ---- 转存 ---------------------------------------------------------

    def transfer(
        self,
        sess: ShareSession,
        fs_ids: List[int],
        dest_dir: str,
        ondup: str = "skip",
    ) -> Dict:
        """把分享内的文件转存到平台账号的 dest_dir。

        ondup 默认 skip：中转区按批次分目录后同名不会相撞，skip 只是额外保险。
        **不要用 newcopy**——它会生成 `xxx(1).ext`，我们按路径就再也定位不到它。

        调用方负责分批（单次 fs_ids 数量有上限，实测值见 docs/netdisk-inbound-sync-plan.md）
        以及 dest_dir 的预先创建。
        """
        if not fs_ids:
            return {"errno": 0, "extra": {}}
        r = self._client.post(
            f"{PAN}/share/transfer",
            params={
                "shareid": sess.shareid,
                "from": sess.uk,
                "bdstoken": sess.bdstoken,
                "channel": "chunlei",
                "web": "1",
                "app_id": WEB_APP_ID,
                "clienttype": "0",
                "ondup": ondup,
                "async": "1",
            },
            data={"fsidlist": json.dumps(fs_ids), "path": dest_dir},
        )
        j = _as_json(r)
        if j.get("errno") != 0:
            raise ShareError("share/transfer", j.get("errno", -1), j)
        return j


def _as_json(resp: httpx.Response) -> dict:
    """解析响应体。网页接口出错时也常带 errno，故不直接 raise_for_status。"""
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        body = (resp.text or "")[:500]
        raise ShareError(
            str(resp.url).split("?")[0], -1,
            f"HTTP {resp.status_code} 返回非 JSON: {body}",
        ) from None


def _first(text: str, pattern: str) -> Optional[str]:
    m = re.search(pattern, text)
    return m.group(1) if m else None
