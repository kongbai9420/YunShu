"""4.4 P19 一键开启 BMC 的 SNMP：只用伪造的 Redfish 接口和伪造的 SNMP 读取，不连接任何设备。"""

from __future__ import annotations

import copy
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.collect.base import AdapterError, EndpointContext
from app.control.base import ControlError
from app.control.bmc_snmp import BMC_SNMP_TASK, RedfishConfigClient, SnmpCredential, execute_bmc_snmp
from app.models import ControlOperation, ManagementEndpoint
from app.services.credentials import create_credential
from tests.api_helpers import add_user, login
from tests.test_control_power import FAKE_SECRET, make_asset, set_owner, setup  # noqa: F401  （setup 是 fixture）

V2C = SnmpCredential("v2c", community="fake-community")
V3 = SnmpCredential("v3", user="fake-snmp")

DELL = {
    "/redfish/v1/": {"Oem": {"Dell": {}}, "Vendor": "Dell"},
    "/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/iDRAC.Embedded.1"}]},
    "/redfish/v1/Managers/iDRAC.Embedded.1": {"Id": "iDRAC.Embedded.1"},
    "/redfish/v1/Managers/iDRAC.Embedded.1/Attributes": {"Attributes": {
        "SNMP.1.AgentEnable": "Disabled", "SNMP.1.AgentCommunity": "", "SNMP.1.SNMPProtocol": "All",
        "Users.2.UserName": "root", "Users.3.UserName": "fake-snmp", "Users.3.SNMPv3Enable": "Disabled",
        "Users.3.SNMPv3AuthenticationType": "MD5", "Users.3.SNMPv3PrivacyType": "DES",
    }},
}
NP = "/redfish/v1/Managers/1/NetworkProtocol"
STANDARD = {
    "/redfish/v1/": {"Oem": {"Hpe": {}}, "Vendor": "HPE"},
    "/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
    "/redfish/v1/Managers/1": {"Id": "1", "NetworkProtocol": {"@odata.id": NP}},
    NP: {"SNMP": {"ProtocolEnabled": False, "EnableSNMPv2c": False, "EnableSNMPv3": False,
                  "CommunityStrings": [{"CommunityString": "", "AccessMode": "Limited"}]}},
}
NO_SNMP = {
    "/redfish/v1/": {"Vendor": "H3C"},
    "/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
    "/redfish/v1/Managers/1": {"Id": "1", "NetworkProtocol": {"@odata.id": NP}},
    NP: {"HTTPS": {"ProtocolEnabled": True}},
}


class FakeRedfish:
    """记录每个请求；PATCH 按 Redfish 语义合并（数组里的 {} 表示该条不改）。apply=False 模拟需要重启才生效。"""

    def __init__(self, tree: dict, *, apply: bool = True, deny_patch: bool = False, hang: bool = False) -> None:
        self.tree = copy.deepcopy(tree)
        self.apply, self.deny_patch, self.hang = apply, deny_patch, hang
        self.calls: list[str] = []
        self.patches: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if request.method == "POST" and path.endswith("/Sessions"):
            return httpx.Response(201, headers={"X-Auth-Token": "fake-token", "Location": "/redfish/v1/SessionService/Sessions/1"})
        if request.headers.get("X-Auth-Token") != "fake-token":
            return httpx.Response(401)
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "PATCH":
            if self.hang:
                raise httpx.ReadTimeout("fake timeout", request=request)
            if self.deny_patch:
                return httpx.Response(400, json={"error": {"@Message.ExtendedInfo": [{"Message": "fake: property is read only"}]}})
            body = json.loads(request.content)
            self.patches.append(body)
            if self.apply:
                self._merge(self.tree[path], body)
            return httpx.Response(200, json={})
        if path in self.tree:
            return httpx.Response(200, json=self.tree[path], headers={"ETag": 'W/"fake-1"'})
        return httpx.Response(404)

    @staticmethod
    def _merge(target: dict, body: dict) -> None:
        for k, v in body.items():
            if isinstance(v, dict) and isinstance(target.get(k), dict):
                FakeRedfish._merge(target[k], v)
            elif isinstance(v, list) and isinstance(target.get(k), list):
                target[k] = [old if new == {} else new for old, new in zip(target[k], v)] + v[len(target[k]):]
            else:
                target[k] = v


def rf_ctx() -> EndpointContext:
    return EndpointContext(address="192.0.2.40", port=443, protocol="redfish", username="root", secret=FAKE_SECRET, tls_verify=False)


def snmp_ctx() -> EndpointContext:
    return EndpointContext(address="192.0.2.40", port=None, protocol="snmp", secret="fake-community", credential_kind="snmp_community")


