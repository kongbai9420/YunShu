from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import CSRF_COOKIE, SESSION_COOKIE, Principal, current_principal, get_db
from app.auth import (
    LoginThrottle,
    hash_password,
    issue_session,
    revoke_session,
    revoke_user_sessions,
    verify_password,
)
from app.models import User
from app.schemas import LoginIn, LoginOut, PasswordChangeIn, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 没有该用户时也做一次哈希，避免用响应时间判断用户名是否存在
_DUMMY_HASH = hash_password("not-a-real-password")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _set_cookies(request: Request, response: Response, token: str, csrf: str) -> None:
    secure = request.app.state.settings.secure_cookies
    max_age = 24 * 3600
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="lax", secure=secure, path="/")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False, samesite="lax", secure=secure, path="/")


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> LoginOut:
    throttle: LoginThrottle = request.app.state.login_throttle
    ip = _client_ip(request)
    remaining = throttle.remaining_lock(ip, body.username)
    if remaining:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"登录失败次数过多，请在 {remaining} 秒后再试")

    user = db.scalar(select(User).where(User.username == body.username))
    # 单点登录账号（4.2 P16）不能用密码登录；同样做一次哈希，响应时间与不存在的账号一致
    local = user is not None and user.auth_source == "local"
    ok = verify_password(body.password, user.password_hash if local else _DUMMY_HASH) and local
    if not user or not ok or not user.is_active:
        throttle.record_failure(ip, body.username)
        audit.record(db, None, "auth.login_failed", target_type="user", summary="登录失败", detail={"username": body.username})
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")

    throttle.clear(ip, body.username)
    issued = issue_session(db, user, ip)
    audit.record(db, user, "auth.login", target_type="user", target_id=user.id, summary="登录")
    _set_cookies(request, response, issued.token, issued.csrf_token)
    return LoginOut(user=UserOut.model_validate(user), csrf_token=issued.csrf_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> None:
    revoke_session(db, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


@router.get("/me", response_model=LoginOut)
def me(principal: Principal = Depends(current_principal)) -> LoginOut:
    return LoginOut(user=UserOut.model_validate(principal.user), csrf_token=principal.session.csrf_token)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: PasswordChangeIn, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)
) -> None:
    user = principal.user
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "原密码不正确")
    if body.old_password == body.new_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码不能与原密码相同")
    user.password_hash = hash_password(body.new_password)
    user.require_password_change = False
    revoke_user_sessions(db, user, keep=principal.session)
    audit.record(db, user, "auth.password_changed", target_type="user", target_id=user.id, summary="修改密码")
