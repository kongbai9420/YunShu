"""4.3 P18b：软件清单——各协议的版本号、iDRAC 代次、发行版生态、SSH 软件包读取与每日计划。全部伪造数据。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app import inventory
from app.collect import snmp_vendors as vendors
from app.collect.base import AdapterError, EndpointContext
from app.collect.snmp import IDRAC, SnmpAdapter
from app.collect.ssh import parse_os, parse_packages
from app.crypto import CredentialCipher, KeyRing
from app.models import Asset, AssetPackage, CollectionStatus, ManagementEndpoint, PackageScan, Task
from tests.api_helpers import add_user, login
from tests.test_bmc_snmp_first import FakeIdrac

NOW = datetime(2026, 10, 19, 2, 0, tzinfo=UTC)


@pytest.mark.parametrize(("os_id", "ver", "eco"), [
    ("debian", "12", "Debian:12"), ("debian", "11.9", "Debian:11"), ("ubuntu", "22.04", "Ubuntu:22.04:LTS"),
    ("ubuntu", "23.10", "Ubuntu:23.10"), ("ubuntu", "24.04", "Ubuntu:24.04:LTS"), ("alpine", "3.19", ""), ("debian", "", ""),
])
def test_ecosystem(os_id: str, ver: str, eco: str) -> None:
    assert inventory.ecosystem(os_id, ver) == eco


@pytest.mark.parametrize(("kw", "gen"), [
    ({"name": "iDRAC9"}, 9), ({"bmc_model": "14G Monolithic"}, 9), ({"bmc_model": "13G Monolithic"}, 8),
    ({"server_model": "PowerEdge R730xd"}, 8), ({"server_model": "PowerEdge R640"}, 9), ({"server_model": "PowerEdge R770"}, 10),
    ({"firmware": "7.00.00.171"}, 9), ({"firmware": "2.83.83.83"}, None), ({}, None),
])
def test_idrac_generation(kw: dict[str, str], gen: int | None) -> None:
    assert inventory.idrac_generation(**kw) == gen


def test_parse_os_keeps_version() -> None:
    assert parse_os('PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nVERSION_ID="12"\nVERSION="12 (bookworm)"') == {
        "os": "Debian GNU/Linux 12 (bookworm)", "os_id": "debian", "os_version": "12", "os_version_text": "12 (bookworm)"}


def test_parse_packages_only_installed_and_source_names() -> None:
    out = parse_packages({"dpkg": "libssl3:amd64\t3.0.15-1~deb12u1\topenssl\t3.0.15-1~deb12u1\tii \nbash\t5.2.15-2+b7\tbash\t5.2.15-2\tii \nold\t1\told\t1\trc "})
    assert out["manager"] == "dpkg" and out["items"] == [["libssl3", "3.0.15-1~deb12u1", "openssl", "3.0.15-1~deb12u1"], ["bash", "5.2.15-2+b7", "bash", "5.2.15-2"]]
    assert parse_packages({})["manager"] == ""


def _status(asset: Asset, protocol: str, facts: dict[str, Any]) -> CollectionStatus:
    return CollectionStatus(endpoint_id=__import__("uuid").uuid4(), asset_id=asset.id, protocol=protocol, state="ok", facts=facts,
                            last_attempt_at=NOW, last_success_at=NOW)


def test_software_items_from_facts() -> None:
    dell = Asset(name="fake-r740", form="physical", role="server", vendor="Dell", model="PowerEdge R740xd")
    items = inventory.software_for(dell, [
        _status(dell, "ssh", {"os": "Debian GNU/Linux 12", "os_id": "debian", "os_version": "12"}),
        _status(dell, "pve-api", {"pve_version": "8.2.4", "kernel": "6.8.12-1-pve"}),
        _status(dell, "snmp", {"idrac": True, "idrac_name": "iDRAC9", "idrac_firmware": "7.00.00.171", "system_model": "PowerEdge R740xd"}),
        _status(dell, "redfish", {"bios": "2.19.1", "model": "PowerEdge R740xd", "bmc_firmware": "7.00.00.171", "bmc_model": "14G Monolithic"}),
    ])
    by = {i["label"]: i for i in items}
    assert by["Debian GNU/Linux 12"]["ecosystem"] == "Debian:12"
    assert by["Proxmox VE"]["cpe"] == "proxmox:virtual_environment" and by["Proxmox VE"]["version"] == "8.2.4"
    assert by["iDRAC9 固件"]["cpe"] == "dell:idrac9_firmware"
    assert [i["label"] for i in items].count("iDRAC9 固件") == 1  # SNMP 与 Redfish 报的同一固件只列一次
    assert by["BIOS"]["cpe"] == "dell:poweredge_r740xd_firmware" and by["BIOS"]["version"] == "2.19.1"
    assert by["Linux 内核"]["cpe"] == ""

    nas = Asset(name="fake-nas", form="physical", role="nas")
    items = inventory.software_for(nas, [_status(nas, "snmp", {"dsm_version": "DSM 7.2-64570 Update 3", "dsm_model": "RS1221+"}),
                                         _status(nas, "esxi-api", {"api_type": "HostAgent", "version": "8.0.2", "build": "22380479"}),
                                         _status(nas, "snmp", {"routeros_version": "7.12.1 (stable)"})])
    by = {i["label"]: i for i in items}
    assert by["群晖 DSM"]["version"] == "7.2-64570-3" and by["群晖 DSM"]["cpe"] == "synology:diskstation_manager"
    assert by["VMware ESXi"]["cpe"] == "vmware:esxi" and "22380479" in by["VMware ESXi"]["detail"]
    assert by["MikroTik RouterOS"]["version"] == "7.12.1"
    # 不是 Dell 的设备不按 iDRAC / PowerEdge 比对
    other = Asset(name="fake-hp", form="physical", role="server", vendor="Fake", model="Fake Server")
    assert inventory.software_for(other, [_status(other, "redfish", {"bios": "1.0", "bmc_firmware": "2.0"})]) == []


class FakeIdracWithVersion(FakeIdrac):
    def get(self, oids: list[str]) -> dict[str, Any]:
        if IDRAC + ".1.1.8.0" in oids:
            return {IDRAC + ".1.1.2.0": "iDRAC9", IDRAC + ".1.1.8.0": "7.00.00.171", IDRAC + ".1.3.12.0": "PowerEdge R740"}
        return super().get(oids)


def test_snmp_reports_idrac_and_vendor_versions() -> None:
    ctx = EndpointContext(address="192.0.2.30", port=161, protocol="snmp", secret="fake-community")
    facts = SnmpAdapter(client_factory=lambda _c: FakeIdracWithVersion()).collect(ctx).facts
    assert (facts["idrac_name"], facts["idrac_firmware"], facts["system_model"]) == ("iDRAC9", "7.00.00.171", "PowerEdge R740")
    facts = SnmpAdapter(client_factory=lambda _c: FakeIdrac()).collect(ctx).facts  # 老固件读不到版本 OID：不影响采集
    assert facts["idrac"] is True and "idrac_firmware" not in facts

    class C:
        def get(self, oids: list[str]) -> dict[str, Any]:
            return {vendors.MT_VERSION: "7.12.1", vendors.SYNO_MODEL: "RS1221+", vendors.SYNO_VERSION: "DSM 7.2-64570 Update 3"}

    assert vendors.versions(C(), "1.3.6.1.4.1.14988.1", {}) == {"routeros_version": "7.12.1"}
    assert vendors.versions(C(), "1.3.6.1.4.1.8072", {vendors.SYNO_SYSTEM_STATUS: 1}) == {"dsm_version": "DSM 7.2-64570 Update 3", "dsm_model": "RS1221+"}


class FakeSsh:
    def __init__(self, result: dict[str, Any] | None = None, error: AdapterError | None = None) -> None:
        self.result, self.error, self.calls = result, error, 0

    def packages(self, ctx: EndpointContext) -> dict[str, Any]:
        self.calls += 1
        ctx.trust.check_ssh("ssh-ed25519 AAAAfake-host")
        if self.error:
            raise self.error
        return self.result or {}


def _host(session, *, enabled: bool = True, lifecycle: str = "active") -> tuple[Asset, ManagementEndpoint]:  # type: ignore[no-untyped-def]
    a = Asset(name="fake-debian", form="physical", role="server", lifecycle=lifecycle)
    ep = ManagementEndpoint(address="192.0.2.40", port=22, protocol="ssh", collect_packages=enabled)
    a.endpoints = [ep]
    session.add(a)
    session.flush()
    return a, ep


def test_scan_stores_replaces_and_reports(session) -> None:  # type: ignore[no-untyped-def]
    cipher = CredentialCipher(KeyRing.generate())
    a, ep = _host(session)
    fake = FakeSsh({"manager": "dpkg", "os_id": "debian", "os_version": "12",
                    "items": [["libssl3", "3.0.15-1~deb12u1", "openssl", "3.0.15-1~deb12u1"], ["bash", "5.2.15-2+b7", "bash", "5.2.15-2"]]})
    scan = inventory.scan_endpoint(session, cipher, ep, fake, NOW)
    assert (scan.state, scan.count, scan.ecosystem) == ("ok", 2, "Debian:12") and ep.ssh_host_key == "ssh-ed25519 AAAAfake-host"
    rows = session.scalars(select(AssetPackage).order_by(AssetPackage.name)).all()
    assert [(r.name, r.source, r.ecosystem) for r in rows] == [("bash", "bash", "Debian:12"), ("libssl3", "openssl", "Debian:12")]
    fake.result = {"manager": "dpkg", "os_id": "debian", "os_version": "12", "items": [["bash", "5.2.15-2+b7", "bash", "5.2.15-2"]]}
    inventory.scan_endpoint(session, cipher, ep, fake, NOW)
    assert [r.name for r in session.scalars(select(AssetPackage))] == ["bash"]  # 卸载的包不再保留
    fake.result = {"manager": "", "os_id": "alpine", "os_version": "3.19", "items": []}
    assert inventory.scan_endpoint(session, cipher, ep, fake, NOW).state == "unsupported"
    fake.error = AdapterError("auth", "SSH 用户名或密码不正确")
    scan = inventory.scan_endpoint(session, cipher, ep, fake, NOW)
    assert scan.state == "failed" and "不正确" in scan.message
    data = inventory.asset_software(session, a)
    assert data["packages_enabled"] == [str(ep.id)] and data["package_scans"][0]["state"] == "failed"


def test_daily_schedule(session) -> None:  # type: ignore[no-untyped-def]
    a, ep = _host(session)
    _host(session, enabled=False)
    _host(session, lifecycle="retired")
    assert inventory.enqueue_due(session, now=NOW) == 1
    assert inventory.enqueue_due(session, now=NOW) == 0  # 同一天不重复
    session.add(PackageScan(endpoint_id=ep.id, asset_id=a.id, state="ok", count=1, last_attempt_at=NOW))
    session.flush()
    assert inventory.enqueue_due(session, now=NOW + timedelta(hours=23)) == 0
    assert inventory.enqueue_due(session, now=NOW + timedelta(hours=25)) == 1
    assert session.scalar(select(Task.kind)) == inventory.PACKAGE_TASK


def test_api_endpoint_switch_software_and_scan(client, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "fake-op", "operator")
    c = login(client, "fake-op")
    a = c.post("/api/assets", json={"name": "fake-debian", "form": "physical", "role": "server",
                                    "endpoints": [{"address": "192.0.2.41", "port": 22, "protocol": "ssh"}]}).json()
    ep = c.get(f"/api/assets/{a['id']}").json()["endpoints"][0]
    assert ep["collect_packages"] is False
    assert c.post(f"/api/assets/{a['id']}/software/scan").status_code == 422
    assert c.patch(f"/api/endpoints/{ep['id']}", json={"collect_packages": True}).json()["collect_packages"] is True
    assert c.post(f"/api/assets/{a['id']}/software/scan").json() == {"queued": 1}
    sw = c.get(f"/api/assets/{a['id']}/software").json()
    assert sw["items"] == [] and sw["packages_enabled"] == [ep["id"]]
    assert c.get(f"/api/assets/{a['id']}/packages?q=ssl").json() == []
