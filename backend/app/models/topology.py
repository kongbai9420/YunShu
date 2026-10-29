"""P3 拓扑：网络链路与画布视图。

- 观察（Observation）：采集事实里的 LLDP 邻居表（CollectionStatus.facts），不在这里存；
- 链路（NetworkLink）：由观察生成的候选，以及人工确认 / 人工新建的链路；
- 视图（TopologyView）：画布布局与“移出视图”，带 revision，和链路、资产都分开。

重新扫描只会新增、刷新或移除 origin=discovered 且 state=candidate 的链路；
已确认、人工和已忽略（rejected）的链路以及视图布局一律不动。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPrimaryKey, utcnow


class NetworkLink(UUIDPrimaryKey, Timestamps, Base):
    """两台资产之间的网络链路。

    origin：discovered（扫描生成）/ confirmed（扫描生成后人工确认）/ manual（人工新建）。
    state：candidate（候选）/ confirmed（已确认）/ rejected（已忽略，作为墓碑保留，重扫不再提出）。
    fingerprint：扫描生成的链路去重键（例如 lldp:<较小资产 ID>|<较大资产 ID>），人工链路为空。
    locked：为真时重新扫描不会改动它；确认和人工链路总是锁定。
    """

    __tablename__ = "network_links"

    source_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    target_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    source_port: Mapped[str] = mapped_column(String(200), default="")
    target_port: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(32), default="ethernet")
    origin: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), index=True)
    fingerprint: Mapped[str] = mapped_column(String(255), default="", index=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(String(500), default="")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TopologyView(UUIDPrimaryKey, Base):
    """画布视图。layout 形如 {"<asset_id>": {"x": 0, "y": 0, "locked": false}}。

    hidden 是移出视图的资产 ID 列表：只是不在这个视图里显示，资产与链路都不删除。
    revision 每次保存加一；带着旧修订号保存返回 409，不做静默覆盖。
    """

    __tablename__ = "topology_views"

    name: Mapped[str] = mapped_column(String(200), unique=True)
    # home（家用）/ datacenter（机房）
    kind: Mapped[str] = mapped_column(String(16), default="home")
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sites.id", ondelete="SET NULL"), index=True
    )
    layout: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    hidden: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(100), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MacObservation(UUIDPrimaryKey, Base):
    """交换机 / 路由器上读到的 MAC 地址表（FDB）与 ARP 表条目（4.2 P14）。

    只作为“这个 MAC / IP 出现在哪台交换机哪个口”的发现证据，不据此合并资产或认定物理链路。
    """

    __tablename__ = "mac_observations"
    __table_args__ = (UniqueConstraint("switch_asset_id", "source", "mac", "ip", "port"),)

    switch_asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(8))  # fdb / arp
    mac: Mapped[str] = mapped_column(String(12), index=True)  # 12 位小写十六进制，不带分隔符
    ip: Mapped[str] = mapped_column(String(64), default="", index=True)
    port: Mapped[str] = mapped_column(String(200), default="")
    vlan: Mapped[int | None] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
