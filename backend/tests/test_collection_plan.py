"""4.4 P20a 采集计划：探测各协议能读到的数据，每项数据只由主来源提供，主来源失败才交给兜底。全部为伪造适配器。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collect import plan as plans
from app.collect.runner import collect_asset
from app.crypto import CredentialCipher
from app.metrics import Point
from app.models import CollectionPlan, CollectionStatus, LatestMetric, ManagementEndpoint
from tests.test_collect import FakeAdapter, cipher, make_asset  # noqa: F401  （cipher 是 fixture）
from tests.test_bmc_snmp_first import NOW  # 10:00，按分钟算正好是低频周期


def fleet(*, snmp_fails: bool = False) -> dict[str, FakeAdapter]:
    return {
        "snmp": FakeAdapter("snmp", fail="timeout" if snmp_fails else None,
                            points=[Point("temp:Inlet", 24.0, "°C"), Point("fan.rpm:Fan1", 5600.0, "RPM"), Point("health.system", 1.0),
                                    Point("disk.health:0", None, quality="unsupported"), Point("bmc.uptime", 100.0, "s")]),
        "redfish": FakeAdapter("redfish", points=[Point("temp:Inlet", 25.0, "°C"), Point("power.watts", 230.0, "W"), Point("disk.health:0", 1.0),
                                                  Point("raid.health:VD0", 1.0), Point("sel.critical_24h", 0.0)]),
        "ipmi": FakeAdapter("ipmi", points=[Point("temp:Inlet", 26.0, "°C"), Point("power.watts", 231.0, "W"), Point("volt:12V", 12.1, "V")]),
    }


def latest(db: Session, asset_id) -> dict[str, tuple[float | None, str, str]]:  # type: ignore[no-untyped-def]
    return {m.metric_key: (m.value, m.source, m.message) for m in db.scalars(select(LatestMetric).where(LatestMetric.asset_id == asset_id))}


def test_item_mapping_and_preferences() -> None:
    assert plans.item_of("temp:Inlet") == plans.item_of("cpu.temp") == "temp"
    assert plans.item_of("raid.health:VD0") == "storage" and plans.item_of("sel.warning_24h") == "sel"
    assert plans.item_of("volt:12V") is None
    built = plans.build({"snmp": ["temp", "health"], "redfish": ["temp", "storage"], "ipmi": ["temp"]})
    # 高频数据 SNMP 优先，低频数据 Redfish 优先，IPMI 最后
    assert built == {"temp": ["snmp", "redfish", "ipmi"], "health": ["snmp"], "storage": ["redfish"]}


def test_probe_then_each_item_comes_from_one_source(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "snmp", "redfish", "ipmi", platform="iDRAC9")
    ad = fleet()
    run = collect_asset(session, cipher, a.id, ad, now=NOW)
    session.commit()
    assert run.probed == "首次采集" and all(len(x.contexts) == 1 for x in ad.values())
    p = session.get(CollectionPlan, a.id)
    assert p.items["temp"] == ["snmp", "redfish", "ipmi"] and p.items["power"] == ["redfish", "ipmi"]
    assert p.items["storage"] == ["redfish"] and p.items["sel"] == ["redfish"]
    assert "disk.health" not in str(p.probed["snmp"])  # SNMP 的硬盘只有“不支持”，不算读到
    got = latest(session, a.id)
    assert got["temp:Inlet"][:2] == (24.0, "snmp") and got["power.watts"][:2] == (230.0, "redfish")
    assert got["disk.health:0"][1] == "redfish" and got["volt:12V"][1] == "ipmi"  # 不属于数据项的随协议保留

    # 下一分钟（不是低频周期）：只连 SNMP（温度、风扇、健康）和 Redfish（功耗的主来源），不连 IPMI
    collect_asset(session, cipher, a.id, ad, now=NOW + timedelta(minutes=1))
    session.commit()
    assert [len(ad[x].contexts) for x in ("snmp", "redfish", "ipmi")] == [2, 2, 1]
    assert latest(session, a.id)["temp:Inlet"][1] == "snmp"


def test_primary_failure_hands_items_to_fallback_with_note(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "snmp", "redfish", "ipmi", platform="iDRAC9")
    collect_asset(session, cipher, a.id, fleet(), now=NOW)
    session.commit()
    ad = fleet(snmp_fails=True)
    run = collect_asset(session, cipher, a.id, ad, now=NOW + timedelta(minutes=1))
    session.commit()
    assert run.degraded and len(ad["ipmi"].contexts) == 0  # 温度交给 Redfish（第二顺位），不用 IPMI
    value, source, message = latest(session, a.id)["temp:Inlet"]
    assert (value, source) == (25.0, "redfish") and "降级：SNMP 不可用，温度改用 Redfish" in message


def test_reprobe_on_endpoint_change_request_and_age(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "snmp", "redfish", platform="iDRAC9")
    ad = fleet()
    collect_asset(session, cipher, a.id, ad, now=NOW)
    session.commit()
    assert collect_asset(session, cipher, a.id, ad, now=NOW + timedelta(minutes=1)).probed == ""
    session.add(ManagementEndpoint(asset_id=a.id, address="192.0.2.99", protocol="ipmi", purpose="monitoring"))
    session.commit()
    session.refresh(a)
    assert collect_asset(session, cipher, a.id, ad, now=NOW + timedelta(minutes=2)).probed == "管理端点有变化"
    session.get(CollectionPlan, a.id).reprobe = True
    session.commit()
    assert collect_asset(session, cipher, a.id, ad, now=NOW + timedelta(minutes=3)).probed == "页面要求重新探测"
    assert "超过 7 天" in collect_asset(session, cipher, a.id, ad, now=NOW + timedelta(days=8)).probed


def test_failed_protocol_is_retried_hourly_and_unused_one_is_skipped(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "snmp", "redfish", "ipmi", platform="iDRAC9")
    collect_asset(session, cipher, a.id, fleet(snmp_fails=True), now=NOW)
    session.commit()
    assert session.get(CollectionPlan, a.id).probed["snmp"] is None
    assert collect_asset(session, cipher, a.id, fleet(), now=NOW + timedelta(minutes=30)).probed == ""
    assert "每小时重试" in collect_asset(session, cipher, a.id, fleet(), now=NOW + timedelta(minutes=61)).probed
    session.commit()
    # SNMP 恢复后重新纳入计划；IPMI 仍在计划里做兜底，所以不是“不使用”
    assert session.get(CollectionPlan, a.id).items["temp"][0] == "snmp"
    other = make_asset(session, "redfish", "snmp", platform="dell-idrac")
    only_generic = {"redfish": fleet()["redfish"], "snmp": FakeAdapter("snmp", points=[Point("bmc.uptime", 1.0)])}
    collect_asset(session, cipher, other.id, only_generic, now=NOW)
    run = collect_asset(session, cipher, other.id, only_generic, now=NOW + timedelta(minutes=1))
    session.commit()
    assert run.skipped == ["snmp"] and len(only_generic["snmp"].contexts) == 1
    st = session.scalar(select(CollectionStatus).where(CollectionStatus.asset_id == other.id, CollectionStatus.protocol == "snmp"))
    assert st.state == "skipped" and "采集计划不使用它" in st.message


def test_single_protocol_bmc_keeps_template_behaviour(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "ipmi", platform="")
    run = collect_asset(session, cipher, a.id, {"ipmi": FakeAdapter("ipmi")}, now=NOW)
    session.commit()
    assert run.profile == "generic-bmc" and run.ok == ["ipmi"] and session.get(CollectionPlan, a.id) is None


def test_plan_api_and_reprobe(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    from tests.api_helpers import add_user, login

    add_user(factory, "olivia", "operator")
    add_user(factory, "victor", "viewer")
    op, viewer = login(app_factory(), "olivia"), login(app_factory(), "victor")
    with factory() as db:
        a = make_asset(db, "snmp", "redfish", platform="iDRAC9")
        single = make_asset(db, "ipmi")
        collect_asset(db, app_factory.cipher, a.id, fleet(), now=NOW)
        db.commit()
        aid, sid = a.id, single.id
    plan = viewer.get(f"/api/assets/{aid}/collection").json()["plan"]
    rows = {r["key"]: r for r in plan["items"]}
    assert plan["exists"] and rows["temp"]["primary"] == "snmp" and rows["temp"]["fallbacks"] == ["redfish"]
    assert rows["storage"]["primary"] == "redfish" and rows["storage"]["slow"] and plan["slow_every"] == plans.SLOW_EVERY
    assert viewer.get(f"/api/assets/{sid}/collection").json()["plan"] is None
    assert viewer.post(f"/api/assets/{aid}/collection-plan/reprobe").status_code == 403
    assert op.post(f"/api/assets/{sid}/collection-plan/reprobe").status_code == 422
    assert op.post(f"/api/assets/{aid}/collection-plan/reprobe").status_code == 202
    assert viewer.get(f"/api/assets/{aid}/collection").json()["plan"]["reprobe"] is True


def test_empty_plan_falls_back_to_template_and_keeps_failures_visible(session: Session, cipher: CredentialCipher) -> None:
    # 预览走查发现：探测时所有协议都不可用，计划为空；之后不能把它们标成“未使用”，也不能一小时不采集
    a = make_asset(session, "redfish", "ipmi", platform="dell-idrac")
    down = {"redfish": FakeAdapter("redfish", fail="timeout"), "ipmi": FakeAdapter("ipmi", fail="timeout")}
    collect_asset(session, cipher, a.id, down, now=NOW)
    session.commit()
    assert session.get(CollectionPlan, a.id).items == {}
    run = collect_asset(session, cipher, a.id, down, now=NOW + timedelta(minutes=10), force=True)
    session.commit()
    assert run.probed == "" and "ipmi" in run.failed and run.skipped == []  # 按模板：IPMI 兜底仍在尝试
    states = {s.protocol: s.state for s in session.scalars(select(CollectionStatus).where(CollectionStatus.asset_id == a.id))}
    assert "skipped" not in states.values()
    up = {"redfish": fleet()["redfish"], "ipmi": fleet()["ipmi"]}
    assert "每小时重试" in collect_asset(session, cipher, a.id, up, now=NOW + timedelta(minutes=61)).probed
    session.commit()
    assert session.get(CollectionPlan, a.id).items["temp"] == ["redfish", "ipmi"]


def test_redfish_login_timeout_has_a_readable_message() -> None:
    import httpx
    import pytest

    from app.collect.base import AdapterError, EndpointContext
    from app.collect.redfish import RedfishAdapter

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("_ssl.c:989: The handshake operation timed out", request=request)

    ctx = EndpointContext(address="192.0.2.11", port=443, protocol="redfish", username="fake-ro", secret="fake-pass", tls_verify=False)
    with pytest.raises(AdapterError) as exc:
        RedfishAdapter(transport=httpx.MockTransport(handler)).collect(ctx)
    assert exc.value.kind == "timeout" and "_ssl" not in str(exc.value)
