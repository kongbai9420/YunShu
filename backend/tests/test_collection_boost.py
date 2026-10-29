"""4.4 P20d 按状态临时提高采集频率：接近阈值 / 告警 / 推送事件时提频，维护中不提频，提频资产每 15 秒入队。不连接任何设备。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metrics, snmp_trap
from app.alerting import evaluate
from app.collect import boost
from app.collect.runner import enqueue_due
from app.metrics import Point
from app.models import AlertRule, Asset, CollectionBoost, MaintenanceWindow, ManagementEndpoint, Task

NOW = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)


def server(db: Session, name: str, *, lifecycle: str = "active", tags: list[str] | None = None) -> Asset:
    a = Asset(name=name, form="physical", role="server", platform="iDRAC9", collection_profile="dell-server", lifecycle=lifecycle, tags=tags or [])
    a.endpoints.append(ManagementEndpoint(address="192.0.2.80", protocol="snmp", purpose="monitoring"))
    db.add(a)
    db.commit()
    return a


def temp(db: Session, a: Asset, value: float, at: datetime) -> None:
    metrics.write_sample(db, a.id, "snmp", [Point("cpu.temp", value, "°C")], observed_at=at, interval_seconds=60)
    db.commit()


def test_near_threshold() -> None:
    assert boost.near_threshold("gte", 80, 73) and not boost.near_threshold("gte", 80, 71) and not boost.near_threshold("gte", 80, 81)
    assert boost.near_threshold("lte", 10, 10.5) and not boost.near_threshold("lte", 10, 12) and not boost.near_threshold("lte", 10, 9)
    assert boost.near_threshold("gte", 0, -0.5)


def test_alert_evaluation_boosts_near_threshold_and_open_alerts_but_not_maintenance(session: Session) -> None:
    session.add(AlertRule(name="fake CPU 温度", metric_key="cpu.temp", threshold=80, recovery_threshold=75, severity="warning"))
    near, hot, calm, fixing = server(session, "fake-near"), server(session, "fake-hot"), server(session, "fake-calm"), server(session, "fake-fixing", tags=["fake-mw"])
    session.add(MaintenanceWindow(name="fake-mw", starts_at=NOW - timedelta(hours=1), ends_at=NOW + timedelta(hours=1), tags=["fake-mw"]))
    for a, v in ((near, 74.0), (hot, 85.0), (calm, 50.0), (fixing, 79.0)):
        temp(session, a, v, NOW - timedelta(seconds=5))
    evaluate(session, now=NOW)
    session.commit()
    rows = {r.asset_id: r for r in session.scalars(select(CollectionBoost))}
    assert set(rows) == {near.id, hot.id}
    assert rows[near.id].reason.startswith("接近阈值：fake CPU 温度") and rows[hot.id].reason == "告警：fake CPU 温度"
    assert rows[hot.id].until.replace(tzinfo=UTC) == NOW + boost.HOLD
    # 告警仍在：每次求值都延长；恢复后不再延长，10 分钟后自然结束
    temp(session, hot, 60.0, NOW + timedelta(seconds=55))
    evaluate(session, now=NOW + timedelta(minutes=1))
    session.commit()
    assert session.get(CollectionBoost, hot.id).until.replace(tzinfo=UTC) == NOW + boost.HOLD  # 已恢复：没有延长
    assert boost.describe(session.get(CollectionBoost, hot.id), NOW + timedelta(minutes=11)) is None


def test_boosted_assets_are_enqueued_every_15_seconds_with_a_cap(session: Session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a, b, c = server(session, "fake-a"), server(session, "fake-b"), server(session, "fake-c")
    boost.mark(session, a.id, "fake", NOW)
    boost.mark(session, b.id, "fake", NOW - timedelta(seconds=1))
    session.commit()
    monkeypatch.setenv("YUNSHU_BOOST_MAX", "1")  # 只有最近登记的 a 提频
    for i in range(4):
        enqueue_due(session, now=NOW + timedelta(seconds=15 * i))
    session.commit()
    count = {x.id: len(session.scalars(select(Task).where(Task.asset_id == x.id, Task.kind == "collect.asset")).all()) for x in (a, b, c)}
    assert count == {a.id: 4, b.id: 1, c.id: 1}


def test_pushed_critical_event_boosts_and_collects_now(session: Session) -> None:
    from app.crypto import CredentialCipher, KeyRing
    from app.services.credentials import create_credential

    cipher = CredentialCipher(KeyRing.generate())
    a = server(session, "fake-trap")
    cred = create_credential(session, cipher, alias="fake-snmp-boost", kind="snmp_community", username="", secret="fake-community")
    a.endpoints[0].credential_id = cred.id
    session.commit()
    from app.services.credentials import reveal_secret
    from tests.test_bmc_push import dell_trap

    r = snmp_trap.TrapReceiver(lambda: Session(bind=session.get_bind(), expire_on_commit=False), lambda c: reveal_secret(c, cipher), port=0)
    assert r.handle(dell_trap("fake: Fan 3 RPM is outside of normal range.", status=4), "192.0.2.80", now=NOW)[0] == "accepted"
    assert r.handle(dell_trap("fake: Fan 3 RPM returned to normal.", status=3, uptime=9), "192.0.2.80", now=NOW)[0] == "accepted"
    session.expire_all()
    row = session.get(CollectionBoost, a.id)
    assert row is not None and "SNMP Trap" in row.reason
    tasks = session.scalars(select(Task).where(Task.asset_id == a.id, Task.kind == "collect.asset")).all()
    assert len(tasks) == 1 and tasks[0].payload.get("event") is True  # 已恢复的提示类事件不再补采


def test_collection_api_shows_boost(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    from tests.api_helpers import add_user, login

    add_user(factory, "vic", "viewer")
    viewer = login(app_factory(), "vic")
    with factory() as db:
        a = server(db, "fake-api")
        boost.mark(db, a.id, "告警：fake", datetime.now(UTC))
        db.commit()
        aid = a.id
    out = viewer.get(f"/api/assets/{aid}/collection").json()
    assert out["boost"]["interval"] == 15 and out["boost"]["reason"] == "告警：fake"
