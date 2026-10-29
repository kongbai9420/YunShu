from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from app import metrics
from app.api.routes_resources import _page
from app.models import Alert, AlertRule, Asset, AssetRelation, CollectionStatus, ManagementEndpoint, Site
from tests.api_helpers import add_user, login


@pytest.mark.parametrize("role", ["admin", "operator", "viewer"])
def test_resources_require_login_and_allow_all_three_roles(app_factory, factory, role):
    c = app_factory()
    assert c.get("/api/overview/resources").status_code == 401
    add_user(factory, f"fake-resource-{role}", role)
    login(c, f"fake-resource-{role}")
    assert c.get("/api/overview/resources").json()["total"] == 0
    assert c.post("/api/overview/resources", json={}).status_code == 405
    assert c.get(f"/api/overview/resources/{uuid.uuid4()}/children").status_code == 404
    assert c.get("/api/overview/resources?limit=101").status_code == 422
    assert c.get("/api/overview/resources?role=invalid").status_code == 422


def test_all_categories_filters_pagination_and_safe_endpoint_fields(app_factory, factory):
    add_user(factory, "fake-resource-reader", "viewer")
    c = login(app_factory(), "fake-resource-reader")
    with factory() as db:
        site = Site(name="假场所", kind="home")
        db.add(site)
        db.flush()
        for i, role in enumerate(["server", "switch", "router", "ap", "firewall", "nas", "storage", "ups", "other"]):
            a = Asset(name=f"假资源{i}", form="physical", role=role, lifecycle="retired" if i == 8 else "active", site_id=site.id, tags=["lab"], platform="fake-linux")
            a.endpoints.append(ManagementEndpoint(address=f"192.0.2.{i + 1}", protocol="ssh", notes="fake-private-note", ssh_host_key="fake-private-key"))
            db.add(a)
        db.add(Asset(name="同名虚拟机", form="vm", role="server", tags=["lab-extra"]))
        db.add(Asset(name="同名虚拟机", form="container", role="server"))
        site_id = str(site.id)
        db.commit()
    all_rows = c.get("/api/overview/resources?limit=100").json()
    assert all_rows["total"] == 11 and len({a["id"] for a in all_rows["items"]}) == 11
    assert {a["role"] for a in all_rows["items"]} == {"server", "switch", "router", "ap", "firewall", "nas", "storage", "ups", "other"}
    assert "fake-private" not in str(all_rows)
    for query, count in [("form=vm", 1), ("form=container", 1), ("role=ups", 1), ("lifecycle=retired", 1), ("tag=lab", 9), ("tag=la", 0), (f"site_id={site_id}", 9), ("q=192.0.2.1", 1), ("platform=fake-linux", 9)]:
        assert c.get(f"/api/overview/resources?{query}").json()["total"] == count
    page1 = c.get("/api/overview/resources?limit=5").json()["items"]
    page2 = c.get("/api/overview/resources?limit=5&offset=5").json()["items"]
    assert len(page1) == len(page2) == 5
    assert not {a["id"] for a in page1} & {a["id"] for a in page2}
    assert c.get("/api/overview/resources?offset=99").json()["items"] == []


def test_confirmed_hosts_are_finite_even_with_cycles_and_multiple_parents(app_factory, factory):
    add_user(factory, "fake-resource-reader", "viewer")
    c = login(app_factory(), "fake-resource-reader")
    with factory() as db:
        host, guest, unrelated = [Asset(name=name, form="vm" if name == "假子机" else "physical", role="server") for name in ["假宿主", "假子机", "假依赖"]]
        extra = [Asset(name=f"假宿主-{i}", form="physical", role="server") for i in range(4)]
        db.add_all([host, guest, unrelated, *extra])
        db.flush()
        for parent in [host, *extra]:
            db.add(AssetRelation(source_asset_id=parent.id, target_asset_id=guest.id, kind="hosts"))
        db.add(AssetRelation(source_asset_id=guest.id, target_asset_id=host.id, kind="hosts"))
        db.add(AssetRelation(source_asset_id=host.id, target_asset_id=unrelated.id, kind="depends_on"))
        host_id, guest_id = str(host.id), str(guest.id)
        db.commit()
    page = c.get("/api/overview/resources").json()
    assert page["total"] == 7
    child = next(a for a in page["items"] if a["id"] == guest_id)
    assert child["parent_count"] == 5 and len(child["parents"]) == 3
    direct = c.get(f"/api/overview/resources/{host_id}/children").json()
    assert direct["total"] == 1 and direct["items"][0]["id"] == guest_id
    assert direct["items"][0]["children_count"] == 1
    assert "children" not in direct["items"][0]  # 只返回一层，不递归沿环读取


