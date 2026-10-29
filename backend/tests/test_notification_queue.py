"""通知真实 SQLite / 任务 / Worker 链路回归；只将网络发送替换为 fake。"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import queue
from app.crypto import CredentialCipher, KeyRing
from app.db import make_session_factory
from app.models import Alert, AlertEvent, AlertRule, Asset, MaintenanceWindow, NotificationChannel, NotificationDelivery, NotificationIntent, Task
from app.notification_queue import enqueue_delivery, make_handler, plan_notifications
from app.notifications import NotificationError
from app.worker import Worker


@pytest.fixture
def database(engine):
    return make_session_factory(engine)


@pytest.fixture
def workers(database):
    created = []

    def make(sender):
        cipher = CredentialCipher(KeyRing.generate())
        worker = Worker(database, {"notification.send": make_handler(lambda: cipher, sender)}, owner="fake-worker")
        created.append(worker)
        return worker

    yield make
    for worker in created:
        worker._pool.shutdown(wait=True, cancel_futures=True)


def seed(database, *, group_seconds=0, severity="warning", tags=None, event_kind="trigger"):
    now = datetime.now(UTC) - timedelta(seconds=1)
    with database() as db:
        asset = Asset(name="fake-server", form="physical", role="server", tags=tags or ["lab"])
        rule = AlertRule(name="fake-rule", metric_key="cpu.load", threshold=90, recovery_threshold=80)
        channel = NotificationChannel(name="fake-email", kind="email", enabled=True,
                                      config={"group_seconds": group_seconds})
        db.add_all([asset, rule, channel])
        db.flush()
        alert = Alert(rule_id=rule.id, asset_id=asset.id, asset_name=asset.name, metric_key="cpu.load",
                      state="resolved" if event_kind == "recover" else "active", severity=severity,
                      title="fake-server · CPU 高负载", value=95, unit="%", threshold=90)
        db.add(alert)
        db.flush()
        event = AlertEvent(alert_id=alert.id, kind=event_kind, title=alert.title, message="fake-metric=95%",
                           severity=severity, episode=1, created_at=now, notify=True, routed=False)
        db.add(event)
        db.commit()
        return {"asset": asset.id, "rule": rule.id, "channel": channel.id, "alert": alert.id, "event": event.id, "now": now}


def plan(database, now):
    with database() as db:
        result = plan_notifications(db, now=now)
        db.commit()
        return result


def rows(database, model):
    with database() as db:
        return list(db.scalars(select(model)))


def make_available(database):
    """推进任务可执行时间，验证真实重试逻辑而不让测试等待30/60秒。"""
    with database() as db:
        for task in db.scalars(select(Task).where(Task.status == "queued")):
            task.available_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()


def test_events_aggregate_once_with_durable_routing_and_one_task(database, workers):
    data = seed(database, group_seconds=30)
    with database() as db:
        first = db.get(AlertEvent, data["event"])
        db.add(AlertEvent(alert_id=data["alert"], kind="repeat", title=first.title, message="仍然超限",
                          severity="warning", episode=1, created_at=data["now"], notify=True, routed=False))
        db.commit()
    assert plan(database, data["now"]) == {"routed": 2, "deliveries": 0}
    assert plan(database, data["now"] + timedelta(seconds=29)) == {"routed": 0, "deliveries": 0}
    assert len(rows(database, NotificationIntent)) == 2
    assert plan(database, data["now"] + timedelta(seconds=30)) == {"routed": 0, "deliveries": 1}
    assert plan(database, data["now"] + timedelta(seconds=60)) == {"routed": 0, "deliveries": 0}
    deliveries = rows(database, NotificationDelivery)
    assert len(deliveries) == 1
    assert len(deliveries[0].event_ids) == 2
    assert "2 条" in deliveries[0].title
    assert "仍然超限" in deliveries[0].text
    assert len(rows(database, Task)) == 1
    calls = []
    worker = workers(lambda *args: calls.append(args) or {"accepted": True})
    make_available(database)
    assert worker.run_once() == "succeeded"
    assert worker.run_once() is None
    assert len(calls) == 1
    assert rows(database, NotificationDelivery)[0].status == "accepted"


def test_severity_tag_and_explicit_rule_channel_routes(database):
    data = seed(database, severity="critical", tags=["production"])
    with database() as db:
        good = db.get(NotificationChannel, data["channel"])
        good.config = {"group_seconds": 0, "tags": ["production"], "severities": ["critical"]}
        wrong_severity = NotificationChannel(name="fake-warning", kind="email", enabled=True,
                                             config={"group_seconds": 0, "severities": ["warning"]})
        wrong_tag = NotificationChannel(name="fake-other-tag", kind="email", enabled=True,
                                       config={"group_seconds": 0, "tags": ["other"]})
        unselected = NotificationChannel(name="fake-unselected", kind="email", enabled=True, config={"group_seconds": 0})
        db.add_all([wrong_severity, wrong_tag, unselected])
        db.flush()
        db.get(AlertRule, data["rule"]).channel_ids = [str(good.id), str(wrong_severity.id), str(wrong_tag.id)]
        db.commit()
    assert plan(database, data["now"]) == {"routed": 1, "deliveries": 1}
    assert [row.channel_id for row in rows(database, NotificationDelivery)] == [data["channel"]]


@pytest.mark.parametrize("suppression", ["silenced", "maintenance", "lifecycle", "acknowledged", "disabled", "scope", "recovered", "old-episode"])
def test_pending_group_rechecks_suppression_and_never_sends_late(database, suppression):
    data = seed(database, group_seconds=30)
    assert plan(database, data["now"])["routed"] == 1
    with database() as db:
        alert = db.get(Alert, data["alert"])
        if suppression == "silenced":
            alert.state, alert.silenced_until = "silenced", data["now"] + timedelta(minutes=5)
        elif suppression == "maintenance":
            db.add(MaintenanceWindow(name="fake-window", starts_at=data["now"], ends_at=data["now"] + timedelta(minutes=5), asset_ids=[str(data["asset"])]))
        elif suppression == "lifecycle":
            db.get(Asset, data["asset"]).lifecycle = "maintenance"
        elif suppression == "acknowledged":
            alert.state, alert.acknowledged_by = "acknowledged", "fake-operator"
        elif suppression == "disabled":
            db.get(AlertRule, data["rule"]).enabled = False
        elif suppression == "scope":
            db.get(AlertRule, data["rule"]).tags = ["other"]
        elif suppression == "recovered":
            alert.state = "resolved"
        else:
            alert.episode = 2
        db.commit()
    assert plan(database, data["now"] + timedelta(seconds=30))["deliveries"] == 0
    assert rows(database, NotificationDelivery)[0].status == "cancelled"
    assert not rows(database, Task)
    # 不在维护/静默结束后迟发已经取消的旧告警。
    assert plan(database, data["now"] + timedelta(minutes=10))["deliveries"] == 0


@pytest.mark.parametrize("change", ["disable-channel", "revision", "delete-channel", "silence-alert", "maintenance"])
def test_handler_rechecks_channel_and_alert_before_sender(database, workers, change):
    data = seed(database)
    plan(database, data["now"])
    with database() as db:
        channel = db.get(NotificationChannel, data["channel"])
        if change == "disable-channel":
            channel.enabled = False
        elif change == "revision":
            channel.revision += 1
            channel.config = {"group_seconds": 0, "subject_template": "fake-modified"}
        elif change == "delete-channel":
            channel.deleted = True
        elif change == "silence-alert":
            alert = db.get(Alert, data["alert"])
            alert.state, alert.silenced_until = "silenced", datetime.now(UTC) + timedelta(minutes=5)
        else:
            db.add(MaintenanceWindow(name="fake-window", starts_at=data["now"], ends_at=datetime.now(UTC) + timedelta(minutes=5), tags=["lab"]))
        db.commit()
    calls = []
    worker = workers(lambda *args: calls.append(args) or {"accepted": True})
    assert worker.run_once() == "succeeded"
    assert not calls
    delivery = rows(database, NotificationDelivery)[0]
    assert (delivery.status, delivery.attempts) == ("cancelled", 0)


def test_acknowledged_alert_recovery_uses_engine_recover_kind(database, workers):
    data = seed(database, event_kind="recover")
    with database() as db:
        db.get(Alert, data["alert"]).acknowledged_by = "fake-operator"
        db.commit()
    assert plan(database, data["now"]) == {"routed": 1, "deliveries": 1}
    calls = []
    worker = workers(lambda *args: calls.append(args) or {"accepted": True})
    assert worker.run_once() == "succeeded"
    assert len(calls) == 1
    assert "恢复" in calls[0][3].text


@pytest.mark.parametrize("refused", [0, 1])
def test_smtp_service_acceptance_is_only_accepted_and_deduplicated(database, workers, refused):
    data = seed(database)
    plan(database, data["now"])
    calls = []

    def sender(kind, config, secrets, message):
        assert kind == "email"
        assert secrets == {}
        calls.append(message)
        return {"accepted": True, "refused_count": refused}

    worker = workers(sender)
    assert worker.run_once() == "succeeded"
    delivery = rows(database, NotificationDelivery)[0]
    assert (delivery.status, delivery.attempts) == ("accepted", 1)
    assert delivery.finished_at is not None
    assert bool(delivery.error) == bool(refused)
    with database() as db:
        # 幂等键相同的重复入队返回原任务，不会再发送。
        enqueue_delivery(db, db.get(NotificationDelivery, delivery.id))
        db.commit()
    assert worker.run_once() is None
    assert len(calls) == 1
    assert len(rows(database, Task)) == 1


def test_known_temporary_failures_retry_three_attempts_and_stop(database, workers):
    data = seed(database)
    plan(database, data["now"])
    calls = []

    def sender(*args):
        calls.append(args)
        raise NotificationError("fake-service temporary failure", retryable=True)

    worker = workers(sender)
    for attempt in (1, 2):
        make_available(database)
        assert worker.run_once() == "queued"
        delivery, task = rows(database, NotificationDelivery)[0], rows(database, Task)[0]
        assert (delivery.status, delivery.attempts, task.attempts) == ("retrying", attempt, attempt)
        # SQLite 读回无时区时间，PostgreSQL（timestamptz）读回带时区；统一按 UTC 比较
        available = task.available_at if task.available_at.tzinfo else task.available_at.replace(tzinfo=UTC)
        assert available > datetime.now(UTC)
    make_available(database)
    assert worker.run_once() == "succeeded"
    delivery = rows(database, NotificationDelivery)[0]
    assert (delivery.status, delivery.attempts) == ("failed", 3)
    assert delivery.finished_at is not None
    assert worker.run_once() is None
    assert len(calls) == 3


def test_known_failure_can_recover_on_retry(database, workers):
    data = seed(database)
    plan(database, data["now"])
    calls = []

    def sender(*args):
        calls.append(args)
        if len(calls) == 1:
            raise NotificationError("fake-service busy", retryable=True)
        return {"accepted": True}

    worker = workers(sender)
    assert worker.run_once() == "queued"
    make_available(database)
    assert worker.run_once() == "succeeded"
    assert rows(database, NotificationDelivery)[0].status == "accepted"
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["uncertain", "unexpected", "permanent", "not-accepted"])
def test_uncertain_and_permanent_results_never_automatically_resend(database, workers, failure):
    data = seed(database)
    plan(database, data["now"])
    calls = []

    def sender(*args):
        calls.append(args)
        if failure == "uncertain":
            raise NotificationError("fake-after-submit timeout", retryable=True, uncertain=True)
        if failure == "unexpected":
            raise RuntimeError("fake-unexpected")
        if failure == "permanent":
            raise NotificationError("fake-auth rejection", retryable=False)
        return {"accepted": False}

    worker = workers(sender)
    assert worker.run_once() == "succeeded"
    delivery = rows(database, NotificationDelivery)[0]
    assert delivery.status == ("uncertain" if failure in {"uncertain", "unexpected"} else "failed")
    assert delivery.attempts == 1
    make_available(database)
    assert worker.run_once() is None
    assert len(calls) == 1


def test_lost_worker_lease_cannot_overwrite_or_send_twice(database, workers):
    data = seed(database)
    plan(database, data["now"])
    calls = []

    def sender(*args):
        calls.append(args)
        with database() as db:
            task = db.scalars(select(Task)).one()
            task.lease_token = "fake-replacement-token"
            task.lease_owner = "fake-replacement-worker"
            db.commit()
        return {"accepted": True}

    worker = workers(sender)
    assert worker.run_once() is None
    assert rows(database, NotificationDelivery)[0].status == "sending"
    plan(database, datetime.now(UTC))
    assert rows(database, NotificationDelivery)[0].status == "uncertain"
    # 即使过期任务被队列回收并再次认领，也不能再次调用sender。
    with database() as db:
        task = db.scalars(select(Task)).one()
        task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert worker.run_once() is None
    make_available(database)
    assert worker.run_once() == "succeeded"
    assert rows(database, NotificationDelivery)[0].status == "uncertain"
    assert len(calls) == 1


def test_delivery_generation_replacement_blocks_old_sender_result(database, workers):
    data = seed(database)
    plan(database, data["now"])

    def sender(*args):
        with database() as db:
            delivery = db.scalars(select(NotificationDelivery)).one()
            delivery.generation += 1
            delivery.status, delivery.error = "cancelled", "fake-new-generation"
            db.commit()
        return {"accepted": True}

    worker = workers(sender)
    assert worker.run_once() == "succeeded"
    delivery = rows(database, NotificationDelivery)[0]
    assert (delivery.status, delivery.error, delivery.generation) == ("cancelled", "fake-new-generation", 2)
    assert rows(database, Task)[0].result == {"status": "superseded"}


def test_crashed_sending_delivery_is_reconciled_and_not_resent(database, workers):
    data = seed(database)
    plan(database, data["now"])
    with database() as db:
        delivery = db.scalars(select(NotificationDelivery)).one()
        delivery.status, delivery.lease_token, delivery.attempts = "sending", "fake-dead-worker", 1
        db.commit()
    plan(database, datetime.now(UTC))
    assert rows(database, NotificationDelivery)[0].status == "uncertain"
    calls = []
    worker = workers(lambda *args: calls.append(args) or {"accepted": True})
    assert worker.run_once() == "succeeded"
    assert not calls


def test_bad_credentials_fail_before_sending_without_exposing_secrets(database, workers):
    data = seed(database)
    with database() as db:
        db.get(NotificationChannel, data["channel"]).kind = "dingtalk"
        db.commit()
    plan(database, data["now"])
    calls = []
    worker = workers(lambda *args: calls.append(args) or {"accepted": True})
    assert worker.run_once() == "succeeded"
    delivery = rows(database, NotificationDelivery)[0]
    assert (delivery.status, delivery.attempts) == ("failed", 0)
    assert delivery.error == "通知凭据缺失或无法解密，请重新保存渠道凭据"
    assert not calls


def test_empty_severity_selection_routes_all_levels(database):
    data = seed(database)
    with database() as db:
        db.get(NotificationChannel, data["channel"]).config = {"severities": [], "group_seconds": 0}
        db.commit()
    assert plan(database, data["now"])["deliveries"] == 1


def test_large_outage_aggregates_and_bounds_channel_rate(database):
    data = seed(database, group_seconds=30)
    with database() as db:
        db.add_all([AlertEvent(alert_id=data["alert"], kind="repeat", title=f"fake-outage-{i}", message="fake-connection-timeout",
                               episode=1, severity="warning", created_at=data["now"], notify=True) for i in range(999)])
        db.commit()
    assert plan(database, data["now"]) == {"routed": 1000, "deliveries": 0}
    assert plan(database, data["now"] + timedelta(seconds=30))["deliveries"] == 1
    assert plan(database, data["now"] + timedelta(seconds=31))["deliveries"] == 0
    assert plan(database, data["now"] + timedelta(seconds=60))["deliveries"] == 1
    deliveries = rows(database, NotificationDelivery)
    assert len(deliveries) == 2 and all(len(d.event_ids) == 500 for d in deliveries)
    assert all("另有 480 条" in d.text and len(d.text) <= 24000 for d in deliveries)


def test_rule_routing_change_cancels_previously_queued_delivery(database, workers):
    data = seed(database)
    plan(database, data["now"])
    with database() as db:
        db.get(AlertRule, data["rule"]).channel_ids = [str(uuid.uuid4())]
        db.commit()
    calls = []
    worker = workers(lambda *args: calls.append(args) or {"accepted": True})
    assert worker.run_once() == "succeeded"
    assert rows(database, NotificationDelivery)[0].status == "cancelled" and not calls
