"""漏洞情报（4.3 P18c）：漏洞记录、各来源的发布记录、受影响产品、数据源状态、订阅、设置。

一个漏洞一条 VulnRecord：有 CVE 编号的以 CVE 为键，多个来源发布的同一 CVE 归并到一起；
只有来源自家编号的（如 AVD、QVD），键为“来源:编号”，不自动与 CVE 合并（疑似同一漏洞由人确认）。
每个来源发布一次记一条 VulnSighting，通知时据此标注“哪些平台发布了这个漏洞”。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKey, utcnow


class VulnRecord(Base):
    __tablename__ = "vuln_records"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    # critical / high / medium / low / unknown
    severity: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    cvss: Mapped[float | None] = mapped_column(Float)
    cvss_vector: Mapped[str] = mapped_column(String(200), default="")
    cvss_source: Mapped[str] = mapped_column(String(40), default="")
    published: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kev: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    kev_added: Mapped[str] = mapped_column(String(10), default="")
    kev_ransomware: Mapped[bool] = mapped_column(Boolean, default=False)
    kev_due: Mapped[str] = mapped_column(String(10), default="")
    epss: Mapped[float | None] = mapped_column(Float)
    epss_percentile: Mapped[float | None] = mapped_column(Float)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    # [{"url", "source", "tags": ["patch" | "advisory" | "mitigation" | "exploit"]}]
    references: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # 各来源给出的修复 / 缓解原文：[{"source", "kind": "action" | "solution" | "workaround", "text"}]
    remediation: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    cwe: Mapped[list[str]] = mapped_column(JSON, default=list)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    # 发过“订阅命中预警”的时间；之后确认命中本地时通知标题为“漏洞确认”
    early_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VulnSighting(Base):
    """某个来源发布了某个漏洞。"""

    __tablename__ = "vuln_sightings"

    record_id: Mapped[str] = mapped_column(String(80), ForeignKey("vuln_records.id", ondelete="CASCADE"), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    native_id: Mapped[str] = mapped_column(String(120), default="")
    title: Mapped[str] = mapped_column(String(500), default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    severity: Mapped[str] = mapped_column(String(16), default="unknown")
    published: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class VulnAffect(UUIDPrimaryKey, Base):
    """受影响的产品与版本区间。

    kind = "cpe"：product 为“厂商:产品”（如 dell:idrac9_firmware），ranges 为 NVD 风格
    [{"exact": "", "update": "", "start_incl": "", "start_excl": "", "end_incl": "", "end_excl": "", "and_other": bool}]；
    kind = "pkg"：ecosystem 如 Debian:12，product 为源码包名，ranges 为 OSV 风格 [{"introduced": "", "fixed": "", "last_affected": ""}]。
    """

    __tablename__ = "vuln_affects"

    record_id: Mapped[str] = mapped_column(String(80), ForeignKey("vuln_records.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(8))
    ecosystem: Mapped[str] = mapped_column(String(64), default="", index=True)
    product: Mapped[str] = mapped_column(String(200), index=True)
    ranges: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(32), default="")
    note: Mapped[str] = mapped_column(String(200), default="")


class VulnSource(Base):
    __tablename__ = "vuln_sources"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), default="never")  # never / ok / failed / running
    message: Mapped[str] = mapped_column(String(1000), default="")
    items: Mapped[int] = mapped_column(Integer, default=0)
    new_items: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class VulnSubscription(UUIDPrimaryKey, Base):
    """订阅：product = “厂商:产品”或“发行版生态”；keyword = 标题 / 描述里的关键词。auto 为按软件清单自动生成。"""

    __tablename__ = "vuln_subscriptions"

    kind: Mapped[str] = mapped_column(String(16))
    value: Mapped[str] = mapped_column(String(200))
    label: Mapped[str] = mapped_column(String(200), default="")
    min_severity: Mapped[str] = mapped_column(String(16), default="unknown")
    kev_only: Mapped[bool] = mapped_column(Boolean, default=False)
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VulnSetting(Base):
    """单行设置（key = "global"）。外网同步默认关闭。"""

    __tablename__ = "vuln_settings"

    key: Mapped[str] = mapped_column(String(16), primary_key=True)
    online_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    nvd_credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("credentials.id", ondelete="SET NULL"))
    # 新漏洞等几分钟再发，让几乎同时发布的其他来源并入同一条（越小越快）
    aggregation_minutes: Mapped[int] = mapped_column(Integer, default=5)
    # 接收外部推送（watchvuln / 通用 Webhook）用的令牌，只存哈希；为空表示未开启
    webhook_token_hash: Mapped[str] = mapped_column(String(64), default="")
    last_bundle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_bundle_info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 4.3 P18e 通知：即时（命中本地且已被利用或 CVSS ≥ 9.0）+ 每日汇总；渠道为空表示所有启用的通知渠道
    notify_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_channel_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    digest_hour: Mapped[int] = mapped_column(Integer, default=9)
    # 订阅命中预警：新漏洞命中订阅的产品或关键词、但还没有版本数据能确认本地影响时，先发一条“待确认”
    early_warning: Mapped[bool] = mapped_column(Boolean, default=True)
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=1)


class VulnAnalysis(UUIDPrimaryKey, Base):
    """AI 影响分析结果。按“漏洞 + 本地影响情况”的指纹缓存，设备版本或影响范围变了才重新分析。"""

    __tablename__ = "vuln_analyses"
    __table_args__ = (UniqueConstraint("record_id", "context_hash"),)

    record_id: Mapped[str] = mapped_column(String(80), ForeignKey("vuln_records.id", ondelete="CASCADE"), index=True)
    context_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(100), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
