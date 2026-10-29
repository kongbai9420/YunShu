"""维护中的预计结束时间：改为维护中时填写（默认 7 天后），过期还没改回在用就提醒。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import inspection
from app.alert_defaults import ensure_rules
from app.alerting import MAINTENANCE_METRIC, evaluate
from app.models import Alert, AlertEvent, Asset, NotificationChannel, NotificationIntent
from app.notification_queue import plan_notifications
from tests.api_helpers import add_user, login

SERVER = {"name": "fake-maint-01", "form": "physical", "role": "server"}


@pytest.fixture
def operator(app_factory, factory) -> TestClient:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    return login(app_factory(), "olivia")


def _aware(value: str) -> datetime:
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def test_expected_end_is_set_kept_and_cleared(operator: TestClient) -> None:
    a = operator.post("/api/assets", json=SERVER).json()
    assert a["maintenance_until"] is None
    # 改为维护中没填：默认 7 天后
    a = operator.patch(f"/api/assets/{a['id']}", json={"revision": a["revision"], "lifecycle": "maintenance"}).json()
    until = _aware(a["maintenance_until"])
    assert timedelta(days=6, hours=23) < until - datetime.now(UTC) <= timedelta(days=7)
    # 改别的字段不动它；填了过去的时间拒绝；可以改成别的未来时间
    r = operator.patch(f"/api/assets/{a['id']}", json={"revision": a["revision"], "notes": "fake"})
    assert r.status_code == 200, r.text
    a = r.json()
    assert _aware(a["maintenance_until"]) == until
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert operator.patch(f"/api/assets/{a['id']}", json={"revision": a["revision"], "maintenance_until": past}).status_code == 422
    later = (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0)
    a = operator.patch(f"/api/assets/{a['id']}", json={"revision": a["revision"], "maintenance_until": later.isoformat()}).json()
    assert _aware(a["maintenance_until"]) == later
    # 改回在用清空
    a = operator.patch(f"/api/assets/{a['id']}", json={"revision": a["revision"], "lifecycle": "active"}).json()
    assert a["maintenance_until"] is None
    # 新建时直接是维护中，也有默认值
    b = operator.post("/api/assets", json={**SERVER, "name": "fake-maint-02", "lifecycle": "maintenance"}).json()
    assert b["maintenance_until"] is not None


def test_overdue_maintenance_alerts_notifies_and_recovers(session: Session) -> None:
    now = datetime(2026, 10, 28, 2, tzinfo=UTC)
    ensure_rules(session)
    a = Asset(name="fake-maint-03", form="physical", role="server", lifecycle="maintenance", maintenance_until=now + timedelta(hours=1))
    imported = Asset(name="fake-maint-imported", form="physical", role="server", lifecycle="maintenance")  # 导入的：没有预计结束，不提醒
    session.add_all([a, imported, NotificationChannel(name="fake-email", kind="email", enabled=True, config={"group_seconds": 0})])
    session.commit()

    evaluate(session, now=now)
    assert session.scalar(select(Alert).where(Alert.metric_key == MAINTENANCE_METRIC)) is None
    later = now + timedelta(hours=2)
    evaluate(session, now=later)
    alert = session.scalar(select(Alert).where(Alert.metric_key == MAINTENANCE_METRIC))
    assert alert.asset_id == a.id and alert.state == "active" and alert.suppression_reason == ""
    trig = session.scalar(select(AlertEvent).where(AlertEvent.alert_id == alert.id, AlertEvent.kind == "trigger"))
    assert trig.notify and "维护中已超过预计结束时间" in trig.message
    plan_notifications(session, now=later)
    assert session.scalar(select(NotificationIntent).where(NotificationIntent.event_id == trig.id)) is not None  # 维护中的资产也真正发出

    items = {(i["asset_name"], i["title"]) for i in inspection.inspect(session, later)[0]}
    assert ("fake-maint-03", "维护已超过预计结束时间") in items and not any(n == "fake-maint-imported" for n, _ in items)

    a.lifecycle, a.maintenance_until = "active", None  # 修好了，改回在用
    evaluate(session, now=later + timedelta(minutes=1))
    assert alert.state == "resolved"
    assert session.scalar(select(AlertEvent).where(AlertEvent.alert_id == alert.id, AlertEvent.kind == "recover")).notify


def test_extending_the_expected_end_recovers(session: Session) -> None:
    now = datetime(2026, 10, 28, 2, tzinfo=UTC)
    ensure_rules(session)
    a = Asset(name="fake-maint-04", form="physical", role="server", lifecycle="maintenance", maintenance_until=now - timedelta(minutes=5))
    session.add(a)
    session.commit()
    evaluate(session, now=now)
    alert = session.scalar(select(Alert).where(Alert.metric_key == MAINTENANCE_METRIC))
    assert alert.state == "active"
    a.maintenance_until = now + timedelta(days=1)
    evaluate(session, now=now + timedelta(minutes=1))
    assert alert.state == "resolved"
