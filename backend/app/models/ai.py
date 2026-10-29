"""AI 接入（4.3 P18a）：用户自己配置的模型接口、全局开关与按功能分配、调用用量。

接口密钥按凭据加密保存（credential_id），本表不存明文。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPrimaryKey, utcnow


class AiProvider(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "ai_providers"

    name: Mapped[str] = mapped_column(String(100), unique=True)
    preset_id: Mapped[str] = mapped_column(String(64), default="custom")
    # openai（兼容接口）/ anthropic / gemini / azure
    protocol: Mapped[str] = mapped_column(String(16))
    base_url: Mapped[str] = mapped_column(String(500))
    api_version: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(200))
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("credentials.id", ondelete="SET NULL"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    quirks: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 连接自检得出的能力表：chat / models / json_modes / best_json / tools / stream
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    check_result: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=1)


class AiSetting(Base):
    """单行全局设置（key = "global"）。AI 总开关默认关闭。"""

    __tablename__ = "ai_settings"

    key: Mapped[str] = mapped_column(String(16), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # redact：主机名、IP 等替换成代号后再发送；full：原样发送（适合本地模型）
    privacy: Mapped[str] = mapped_column(String(16), default="redact")
    default_provider_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("ai_providers.id", ondelete="SET NULL"))
    # {功能: {"enabled": bool, "provider_id": str | None}}
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    daily_call_limit: Mapped[int] = mapped_column(Integer, default=200)
    daily_token_limit: Mapped[int] = mapped_column(Integer, default=500000)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60)
    proxy_url: Mapped[str] = mapped_column(String(500), default="")
    log_content: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    changed_by: Mapped[str] = mapped_column(String(100), default="")
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiUsage(UUIDPrimaryKey, Base):
    """每次调用一行：谁、哪个功能、哪个模型、是否成功、耗时与 token。默认不记录内容。"""

    __tablename__ = "ai_usage"

    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user_name: Mapped[str] = mapped_column(String(100), default="")
    feature: Mapped[str] = mapped_column(String(32))
    provider_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("ai_providers.id", ondelete="SET NULL"), index=True)
    model: Mapped[str] = mapped_column(String(200), default="")
    mode: Mapped[str] = mapped_column(String(16), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error_kind: Mapped[str] = mapped_column(String(32), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class AiNote(UUIDPrimaryKey, Base):
    """AI 生成的解读与摘要（4.3 P18f）：kind = alert（告警解读）/ inspection（巡检摘要），ref 为告警或报告编号。
    按输入事实的指纹缓存，事实不变就不重复调用。"""

    __tablename__ = "ai_notes"
    __table_args__ = (UniqueConstraint("kind", "ref", "context_hash"),)

    kind: Mapped[str] = mapped_column(String(16))
    ref: Mapped[str] = mapped_column(String(80), index=True)
    context_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(100), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
