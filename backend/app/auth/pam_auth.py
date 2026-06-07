"""PAM 认证：用系统账号密码登录。

服务以 root 运行，可直接通过 PAM 校验任意系统用户的密码。
认证成功不等于授权——授权由后续所有操作以该用户身份执行来保证。
"""
from __future__ import annotations

import pam

from ..logger import get_logger
from ..privilege.actas import PrivilegeError, resolve_user

log = get_logger(__name__)

_pam = pam.pam()


def authenticate(username: str, password: str) -> bool:
    """校验系统账号密码。"""
    if not username or not password:
        return False
    # 先确认是真实系统用户，避免对不存在的用户做无谓 PAM 调用
    try:
        resolve_user(username)
    except PrivilegeError:
        log.warning("登录失败：用户不存在 %s", username)
        return False
    ok = _pam.authenticate(username, password, service="login")
    if not ok:
        log.warning("登录失败：密码错误 %s (pam: %s)", username, _pam.reason)
    return bool(ok)
