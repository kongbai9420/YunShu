"""请求依赖：数据库会话、当前用户、角色校验、CSRF。

浏览器用 HttpOnly 会话 Cookie；改数据的请求还必须带 X-CSRF-Token 头，值等于登录时下发的
CSRF 令牌（同时放在非 HttpOnly 的 yunshu_csrf Cookie 里供前端读取）。
权限在后端校验，不依赖前端隐藏按钮。
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth import resolve_session
from app.models import LoginSession, User

SESSION_COOKIE = "yunshu_session"
CSRF_COOKIE = "yunshu_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_db(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    with factory() as db:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


@dataclass
class Principal:
    user: User
    session: LoginSession


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    resolved = resolve_session(db, request.cookies.get(SESSION_COOKIE))
    if resolved is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录或会话已过期")
    login, user = resolved
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not hmac.compare_digest(sent, login.csrf_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF 校验失败")
    return Principal(user=user, session=login)


def current_user(principal: Principal = Depends(current_principal)) -> User:
    return principal.user


def require_roles(*roles: str) -> Callable[..., User]:
    def checker(principal: Principal = Depends(current_principal)) -> User:
        user = principal.user
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "当前角色无权执行此操作")
        if user.require_password_change:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "请先修改初始密码")
        return user

    return checker


# 读：三个角色都可以；写资产与凭据：管理员和运维；用户与审计：仅管理员
reader = require_roles("admin", "operator", "viewer")
editor = require_roles("admin", "operator")
admin_only = require_roles("admin")
