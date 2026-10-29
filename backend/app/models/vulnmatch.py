"""漏洞比对结果与处置（4.3 P18d）。"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKey, utcnow

# 处置状态：open 待处理 / affected 确认受影响 / not_affected 不受影响 / mitigated 已缓解 / accepted 接受风险 / false_positive 误报
TRIAGE = ("open", "affected", "not_affected", "mitigated", "accepted", "false_positive")


class VulnMatch(UUIDPrimaryKey, Base):
    """某台资产上的某个软件 / 固件版本命中某个漏洞。版本升级后不再命中时 active 置为 False（记录保留）。"""

    __tablename__ = "vuln_matches"
    __table_args__ = (UniqueConstraint("asset_id", "record_id", "product"),)

    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    record_id: Mapped[str] = mapped_column(String(80), ForeignKey("vuln_records.id", ondelete="CASCADE"), index=True)
    # cpe 为“厂商:产品”；软件包为“生态/源码包”，如 Debian:12/openssl
    product: Mapped[str] = mapped_column(String(260))
    label: Mapped[str] = mapped_column(String(200), default="")
    installed_version: Mapped[str] = mapped_column(String(200), default="")
    fixed_version: Mapped[str] = mapped_column(String(200), default="")
    # affected：版本落在受影响区间；possible：数据不完整或补丁级别无法判断
    confidence: Mapped[str] = mapped_column(String(16), default="affected")
    note: Mapped[str] = mapped_column(String(500), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    review_at: Mapped[date | None] = mapped_column(Date)
    status_by: Mapped[str] = mapped_column(String(100), default="")
    status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 通知后漏洞才被列入 KEV 时补发一次“升级”通知
    notified_kev: Mapped[bool] = mapped_column(Boolean, default=False)