def test_metric_quality_and_power_are_not_inherited_from_host(app_factory, factory):
    add_user(factory, "fake-resource-reader", "viewer")
    c = login(app_factory(), "fake-resource-reader")
    now = datetime.now(UTC)
    with factory() as db:
        host = Asset(name="假失联宿主", form="physical", role="server", platform="dell-idrac")
        host.endpoints.append(ManagementEndpoint(address="192.0.2.80", protocol="ipmi"))
        guest = Asset(name="假未知子机", form="vm", role="server")
        switch = Asset(name="假过期交换机", form="physical", role="switch", collection_profile="snmp-switch")
        switch.endpoints.append(ManagementEndpoint(address="192.0.2.81", protocol="snmp"))
        db.add_all([host, guest, switch])
        db.flush()
        db.add(AssetRelation(source_asset_id=host.id, target_asset_id=guest.id, kind="hosts"))
        db.add(CollectionStatus(endpoint_id=host.endpoints[0].id, asset_id=host.id, protocol="ipmi", state="failed", last_attempt_at=now))
        db.add(CollectionStatus(endpoint_id=switch.endpoints[0].id, asset_id=switch.id, protocol="snmp", state="ok", last_attempt_at=now - timedelta(hours=1)))
        rule_id = db.scalar(select(AlertRule.id).limit(1))
        assert rule_id is not None
        for n, state in enumerate(["active", "acknowledged", "silenced", "resolved", "pending"]):
            db.add(Alert(rule_id=rule_id, asset_id=host.id, metric_key=f"fake.resource.{n}", state=state))
        metrics.write_sample(db, host.id, "ipmi", [metrics.Point("power.on", 0, ""), metrics.Point("cpu.temp", 60, "°C")], observed_at=now - timedelta(hours=1))
        metrics.write_sample(db, host.id, "ipmi", [metrics.Point("power.watts", None, "W", quality="failed")], observed_at=now)
        host_id = str(host.id)
        db.commit()
    page = c.get("/api/overview/resources").json()
    h = next(a for a in page["items"] if a["id"] == host_id)
    assert h["collection_state"] == "failed"
    assert h["active_alerts"] == 3
    assert next(a for a in page["items"] if a["name"] == "假过期交换机")["collection_state"] == "stale"
    for key in ["power.on", "cpu.temp"]:
        assert h["metrics"][key]["quality"] == "stale" and h["metrics"][key]["value"] is None
    assert h["metrics"]["power.watts"]["quality"] == "failed" and h["metrics"]["power.watts"]["value"] is None
    g = c.get(f"/api/overview/resources/{host_id}/children").json()["items"][0]
    assert g["metrics"] == {} and g["collection_state"] == "unmonitored" and g["active_alerts"] == 0


def test_batch_query_budget_for_120_assets_and_paginated_children(engine, session):
    """固定代表集：120 资产，60 条承载；25 项一页，SQL 数不随卡片数线性增长。"""
    site = Site(name="假批量场所", kind="datacenter")
    session.add(site)
    session.flush()
    assets = [Asset(name=f"fake-batch-{i:03}", form="physical", role="server", site_id=site.id) for i in range(120)]
    session.add_all(assets)
    session.flush()
    for a in assets:
        a.endpoints.append(ManagementEndpoint(address="192.0.2.90", protocol="ssh"))
        metrics.write_sample(session, a.id, "ssh", [metrics.Point("cpu.load", 1, "%")], observed_at=datetime.now(UTC))
    session.add_all([AssetRelation(source_asset_id=assets[0].id, target_asset_id=a.id, kind="hosts") for a in assets[1:61]])
    session.commit()
    counts = []
    for size in [1, 25]:
        statements = []
        def track(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", track)
        try:
            page = _page(session, select(Asset), size, 0)
            assert page["total"] == 120 and len(page["items"]) == size
            assert page["items"][0]["children_count"] == 60
        finally:
            event.remove(engine, "before_cursor_execute", track)
        counts.append(len(statements))
    assert counts[0] == counts[1] and counts[1] <= 10
