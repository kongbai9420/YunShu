from __future__ import annotations

from types import SimpleNamespace
from subprocess import CompletedProcess

import httpx
import pytest
from sqlalchemy import func, select

from app.api.routes_asset_information import candidate_fields
from app.collect.base import AdapterError, EndpointContext, ProbeResult
from app.collect.esxi import EsxiAdapter
from app.collect.ipmi import IpmiAdapter, IpmitoolClient, PyghmiClient
from app.collect.pve import PveAdapter
from app.collect.redfish import RedfishAdapter
from app.collect.snmp import SYS_DESCR, SYS_NAME, SnmpAdapter
from app.collect.ssh import INVENTORY_COMMAND, MARK, SshAdapter
from app.models import Asset, AuditEvent, Credential, ManagementEndpoint
from tests.api_helpers import add_user, login

EP = {"protocol": "ssh", "address": "192.0.2.42", "new_credential": {
    "alias": "fake-inventory", "username": "fakeuser", "kind": "password", "secret": "fake-inventory-password"}}
FACTS = {"hostname": "fake-device", "os_id": "debian", "vendor": "FakeVendor", "model": "FakeModel", "serial": "FAKE-SERIAL"}


@pytest.fixture
def operator(app_factory, factory):
    add_user(factory, "fake-editor", "operator")
    return login(app_factory(), "fake-editor")


class Adapter:
    def __init__(self):
        self.contexts = []
        self.failure = None

    def probe(self, ctx):
        self.contexts.append(ctx)
        ctx.trust.check_ssh("ssh-ed25519 AAAAfake-inventory")
        if self.failure:
            raise self.failure
        return ProbeResult(True, facts=FACTS)


@pytest.fixture
def adapter(operator):
    instance = Adapter()
    operator.app.state.information_adapters = {p: instance for p in ("ssh", "snmp", "ipmi", "redfish", "pve-api", "esxi-api")}
    return instance


def test_preview_before_create_never_persists_asset_or_secret(operator, adapter, factory):
    response = operator.post("/api/asset-information/preview", json={"endpoint": EP})
    assert response.status_code == 200, response.text
    info = response.json()
    assert info["fields"] == {"name": "fake-device", "platform": "debian", "vendor": "FakeVendor", "model": "FakeModel", "serial_no": "FAKE-SERIAL"}
    assert info["ssh_host_key"] == "ssh-ed25519 AAAAfake-inventory"
    assert adapter.contexts[0].secret == EP["new_credential"]["secret"]
    with factory() as db:
        for entity in (Asset, Credential, ManagementEndpoint):
            assert db.scalar(select(func.count()).select_from(entity)) == 0
        audit = db.scalars(select(AuditEvent)).all()
        assert EP["new_credential"]["secret"] not in str([event.detail for event in audit])
    # 只采用型号和序列号，其余保持用户填写；获取与取消本身不建资产。
    body = {"name": "手工名称", "form": "physical", "role": "server", "vendor": "手工品牌",
            "model": info["fields"]["model"], "serial_no": info["fields"]["serial_no"],
            "endpoints": [{**EP, "ssh_host_key": info["ssh_host_key"]}]}
    asset = operator.post("/api/assets", json=body).json()
    assert (asset["name"], asset["vendor"], asset["model"], asset["serial_no"]) == ("手工名称", "手工品牌", "FakeModel", "FAKE-SERIAL")
    assert asset["endpoints"][0]["trust_pinned_at"]


def test_existing_credential_preview_and_manual_patch(operator, adapter):
    asset = operator.post("/api/assets", json={"name": "原名", "form": "physical", "role": "server", "vendor": "原品牌", "endpoints": [EP]}).json()
    ep_id = asset["endpoints"][0]["id"]
    info = operator.post("/api/asset-information/preview", json={"endpoint_id": ep_id}).json()
    before = operator.get(f"/api/assets/{asset['id']}").json()
    assert before["revision"] == 1 and before["vendor"] == "原品牌" and before["model"] == ""
    updated = operator.patch(f"/api/assets/{asset['id']}", json={"revision": 1, "model": info["fields"]["model"]}).json()
    assert updated["vendor"] == "原品牌" and updated["name"] == "原名" and updated["model"] == "FakeModel"
    assert operator.patch(f"/api/assets/{asset['id']}", json={"revision": 1, "name": "stale"}).status_code == 409


