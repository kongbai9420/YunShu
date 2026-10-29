"""4.2 P14：设备型号库导入、机柜 U 位冲突与视图、MAC / ARP 表与按地址定位、端口级并行链路。全部伪造数据。"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collect.snmp import ARP_MAC, BRIDGE_PORT_IFINDEX, FDB_EVERY_SECONDS, FDB_PORT, FDB_STATUS, IF_NAME, QFDB_PORT, SnmpAdapter
from app.models import Asset, CollectionStatus, DeviceType, MacObservation, ManagementEndpoint, NetworkLink, Rack, Site
from app.services import devicetypes, locate
from app.services.topology import apply_plan, build_plan, lldp_fingerprint
from tests.api_helpers import add_user, login

R740 = """---
manufacturer: Dell
model: PowerEdge R740xd
slug: dell-poweredge-r740xd
part_number: R740XD
u_height: 2
is_full_depth: true
comments: '[Dell spec sheet](https://example.invalid)'
interfaces:
  - name: iDRAC
    type: 1000base-t
    mgmt_only: true
  - name: NIC1
    type: 1000base-t
power-ports:
  - name: PSU1
    type: iec-60320-c14
  - name: PSU2
    type: iec-60320-c14
"""
SWITCH = """manufacturer: "Fake Networks"
model: FS-48 # 注释
u_height: 1
is_full_depth: false
interfaces:
- name: ge-1
  type: 1000base-t
- name: ge-2
  type: 1000base-t
- name: ge-3
  type: 1000base-t
