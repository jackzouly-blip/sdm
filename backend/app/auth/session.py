"""会话：登录后签发 JWT，请求时校验并解析出当前用户。"""
from __future__ import annotations

import time
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Query, status
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


def current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """FastAPI 依赖：返回当前登录用户名，未登录/过期则 401。"""
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录"
        )
    try:
        payload = _decode(creds.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已过期"
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的令牌"
        )
    return payload["sub"]


def current_user_query(
    token: Optional[str] = Query(None),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """同 current_user，但允许 token 经 query 参数传入——供浏览器原生下载等
    无法设置 Authorization 头的场景（如 <a href> 直链下载）。"""
    raw = token or (creds.credentials if creds else None)
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    try:
        payload = _decode(raw)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已过期")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的令牌")
    return payload["sub"]