def test_existing_pin_change_rejected_without_replacing(operator, adapter):
    asset = operator.post("/api/assets", json={"name": "原名", "form": "physical", "role": "server", "endpoints": [{**EP, "ssh_host_key": "ssh-ed25519 AAAAprevious"}]}).json()
    response = operator.post("/api/asset-information/preview", json={"endpoint_id": asset["endpoints"][0]["id"]})
    assert response.status_code == 422 and "指纹" in response.text
    assert operator.get(f"/api/assets/{asset['id']}").json()["endpoints"][0]["ssh_host_key"] == "ssh-ed25519 AAAAprevious"


@pytest.mark.parametrize("kind", ["auth", "timeout", "unreachable", "protocol", "unconfigured", "unsupported"])
def test_failures_keep_input_secret_out_of_response(operator, adapter, kind):
    adapter.failure = AdapterError(kind, EP["new_credential"]["secret"])
    result = operator.post("/api/asset-information/preview", json={"endpoint": EP})
    assert result.status_code == 422 and EP["new_credential"]["secret"] not in result.text


def test_permissions_validation_and_no_connection_without_credentials(operator, adapter, app_factory, factory):
    assert operator.post("/api/asset-information/preview", json={}).status_code == 422
    assert operator.post("/api/asset-information/preview", json={"endpoint": EP, "endpoint_id": "fake"}).status_code == 422
    for value in ({"protocol": "http", "address": "192.0.2.42"}, {"protocol": "ssh", "address": "192.0.2.42"}, {**EP, "address": "-fake"}):
        assert operator.post("/api/asset-information/preview", json={"endpoint": value}).status_code == 422
    assert adapter.contexts == []
    add_user(factory, "fake-viewer", "viewer")
    viewer = login(app_factory(), "fake-viewer")
    assert viewer.post("/api/asset-information/preview", json={"endpoint": EP}).status_code == 403
    operator.headers.pop("X-CSRF-Token")
    assert operator.post("/api/asset-information/preview", json={"endpoint": EP}).status_code == 403


def test_only_use_real_nonempty_bounded_fields():
    assert candidate_fields({**FACTS, "secret": "fake", "model": "To Be Filled By O.E.M.", "serial": None, "os_id": "x" * 65}, "fake-device") == {"vendor": "FakeVendor"}


def test_ssh_inventory_fixed_read_only_and_missing_dmi():
    outputs = {"hostname": "fake-linux", "os": 'ID=ubuntu\nPRETTY_NAME="Ubuntu fake"', "vendor": "Fake Vendor", "model": "Fake Model", "serial": "FAKE-SN"}
    closed = []
    def run(command):
        assert command == INVENTORY_COMMAND
        assert not any(word in command.replace("2>/dev/null", "") for word in ("sudo", "reboot", "shutdown", " >"))
        return "\n".join(f"{MARK}{key}\n{value}" for key, value in outputs.items())
    adapter = SshAdapter(lambda ctx: SimpleNamespace(run=run, close=lambda: closed.append(True)))
    info = adapter.inventory(EndpointContext("192.0.2.42", 22, "ssh"))
    assert info.facts["serial"] == "FAKE-SN" and info.facts["os_id"] == "ubuntu" and closed
    outputs["serial"] = outputs["model"] = ""
    assert candidate_fields(adapter.inventory(EndpointContext("192.0.2.42", 22, "ssh")).facts, "") == {"name": "fake-linux", "platform": "ubuntu", "vendor": "Fake Vendor"}


