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


def issue_scoped_token(
    username: str, scope: str, ttl_seconds: int, **claims: object
) -> str:
    """签发**受限**令牌：只能用于声明的 scope，且以分钟计过期。

    用途是把凭据交给本机以外的执行方（如桌面端 vektor3d 拉源文件、回传产物）。
    交常规会话 JWT 出去等于把整个账号（8 小时、全部接口）借出去；这里的令牌带
    `scp` 声明，`_resolve_principal` 默认拒绝任何带 `scp` 的令牌，只有显式声明
    接受该 scope 的接口才认——即便泄露，能做的也只有那一件事。
    """
    s = get_settings()
    now = int(time.time())
    payload = {
        "sub": username,
        "scp": scope,
        "iat": now,
        "exp": now + max(1, int(ttl_seconds)),
        **claims,
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def _decode(token: str) -> dict:
    s = get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])


def _decode_or_401(raw_token: str) -> dict:
    try:
        return _decode(raw_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已过期")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的令牌")


def _resolve_principal(
    raw_token: Optional[str],
    act_as: Optional[str],
    *,
    allow_scopes: Optional[dict] = None,
) -> str:
    """把凭据解析成目标用户名；未登录/过期/无效则 401。

    带 `scp` 的受限令牌**默认一律拒绝**：只有把该 scope 列进 allow_scopes
    （scope → bindings，如 {"gid": 路径上的那一个}）的接口才接受它，并逐条核对
    bindings。默认拒绝是关键——反过来做成"默认接受、个别接口排除"，漏掉一个
    接口就等于把受限令牌变成了通行证。
    """
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
    payload = _decode_or_401(raw_token)
    scope = payload.get("scp")
    if scope:
        if scope not in (allow_scopes or {}):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"该令牌仅限 {scope} 用途，不能用于此接口",
            )
        for key, expected in (allow_scopes or {})[scope].items():
            if payload.get(key) != expected:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"令牌未授权访问该资源（{key} 不匹配）",
                )
    return payload["sub"]


def resolve_scoped_principal(
    raw_token: Optional[str],
    act_as: Optional[str],
    *,
    scope: str,
    bindings: dict,
) -> str:
    """供业务模块构造"既接受常规用户令牌、也接受某个受限令牌"的依赖。

    放在这里而不是各业务模块自己解 JWT：签发与校验必须共用一处，
    否则 scp 的默认拒绝语义迟早会在某个模块里被绕过。
    """
    return _resolve_principal(raw_token, act_as, allow_scopes={scope: bindings})


def resolve_multi_scoped_principal(
    raw_token: Optional[str],
    act_as: Optional[str],
    *,
    allow_scopes: dict,
) -> str:
    """同上，但一个接口可接受多种受限令牌（scope → bindings 各查各的）。

    场景：需求文档下载既收单文档的 doc.analyze 票据，也收整项目的 ai.read 票据。
    """
    return _resolve_principal(raw_token, act_as, allow_scopes=allow_scopes)


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


def principal_is_admin(
    user: str, raw_token: Optional[str], portal_admin: Optional[str]
) -> bool:
    """给定已解析出的用户，判断这次调用是否拥有管理员视图。

    单独提出来是因为并非所有接口都用 current_user 解析主体（几何端点还接受受限
    票据）；管理员判据必须只有这一处实现，否则各接口迟早各判各的。
    """
    s = get_settings()
    if s.is_admin(user):
        return True
    if s.agent_api_token and raw_token == s.agent_api_token and portal_admin == "1":
        return True
    return False


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

    ⚠ 不要与 `current_user_query` 搭配：它依赖 `current_user`（只读 Authorization 头），
    凑在一起会让"允许 query 传 token"的端点在这一步就 401——而报错完全看不出与
    管理员判定有关。这类端点应像 sim 的几何端点那样，用 `principal_is_admin()`
    自建一个与该端点同源的管理员依赖。
    """
    return principal_is_admin(user, creds.credentials if creds else None, portal_admin)
