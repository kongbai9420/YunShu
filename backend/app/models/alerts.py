"""P4 告警、生命周期事件与通知出站记录；秘密仅通过凭据引用保存。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPrimaryKey, utcnow


class AlertRule(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "alert_rules"
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    kind: Mapped[str] = mapped_column(String(24), default="threshold")
    metric_key: Mapped[str] = mapped_column(String(200), default="cpu.load")
    comparison: Mapped[str] = mapped_column(String(8), default="gte")
    threshold: Mapped[float] = mapped_column(Float, default=90)
    recovery_threshold: Mapped[float] = mapped_column(Float, default=85)
    consecutive_samples: Mapped[int] = mapped_column(Integer, default=1)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=60)
    repeat_seconds: Mapped[int] = mapped_column(Integer, default=0)
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    channel_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    suppress_by_dependency: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    legacy_id: Mapped[str | None] = mapped_column(String(64), unique=True)


class Alert(UUIDPrimaryKey, Base):
    """每个规则 / 资产 / 指标的当前状态；每次触发通过 episode 与事件保留历史。"""
    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("rule_id", "asset_id", "metric_key"),)
    rule_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("alert_rules.id", ondelete="RESTRICT"), index=True)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="SET NULL"), index=True)
    asset_name: Mapped[str] = mapped_column(String(200), default="")
    metric_key: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    title: Mapped[str] = mapped_column(String(400), default="")
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(32), default="")
    threshold: Mapped[float | None] = mapped_column(Float)
    episode: Mapped[int] = mapped_column(Integer, default=1)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_sample_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str] = mapped_column(String(200), default="")
    silenced_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppression_reason: Mapped[str] = mapped_column(String(400), default="")


class AlertEvent(UUIDPrimaryKey, Base):
    __tablename__ = "alert_events"
    alert_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("alerts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(400), default="")
    message: Mapped[str] = mapped_column(String(2000), default="")
    value: Mapped[float | None] = mapped_column(Float)
    threshold: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32))
    metric_key: Mapped[str | None] = mapped_column(String(200))
    asset_name: Mapped[str | None] = mapped_column(String(200))
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    episode: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    notify: Mapped[bool] = mapped_column(Boolean, default=True)
    routed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class MaintenanceWindow(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "maintenance_windows"
    name: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)


class NotificationChannel(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "notification_channels"
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("credentials.id", ondelete="SET NULL"))
    revision: Mapped[int] = mapped_column(Integer, default=1)


class NotificationDelivery(UUIDPrimaryKey, Base):
    __tablename__ = "notification_deliveries"
    channel_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("notification_channels.id", ondelete="RESTRICT"), index=True)
    channel_name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    # alert：由告警事件渲染；vuln：漏洞情报通知，内容在生成时已确定（4.3 P18e）
    kind: Mapped[str] = mapped_column(String(16), default="alert")
    title: Mapped[str] = mapped_column(String(400), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    html: Mapped[str] = mapped_column(Text, default="")
    generation: Mapped[int] = mapped_column(Integer, default=1)
    lease_token: Mapped[str] = mapped_column(String(100), default="")
    channel_revision: Mapped[int] = mapped_column(Integer, default=1)


class NotificationIntent(UUIDPrimaryKey, Base):
    __tablename__ = "notification_intents"
    __table_args__ = (UniqueConstraint("event_id", "channel_id"),)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("alert_events.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("notification_channels.id", ondelete="RESTRICT"), index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("notification_deliveries.id", ondelete="SET NULL"))
