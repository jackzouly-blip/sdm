"""计算节点监控（仅管理员）。

  - GET  /nodes                         采集 `pbsnodes -a`，返回各节点状态。
  - POST /nodes/{name}/restart-services  ssh 到该节点重启 pbs 服务（pbs_mom / trqauthd）。

安全：两个接口都用管理员白名单收口。重启走 ssh 免密（后端 root 身份），节点名严格
校验为 pbsnodes 已知节点，避免注入到 ssh 目标主机。
"""
from __future__ import annotations

import re
import shutil
import subprocess

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth.session import current_user, is_admin_request
from ..config import get_settings
from ..logger import get_logger
from .pbsnodes import PbsnodesError, fetch_nodes

log = get_logger(__name__)
router = APIRouter(prefix="/nodes", tags=["nodes"])

# 合法节点名：字母/数字/点/连字符/下划线（主机名字符集），拒绝其余以防注入到 ssh 目标
_NODE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _require_admin(is_admin: bool) -> None:
    if not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权管理计算节点")


@router.get("")
def list_nodes(
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> dict:
    """采集并返回各计算节点状态。仅管理员可用。"""
    _require_admin(is_admin)
    try:
        nodes = fetch_nodes()
    except PbsnodesError as e:
        raise HTTPException(status_code=502, detail=str(e))
    # 汇总便于前端顶部展示
    summary = {
        "total": len(nodes),
        "up": sum(1 for n in nodes if n["health"] == "up"),
        "down": sum(1 for n in nodes if n["health"] == "down"),
        "offline": sum(1 for n in nodes if n["health"] == "offline"),
        "unknown": sum(1 for n in nodes if n["health"] == "unknown"),
    }
    return {"nodes": nodes, "summary": summary}


@router.post("/{name}/restart-services")
def restart_node_services(
    name: str,
    user: str = Depends(current_user),
    is_admin: bool = Depends(is_admin_request),
) -> dict:
    """ssh 到指定节点重启 pbs 服务（默认 pbs_mom trqauthd）。仅管理员可用。"""
    _require_admin(is_admin)

    if not _NODE_NAME_RE.match(name):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="非法的节点名")

    # 节点名必须是 pbsnodes 已知节点，避免 ssh 到任意主机
    try:
        known = {n["name"] for n in fetch_nodes()}
    except PbsnodesError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if name not in known:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未知节点")

    s = get_settings()
    services = s.node_restart_services.split()
    if not services:
        raise HTTPException(status_code=500, detail="未配置需重启的服务名")

    ssh = shutil.which("ssh")
    if not ssh:
        raise HTTPException(status_code=500, detail="未找到 ssh 命令")

    target = f"{s.node_ssh_user}@{name}"
    cmd = [
        ssh,
        "-o", "BatchMode=yes",             # 禁用交互，纯免密
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        target,
        "systemctl", "restart", *services,
    ]
    log.info("重启节点服务: node=%s services=%s 操作人=%s", name, " ".join(services), user)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=s.node_ssh_timeout, check=False
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail=f"ssh 到 {name} 超时")

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        log.warning("重启节点 %s 服务失败（rc=%s）：%s", name, proc.returncode, msg)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"重启失败: {msg[:300] or f'退出码 {proc.returncode}'}",
        )

    log.info("节点 %s 服务已重启（操作人 %s）", name, user)
    return {"node": name, "restarted": True, "services": services}
