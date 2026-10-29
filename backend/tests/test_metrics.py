from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metrics
from app.metrics import Point, Retention
from app.models import Asset, LatestMetric, MetricRollup
from tests.api_helpers import add_user, login

T = datetime(2026, 10, 16, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def asset(session: Session) -> Asset:
    a = Asset(name="甲", form="physical", role="server")
    session.add(a)
    session.commit()
    return a


def test_point_rules() -> None:
    with pytest.raises(ValueError):
        Point("cpu.temp", None)  # 有效值不能为空
    with pytest.raises(ValueError):
        Point("cpu.temp", 1, quality="stale")  # stale 只在读取时计算
    p = Point("cpu.temp", 55, quality="failed")
    assert p.value is None  # 失败不保留数值，不会显示成 0
    assert Point("fan.pct", 40, quality="estimated").estimated


def test_write_and_latest(session: Session, asset: Asset) -> None:
    metrics.write_sample(
        session, asset.id, "ipmi",
        [Point("cpu.temp", 61.0, "°C"), Point("power.watts", None, "W", quality="unsupported", message="BMC 不提供功耗")],
        observed_at=T,
    )
    session.commit()
    got = {v.key: v for v in metrics.latest(session, asset.id, now=T + timedelta(seconds=30))}
    assert got["cpu.temp"].value == 61.0 and got["cpu.temp"].quality == "valid"
    assert got["power.watts"].value is None and got["power.watts"].quality == "unsupported"
    assert metrics.raw_tables(session) == ["metric_raw_20261016"]


def test_stale_computed_on_read(session: Session, asset: Asset) -> None:
    metrics.write_sample(session, asset.id, "snmp", [Point("cpu.load", 12.0, "%")], observed_at=T, interval_seconds=60)
    session.commit()
    fresh = metrics.latest(session, asset.id, now=T + timedelta(seconds=150))[0]
    stale = metrics.latest(session, asset.id, now=T + timedelta(seconds=181))[0]
    assert fresh.quality == "valid"
    assert stale.quality == "stale" and stale.value is None and stale.last_valid_value == 12.0


def test_failure_keeps_last_valid(session: Session, asset: Asset) -> None:
    metrics.write_sample(session, asset.id, "ipmi", [Point("cpu.temp", 60.0, "°C")], observed_at=T)
    metrics.write_sample(session, asset.id, "ipmi", [Point("cpu.temp", None, "°C", quality="failed", message="超时")], observed_at=T + timedelta(minutes=1))
    session.commit()
    row = session.get(LatestMetric, (asset.id, "cpu.temp"))
    assert row.quality == "failed" and row.value is None
    assert row.last_valid_value == 60.0


def test_older_sample_does_not_override_latest(session: Session, asset: Asset) -> None:
    metrics.write_sample(session, asset.id, "ipmi", [Point("cpu.temp", 70.0)], observed_at=T + timedelta(minutes=5))
    metrics.write_sample(session, asset.id, "ipmi", [Point("cpu.temp", 50.0)], observed_at=T)
    session.commit()
    assert session.get(LatestMetric, (asset.id, "cpu.temp")).value == 70.0


def test_day_partitioning(session: Session, asset: Asset) -> None:
    for d in range(3):
        metrics.write_sample(session, asset.id, "ipmi", [Point("cpu.temp", 50.0 + d)], observed_at=T + timedelta(days=d))
    session.commit()
    assert metrics.raw_tables(session) == ["metric_raw_20261016", "metric_raw_20261017", "metric_raw_20261018"]


def test_rollup_minute_and_hour(session: Session, asset: Asset) -> None:
    # 两个小时，每 15 秒一个点：第一个小时 40，第二个小时 60；中间夹一个失败点
    for i in range(0, 2 * 3600, 15):
        v = 40.0 if i < 3600 else 60.0
        metrics.write_sample(session, asset.id, "ipmi", [Point("cpu.temp", v)], observed_at=T + timedelta(seconds=i), interval_seconds=15)
    metrics.write_sample(session, asset.id, "ipmi", [Point("cpu.temp", None, quality="failed")], observed_at=T + timedelta(seconds=7))
    session.commit()
    out = metrics.rollup(session, now=T + timedelta(hours=2, seconds=5))
    session.commit()
    assert out == {"minute": 120, "hour": 2}
    # 压缩存放：分钟汇总每小时一行，小时汇总每天一行
    assert session.query(MetricRollup).filter_by(resolution=60).count() == 2
    assert session.query(MetricRollup).filter_by(resolution=3600).count() == 1
    minutes = metrics.series(session, asset.id, "cpu.temp", T, T + timedelta(hours=2), resolution="minute")["points"]
    assert len(minutes) == 120 and all(p["n"] == 4 for p in minutes)  # 失败点不计入
    hours = metrics.series(session, asset.id, "cpu.temp", T, T + timedelta(hours=2), resolution="hour")["points"]
    assert [round(h["avg"]) for h in hours] == [40, 60] and [h["n"] for h in hours] == [240, 240]

    # 重复执行不重复计数
    again = metrics.rollup(session, now=T + timedelta(hours=2, seconds=5))
    session.commit()
    assert again == {"minute": 0, "hour": 0}
    assert [h["n"] for h in metrics.series(session, asset.id, "cpu.temp", T, T + timedelta(hours=2), resolution="hour")["points"]] == [240, 240]


def test_rollup_min_max(session: Session, asset: Asset) -> None:
    for i, v in enumerate([10.0, 30.0, 20.0]):
        metrics.write_sample(session, asset.id, "ipmi", [Point("cpu.temp", v)], observed_at=T + timedelta(seconds=i * 10))
    session.commit()
    metrics.rollup(session, now=T + timedelta(minutes=2))
    session.commit()
    p = metrics.series(session, asset.id, "cpu.temp", T, T + timedelta(minutes=1), resolution="minute")["points"][0]
    assert (p["min"], p["max"], p["avg"], p["n"]) == (10.0, 30.0, 20.0, 3)


def test_rollup_skips_current_minute(session: Session, asset: Asset) -> None:
    metrics.write_sample(session, asset.id, "ipmi", [Point("cpu.temp", 40.0)], observed_at=T + timedelta(seconds=10))
    session.commit()
    assert metrics.rollup(session, now=T + timedelta(seconds=50))["minute"] == 0
    assert metrics.rollup(session, now=T + timedelta(seconds=70))["minute"] == 1


def test_retention_drops_whole_day_tables(session: Session, asset: Asset) -> None:
    for d in range(4):
        metrics.write_sample(session, asset.id, "ipmi", [Point("cpu.temp", 50.0)], observed_at=T + timedelta(days=d))
    now = T + timedelta(days=3, hours=12)  # 10-19 22:00；保留 48 小时 → 截止 10-17 22:00
    old_minute = int((now - timedelta(days=31)).timestamp()) // 3600 * 3600
    old_hour = int((now - timedelta(days=400)).timestamp()) // 86400 * 86400
    keep_hour = int((now - timedelta(days=100)).timestamp()) // 86400 * 86400
    for res, b in ((60, old_minute), (3600, old_hour), (3600, keep_hour)):
        session.add(MetricRollup(asset_id=asset.id, resolution=res, bucket=b, data="{}"))
    session.commit()
    out = metrics.enforce_retention(session, Retention(raw_hours=48), now=now)
    session.commit()
    assert out == {"raw_tables": 1, "minute": 1, "hour": 1}
    assert metrics.raw_tables(session) == ["metric_raw_20261017", "metric_raw_20261018", "metric_raw_20261019"]


def test_series_resolutions(session: Session, asset: Asset) -> None:
    for i in range(0, 3600, 60):
        metrics.write_sample(session, asset.id, "ipmi", [Point("cpu.temp", float(i // 60))], observed_at=T + timedelta(seconds=i))
    session.commit()
    metrics.rollup(session, now=T + timedelta(hours=1, minutes=1))
    session.commit()
    raw = metrics.series(session, asset.id, "cpu.temp", T, T + timedelta(hours=1))
    assert raw["resolution"] == "raw" and len(raw["points"]) == 60
    week = metrics.series(session, asset.id, "cpu.temp", T - timedelta(days=6), T + timedelta(hours=1))
    assert week["resolution"] == "minute" and len(week["points"]) == 60
    month = metrics.series(session, asset.id, "cpu.temp", T - timedelta(days=30), T + timedelta(hours=1))
    assert month["resolution"] == "hour" and len(month["points"]) == 1


def test_schedules_registered() -> None:
    from app.settings import Settings
    from app.tasks import build_schedules

    names = [s.name for s in build_schedules(Settings.from_env())]
    assert names == ["collect.enqueue", "alerts.evaluate", "control.reconcile", "thermal.policies", "metrics.rollup", "metrics.retention", "inspection.daily", "tasks.prune", "inventory.packages", "server_events.prune", "inspection.jobs", "inspection.prune", "bmc.prune", "history.prune", "assets.purge", "vuln.sync", "vuln.match", "vuln.notify"]


def test_metrics_api(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "victor", "viewer")
    c = login(app_factory(), "victor")
    with factory() as db:
        a = Asset(name="甲", form="physical", role="server")
        db.add(a)
        db.flush()
        now = datetime.now(UTC)
        metrics.write_sample(db, a.id, "ipmi", [Point("cpu.temp", 55.0, "°C"), Point("fan.rpm:FAN1", None, "RPM", quality="unconfigured")], observed_at=now)
        db.commit()
        aid = str(a.id)
    items = {m["key"]: m for m in c.get(f"/api/assets/{aid}/metrics").json()["items"]}
    assert items["cpu.temp"]["value"] == 55.0 and items["cpu.temp"]["quality"] == "valid"
    assert items["fan.rpm:FAN1"]["value"] is None and items["fan.rpm:FAN1"]["quality"] == "unconfigured"
    s = c.get(f"/api/assets/{aid}/metrics/series", params={"key": "cpu.temp"}).json()
    assert s["resolution"] == "raw" and len(s["points"]) == 1
    assert c.get("/api/assets/00000000-0000-0000-0000-000000000000/metrics").status_code == 404
