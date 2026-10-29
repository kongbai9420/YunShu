"""4.4 P20c 服务器告警推送：SNMP Trap 接收、Redfish 事件接收、在 BMC 上配置推送目标、严重事件直接通知。

只用伪造的 Trap 报文、伪造的 Redfish 接口与内存数据库，不监听真实端口，不连接任何设备。
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pyasn1.codec.ber import encoder
from pysnmp.proto import api, rfc1902
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import server_events, snmp_trap
from app.control.base import ControlError
from app.control.bmc_snmp import BMC_SNMP_TASK, RedfishConfigClient, SnmpCredential, execute_bmc_push
from app.models import AlertEvent, AlertRule, Asset, BmcEventSubscription, ManagementEndpoint, ServerEvent
from app.services.credentials import create_credential, reveal_secret
from tests.api_helpers import add_user, login
from tests.test_bmc_snmp import DELL, FakeRedfish, rf_ctx
from tests.test_control_power import FAKE_SECRET, make_asset, set_owner, setup  # noqa: F401  （setup 是 fixture）

V2C = SnmpCredential("v2c", community="fake-community")
NOW = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)
DELL_MSG, DELL_STATUS = "1.3.6.1.4.1.674.10892.5.3.1.2", "1.3.6.1.4.1.674.10892.5.3.1.3"


# ---- Trap 报文 ----


def v2c_trap(community: str, binds: list[tuple[str, object]], *, oid: str = "1.3.6.1.4.1.674.10892.5.3.2.1.0.2102", uptime: int = 4242,
             inform: bool = False) -> bytes:
    m = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
    pdu = m.InformRequestPDU() if inform else m.SNMPv2TrapPDU()
    m.apiPDU.set_defaults(pdu)
    m.apiPDU.set_varbinds(pdu, [(snmp_trap.SYS_UPTIME, rfc1902.TimeTicks(uptime)), (snmp_trap.TRAP_OID, rfc1902.ObjectName(oid))] + binds)
    msg = m.Message()
    m.apiMessage.set_defaults(msg)
    m.apiMessage.set_community(msg, community)
    m.apiMessage.set_pdu(msg, pdu)
    return encoder.encode(msg)


def v1_trap(community: str, generic: int) -> bytes:
    m = api.PROTOCOL_MODULES[api.SNMP_VERSION_1]
    pdu = m.TrapPDU()
    m.apiTrapPDU.set_defaults(pdu)
    m.apiTrapPDU.set_generic_trap(pdu, generic)
    msg = m.Message()
    m.apiMessage.set_defaults(msg)
    m.apiMessage.set_community(msg, community)
    m.apiMessage.set_pdu(msg, pdu)
    return encoder.encode(msg)


def dell_trap(text: str = "fake: The system inlet temperature is greater than the upper critical threshold.", status: int = 5, **kw) -> bytes:  # type: ignore[no-untyped-def]
    return v2c_trap("fake-community", [(DELL_MSG, rfc1902.OctetString(text)), (DELL_STATUS, rfc1902.Integer(status))], **kw)


def test_decode_dell_generic_and_other_traps() -> None:
    t = snmp_trap.decode(dell_trap())
    e = snmp_trap.to_entry(t, NOW)
    assert t.version == "v2c" and t.community == "fake-community" and e["severity"] == "critical" and "inlet temperature" in e["message"]
    assert snmp_trap.to_entry(snmp_trap.decode(dell_trap("fake: Fan 3 RPM is outside of normal range.", status=4)), NOW)["severity"] == "warning"
    assert snmp_trap.to_entry(snmp_trap.decode(dell_trap("fake: Fan 3 RPM returned to normal.", status=3)), NOW)["severity"] == "info"
    cold = snmp_trap.to_entry(snmp_trap.decode(v1_trap("fake-community", 0)), NOW)
    assert "coldStart" in cold["message"]
    other = v2c_trap("fake-community", [("1.3.6.1.4.1.99999.1.1", rfc1902.OctetString("fake: Power supply 2 failed"))], oid="1.3.6.1.4.1.99999.0.1")
    assert snmp_trap.to_entry(snmp_trap.decode(other), NOW)["severity"] == "critical"
    assert snmp_trap.decode(dell_trap(inform=True)).response  # Inform 要回应
    for junk in (b"fake", b"\x30\x03\x02\x01\x03"):  # 乱码、SNMPv3
        with pytest.raises(ValueError):
            snmp_trap.decode(junk)


# ---- Trap 接收：来源与团体名 ----


def build_trap_asset(db: Session, cipher, *, community: str = "fake-community"):  # type: ignore[no-untyped-def]
    a = make_asset(db, cipher, name="fake-r740", address="192.0.2.40")
    cred = create_credential(db, cipher, alias=f"snmp-{uuid.uuid4()}", kind="snmp_community", username="", secret=community)
    db.add(ManagementEndpoint(asset_id=a.id, address="192.0.2.40", protocol="snmp", purpose="monitoring", credential_id=cred.id))
    db.commit()
    return a.id


def receiver(factory, cipher) -> snmp_trap.TrapReceiver:  # type: ignore[no-untyped-def]
    return snmp_trap.TrapReceiver(factory, lambda cred: reveal_secret(cred, cipher), port=0, bind="127.0.0.1")


def test_trap_receiver_checks_source_and_community(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        aid = build_trap_asset(db, app_factory.cipher)
    r = receiver(factory, app_factory.cipher)
    assert r.handle(dell_trap(), "198.51.100.9", now=NOW)[0] == "unknown_source"
    assert r.handle(v2c_trap("wrong", []), "192.0.2.40", now=NOW)[0] == "bad_community"
    assert r.handle(dell_trap(), "192.0.2.40", now=NOW)[0] == "accepted"
    assert r.handle(dell_trap(), "192.0.2.40", now=NOW)[0] == "duplicate"  # 同一条（同 uptime）重发
    result, reply = r.handle(dell_trap(inform=True, uptime=5000), "192.0.2.40", now=NOW)
    assert result == "accepted" and reply
    with factory() as db:
        rows = db.scalars(select(ServerEvent).where(ServerEvent.asset_id == aid)).all()
        assert {(e.source, e.severity, e.state) for e in rows} == {("snmp_trap", "critical", "open")} and len(rows) == 2
    assert r.stats["unknown_source"] == 1 and r.stats["bad_community"] == 1



def test_trap_receiver_listens_on_udp_and_answers_informs(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    import socket
    import threading
    import time

    with factory() as db:
        a = make_asset(db, app_factory.cipher, name="fake-loopback", address="127.0.0.1")
        cred = create_credential(db, app_factory.cipher, alias="snmp-loop", kind="snmp_community", username="", secret="fake-community")
        db.add(ManagementEndpoint(asset_id=a.id, address="127.0.0.1", protocol="snmp", purpose="monitoring", credential_id=cred.id))
        db.commit()
    r = receiver(factory, app_factory.cipher)
    threading.Thread(target=r.serve, daemon=True).start()
    assert r.ready.wait(5)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(5)
            s.sendto(dell_trap(inform=True), ("127.0.0.1", r.port))
            assert s.recvfrom(65535)[0]  # Inform 的回应
        deadline = time.monotonic() + 5
        while r.stats["accepted"] < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert r.stats["accepted"] == 1
    finally:
        r.stop()

def test_trap_community_hash_from_platform_configuration_and_rate_limit(app_factory, factory, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        a = make_asset(db, app_factory.cipher, name="fake-r640", address="192.0.2.41")  # 只有控制端点，没有 SNMP 凭据
        db.add(BmcEventSubscription(asset_id=a.id, kind="snmp_trap", destination="192.0.2.10",
                                    token_hash=hashlib.sha256(b"fake-trap-community").hexdigest()))
        db.commit()
        aid = a.id
    r = receiver(factory, app_factory.cipher)
    assert r.handle(v2c_trap("fake-other", []), "192.0.2.41", now=NOW)[0] == "bad_community"
    monkeypatch.setattr(snmp_trap, "RATE_PER_MINUTE", 2)
    results = [r.handle(v2c_trap("fake-trap-community", [], uptime=i), "192.0.2.41", now=NOW)[0] for i in range(3)]
    assert results == ["accepted", "accepted", "rate_limited"]
    with factory() as db:
        sub = db.scalar(select(BmcEventSubscription).where(BmcEventSubscription.asset_id == aid))
        assert sub.events_received == 2 and sub.last_event_at is not None


def test_pushed_and_polled_copies_of_one_event_are_stored_once(session: Session) -> None:
    from app.crypto import CredentialCipher, KeyRing

    a = make_asset(session, CredentialCipher(KeyRing.generate()), name="fake-r750", address="192.0.2.42")
    text = "fake: The power supply 2 lost AC power."
    assert server_events.record(session, a.id, "snmp_trap", [{"id": "t1", "created": NOW, "severity": "critical", "message": text}], NOW) == 1
    polled = [{"id": "17", "created": NOW - timedelta(seconds=40), "severity": "Critical", "message": text.rstrip(".") + " "}]
    assert server_events.record(session, a.id, "redfish", polled, NOW) == 0
    later = [{"id": "18", "created": NOW + timedelta(minutes=10), "severity": "Critical", "message": text}]
    assert server_events.record(session, a.id, "redfish", later, NOW) == 1  # 超过 2 分钟算另一件事


# ---- Redfish 事件接收 ----


def test_redfish_event_receiver_needs_a_known_token(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    client = app_factory()
    with factory() as db:
        a = make_asset(db, app_factory.cipher, name="fake-r740xd", address="192.0.2.43")
        db.add(BmcEventSubscription(asset_id=a.id, kind="redfish_event", destination="https://yunshu.example.test/api/bmc-events/",
                                    token_hash=hashlib.sha256(b"ysb_fake-token").hexdigest()))
        db.commit()
        aid = a.id
    body = {"Events": [{"EventId": "8812", "EventTimestamp": "2026-10-27T02:59:00+00:00", "MessageId": "PSU0003",
                        "MessageSeverity": "Critical", "Message": "fake: The power input for power supply 1 is lost.",
                        "OriginOfCondition": {"@odata.id": "/redfish/v1/Chassis/System.Embedded.1/Power"}},
                       {"EventId": "8813", "MessageSeverity": "OK", "Message": "fake: The power input for power supply 1 has been restored."}]}
    assert client.post("/api/bmc-events/ysb_wrong", json=body).status_code == 404
    r = client.post("/api/bmc-events/ysb_fake-token", json=body)
    assert r.status_code == 200 and r.json() == {"accepted": 2}
    assert client.post("/api/bmc-events/ysb_fake-token", content=b"not json").status_code == 422
    with factory() as db:
        rows = {e.id: e for e in db.scalars(select(ServerEvent).where(ServerEvent.asset_id == aid))}
        crit = next(e for e in rows.values() if e.severity == "critical")
        assert crit.source == "redfish_event" and crit.state == "open" and crit.category == "power" and "Chassis" in crit.message
        assert any(e.severity == "info" and e.state == "archived" for e in rows.values())
        sub = db.scalar(select(BmcEventSubscription).where(BmcEventSubscription.asset_id == aid))
        assert sub.events_received == 1 and sub.last_event_at is not None


# ---- 在 BMC 上配置推送 ----

EVENTS = "/redfish/v1/EventService"
SUBS = "/redfish/v1/EventService/Subscriptions"


class FakeEventBmc(FakeRedfish):
    """在 FakeRedfish 上加事件订阅集合：POST 新建、DELETE 删除。"""

    def __init__(self, tree: dict, *, snmp: bool = False, hang_post: bool = False) -> None:
        tree = copy.deepcopy(tree)
        tree[EVENTS] = {"ServiceEnabled": True, "EventTypesForSubscription": ["Alert", "StatusChange"],
                        "Subscriptions": {"@odata.id": SUBS}, **({"SNMP": {}} if snmp else {})}
        tree[SUBS] = {"Members": []}
        super().__init__(tree)
        self.posts: list[dict] = []
        self.hang_post = hang_post
        self.next = 1

    def add(self, destination: str) -> str:
        uri = f"{SUBS}/{self.next}"
        self.next += 1
        self.tree[uri] = {"Destination": destination}
        self.tree[SUBS]["Members"].append({"@odata.id": uri})
        return uri

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == SUBS and request.headers.get("X-Auth-Token") == "fake-token":
            self.calls.append(f"POST {path}")
            if self.hang_post:
                raise httpx.ReadTimeout("fake timeout", request=request)
            body = json.loads(request.content)
            self.posts.append(body)
            return httpx.Response(201, headers={"Location": f"https://192.0.2.40{self.add(body['Destination'])}"})
        if request.method == "DELETE" and path.startswith(SUBS + "/"):
            self.calls.append(f"DELETE {path}")
            self.tree.pop(path, None)
            self.tree[SUBS]["Members"] = [m for m in self.tree[SUBS]["Members"] if m["@odata.id"] != path]
            return httpx.Response(204)
        return super().handler(request)


def push(fake: FakeEventBmc, action: str, cred: SnmpCredential | None = V2C, **kw):  # type: ignore[no-untyped-def]
    kw = {"target": "192.0.2.10", "events_url": "https://yunshu.example.test", "token": "ysb_fake-token"} | kw
    return execute_bmc_push(rf_ctx(), action, cred=cred, factory=lambda c: RedfishConfigClient(c, transport=httpx.MockTransport(fake.handler)), **kw)


ATTR = "/redfish/v1/Managers/iDRAC.Embedded.1/Attributes"


def dell_with_slots(**extra) -> dict:  # type: ignore[no-untyped-def]
    tree = copy.deepcopy(DELL)
    tree[ATTR]["Attributes"].update({"SNMP.1.AgentCommunity": "fake-community", "IPMILan.1.AlertEnable": "Disabled", "SNMP.1.TrapFormat": "SNMPv1",
                                     "SNMPAlert.1.DestAddr": "198.51.100.7", "SNMPAlert.1.State": "Enabled",
                                     "SNMPAlert.2.DestAddr": "", "SNMPAlert.2.State": "Disabled", **extra})
    return tree


def test_dell_trap_target_uses_a_free_slot_and_never_overwrites_others() -> None:
    fake = FakeEventBmc(dell_with_slots())
    out = push(fake, "trap_on")
    assert out.status == "verified" and out.observed["slot"] == "2"
    assert fake.patches == [{"Attributes": {"SNMPAlert.2.DestAddr": "192.0.2.10", "SNMPAlert.2.State": "Enabled", "IPMILan.1.AlertEnable": "Enabled"}}]
    assert "全局告警开关" in out.message and "fake-community" not in json.dumps(out.observed)
    assert push(fake, "trap_on").message.startswith("没有需要修改")  # 已配置：不再修改
    off = push(fake, "trap_off", cred=None)
    assert off.status == "verified" and fake.patches[-1] == {"Attributes": {"SNMPAlert.2.State": "Disabled", "SNMPAlert.2.DestAddr": ""}}
    assert fake.tree[ATTR]["Attributes"]["SNMPAlert.1.DestAddr"] == "198.51.100.7"  # 别人的目标不动
    full = FakeEventBmc(dell_with_slots(**{"SNMPAlert.2.DestAddr": "198.51.100.8"}))
    with pytest.raises(ControlError) as exc:
        push(full, "trap_on")
    assert exc.value.kind == "rejected" and "占用" in str(exc.value) and full.patches == []
    other = FakeEventBmc(dell_with_slots(**{"SNMP.1.AgentCommunity": "someone-else"}))
    with pytest.raises(ControlError) as exc:
        push(other, "trap_on")
    assert "团体名" in str(exc.value) and other.patches == []
    with pytest.raises(ControlError):
        push(FakeEventBmc(dell_with_slots()), "trap_on", cred=SnmpCredential("v3", user="fake-snmp"))


def test_standard_trap_subscription_or_manual_hint() -> None:
    from tests.test_bmc_snmp import NO_SNMP

    fake = FakeEventBmc(NO_SNMP, snmp=True)
    out = push(fake, "trap_on")
    assert out.status == "verified" and fake.posts == [{"Destination": "snmp://192.0.2.10:162", "SubscriptionType": "SNMPTrap", "Protocol": "SNMPv2c",
                                                         "Context": "yunshu", "SNMP": {"TrapCommunity": "fake-community"}}]
    assert push(fake, "trap_off", cred=None).status == "verified" and fake.tree[SUBS]["Members"] == []
    with pytest.raises(ControlError) as exc:
        push(FakeEventBmc(NO_SNMP), "trap_on")
    assert exc.value.kind == "unsupported" and "192.0.2.10" in str(exc.value)


def test_event_subscription_replaces_old_platform_subscriptions_only() -> None:
    fake = FakeEventBmc(DELL)
    fake.add("https://yunshu.example.test/api/bmc-events/ysb_old")
    fake.add("https://other-monitor.example.test/hook")
    out = push(fake, "events_on")
    assert out.status == "verified" and out.observed["removed"] == 1 and out.observed["remote"] == f"{SUBS}/3"
    assert fake.posts == [{"Destination": "https://yunshu.example.test/api/bmc-events/ysb_fake-token", "Protocol": "Redfish", "Context": "yunshu",
                           "EventTypes": ["Alert"]}]
    assert "ysb_fake-token" not in json.dumps(out.observed)
    off = push(fake, "events_off")
    assert off.status == "verified" and [fake.tree[m["@odata.id"]]["Destination"] for m in fake.tree[SUBS]["Members"]] == ["https://other-monitor.example.test/hook"]
    with pytest.raises(ControlError) as exc:
        push(FakeEventBmc(DELL, hang_post=True), "events_on")
    assert exc.value.kind == "timeout"


# ---- 门禁与整条流程 ----


class Fleet:
    def __init__(self, fake: FakeEventBmc) -> None:
        self.fake = fake

    def factory(self, ctx):  # type: ignore[no-untyped-def]
        return RedfishConfigClient(ctx, transport=httpx.MockTransport(self.fake.handler))


def build(db, cipher):  # type: ignore[no-untyped-def]
    a = make_asset(db, cipher, name="fake-r740", address="192.0.2.40")
    rf = create_credential(db, cipher, alias=f"rf-{uuid.uuid4()}", kind="password", username="root", secret=FAKE_SECRET)
    db.add(ManagementEndpoint(asset_id=a.id, address="192.0.2.40", port=443, protocol="redfish", purpose="management", credential_id=rf.id, tls_verify=False))
    snmp = create_credential(db, cipher, alias=f"snmp-{uuid.uuid4()}", kind="snmp_community", username="", secret="fake-community")
    v3 = create_credential(db, cipher, alias=f"v3-{uuid.uuid4()}", kind="snmpv3", username="fake-snmp", secret="{}")
    db.commit()
    return a.id, snmp.id, v3.id


def request(client, ids, action, credential_id=None):  # type: ignore[no-untyped-def]
    return client.post("/api/control/bmc-snmp", json={"asset_ids": [str(i) for i in ids], "action": action, "confirm": "",
                                                      "credential_id": str(credential_id) if credential_id else None,
                                                      "reason": "验收测试", "request_key": uuid.uuid4().hex})


def worker(app_factory, factory, fleet: Fleet):  # type: ignore[no-untyped-def]
    from app.control.service import make_bmc_snmp_handler
    from app.worker import Worker

    return Worker(factory, {BMC_SNMP_TASK: make_bmc_snmp_handler(lambda: app_factory.cipher, fleet.factory)})


def test_push_actions_share_the_snmp_gate_and_store_only_hashes(app_factory, factory, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("YUNSHU_BMC_SNMP", "1")
    add_user(factory, "ada", "admin")
    admin = login(app_factory(), "ada")
    with factory() as db:
        set_owner(db, "new")
        aid, cid, v3 = build(db, app_factory.cipher)
    info = admin.get(f"/api/control/assets/{aid}/bmc-snmp").json()
    codes = {a["key"]: a.get("code") for a in info["actions"]}
    assert codes["trap_on"] == "trap_disabled" and codes["events_on"] == "events_disabled"
    assert info["push"]["trap"]["enabled"] is False and info["push"]["events"]["subscription"] is None

    monkeypatch.setenv("YUNSHU_SNMP_TRAP", "1")
    monkeypatch.setenv("YUNSHU_SNMP_TRAP_TARGET", "192.0.2.10")
    monkeypatch.setenv("YUNSHU_BMC_EVENTS_URL", "http://yunshu.example.test")  # 不是 HTTPS：视为没设置
    assert {a["key"]: a["allowed"] for a in admin.get(f"/api/control/assets/{aid}/bmc-snmp").json()["actions"]}["events_on"] is False
    monkeypatch.setenv("YUNSHU_BMC_EVENTS_URL", "https://yunshu.example.test/")
    assert request(admin, [aid], "trap_on", v3).json()["operations"][0]["message"].endswith("请选择团体名凭据")

    fleet = Fleet(FakeEventBmc(dell_with_slots()))
    assert request(admin, [aid], "trap_on", cid).status_code == 202
    assert worker(app_factory, factory, fleet).run_once() == "succeeded"
    assert request(admin, [aid], "events_on").status_code == 202
    assert worker(app_factory, factory, fleet).run_once() == "succeeded"
    with factory() as db:
        subs = {s.kind: s for s in db.scalars(select(BmcEventSubscription).where(BmcEventSubscription.asset_id == aid))}
        assert subs["snmp_trap"].destination == "192.0.2.10" and subs["snmp_trap"].token_hash == hashlib.sha256(b"fake-community").hexdigest()
        assert subs["redfish_event"].destination == "https://yunshu.example.test/api/bmc-events/" and subs["redfish_event"].remote_id == f"{SUBS}/1"
    # 订阅地址里的令牌能被接收端认出来；库里和操作记录里都没有令牌原文
    dest = fleet.fake.posts[0]["Destination"]
    token = dest.rsplit("/", 1)[1]
    r = app_factory().post(f"/api/bmc-events/{token}", json={"Events": [{"EventId": "1", "MessageSeverity": "Warning", "Message": "fake: fan degraded"}]})
    assert r.json() == {"accepted": 1}
    ops = admin.get("/api/control/operations").json()
    assert token not in json.dumps(ops) and "fake-community" not in json.dumps(ops)
    push_info = admin.get(f"/api/control/assets/{aid}/bmc-snmp").json()["push"]
    assert push_info["events"]["subscription"]["events_received"] == 1 and push_info["trap"]["subscription"]["destination"] == "192.0.2.10"

    assert request(admin, [aid], "events_off").status_code == 202
    assert worker(app_factory, factory, fleet).run_once() == "succeeded"
    with factory() as db:
        assert db.scalar(select(BmcEventSubscription).where(BmcEventSubscription.asset_id == aid, BmcEventSubscription.kind == "redfish_event")) is None
    assert app_factory().post(f"/api/bmc-events/{token}", json={"Events": []}).status_code == 404


# ---- 严重事件直接通知 ----


def test_open_critical_events_alert_remind_on_new_ones_and_recover_when_handled(session: Session) -> None:
    from app.alert_defaults import ensure_rules
    from sqlalchemy import func

    from app.alerting import evaluate
    from app.crypto import CredentialCipher, KeyRing
    from app.models import Alert, NotificationChannel, NotificationDelivery, NotificationIntent
    from app.notification_queue import plan_notifications

    ensure_rules(session)
    a = make_asset(session, CredentialCipher(KeyRing.generate()), name="fake-r750", address="192.0.2.44")
    session.add(NotificationChannel(name="fake-email", kind="email", enabled=True, config={"group_seconds": 0}))
    session.commit()
    rule = session.scalar(select(AlertRule).where(AlertRule.legacy_id == "server_event_critical"))
    server_events.record(session, a.id, "redfish_event", [{"id": "1", "created": NOW, "severity": "Critical", "message": "fake: PSU 1 lost AC"}], NOW)
    server_events.record(session, a.id, "redfish_event", [{"id": "2", "created": NOW, "severity": "Warning", "message": "fake: fan slow"}], NOW)
    evaluate(session, now=NOW + timedelta(seconds=15))
    alert = session.scalar(select(Alert).where(Alert.rule_id == rule.id, Alert.asset_id == a.id))
    trig = session.scalar(select(AlertEvent).where(AlertEvent.alert_id == alert.id, AlertEvent.kind == "trigger"))
    assert alert.state == "active" and alert.value == 1 and trig.notify and "PSU 1 lost AC" in trig.message
    evaluate(session, now=NOW + timedelta(seconds=90))
    assert session.scalar(select(AlertEvent).where(AlertEvent.alert_id == alert.id, AlertEvent.kind == "repeat")) is None  # 没有新事件不提醒

    alert.state, alert.acknowledged_by = "acknowledged", "fake-admin"  # 已确认的告警，来了新的严重事件仍提醒
    later = NOW + timedelta(minutes=5)
    server_events.record(session, a.id, "snmp_trap", [{"id": "3", "created": later, "severity": "critical", "message": "fake: DIMM A1 uncorrectable ECC"}], later)
    evaluate(session, now=later + timedelta(seconds=15))
    rep = session.scalar(select(AlertEvent).where(AlertEvent.alert_id == alert.id, AlertEvent.kind == "repeat"))
    assert rep is not None and rep.notify and "DIMM A1" in rep.message and "PSU" not in rep.message
    # 要真正排进投递（以前在投递规划里被“已确认不再通知”拦掉）
    plan_notifications(session, now=later + timedelta(seconds=15))
    intent = session.scalar(select(NotificationIntent).where(NotificationIntent.event_id == rep.id))
    assert intent is not None and session.get(NotificationDelivery, intent.delivery_id).status == "queued"
    # 已确认的告警不按重复提醒间隔再提醒，只在来新事件时提醒
    rule.repeat_seconds = 60
    evaluate(session, now=later + timedelta(minutes=3))
    assert session.scalar(select(func.count()).select_from(AlertEvent).where(AlertEvent.alert_id == alert.id, AlertEvent.kind == "repeat")) == 1

    for e in session.scalars(select(ServerEvent).where(ServerEvent.asset_id == a.id)):
        e.state = "handled"
    evaluate(session, now=later + timedelta(minutes=5))
    assert alert.state == "resolved"
    assert session.scalar(select(AlertEvent).where(AlertEvent.alert_id == alert.id, AlertEvent.kind == "recover")).notify


def test_new_rule_replaces_old_sel_rule_keeping_its_scope(session: Session) -> None:
    from app.alert_defaults import ensure_rules

    old = AlertRule(name="BMC 日志出现严重事件", metric_key="sel.critical_24h", threshold=1, recovery_threshold=0, severity="critical",
                    legacy_id="bmc_sel_critical", tags=["fake-rack"], channel_ids=[str(uuid.uuid4())])
    session.add(old)
    session.commit()
    ensure_rules(session)
    new = session.scalar(select(AlertRule).where(AlertRule.legacy_id == "server_event_critical"))
    assert old.enabled is False and new.enabled and new.tags == ["fake-rack"] and new.channel_ids == old.channel_ids



def test_first_seed_with_legacy_alert_settings_still_migrates_them(session: Session) -> None:
    """回归：首次写默认规则时同时有旧版告警设置待迁入（预览走查发现变量重名导致启动失败）。"""
    from app.alert_defaults import ensure_rules
    from app.models import LegacyMapping

    session.add(LegacyMapping(source="yunshu-3.x", legacy_kind="alert_settings", legacy_id="global", target_type="pending",
                              payload={"server_cpu_threshold": "91"}))
    session.commit()
    ensure_rules(session)
    mapping = session.scalar(select(LegacyMapping).where(LegacyMapping.legacy_kind == "alert_settings"))
    assert mapping.target_type == "alert_rules"
    assert session.scalar(select(AlertRule).where(AlertRule.legacy_id == "server_event_critical")).enabled
