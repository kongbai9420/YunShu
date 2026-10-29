"""离线告警状态机验收：只写持久采集证据，不连接设备或通知服务。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerting import evaluate
from app.models import Alert, AlertEvent, AlertRule, Asset, AssetRelation, CollectionStatus, LatestMetric, MaintenanceWindow, ManagementEndpoint, NetworkLink

NOW = datetime(2026, 10, 16, 0, 0, tzinfo=UTC)


def asset(db: Session, name: str = "fake-server", **kwargs) -> Asset:
    obj = Asset(name=name, form="physical", role="server", platform="Debian", **kwargs)
    db.add(obj)
    db.flush()
    return obj


def rule(db: Session, **kwargs) -> AlertRule:
    values = dict(name="CPU 超限", metric_key="cpu.load", threshold=90, recovery_threshold=80,
                  consecutive_samples=1, duration_seconds=0, cooldown_seconds=0, repeat_seconds=0)
    values.update(kwargs)
    obj = AlertRule(**values)
    db.add(obj)
    db.flush()
    return obj


def sample(db: Session, obj: Asset, value: float | None, at: datetime = NOW, *, key: str = "cpu.load", quality: str = "valid", estimated: bool = False) -> LatestMetric:
    row = db.get(LatestMetric, (obj.id, key))
    if row is None:
        row = LatestMetric(asset_id=obj.id, metric_key=key, interval_seconds=60, source="fake", unit="%")
        db.add(row)
    row.value, row.observed_at, row.quality, row.estimated = value, at, quality, estimated
    db.flush()
    return row


def test_history_keeps_trigger_threshold_and_wildcard_matches(session):
    obj = asset(session)
    configured = rule(session, metric_key="sensor.temp*", threshold=80, recovery_threshold=70)
    sample(session, obj, 90, key="sensor.temp:CPU")
    evaluate(session, now=NOW)
    triggered = session.scalar(select(AlertEvent).where(AlertEvent.kind == "trigger"))
    assert triggered.threshold == 80 and triggered.value == 90 and triggered.metric_key == "sensor.temp:CPU"
    configured.threshold = 99
    sample(session, obj, 60, at=NOW+timedelta(seconds=60), key="sensor.temp:CPU")
    evaluate(session, now=NOW+timedelta(seconds=60))
    assert triggered.threshold == 80 and triggered.value == 90


def current(db: Session, obj: Asset | None = None) -> Alert:
    statement = select(Alert)
    if obj:
        statement = statement.where(Alert.asset_id == obj.id)
    return db.scalars(statement).one()


def events(db: Session, kind: str | None = None, *, notify: bool | None = None) -> list[AlertEvent]:
    statement = select(AlertEvent).order_by(AlertEvent.created_at)
    if kind:
        statement = statement.where(AlertEvent.kind == kind)
    if notify is not None:
        statement = statement.where(AlertEvent.notify == notify)
    return list(db.scalars(statement))


def endpoint(db: Session, obj: Asset, *, protocol: str = "ssh", purpose: str = "collection") -> ManagementEndpoint:
    ep = ManagementEndpoint(asset_id=obj.id, address="192.0.2.10", protocol=protocol, purpose=purpose)
    db.add(ep)
    db.flush()
    return ep


def status(db: Session, ep: ManagementEndpoint, at: datetime = NOW, *, state: str = "failed", error: str = "timeout", failures: int = 1) -> CollectionStatus:
    row = db.get(CollectionStatus, ep.id)
    if row is None:
        row = CollectionStatus(endpoint_id=ep.id, asset_id=ep.asset_id, protocol=ep.protocol)
        db.add(row)
    row.state, row.error_kind, row.last_attempt_at, row.consecutive_failures = state, error, at, failures
    db.flush()
    return row


def test_new_samples_and_duration_survive_restart(session: Session):
    obj = asset(session)
    rule(session, consecutive_samples=3, duration_seconds=120)
    sample(session, obj, 95)
    evaluate(session, now=NOW)
    alert = current(session)
    assert (alert.state, alert.sample_count) == ("pending", 1)
    evaluate(session, now=NOW + timedelta(seconds=120))
    assert (alert.state, alert.sample_count) == ("pending", 1)
    session.commit()
    session.expire_all()  # 模拟 Worker 重启：不依赖进程中的计数或缓存。
    sample(session, obj, 96, NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=120))
    assert current(session).sample_count == 2
    sample(session, obj, 97, NOW + timedelta(seconds=120))
    evaluate(session, now=NOW + timedelta(seconds=120))
    assert current(session).state == "active"
    assert len(events(session, "trigger", notify=True)) == 1
    evaluate(session, now=NOW + timedelta(seconds=135))
    assert len(events(session, "trigger", notify=True)) == 1


@pytest.mark.parametrize("quality,estimated,value", [("failed", False, None), ("unconfigured", False, None), ("unsupported", False, None), ("stale", False, 0), ("estimated", True, 0), ("valid", True, 0)])
def test_unknown_neither_triggers_nor_recovers(session: Session, quality: str, estimated: bool, value: float | None):
    obj = asset(session)
    rule(session)
    sample(session, obj, value, quality=quality, estimated=estimated)
    evaluate(session, now=NOW)
    assert not events(session)
    sample(session, obj, 95, NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=60))
    assert current(session).state == "active"
    sample(session, obj, value, NOW + timedelta(seconds=120), quality=quality, estimated=estimated)
    evaluate(session, now=NOW + timedelta(seconds=120))
    assert current(session).state == "active"
    assert not events(session, "recover")


def test_stale_and_gap_break_pending_evidence(session: Session):
    obj = asset(session)
    rule(session, consecutive_samples=2, duration_seconds=60)
    sample(session, obj, 95)
    evaluate(session, now=NOW)
    evaluate(session, now=NOW + timedelta(seconds=181))
    assert current(session).sample_count == 0
    sample(session, obj, 96, NOW + timedelta(seconds=240))
    evaluate(session, now=NOW + timedelta(seconds=240))
    assert (current(session).state, current(session).sample_count) == ("pending", 1)
    sample(session, obj, 97, NOW + timedelta(seconds=600))
    evaluate(session, now=NOW + timedelta(seconds=600))
    assert current(session).sample_count == 1


def test_hysteresis_recovery_and_cooldown_between_episodes(session: Session):
    obj = asset(session)
    rule(session, cooldown_seconds=120)
    sample(session, obj, 95)
    evaluate(session, now=NOW)
    sample(session, obj, 85, NOW + timedelta(seconds=15))
    evaluate(session, now=NOW + timedelta(seconds=15))
    assert current(session).state == "active"
    sample(session, obj, 80, NOW + timedelta(seconds=30))
    evaluate(session, now=NOW + timedelta(seconds=30))
    assert current(session).state == "resolved"
    assert len(events(session, "recover", notify=True)) == 1
    sample(session, obj, 95, NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=60))
    assert current(session).episode == 2
    assert len(events(session, "trigger", notify=True)) == 1
    evaluate(session, now=NOW + timedelta(seconds=150))
    assert len(events(session, "trigger", notify=True)) == 2


def test_lte_and_instance_matching(session: Session):
    obj = asset(session)
    rule(session, metric_key="disk.free_pct", comparison="lte", threshold=10, recovery_threshold=20)
    sample(session, obj, 5, key="disk.free_pct:/")
    sample(session, obj, 40, key="disk.free_pct:/data")
    evaluate(session, now=NOW)
    assert current(session).metric_key == "disk.free_pct:/"
    sample(session, obj, 20, NOW + timedelta(seconds=60), key="disk.free_pct:/")
    evaluate(session, now=NOW + timedelta(seconds=60))
    assert current(session).state == "resolved"


def test_repeat_opt_in_ack_and_silence_expiry(session: Session):
    obj = asset(session)
    target = rule(session, repeat_seconds=30, cooldown_seconds=45)
    sample(session, obj, 95)
    evaluate(session, now=NOW)
    evaluate(session, now=NOW + timedelta(seconds=30))
    assert not events(session, "repeat")
    evaluate(session, now=NOW + timedelta(seconds=45))
    assert len(events(session, "repeat", notify=True)) == 1
    alert = current(session)
    alert.state, alert.acknowledged_by = "acknowledged", "fake-operator"
    evaluate(session, now=NOW + timedelta(seconds=90))
    assert len(events(session, "repeat", notify=True)) == 1
    alert.state, alert.silenced_until = "silenced", NOW + timedelta(seconds=110)
    evaluate(session, now=NOW + timedelta(seconds=120))
    assert alert.state == "acknowledged"
    alert.state, alert.acknowledged_by = "active", ""
    target.repeat_seconds = 0
    evaluate(session, now=NOW + timedelta(seconds=135))
    assert len(events(session, "repeat", notify=True)) == 1


def test_repeat_requires_current_fresh_failure_evidence(session: Session):
    obj = asset(session)
    rule(session, repeat_seconds=30)
    sample(session, obj, 95)
    evaluate(session, now=NOW)
    evaluate(session, now=NOW + timedelta(seconds=181))
    assert not events(session, "repeat")
    assert current(session).state == "active"


def test_availability_counts_only_actual_new_attempts_with_backoff(session: Session):
    obj = asset(session)
    ep = endpoint(session, obj)
    rule(session, kind="availability", metric_key="availability", consecutive_samples=3)
    status(session, ep)
    evaluate(session, now=NOW)
    evaluate(session, now=NOW + timedelta(seconds=60))
    assert current(session).sample_count == 1
    status(session, ep, NOW + timedelta(seconds=120), failures=2)
    evaluate(session, now=NOW + timedelta(seconds=120))
    status(session, ep, NOW + timedelta(seconds=360), failures=3)
    evaluate(session, now=NOW + timedelta(seconds=360))
    assert current(session).state == "active"
    status(session, ep, NOW + timedelta(seconds=420), state="ok", error="", failures=0)
    evaluate(session, now=NOW + timedelta(seconds=420))
    assert current(session).state == "resolved"
    assert len(events(session, "recover", notify=True)) == 1


@pytest.mark.parametrize("state,error", [("failed", "auth"), ("failed", "protocol"), ("untrusted", "untrusted"), ("unconfigured", "unconfigured"), ("skipped", "")])
def test_availability_excludes_configuration_or_protocol_error(session: Session, state: str, error: str):
    obj = asset(session)
    ep = endpoint(session, obj)
    rule(session, kind="availability", metric_key="availability")
    status(session, ep, state=state, error=error)
    evaluate(session, now=NOW)
    assert not events(session)


def test_availability_multiple_endpoints_and_stale_worker(session: Session):
    obj = asset(session)
    first, second = endpoint(session, obj), endpoint(session, obj, protocol="ipmi")
    control = endpoint(session, obj, purpose="control")
    rule(session, kind="availability", metric_key="availability")
    status(session, first)
    status(session, second, state="ok", error="", failures=0)
    status(session, control)
    evaluate(session, now=NOW)
    assert not events(session)
    status(session, second, NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=60))
    assert current(session).state == "active"
    status(session, first, NOW + timedelta(seconds=120), state="failed", error="auth")
    evaluate(session, now=NOW + timedelta(seconds=120))
    assert current(session).state == "active"
    assert not events(session, "recover")
    evaluate(session, now=NOW + timedelta(seconds=1200))
    assert current(session).state == "active"


def test_maintenance_suppresses_without_fictitious_recovery(session: Session):
    obj = asset(session, tags=["lab"])
    rule(session)
    window = MaintenanceWindow(name="fake-maintenance", starts_at=NOW, ends_at=NOW + timedelta(seconds=90), tags=["lab"])
    session.add(window)
    sample(session, obj, 95)
    evaluate(session, now=NOW)
    assert current(session).state == "active"
    assert current(session).suppression_reason
    assert not events(session, notify=True)
    sample(session, obj, 70, NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=60))
    assert current(session).state == "resolved"
    assert not events(session, "recover", notify=True)
    sample(session, obj, 95, NOW + timedelta(seconds=120))
    evaluate(session, now=NOW + timedelta(seconds=120))
    assert len(events(session, "trigger", notify=True)) == 1


@pytest.mark.parametrize("lifecycle", ["maintenance", "planned", "retired"])
def test_lifecycle_suppression_then_notify_when_active(session: Session, lifecycle: str):
    obj = asset(session, lifecycle=lifecycle)
    rule(session)
    sample(session, obj, 95)
    evaluate(session, now=NOW)
    assert not events(session, notify=True)
    assert session.scalar(select(Alert)) is None  # 维护中不产生新告警（连告警记录都没有）
    obj.lifecycle = "active"
    evaluate(session, now=NOW + timedelta(seconds=15))
    assert len(events(session, "trigger", notify=True)) == 1


def test_alert_open_before_maintenance_is_kept_but_not_notified(session: Session):
    obj = asset(session)
    rule(session)
    sample(session, obj, 95)
    evaluate(session, now=NOW)
    assert current(session).state == "active" and len(events(session, "trigger", notify=True)) == 1
    obj.lifecycle = "maintenance"
    evaluate(session, now=NOW + timedelta(seconds=15))
    assert current(session).state == "active" and current(session).suppression_reason == "资产处于维护中状态"
    obj.lifecycle = "active"
    evaluate(session, now=NOW + timedelta(seconds=30))
    assert current(session).suppression_reason == ""


def test_scope_disabled_and_deleted_are_closed_not_recovered(session: Session):
    obj = asset(session, tags=["lab"])
    other = asset(session, "fake-other", tags=["lab"])
    target = rule(session, asset_ids=[str(obj.id)], tags=["lab"])
    sample(session, obj, 95)
    sample(session, other, 95)
    evaluate(session, now=NOW)
    assert len(list(session.scalars(select(Alert)))) == 1
    obj.tags = ["other"]
    evaluate(session, now=NOW + timedelta(seconds=15))
    assert current(session).state == "resolved"
    assert len(events(session, "closed", notify=False)) == 1
    assert not events(session, "recover")
    obj.tags = ["lab"]
    sample(session, obj, 96, NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=60))
    target.enabled = False
    evaluate(session, now=NOW + timedelta(seconds=75))
    assert len(events(session, "closed", notify=False)) == 2
    evaluate(session, now=NOW + timedelta(seconds=90))
    assert len(events(session, "closed")) == 2


@pytest.mark.parametrize("relation_type", ["depends_on", "manual", "confirmed", "candidate"])
def test_dependencies_only_trusted_upstream_availability(session: Session, relation_type: str):
    up = asset(session, "fake-switch")
    up.role = "switch"
    down = asset(session, "fake-server")
    ep = endpoint(session, up)
    # 故意先添加下游规则，证明本轮规则顺序不会泄漏通知。
    rule(session, asset_ids=[str(down.id)])
    rule(session, name="上游不可达", kind="availability", metric_key="availability", asset_ids=[str(up.id)])
    if relation_type == "depends_on":
        session.add(AssetRelation(source_asset_id=down.id, target_asset_id=up.id, kind="depends_on"))
    else:
        session.add(NetworkLink(source_asset_id=up.id, target_asset_id=down.id,
                                origin="discovered" if relation_type == "candidate" else relation_type,
                                state="candidate" if relation_type == "candidate" else "confirmed"))
    sample(session, down, 95)
    status(session, ep)
    evaluate(session, now=NOW)
    down_alert = current(session, down)
    assert bool(down_alert.suppression_reason) == (relation_type != "candidate")
    down_notifications = [e for e in events(session, "trigger", notify=True) if e.alert_id == down_alert.id]
    assert len(down_notifications) == (1 if relation_type == "candidate" else 0)
    status(session, ep, NOW + timedelta(seconds=60), state="ok", error="", failures=0)
    evaluate(session, now=NOW + timedelta(seconds=60))
    assert not down_alert.suppression_reason
    assert len([e for e in events(session, "trigger", notify=True) if e.alert_id == down_alert.id]) == 1


def test_threshold_on_upstream_does_not_suppress_downstream_and_cycles_terminate(session: Session):
    up, down = asset(session, "fake-up"), asset(session, "fake-down")
    rule(session)
    session.add_all([AssetRelation(source_asset_id=down.id, target_asset_id=up.id, kind="depends_on"),
                     AssetRelation(source_asset_id=up.id, target_asset_id=down.id, kind="depends_on")])
    sample(session, up, 95)
    sample(session, down, 95)
    evaluate(session, now=NOW)
    assert len(events(session, "trigger", notify=True)) == 2


def test_evaluate_does_not_commit_callers_transaction(session: Session):
    obj = asset(session)
    rule(session)
    sample(session, obj, 95)
    session.commit()
    evaluate(session, now=NOW)
    assert current(session).state == "active"
    session.rollback()
    assert not list(session.scalars(select(Alert)))


def test_future_and_nonfinite_samples_cannot_trigger_or_block_next_valid_sample(session: Session):
    obj = asset(session)
    rule(session, consecutive_samples=2)
    sample(session, obj, 95)
    evaluate(session, now=NOW)
    sample(session, obj, 99, NOW + timedelta(hours=1))
    evaluate(session, now=NOW + timedelta(seconds=15))
    sample(session, obj, 96, NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=60))
    assert current(session).sample_count == 1
    sample(session, obj, float("inf"), NOW + timedelta(seconds=120))
    evaluate(session, now=NOW + timedelta(seconds=120))
    assert current(session).sample_count == 0
    assert not events(session, "trigger")


def test_silenced_recovery_persists_without_notification_and_expiry_without_data(session: Session):
    obj = asset(session)
    rule(session)
    row = sample(session, obj, 95)
    evaluate(session, now=NOW)
    alert = current(session)
    alert.state, alert.silenced_until = "silenced", NOW + timedelta(minutes=10)
    sample(session, obj, 70, NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=60))
    assert alert.state == "resolved"
    assert len(events(session, "recover", notify=False)) == 1
    sample(session, obj, 95, NOW + timedelta(seconds=120))
    evaluate(session, now=NOW + timedelta(seconds=120))
    alert.state, alert.silenced_until = "silenced", NOW + timedelta(seconds=150)
    alert.acknowledged_by = "fake-operator"
    session.delete(row)
    evaluate(session, now=NOW + timedelta(seconds=180))
    assert alert.state == "acknowledged"


def test_dependency_cycles_do_not_silence_all_root_causes(session: Session):
    first, second = asset(session, "fake-first"), asset(session, "fake-second")
    rule(session, kind="availability", metric_key="availability")
    for obj, upstream in ((first, second), (second, first)):
        status(session, endpoint(session, obj))
        session.add(AssetRelation(source_asset_id=obj.id, target_asset_id=upstream.id, kind="depends_on"))
    evaluate(session, now=NOW)
    assert len(events(session, "trigger", notify=True)) == 2


def notify(db: Session, at: datetime) -> list:
    """建（或沿用）一个启用的渠道，把到期事件规划成投递，返回本次新建的投递。"""
    from app.models import NotificationChannel, NotificationDelivery
    from app.notification_queue import plan_notifications
    if db.scalar(select(NotificationChannel).where(NotificationChannel.name == "fake-email")) is None:
        db.add(NotificationChannel(name="fake-email", kind="email", enabled=True, config={"group_seconds": 0}))
        db.flush()
    before = set(db.scalars(select(NotificationDelivery.id)))
    plan_notifications(db, now=at)
    return [d for d in db.scalars(select(NotificationDelivery)) if d.id not in before]


def test_recovery_inside_maintenance_window_is_sent_when_the_window_ends(session: Session):
    """维护窗口期间恢复：窗口内不发，窗口结束后补发一次恢复通知。"""
    obj = asset(session, tags=["lab"])
    rule(session)
    sample(session, obj, 95)
    evaluate(session, now=NOW)
    assert len(events(session, "trigger", notify=True)) == 1
    assert [d.status for d in notify(session, NOW)] == ["queued"]  # 触发通知真的发出去了
    session.add(MaintenanceWindow(name="fake-window", starts_at=NOW + timedelta(seconds=30), ends_at=NOW + timedelta(seconds=300), tags=["lab"]))
    sample(session, obj, 95, NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=60))
    sample(session, obj, 50, NOW + timedelta(seconds=120))
    evaluate(session, now=NOW + timedelta(seconds=120))
    assert current(session).state == "resolved" and not events(session, "recover", notify=True)  # 窗口内不发
    evaluate(session, now=NOW + timedelta(seconds=240))
    assert not events(session, "recover", notify=True)
    evaluate(session, now=NOW + timedelta(seconds=360))  # 窗口已结束
    sent = events(session, "recover", notify=True)
    assert len(sent) == 1 and "维护窗口期间已恢复" in sent[0].message
    from app.models import NotificationIntent
    notify(session, NOW + timedelta(seconds=360))
    assert session.scalar(select(NotificationIntent).where(NotificationIntent.event_id == sent[0].id)) is not None  # 真正进入投递
    evaluate(session, now=NOW + timedelta(seconds=420))
    assert len(events(session, "recover", notify=True)) == 1  # 只补发一次


def test_recovery_after_maintenance_status_is_sent_once(session: Session):
    obj = asset(session)
    rule(session)
    sample(session, obj, 95)
    evaluate(session, now=NOW)
    notify(session, NOW)
    obj.lifecycle = "maintenance"
    evaluate(session, now=NOW + timedelta(seconds=30))
    obj.lifecycle = "active"
    sample(session, obj, 50, NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=120))
    assert current(session).state == "resolved" and len(events(session, "recover", notify=True)) == 1


def test_never_notified_or_closed_alerts_get_no_late_recovery(session: Session):
    obj = asset(session, tags=["lab"])
    target = rule(session)
    session.add(MaintenanceWindow(name="fake-window", starts_at=NOW - timedelta(seconds=10), ends_at=NOW + timedelta(seconds=100), tags=["lab"]))
    sample(session, obj, 95)
    evaluate(session, now=NOW)  # 在窗口里触发，没通知过
    sample(session, obj, 50, NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=200))
    assert not events(session, notify=True)
    # 规则停用关闭的告警，重新启用后也不补发
    other = asset(session, "fake-other")
    sample(session, other, 95, NOW + timedelta(seconds=200))
    evaluate(session, now=NOW + timedelta(seconds=200))
    assert len(events(session, "trigger", notify=True)) == 1
    target.enabled = False
    evaluate(session, now=NOW + timedelta(seconds=230))
    target.enabled = True
    sample(session, other, 50, NOW + timedelta(seconds=260))
    evaluate(session, now=NOW + timedelta(seconds=260))
    assert not events(session, "recover", notify=True)


def test_no_late_recovery_when_trigger_notice_was_cancelled(session: Session):
    """预览走查发现：触发通知还在合并等待时维护窗口开始，投递被取消、用户从没收到；窗口结束后不能只补发一条“已恢复”。"""
    from app.models import NotificationChannel, NotificationDelivery
    from app.notification_queue import plan_notifications
    obj = asset(session, tags=["lab"])
    rule(session)
    session.add(NotificationChannel(name="fake-grouped", kind="email", enabled=True, config={"group_seconds": 30}))
    sample(session, obj, 95)
    evaluate(session, now=NOW)
    plan_notifications(session, now=NOW)  # 只登记意图，30 秒后才合并发出
    session.add(MaintenanceWindow(name="fake-window", starts_at=NOW + timedelta(seconds=10), ends_at=NOW + timedelta(seconds=300), tags=["lab"]))
    session.flush()
    plan_notifications(session, now=NOW + timedelta(seconds=40))  # 到期时已在窗口里：投递取消
    assert [d.status for d in session.scalars(select(NotificationDelivery))] == ["cancelled"]
    sample(session, obj, 50, NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=60))
    evaluate(session, now=NOW + timedelta(seconds=360))  # 窗口已结束
    assert current(session).state == "resolved"
    assert not events(session, "recover", notify=True)
