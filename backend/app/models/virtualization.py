from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKey, utcnow


class VirtualizationSource(UUIDPrimaryKey, Base):
    """每个管理端点独立的观察范围；目标或原生服务身份变化时停止关联确认。"""
    __tablename__ = "virtualization_sources"
    endpoint_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("management_endpoints.id", ondelete="CASCADE"), unique=True)
    platform: Mapped[str] = mapped_column(String(16))
    target: Mapped[str] = mapped_column(String(512))
    native_uuid: Mapped[str] = mapped_column(String(200), default="")
    reported_uuid: Mapped[str] = mapped_column(String(200), default="")
    identity_conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VirtualizationObservation(UUIDPrimaryKey, Base):
    __tablename__ = "virtualization_observations"
    __table_args__ = (UniqueConstraint("source_id", "kind", "external_id"), UniqueConstraint("source_id", "asset_id"))
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("virtualization_sources.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    host_external_id: Mapped[str] = mapped_column(String(200), default="")
    power: Mapped[str] = mapped_column(String(16), default="unknown")
    connection: Mapped[str] = mapped_column(String(32), default="unknown")
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    confirmed_revision: Mapped[int] = mapped_column(Integer, default=0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="SET NULL"), index=True)
    confirmed_host_asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="SET NULL"))
    owned_relation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("asset_relations.id", ondelete="SET NULL"))