def test_snmp_entity_only_chassis():
    base = "1.3.6.1.2.1.47.1.1.1.1"
    class Client:
        def get(self, oids):
            return {SYS_NAME: "fake-switch", SYS_DESCR: "description", **{base + f".{column}.4": value for column, value in ((11, "FAKE-SN"), (12, "Fake Vendor"), (13, "FakeModel"))}}
        def walk(self, oid): return {base + ".5.4": 3, base + ".5.5": 6}
        def close(self): pass
    facts = SnmpAdapter(lambda ctx: Client()).inventory(EndpointContext("192.0.2.42", 161, "snmp")).facts
    assert facts == {"sys_name": "fake-switch", "serial": "FAKE-SN", "vendor": "Fake Vendor", "model": "FakeModel"}


def test_ipmi_fru_native_and_fallback(monkeypatch):
    closed = []
    client = PyghmiClient(EndpointContext("192.0.2.42", None, "ipmi", username="fake-user", secret="fake-password"), command=SimpleNamespace(get_inventory_of_component=lambda name: {"Manufacturer": "FakeVendor", "Product name": "FakeModel", "Serial Number": "FAKE-SN"}))
    assert client.inventory() == {"vendor": "FakeVendor", "model": "FakeModel", "serial": "FAKE-SN"}
    def unavailable(ctx): raise AdapterError("unsupported", "fake unavailable")
    fallback = SimpleNamespace(inventory=lambda: client.inventory(), close=lambda: closed.append(True))
    assert IpmiAdapter(unavailable, lambda ctx: fallback).inventory(EndpointContext("192.0.2.42", None, "ipmi")).facts["model"] == "FakeModel"
    assert closed == [True]
    monkeypatch.setattr("app.collect.ipmi.shutil.which", lambda name: "fake-ipmitool")
    def run(command, **kwargs):
        assert command[-3:] == ["fru", "print", "0"]
        assert "fake-password" not in command and kwargs["env"]["IPMI_PASSWORD"] == "fake-password"
        return CompletedProcess(command, 0, "Product Manufacturer : FakeVendor\nProduct Name : FakeModel\nProduct Serial : FAKE-SN\nBoard Serial : WRONG-MODULE-SN", "")
    tool = IpmitoolClient(EndpointContext("192.0.2.42", None, "ipmi", username="fake-user", secret="fake-password"), runner=run)
    assert tool.inventory() == {"vendor": "FakeVendor", "model": "FakeModel", "serial": "FAKE-SN"}


def test_redfish_pve_and_vmware_inventory():
    def redfish(req):
        if req.method == "POST": return httpx.Response(201, headers={"X-Auth-Token": "fake-token"})
        if req.url.path.endswith("/Systems"): return httpx.Response(200, json={"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]})
        return httpx.Response(200, json={"HostName": "fake-device", "Manufacturer": "FakeVendor", "Model": "FakeModel", "SerialNumber": "FAKE-SN"})
    ctx = EndpointContext("192.0.2.42", None, "redfish", username="fake", secret="fake-password")
    assert RedfishAdapter(httpx.MockTransport(redfish), lambda *args: "fake-fp").inventory(ctx).facts["serial"] == "FAKE-SN"
    nodes = [{"node": "fake-pve"}]
    def pve(req): return httpx.Response(200, json={"data": nodes if req.url.path.endswith("/nodes") else {"version": "fake"}})
    ctx.protocol, ctx.credential_kind = "pve-api", "token"
    adapter = PveAdapter(httpx.MockTransport(pve), lambda *args: "fake-fp")
    assert adapter.inventory(ctx).facts["hostname"] == "fake-pve"
    nodes.append({"node": "fake-other"})
    assert adapter.inventory(ctx).facts["hostname"] is None
    vc = SimpleNamespace(about=lambda: {"api_type": "VirtualCenter"}, hosts=lambda: pytest.fail("不能把 vCenter 主机当成当前设备"), close=lambda: None)
    assert EsxiAdapter(lambda ctx: vc, lambda *args: "fake-fp").inventory(ctx).facts == {"platform": "vcenter"}
