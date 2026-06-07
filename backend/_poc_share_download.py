"""POC：从他人百度分享链接 -> 转存到本账号 -> 下载到本地。

这是"他人分享"路径的完整验证脚本（命令行）。前半段(verify/list/transfer)走
网页私有接口，需要本账号的网页 cookie；后半段下载复用官方 OAuth dlink（已验证）。

凭据（环境变量，避免明文写入代码/聊天）：
  BAIDU_BDUSS   必填，网页登录 cookie BDUSS
  BAIDU_STOKEN  转存需要，网页 cookie STOKEN
  BAIDU_TRANSFER_DIR  转存落点（本账号网盘目录），默认 "/"

用法：
  HPC_FS_ROOTS=/tmp/hpc-sandbox \
  BAIDU_BDUSS=xxx BAIDU_STOKEN=yyy \
  .venv/bin/python _poc_share_download.py "<分享链接>" "<提取码>" "<本地目标目录>"

注意：BDUSS/STOKEN 必须是与门户 OAuth 同一个百度账号，转存后才能用官方直链下载。
脚本对每一步打印 errno/原始返回，便于定位接口漂移。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

import httpx

from app.netdisk.engine import NetdiskEngine

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
PAN = "https://pan.baidu.com"
FILEMETA = "https://pan.baidu.com/rest/2.0/xpan/multimedia"


def _surl_of(url: str) -> str:
    """从分享链接提取 surl（/s/1 后面的部分，不含前导 1）。"""
    m = re.search(r"/s/1([\w-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"surl=([\w-]+)", url)
    if m:
        return m.group(1)
    raise SystemExit(f"无法从链接解析 surl: {url}")


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit("用法: _poc_share_download.py <分享链接> <提取码> <本地目标目录>")
    share_url, pwd, local_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    bduss = os.getenv("BAIDU_BDUSS")
    stoken = os.getenv("BAIDU_STOKEN")
    transfer_dir = os.getenv("BAIDU_TRANSFER_DIR", "/")
    if not bduss:
        raise SystemExit("缺少环境变量 BAIDU_BDUSS")

    surl = _surl_of(share_url)
    share_page = f"{PAN}/s/1{surl}"
    print(f"[0] surl={surl} 分享页={share_page} 转存落点={transfer_dir}")

    cookies = {"BDUSS": bduss}
    if stoken:
        cookies["STOKEN"] = stoken
    cli = httpx.Client(
        headers={"User-Agent": BROWSER_UA, "Referer": share_page},
        cookies=cookies,
        timeout=60,
        follow_redirects=True,
    )

    # [1] bdstoken
    r = cli.get(
        f"{PAN}/api/gettemplatevariable",
        params={
            "clienttype": "0",
            "app_id": "250528",
            "web": "1",
            "fields": '["bdstoken"]',
        },
    )
    j = r.json()
    bdstoken = (j.get("result") or {}).get("bdstoken")
    print(f"[1] gettemplatevariable errno={j.get('errno')} bdstoken={bool(bdstoken)}")
    if not bdstoken:
        print("    原始返回:", j)
        return

    # [2] verify 提取码 -> 得到 BDCLND cookie
    r = cli.post(
        f"{PAN}/share/verify",
        params={
            "surl": surl,
            "t": str(int(time.time() * 1000)),
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "bdstoken": bdstoken,
            "clienttype": "0",
        },
        data={"pwd": pwd, "vcode": "", "vcode_str": ""},
    )
    j = r.json()
    print(f"[2] share/verify errno={j.get('errno')} 返回={j}")
    if j.get("errno") != 0:
        print("    提取码校验失败（errno!=0），终止。")
        return
    randsk = j.get("randsk")
    if randsk:
        cli.cookies.set("BDCLND", randsk, domain=".baidu.com")

    # [3] 解析 shareid / uk（从分享页 HTML）
    html = cli.get(share_page).text
    shareid = _first(html, r'"shareid"\s*:\s*(\d+)')
    uk = _first(html, r'"share_uk"\s*:\s*"?(\d+)') or _first(html, r'"uk"\s*:\s*(\d+)')
    print(f"[3] 分享页解析 shareid={shareid} uk={uk}")

    # [4] list 分享文件
    r = cli.get(
        f"{PAN}/share/list",
        params={
            "shorturl": surl,
            "root": "1",
            "web": "1",
            "app_id": "250528",
            "channel": "chunlei",
            "clienttype": "0",
            "page": "1",
            "num": "100",
            "order": "time",
            "desc": "1",
            "bdstoken": bdstoken,
        },
    )
    j = r.json()
    files = j.get("list") or []
    print(f"[4] share/list errno={j.get('errno')} 文件数={len(files)}")
    for f in files:
        print(f"      - {f.get('server_filename')} fs_id={f.get('fs_id')} isdir={f.get('isdir')} size={f.get('size')}")
    if not files:
        print("    无文件或 list 失败，原始返回:", j)
        return
    fsidlist = [int(f["fs_id"]) for f in files]

    # [5] transfer 转存到本账号
    r = cli.post(
        f"{PAN}/share/transfer",
        params={
            "shareid": shareid,
            "from": uk,
            "bdstoken": bdstoken,
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "clienttype": "0",
            "ondup": "newcopy",
            "async": "1",
        },
        data={"fsidlist": json.dumps(fsidlist), "path": transfer_dir},
    )
    j = r.json()
    print(f"[5] share/transfer errno={j.get('errno')} 返回={j}")
    if j.get("errno") != 0:
        print("    转存失败，终止（常见：errno=-33 转存数量超限 / 12 部分失败 / -6 鉴权）。")
        return

    # [6] 转存成功 -> 用官方 OAuth dlink 下载到本地
    os.makedirs(local_dir, exist_ok=True)
    eng = NetdiskEngine()
    try:
        token = eng.oauth.get_access_token()
        listed = eng.client.list_dir(transfer_dir.rstrip("/") or "/")
        names = {f.get("server_filename") for f in files}
        targets = [
            it for it in listed if it.get("server_filename") in names and not it.get("isdir")
        ]
        print(f"[6] 本账号 {transfer_dir} 命中待下载文件 {len(targets)} 个")
        for it in targets:
            fs_id = it["fs_id"]
            meta = httpx.get(
                FILEMETA,
                params={"method": "filemetas", "access_token": token, "fsids": f"[{fs_id}]", "dlink": "1"},
                headers={"User-Agent": "pan.baidu.com"},
                timeout=60,
            ).json()
            dlink = (meta.get("list") or [{}])[0].get("dlink")
            if not dlink:
                print(f"      ✗ {it['server_filename']} 未拿到 dlink: {meta}")
                continue
            sep = "&" if "?" in dlink else "?"
            out = os.path.join(local_dir, it["server_filename"])
            with httpx.stream("GET", f"{dlink}{sep}access_token={token}", headers={"User-Agent": "pan.baidu.com"}, timeout=300, follow_redirects=True) as resp:
                with open(out, "wb") as fp:
                    for chunk in resp.iter_bytes(1 << 20):
                        fp.write(chunk)
            print(f"      ✓ 已下载 {out} ({os.path.getsize(out)} 字节)")
    finally:
        eng.close()
    print("✅ POC 完成")


def _first(text: str, pattern: str):
    m = re.search(pattern, text)
    return m.group(1) if m else None


if __name__ == "__main__":
    main()
