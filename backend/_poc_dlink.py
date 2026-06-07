"""POC：验证用现有 OAuth access_token 通过 filemetas?dlink=1 下载自有文件。

这是"他人分享转存到本账号后"的最后一步下载能力验证，不涉及网页 cookie。
运行：HPC_FS_ROOTS=/tmp/hpc-sandbox .venv/bin/python _poc_dlink.py <网盘文件名>
"""
import sys

import httpx

from app.netdisk.engine import NetdiskEngine

FILEMETA = "https://pan.baidu.com/rest/2.0/xpan/multimedia"
HEADERS = {"User-Agent": "pan.baidu.com"}


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "binpack_test.part1.tar.gz"
    eng = NetdiskEngine()
    try:
        token = eng.oauth.get_access_token()
        remote = eng.remote_path_for("leiyou", name)
        print("远端路径:", remote)
        fs_id = eng.client.get_fsid(remote)
        print("fs_id:", fs_id)

        # filemetas 拿 dlink
        r = httpx.get(
            FILEMETA,
            params={
                "method": "filemetas",
                "access_token": token,
                "fsids": f"[{fs_id}]",
                "dlink": "1",
            },
            headers=HEADERS,
            timeout=60,
        )
        meta = r.json()
        print("filemetas errno:", meta.get("errno"))
        info = (meta.get("list") or [{}])[0]
        dlink = info.get("dlink")
        print("dlink:", (dlink[:80] + "...") if dlink else None)
        if not dlink:
            print("❌ 未拿到 dlink，原始返回:", meta)
            return

        # 下载（dlink 必须带 access_token + UA pan.baidu.com，会 302 跳转）
        # 注意：dlink 已含已签名 query，access_token 必须手工拼接，不能用 params=
        # （httpx 会重新编码已有 query 破坏签名 -> 31023 sign error）。
        sep = "&" if "?" in dlink else "?"
        dl_url = f"{dlink}{sep}access_token={token}"
        with httpx.stream(
            "GET",
            dl_url,
            headers=HEADERS,
            timeout=120,
            follow_redirects=True,
        ) as resp:
            print("下载 HTTP 状态:", resp.status_code)
            print("响应头 Content-Type:", resp.headers.get("content-type"))
            body = b""
            for chunk in resp.iter_bytes(65536):
                body += chunk
                if len(body) >= 200000:
                    break
            if resp.status_code == 200 and resp.headers.get(
                "content-type", ""
            ).startswith("application/"):
                print(f"✅ 已成功读取 {len(body)} 字节，下载链路可用")
            else:
                print(f"⚠️ 非正常下载，读取 {len(body)} 字节，内容预览:")
                print(body[:500].decode("utf-8", "replace"))
    finally:
        eng.close()


if __name__ == "__main__":
    main()
