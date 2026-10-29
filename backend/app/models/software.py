"""软件清单（4.3 P18b）：SSH 读到的已安装软件包，以及每个端点最近一次读取的结果。

系统、固件、虚拟化版本不单独存表，由 app/inventory.py 从各端点最近一次采集的 facts 推导。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKey, utcnow


class AssetPackage(UUIDPrimaryKey, Base):
    __tablename__ = "asset_packages"
    __table_args__ = (UniqueConstraint("endpoint_id", "name"),)

    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("management_endpoints.id", ondelete="CASCADE"))
    # 发行版生态（与 OSV 一致），如 Debian:12、Ubuntu:22.04:LTS；认不出时为空，不参与比对
    ecosystem: Mapped[str] = mapped_column(String(64), default="", index=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(200), index=True)
    source_version: Mapped[str] = mapped_column(String(200))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PackageScan(Base):
    __tablename__ = "package_scans"

    endpoint_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("management_endpoints.id", ondelete="CASCADE"), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String(16), default="pending")  # ok / failed / unsupported
    message: Mapped[str] = mapped_column(String(1000), default="")
    manager: Mapped[str] = mapped_column(String(16), default="")
    ecosystem: Mapped[str] = mapped_column(String(64), default="")
    count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
