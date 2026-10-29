"""4.4 P20c 服务器告警：BMC 日志逐条入库、去重、IPMI 兜底读 SEL、采集计划只存负责协议的条目、板块接口。全部为伪造数据。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import server_events
from app.collect.ipmi import IpmiAdapter, PyghmiClient
from app.collect.runner import collect_asset
from app.crypto import CredentialCipher
from app.metrics import Point
from app.models import ServerEvent
from tests.test_bmc_snmp_first import _sel_adapter, ctx
from tests.test_collect import FakeAdapter, cipher, make_asset  # noqa: F401  （cipher 是 fixture）
from tests.test_collect import NOW as BASE
from tests.test_ipmi_redfish import READINGS, FakeCommand
from tests.test_ipmi_redfish import ctx as ipmi_ctx

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
ENTRIES = [
    {"id": "1", "created": "2026-10-27T11:00:00+00:00", "severity": "Critical", "message": "fake: The system board Inlet temperature is above the critical threshold"},
    {"id": "2", "created": "2026-10-26T09:00:00Z", "severity": "Warning", "message": "fake: Power supply redundancy is degraded"},
    {"id": "3", "created": "2026-09-01T09:00:00Z", "severity": "Critical", "message": "fake: Correctable memory error rate exceeded for DIMM A1"},
    {"id": "4", "created": "2026-10-27T11:30:00Z", "severity": "OK", "message": "fake: Log cleared"},
    {"id": "5", "created": "not-a-date", "severity": "Critical", "message": "fake: no time"},
]


def events(db: Session, asset_id) -> list[ServerEvent]:  # type: ignore[no-untyped-def]
    return list(db.scalars(select(ServerEvent).where(ServerEvent.asset_id == asset_id).order_by(ServerEvent.occurred_at.desc())))


def test_record_dedupes_classifies_and_archives_old_or_info(session: Session) -> None:
    a = make_asset(session, "redfish")
    assert server_events.record(session, a.id, "redfish", ENTRIES, NOW) == 4  # 时间读不出的跳过
    assert server_events.record(session, a.id, "redfish", ENTRIES, NOW + timedelta(minutes=5)) == 0  # 再读不重复
    got = {e.record_key.split("@")[0]: e for e in events(session, a.id)}
    assert (got["1"].severity, got["1"].category, got["1"].state) == ("critical", "temperature", "open")
    assert (got["2"].severity, got["2"].category, got["2"].state) == ("warning", "power", "open")
    assert (got["3"].category, got["3"].state) == ("memory", "archived")  # 首次读到时已是一个多月前
    assert (got["4"].severity, got["4"].state) == ("info", "archived")
    # BMC 清空日志后记录号 1 重用：时间不同，算新的一条
    again = [{"id": "1", "created": "2026-10-27T11:50:00Z", "severity": "Warning", "message": "fake: Fan 3 RPM is less than the lower warning threshold"}]
    assert server_events.record(session, a.id, "redfish", again, NOW) == 1
    assert events(session, a.id)[0].category == "fan"
    assert server_events.prune(session, now=NOW + timedelta(days=200)) == 5


def test_redfish_sel_hands_every_entry_to_collection() -> None:
    entries: list[dict[str, Any]] = []
    raw = [{"Id": "7", "Severity": "Critical", "Created": "2026-10-27T11:00:00+00:00", "Message": "fake: PSU 2 lost input"}]
    _sel_adapter(raw)._sel(None, ctx(), now=NOW, entries=entries)  # type: ignore[arg-type]
    assert entries == [{"id": "7", "created": "2026-10-27T11:00:00+00:00", "severity": "Critical", "message": "fake: PSU 2 lost input"}]


def test_ipmi_reads_sel_only_when_asked() -> None:
    cmd = FakeCommand(READINGS)
    log = [{"event_id": 21, "timestamp": NOW.timestamp() - 600, "severity": 2, "component": "PS2 Status", "event": "Power Supply AC lost"},
           {"event_id": 22, "timestamp": NOW.timestamp() - 300, "severity": 1, "component": "Fan3", "event": "Lower Critical going low",
            "deassertion": True}]
    cmd.get_event_log = lambda: iter(log)  # type: ignore[attr-defined]
    adapter = IpmiAdapter(client_factory=lambda c: PyghmiClient(c, command=cmd), reuse_sessions=False)
    plain = adapter.collect(ipmi_ctx("ipmi", 623))
    assert "sel_entries" not in plain.facts and not any(p.key.startswith("sel.") for p in plain.points)
    c = ipmi_ctx("ipmi", 623)
    c.options["sel"] = True
    r = adapter.collect(c)
    got = r.facts["sel_entries"]
    assert got[0] == {"id": 21, "created": log[0]["timestamp"], "severity": "Critical", "message": "PS2 Status Power Supply AC lost"}
    assert got[1]["severity"] == "Warning" and got[1]["message"].endswith("（已恢复）")
    assert {p.key for p in r.points} >= {"sel.critical_24h", "sel.warning_24h"}


def test_plan_stores_sel_only_from_its_owner(session: Session, cipher: CredentialCipher) -> None:
    sel_from = {"redfish": [{"id": "r1", "created": BASE.isoformat(), "severity": "Critical", "message": "fake: redfish view"}],
                "ipmi": [{"id": "i1", "created": BASE.isoformat(), "severity": "Critical", "message": "fake: ipmi view"}]}

    class SelAdapter(FakeAdapter):
        def collect(self, c):  # type: ignore[no-untyped-def]
            r = super().collect(c)
            if self.protocol == "redfish" or c.options.get("sel"):
                r.facts["sel_entries"] = sel_from[self.protocol]
            return r

    a = make_asset(session, "redfish", "ipmi", platform="dell-idrac")
    ad = {"redfish": SelAdapter("redfish", points=[Point("sel.critical_24h", 1.0), Point("power.watts", 200.0, "W")]),
          "ipmi": SelAdapter("ipmi", points=[Point("sel.critical_24h", 1.0), Point("power.watts", 201.0, "W")])}
    collect_asset(session, cipher, a.id, ad, now=BASE)
    session.commit()
    got = events(session, a.id)
    assert [(e.source, e.message) for e in got] == [("redfish", "fake: redfish view")]  # 同一事件不从两个协议各存一份
    # Redfish 不通：日志改由 IPMI 读（低频周期）
    ad["redfish"].fail = "timeout"
    collect_asset(session, cipher, a.id, ad, now=BASE + timedelta(minutes=10))
    session.commit()
    assert {e.source for e in events(session, a.id)} == {"redfish", "ipmi"}
    assert ad["ipmi"].contexts[-1].options.get("sel") is True


def test_ipmi_only_server_reads_sel_every_tenth_cycle(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "ipmi")
    ipmi = FakeAdapter("ipmi")
    for i in range(10):
        collect_asset(session, cipher, a.id, {"ipmi": ipmi}, now=BASE + timedelta(minutes=5 * i))
    assert [bool(c.options.get("sel")) for c in ipmi.contexts].count(True) == 1


def test_board_api_filters_and_handling(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    from tests.api_helpers import add_user, login

    add_user(factory, "olivia", "operator")
    add_user(factory, "victor", "viewer")
    op, viewer = login(app_factory(), "olivia"), login(app_factory(), "victor")
    now = datetime.now(UTC)
    with factory() as db:
        a = make_asset(db, "redfish")
        fresh = [{**e, "created": (now - timedelta(hours=i + 1)).isoformat()} for i, e in enumerate(ENTRIES[:2])]
        server_events.record(db, a.id, "redfish", fresh, now)
        db.commit()
    page = viewer.get("/api/server-events").json()
    assert page["total"] == 2 and page["items"][0]["severity"] == "critical" and page["items"][0]["category_label"] == "温度"
    assert viewer.get("/api/server-events", params={"severity": "warning"}).json()["total"] == 1
    assert viewer.get("/api/server-events", params={"q": "redundancy"}).json()["total"] == 1
    assert viewer.get("/api/server-events/summary").json()["open"]["critical"] == {"events": 1, "servers": 1}
    ids = [i["id"] for i in page["items"]]
    assert viewer.post("/api/server-events/state", json={"ids": ids, "state": "handled", "note": "x"}).status_code == 403
    assert op.post("/api/server-events/state", json={"ids": ids, "state": "handled"}).status_code == 422  # 要写处理结果
    assert op.post("/api/server-events/state", json={"ids": ids, "state": "handled", "note": "已更换电源"}).json() == {"updated": 2}
    assert viewer.get("/api/server-events").json()["total"] == 0
    handled = viewer.get("/api/server-events", params={"state": "handled"}).json()["items"]
    assert handled[0]["note"] == "已更换电源" and handled[0]["handled_by"]
    assert op.post("/api/server-events/state", json={"ids": ids[:1], "state": "open"}).json() == {"updated": 1}
