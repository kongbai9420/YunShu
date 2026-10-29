"""4.2 P13 带外采集减负：iDRAC 走 SNMP 为主、IPMI 回退、Redfish 降频补充并读取 BMC 日志。

服务器数据采集以 SNMP 为主，避免在 BMC 留下大量登录日志。全部为伪造响应，不连接设备。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.collect.base import PROFILES, AdapterError, Profile, Step, _load_profiles
from app.collect.redfish import RedfishAdapter
from app.collect.runner import collect_asset
from app.collect.snmp import IDRAC, SnmpAdapter
from app.crypto import CredentialCipher, KeyRing
from app.metrics import Point
from tests.test_collect import FakeAdapter, make_asset

NOW = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)


class FakeIdrac:
    """按 IDRAC-MIB-SMIv2 表结构返回的伪造 SNMP 数据。"""

    def __init__(self, *, idrac: bool = True, v1: bool = False) -> None:
        self.idrac, self.v1 = idrac, v1
        t, f, ps, amp, mem = (IDRAC + s for s in (".4.700.20.1", ".4.700.12.1", ".4.600.12.1", ".4.600.30.1", ".4.1100.50.1"))
        pd, vd = IDRAC + ".5.1.20.130.4.1", IDRAC + ".5.1.20.140.1.1"
        self.tables = {
            f"{t}.6": {f"{t}.6.1.1": 245, f"{t}.6.1.2": 612, f"{t}.6.1.3": None},
            f"{t}.8": {f"{t}.8.1.1": "System Board Inlet Temp", f"{t}.8.1.2": "CPU1 Temp", f"{t}.8.1.3": "CPU2 Temp"},
            f"{f}.6": {f"{f}.6.1.1": 5640, f"{f}.6.1.2": 5520},
            f"{f}.8": {f"{f}.8.1.1": "System Board Fan1", f"{f}.8.1.2": "System Board Fan2"},
            f"{ps}.5": {f"{ps}.5.1.1": 3, f"{ps}.5.1.2": 5},
            f"{ps}.8": {f"{ps}.8.1.1": "PS1 Status", f"{ps}.8.1.2": "PS2 Status"},
            f"{amp}.6": {f"{amp}.6.1.1": 4, f"{amp}.6.1.3": 238},
            f"{amp}.7": {f"{amp}.7.1.1": 23, f"{amp}.7.1.3": 26},
            f"{mem}.5": {f"{mem}.5.1.1": 3, f"{mem}.5.1.2": 4},
            f"{mem}.8": {f"{mem}.8.1.1": "DIMM.Socket.A1", f"{mem}.8.1.2": "DIMM.Socket.B1"},
            f"{pd}.2": {f"{pd}.2.1": "Physical Disk 0:1:0", f"{pd}.2.2": "Physical Disk 0:1:1"},
            f"{pd}.24": {f"{pd}.24.1": 3, f"{pd}.24.2": 2},
            f"{vd}.2": {f"{vd}.2.1": "Virtual Disk 0"},
            f"{vd}.20": {f"{vd}.20.1": 4},
        }
        self.walks: list[str] = []

    def get(self, oids: list[str]) -> dict[str, Any]:
        if any(o.startswith(IDRAC) for o in oids):
            if self.v1:
                raise AdapterError("protocol", "SNMP 错误：noSuchName")
            if not self.idrac:
                return {o: None for o in oids}
            return {IDRAC + ".2.1.0": 4, IDRAC + ".2.4.0": 4}
        return {"1.3.6.1.2.1.1.5.0": "fake-idrac", "1.3.6.1.2.1.1.1.0": "fake", "1.3.6.1.2.1.1.3.0": 360000,
                "1.3.6.1.4.1.2021.4.5.0": None, "1.3.6.1.4.1.2021.4.6.0": None, "1.3.6.1.4.1.2021.10.1.3.1": None}

    def walk(self, oid: str) -> dict[str, Any]:
        self.walks.append(oid)
        return dict(self.tables.get(oid, {}))


def ctx():  # type: ignore[no-untyped-def]
    from app.collect.base import EndpointContext

    return EndpointContext(address="192.0.2.30", port=161, protocol="snmp", secret="fake-community")


def test_idrac_mib_points_use_the_same_keys_as_ipmi_and_redfish() -> None:
    client = FakeIdrac()
    result = SnmpAdapter(client_factory=lambda _c: client).collect(ctx())
    got = {p.key: p for p in result.points}
    assert result.facts["idrac"] is True
    assert got["health.system"].value == 0.5 and got["power.on"].value == 1
    assert got["temp:System_Board_Inlet_Temp"].value == 24.5 and got["temp:CPU1_Temp"].value == 61.2
    assert got["temp:CPU2_Temp"].quality == "unsupported" and got["cpu.temp"].value == 61.2
    assert got["fan.rpm:System_Board_Fan1"].value == 5640
    assert got["psu.health:PS1_Status"].value == 1 and got["psu.health:PS2_Status"].value == 0
    assert got["power.watts"].value == 238  # 只取类型 26（整机功率）
    assert got["mem.health:DIMM.Socket.B1"].value == 0.5
    assert got["disk.health:Physical_Disk_0:1:0"].value == 1 and got["disk.health:Physical_Disk_0:1:1"].quality == "unsupported"
    assert got["raid.health:Virtual_Disk_0"].value == 0.5
    # BMC 不采集接口 / HOST-RESOURCES，避免无意义的“不支持”指标
    assert "cpu.load" not in got and not any(k.startswith("if.") for k in got)


@pytest.mark.parametrize("client", [FakeIdrac(idrac=False), FakeIdrac(v1=True)])
def test_non_idrac_devices_keep_generic_collection(client: FakeIdrac) -> None:
    result = SnmpAdapter(client_factory=lambda _c: client).collect(ctx())
    keys = {p.key for p in result.points}
    assert "idrac" not in result.facts and "cpu.load" in keys and "health.system" not in keys


def test_dell_profile_is_snmp_first_and_generic_bmc_polls_less_often() -> None:
    dell = PROFILES["dell-server"]
    assert [(s.protocol, s.mode, s.every) for s in dell.steps] == [("snmp", "primary", 1), ("ipmi", "fallback", 1), ("redfish", "supplement", 10)]
    assert PROFILES["generic-bmc"].interval == 300
    # 4.4 P20a：通用 BMC 改为 Redfish 首选、SNMP 补充、IPMI 兜底
    assert [(s.protocol, s.mode) for s in PROFILES["generic-bmc"].steps] == [("redfish", "primary"), ("snmp", "supplement"), ("ipmi", "fallback")]


def test_every_is_only_allowed_on_supplements(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json
    from pathlib import Path

    bad = [{"name": "x", "label": "x", "interval": 60, "steps": [{"protocol": "snmp", "every": 5}]}]
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: json.dumps(bad) if self.name == "profiles.json" else real(self, *a, **k))
    with pytest.raises(ValueError):
        _load_profiles()


def _day(session: Session, *, snmp_fails: bool) -> dict[str, int]:
    cipher = CredentialCipher(KeyRing.generate())
    a = make_asset(session, "snmp", "ipmi", "redfish", platform="iDRAC9")
    # 与真实 iDRAC 一样，SNMP 也给出温度；采集计划据此让 SNMP 负责温度与健康（4.4 P20a）
    adapters = {"snmp": FakeAdapter("snmp", fail="timeout" if snmp_fails else None, points=[Point("health.system", 1.0), Point("cpu.temp", 50.0, "°C")]),
                "ipmi": FakeAdapter("ipmi", points=[Point("cpu.temp", 50.0, "°C")]),
                "redfish": FakeAdapter("redfish", points=[Point("sel.critical_24h", 0.0)])}
    cycles = 24 * 60
    for i in range(cycles):
        collect_asset(session, cipher, a.id, adapters, now=NOW + timedelta(minutes=i), force=True)
        session.commit()
    return {p: len(ad.contexts) for p, ad in adapters.items()} | {"cycles": cycles}


def test_one_day_of_snmp_first_collection_creates_almost_no_bmc_sessions(session: Session) -> None:
    calls = _day(session, snmp_fails=False)
    # 改动前：Dell 档案 IPMI 为主，每个周期新建一次 IPMI 会话 = 每天 1440 次登录；Redfish 令牌复用不计
    before = calls["cycles"]
    # 改动后：SNMP 无会话；IPMI 只在首次探测时调用一次；Redfish 每 10 个周期一次且复用令牌（最坏按每次都登录计）
    after = calls["ipmi"] + calls["redfish"]
    assert calls["snmp"] == 1440 and calls["ipmi"] == 1 and calls["redfish"] == 144
    assert after <= before * 0.10 + 1  # 多出的 1 次是首次探测


def test_ipmi_takes_over_when_snmp_is_down(session: Session) -> None:
    calls = _day(session, snmp_fails=True)
    # SNMP 每个周期都失败时，每个周期都由 IPMI 接上，数据不断（此时登录日志回到改动前的水平）。
    # 采集计划不再每个周期去试已知不通的 SNMP，只在每小时重新探测时试一次，探测时也会多读一次 Redfish
    assert calls["ipmi"] == 1440 and calls["snmp"] <= 25 and calls["redfish"] <= 144 + 25


def _sel_adapter(entries: list[dict[str, Any]], *, service_id: str = "Sel") -> RedfishAdapter:
    pages = {
        "/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/iDRAC.Embedded.1"}]},
        "/redfish/v1/Managers/iDRAC.Embedded.1": {"LogServices": {"@odata.id": "/m/ls"}},
        "/m/ls": {"Members": [{"@odata.id": "/m/ls/Lclog"}, {"@odata.id": "/m/ls/Sel"}]},
        "/m/ls/Lclog": {"Id": "Lclog", "Entries": {"@odata.id": "/m/ls/Lclog/Entries"}},
        "/m/ls/Sel": {"Id": service_id, "Entries": {"@odata.id": "/m/ls/Sel/Entries"}},
        "/m/ls/Sel/Entries?$top=100": {"Members": entries},
    }
    adapter = RedfishAdapter()
    adapter._get = lambda http, c, path: pages[path]  # type: ignore[method-assign]
    return adapter


def test_sel_counts_recent_critical_and_warning_events_without_clearing() -> None:
    now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    entries = [
        {"Severity": "Critical", "Created": "2026-10-19T11:00:00+00:00", "Message": "fake: The system board fail-safe voltage is outside of range."},
        {"Severity": "Critical", "Created": "2026-10-19T08:00:00Z", "Message": "fake: older critical"},
        {"Severity": "Warning", "Created": "2026-10-18T13:00:00+00:00", "Message": "fake: fan redundancy degraded"},
        {"Severity": "Critical", "Created": "2026-10-17T11:00:00+00:00", "Message": "fake: two days ago"},
        {"Severity": "OK", "Created": "2026-10-19T11:30:00+00:00", "Message": "fake: log cleared"},
        {"Severity": "Critical", "Created": "not-a-date"},
    ]
    pts = {p.key: p for p in _sel_adapter(entries)._sel(None, ctx(), now=now)}  # type: ignore[arg-type]
    assert pts["sel.critical_24h"].value == 2 and "fail-safe voltage" in pts["sel.critical_24h"].message
    assert pts["sel.warning_24h"].value == 1


def test_sel_missing_is_reported_as_unsupported() -> None:
    pts = _sel_adapter([], service_id="Other")._sel(None, ctx())  # type: ignore[arg-type]
    assert [(p.key, p.quality) for p in pts] == [("sel.critical_24h", "unsupported")]


def test_default_rule_alerts_on_bmc_critical_events(session: Session) -> None:
    from sqlalchemy import select

    from app.alert_defaults import ensure_rules
    from app.models import AlertRule

    ensure_rules(session)
    ensure_rules(session)
    session.commit()
    rules = session.scalars(select(AlertRule).where(AlertRule.legacy_id.in_(("bmc_sel_critical", "server_event_critical"))).order_by(AlertRule.legacy_id)).all()
    assert [(r.legacy_id, r.enabled) for r in rules] == [("bmc_sel_critical", False), ("server_event_critical", True)]
    assert rules[1].metric_key == "server_event.open_critical" and rules[1].threshold == 1 and rules[1].severity == "critical"


def test_reduced_supplement_runs_every_cycle_when_it_is_the_only_source(session: Session) -> None:
    cipher = CredentialCipher(KeyRing.generate())
    a = make_asset(session, "redfish", platform="iDRAC9")
    redfish = FakeAdapter("redfish")
    for i in range(20):
        collect_asset(session, cipher, a.id, {"redfish": redfish}, now=NOW + timedelta(minutes=i))
    assert len(redfish.contexts) == 20


def test_profile_dataclass_default_every() -> None:
    assert Step("snmp").every == 1 and isinstance(PROFILES["dell-server"], Profile)
