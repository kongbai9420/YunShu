"""历史记录的保留期。

指标、服务器告警、巡检、采集任务各自有清理；这里补上其余会一直增长的表：
- 告警：已结束超过保留期的告警连同它的事件；仍未结束的告警只删旧周期的事件。
- 通知：已结束（成功、失败、取消、不确定）超过保留期的投递；没有投递或投递已删的旧意图。
- 任务：采集以外已结束的任务（通知、控制、巡检、漏洞同步等）。
- AI 用量：超过保留期的调用记录（每日额度只看最近 24 小时）。
- 审计与控制操作记录：单独的保留期，默认更长；设为 0 表示永久保留。

告警、通知、任务、AI 用量用 YUNSHU_HISTORY_RETENTION_DAYS（默认 180 天）；
审计与控制操作记录用 YUNSHU_AUDIT_RETENTION_DAYS（默认 365 天，0 为不清理）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session

from app.models import AiUsage, Alert, AlertEvent, AuditEvent, ControlOperation, NotificationDelivery, NotificationIntent, Task
from app.queue import TERMINAL

DELIVERY_DONE = ("accepted", "failed", "cancelled", "uncertain")
CONTROL_OPEN = ("queued", "running")


@dataclass(frozen=True)
class HistoryRetention:
    history_days: int = 180
    audit_days: int = 365  # 0：不清理


def prune(db: Session, keep: HistoryRetention = HistoryRetention(), *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    out: dict[str, int] = {}
    if keep.history_days > 0:
        cutoff = now - timedelta(days=keep.history_days)
        old_alerts = select(Alert.id).where(Alert.state == "resolved", Alert.resolved_at < cutoff)
        # 旧周期的事件：所属告警已经进入了更新的周期
        old_episodes = select(AlertEvent.id).join(Alert, Alert.id == AlertEvent.alert_id).where(
            AlertEvent.created_at < cutoff, AlertEvent.episode < Alert.episode)
        doomed = select(AlertEvent.id).where(AlertEvent.alert_id.in_(old_alerts))
        out["notification_intents"] = _count(db.execute(delete(NotificationIntent).where(
            NotificationIntent.event_id.in_(doomed) | NotificationIntent.event_id.in_(old_episodes))))
        out["alert_events"] = _count(db.execute(delete(AlertEvent).where(
            AlertEvent.id.in_(old_episodes) | AlertEvent.alert_id.in_(old_alerts)).execution_options(synchronize_session=False)))
        out["alerts"] = _count(db.execute(delete(Alert).where(Alert.id.in_(old_alerts)).execution_options(synchronize_session=False)))
        old_deliveries = select(NotificationDelivery.id).where(
            NotificationDelivery.status.in_(DELIVERY_DONE), NotificationDelivery.created_at < cutoff)
        out["notification_intents"] += _count(db.execute(delete(NotificationIntent).where(
            NotificationIntent.delivery_id.in_(old_deliveries))))
        out["notification_deliveries"] = _count(db.execute(delete(NotificationDelivery).where(
            NotificationDelivery.id.in_(old_deliveries)).execution_options(synchronize_session=False)))
        out["tasks"] = _count(db.execute(delete(Task).where(
            Task.kind != "collect.asset", Task.status.in_(TERMINAL), Task.finished_at < cutoff)))
        out["ai_usage"] = _count(db.execute(delete(AiUsage).where(AiUsage.at < cutoff)))
    if keep.audit_days > 0:
        cutoff = now - timedelta(days=keep.audit_days)
        out["audit_events"] = _count(db.execute(delete(AuditEvent).where(AuditEvent.occurred_at < cutoff)))
        out["control_operations"] = _count(db.execute(delete(ControlOperation).where(
            and_(ControlOperation.status.notin_(CONTROL_OPEN), ControlOperation.finished_at < cutoff))))
    return out


def _count(result) -> int:  # type: ignore[no-untyped-def]
    return result.rowcount or 0
