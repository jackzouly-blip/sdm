"""打包上传路由 + 异步任务查询 + WebSocket 进度推送。"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel

from ..auth.session import current_user
from ..tasks.manager import TaskManager

# 导入以触发任务注册（register_task 装饰器）
from . import service  # noqa: F401

router = APIRouter(tags=["packaging"])


def _tm(request: Request) -> TaskManager:
    return request.app.state.task_manager


class PackageRequest(BaseModel):
    paths: List[str]
    archive: Optional[str] = None
    pwd: Optional[str] = None
    period: int = 7


class TaskIdResponse(BaseModel):
    task_id: str


@router.post("/package", response_model=TaskIdResponse)
def create_package(
    req: PackageRequest,
    request: Request,
    user: str = Depends(current_user),
) -> TaskIdResponse:
    if not req.paths:
        raise HTTPException(status_code=400, detail="未选择任何文件")
    task_id = _tm(request).submit(
        "package_upload",
        owner=user,
        params=req.model_dump(),
    )
    return TaskIdResponse(task_id=task_id)


@router.get("/tasks")
def list_tasks(request: Request, user: str = Depends(current_user)) -> list:
    return _tm(request).list_by_owner(user)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request, user: str = Depends(current_user)) -> dict:
    snap = _tm(request).snapshot(task_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if snap["owner"] != user:
        raise HTTPException(status_code=403, detail="无权访问该任务")
    return snap


@router.websocket("/ws/tasks/{task_id}")
async def task_progress_ws(websocket: WebSocket, task_id: str):
    """实时推送某任务进度。鉴权用 query 参数 token（WS 不便带 Authorization 头）。"""
    from ..auth.session import _decode  # 局部导入避免循环

    token = websocket.query_params.get("token", "")
    try:
        payload = _decode(token)
        user = payload["sub"]
    except Exception:  # noqa: BLE001
        await websocket.close(code=4401)
        return

    tm: TaskManager = websocket.app.state.task_manager
    snap = tm.snapshot(task_id)
    if snap is None or snap["owner"] != user:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _on_update(s: dict) -> None:
        # 回调在工作线程触发，跨线程投递到事件循环
        loop.call_soon_threadsafe(queue.put_nowait, s)

    tm.subscribe(task_id, _on_update)
    try:
        await websocket.send_json(snap)  # 先发当前快照
        # 已是终态则直接结束
        while snap["status"] not in ("success", "failed", "interrupted"):
            snap = await queue.get()
            await websocket.send_json(snap)
    except WebSocketDisconnect:
        pass
    finally:
        tm.unsubscribe(task_id, _on_update)
