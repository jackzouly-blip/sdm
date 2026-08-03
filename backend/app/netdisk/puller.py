"""入站同步任务：分享链接 → 中转区 → 集群落盘。

一轮同步做四件事：
  1. 打开分享（verify 提取码）并递归列目录——这一步同时验证链接是否还活着
  2. 与清单按 fs_id 比对，得出"用户这轮新加了什么"
  3. 把待处理文件分批转存到中转区的**独立批次目录**
  4. 用官方 OAuth dlink 逐个下载到集群，以属主身份落盘

中转区按批次分目录是正确性要求而非整洁癖：中转区不清空时，客户把同名文件换成
新版本（新 fs_id、同名）会让 ondup 跳过转存，于是下载到上一版的旧文件却报成功。
批次目录让同名永不相撞。详见 docs/netdisk-inbound-sync-plan.md。

失败分三类，处理方式完全不同：
  链接失效  → link_state=invalid，前端提示**用户**重新提交链接
  cookie 失效 → link_state=auth_failed，这是全局故障，得**运维**换 BDUSS
  配额/网络 → 保持 ok，文件留在 failed 状态，下轮自动重试
"""
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

from ..logger import get_logger
from ..tasks.manager import TaskHandle, register_task

log = get_logger(__name__)


# 同步库由 main 在启动时注入（与 set_dispatcher / set_streamer 同一模式），
# 避免任务函数反向 import main 造成循环依赖。
_db = None


def set_sync_db(db) -> None:
    global _db
    _db = db


def _sync_db():
    return _db


def local_dir_for(owner: str, name: str) -> str:
    """派生集群落点：<inbox 根>/<属主>/<源名>。

    与 sim_workdir_base_dir 同样的思路——不让用户手填集群绝对路径，
    且保证天然落在 fs_roots 白名单内（否则写入会被 403 拒掉）。
    """
    from ..config import get_settings

    from .download import _safe_filename

    base = get_settings().netdisk_inbox_base_dir.rstrip("/")
    return f"{base}/{owner}/{_safe_filename(name)}"


def remote_batch_dir(owner: str, share_id: int, batch_id: str) -> str:
    """中转区批次目录：<中转根>/<属主>/<源 id>/<批次>。"""
    from ..config import get_settings

    base = get_settings().netdisk_inbox_remote.rstrip("/")
    return f"{base}/{owner}/{share_id}/{batch_id}"


def _normalize(entries: List[dict]) -> List[dict]:
    """把 share/list 的原始条目收敛成清单需要的字段。"""
    out = []
    for e in entries:
        path = e.get("path") or ""
        out.append({
            "fs_id": str(e.get("fs_id")),
            "share_path": path,
            "filename": e.get("server_filename") or path.rsplit("/", 1)[-1],
            "size": int(e.get("size", 0) or 0),
            "md5": e.get("md5") or "",
        })
    return out


