"""AI 调用的运行时：读设置、解密接口密钥、检查开关与配额、记录用量。

放在 app/ai 包外：app/ai 只做纯逻辑，不接触凭据与数据库写入（tests/test_ai_boundary.py）。
各功能（漏洞分析、告警解读等）只通过 run_json / run_chat 调用 AI。
"""
from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.protocols import AiError, ChatRequest, ChatResult, Endpoint, chat
from app.ai.redact import Redactor
from app.ai.structured import call_json
from app.crypto import CredentialCipher
from app.models import AiProvider, AiSetting, AiUsage, Credential
from app.services.credentials import reveal_secret

# 功能清单：键 → 名称。新功能在这里登记后，设置页面才能为它开关和选模型
FEATURES: dict[str, str] = {
    "cve_analysis": "漏洞影响分析",
    "alert_explain": "告警解读",
    "inspection_summary": "巡检报告摘要",
    "assistant": "问答助手",
    "mcp": "只读 MCP 接口（供外部 AI 工具查询）",
}


def settings_row(db: Session) -> AiSetting:
    row = db.get(AiSetting, "global")
    if row is None:
        row = AiSetting(key="global", enabled=False, privacy="redact", features={}, daily_call_limit=200,
                        daily_token_limit=500000, timeout_seconds=60, proxy_url="", log_content=False, revision=1)
        db.add(row)
        db.flush()
    return row


def endpoint(db: Session, cipher: CredentialCipher, provider: AiProvider, settings: AiSetting | None = None) -> Endpoint:
    settings = settings or settings_row(db)
    key = ""
    if provider.credential_id:
        cred = db.get(Credential, provider.credential_id)
        if cred is not None:
            key = reveal_secret(cred, cipher)
    return Endpoint(protocol=provider.protocol, base_url=provider.base_url, api_key=key, model=provider.model,
                    api_version=provider.api_version, timeout=float(settings.timeout_seconds), quirks=frozenset(provider.quirks or []),
                    proxy=settings.proxy_url)


def feature_enabled(settings: AiSetting, feature: str) -> bool:
    cfg = (settings.features or {}).get(feature) or {}
    return bool(settings.enabled and cfg.get("enabled"))


def provider_for(db: Session, feature: str) -> AiProvider:
    settings = settings_row(db)
    if not feature_enabled(settings, feature):
        raise AiError("disabled", f"AI 功能“{FEATURES.get(feature, feature)}”未开启")
    cfg = (settings.features or {}).get(feature) or {}
    pid = cfg.get("provider_id") or (str(settings.default_provider_id) if settings.default_provider_id else None)
    provider = db.get(AiProvider, uuid.UUID(pid)) if pid else None
    if provider is None or not provider.enabled:
        raise AiError("disabled", f"“{FEATURES.get(feature, feature)}”没有可用的模型接口，请在 AI 设置里指定")
    return provider


def usage_today(db: Session, now: datetime | None = None) -> tuple[int, int]:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=1)
    calls, tokens = db.execute(select(func.count(AiUsage.id), func.coalesce(func.sum(AiUsage.input_tokens + AiUsage.output_tokens), 0))
                               .where(AiUsage.at >= since)).one()
    return int(calls or 0), int(tokens or 0)


def check_quota(db: Session, settings: AiSetting, now: datetime | None = None) -> None:
    calls, tokens = usage_today(db, now)
    if calls >= settings.daily_call_limit or tokens >= settings.daily_token_limit:
        raise AiError("quota", f"已达到 24 小时内的调用上限（{calls} 次 / {tokens} token），可在 AI 设置里调整")


def record(db: Session, *, feature: str, provider: AiProvider | None, user_name: str, started: float, mode: str = "",
           result: ChatResult | None = None, error: AiError | None = None, content: dict[str, Any] | None = None) -> AiUsage:
    row = AiUsage(at=datetime.now(UTC), user_name=user_name[:100], feature=feature, provider_id=provider.id if provider else None,
                  model=provider.model if provider else "", mode=mode, ok=error is None, error_kind=error.kind if error else "",
                  latency_ms=int((time.perf_counter() - started) * 1000),
                  input_tokens=result.input_tokens if result else 0, output_tokens=result.output_tokens if result else 0,
                  content=content)
    db.add(row)
    return row


def redactor(db: Session) -> Redactor:
    return Redactor(enabled=settings_row(db).privacy != "full")


def run_json(
    db: Session, cipher: CredentialCipher, feature: str, *, user_name: str, messages: list[dict[str, Any]], schema: dict[str, Any],
    system: str = "", redact: Redactor | None = None, max_tokens: int = 2048, client: httpx.Client | None = None,
) -> Any:
    """调用 AI 得到校验过的 JSON；脱敏器由调用方登记好已知名称后传入，返回值已还原。"""
    provider = provider_for(db, feature)
    settings = settings_row(db)
    check_quota(db, settings)
    ep = endpoint(db, cipher, provider, settings)
    modes = (provider.capabilities or {}).get("json_modes") or None
    started = time.perf_counter()
    try:
        value, mode, result = call_json(ep, messages, schema, system=system, modes=modes, max_tokens=max_tokens, client=client)
    except AiError as exc:
        record(db, feature=feature, provider=provider, user_name=user_name, started=started, error=exc)
        raise
    content = {"messages": messages, "output": value} if settings.log_content else None
    record(db, feature=feature, provider=provider, user_name=user_name, started=started, mode=mode, result=result, content=content)
    return redact.restore(value) if redact else value


def run_chat(
    db: Session, cipher: CredentialCipher, feature: str, *, user_name: str, request: ChatRequest, client: httpx.Client | None = None,
) -> ChatResult:
    provider = provider_for(db, feature)
    settings = settings_row(db)
    check_quota(db, settings)
    ep = endpoint(db, cipher, provider, settings)
    started = time.perf_counter()
    try:
        result = chat(ep, request, client)
    except AiError as exc:
        record(db, feature=feature, provider=provider, user_name=user_name, started=started, error=exc)
        raise
    record(db, feature=feature, provider=provider, user_name=user_name, started=started, result=result)
    return result
