"""PVE 与 ESXi 采集测试，全部伪造数据。

PVE 用 httpx 的 MockTransport 模拟 REST 接口；ESXi 没有可用的模拟器，只测数据映射（需要实机验证）。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.collect.base import AdapterError, EndpointContext, Trust
from app.collect.esxi import EsxiAdapter
from app.collect.pve import PveAdapter

TOKEN_USER, TOKEN = "monitor@pve!yunshu", "fake-token-0000"


def pts(r) -> dict:  # type: ignore[no-untyped-def]
    return {p.key: p for p in r.points}


class FakePve:
    def __init__(self, nodes: list[dict[str, Any]] | None = None) -> None:
        self.nodes = nodes or [{"node": "pve1", "status": "online"}]
        self.methods: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.methods.append(req.method)
        path = req.url.path.removeprefix("/api2/json")
        if req.method == "POST" and path == "/access/ticket":
            form = dict(x.split("=") for x in req.content.decode().split("&"))
            if form == {"username": "root%40pam", "password": "fake-pw"}:
                return httpx.Response(200, json={"data": {"ticket": "PVE:fake-ticket"}})
            return httpx.Response(401)
        authed = req.headers.get("Authorization") == f"PVEAPIToken={TOKEN_USER}={TOKEN}" or "PVEAuthCookie=PVE:fake-ticket" in req.headers.get("cookie", "")
        if not authed:
            return httpx.Response(401)
        data: Any = {
            "/version": {"version": "8.2.4"},
            "/nodes": self.nodes,
            "/nodes/pve1/status": {"cpu": 0.125, "memory": {"used": 32 * 2**30, "total": 128 * 2**30}, "loadavg": ["0.80", "0.70", "0.60"],
                                   "uptime": 864000, "rootfs": {"used": 20 * 2**30, "total": 100 * 2**30}, "cpuinfo": {"model": "Fake Xeon"}, "kversion": "6.8"},
            "/nodes/pve1/storage": [{"storage": "local-lvm", "total": 1000, "used": 400, "active": 1}, {"storage": "nas", "total": 0, "used": 0, "active": 0}],
            "/cluster/resources": [
                {"vmid": 100, "name": "ubuntu-app", "type": "qemu", "status": "running", "cpu": 0.05, "mem": 2 * 2**30, "maxmem": 8 * 2**30, "node": "pve1"},
                {"vmid": 101, "name": "openwrt", "type": "qemu", "status": "stopped", "node": "pve1"},
                {"vmid": 200, "name": "debian-ct", "type": "lxc", "status": "running", "cpu": 0.01, "mem": 2**28, "maxmem": 2**30, "node": "pve1"},
            ],
        }.get(path)
        return httpx.Response(200, json={"data": data}) if data is not None else httpx.Response(404)


def pve(fake: FakePve, fp: str = "AA") -> PveAdapter:
    return PveAdapter(transport=httpx.MockTransport(fake), fingerprint=lambda a, p, t: fp)


def test_pve_with_api_token() -> None:
    fake = FakePve()
    c = EndpointContext("192.0.2.60", 8006, "pve-api", username=TOKEN_USER, secret=TOKEN, credential_kind="token")
    r = pve(fake).collect(c)
    p = pts(r)
    assert p["cpu.load"].value == 12.5 and p["mem.used_pct"].value == 25.0 and p["disk.used_pct:/"].value == 20.0
    assert p["load.1m"].value == 0.8 and p["system.uptime"].value == 864000
    assert p["storage.used_pct:local-lvm"].value == 40.0 and "storage.used_pct:nas" not in p
    assert p["vms.total"].value == 3 and p["vms.running"].value == 2
    assert p["guest.up:101-openwrt"].value == 0 and p["guest.mem_pct:100-ubuntu-app"].value == 25.0
    assert r.facts["pve_version"] == "8.2.4" and len(r.facts["guests"]) == 3
    assert set(fake.methods) == {"GET"}  # 令牌方式全程只读
    assert c.trust.observed_tls == "AA"


def test_pve_with_password_ticket() -> None:
    c = EndpointContext("192.0.2.60", 8006, "pve-api", username="root@pam", secret="fake-pw", credential_kind="password")
    assert pts(pve(FakePve()).collect(c))["vms.running"].value == 2
    bad = EndpointContext("192.0.2.60", 8006, "pve-api", username="root@pam", secret="wrong", credential_kind="password")
    with pytest.raises(AdapterError) as e:
        pve(FakePve()).collect(bad)
    assert e.value.kind == "auth"


def test_pve_cluster_suffixes_and_offline_node() -> None:
    fake = FakePve(nodes=[{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "offline"}])
    c = EndpointContext("192.0.2.60", 8006, "pve-api", username=TOKEN_USER, secret=TOKEN, credential_kind="token")
    p = pts(pve(fake).collect(c))
    assert p["pve.node_up:pve2"].value == 0 and p["cpu.load:pve1"].value == 12.5


def test_pve_fingerprint_mismatch() -> None:
    c = EndpointContext("192.0.2.60", 8006, "pve-api", username=TOKEN_USER, secret=TOKEN, credential_kind="token", trust=Trust(tls_fingerprint="AA"))
    fake = FakePve()
    with pytest.raises(AdapterError) as e:
        pve(fake, fp="BB").collect(c)
    assert e.value.kind == "untrusted" and fake.methods == []


class FakeVsphere:
    def __init__(self, hosts: list[dict[str, Any]]) -> None:
        self._hosts = hosts
        self.closed = False

    def about(self) -> dict[str, Any]:
        return {"product": "VMware ESXi 8.0.2", "api_type": "HostAgent", "version": "8.0.2"}

    def hosts(self) -> list[dict[str, Any]]:
        return self._hosts

    def datastores(self) -> list[dict[str, Any]]:
        return [{"name": "datastore1", "capacity": 1000, "free": 250, "accessible": True}, {"name": "nfs", "capacity": 0, "free": 0, "accessible": False}]

    def vms(self) -> list[dict[str, Any]]:
        return [{"name": "vm-a", "power": "poweredOn"}, {"name": "vm-b", "power": "poweredOff"}]

    def close(self) -> None:
        self.closed = True


HOST = {"name": "esxi1", "cpu_used_mhz": 4800, "cpu_total_mhz": 48000, "mem_used_bytes": 64 * 2**30, "mem_total_bytes": 256 * 2**30,
        "uptime": 3600, "power": "poweredOn", "connection": "connected", "model": "PowerEdge R740", "vendor": "Dell Inc."}


def test_esxi_mapping() -> None:
    fake = FakeVsphere([HOST])
    a = EsxiAdapter(client_factory=lambda c: fake, fingerprint=lambda a, p, t: "CC")
    r = a.collect(EndpointContext("192.0.2.70", 443, "esxi-api", username="ro", secret="fake"))
    p = pts(r)
    assert p["cpu.load"].value == 10.0 and p["mem.used_pct"].value == 25.0 and p["host.up"].value == 1
    assert p["storage.used_pct:datastore1"].value == 75.0
    assert p["storage.used_pct:nfs"].quality == "failed" and p["storage.used_pct:nfs"].value is None
    assert p["vms.running"].value == 1 and p["guest.up:vm-b"].value == 0
    assert r.facts["model"] == "PowerEdge R740" and fake.closed


def test_esxi_disconnected_host_in_vcenter() -> None:
    other = {**HOST, "name": "esxi2", "connection": "disconnected"}
    p = pts(EsxiAdapter(client_factory=lambda c: FakeVsphere([HOST, other]), fingerprint=lambda a, p, t: "CC").collect(
        EndpointContext("192.0.2.71", 443, "esxi-api", username="ro", secret="fake")))
    assert p["host.up:esxi2"].value == 0 and "cpu.load:esxi2" not in p and p["cpu.load:esxi1"].value == 10.0


def test_esxi_needs_credentials() -> None:
    with pytest.raises(AdapterError) as e:
        EsxiAdapter(fingerprint=lambda a, p, t: "CC").collect(EndpointContext("192.0.2.70", 443, "esxi-api"))
    assert e.value.kind == "unconfigured"


def test_pve_observation_ids_do_not_depend_on_guest_names_or_host_node() -> None:
    fake = FakePve()
    ctx = EndpointContext("192.0.2.60", 8006, "pve-api", username=TOKEN_USER, secret=TOKEN, credential_kind="token")
    snapshot = pve(fake).collect(ctx).facts["virtualization"]
    assert snapshot["platform"] == "pve" and snapshot["complete"] is True
    assert snapshot["hosts"] == [{"external_id": "pve1", "name": "pve1", "connection": "online"}]
    assert [(g["kind"], g["external_id"], g["host_external_id"], g["power"]) for g in snapshot["guests"]] == [
        ("vm", "100", "pve1", "on"), ("vm", "101", "pve1", "off"), ("container", "200", "pve1", "on"),
    ]
    assert all(method == "GET" for method in fake.methods)


def test_vmware_observations_keep_duplicate_names_and_unknown_power() -> None:
    class IdentifiedVsphere(FakeVsphere):
        def about(self):
            return {**super().about(), "instance_uuid": "fake-service-instance"}

        def vms(self):
            return [
                {"name": "同名虚拟机", "external_id": "uuid:fake-vm-a", "host_external_id": "host-1", "power": "poweredOn", "connection": "connected"},
                {"name": "同名虚拟机", "external_id": "uuid:fake-vm-b", "host_external_id": "host-2", "power": "poweredOff", "connection": "connected"},
                {"name": "假失联", "external_id": "moref:vm-3", "host_external_id": "host-1", "power": "poweredOn", "connection": "disconnected"},
                {"name": "假挂起", "external_id": "moref:vm-4", "host_external_id": "", "power": "suspended", "connection": "connected"},
            ]
    fake = IdentifiedVsphere([{**HOST, "external_id": "host-1"}])
    snapshot = EsxiAdapter(client_factory=lambda _: fake, fingerprint=lambda *_: "CC").collect(EndpointContext("192.0.2.70", 443, "esxi-api")).facts["virtualization"]
    assert snapshot["instance_uuid"] == "fake-service-instance" and snapshot["hosts"][0]["external_id"] == "host-1"
    assert [g["power"] for g in snapshot["guests"]] == ["on", "off", "unknown", "unknown"]
    assert len({g["external_id"] for g in snapshot["guests"]}) == 4 and fake.closed


def test_pyvmomi_maps_native_vm_and_runtime_host_references() -> None:
    from types import SimpleNamespace as NS
    from app.collect.esxi import PyvmomiClient

    client = PyvmomiClient.__new__(PyvmomiClient)
    client.vim = NS(VirtualMachine=object())
    client._view = lambda _: [
        NS(name="同名", _moId="vm-1", config=NS(instanceUuid="fake-instance-a"), runtime=NS(powerState="poweredOn", connectionState="connected", host=NS(_moId="host-1"))),
        NS(name="同名", _moId="vm-2", config=None, runtime=NS(powerState="poweredOff", connectionState="connected", host=None)),
    ]
    result = client.vms()
    assert result[0]["external_id"] == "uuid:fake-instance-a" and result[0]["host_external_id"] == "host-1"
    assert result[1]["external_id"] == "moref:vm-2" and result[1]["host_external_id"] == ""
    assert client.inventory_complete is True


def test_vmware_native_property_reads_remain_bounded_when_inventory_is_truncated() -> None:
    from types import SimpleNamespace as NS
    from app.collect.esxi import MAX_VMS, PyvmomiClient

    item = NS(name="fake-vm", _moId="vm-1", config=None, runtime=NS(powerState="poweredOn", connectionState="connected", host=None))
    client = PyvmomiClient.__new__(PyvmomiClient)
    client.vim = NS(VirtualMachine=object())
    # 超出上限的引用不能读取其属性。
    client._view = lambda _: [item] * MAX_VMS + [object()]
    assert len(client.vms()) == MAX_VMS and client.inventory_complete is False
