"""打包上传服务：勾选结果文件 -> tar 到 scratch -> 上传网盘 -> 生成分享链接 -> 清理。

权限与隔离：
  - 待打包文件先经白名单 + 防穿越校验，再以登录用户身份 stat 确认可读。
  - tar 以登录用户身份执行（run_as_user），读源文件受 OS 权限约束。
  - 每个任务一个独立 scratch 子目录，root 运行时 chown 给目标用户以便其写入 tar。
  - 上传读 tar 在主进程（root 可读用户文件）；完成后无论成败都清理 scratch。

进度分配：打包 0~40%，上传 40~90%，分享 90~100%。
"""
from __future__ import annotations

import os
import shutil
import time
from typing import Dict, List

from ..config import get_settings
from ..fs.browser import path_size, stat_path
from ..logger import get_logger
from ..netdisk.engine import NetdiskEngine
from ..privilege.actas import resolve_user, run_as_user
from ..tasks.manager import TaskHandle, register_task

log = get_logger(__name__)


def _validate_paths(user: str, paths: List[str], roots: List[str]) -> List[str]:
    """校验每个路径在白名单内且用户可访问，返回归一化路径。"""
    if not paths:
        raise ValueError("未选择任何文件")
    norm_paths = []
    for p in paths:
        info = stat_path(user, p, roots)  # 含白名单 + 防穿越 + 以用户身份 stat
        norm_paths.append(info["path"])
    return norm_paths


def _common_base(paths: List[str]) -> str:
    """选中文件的公共父目录，作为 tar 的 -C 基准以保留相对结构。"""
    if len(paths) == 1:
        return os.path.dirname(paths[0])
    base = os.path.commonpath(paths)
    # commonpath 可能返回某个被选中的目录本身；tar -C 需要目录
    return base if os.path.isdir(base) else os.path.dirname(base)


def _bin_pack(items: List[tuple], threshold: int) -> List[Dict]:
    """贪心首次适配：把 (path, size) 分到多个包，每包总量不超过 threshold。

    按大小降序放置；单个条目本身超过阈值时独占一个包（best-effort，可能仍超
    网盘单文件上限，由调用方告警）。返回 [{"paths": [...], "size": int}, ...]。
    """
    bins: List[Dict] = []
    for path, size in sorted(items, key=lambda x: x[1], reverse=True):
        target = None
        for b in bins:
            if b["size"] + size <= threshold:
                target = b
                break
        if target is None:
            bins.append({"paths": [path], "size": size})
        else:
            target["paths"].append(path)
            target["size"] += size
    return bins


def _volume_name(base: str, idx: int, total: int) -> str:
    """多包时为归档名插入 .partN 后缀；单包保持原名。"""
    if total <= 1:
        return base
    if base.endswith(".tar.gz"):
        stem = base[: -len(".tar.gz")]
        return f"{stem}.part{idx + 1}.tar.gz"
    root, ext = os.path.splitext(base)
    return f"{root}.part{idx + 1}{ext}"


def _make_scratch(user: str, task_id: str) -> str:
    """创建任务专属 scratch 子目录；root 运行时 chown 给目标用户。"""
    s = get_settings()
    os.makedirs(s.scratch_dir, exist_ok=True)
    task_dir = os.path.join(s.scratch_dir, task_id)
    os.makedirs(task_dir, exist_ok=True)
    if os.geteuid() == 0:
        ident = resolve_user(user)
        os.chown(task_dir, ident.uid, ident.gid)
    return task_dir


@register_task("package_upload")
def package_upload(handle: TaskHandle, params: dict) -> Dict:
    """打包上传任务主体。

    params:
      paths:    List[str] 待打包的绝对路径（文件或目录）
      archive:  str       归档文件名（不含路径），如 case2_results.tar.gz
      pwd:      str|None   分享提取码，留空自动生成
      period:   int        分享有效天数，0=永久
    """
    user = handle.owner
    s = get_settings()
    roots = s.fs_root_list

    paths = _validate_paths(user, params["paths"], roots)
    base_archive = params.get("archive") or f"hpc_{int(time.time())}.tar.gz"
    if "/" in base_archive:
        raise ValueError("归档文件名不能包含路径分隔符")

    # --- 按大小分组：总量超过阈值时拆成多个包，每包不超过阈值 ---
    threshold = s.package_volume_bytes
    sized = [(p, path_size(user, p, roots)) for p in paths]
    bins = _bin_pack(sized, threshold)
    total_bins = len(bins)
    for b in bins:
        if b["size"] > threshold and len(b["paths"]) == 1:
            log.warning(
                "单个条目 %s 约 %.2f GB 超过分卷阈值，可能超出网盘单文件上限",
                b["paths"][0],
                b["size"] / 1024**3,
            )

    task_dir = _make_scratch(user, handle.id)
    engine = NetdiskEngine()
    packages: List[Dict] = []
    try:
        # --- 逐包：打包（以用户身份 tar）-> 上传 -> 删本地归档 ---
        for i, b in enumerate(bins):
            archive = _volume_name(base_archive, i, total_bins)
            archive_path = os.path.join(task_dir, archive)

            handle.update(
                phase=f"打包中 {i + 1}/{total_bins}",
                progress=5 + (i / total_bins) * 35,
            )
            base = _common_base(b["paths"])
            rel_members = [os.path.relpath(p, base) for p in b["paths"]]
            argv = ["tar", "-czf", archive_path, "-C", base, *rel_members]
            proc = run_as_user(user, argv, timeout=3600)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"打包失败: {proc.stderr.decode('utf-8', 'replace')[:500]}"
                )
            if not os.path.exists(archive_path):
                raise RuntimeError("打包未生成归档文件")

            remote = engine.remote_path_for(user, archive)

            def _on_progress(done: int, total: int, _i: int = i) -> None:
                seg = 50 / total_bins  # 每个包占 40~90% 的等分区间
                pct = 40 + _i * seg + (done / max(total, 1)) * seg
                handle.update(
                    phase=f"上传中 包{_i + 1}/{total_bins} {done}/{total}",
                    progress=pct,
                )

            engine.upload(archive_path, remote, progress_cb=_on_progress)
            packages.append(
                {
                    "archive": archive,
                    "remote_path": remote,
                    "size": os.path.getsize(archive_path),
                }
            )
            # 上传完成即删本地归档，把磁盘占用限制在单个包大小内
            try:
                os.remove(archive_path)
            except OSError:
                pass

        # --- 一个分享链接含全部包（一个提取码）---
        handle.update(phase="生成分享链接", progress=92)
        share = engine.share_many(
            [p["remote_path"] for p in packages],
            pwd=params.get("pwd"),
            period=int(params.get("period", 7)),
        )
    finally:
        engine.close()
        # 无论成败，清理 scratch
        shutil.rmtree(task_dir, ignore_errors=True)

    handle.update(phase="完成", progress=100)
    return {
        "packages": packages,
        "count": len(packages),
        "total_size": sum(p["size"] for p in packages),
        "link": share["link"],
        "pwd": share["pwd"],
        "period": share["period"],
    }
