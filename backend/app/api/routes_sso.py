"""OIDC 单点登录接口（4.2 P16）。规则见 app/oidc.py；未配置时只有 status 可用并返回未开启。"""

from __future__ import annotations

import base64
import json
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import get_db
from app.api.routes_auth import _client_ip, _set_cookies
from app.auth import issue_session
from app.models import User
from app.oidc import OidcConfig, OidcError, Provider

router = APIRouter(prefix="/api/auth/oidc", tags=["auth"])
FLOW_COOKIE = "yunshu_oidc"


def provider(request: Request) -> Provider | None:
    if not hasattr(request.app.state, "oidc_provider"):
        cfg = OidcConfig.from_env()
        request.app.state.oidc_provider = Provider(cfg) if cfg else None
    return request.app.state.oidc_provider


def _fail(message: str) -> RedirectResponse:
    response = RedirectResponse(f"/#/?sso_error={quote(message)}", status_code=303)
    response.delete_cookie(FLOW_COOKIE, path="/api/auth/oidc")
    return response


@router.get("/status")
def status(request: Request) -> dict:
    p = provider(request)
    return {"enabled": p is not None, "label": p.config.label if p else ""}


@router.get("/start")
def start(request: Request) -> RedirectResponse:
    p = provider(request)
    if p is None:
        return _fail("未开启单点登录")
    try:
        url, flow = p.start()
    except (OidcError, Exception) as exc:  # noqa: BLE001 - 身份提供方不可达等：回登录页提示
        return _fail(f"无法连接身份提供方：{type(exc).__name__}" if not isinstance(exc, OidcError) else str(exc))
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(FLOW_COOKIE, base64.urlsafe_b64encode(json.dumps(flow).encode()).decode(), max_age=600, httponly=True,
                        samesite="lax", secure=request.app.state.settings.secure_cookies, path="/api/auth/oidc")
    return response


@router.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "", db: Session = Depends(get_db)) -> RedirectResponse:
    p = provider(request)
    if p is None:
        return _fail("未开启单点登录")
    if error:
        return _fail(f"身份提供方返回错误：{error[:100]}")
    try:
        flow = json.loads(base64.urlsafe_b64decode(request.cookies.get(FLOW_COOKIE, "")))
    except (ValueError, json.JSONDecodeError):
        flow = {}
    if not flow or not code or not secrets.compare_digest(str(flow.get("state", "")), state):
        return _fail("登录已过期或来源不正确，请重新点击单点登录")
    try:
        claims = p.finish(code, flow)
    except OidcError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"无法完成单点登录：{type(exc).__name__}")

    subject = f"{p.config.issuer}|{claims['sub']}"[:255]
    groups = claims.get("groups") if isinstance(claims.get("groups"), list) else []
    role = p.config.role_for([str(g) for g in groups])
    user = db.scalar(select(User).where(User.external_subject == subject))
    if user is None:
        username = str(claims.get("preferred_username") or claims.get("email") or claims["sub"])[:100]
        if db.scalar(select(User).where(User.username == username)) is not None:
            audit.record(db, None, "auth.sso_conflict", summary=f"单点登录账号与本地账号 {username} 同名，已拒绝")
            db.commit()
            return _fail(f"平台里已有名为“{username}”的本地账号，请联系管理员处理后再用单点登录")
        user = User(username=username, display_name=str(claims.get("name") or "")[:200], role=role, password_hash="!",
                    auth_source="oidc", external_subject=subject, require_password_change=False, scope_all=p.config.scope_all)
        db.add(user)
        db.flush()
        audit.record(db, user, "auth.sso_user_created", target_type="user", target_id=user.id, summary=f"单点登录新建账号（{role}）")
    elif not user.is_active:
        return _fail("账号已停用")
    elif user.role != role:
        audit.record(db, user, "auth.sso_role_synced", target_type="user", target_id=user.id, summary=f"按身份提供方的组同步角色：{user.role} → {role}")
        user.role = role
    issued = issue_session(db, user, _client_ip(request))
    audit.record(db, user, "auth.login", target_type="user", target_id=user.id, summary="单点登录")
    response = RedirectResponse("/#/", status_code=303)
    _set_cookies(request, response, issued.token, issued.csrf_token)
    response.delete_cookie(FLOW_COOKIE, path="/api/auth/oidc")
    return response
