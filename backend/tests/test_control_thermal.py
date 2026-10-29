"""风扇温控：命令库匹配、IPMI / Redfish 执行与自定义命令。只用伪造的 BMC，不连接任何真实设备。"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest

from app.control.base import ControlError
from app.control.service import THERMAL_TASK, make_thermal_handler
from app.control.thermal import (
    PRODUCTION_CATALOG, Evidence, EvidenceSource, GateDenied, Identity, ThermalOperation, ThermalProfile, check_operation,
    match_profile, parse_commands, validate_pwm,
)
from app.services.credentials import create_credential
from app.models import ControlTarget, ManagementEndpoint
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


def _merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


class FakeRedfishBmc:
    """按路径返回资源；PATCH 合并进资源并记录。apply=False 时 BMC 接受但不生效（用来测回读不一致）。"""

    def __init__(self, vendor: str, model: str, firmware: str, extra: dict[str, dict[str, Any]], *, apply: bool = True) -> None:
        self.res: dict[str, dict[str, Any]] = {
            "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]},
            "/redfish/v1/Systems/1": {"Manufacturer": vendor, "Model": model},
            "/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
            "/redfish/v1/Managers/1": {"FirmwareVersion": firmware},
            "/redfish/v1/Chassis": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
            "/redfish/v1/Chassis/1": {"Thermal": {"@odata.id": "/redfish/v1/Chassis/1/Thermal"}},
            **copy.deepcopy(extra),
        }
        self.apply = apply
        self.patches: list[tuple[str, dict[str, Any], str]] = []

    def factory(self, ctx):  # type: ignore[no-untyped-def]
        assert ctx.secret == FAKE_SECRET
        return self

    def get(self, path: str) -> tuple[dict[str, Any], str]:
        return copy.deepcopy(self.res.get(path, {})), f'W/"{len(self.patches)}"'

    def patch(self, path: str, body: dict[str, Any], etag: str) -> None:
        self.patches.append((path, body, etag))
        if self.apply:
            _merge(self.res[path], copy.deepcopy(body))

    def close(self) -> None:
        pass


def test_builtin_catalog_matching() -> None:
    ids = {
        Identity("DELL", "PowerEdge R730", "2.83"): "dell-idrac-ipmi",
        Identity("Dell Inc.", "PowerEdge R740", "3.30.30.30"): "dell-idrac-ipmi",
        Identity("Dell Inc.", "PowerEdge R740", "3.34.34.34"): "dell-idrac9-redfish",
        Identity("DELL", "PowerEdge R650", "7.00"): "dell-idrac9-redfish",
        Identity("Supermicro", "X11DPi-N", "1.73"): "supermicro-ipmi",
        Identity("HUAWEI", "RH2288 V3", "3.23"): "huawei-ibmc-redfish",
        Identity("XFUSION", "2288H V6", "3.10"): "huawei-ibmc-redfish",
        Identity("HPE", "ProLiant DL380 Gen10", "2.72"): "hpe-ilo-redfish",
    }
    for ident, pid in ids.items():
        p = match_profile(PRODUCTION_CATALOG, ident)
        assert p.profile_id == pid, ident
        assert p.evidence.source == EvidenceSource.COMMUNITY
    for ident, code in ((Identity("Supermicro", "X9DRi-LN4F+", "3.40"), "model_unknown"),
                        (Identity("Lenovo", "ThinkSystem SR650", "5.10"), "vendor_unknown"),
                        (Identity("", "PowerEdge R730", "2.83"), "vendor_missing")):
        with pytest.raises(GateDenied) as e:
            match_profile(PRODUCTION_CATALOG, ident)
        assert e.value.code == code


def test_gate_rules() -> None:
    with pytest.raises(GateDenied) as e:
        match_profile((), GOOD)
    assert e.value.code == "catalog_empty"
    with pytest.raises(GateDenied) as e:
        match_profile((FIXTURE,), Identity("Dell Inc.", "PowerEdge R730", "3.10"))
    assert e.value.code == "firmware_unsupported"
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


def test_parse_custom_commands() -> None:
    steps = parse_commands("raw 0x3a 0x07 0xff {pwm} 0x01\n# 注释\n58 7 1", needs_pwm=True)
    assert [s.text() for s in steps] == ["0x3a 0x07 0xff {pwm} 0x01", "0x3a 0x07 0x01"]
    bad = {
        "": "至少要有一条命令",
        "0x00 0x02 0x00": "OEM 段",  # 机箱电源命令
        "0x06 0x45 0x01": "OEM 段",  # 用户管理
        "0x30 0x30 0x100": "超出一个字节",
        "0x30 {pwm} 0x01": "数据部分",
        "0x30 0x30 abc": "不是数字",
        "0x30": "至少要有",
        "\n".join(["0x30 0x30 0x01"] * 7): "最多",
    }
    for text, msg in bad.items():
        with pytest.raises(ValueError) as e:
            parse_commands(text, needs_pwm=False)
        assert msg in str(e.value), text
    with pytest.raises(ValueError, match="要有 \\{pwm\\}"):
        parse_commands("0x30 0x30 0x01 0x00", needs_pwm=True)
    with pytest.raises(ValueError, match="不能有"):
        parse_commands("0x30 0x30 0x01 {pwm}", needs_pwm=False)


@pytest.fixture
def op(app_factory, factory):  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    return login(app_factory(), "olivia")


@pytest.fixture
def admin(app_factory, factory):  # type: ignore[no-untyped-def]
    add_user(factory, "ada", "admin")
    return login(app_factory(), "ada")


def thermal(client, ids, action, pwm=None, confirm=""):  # type: ignore[no-untyped-def]
    body = {"asset_ids": [str(i) for i in ids], "action": action, "confirm": confirm, "reason": "测试", "request_key": uuid.uuid4().hex}
    if pwm is not None:
        body["pwm"] = pwm
    return client.post("/api/control/thermal", json=body)


def worker(app_factory, factory, bmc, catalog=(FIXTURE,), redfish=None):  # type: ignore[no-untyped-def]
    from app.worker import Worker

    handler = make_thermal_handler(lambda: app_factory.cipher, bmc.factory if bmc else None, catalog,
                                   redfish_factory=redfish.factory if redfish else None)
    return Worker(factory, {THERMAL_TASK: handler})


def last_op(client, aid):  # type: ignore[no-untyped-def]
    return client.get(f"/api/control/operations?asset_id={aid}").json()[0]


def add_redfish(db, cipher, asset, address="192.0.2.30"):  # type: ignore[no-untyped-def]
    ep = ManagementEndpoint(address=address, port=443, protocol="redfish", purpose="management")
    ep.credential_id = create_credential(db, cipher, alias=f"rf-{uuid.uuid4()}", kind="password", username="root", secret=FAKE_SECRET).id
    asset.endpoints.append(ep)
    db.commit()


def redfish_only_asset(db, cipher):  # type: ignore[no-untyped-def]
    a = make_asset(db, cipher)
    a.endpoints[0].protocol, a.endpoints[0].port = "redfish", 443
    db.commit()
    return a.id


def test_builtin_dell_ipmi_flow(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    info = op.get(f"/api/control/assets/{aid}/thermal").json()
    assert info["catalog_size"] == len(PRODUCTION_CATALOG) and all(a["allowed"] for a in info["actions"])
    assert thermal(op, [aid], "set_speed_all", 50).json()["detail"]["code"] == "confirm"
    assert thermal(op, [aid], "set_speed_all", 50.5, confirm="甲").status_code == 422
    thermal(op, [aid], "set_speed_all", 45, confirm="甲")
    bmc = FakeThermalBmc()
    worker(app_factory, factory, bmc, catalog=PRODUCTION_CATALOG).run_once()
    o = last_op(op, aid)
    assert o["status"] == "unconfirmed" and "无法确认已生效" in o["message"] and "未经本项目实机验证" in o["message"]
    assert bmc.writes == [(0x30, 0x30, [0x01, 0x00]), (0x30, 0x30, [0x02, 0xFF, 45])]
    with factory() as db:
        t = db.get(ControlTarget, "bmc:192.0.2.30")
        assert (t.thermal_mode, t.applied_pwm) == ("manual", 45)
    thermal(op, [aid], "restore_auto")
    worker(app_factory, factory, bmc, catalog=PRODUCTION_CATALOG).run_once()
    assert bmc.writes[-1] == (0x30, 0x30, [0x01, 0x01])
    with factory() as db:
        t = db.get(ControlTarget, "bmc:192.0.2.30")
        assert (t.thermal_mode, t.applied_pwm) == ("auto", None)


def test_below_safe_min_rejected_without_clamp(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    op.app.state.thermal_catalog = (FIXTURE,)
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    r = thermal(op, [aid], "set_speed_all", 20, confirm="甲").json()["operations"][0]
    assert r["status"] == "queued"  # 安全下限取决于设备：执行时识别后才知道
    bmc = FakeThermalBmc()
    worker(app_factory, factory, bmc).run_once()
    o = last_op(op, aid)
    assert o["status"] == "rejected" and "安全下限 30%" in o["message"] and bmc.writes == []  # 没有改成 30% 下发


def test_user_fields_cannot_unlock_and_unknown_device_zero_writes(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        a = make_asset(db, app_factory.cipher)
        a.vendor, a.model, a.notes = "Dell Inc.", "PowerEdge R730xd", "verified: true"  # 用户填写的字段不作数
        db.commit()
        aid = a.id
    thermal(op, [aid], "restore_auto")
    bmc = FakeThermalBmc(Identity("Lenovo", "ThinkSystem SR650", "5.10"))
    worker(app_factory, factory, bmc, catalog=PRODUCTION_CATALOG).run_once()
    o = last_op(op, aid)
    assert o["status"] == "rejected" and "不会尝试其他厂商" in o["message"] and bmc.writes == []


def test_new_idrac_needs_redfish_endpoint(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    thermal(op, [aid], "restore_auto")
    bmc = FakeThermalBmc(Identity("DELL", "PowerEdge R740", "5.10"))
    worker(app_factory, factory, bmc, catalog=PRODUCTION_CATALOG).run_once()
    o = last_op(op, aid)
    assert o["status"] == "rejected" and "Redfish 端点" in o["message"] and bmc.writes == []


DELL_ATTRS = "/redfish/v1/Managers/iDRAC.Embedded.1/Oem/Dell/DellAttributes/System.Embedded.1"


def test_new_idrac_minimum_fan_speed_via_redfish(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        a = make_asset(db, app_factory.cipher)
        add_redfish(db, app_factory.cipher, a)
        aid = a.id
    bmc = FakeThermalBmc(Identity("DELL", "PowerEdge R740", "5.10"))  # IPMI 识别，Redfish 执行
    rf = FakeRedfishBmc("Dell Inc.", "PowerEdge R740", "5.10.50.00", {
        DELL_ATTRS: {"Attributes": {"ThermalSettings.1.MinimumFanSpeed": 255, "ThermalSettings.1.MFSMinimumLimit": 7}},
    })
    thermal(op, [aid], "set_speed_all", 6, confirm="甲")
    worker(app_factory, factory, bmc, PRODUCTION_CATALOG, rf).run_once()
    o = last_op(op, aid)
    assert o["status"] == "rejected" and "MFSMinimumLimit" in o["message"] and rf.patches == []
    thermal(op, [aid], "set_speed_all", 35, confirm="甲")
    worker(app_factory, factory, bmc, PRODUCTION_CATALOG, rf).run_once()
    o = last_op(op, aid)
    assert o["status"] == "verified" and "最低转速下限" in o["message"]
    assert rf.patches[-1] == (DELL_ATTRS, {"Attributes": {"ThermalSettings.1.MinimumFanSpeed": 35}}, 'W/"0"')
    assert bmc.writes == []
    thermal(op, [aid], "restore_auto")
    worker(app_factory, factory, bmc, PRODUCTION_CATALOG, rf).run_once()
    assert rf.patches[-1][1] == {"Attributes": {"ThermalSettings.1.MinimumFanSpeed": 7}}
    assert last_op(op, aid)["status"] == "verified"


HUAWEI_THERMAL = {"/redfish/v1/Chassis/1/Thermal": {"Oem": {"Huawei": {
    "FanSpeedAdjustmentMode": "Automatic", "FanSpeedLevelPercents": [30, 30, 30, 30], "FanManualModeTimeoutSeconds": 30}}}}


def test_huawei_redfish_manual_and_readback(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = redfish_only_asset(db, app_factory.cipher)
    rf = FakeRedfishBmc("Huawei", "RH2288 V3", "3.23", HUAWEI_THERMAL)
    thermal(op, [aid], "set_speed_all", 40, confirm="甲")
    worker(app_factory, factory, None, PRODUCTION_CATALOG, rf).run_once()
    o = last_op(op, aid)
    assert o["status"] == "verified"
    path, body, etag = rf.patches[-1]
    assert path == "/redfish/v1/Chassis/1/Thermal" and etag
    assert body["Oem"]["Huawei"]["FanSpeedAdjustmentMode"] == "Manual"
    assert body["Oem"]["Huawei"]["FanSpeedLevelPercents"] == [40, 40, 40, 40]
    assert body["Oem"]["Huawei"]["FanManualModeTimeoutSeconds"] > 86400
    thermal(op, [aid], "restore_auto")
    worker(app_factory, factory, None, PRODUCTION_CATALOG, rf).run_once()
    assert rf.patches[-1][1] == {"Oem": {"Huawei": {"FanSpeedAdjustmentMode": "Automatic"}}}
    # BMC 接受但读回来没变：不能算已确认
    lazy = FakeRedfishBmc("Huawei", "RH2288 V3", "3.23", HUAWEI_THERMAL, apply=False)
    thermal(op, [aid], "set_speed_all", 40, confirm="甲")
    worker(app_factory, factory, None, PRODUCTION_CATALOG, lazy).run_once()
    assert last_op(op, aid)["status"] == "unconfirmed"


def test_hpe_cooling_levels(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = redfish_only_asset(db, app_factory.cipher)
    rf = FakeRedfishBmc("HPE", "ProLiant DL380 Gen10", "2.72", {
        "/redfish/v1/Chassis/1/Thermal": {"Oem": {"Hpe": {"ThermalConfiguration": "OptimalCooling"}}}})
    for pwm, level in ((95, "MaximumCooling"), (60, "IncreasedCooling"), (30, "OptimalCooling")):
        thermal(op, [aid], "set_speed_all", pwm, confirm="甲")
        worker(app_factory, factory, None, PRODUCTION_CATALOG, rf).run_once()
        assert rf.patches[-1][1] == {"Oem": {"Hpe": {"ThermalConfiguration": level}}}
        assert last_op(op, aid)["status"] == "verified"
    with factory() as db:
        t = db.get(ControlTarget, "bmc:192.0.2.30")
        assert (t.thermal_mode, t.applied_pwm) == ("manual", 30)  # 平台下发的档位，策略据此不重复提交
    thermal(op, [aid], "restore_auto")
    worker(app_factory, factory, None, PRODUCTION_CATALOG, rf).run_once()
    assert rf.patches[-1][1] == {"Oem": {"Hpe": {"ThermalConfiguration": "OptimalCooling"}}}
    with factory() as db:
        t = db.get(ControlTarget, "bmc:192.0.2.30")
        assert (t.thermal_mode, t.applied_pwm) == ("auto", None)


def test_redfish_without_fan_settings_is_rejected(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = redfish_only_asset(db, app_factory.cipher)
    rf = FakeRedfishBmc("Huawei", "2288H V5", "3.10", {"/redfish/v1/Chassis/1/Thermal": {"Fans": []}})
    thermal(op, [aid], "restore_auto")
    worker(app_factory, factory, None, PRODUCTION_CATALOG, rf).run_once()
    o = last_op(op, aid)
    assert o["status"] == "rejected" and "FanSpeedAdjustmentMode" in o["message"] and rf.patches == []


CUSTOM = {
    "name": "联想 XCC（自测）", "vendor": "Lenovo", "model_pattern": "ThinkSystem SR6.*", "firmware_min": "", "firmware_max": "",
    "restore_commands": "0x3a 0x07 0xff 0x00 0x00", "set_commands": "0x3a 0x07 0xff {pwm} 0x01", "pwm_scale": "byte",
    "safe_min_pwm": 25, "safe_max_pwm": 100, "enabled": True, "note": "测试",
}


def test_custom_commands_admin_only_and_used_first(app_factory, factory, op, admin) -> None:  # type: ignore[no-untyped-def]
    assert op.post("/api/control/thermal/commands", json=CUSTOM).status_code == 403
    bad = admin.post("/api/control/thermal/commands", json={**CUSTOM, "set_commands": "0x00 0x02 {pwm}"})
    assert bad.status_code == 422 and "OEM 段" in bad.json()["detail"]
    assert admin.post("/api/control/thermal/commands", json={**CUSTOM, "model_pattern": "("}).status_code == 422
    created = admin.post("/api/control/thermal/commands", json=CUSTOM)
    assert created.status_code == 201
    listing = op.get("/api/control/thermal/commands").json()
    assert listing["can_edit"] is False and len(listing["builtin"]) == len(PRODUCTION_CATALOG)
    assert [c["name"] for c in listing["custom"]] == ["联想 XCC（自测）"] and listing["custom"][0]["vendor"] == "lenovo"

    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    thermal(op, [aid], "set_speed_all", 50, confirm="甲")
    bmc = FakeThermalBmc(Identity("Lenovo", "ThinkSystem SR650", "5.10"))
    worker(app_factory, factory, bmc, catalog=PRODUCTION_CATALOG).run_once()
    o = last_op(op, aid)
    assert o["status"] == "unconfirmed" and "管理员自定义" in o["message"]
    assert bmc.writes == [(0x3A, 0x07, [0xFF, 128, 0x01])]  # 50% 按 0–255 刻度换算

    pid = created.json()["id"]
    assert admin.put(f"/api/control/thermal/commands/{pid}", json={**CUSTOM, "enabled": False}).status_code == 200
    thermal(op, [aid], "restore_auto")
    worker(app_factory, factory, bmc, catalog=PRODUCTION_CATALOG).run_once()
    assert last_op(op, aid)["status"] == "rejected" and len(bmc.writes) == 1  # 停用后又匹配不到了
    assert admin.delete(f"/api/control/thermal/commands/{pid}").status_code == 204
    assert admin.get("/api/control/thermal/commands").json()["custom"] == []


def test_staged_failure_is_failed_and_mode_unknown(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    thermal(op, [aid], "set_speed_all", 60, confirm="甲")
    bmc = FakeThermalBmc(fail_at=2)
    worker(app_factory, factory, bmc, catalog=PRODUCTION_CATALOG).run_once()
    o = last_op(op, aid)
    assert o["status"] == "failed" and "停在手动模式" in o["message"]
    with factory() as db:
        t = db.get(ControlTarget, "bmc:192.0.2.30")
        assert t.thermal_mode == "unknown" and t.applied_pwm is None and t.state == "ok"


def test_timeout_is_indeterminate_no_protocol_switch(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    thermal(op, [aid], "restore_auto")
    bmc = FakeThermalBmc(fail_at=1, fail_kind="timeout")
    w = worker(app_factory, factory, bmc, catalog=PRODUCTION_CATALOG)
    w.run_once()
    assert w.run_once() is None and len(bmc.writes) == 1
    assert last_op(op, aid)["status"] == "indeterminate"
    with factory() as db:
        t = db.get(ControlTarget, "bmc:192.0.2.30")
        assert t.state == "indeterminate" and t.thermal_mode == "unknown"
    assert thermal(op, [aid], "restore_auto").json()["operations"][0]["status"] == "rejected"


def test_power_and_thermal_share_target_mutex(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    assert thermal(op, [aid], "restore_auto").json()["operations"][0]["status"] == "queued"
    p = op.post("/api/control/power", json={"asset_ids": [str(aid)], "action": "on", "request_key": uuid.uuid4().hex}).json()
    assert p["operations"][0]["status"] == "rejected" and "未完成的控制操作" in p["operations"][0]["message"]


def per_fan(client, ids, *, preset=None, speeds=None, confirm="甲"):  # type: ignore[no-untyped-def]
    body = {"asset_ids": [str(i) for i in ids], "action": "set_speed_per_fan", "confirm": confirm, "reason": "测试",
            "request_key": uuid.uuid4().hex}
    if preset:
        body["preset"] = preset
    if speeds is not None:
        body["speeds"] = speeds
    return client.post("/api/control/thermal", json=body)


def test_preset_per_fan_on_dell_ipmi(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    info = op.get(f"/api/control/assets/{aid}/thermal").json()
    assert {p["key"] for p in info["presets"]} >= {"silent", "turbo", "one_node_l"}
    assert per_fan(op, [aid], preset="silent", confirm="").json()["detail"]["code"] == "confirm"  # 高风险，要确认语
    assert per_fan(op, [aid], preset="nope").status_code == 422
    assert per_fan(op, [aid], speeds=[30, 30.5]).status_code == 422
    r = per_fan(op, [aid], preset="one_node_l").json()["operations"][0]
    assert r["status"] == "queued" and "单节点负载（左）" in r["action_label"]
    bmc = FakeThermalBmc()
    worker(app_factory, factory, bmc, catalog=PRODUCTION_CATALOG).run_once()
    o = last_op(op, aid)
    assert o["status"] == "unconfirmed"
    speeds = [25, 40, 30, 25, 20, 20]
    assert bmc.writes == [(0x30, 0x30, [0x01, 0x00])] + [(0x30, 0x30, [0x02, i, v]) for i, v in enumerate(speeds)]
    with factory() as db:
        t = db.get(ControlTarget, "bmc:192.0.2.30")
        assert (t.thermal_mode, t.applied_pwm, t.applied_speeds) == ("per_fan", 40, speeds)
    # 任一风扇低于安全下限：整体拒绝，不下发
    per_fan(op, [aid], speeds=[30, 15, 30])
    worker(app_factory, factory, bmc, catalog=PRODUCTION_CATALOG).run_once()
    o = last_op(op, aid)
    assert o["status"] == "rejected" and "风扇 2 的转速 15%" in o["message"] and len(bmc.writes) == 7
    thermal(op, [aid], "restore_auto")
    worker(app_factory, factory, bmc, catalog=PRODUCTION_CATALOG).run_once()
    with factory() as db:
        t = db.get(ControlTarget, "bmc:192.0.2.30")
        assert (t.thermal_mode, t.applied_speeds) == ("auto", None)


def test_per_fan_unsupported_models_rejected(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    per_fan(op, [aid], preset="turbo")
    bmc = FakeThermalBmc(Identity("Supermicro", "X11DPi-N", "1.73"))  # 超微只能按区
    worker(app_factory, factory, bmc, catalog=PRODUCTION_CATALOG).run_once()
    o = last_op(op, aid)
    assert o["status"] == "rejected" and "不支持“按风扇设定转速”" in o["message"] and bmc.writes == []


def test_huawei_per_fan_follows_device_fan_count(app_factory, factory, op) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        set_owner(db, "new")
        aid = redfish_only_asset(db, app_factory.cipher)
    rf = FakeRedfishBmc("Huawei", "RH2288 V3", "3.23", HUAWEI_THERMAL)  # 设备报告 4 个风扇
    per_fan(op, [aid], preset="one_node_r")
    worker(app_factory, factory, None, PRODUCTION_CATALOG, rf).run_once()
    assert last_op(op, aid)["status"] == "verified"
    assert rf.patches[-1][1]["Oem"]["Huawei"]["FanSpeedLevelPercents"] == [20, 20, 25, 30]  # 多出来的风扇不下发
    scalar = FakeRedfishBmc("Huawei", "RH2288 V3", "3.23", {"/redfish/v1/Chassis/1/Thermal": {"Oem": {"Huawei": {
        "FanSpeedAdjustmentMode": "Automatic", "FanSpeedLevelPercents": 30}}}})
    per_fan(op, [aid], preset="turbo")
    worker(app_factory, factory, None, PRODUCTION_CATALOG, scalar).run_once()
    o = last_op(op, aid)
    assert o["status"] == "rejected" and "不支持按风扇设定" in o["message"] and scalar.patches == []


def test_custom_per_fan_commands(app_factory, factory, op, admin) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match=r"只能用在"):
        parse_commands("0x3a 0x02 {fan} {pwm}", needs_pwm=True)
    with pytest.raises(ValueError, match=r"同时含"):
        parse_commands("0x3a 0x01 0x00\n0x3a 0x02 0x01 {pwm}", needs_pwm=True, per_fan=True)
    bad = admin.post("/api/control/thermal/commands", json={**CUSTOM, "fan_commands": "0x3a 0x02 0x00 {pwm}"})
    assert bad.status_code == 422 and "按风扇设定" in bad.json()["detail"]
    created = admin.post("/api/control/thermal/commands", json={**CUSTOM, "pwm_scale": "percent",
                                                                "fan_commands": "0x3a 0x01 0x00\n0x3a 0x02 {fan} {pwm}"})
    assert created.status_code == 201 and created.json()["fan_commands"].endswith("{pwm}")
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    per_fan(op, [aid], speeds=[30, 50])
    bmc = FakeThermalBmc(Identity("Lenovo", "ThinkSystem SR650", "5.10"))
    worker(app_factory, factory, bmc, catalog=PRODUCTION_CATALOG).run_once()
    assert last_op(op, aid)["status"] == "unconfirmed"
    assert bmc.writes == [(0x3A, 0x01, [0x00]), (0x3A, 0x02, [0, 30]), (0x3A, 0x02, [1, 50])]
