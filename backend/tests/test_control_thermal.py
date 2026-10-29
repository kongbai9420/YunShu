"""P5 风扇温控门禁：只用伪造的 BMC 和离线测试画像，不连接任何真实设备。"""

from __future__ import annotations

import uuid

import pytest

from app.control.base import ControlError
from app.control.service import THERMAL_TASK, make_thermal_handler
from app.control.thermal import (
    PRODUCTION_CATALOG, Evidence, EvidenceSource, GateDenied, Identity, ThermalOperation, ThermalProfile, check_operation,
    match_profile, validate_pwm,
)
from app.models import ControlTarget
from tests.api_helpers import add_user, login
from tests.test_control_power import FAKE_SECRET, make_asset, set_owner

FIXTURE = ThermalProfile(
    profile_id="fixture-dell-r7x0", vendor="dell inc.", model_patterns=(r"PowerEdge R7[0-9]0(xd)?",),
    firmware_min="2.60", firmware_max="2.90", driver_id="dell_idrac_raw_v1",
    operations=frozenset({ThermalOperation.RESTORE_AUTO, ThermalOperation.SET_SPEED_ALL}),
    safe_min_pwm=30, safe_max_pwm=100, evidence=Evidence(EvidenceSource.STANDALONE_FIXTURE, note="离线测试专用"),
)
GOOD = Identity("Dell Inc.", "PowerEdge R730xd", "2.83")


class FakeThermalBmc:
    def __init__(self, identity: Identity = GOOD, *, fail_at: int | None = None, fail_kind: str = "protocol") -> None:
        self.ident = identity
        self.fail_at = fail_at
        self.fail_kind = fail_kind
        self.writes: list[tuple[int, int, list[int]]] = []
        self.reads = 0

    def factory(self, ctx):  # type: ignore[no-untyped-def]
        assert ctx.secret == FAKE_SECRET
        return self

    def identity(self) -> Identity:
        self.reads += 1
        return self.ident

    def raw(self, netfn: int, command: int, data: list[int]) -> None:
        self.writes.append((netfn, command, data))
        if self.fail_at is not None and len(self.writes) == self.fail_at:
            raise ControlError(self.fail_kind, "模拟失败")

    def close(self) -> None:
        pass


def test_production_catalog_is_empty_and_gate_rules() -> None:
    assert PRODUCTION_CATALOG == ()
    with pytest.raises(GateDenied) as e:
        match_profile((), GOOD)
    assert e.value.code == "catalog_empty"
    cases = {
        "vendor_missing": Identity("", "PowerEdge R730", "2.83"),
        "vendor_unknown": Identity("Supermicro", "X11", "1.0"),
        "model_unknown": Identity("Dell Inc.", "PowerEdge R640", "2.83"),
        "firmware_unsupported": Identity("Dell Inc.", "PowerEdge R730", "3.10"),
    }
    for code, ident in cases.items():
        with pytest.raises(GateDenied) as e:
            match_profile((FIXTURE,), ident)
        assert e.value.code == code
    unverified = ThermalProfile(**{**FIXTURE.__dict__, "evidence": Evidence(EvidenceSource.UNVERIFIED)})
    with pytest.raises(GateDenied) as e:
        match_profile((unverified,), GOOD)
    assert e.value.code == "not_verified"
    p = match_profile((FIXTURE,), GOOD)
    for pwm, code in ((29, "pwm_below_safe"), (None, "pwm_missing")):
        with pytest.raises(GateDenied) as e:
            check_operation(p, ThermalOperation.SET_SPEED_ALL, pwm)
        assert e.value.code == code
    for bad in (50.5, "60", True, -1, 101):
        with pytest.raises(GateDenied):
            validate_pwm(bad)
    restricted = ThermalProfile(**{**FIXTURE.__dict__, "operations": frozenset({ThermalOperation.RESTORE_AUTO})})
    with pytest.raises(GateDenied) as e:
        check_operation(restricted, ThermalOperation.SET_SPEED_ALL, 50)
    assert e.value.code == "operation_unauthorized"


@pytest.fixture
def op(app_factory, factory):  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    return login(app_factory(), "olivia")


def thermal(client, ids, action, pwm=None, confirm=""):  # type: ignore[no-untyped-def]
    body = {"asset_ids": [str(i) for i in ids], "action": action, "confirm": confirm, "reason": "测试", "request_key": uuid.uuid4().hex}
    if pwm is not None:
        body["pwm"] = pwm
    return client.post("/api/control/thermal", json=body)


def worker(app_factory, factory, bmc, catalog=(FIXTURE,)):  # type: ignore[no-untyped-def]
    from app.worker import Worker

    return Worker(factory, {THERMAL_TASK: make_thermal_handler(lambda: app_factory.cipher, bmc.factory, catalog)})


