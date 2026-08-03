"""Phase 0 探针：验证「客户分享链接 → 平台账号中转 → 集群落盘」整条入站链路。

与 _poc_share_download.py（一次性验证脚本）不同，本探针直接调用**生产模块**
（app/netdisk/share_client.py、download.py），所以跑通它等于跑通了后续要用的代码。

回答 docs/netdisk-inbound-sync-plan.md 里 Phase 0 的五个问题：
  Q1 分享文件夹新增文件后，share/list 能否看到？  -> scan + rescan 子命令
  Q2 转存能否落到 /apps/HPC/ 且 OAuth 能读到？    -> pull 子命令
  Q3 dlink 实际下载吞吐是多少？                    -> pull 子命令（逐文件计时）
  Q4 单次转存的 fs_id 数量上限？                   -> pull 子命令（--batch 试探）
  Q5 百度返回的 md5 与本地实算是否一致？           -> pull 子命令（校验开关）

凭据：优先读配置（HPC_NETDISK_BDUSS/HPC_NETDISK_STOKEN），回退到环境变量
      BAIDU_BDUSS/BAIDU_STOKEN，避免明文写进代码。

用法（在生产服务器上跑，本地测不出真实吞吐）：
  # Q1：先扫一遍存快照
  HPC_FS_ROOTS=/data .venv/bin/python _poc_pull.py scan "<分享链接>" "<提取码>"
  # …让客户往分享文件夹里再放一个文件…
  HPC_FS_ROOTS=/data .venv/bin/python _poc_pull.py rescan "<分享链接>" "<提取码>"

  # Q2/Q3/Q4/Q5：转存 + 下载 + 计时
  HPC_FS_ROOTS=/data .venv/bin/python _poc_pull.py pull "<分享链接>" "<提取码>" \
      --user leiyou --dest /data/tmp/pull-probe --limit 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict

from app.config import get_settings
from app.netdisk.download import download_file, md5_as_user, resolve_dlinks
from app.netdisk.share_client import BaiduShareClient, ShareError

SNAPSHOT = os.path.join(os.path.dirname(__file__), "state", "_poc_pull_snapshot.json")


def _creds() -> tuple:
    """取平台凭据：与服务端同源——先查同步库（管理页配的），再回退到环境变量。

    管理页把凭据存进 netdisk_sync.db，只读 env 会拿不到，探针就成了"配好了却
    还说没配"。这里复用 NetdiskSyncDB.resolve_credentials，保证与生产同一套优先级。
    """
    s = get_settings()
    db_path = os.path.join(os.path.dirname(__file__), "state", "netdisk_sync.db")
    if os.path.exists(db_path):
        from app.netdisk.sync_db import NetdiskSyncDB

        db = NetdiskSyncDB(db_path)
        try:
            bduss, stoken = db.resolve_credentials()
        finally:
            db.close()
        if bduss:
            return bduss, stoken
    bduss = s.netdisk_bduss or os.getenv("BAIDU_BDUSS", "")
    stoken = s.netdisk_stoken or os.getenv("BAIDU_STOKEN", "")
    if not bduss:
        raise SystemExit(
            "缺少凭据：请先在门户「网盘数据 → 平台网盘凭据」配置，"
            "或设置 HPC_NETDISK_BDUSS / 环境变量 BAIDU_BDUSS"
        )
    return bduss, stoken


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def _walk(cli: BaiduShareClient, url: str, pwd: str, sub_dir: str) -> tuple:
    sess = cli.open_share(url, pwd)
    print(f"  会话建立: {sess}")
    t0 = time.time()
    files = cli.walk_share(sess, sub_dir)
    print(f"  递归遍历 {len(files)} 个文件，耗时 {time.time() - t0:.1f}s")
    return sess, files


def cmd_scan(args) -> None:
    """Q1 前半：列出分享内容并存快照。"""
    bduss, stoken = _creds()
    with BaiduShareClient(bduss, stoken) as cli:
        _, files = _walk(cli, args.url, args.pwd, args.sub_dir)
    total = sum(int(f.get("size", 0)) for f in files)
    for f in files[:20]:
        print(f"    - {f.get('path')} fs_id={f.get('fs_id')} "
              f"size={_human(int(f.get('size', 0)))} md5={f.get('md5')}")
    if len(files) > 20:
        print(f"    …其余 {len(files) - 20} 个略")
    print(f"  合计 {len(files)} 文件 / {_human(total)}")

    os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
    snap = {
        "url": args.url,
        "sub_dir": args.sub_dir,
        "taken_at": time.time(),
        "files": {str(f["fs_id"]): f.get("path") for f in files},
    }
    with open(SNAPSHOT, "w", encoding="utf-8") as fp:
        json.dump(snap, fp, ensure_ascii=False, indent=2)
    print(f"\n✅ 快照已存: {SNAPSHOT}")
    print("   现在让客户往分享文件夹里再放一个文件，然后跑 rescan。")


def cmd_rescan(args) -> None:
    """Q1 后半：重新列出并与快照 diff——这是"持续同步"能否成立的判据。"""
    if not os.path.exists(SNAPSHOT):
        raise SystemExit(f"没有快照，先跑 scan：{SNAPSHOT}")
    with open(SNAPSHOT, encoding="utf-8") as fp:
        snap = json.load(fp)
    old = snap["files"]

    bduss, stoken = _creds()
    with BaiduShareClient(bduss, stoken) as cli:
        _, files = _walk(cli, args.url, args.pwd, args.sub_dir)
    new = {str(f["fs_id"]): f.get("path") for f in files}

    added = {k: v for k, v in new.items() if k not in old}
    removed = {k: v for k, v in old.items() if k not in new}
    print(f"\n  快照时间: {time.strftime('%F %T', time.localtime(snap['taken_at']))}")
    print(f"  新增 {len(added)} / 消失 {len(removed)} / 不变 {len(new) - len(added)}")
    for k, v in list(added.items())[:20]:
        print(f"    + {v} (fs_id={k})")
    for k, v in list(removed.items())[:20]:
        print(f"    - {v} (fs_id={k})")

    print("\n" + "=" * 60)
    if added:
        print("✅ Q1 结论：分享链接是活的，新增文件能被 share/list 看到。")
        print("   → 「定时轮询分享链接 + 按 fs_id 增量转存」方案成立。")
    else:
        print("⚠️  Q1 未观察到新增。请确认：客户确实往**被分享的那个目录**里加了文件；")
        print("   若确实加了却看不到，则分享是静态快照，整个持续同步方案需要重做。")


def cmd_pull(args) -> None:
    """Q2/Q3/Q4/Q5：转存到中转区 → OAuth 下载 → 计时与校验。"""
    from app.netdisk.engine import NetdiskEngine

    s = get_settings()
    bduss, stoken = _creds()
    batch_id = time.strftime("%Y%m%d-%H%M%S")
    dest_remote = f"{s.netdisk_inbox_remote.rstrip('/')}/_probe/{batch_id}"
    dest_local = args.dest

    print(f"[0] 中转区落点: {dest_remote}")
    print(f"    集群落点:   {dest_local}  (以用户 {args.user} 身份写入)")

    # --- 列分享 ---
    with BaiduShareClient(bduss, stoken) as cli:
        sess, files = _walk(cli, args.url, args.pwd, args.sub_dir)
        if not files:
            raise SystemExit("分享内没有文件")
        picked = files[: args.limit] if args.limit else files
        by_name: Dict[str, dict] = {}
        for f in picked:
            by_name[str(f.get("server_filename") or f.get("path", "").rsplit("/", 1)[-1])] = f
        total = sum(int(f.get("size", 0)) for f in picked)
        print(f"\n[1] 选中 {len(picked)} 个文件 / {_human(total)} 用于转存")

        # --- Q2/Q4: 转存（先建目录，否则 errno=2 转存路径不存在）---
        fs_ids = [int(f["fs_id"]) for f in picked]
        from app.netdisk.puller import ensure_remote_dir

        print(f"[2] 建中转目录 {dest_remote}")
        ensure_remote_dir(dest_remote)
        print(f"[2] share/transfer → {dest_remote}（批量 {len(fs_ids)}）")
        t0 = time.time()
        try:
            res = cli.transfer(sess, fs_ids, dest_remote, ondup="skip")
        except ShareError as e:
            print(f"    ❌ 转存失败 errno={e.errno}: {e.payload}")
            if e.is_quota_exceeded:
                print(f"    → Q4：{len(fs_ids)} 个已超单次上限，减小 --batch 再试")
            if e.is_auth_failure:
                print("    → Q5：cookie 已失效，需更换 BDUSS/STOKEN")
            if e.is_link_invalid:
                print("    → 链接不可用（取消分享/改提取码/删目录）")
            raise SystemExit(1)
        print(f"    ✅ 转存成功，耗时 {time.time() - t0:.1f}s，返回 {res}")
        print(f"    → Q4：单次 {len(fs_ids)} 个 fs_id 通过（上限还需加大 --limit 试探）")

    # --- Q2 后半：OAuth 能否读到中转区 ---
    eng = NetdiskEngine()
    try:
        token = eng.oauth.get_access_token()
        print(f"\n[3] OAuth list 中转区 {dest_remote}")
        try:
            listed = eng.client.list_dir(dest_remote)
        except Exception as e:  # noqa: BLE001
            print(f"    ❌ OAuth 读不到中转区: {e}")
            print("    → Q2 结论：普通权限应用无法读该路径。")
            print("       若落点已在 /apps/HPC 下仍失败，需检查应用目录名是否匹配。")
            raise SystemExit(1)
        print(f"    ✅ 列到 {len(listed)} 个条目")
        print("    → Q2 结论：转存落点可写、OAuth 可读，链路打通。")

        fs_map = {int(it["fs_id"]): it for it in listed if not it.get("isdir")}
        metas = resolve_dlinks(eng.client, list(fs_map))
        print(f"[4] filemetas 解析到 {len(metas)} 个 dlink")

        # --- Q3/Q5：下载计时 + md5 校验 ---
        os.makedirs(dest_local, exist_ok=True)
        grand_bytes = 0
        grand_t0 = time.time()
        for fs_id, rf in metas.items():
            share_meta = by_name.get(rf.filename, {})
            share_md5 = str(share_meta.get("md5") or "")
            print(f"\n[5] 下载 {rf.filename}  {_human(rf.size)}")
            t0 = time.time()
            try:
                path = download_file(
                    rf, token, dest_local, args.user,
                    verify_md5=False,  # 先不拦，下面手工比对以便看清差异
                )
            except Exception as e:  # noqa: BLE001
                print(f"    ❌ 下载失败: {e}")
                continue
            dt = max(time.time() - t0, 0.001)
            grand_bytes += rf.size
            print(f"    ✅ {path}")
            print(f"    吞吐 {_human(rf.size / dt)}/s（{_human(rf.size)} / {dt:.1f}s）")

            local_md5 = md5_as_user(args.user, path)
            print(f"    md5  本地实算 {local_md5}")
            print(f"         filemetas {rf.md5 or '(无)'}")
            print(f"         share/list {share_md5 or '(无)'}")
            marks = []
            if rf.md5 and rf.md5.lower() == local_md5.lower():
                marks.append("filemetas 一致")
            if share_md5 and share_md5.lower() == local_md5.lower():
                marks.append("share/list 一致")
            print(f"    → Q5：{'、'.join(marks) if marks else '❌ 均不一致，md5 不可作为校验依据'}")

        if grand_bytes:
            gdt = max(time.time() - grand_t0, 0.001)
            print("\n" + "=" * 60)
            print(f"✅ Q3 结论：整体吞吐 {_human(grand_bytes / gdt)}/s "
                  f"（{_human(grand_bytes)} / {gdt:.1f}s）")
            print("   对照客户当前 HTTP 直传速率判断是否值得做。")
            print(f"   注意：本次用的平台账号会员等级会影响此值——换普通/SVIP 账号各测一次。")
        print(f"\n中转区探针目录 {dest_remote} 请手工清理。")
    finally:
        eng.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="网盘入站同步 Phase 0 探针")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("url", help="分享链接")
        p.add_argument("pwd", help="提取码")
        p.add_argument("--sub-dir", default="", help="只处理分享内的某个子目录")

    common(sub.add_parser("scan", help="Q1：列出分享内容并存快照"))
    common(sub.add_parser("rescan", help="Q1：重新列出并与快照 diff"))
    p = sub.add_parser("pull", help="Q2/Q3/Q4/Q5：转存 + 下载 + 计时")
    common(p)
    p.add_argument("--user", required=True, help="以该 POSIX 用户身份落盘")
    p.add_argument("--dest", required=True, help="集群本地目标目录")
    p.add_argument("--limit", type=int, default=3, help="只取前 N 个文件（0=全部）")

    args = ap.parse_args()
    {"scan": cmd_scan, "rescan": cmd_rescan, "pull": cmd_pull}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except ShareError as e:
        print(f"\n❌ 分享接口失败 errno={e.errno}", file=sys.stderr)
        print(f"   原始返回: {e.payload}", file=sys.stderr)
        sys.exit(1)