def run(fake: FakeRedfish, action: str = "enable", cred: SnmpCredential | None = V2C, probe=lambda c: "fake-idrac"):  # type: ignore[no-untyped-def]
    return execute_bmc_snmp(rf_ctx(), action, cred=cred, snmp_ctx=snmp_ctx(),
                            factory=lambda c: RedfishConfigClient(c, transport=httpx.MockTransport(fake.handler)), probe=probe)


# ---- 识别与修改 ----


def test_dell_enable_sets_agent_and_community_then_reads_back() -> None:
    fake = FakeRedfish(DELL)
    out = run(fake)
    assert out.status == "verified" and "fake-idrac" in out.message
    assert fake.patches == [{"Attributes": {"SNMP.1.AgentEnable": "Enabled", "SNMP.1.AgentCommunity": "fake-community"}}]
    assert out.observed["before"] is False and out.observed["after"] is True and out.observed["style"] == "dell"
    assert "fake-community" not in json.dumps(out.observed)  # 操作记录里不留团体名
    assert fake.calls[-1] == "DELETE /redfish/v1/SessionService/Sessions/1"  # 会话用完即删


def test_dell_does_not_overwrite_a_community_already_in_use() -> None:
    tree = copy.deepcopy(DELL)
    tree["/redfish/v1/Managers/iDRAC.Embedded.1/Attributes"]["Attributes"].update({"SNMP.1.AgentEnable": "Enabled", "SNMP.1.AgentCommunity": "other"})
    fake = FakeRedfish(tree)
    with pytest.raises(ControlError) as exc:
        run(fake)
    assert exc.value.kind == "rejected" and fake.patches == []


def test_dell_v3_enables_the_matching_idrac_user() -> None:
    fake = FakeRedfish(DELL)
    out = run(fake, cred=SnmpCredential("v3", user="fake-snmp", auth_protocol="SHA", priv_protocol="AES"))
    assert out.status == "verified"
    assert fake.patches[0]["Attributes"] == {"SNMP.1.AgentEnable": "Enabled", "Users.3.SNMPv3Enable": "Enabled",
                                            "Users.3.SNMPv3AuthenticationType": "SHA", "Users.3.SNMPv3PrivacyType": "AES"}
    with pytest.raises(ControlError) as exc:
        run(FakeRedfish(DELL), cred=SnmpCredential("v3", user="nobody"))
    assert "nobody" in str(exc.value)


def test_standard_enable_appends_community_and_flags_ilo_reset() -> None:
    fake = FakeRedfish(STANDARD)
    out = run(fake)
    assert fake.patches == [{"SNMP": {"ProtocolEnabled": True, "EnableSNMPv2c": True,
                                      "CommunityStrings": [{}, {"CommunityString": "fake-community", "AccessMode": "Limited"}]}}]
    assert out.status == "verified" and len(fake.tree[NP]["SNMP"]["CommunityStrings"]) == 2
    pending = run(FakeRedfish(STANDARD, apply=False))
    assert pending.status == "accepted" and "重启管理芯片" in pending.message


def test_unsupported_bmc_is_refused_without_any_write() -> None:
    fake = FakeRedfish(NO_SNMP)
    with pytest.raises(ControlError) as exc:
        run(fake)
    assert exc.value.kind == "unsupported" and "手动开启" in str(exc.value)
    assert not any(c.startswith("PATCH") for c in fake.calls)


def test_probe_failure_after_enable_is_accepted_not_verified() -> None:
    def probe(ctx):  # type: ignore[no-untyped-def]
        raise AdapterError("timeout", "SNMP 无应答")

    out = run(FakeRedfish(DELL), probe=probe)
    assert out.status == "accepted" and "读不到数据" in out.message and out.observed["snmp_probe"] == "timeout"


def test_disable_and_errors() -> None:
    tree = copy.deepcopy(STANDARD)
    tree[NP]["SNMP"]["ProtocolEnabled"] = True
    fake = FakeRedfish(tree)
    out = run(fake, "disable", cred=None)
    assert out.status == "verified" and fake.patches == [{"SNMP": {"ProtocolEnabled": False}}]
    assert run(FakeRedfish(STANDARD), "disable", cred=None).observed["changed"] == []  # 本来就关着：不发修改
    for fake, kind in ((FakeRedfish(DELL, deny_patch=True), "protocol"), (FakeRedfish(DELL, hang=True), "timeout")):
        with pytest.raises(ControlError) as exc:
            run(fake)
        assert exc.value.kind == kind


# ---- 门禁、执行与自动添加端点 ----


class FakeFleet:
    def __init__(self, tree: dict) -> None:
        self.fake = FakeRedfish(tree)
        self.probed: list[str] = []

    def factory(self, ctx):  # type: ignore[no-untyped-def]
        assert ctx.username == "root" and ctx.secret == FAKE_SECRET
        return RedfishConfigClient(ctx, transport=httpx.MockTransport(self.fake.handler))

    def probe(self, ctx):  # type: ignore[no-untyped-def]
        assert ctx.protocol == "snmp" and ctx.secret == "fake-community"
        self.probed.append(ctx.address)
        return "fake-idrac"