def test_production_rejects_all_fan_control(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    info = op.get(f"/api/control/assets/{aid}/thermal").json()
    assert info["catalog_size"] == 0 and {a["code"] for a in info["actions"]} == {"catalog_empty"}
    r = thermal(op, [aid], "restore_auto").json()["operations"][0]
    assert r["status"] == "rejected" and "经过实机验证" in r["message"]
    # 即使队列里出现了任务（例如测试注入），生产空目录的 Worker 也零写
    bmc = FakeThermalBmc()
    assert worker(app_factory, factory, bmc, catalog=()).run_once() is None
    assert bmc.writes == [] and bmc.reads == 0


def test_fixture_profile_flow_no_silent_clamp(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    op.app.state.thermal_catalog = (FIXTURE,)
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    assert thermal(op, [aid], "set_speed_all", 50).json()["detail"]["code"] == "confirm"
    assert thermal(op, [aid], "set_speed_all", 50.5, confirm="甲").status_code == 422
    r = thermal(op, [aid], "set_speed_all", 20, confirm="甲").json()["operations"][0]
    assert r["status"] == "queued"  # 安全下限取决于设备画像：执行时识别后才知道
    bmc = FakeThermalBmc()
    worker(app_factory, factory, bmc).run_once()
    o = op.get(f"/api/control/operations?asset_id={aid}").json()[0]
    assert o["status"] == "rejected" and "安全下限 30%" in o["message"] and bmc.writes == []  # 没有改成 30% 下发

    thermal(op, [aid], "set_speed_all", 45, confirm="甲")
    worker(app_factory, factory, bmc).run_once()
    o = op.get(f"/api/control/operations?asset_id={aid}").json()[0]
    assert o["status"] == "unconfirmed" and "无法确认已生效" in o["message"]
    assert bmc.writes == [(0x30, 0x30, [0x01, 0x00]), (0x30, 0x30, [0x02, 0xFF, 45])]
    with factory() as db:
        t = db.get(ControlTarget, "bmc:192.0.2.30")
        assert (t.thermal_mode, t.applied_pwm) == ("manual", 45)

    thermal(op, [aid], "restore_auto")
    worker(app_factory, factory, bmc).run_once()
    assert bmc.writes[-1] == (0x30, 0x30, [0x01, 0x01])
    with factory() as db:
        t = db.get(ControlTarget, "bmc:192.0.2.30")
        assert (t.thermal_mode, t.applied_pwm) == ("auto", None)


def test_user_fields_cannot_unlock_and_unknown_device_zero_writes(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    op.app.state.thermal_catalog = (FIXTURE,)
    with factory() as db:
        set_owner(db, "new")
        a = make_asset(db, app_factory.cipher)
        a.vendor, a.model, a.notes = "Dell Inc.", "PowerEdge R730xd", "verified: true"  # 用户填写的字段不作数
        db.commit()
        aid = a.id
    thermal(op, [aid], "restore_auto")
    bmc = FakeThermalBmc(Identity("Supermicro", "X11DPi", "1.73"))
    worker(app_factory, factory, bmc).run_once()
    o = op.get(f"/api/control/operations?asset_id={aid}").json()[0]
    assert o["status"] == "rejected" and "不会尝试其他厂商" in o["message"] and bmc.writes == []


def test_staged_failure_is_failed_and_mode_unknown(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    op.app.state.thermal_catalog = (FIXTURE,)
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    thermal(op, [aid], "set_speed_all", 60, confirm="甲")
    bmc = FakeThermalBmc(fail_at=2)
    worker(app_factory, factory, bmc).run_once()
    o = op.get(f"/api/control/operations?asset_id={aid}").json()[0]
    assert o["status"] == "failed" and "停在手动模式" in o["message"]
    with factory() as db:
        t = db.get(ControlTarget, "bmc:192.0.2.30")
        assert t.thermal_mode == "unknown" and t.applied_pwm is None and t.state == "ok"


def test_timeout_is_indeterminate_no_protocol_switch(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    op.app.state.thermal_catalog = (FIXTURE,)
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    thermal(op, [aid], "restore_auto")
    bmc = FakeThermalBmc(fail_at=1, fail_kind="timeout")
    w = worker(app_factory, factory, bmc)
    w.run_once()
    assert w.run_once() is None and len(bmc.writes) == 1
    o = op.get(f"/api/control/operations?asset_id={aid}").json()[0]
    assert o["status"] == "indeterminate"
    with factory() as db:
        t = db.get(ControlTarget, "bmc:192.0.2.30")
        assert t.state == "indeterminate" and t.thermal_mode == "unknown"
    assert thermal(op, [aid], "restore_auto").json()["operations"][0]["status"] == "rejected"


def test_power_and_thermal_share_target_mutex(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    op.app.state.thermal_catalog = (FIXTURE,)
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    assert thermal(op, [aid], "restore_auto").json()["operations"][0]["status"] == "queued"
    p = op.post("/api/control/power", json={"asset_ids": [str(aid)], "action": "on", "request_key": uuid.uuid4().hex}).json()
    assert p["operations"][0]["status"] == "rejected" and "未完成的控制操作" in p["operations"][0]["message"]
