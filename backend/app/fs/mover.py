"""跨文件系统移动的异步任务。

同一文件系统内的移动是 os.rename，瞬时完成，路由里同步做掉即可。
跨文件系统（如 inbox 落点与工作目录不在同一挂载）必须逐字节复制，对 GB 级
CAE 数据是分钟级操作，放在请求里必然超时——故走 TaskManager，进度与打包/
网盘同步同构，前端用 pollTask 拿进度。
"""
from __future__ import annotations

from typing import Dict

from ..logger import get_logger
from ..tasks.manager import TaskHandle, register_task

log = get_logger(__name__)


@register_task("fs_move")
def fs_move(handle: TaskHandle, params: dict) -> Dict:
    """跨文件系统移动。params: {user, paths, dst_dir, roots}"""
    from .browser import move_paths

    user = params["user"]
    paths = params["paths"]
    dst_dir = params["dst_dir"]
    roots = params["roots"]

    def _cb(done: int, total: int, name: str) -> None:
        pct = 100.0 * done / max(total, 1)
        handle.update(phase=(f"移动 {done + 1}/{total}：{name}" if name else "完成"),
                      progress=pct)

    result = move_paths(user, paths, dst_dir, roots, progress_cb=_cb)
    log.info("跨文件系统移动完成 user=%s 成功 %d 失败 %d",
             user, len(result["moved"]), len(result["failed"]))
    return result
