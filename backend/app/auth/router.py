"""认证路由：登录 / 当前用户。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..config import get_settings
from ..privilege.actas import resolve_user
from .pam_auth import authenticate
from .session import current_user, issue_token

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest) -> LoginResponse:
    if not authenticate(req.username, req.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        )
    return LoginResponse(token=issue_token(req.username), username=req.username)


class MeResponse(BaseModel):
    username: str
    uid: int
    home: str
    is_admin: bool


@router.get("/me", response_model=MeResponse)
def me(user: str = Depends(current_user)) -> MeResponse:
    ident = resolve_user(user)
    return MeResponse(
        username=ident.name,
        uid=ident.uid,
        home=ident.home,
        is_admin=get_settings().is_admin(user),
    )
