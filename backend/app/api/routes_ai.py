"""AI 设置（4.3 P18a）：模型接口的增删改、连接自检、模型列表、全局开关与按功能分配、用量。只有管理员可用。"""
from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app import ai_runtime, audit
from app.ai import selfcheck
from app.ai.presets import QUIRKS, preset, presets
from app.ai.protocols import ERROR_TEXT, AiError, Endpoint, list_models
from app.api.deps import admin_only, get_db
from app.models import AiProvider, AiSetting, AiUsage, Credential, User
from app.services.credentials import create_credential, set_secret

router = APIRouter(prefix="/api/ai", tags=["ai"])
Protocol = Literal["openai", "anthropic", "gemini", "azure"]


def _check_url(url: str) -> str:
    """接口地址：只允许 http / https；明文 http 只能发往本机或内网，避免密钥明文经过公网。"""
    url = url.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        _ = parts.port
    except ValueError:
        raise HTTPException(422, "接口地址格式不正确") from None
    if parts.scheme not in ("http", "https") or not host or "<" in url or parts.username or parts.password:
        raise HTTPException(422, "接口地址需要完整的 http(s) 地址，且不能包含用户名密码或占位符")
    if parts.scheme == "http":
        try:
            ip = ipaddress.ip_address(host)
            local = ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            local = host == "localhost" or host.endswith((".local", ".lan", ".internal", ".localhost"))
        if not local:
            raise HTTPException(422, "明文 http 只允许本机或内网地址；公网接口请使用 https")
    return url


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    preset_id: str = Field("custom", max_length=64)
    protocol: Protocol
    base_url: str = Field(min_length=1, max_length=500)
    api_version: str = Field("", max_length=32)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(None, max_length=8000)
    enabled: bool = True
    quirks: list[str] = Field(default_factory=list)


class ProviderUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    protocol: Protocol | None = None
    base_url: str | None = Field(None, min_length=1, max_length=500)
    api_version: str | None = Field(None, max_length=32)
    model: str | None = Field(None, min_length=1, max_length=200)
    api_key: str | None = Field(None, max_length=8000)
    enabled: bool | None = None
    quirks: list[str] | None = None
    revision: int


def _out(p: AiProvider) -> dict:
    pre = preset(p.preset_id)
    return {"id": str(p.id), "name": p.name, "preset_id": p.preset_id, "preset_name": pre["name"] if pre else "自定义",
            "tested_preset": bool(pre and pre.get("tested")), "protocol": p.protocol, "base_url": p.base_url,
            "api_version": p.api_version, "model": p.model, "has_key": p.credential_id is not None, "enabled": p.enabled,
            "quirks": p.quirks or [], "capabilities": p.capabilities or {}, "check_result": p.check_result or [],
            "checked_at": p.checked_at.isoformat() if p.checked_at else None, "revision": p.revision}


def _store_key(db: Session, request: Request, provider: AiProvider, key: str) -> None:
    cred = db.get(Credential, provider.credential_id) if provider.credential_id else None
    if not key:
        if cred is not None:
            provider.credential_id = None
            db.delete(cred)
        return
    if cred is None:
        cred = create_credential(db, request.app.state.cipher, alias=f"AI 接口-{uuid.uuid4()}", kind="token", secret=key, notes="由 AI 设置管理的接口密钥")
        db.flush()
        provider.credential_id = cred.id
    else:
        set_secret(cred, request.app.state.cipher, key)


def _quirks(values: list[str]) -> list[str]:
    bad = set(values) - QUIRKS
    if bad:
        raise HTTPException(422, f"不认识的兼容选项：{', '.join(sorted(bad))}")
    return sorted(set(values))


def _get(db: Session, provider_id: uuid.UUID) -> AiProvider:
    p = db.get(AiProvider, provider_id)
    if p is None:
        raise HTTPException(404, "模型接口不存在")
    return p


def _ai_error(exc: AiError) -> HTTPException:
    return HTTPException(status.HTTP_502_BAD_GATEWAY, {"kind": exc.kind, "message": str(exc) or ERROR_TEXT.get(exc.kind, "调用失败")})


@router.get("/presets")
def get_presets(_: User = Depends(admin_only)) -> list[dict]:
    return presets()


@router.get("/providers")
def list_providers(_: User = Depends(admin_only), db: Session = Depends(get_db)) -> list[dict]:
    return [_out(p) for p in db.scalars(select(AiProvider).order_by(AiProvider.created_at, AiProvider.name))]


