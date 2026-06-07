"""命令行入口。

用法：
  python -m baidu_uploader.cli auth      # 首次授权 / 刷新并验证 token
  python -m baidu_uploader.cli whoami    # 打印当前网盘账号与容量
"""
from __future__ import annotations

import argparse
import sys

import os

import httpx

from .api.client import BaiduPanClient
from .auth.oauth import BaiduOAuth
from .config import load_config
from .core.uploader import Uploader
from .logger import get_logger, setup_logging
from .state.store import StateStore

log = get_logger(__name__)

# 校验 token 是否可用的接口
NAS_URL = "https://pan.baidu.com/rest/2.0/xpan/nas"
QUOTA_URL = "https://pan.baidu.com/api/quota"
# 百度 xpan 接口要求 User-Agent 含 pan.baidu.com，否则返回 unsupported api
BAIDU_HEADERS = {"User-Agent": "pan.baidu.com"}


def _check_token(access_token: str) -> None:
    """调用用户信息 + 配额接口，验证 token 真正可用。"""
    with httpx.Client(timeout=30, headers=BAIDU_HEADERS) as c:
        u = c.get(
            NAS_URL, params={"method": "uinfo", "access_token": access_token}
        ).json()
        if u.get("errno", 0) != 0:
            raise RuntimeError(f"uinfo 调用失败: {u}")
        print(f"账号: {u.get('baidu_name')} (uk={u.get('uk')}, vip={u.get('vip_type')})")

        q = c.get(
            QUOTA_URL,
            params={"access_token": access_token, "checkfree": 1, "checkexpire": 1},
        ).json()
        if q.get("errno", 0) == 0:
            used = q.get("used", 0) / 1024**3
            total = q.get("total", 0) / 1024**3
            print(f"容量: 已用 {used:.2f} GB / 共 {total:.2f} GB")


def cmd_auth(cfg) -> int:
    oauth = BaiduOAuth(cfg)
    token = oauth.get_access_token()
    print("\n✓ 已获得 access_token")
    _check_token(token)
    return 0


def cmd_whoami(cfg) -> int:
    oauth = BaiduOAuth(cfg)
    token = oauth.get_access_token()
    _check_token(token)
    return 0


def cmd_upload(cfg, local: str, remote: str | None) -> int:
    """上传单个本地文件。remote 缺省时放到 remote_dir 下，同名。"""
    oauth = BaiduOAuth(cfg)
    if remote is None:
        remote = f"{cfg.upload.remote_dir.rstrip('/')}/{os.path.basename(local)}"
    client = BaiduPanClient(oauth.get_access_token)
    store = StateStore(cfg.state.db_path)
    try:
        uploader = Uploader(
            client, cfg.upload.chunk_size, cfg.upload.concurrency, store=store
        )
        result = uploader.upload_file(local, remote)
    finally:
        client.close()
        store.close()
    if result.get("skipped"):
        print(f"\n✓ 已跳过（内容未变更）: {result.get('path')}")
    else:
        print(f"\n✓ 上传成功: {result.get('path')} (大小 {result.get('size')} 字节)")
    return 0


def cmd_share(cfg, remote: str, pwd: str, period: int) -> int:
    """为网盘文件创建分享链接。"""
    # 百度强制要求提取码，未指定时自动生成 4 位随机码
    if not pwd:
        import random
        import string

        pwd = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    oauth = BaiduOAuth(cfg)
    client = BaiduPanClient(oauth.get_access_token)
    try:
        fs_id = client.get_fsid(remote)
        res = client.create_share([fs_id], pwd=pwd, period=period)
    finally:
        client.close()
    period_txt = "永久" if period == 0 else f"{period} 天"
    print(f"\n✓ 分享创建成功")
    print(f"  文件: {remote}")
    print(f"  链接: {res.get('link')}")
    print(f"  提取码: {pwd}")
    print(f"  有效期: {period_txt}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baidu-uploader")
    parser.add_argument("-c", "--config", default=None, help="配置文件路径")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="首次授权 / 刷新并验证 token")
    sub.add_parser("whoami", help="打印当前网盘账号与容量")
    p_up = sub.add_parser("upload", help="上传单个本地文件")
    p_up.add_argument("local", help="本地文件路径")
    p_up.add_argument("-r", "--remote", default=None, help="网盘目标路径（默认放 remote_dir 下）")
    p_sh = sub.add_parser("share", help="为网盘文件创建分享链接")
    p_sh.add_argument("remote", help="网盘文件路径")
    p_sh.add_argument("-p", "--pwd", default="", help="提取码（4位）；留空则公开分享")
    p_sh.add_argument(
        "-d", "--period", type=int, default=7, help="有效天数，0=永久（默认7）"
    )

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.log.level, cfg.log.file)

    if args.cmd == "auth":
        return cmd_auth(cfg)
    if args.cmd == "whoami":
        return cmd_whoami(cfg)
    if args.cmd == "upload":
        return cmd_upload(cfg, args.local, args.remote)
    if args.cmd == "share":
        return cmd_share(cfg, args.remote, args.pwd, args.period)
    return 1


if __name__ == "__main__":
    sys.exit(main())