def build(db, cipher, *, purpose="management"):  # type: ignore[no-untyped-def]
    a = make_asset(db, cipher, name="fake-r740", address="192.0.2.40")
    rf = create_credential(db, cipher, alias=f"rf-{uuid.uuid4()}", kind="password", username="root", secret=FAKE_SECRET)
    db.add(ManagementEndpoint(asset_id=a.id, address="192.0.2.40", port=443, protocol="redfish", purpose=purpose, credential_id=rf.id, tls_verify=False))
    snmp = create_credential(db, cipher, alias=f"snmp-{uuid.uuid4()}", kind="snmp_community", username="", secret="fake-community")
    db.commit()
    return a.id, snmp.id


def worker(app_factory, factory, fleet: FakeFleet):  # type: ignore[no-untyped-def]
    from app.control.service import make_bmc_snmp_handler
    from app.worker import Worker

    return Worker(factory, {BMC_SNMP_TASK: make_bmc_snmp_handler(lambda: app_factory.cipher, fleet.factory, fleet.probe)})


def request(client, ids, action, credential_id=None, confirm=""):  # type: ignore[no-untyped-def]
    return client.post("/api/control/bmc-snmp", json={"asset_ids": [str(i) for i in ids], "action": action, "confirm": confirm,
                                                      "credential_id": str(credential_id) if credential_id else None,
                                                      "reason": "验收测试", "request_key": uuid.uuid4().hex})


def test_disabled_by_default_and_admin_only(app_factory, factory, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        aid, cid = build(db, app_factory.cipher)
    info = op.get(f"/api/control/assets/{aid}/bmc-snmp").json()
    assert info["enabled"] is False and all(a["code"] == "disabled" for a in info["actions"])
    assert request(op, [aid], "enable", cid).status_code == 403  # 运维账号不能改设备设置


def test_enable_runs_through_redfish_and_adds_snmp_endpoint(app_factory, factory, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("YUNSHU_BMC_SNMP", "1")
    add_user(factory, "ada", "admin")
    admin = login(app_factory(), "ada")
    with factory() as db:
        set_owner(db, "new")
        aid, cid = build(db, app_factory.cipher)
    info = admin.get(f"/api/control/assets/{aid}/bmc-snmp").json()
    assert [a["key"] for a in info["actions"] if a["allowed"]] == ["enable", "disable"] and info["endpoint"]["address"] == "192.0.2.40"
    assert request(admin, [aid], "enable").json()["operations"][0]["message"].startswith("请选择")
    assert request(admin, [aid], "disable", confirm="wrong").status_code == 422  # 关闭是高风险，要输入确认语
    r = request(admin, [aid], "enable", cid)
    assert r.status_code == 202 and r.json()["operations"][0]["status"] == "queued"
    fleet = FakeFleet(DELL)
    assert worker(app_factory, factory, fleet).run_once() == "succeeded"
    assert fleet.probed == ["192.0.2.40"] and len(fleet.fake.patches) == 1
    with factory() as db:
        o = db.scalar(select(ControlOperation).where(ControlOperation.asset_id == aid, ControlOperation.status != "rejected"))
        assert o.status == "verified" and o.kind == "bmc_snmp" and o.target_key == "bmc:192.0.2.40" and "已在资产上添加" in o.message
        ep = db.scalar(select(ManagementEndpoint).where(ManagementEndpoint.asset_id == aid, ManagementEndpoint.protocol == "snmp"))
        assert ep is not None and ep.purpose == "monitoring" and ep.credential_id == cid
    # 再开一次：设置已是开启，不发修改，也不重复添加端点
    request(admin, [aid], "enable", cid)
    assert worker(app_factory, factory, fleet).run_once() == "succeeded" and len(fleet.fake.patches) == 1
    with factory() as db:
        assert len(db.scalars(select(ManagementEndpoint).where(ManagementEndpoint.asset_id == aid, ManagementEndpoint.protocol == "snmp")).all()) == 1


def test_monitoring_only_redfish_endpoint_is_not_used(app_factory, factory, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("YUNSHU_BMC_SNMP", "1")
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        aid, _ = build(db, app_factory.cipher, purpose="monitoring")
    actions = op.get(f"/api/control/assets/{aid}/bmc-snmp").json()["actions"]
    assert all(a["code"] == "no_endpoint" for a in actions)


def test_connection_timeout_is_a_plain_failure_before_any_write() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("fake handshake timeout", request=request)

    with pytest.raises(ControlError) as exc:
        RedfishConfigClient(rf_ctx(), transport=httpx.MockTransport(handler))
    assert exc.value.kind == "unreachable" and "没有修改任何设置" in str(exc.value) and "_ssl" not in str(exc.value)
