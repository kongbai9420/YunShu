"""P5 温控策略：旧版迁入、曲线、82℃ 熔断、陈旧遥测不降速、脱管告警。只用伪造数据，不连接设备。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import metrics
from app.alert_defaults import ensure_rules
from app.alerting import evaluate
from app.control.policy import (
    FUSE_METRIC, UNGOVERNED_METRIC, CurveError, decide, governance_metrics, interpolate, migrate_legacy, run_policies,
    validate_nodes,
)
from app.metrics import Point
from app.models import Alert, Asset, ControlOperation, ControlTarget, LegacyMapping, ThermalCurve, ThermalPolicy
from tests.api_helpers import add_user, login
from tests.test_control_power import make_asset, set_owner
from tests.test_control_thermal import FIXTURE

NODES = [{"temp": 40, "speed": 30}, {"temp": 60, "speed": 50}, {"temp": 82, "speed": 100}]
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def test_validate_and_interpolate() -> None:
    assert validate_nodes([{"temp": 82, "speed": 100}, {"temp": 40, "speed": 30}])[0]["temp"] == 40
    for bad in ([], [{"temp": 40, "speed": 30}], [{"temp": 40, "speed": 30}, {"temp": 40, "speed": 50}],
                [{"temp": 40, "speed": 30.5}, {"temp": 60, "speed": 50}], [{"temp": float("nan"), "speed": 30}, {"temp": 60, "speed": 50}],
                [{"temp": 40, "speed": 130}, {"temp": 60, "speed": 50}]):
        with pytest.raises(CurveError):
            validate_nodes(bad)
    assert [interpolate(NODES, t) for t in (20, 40, 50, 60, 71, 90)] == [30, 30, 40, 50, 75, 100]


def policy(mode="curve", fixed=None, fuse="normal"):  # type: ignore[no-untyped-def]
    return ThermalPolicy(asset_id=uuid.uuid4(), mode=mode, fixed_pwm=fixed, fuse_state=fuse, enabled=True)


def target(mode="manual", pwm=50):  # type: ignore[no-untyped-def]
    return ControlTarget(key="bmc:192.0.2.30", state="ok", thermal_mode=mode, applied_pwm=pwm)


def test_decide_rules() -> None:
    # 82℃ 熔断：恢复原厂自动；已熔断且已是自动时不重复
    d = decide(policy(), NODES, 82.0, True, target())
    assert d.action == "restore_auto" and d.fuse == "tripped"
    assert decide(policy(fuse="tripped"), NODES, 85.0, True, target("auto", None)).action is None
    # 回差：熔断后降到 77℃ 以下才恢复
    assert decide(policy(fuse="tripped"), NODES, 79.0, True, target("auto", None)).fuse is None
    assert decide(policy(fuse="tripped"), NODES, 76.0, True, target("auto", None)).fuse == "normal"
    assert decide(policy(fuse="tripped"), NODES, None, False, target("auto", None)).action is None
    # 正常曲线与最小步长
    assert (decide(policy(), NODES, 50.0, True, target(pwm=30)).action, decide(policy(), NODES, 50.0, True, target(pwm=30)).pwm) == ("set_speed_all", 40)
    assert decide(policy(), NODES, 50.0, True, target(pwm=41)).action is None
    # 陈旧遥测：不降速；不知道当前转速时不下发
    assert decide(policy(), NODES, 45.0, False, target(pwm=60)).action is None
    assert decide(policy(), NODES, 70.0, False, target(pwm=60)).pwm == 73  # 过期时只允许升速
    assert decide(policy(), NODES, 70.0, False, target("unknown", None)).action is None
    assert decide(policy(), NODES, None, False, None).action is None
    # 固定转速：温度只用于熔断
    assert decide(policy("fixed", 35), NODES, 50.0, True, target(pwm=60)).pwm == 35
    assert decide(policy("fixed", 35), NODES, 83.0, True, target(pwm=35)).action == "restore_auto"


def legacy(db, asset_id, mode, **extra):  # type: ignore[no-untyped-def]
    db.add(LegacyMapping(source="yunshu-3", legacy_kind="hardware_node", legacy_id=str(uuid.uuid4()), target_type="asset",
                         target_id=asset_id, payload={"mode": mode, **extra}))


def test_migrate_legacy(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        ids = [make_asset(db, app_factory.cipher, n, f"192.0.2.{50 + i}").id for i, n in enumerate("甲乙丙丁戊")]
        db.add(LegacyMapping(source="yunshu-3", legacy_kind="curve_nodes", legacy_id="global", target_type="pending",
                             payload={"items": NODES}))
        legacy(db, ids[0], "auto")
        legacy(db, ids[1], "manual", manual_speed="35")
        legacy(db, ids[2], "dynamic")
        legacy(db, ids[3], "preset", preset_key="silent")
        legacy(db, ids[4], "manual", manual_speed="abc")
        db.commit()
        assert migrate_legacy(db) == {"curve": 1, "policies": 5, "pending": 2}
        db.commit()
        assert migrate_legacy(db) == {"curve": 0, "policies": 0, "pending": 0}  # 只迁一次
        got = {p.asset_id: p for p in db.scalars(select(ThermalPolicy))}
        assert (got[ids[0]].mode, got[ids[0]].enabled) == ("auto", True)
        assert (got[ids[1]].mode, got[ids[1]].fixed_pwm, got[ids[1]].enabled) == ("fixed", 35, True)
        assert got[ids[2]].mode == "curve"
        assert got[ids[3]].mode == "auto" and not got[ids[3]].enabled and "情景方案" in got[ids[3]].pending_note
        assert not got[ids[4]].enabled and "无效" in got[ids[4]].pending_note
        assert db.scalar(select(ThermalCurve)).nodes == NODES


def test_bad_legacy_curve_is_pending(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        aid = make_asset(db, app_factory.cipher).id
        db.add(LegacyMapping(source="yunshu-3", legacy_kind="curve_nodes", legacy_id="global", target_type="pending",
                             payload={"items": [{"temp": 40, "speed": 30}]}))
        legacy(db, aid, "dynamic")
        db.commit()
        assert migrate_legacy(db)["pending"] == 2
        p = db.scalar(select(ThermalPolicy))
        assert p.mode == "auto" and not p.enabled and db.scalar(select(ThermalCurve)) is None


def _setup(db, cipher, mode="curve", fixed=None):  # type: ignore[no-untyped-def]
    a = make_asset(db, cipher)
    db.add(ThermalCurve(key="default", nodes=NODES))
    db.add(ThermalPolicy(asset_id=a.id, mode=mode, fixed_pwm=fixed, enabled=True, legacy_mode="dynamic"))
    db.commit()
    return a.id


def _temp(db, aid, value, at=NOW):  # type: ignore[no-untyped-def]
    metrics.write_sample(db, aid, "ipmi", [Point("cpu.temp", value, "°C")], observed_at=at)
    db.commit()


def test_legacy_owner_does_nothing(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        aid = _setup(db, app_factory.cipher)
        _temp(db, aid, 90.0)
        assert run_policies(db, now=NOW, catalog=(FIXTURE,)) == {"requested": 0, "skipped": 0, "ungoverned": 0}
        assert governance_metrics(db, now=NOW) == 0
        assert db.scalar(select(ControlOperation)) is None


def test_empty_catalog_is_ungoverned_and_alerts(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = _setup(db, app_factory.cipher)
        _temp(db, aid, 50.0)
        # 命令库为空（没有内置命令、也没有自定义命令）时，策略无法执行
        assert run_policies(db, now=NOW, catalog=())["ungoverned"] == 1
        assert db.scalar(select(ControlOperation)) is None  # 不刷出被拒绝的操作
        p = db.scalar(select(ThermalPolicy))
        assert "无法执行" in p.last_decision and "命令库是空的" in p.last_decision
        governance_metrics(db, now=NOW, catalog=())
        db.commit()
        v = {x.key: x for x in metrics.latest(db, aid, now=NOW)}
        assert v[UNGOVERNED_METRIC].value == 1.0 and "无法接管" in v[UNGOVERNED_METRIC].message
        assert v[FUSE_METRIC].value == 0.0
        ensure_rules(db)
        evaluate(db, now=NOW)
        db.commit()
        alert = db.scalar(select(Alert).where(Alert.metric_key == UNGOVERNED_METRIC))
        assert alert is not None and alert.state == "active"


def test_fixture_catalog_requests_and_fuse(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = _setup(db, app_factory.cipher)
        _temp(db, aid, 50.0)
        assert run_policies(db, now=NOW, catalog=(FIXTURE,))["requested"] == 1
        db.commit()
        op = db.scalar(select(ControlOperation))
        assert (op.action, op.params, op.requested_by, op.status) == ("set_speed_all", {"pwm": 40}, "温控策略（自动）", "queued")
        # 还在排队：同一地址不重复下发
        assert run_policies(db, now=NOW + timedelta(seconds=15), catalog=(FIXTURE,))["ungoverned"] == 1
        op.status = "unconfirmed"
        db.add(ControlTarget(key="bmc:192.0.2.30", state="ok", thermal_mode="manual", applied_pwm=40))
        db.commit()
        _temp(db, aid, 84.0, NOW + timedelta(seconds=30))
        assert run_policies(db, now=NOW + timedelta(seconds=30), catalog=(FIXTURE,))["requested"] == 1
        db.commit()
        last = db.scalars(select(ControlOperation).order_by(ControlOperation.created_at.desc())).first()
        assert last.action == "restore_auto" and "熔断" in last.reason
        assert db.scalar(select(ThermalPolicy)).fuse_state == "tripped"
        governance_metrics(db, now=NOW + timedelta(seconds=30), catalog=(FIXTURE,))
        db.commit()
        v = {x.key: x for x in metrics.latest(db, aid, now=NOW + timedelta(seconds=30))}
        assert v[FUSE_METRIC].value == 1.0


def test_rejected_request_not_repeated_within_ten_minutes(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = _setup(db, app_factory.cipher, "fixed", 20)
        _temp(db, aid, 50.0)
        run_policies(db, now=NOW, catalog=(FIXTURE,))
        op = db.scalar(select(ControlOperation))
        op.status = "rejected"  # 例如执行时发现低于画像安全下限
        db.commit()
        _temp(db, aid, 50.0, NOW + timedelta(minutes=1))
        run_policies(db, now=NOW + timedelta(minutes=1), catalog=(FIXTURE,))
        db.commit()
        assert len(db.scalars(select(ControlOperation)).all()) == 1
        assert "10 分钟内不重复" in db.scalar(select(ThermalPolicy)).last_decision
        _temp(db, aid, 50.0, NOW + timedelta(minutes=11))
        run_policies(db, now=NOW + timedelta(minutes=11), catalog=(FIXTURE,))
        db.commit()
        assert len(db.scalars(select(ControlOperation)).all()) == 2


def test_policy_and_curve_api(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    add_user(factory, "victor", "viewer")
    op, v = login(app_factory(), "olivia"), login(app_factory(), "victor")
    with factory() as db:
        aid = make_asset(db, app_factory.cipher).id
    body = {"revision": 0, "mode": "fixed", "fixed_pwm": 40, "enabled": True}
    assert v.put(f"/api/control/thermal/policies/{aid}", json=body).status_code == 403
    r = op.put(f"/api/control/thermal/policies/{aid}", json=body)
    assert r.status_code == 200 and r.json()["revision"] == 1
    stale = op.put(f"/api/control/thermal/policies/{aid}", json={**body, "fixed_pwm": 50})
    assert stale.status_code == 409 and stale.json()["detail"]["current"]["fixed_pwm"] == 40
    assert op.put(f"/api/control/thermal/policies/{aid}", json={**body, "revision": 1, "fixed_pwm": 101}).status_code == 422
    assert op.put(f"/api/control/thermal/policies/{aid}", json={**body, "revision": 1, "mode": "fixed", "fixed_pwm": None}).status_code == 422
    assert op.put("/api/control/thermal/curves/default", json={"revision": 0, "nodes": NODES[:1]}).status_code == 422
    c = op.put("/api/control/thermal/curves/default", json={"revision": 0, "nodes": NODES}).json()
    assert c["revision"] == 1 and c["safety_temp"] == 82
    assert op.put("/api/control/thermal/curves/default", json={"revision": 0, "nodes": NODES}).status_code == 409
    listing = v.get("/api/control/thermal/policies").json()
    assert listing["policies"][0]["mode"] == "fixed" and listing["curve"]["revision"] == 1


@pytest.mark.parametrize(("vendor", "model", "thermal", "pwm"), [
    ("Huawei", "RH2288 V3", {"Oem": {"Huawei": {"FanSpeedAdjustmentMode": "Automatic", "FanSpeedLevelPercents": 30}}}, 40),
    # HPE 低于 50% 落在“最佳散热”档：仍记为平台已下发，不能每轮重复提交
    ("HPE", "ProLiant DL380 Gen10", {"Oem": {"Hpe": {"ThermalConfiguration": "OptimalCooling"}}}, 30),
])
def test_redfish_only_policy_is_not_resent_every_cycle(app_factory, factory, vendor, model, thermal, pwm) -> None:  # type: ignore[no-untyped-def]
    from app.control.thermal import PRODUCTION_CATALOG
    from tests.test_control_thermal import FakeRedfishBmc, worker

    with factory() as db:
        set_owner(db, "new")
        aid = _setup(db, app_factory.cipher, "fixed", pwm)
        asset = db.get(Asset, aid)
        asset.endpoints[0].protocol, asset.endpoints[0].port = "redfish", 443  # 只有 Redfish 端点
        db.commit()
        _temp(db, aid, 50.0)
        assert run_policies(db, now=NOW, catalog=PRODUCTION_CATALOG)["requested"] == 1
        db.commit()
    rf = FakeRedfishBmc(vendor, model, "3.10", {"/redfish/v1/Chassis/1/Thermal": thermal})
    worker(app_factory, factory, None, PRODUCTION_CATALOG, rf).run_once()
    with factory() as db:
        first = db.scalar(select(ControlOperation))
        assert first.status == "verified", first.message
        for i in range(1, 4):
            at = NOW + timedelta(seconds=15 * i)
            _temp(db, aid, 50.0, at)
            assert run_policies(db, now=at, catalog=PRODUCTION_CATALOG)["requested"] == 0
            db.commit()
        assert len(db.scalars(select(ControlOperation)).all()) == 1 and len(rf.patches) == 1
        assert "不调整" in db.scalar(select(ThermalPolicy)).last_decision
