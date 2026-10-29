"""历史记录保留期：过期的已结束记录被清理，未结束的和保留期内的不动。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerting import evaluate
from app.models import (
    AiUsage, Alert, AlertEvent, AlertRule, Asset, AuditEvent, ControlOperation, NotificationChannel, NotificationDelivery,
    NotificationIntent, Task,
)
from app.retention import HistoryRetention, prune

NOW = datetime(2026, 10, 28, 8, tzinfo=UTC)
OLD = NOW - timedelta(days=200)
RECENT = NOW - timedelta(days=10)


def count(db: Session, model) -> int:  # type: ignore[no-untyped-def]
    return db.scalar(select(func.count()).select_from(model)) or 0


def alert(db: Session, rule: AlertRule, asset: Asset, *, state: str, episode: int = 1, resolved_at=None) -> Alert:  # type: ignore[no-untyped-def]
    a = Alert(rule_id=rule.id, asset_id=asset.id, asset_name=asset.name, metric_key=f"cpu.load:{state}{episode}{resolved_at}", state=state,
              severity="warning", title="fake", episode=episode, resolved_at=resolved_at, acknowledged_by="", suppression_reason="")
    db.add(a)
    db.flush()
    return a


def event(db: Session, a: Alert, at: datetime, episode: int = 1, notify: bool = False) -> AlertEvent:
    e = AlertEvent(alert_id=a.id, kind="trigger", title="fake", message="", episode=episode, created_at=at, notify=notify, routed=True)
    db.add(e)
    db.flush()
    return e


def test_old_closed_history_is_pruned_and_open_or_recent_is_kept(session: Session) -> None:
    asset = Asset(name="fake-server", form="physical", role="server")
    rule = AlertRule(name="fake-rule", metric_key="cpu.load", threshold=90, recovery_threshold=80)
    channel = NotificationChannel(name="fake-email", kind="email", enabled=True, config={})
    session.add_all([asset, rule, channel])
    session.flush()

    old_closed = alert(session, rule, asset, state="resolved", resolved_at=OLD)
    old_event = event(session, old_closed, OLD)
    delivery = NotificationDelivery(channel_id=channel.id, channel_name="fake-email", status="accepted", created_at=OLD, event_ids=[])
    session.add(delivery)
    session.flush()
    session.add(NotificationIntent(event_id=old_event.id, channel_id=channel.id, due_at=OLD, delivery_id=delivery.id))
    recent_closed = alert(session, rule, asset, state="resolved", resolved_at=RECENT)
    event(session, recent_closed, RECENT)
    still_open = alert(session, rule, asset, state="active", episode=2)
    event(session, still_open, OLD, episode=1)  # 旧周期：删
    event(session, still_open, OLD, episode=2)  # 当前周期：留

    session.add_all([
        Task(kind="notification.send", status="succeeded", finished_at=OLD),
        Task(kind="notification.send", status="running"),
        Task(kind="collect.asset", status="succeeded", finished_at=OLD),  # 采集任务由采集自己的清理负责
        AuditEvent(action="fake.old", occurred_at=OLD), AuditEvent(action="fake.recent", occurred_at=RECENT),
        AuditEvent(action="fake.year", occurred_at=NOW - timedelta(days=400)),
        ControlOperation(kind="power", action="soft", status="verified", finished_at=NOW - timedelta(days=400)),
        ControlOperation(kind="power", action="soft", status="queued"),
        AiUsage(feature="fake", at=OLD), AiUsage(feature="fake", at=RECENT),
    ])
    session.commit()

    out = prune(session, HistoryRetention(history_days=180, audit_days=365), now=NOW)
    session.commit()
    assert out["alerts"] == 1 and out["alert_events"] == 2 and out["notification_deliveries"] == 1
    assert {a.id for a in session.scalars(select(Alert))} == {recent_closed.id, still_open.id}
    assert [e.episode for e in session.scalars(select(AlertEvent).where(AlertEvent.alert_id == still_open.id))] == [2]
    assert count(session, NotificationIntent) == 0 and count(session, NotificationDelivery) == 0
    assert sorted(t.kind + ":" + t.status for t in session.scalars(select(Task))) == ["collect.asset:succeeded", "notification.send:running"]
    assert sorted(e.action for e in session.scalars(select(AuditEvent))) == ["fake.old", "fake.recent"]  # 审计按 365 天
    assert [o.status for o in session.scalars(select(ControlOperation))] == ["queued"]
    assert count(session, AiUsage) == 1

    # 审计保留期为 0：永久保留
    session.add(AuditEvent(action="fake.ancient", occurred_at=NOW - timedelta(days=3000)))
    session.commit()
    assert "audit_events" not in prune(session, HistoryRetention(history_days=180, audit_days=0), now=NOW)
    assert count(session, AuditEvent) == 3


def test_evaluation_still_knows_what_was_notified(session: Session) -> None:
    """求值只读当前周期的通知记录后，冷却与“已通知过”的判断不变。"""
    from app.models import LatestMetric

    asset = Asset(name="fake-server", form="physical", role="server")
    rule = AlertRule(name="fake-rule", metric_key="cpu.load", threshold=90, recovery_threshold=80, repeat_seconds=0)
    session.add_all([asset, rule])
    session.flush()
    session.add(LatestMetric(asset_id=asset.id, metric_key="cpu.load", value=95, unit="%", quality="valid", observed_at=NOW, interval_seconds=60))
    session.commit()
    evaluate(session, now=NOW + timedelta(seconds=1))
    session.commit()
    triggers = lambda: session.scalar(select(func.count()).select_from(AlertEvent).where(AlertEvent.kind == "trigger", AlertEvent.notify.is_(True)))  # noqa: E731
    assert triggers() == 1
    for i in range(1, 4):
        m = session.scalar(select(LatestMetric))
        m.observed_at = NOW + timedelta(seconds=60 * i)
        session.commit()
        evaluate(session, now=NOW + timedelta(seconds=60 * i + 1))
        session.commit()
    assert triggers() == 1  # 告警一直持续，不重复发触发通知