def run_sync(share_id: int, handle: Optional[TaskHandle] = None) -> Dict:
    """执行一轮同步。返回 {new, transferred, downloaded, failed}。

    调用方必须已通过 try_begin_sync 认领；本函数负责 end_sync 收尾。
    """
    from ..config import get_settings
    from .download import download_file, part_size, resolve_dlinks
    from .engine import NetdiskEngine
    from .share_client import BaiduShareClient, ShareError

    db = _sync_db()
    if db is None:
        raise RuntimeError("同步库未初始化")
    row = db.get(share_id)
    if row is None:
        raise RuntimeError(f"分享源不存在: {share_id}")

    s = get_settings()
    owner = row["owner"]
    local_dir = row["local_dir"]
    stats = {"new": 0, "transferred": 0, "downloaded": 0, "failed": 0}

    def _phase(text: str, pct: float) -> None:
        if handle is not None:
            handle.update(phase=text, progress=pct)

    bduss, stoken = db.resolve_credentials()
    if not bduss:
        db.end_sync(share_id, "failed", "平台未配置网盘凭据", "auth_failed")
        raise RuntimeError("平台未配置网盘凭据（管理员可在「网盘数据」页配置）")

    # --- 1/2：列分享 + 增量比对 -----------------------------------------
    _phase("打开分享链接", 2)
    try:
        with BaiduShareClient(bduss, stoken) as cli:
            sess = cli.open_share(row["share_url"], row["pwd"])
            _phase("列出分享内容", 6)
            entries = cli.walk_share(
                sess, row["sub_dir"], max_entries=s.netdisk_pull_max_entries
            )
            # 能列出内容说明 cookie 还活着——顺手把凭据状态刷成 ok，
            # 让管理页在凭据恢复后自动消掉红标，不必人工点“测试连接”
            db.mark_credential_state("ok")
            files = _normalize(entries)
            stats["new"] = db.add_seen(share_id, files)
            log.info("分享源 %s：列到 %d 个文件，新增 %d 个",
                     share_id, len(files), stats["new"])

            pending = db.pending_files(share_id)
            if not pending:
                db.end_sync(share_id, "done", "", "ok")
                _phase("没有待同步的文件", 100)
                return stats

            # --- 3：分批转存到独立批次目录 ---------------------------
            batch_id = time.strftime("%Y%m%d-%H%M%S")
            dest_remote = remote_batch_dir(owner, share_id, batch_id)
            todo = [r for r in pending if r["state"] != "transferred"]
            n_batches = max(1, (len(todo) + s.netdisk_pull_batch - 1) // s.netdisk_pull_batch)
            for bi in range(n_batches):
                chunk = todo[bi * s.netdisk_pull_batch:(bi + 1) * s.netdisk_pull_batch]
                if not chunk:
                    continue
                _phase(f"转存 {bi + 1}/{n_batches} 批（{len(chunk)} 个）",
                       6 + 9 * (bi + 1) / n_batches)
                fs_ids = [r["fs_id"] for r in chunk]
                try:
                    cli.transfer(sess, [int(x) for x in fs_ids], dest_remote)
                except ShareError as e:
                    if e.is_quota_exceeded:
                        # 配额触顶：本批留待下轮，已转存的继续走下载
                        db.mark(share_id, fs_ids, state="failed",
                                error=f"转存配额受限 errno={e.errno}")
                        stats["failed"] += len(fs_ids)
                        log.warning("分享源 %s 转存配额触顶 errno=%s，本批推迟",
                                    share_id, e.errno)
                        continue
                    raise
                db.mark(share_id, fs_ids, state="transferred", batch_id=batch_id,
                        error="")
                stats["transferred"] += len(fs_ids)
    except ShareError as e:
        link_state = "ok"
        if e.is_auth_failure:
            link_state = "auth_failed"
            # 回写凭据状态：这是全局故障（所有源都会挂），管理页要显眼提示该换 cookie
            db.mark_credential_state("auth_failed")
            log.error("网盘 cookie 已失效，入站同步全局中断：%s", e)
        elif e.is_link_invalid:
            link_state = "invalid"
        db.end_sync(share_id, "failed", str(e)[:500], link_state)
        raise

    # --- 4：从中转区下载到集群 -------------------------------------------
    ready = [r for r in db.pending_files(share_id) if r["state"] == "transferred"]
    if not ready:
        db.end_sync(share_id, "done", "", "ok")
        _phase("完成", 100)
        return stats

    engine = NetdiskEngine()
    try:
        token = engine.oauth.get_access_token()
        # 按批次目录分组，逐批解析 dlink（dlink 有效期约 8 小时，用时才取）
        by_batch: Dict[str, List] = {}
        for r in ready:
            by_batch.setdefault(r["batch_id"], []).append(r)

        total = len(ready)
        idx = 0
        for bid, rows in by_batch.items():
            dest_remote = remote_batch_dir(owner, share_id, bid)
            try:
                listed = engine.client.list_dir(dest_remote)
            except Exception as e:  # noqa: BLE001
                log.exception("列中转区失败 %s", dest_remote)
                db.mark(share_id, [r["fs_id"] for r in rows], state="failed",
                        error=f"中转区不可读: {e}"[:300])
                stats["failed"] += len(rows)
                continue
            # 中转后 fs_id 会变（是新的副本），只能按文件名对应
            by_name = {it.get("server_filename"): it for it in listed
                       if not it.get("isdir")}
            metas = resolve_dlinks(
                engine.client,
                [int(it["fs_id"]) for it in by_name.values()],
            )
            name_to_meta = {m.filename: m for m in metas.values()}

            for r in rows:
                idx += 1
                fname = r["filename"]
                base_pct = 15 + 80 * (idx - 1) / total
                seg = 80.0 / total
                _phase(f"下载 {idx}/{total}：{fname}", base_pct)
                rf = name_to_meta.get(fname)
                if rf is None:
                    db.mark(share_id, [r["fs_id"]], state="failed",
                            error="中转区未找到该文件（转存可能未完成）")
                    stats["failed"] += 1
                    continue
                # 分享侧的 md5 更可信（是客户原文件的），用它覆盖校验依据
                if r["md5"]:
                    rf.md5 = r["md5"]

                def _cb(done: int, tot: int, _b=base_pct, _s=seg) -> None:
                    if handle is not None:
                        handle.update(progress=_b + (done / max(tot, 1)) * _s)

                try:
                    resume = part_size(owner, local_dir, fname)
                    path = download_file(
                        rf, token, local_dir, owner,
                        resume_from=resume,
                        verify_md5=s.netdisk_pull_verify_md5,
                        progress_cb=_cb,
                    )
                except Exception as e:  # noqa: BLE001
                    log.exception("下载失败 share=%s file=%s", share_id, fname)
                    db.mark(share_id, [r["fs_id"]], state="failed",
                            error=str(e)[:300])
                    stats["failed"] += 1
                    continue
                db.mark(share_id, [r["fs_id"]], state="done", local_path=path,
                        error="")
                stats["downloaded"] += 1
    finally:
        engine.close()

    status = "done" if not stats["failed"] else "partial"
    db.end_sync(share_id, status, "", "ok")
    _phase("完成", 100)
    log.info("分享源 %s 同步结束: %s", share_id, stats)
    return stats


@register_task("netdisk_pull")
def netdisk_pull(handle: TaskHandle, params: dict) -> Dict:
    """异步同步任务。params: {share_id}"""
    share_id = int(params["share_id"])
    try:
        return run_sync(share_id, handle=handle)
    except Exception:
        db = _sync_db()
        if db is not None:
            r = db.get(share_id)
            # end_sync 已在 run_sync 内针对已知失败写过；这里兜底未预料的异常
            if r is not None and r["syncing"]:
                db.end_sync(share_id, "failed", "未捕获异常")
        raise
