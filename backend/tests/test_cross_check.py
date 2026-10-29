"""4.4 P20e 双来源核对：温度 / 功耗用主来源和另一个协议比对，不一致时改由 Redfish 提供。只用伪造的适配器，不连接任何设备。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.collect import plan as plans
from app.collect.runner import collect_asset
from app.crypto import CredentialCipher, KeyRing
from app.metrics import Point
from app.models import Asset, CollectionPlan, ManagementEndpoint
from tests.test_collect import FakeAdapter

NOW = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)


def P(key: str, v: float, unit: str = "") -> Point:  # noqa: N802
    return Point(key, v, unit)


def test_comparable_and_tolerance() -> None:
    a = [P("temp:System Board Inlet Temp", 24.0, "°C"), P("cpu.temp", 55.0, "°C"), P("power.watts", 300.0, "W")]
    b = [P("temp:System_Board_Inlet_Temp", 25.0, "°C"), P("temp:CPU1_Temp", 57.0, "°C"), P("power.watts", 320.0, "W")]
    assert plans.comparable("temp", a, b) == [("systemboardinlettemp", 24.0, 25.0)]  # 名称能对上的优先
    assert plans.comparable("temp", [P("cpu.temp", 55.0)], b) == [("CPU 温度", 55.0, 57.0)]  # 对不上时比 CPU 最高温
    assert plans.comparable("power", a, b) == [("整机功耗", 300.0, 320.0)]
    assert not plans._off("power", 300.0, 320.0) and plans._off("power", 300.0, 340.0)
    assert not plans._off("temp", 24.0, 27.0) and plans._off("temp", 24.0, 27.5)
    assert plans.comparable("power", [P("cpu.temp", 1.0)], b) == []


def bmc(db: Session) -> Asset:
    a = Asset(name="fake-xcheck", form="physical", role="server", platform="iDRAC9", collection_profile="dell-server", lifecycle="active")
    for p in ("snmp", "redfish"):
        a.endpoints.append(ManagementEndpoint(address="192.0.2.90", protocol=p, purpose="monitoring"))
    db.add(a)
    db.commit()
    return a


def adapters(snmp_temp: float, snmp_watts: float) -> dict[str, FakeAdapter]:
    return {"snmp": FakeAdapter("snmp", points=[P("temp:Inlet", snmp_temp, "°C"), P("power.watts", snmp_watts, "W"), P("fan.rpm:1", 5000.0)]),
            "redfish": FakeAdapter("redfish", points=[P("temp:Inlet", 24.0, "°C"), P("power.watts", 300.0, "W"), P("raid.health:VD0", 1.0)])}


def test_probe_pins_inconsistent_item_to_redfish(session: Session) -> None:
    cipher = CredentialCipher(KeyRing.generate())
    a = bmc(session)
    collect_asset(session, cipher, a.id, adapters(31.0, 305.0), now=NOW)  # SNMP 温度偏 7℃，功耗一致
    row = session.get(CollectionPlan, a.id)
    assert row.items["temp"][0] == "redfish" and row.items["power"][0] == "snmp" and row.check["pinned"] == {"temp": "redfish"}
    out = plans.describe(row)
    temp = next(c for c in out["check"] if c["key"] == "temp")
    assert temp["ok"] is False and "MIB 对应可能有误" in temp["detail"] and "31" in temp["detail"]
    assert next(c for c in out["check"] if c["key"] == "power")["ok"] is True
    # 页面要求重新探测：清除固定，重新核对（这次一致了）
    row.reprobe = True
    session.commit()
    collect_asset(session, cipher, a.id, adapters(24.5, 305.0), now=NOW + timedelta(minutes=5))
    row = session.get(CollectionPlan, a.id)
    assert row.items["temp"][0] == "snmp" and row.check["pinned"] == {}


def test_daily_check_runs_the_other_protocol_once(session: Session) -> None:
    cipher = CredentialCipher(KeyRing.generate())
    a = bmc(session)
    ad = adapters(24.0, 300.0)
    collect_asset(session, cipher, a.id, ad, now=NOW)
    session.commit()
    redfish_calls = len(ad["redfish"].contexts)
    collect_asset(session, cipher, a.id, ad, now=NOW + timedelta(minutes=1))  # 当天已核对：只用 SNMP
    assert len(ad["redfish"].contexts) == redfish_calls
    ad["snmp"].points = [P("temp:Inlet", 24.0, "°C"), P("power.watts", 380.0, "W"), P("fan.rpm:1", 5000.0)]  # SNMP 功耗偏 27%
    later = NOW + timedelta(days=1, minutes=1)
    collect_asset(session, cipher, a.id, ad, now=later)
    row = session.get(CollectionPlan, a.id)
    assert len(ad["redfish"].contexts) == redfish_calls + 1  # 每天一次读另一个协议
    assert row.items["power"][0] == "redfish" and row.check["pinned"] == {"power": "redfish"}
    assert row.checked_at.replace(tzinfo=UTC) == later
    # 固定在之后的自动探测（超过 7 天）中保留
    collect_asset(session, cipher, a.id, ad, now=NOW + timedelta(days=8))
    row = session.get(CollectionPlan, a.id)
    assert row.items["power"][0] == "redfish"
