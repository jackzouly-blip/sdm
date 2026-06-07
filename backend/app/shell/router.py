"""在线 shell：仅管理员可用的交互式 Web 终端。

通过 WebSocket 连接，后端为管理员本人启动一个降权后的 PTY 登录 shell：
  - 鉴权：query 参数 token（WS 不便带 Authorization 头），解出用户名后再用
    settings.is_admin 校验；非管理员直接关闭连接。
  - 数据通道：二进制帧承载终端 IO（键盘输入 / 终端输出）；JSON 文本帧承载
    控制消息（目前仅 resize）。
  - 生命周期：任一端断开即清理 —— 移除读监听、关闭 fd、杀掉子 shell。

安全：真·交互式终端等价于给管理员的受控远程执行，已用管理员白名单收口，并
对会话起止记审计日志。生产环境后端须以 root 运行，降权才能落到管理员本人。
"""
from __future__ import annotations

import asyncio
import json
import os
import signal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth.session import _decode
from ..config import get_settings
from ..logger import get_logger
from ..privilege.actas import PrivilegeError, fork_pty_as_user, set_winsize

router = APIRouter(tags=["shell"])
log = get_logger(__name__)

# 单次从 PTY 读取的最大字节数
_READ_CHUNK = 65536


@router.websocket("/ws/shell")
async def shell_ws(websocket: WebSocket) -> None:
    settings = get_settings()

    # 1) 鉴权：token -> 用户名
    token = websocket.query_params.get("token", "")
    try:
        user = _decode(token)["sub"]
    except Exception:  # noqa: BLE001
        await websocket.close(code=4401)
        return

    # 2) 仅管理员可用
    if not settings.is_admin(user):
        await websocket.close(code=4403)
        return

    await websocket.accept()

    # 3) 启动降权 PTY 登录 shell
    try:
        pid, fd = fork_pty_as_user(user)
    except PrivilegeError as e:
        await websocket.send_json({"type": "error", "message": str(e)})
        await websocket.close(code=1011)
        return

    log.info("在线 shell 会话开始: user=%s pid=%s", user, pid)
    loop = asyncio.get_event_loop()
    os.set_blocking(fd, False)

    out_queue: asyncio.Queue[bytes] = asyncio.Queue()
    shell_closed = asyncio.Event()

    def _on_fd_readable() -> None:
        # 事件循环线程内回调：读 PTY 输出投递到队列，EOF 即标记 shell 退出
        try:
            data = os.read(fd, _READ_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            try:
                loop.remove_reader(fd)
            except Exception:  # noqa: BLE001
                pass
            shell_closed.set()
            return
        out_queue.put_nowait(data)

    loop.add_reader(fd, _on_fd_readable)

    async def pump_output() -> None:
        # 顺序把 PTY 输出发往浏览器（单写者，保证字节顺序）
        while True:
            data = await out_queue.get()
            await websocket.send_bytes(data)

    async def pump_input() -> None:
        # 浏览器 -> PTY：二进制帧写入 shell；JSON 文本帧处理 resize
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data is not None:
                os.write(fd, data)
                continue
            text = msg.get("text")
            if text:
                try:
                    ev = json.loads(text)
                    if ev.get("type") == "resize":
                        set_winsize(fd, int(ev["rows"]), int(ev["cols"]))
                except Exception:  # noqa: BLE001
                    pass

    output_task = asyncio.create_task(pump_output())
    input_task = asyncio.create_task(pump_input())
    closed_task = asyncio.create_task(shell_closed.wait())

    try:
        # 任一结束即收场：浏览器断开、shell 退出，或发送失败
        await asyncio.wait(
            {input_task, output_task, closed_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except WebSocketDisconnect:
        pass
    finally:
        for t in (output_task, input_task, closed_task):
            t.cancel()
        try:
            loop.remove_reader(fd)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError, OSError):
            pass
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("在线 shell 会话结束: user=%s pid=%s", user, pid)