"""
PDU = "manufacturer: Fake Power\nmodel: Vertical PDU\nu_height: 0\n"


def library_zip(extra: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("devicetype-library-master/device-types/Dell/PowerEdge-R740xd.yaml", R740)
        zf.writestr("devicetype-library-master/device-types/Fake/FS-48.yml", SWITCH)
        zf.writestr("devicetype-library-master/device-types/Fake/PDU.yaml", PDU)
        zf.writestr("devicetype-library-master/device-types/Fake/broken.yaml", "model: no manufacturer\n")
        zf.writestr("devicetype-library-master/module-types/Dell/x.yaml", R740)  # 不是型号目录，忽略
        zf.writestr("devicetype-library-master/elevation-images/Dell/r740.png", b"\x89PNG")
        for name, text in (extra or {}).items():
            zf.writestr(name, text)
    return buf.getvalue()


def test_parse_reads_only_the_fields_racks_need() -> None:
    item = devicetypes.parse(R740)
    assert item == {"manufacturer": "Dell", "model": "PowerEdge R740xd", "slug": "dell-poweredge-r740xd", "part_number": "R740XD",
                    "u_height": 2.0, "is_full_depth": True, "interfaces": 2, "power_ports": 2}
    sw = devicetypes.parse(SWITCH)
    assert sw["manufacturer"] == "Fake Networks" and sw["model"] == "FS-48" and sw["interfaces"] == 3 and sw["is_full_depth"] is False
    with pytest.raises(devicetypes.DeviceTypeError):
        devicetypes.parse("manufacturer: X\nmodel: Y\nu_height: tall\n")


def test_import_is_idempotent_and_keeps_manual_types(session: Session) -> None:
    session.add(DeviceType(manufacturer="Fake Networks", model="FS-48", u_height=4, source="manual"))
    session.commit()
    first = devicetypes.import_zip(session, library_zip())
    session.commit()
    assert (first.created, first.kept_manual, len(first.errors)) == (2, 1, 1) and "broken.yaml" in first.errors[0]
    second = devicetypes.import_zip(session, library_zip())
    session.commit()
    assert (second.created, second.updated, second.unchanged) == (0, 0, 2)
    assert session.scalar(select(DeviceType).where(DeviceType.model == "FS-48")).u_height == 4  # 手工录入不被覆盖
    only = devicetypes.import_zip(session, library_zip({"x/device-types/HPE/dl380.yaml": "manufacturer: HPE\nmodel: DL380\nu_height: 2\n"}), manufacturers={"hpe"})
    assert (only.created, only.unchanged) == (1, 0)
    with pytest.raises(devicetypes.DeviceTypeError):
        devicetypes.import_zip(session, b"not a zip")


@pytest.fixture
def rackroom(factory) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    with factory() as db:
        site = Site(name="fake-机房")
        db.add(site)
        db.flush()
        rack = Rack(site_id=site.id, name="A01", height_u=10)
        db.add(rack)
        db.flush()
        types = {}
        for key, (model, u, full) in {"2u": ("2U Server", 2, True), "half": ("1U Half", 1, False), "zero": ("0U PDU", 0, True)}.items():
            t = DeviceType(manufacturer="Fake", model=model, u_height=u, is_full_depth=full, source="manual")
            db.add(t)
            types[key] = t
        db.commit()
        return {"site": site.id, "rack": rack.id, **{k: v.id for k, v in types.items()}}


def _create(client, room, name: str, unit: int | None, dtype: str | None = None, face: str = "front"):  # type: ignore[no-untyped-def]
    return client.post("/api/assets", json={"name": name, "form": "physical", "role": "server", "site_id": str(room["site"]),
                                            "rack_id": str(room["rack"]), "rack_unit": unit, "rack_face": face,
                                            "device_type_id": str(room[dtype]) if dtype else None})


def test_rack_placement_conflicts_and_elevation(client, factory, rackroom) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "fake-ops", "operator")
    login(client, "fake-ops")
    assert _create(client, rackroom, "fake-db01", 1, "2u").status_code == 201  # 占 U1-U2 两面
    clash = _create(client, rackroom, "fake-web01", 2)
    assert clash.status_code == 409 and "U2" in clash.json()["detail"] and "fake-db01" in clash.json()["detail"]
    assert _create(client, rackroom, "fake-top", 10, "2u").status_code == 409  # 超出 10U
    front = _create(client, rackroom, "fake-front", 3, "half", "front")
    assert front.status_code == 201
    assert _create(client, rackroom, "fake-rear", 3, "half", "rear").status_code == 201  # 半深设备前后各装一台
    assert _create(client, rackroom, "fake-pdu", 3, "zero").status_code == 201  # 0U 不占位
    assert _create(client, rackroom, "fake-loose", None).status_code == 201

    # 修改时同样检查；改型号变高也会冲突
    asset = front.json()
    moved = client.patch(f"/api/assets/{asset['id']}", json={"revision": asset["revision"], "device_type_id": str(rackroom["2u"])})
    assert moved.status_code == 409

    view = client.get(f"/api/racks/{rackroom['rack']}/elevation").json()
    by_name = {i["name"]: i for i in view["items"]}
    assert by_name["fake-db01"]["height"] == 2 and by_name["fake-db01"]["full_depth"] is True
    assert by_name["fake-front"]["faces"] == ["front"] and by_name["fake-rear"]["faces"] == ["rear"]
    assert view["free_u"] == {"front": 7, "rear": 7} and [u["name"] for u in view["unplaced"]] == ["fake-loose"]
    summary = client.get("/api/racks/summary").json()
    assert summary[0]["name"] == "A01" and summary[0]["placed"] == 4  # db01、front、rear、0U 的 PDU


def test_rack_view_hides_out_of_scope_assets(client, factory, rackroom) -> None:  # type: ignore[no-untyped-def]
    from app.models import User, UserScopeAsset

    with factory() as db:
        mine = Asset(name="fake-mine", form="physical", role="server", rack_id=rackroom["rack"], rack_unit=1)
        other = Asset(name="fake-secret", form="physical", role="server", rack_id=rackroom["rack"], rack_unit=5)
        db.add_all([mine, other])
        db.commit()
        mine_id = mine.id
    add_user(factory, "fake-scoped", "viewer")
    with factory() as db:
        u = db.scalar(select(User).where(User.username == "fake-scoped"))
        u.scope_all = False
        db.add(UserScopeAsset(user_id=u.id, asset_id=mine_id))
        db.commit()
    login(client, "fake-scoped")
    view = client.get(f"/api/racks/{rackroom['rack']}/elevation").json()
    names = sorted(i["name"] for i in view["items"])
    assert names == ["fake-mine", "已占用（无权查看）"] and "fake-secret" not in str(view)
    assert client.get(f"/api/racks/{uuid.uuid4()}/elevation").status_code == 404


class FakeSwitch:
    def __init__(self) -> None:
        self.walks: list[str] = []

    def get(self, oids):  # type: ignore[no-untyped-def]
        return {o: None for o in oids} | {"1.3.6.1.2.1.1.5.0": "fake-sw1", "1.3.6.1.2.1.1.3.0": 100}

    def walk(self, oid):  # type: ignore[no-untyped-def]
        self.walks.append(oid)
        return {
            IF_NAME: {f"{IF_NAME}.1": "ge-1", f"{IF_NAME}.2": "ge-2", f"{IF_NAME}.49": "xe-49"},
            BRIDGE_PORT_IFINDEX: {f"{BRIDGE_PORT_IFINDEX}.1": 1, f"{BRIDGE_PORT_IFINDEX}.2": 2, f"{BRIDGE_PORT_IFINDEX}.49": 49},
            QFDB_PORT: {f"{QFDB_PORT}.10.0.17.34.51.68.85": 1, f"{QFDB_PORT}.10.170.187.204.221.238.255": 49, f"{QFDB_PORT}.10.0.0.0.0.0.1": 0},
            ARP_MAC: {f"{ARP_MAC}.1.192.0.2.55": "\x00\x11\x22\x33\x44\x55", f"{ARP_MAC}.1.192.0.2.66": "aabbccddeeff"},
        }.get(oid, {})


def test_snmp_reads_mac_tables_at_most_every_ten_minutes() -> None:
    from app.collect.base import EndpointContext

    clock = {"t": 1000.0}
    sw = FakeSwitch()
    adapter = SnmpAdapter(client_factory=lambda _c: sw, clock=lambda: clock["t"])
    ctx = EndpointContext(address="192.0.2.70", port=161, protocol="snmp", secret="fake-community")
    tables = adapter.collect(ctx).facts["mac_tables"]
    assert {"mac": "001122334455", "port": "ge-1", "vlan": 10} in tables["fdb"]
    assert {"mac": "aabbccddeeff", "port": "xe-49", "vlan": 10} in tables["fdb"] and len(tables["fdb"]) == 2  # 端口 0 不记录
    assert {"mac": "001122334455", "ip": "192.0.2.55", "port": "ge-1"} in tables["arp"]
    clock["t"] += 60
    assert "mac_tables" not in adapter.collect(ctx).facts
    clock["t"] += FDB_EVERY_SECONDS
    assert "mac_tables" in adapter.collect(ctx).facts


def test_bridge_mib_fallback_skips_self_entries() -> None:
    from app.collect.base import EndpointContext

    class Old(FakeSwitch):
        def walk(self, oid):  # type: ignore[no-untyped-def]
            return {FDB_PORT: {f"{FDB_PORT}.0.17.34.51.68.85": 2, f"{FDB_PORT}.0.0.0.0.0.9": 1},
                    FDB_STATUS: {f"{FDB_STATUS}.0.17.34.51.68.85": 3, f"{FDB_STATUS}.0.0.0.0.0.9": 4}}.get(oid, {})

    tables = SnmpAdapter(client_factory=lambda _c: Old()).collect(EndpointContext(address="192.0.2.71", port=161, protocol="snmp")).facts["mac_tables"]
    assert tables["fdb"] == [{"mac": "001122334455", "port": "if2", "vlan": None}]


def test_locate_by_ip_and_mac_ranks_access_ports_first(session: Session) -> None:
    now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    core = Asset(name="fake-core", form="physical", role="switch")
    edge = Asset(name="fake-edge", form="physical", role="switch")
    server = Asset(name="fake-srv", form="physical", role="server")
    server.endpoints.append(ManagementEndpoint(address="192.0.2.55", protocol="ssh", purpose="monitoring"))
    session.add_all([core, edge, server])
    session.flush()
    target = "001122334455"
    noise = [{"mac": f"0200000000{i:02x}", "port": "xe-49", "vlan": 10} for i in range(20)]
    locate.record(session, core.id, {"fdb": [{"mac": target, "port": "xe-49", "vlan": 10}, *noise],
                                     "arp": [{"mac": target, "ip": "192.0.2.55", "port": "vlan10"}]}, now)
    locate.record(session, edge.id, {"fdb": [{"mac": target, "port": "ge-7", "vlan": 10}], "arp": []}, now)
    session.commit()

    by_ip = locate.search(session, "192.0.2.55", None)
    assert by_ip["kind"] == "ip" and by_ip["macs"] == ["00:11:22:33:44:55"] and by_ip["assets"] == [{"id": str(server.id), "name": "fake-srv"}]
    assert [(x["switch_name"], x["port"], x["uplink"]) for x in by_ip["locations"]] == [("fake-edge", "ge-7", False), ("fake-core", "xe-49", True)]
    assert locate.search(session, "00-11-22-33-44-55", None)["locations"][0]["port"] == "ge-7"
    assert locate.search(session, "not-an-address", None)["kind"] == "invalid"
    assert locate.search(session, "192.0.2.99", None)["message"] == "ARP 表里没有这个 IP"
    # 范围受限：只看可见交换机上的观察
    only_core = select(Asset.id).where(Asset.id == core.id)
    assert [x["switch_name"] for x in locate.search(session, target, only_core)["locations"]] == ["fake-core"]

    # 7 天没再出现的条目清理；仍出现的刷新时间
    locate.record(session, edge.id, {"fdb": [], "arp": []}, now + timedelta(days=8))
    session.commit()
    assert session.scalar(select(MacObservation).where(MacObservation.switch_asset_id == edge.id)) is None


def _status(db: Session, asset: Asset, neighbors: list[dict[str, str]]) -> None:
    ep = ManagementEndpoint(asset_id=asset.id, address=f"192.0.2.{len(neighbors) + 80}", protocol="snmp", purpose="monitoring")
    db.add(ep)
    db.flush()
    db.add(CollectionStatus(endpoint_id=ep.id, asset_id=asset.id, protocol="snmp", state="ok", facts={"lldp_neighbors": neighbors},
                            last_attempt_at=datetime.now(UTC), last_success_at=datetime.now(UTC)))


def test_parallel_links_between_two_switches_are_separate_candidates(session: Session) -> None:
    a = Asset(name="fake-sw-a", form="physical", role="switch")
    b = Asset(name="fake-sw-b", form="physical", role="switch")
    session.add_all([a, b])
    session.flush()
    _status(session, a, [{"local_port": "xe-1", "remote_name": "fake-sw-b", "remote_port": "xe-9"},
                         {"local_port": "xe-2", "remote_name": "fake-sw-b", "remote_port": "xe-10"}])
    _status(session, b, [{"local_port": "xe-9", "remote_name": "fake-sw-a", "remote_port": "xe-1"},
                         {"local_port": "xe-10", "remote_name": "fake-sw-a", "remote_port": "xe-2"}])
    session.commit()
    plan = build_plan(session)
    assert len(plan.added) == 2  # 两条线、两端都上报，合并成两条候选
    apply_plan(session, plan)
    session.commit()
    links = session.scalars(select(NetworkLink)).all()
    assert sorted((link.source_port, link.target_port) for link in links) in ([("xe-1", "xe-9"), ("xe-2", "xe-10")], [("xe-10", "xe-2"), ("xe-9", "xe-1")])

    # 人工确认其中一条（带端口）：只覆盖这一对端口，另一条候选不受影响；重扫不改动已确认链路
    first = links[0]
    first.state, first.origin, first.locked = "confirmed", "confirmed", True
    session.commit()
    again = build_plan(session)
    assert len(again.added) == 0 and len(again.removed) == 0 and len(again.skipped_covered) == 1 and len(again.unchanged) == 1


def test_manual_link_without_ports_still_covers_the_pair_and_old_rejections_still_apply(session: Session) -> None:
    a = Asset(name="fake-sw-c", form="physical", role="switch")
    b = Asset(name="fake-sw-d", form="physical", role="switch")
    session.add_all([a, b])
    session.flush()
    _status(session, a, [{"local_port": "xe-1", "remote_name": "fake-sw-d", "remote_port": "xe-2"}])
    session.add(NetworkLink(source_asset_id=a.id, target_asset_id=b.id, origin="manual", state="confirmed", fingerprint="", locked=True))
    session.commit()
    assert len(build_plan(session).skipped_covered) == 1
    session.query(NetworkLink).delete()
    session.add(NetworkLink(source_asset_id=a.id, target_asset_id=b.id, origin="discovered", state="rejected",
                            fingerprint=lldp_fingerprint(a.id, b.id)))  # 4.2 之前按设备对忽略
    session.commit()
    plan = build_plan(session)
    assert len(plan.added) == 0 and len(plan.skipped_rejected) == 1


def test_api_locate_and_device_type_import(client, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "fake-admin", "admin")
    login(client, "fake-admin")
    r = client.post("/api/device-types/import", files={"file": ("lib.zip", library_zip(), "application/zip")}, data={"manufacturers": ""})
    assert r.status_code == 200 and r.json()["created"] == 3
    found = client.get("/api/device-types", params={"q": "r740"}).json()
    assert found["items"][0]["model"] == "PowerEdge R740xd" and found["items"][0]["u_height"] == 2
    assert client.post("/api/device-types/import", files={"file": ("x.zip", b"nope", "application/zip")}).status_code == 422
    assert client.get("/api/locate", params={"q": "192.0.2.1"}).json()["locations"] == []
