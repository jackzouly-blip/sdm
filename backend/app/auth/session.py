"""会话：登录后签发 JWT，请求时校验并解析出当前用户。

支持两种凭据，统一解析成"目标用户名"（act-as）：
  1) 按用户 JWT（既有方式）：token 自身携带 sub=用户名。
  2) agent 级 Bearer token（HPC_AGENT_API_TOKEN）：供 3dix 门户作为可信内部
     调用方使用；目标用户名经 X-Act-As-User 头传入，agent 据此 setuid。
"""
from __future__ import annotations

import time
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import get_settings

_bearer = HTTPBearer(auto_error=False)


def issue_token(username: str) -> str:
    s = get_settings()
    now = int(time.time())
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + s.session_ttl_minutes * 60,
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def _decode(token: str) -> dict:
    s = get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])


def _resolve_principal(raw_token: Optional[str], act_as: Optional[str]) -> str:
    """把凭据解析成目标用户名；未登录/过期/无效则 401。"""
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    s = get_settings()
    # agent 级 token：可信门户内部调用，目标用户名由 X-Act-As-User 指定
    if s.agent_api_token and raw_token == s.agent_api_token:
        if not act_as:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="agent 调用缺少 X-Act-As-User 头",
            )
        return act_as
    # 按用户 JWT
    try:
        payload = _decode(raw_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已过期")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的令牌")
    return payload["sub"]


def current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    act_as: Optional[str] = Header(None, alias="X-Act-As-User"),
) -> str:
    """FastAPI 依赖：返回当前操作的目标用户名，未登录/过期则 401。"""
    return _resolve_principal(creds.credentials if creds else None, act_as)


def current_user_query(
    token: Optional[str] = Query(None),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    act_as: Optional[str] = Header(None, alias="X-Act-As-User"),
) -> str:
    """同 current_user，但允许 token 经 query 参数传入——供浏览器原生下载等
    无法设置 Authorization 头的场景（如 <a href> 直链下载）。"""
    raw = token or (creds.credentials if creds else None)
    return _resolve_principal(raw, act_as)


def is_admin_request(
    user: str = Depends(current_user),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    portal_admin: Optional[str] = Header(None, alias="X-Hpc-Portal-Admin"),
) -> bool:
    """当前调用是否拥有管理员视图（看全部作业 / 机时统计）。

    两种来源：
      ① agent 侧 HPC_ADMIN_USERS 白名单命中目标用户名；
      ② 可信门户（agent token）通过 X-Hpc-Portal-Admin: 1 断言调用者是门户管理员。
    门户断言仅在持有 agent token 时才被采信，普通按用户 JWT 无法借此提权。
    """
    s = get_settings()
    if s.is_admin(user):
        return True
    raw = creds.credentials if creds else None
    if s.agent_api_token and raw == s.agent_api_token and portal_admin == "1":
        return True
    return False
