"""按状态临时提高采集频率（4.4 P20d）。

- 资产有未结束的告警（待满足条件、告警中、已确认、静默），或某个阈值规则的指标已接近阈值（差距在阈值的 10% 以内），
  告警求值时为它登记“提频”，持续到最后一次满足条件后 10 分钟。维护中或处于维护窗口的资产不提频。
- 收到 BMC 推送的严重 / 警告事件（SNMP Trap、Redfish 事件）时同样登记，并立即补采一次（同一资产每分钟最多一次）。
- 提频期间每 15 秒采集一次，代替按模板周期的采集；同时提频的资产最多 10 台（YUNSHU_BOOST_MAX），多出的按原周期。
- 仍受每台 BMC 排队与每分钟访问上限约束（4.4 P20b）：提频主要加快 SNMP 等不限次数的来源，Redfish / IPMI 超出上限的照常顺延；
  连续失败的协议照常按冷却退避。
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import queue
from app.models import CollectionBoost

BOOST_INTERVAL = 15
HOLD = timedelta(minutes=10)
NEAR = 0.10


def boost_max() -> int:
    try:
        return max(0, int(os.environ.get("YUNSHU_BOOST_MAX", "10")))
    except ValueError:
        return 10


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def mark(db: Session, asset_id: uuid.UUID, reason: str, now: datetime) -> None:
    """登记或延长提频；原因取最新一次。"""
    row = db.get(CollectionBoost, asset_id)
    if row is None:
        db.add(CollectionBoost(asset_id=asset_id, started_at=now, until=now + HOLD, reason=reason[:200]))
    else:
        if _aware(row.until) < now:
            row.started_at = now
        row.until, row.reason = now + HOLD, reason[:200]


def near_threshold(comparison: str, threshold: float, value: float) -> bool:
    """还没超过阈值，但差距在阈值的 10% 以内（阈值为 0 时差距 1 以内）。"""
    margin = abs(threshold) * NEAR or 1.0
    if comparison == "lte":
        return threshold < value <= threshold + margin
    return threshold - margin <= value < threshold


def active(db: Session, now: datetime) -> dict[uuid.UUID, CollectionBoost]:
    """正在提频的资产（最近登记的优先，最多 boost_max() 台）。"""
    rows = db.scalars(select(CollectionBoost).where(CollectionBoost.until > now).order_by(CollectionBoost.until.desc())).all()
    return {r.asset_id: r for r in rows[: boost_max()]}


def on_event(db: Session, asset_id: uuid.UUID, source: str, now: datetime) -> None:
    """收到推送的严重 / 警告事件：提频，并立即补采一次。"""
    mark(db, asset_id, "收到 " + ("SNMP Trap" if source == "snmp_trap" else "Redfish 事件") + " 推送的告警", now)
    queue.enqueue(db, "collect.asset", asset_id=asset_id, payload={"asset_id": str(asset_id), "event": True},
                  idempotency_key=f"collect:{asset_id}:event:{int(now.timestamp()) // 60}", max_attempts=1, priority=5)


def prune(db: Session, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    res = db.execute(delete(CollectionBoost).where(CollectionBoost.until < now - timedelta(days=1)), execution_options={"synchronize_session": False})
    return res.rowcount or 0


def describe(row: CollectionBoost | None, now: datetime) -> dict | None:
    if row is None or _aware(row.until) <= now:
        return None
    return {"interval": BOOST_INTERVAL, "until": _aware(row.until).isoformat(), "since": _aware(row.started_at).isoformat(), "reason": row.reason}
