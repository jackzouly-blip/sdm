"""d3plot 解析任务：以登录用户身份调用 extract.py，产物写入按用户+内容分桶的缓存。"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from ..config import get_settings
from ..logger import get_logger
from ..privilege.actas import resolve_user, run_as_user
from ..tasks.manager import TaskHandle, register_task

log = get_logger(__name__)
EXTRACT_SCRIPT = str(Path(__file__).parent / "extract.py")


def cache_key(realpath: str, mtime: float, size: int, family_sig: str = "") -> str:
    """按真实路径 + mtime + 大小 + d3plot 家族签名生成缓存键；源文件或新增 state
    文件变了键就变（自动失效）。family_sig 捕获仿真推进时新增的 d3plot01.. 状态文件
    （主文件 d3plot 自身 mtime/size 通常不变，故必须纳入家族签名）。"""
    return hashlib.sha1(
        f"{realpath}|{int(mtime)}|{int(size)}|{family_sig}".encode()
    ).hexdigest()[:16]


def asset_dir(user: str, key: str) -> Path:
    return Path(get_settings().d3plot_cache_dir) / user / key


def _python() -> str:
    return get_settings().d3plot_python or sys.executable


@register_task("d3plot_view")
def d3plot_view(handle: TaskHandle, params: dict) -> dict:
    # 执行/缓存身份用 run_as（管理员=root，可解析任意用户作业）；任务属主仅用于进度鉴权
    user = params.get("run_as") or handle.owner
    s = get_settings()
    d3 = params["d3plot"]          # 已校验的 d3plot 文件绝对路径
    key = params["key"]
    out = asset_dir(user, key)

    handle.update(phase="准备", progress=5)
    out.mkdir(parents=True, exist_ok=True)
    # root 运行时把产物目录交给目标用户，降权子进程才能写入
    if os.geteuid() == 0:
        ident = resolve_user(user)
        os.chown(out, ident.uid, ident.gid)

    handle.update(phase="解析 d3plot（lasso）", progress=15)
    max_states = int(params.get("max_states", s.d3plot_max_states))
    max_tris = int(params.get("max_tris", s.d3plot_max_tris))  # 0=不减面(高精度)
    argv = [_python(), EXTRACT_SCRIPT, d3, str(out), str(max_states), str(max_tris)]
    proc = run_as_user(user, argv, timeout=s.d3plot_timeout)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[-1000:]
        raise RuntimeError(f"d3plot 解析失败: {err}")

    summary = {}
    try:
        line = proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
        summary = json.loads(line)
    except Exception:  # noqa: BLE001
        log.warning("解析摘要读取失败，回退读 model.json")
        try:
            summary = json.loads((out / "model.json").read_text("utf-8"))
        except Exception:  # noqa: BLE001
            pass

    handle.update(phase="完成", progress=100)
    return {"key": key, "d3plot": d3, **{k: summary.get(k) for k in
            ("n_verts", "n_tris", "n_states", "n_beam_lines", "n_parts", "fields", "bytes", "elapsed")}}