@router.post("/providers", status_code=status.HTTP_201_CREATED)
def create_provider(body: ProviderIn, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    if db.scalar(select(AiProvider.id).where(AiProvider.name == body.name)) is not None:
        raise HTTPException(409, "名称已存在")
    p = AiProvider(name=body.name, preset_id=body.preset_id if preset(body.preset_id) else "custom", protocol=body.protocol,
                   base_url=_check_url(body.base_url), api_version=body.api_version, model=body.model.strip(), enabled=body.enabled,
                   quirks=_quirks(body.quirks), capabilities={}, check_result=[])
    db.add(p)
    db.flush()
    if body.api_key:
        _store_key(db, request, p, body.api_key.strip())
    audit.record(db, actor, "ai.provider.create", target_type="ai_provider", target_id=p.id, summary=f"新增 AI 接口 {p.name}（{p.base_url}，{p.model}）")
    return _out(p)


@router.patch("/providers/{provider_id}")
def update_provider(provider_id: uuid.UUID, body: ProviderUpdate, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    p = _get(db, provider_id)
    if body.revision != p.revision:
        raise HTTPException(409, "已被其他人修改，请刷新后再改")
    changes = body.model_dump(exclude_unset=True, exclude={"revision", "api_key"})
    if "name" in changes and changes["name"] != p.name and db.scalar(select(AiProvider.id).where(AiProvider.name == changes["name"])) is not None:
        raise HTTPException(409, "名称已存在")
    if "base_url" in changes:
        changes["base_url"] = _check_url(changes["base_url"])
    if "quirks" in changes:
        changes["quirks"] = _quirks(changes["quirks"] or [])
    connection_changed = any(k in changes and changes[k] != getattr(p, k) for k in ("protocol", "base_url", "api_version", "model", "quirks"))
    for k, v in changes.items():
        setattr(p, k, v)
    if "api_key" in body.model_fields_set:
        _store_key(db, request, p, (body.api_key or "").strip())
        connection_changed = True
    if connection_changed:
        # 连接方式变了，原来的自检结果作废
        p.capabilities, p.check_result, p.checked_at = {}, [], None
    p.revision += 1
    audit.record(db, actor, "ai.provider.update", target_type="ai_provider", target_id=p.id,
                 summary=f"修改 AI 接口 {p.name}", detail={"fields": sorted([*changes, *(["api_key"] if "api_key" in body.model_fields_set else [])])})
    return _out(p)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(provider_id: uuid.UUID, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> None:
    p = _get(db, provider_id)
    settings = ai_runtime.settings_row(db)
    if settings.default_provider_id == p.id:
        settings.default_provider_id = None
    settings.features = {k: ({**v, "provider_id": None} if v.get("provider_id") == str(p.id) else v) for k, v in (settings.features or {}).items()}
    cred = db.get(Credential, p.credential_id) if p.credential_id else None
    audit.record(db, actor, "ai.provider.delete", target_type="ai_provider", target_id=p.id, summary=f"删除 AI 接口 {p.name}")
    db.delete(p)
    if cred is not None:
        db.delete(cred)


@router.post("/providers/{provider_id}/check")
def check_provider(provider_id: uuid.UUID, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    p = _get(db, provider_id)
    ep = ai_runtime.endpoint(db, request.app.state.cipher, p)
    client = getattr(request.app.state, "ai_http_client", None)
    caps, results = selfcheck.run(ep, client)
    p.capabilities, p.check_result, p.checked_at = caps, results, datetime.now(UTC)
    audit.record(db, actor, "ai.provider.check", target_type="ai_provider", target_id=p.id,
                 summary=f"AI 接口 {p.name} 连接自检：{sum(r['ok'] for r in results)}/{len(results)} 项通过")
    return _out(p)


class ModelsIn(BaseModel):
    protocol: Protocol
    base_url: str = Field(min_length=1, max_length=500)
    api_version: str = Field("", max_length=32)
    api_key: str | None = Field(None, max_length=8000)
    provider_id: uuid.UUID | None = None


@router.post("/models")
def fetch_models(body: ModelsIn, request: Request, _: User = Depends(admin_only), db: Session = Depends(get_db)) -> list[str]:
    """获取模型列表。新增时用填写的密钥；编辑时没重新填密钥就用已保存的。"""
    key = (body.api_key or "").strip()
    settings = ai_runtime.settings_row(db)
    if not key and body.provider_id:
        p = _get(db, body.provider_id)
        key = ai_runtime.endpoint(db, request.app.state.cipher, p, settings).api_key
    ep = Endpoint(protocol=body.protocol, base_url=_check_url(body.base_url), api_key=key, model="", api_version=body.api_version,
                  timeout=float(min(settings.timeout_seconds, 30)), proxy=settings.proxy_url)
    try:
        return list_models(ep, getattr(request.app.state, "ai_http_client", None))
    except AiError as exc:
        raise _ai_error(exc) from None


class FeatureCfg(BaseModel):
    enabled: bool = False
    provider_id: uuid.UUID | None = None


class SettingsIn(BaseModel):
    enabled: bool
    privacy: Literal["redact", "full"]
    default_provider_id: uuid.UUID | None = None
    features: dict[str, FeatureCfg] = Field(default_factory=dict)
    daily_call_limit: int = Field(ge=1, le=100000)
    daily_token_limit: int = Field(ge=1000, le=1_000_000_000)
    timeout_seconds: int = Field(ge=5, le=600)
    proxy_url: str = Field("", max_length=500)
    log_content: bool = False
    revision: int


def _settings_out(s: AiSetting) -> dict:
    return {"enabled": s.enabled, "privacy": s.privacy, "default_provider_id": str(s.default_provider_id) if s.default_provider_id else None,
            "features": {k: {"name": name, **({"enabled": False, "provider_id": None} | ((s.features or {}).get(k) or {}))}
                         for k, name in ai_runtime.FEATURES.items()},
            "daily_call_limit": s.daily_call_limit, "daily_token_limit": s.daily_token_limit, "timeout_seconds": s.timeout_seconds,
            "proxy_url": s.proxy_url, "log_content": s.log_content, "revision": s.revision,
            "changed_by": s.changed_by, "changed_at": s.changed_at.isoformat() if s.changed_at else None}


@router.get("/settings")
def get_settings(_: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    return _settings_out(ai_runtime.settings_row(db))


@router.put("/settings")
def put_settings(body: SettingsIn, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    s = ai_runtime.settings_row(db)
    if body.revision != s.revision:
        raise HTTPException(409, "设置已被其他人修改，请刷新后再改")
    unknown = set(body.features) - set(ai_runtime.FEATURES)
    if unknown:
        raise HTTPException(422, f"不认识的功能：{', '.join(sorted(unknown))}")
    ids = [body.default_provider_id, *(f.provider_id for f in body.features.values())]
    for pid in ids:
        if pid is not None and db.get(AiProvider, pid) is None:
            raise HTTPException(422, "选择的模型接口不存在")
    if body.proxy_url:
        parts = urlsplit(body.proxy_url)
        if parts.scheme not in ("http", "https", "socks5") or not parts.hostname:
            raise HTTPException(422, "代理地址格式不正确")
    before = _settings_out(s)
    s.enabled, s.privacy, s.default_provider_id = body.enabled, body.privacy, body.default_provider_id
    s.features = {k: {"enabled": f.enabled, "provider_id": str(f.provider_id) if f.provider_id else None} for k, f in body.features.items()}
    s.daily_call_limit, s.daily_token_limit, s.timeout_seconds = body.daily_call_limit, body.daily_token_limit, body.timeout_seconds
    s.proxy_url, s.log_content = body.proxy_url.strip(), body.log_content
    s.revision += 1
    s.changed_by, s.changed_at = actor.username, datetime.now(UTC)
    after = _settings_out(s)
    changed = sorted(k for k in after if k not in ("revision", "changed_by", "changed_at") and after[k] != before[k])
    audit.record(db, actor, "ai.settings.update", target_type="ai_settings", target_id="global",
                 summary=("开启" if body.enabled else "关闭") + " AI 功能" if "enabled" in changed else "修改 AI 设置", detail={"fields": changed})
    return after


@router.get("/usage")
def usage(days: int = Query(7, ge=1, le=90), _: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    since = datetime.now(UTC) - timedelta(days=days)
    rows = db.execute(select(AiUsage.feature, func.count(AiUsage.id), func.sum(AiUsage.input_tokens), func.sum(AiUsage.output_tokens),
                             func.sum(case((AiUsage.ok.is_(False), 1), else_=0)))
                      .where(AiUsage.at >= since).group_by(AiUsage.feature)).all()
    calls, tokens = ai_runtime.usage_today(db)
    recent = db.scalars(select(AiUsage).order_by(AiUsage.at.desc()).limit(50))
    names = {p.id: p.name for p in db.scalars(select(AiProvider))}
    return {
        "today": {"calls": calls, "tokens": tokens},
        "by_feature": [{"feature": f, "name": ai_runtime.FEATURES.get(f, "连接自检" if f == "selfcheck" else f), "calls": int(c),
                        "input_tokens": int(i or 0), "output_tokens": int(o or 0), "failed": int(e or 0)} for f, c, i, o, e in rows],
        "recent": [{"at": u.at.isoformat(), "user": u.user_name, "feature": u.feature, "provider": names.get(u.provider_id, ""),
                    "model": u.model, "mode": u.mode, "ok": u.ok, "error_kind": u.error_kind, "latency_ms": u.latency_ms,
                    "tokens": u.input_tokens + u.output_tokens} for u in recent],
    }

