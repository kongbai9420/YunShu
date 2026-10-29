"""资源全景显示排序：独立存储、修订冲突、按排序分页、子资源顺序，只改显示不改关系。"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models import Asset, AssetRelation, AuditEvent, Dashboard
from tests.api_helpers import add_user, login


def _seed(factory):
    with factory() as db:
        host = Asset(name="假宿主甲", form="physical", role="server")
        lone = Asset(name="假独立乙", form="physical", role="server")
        sw = Asset(name="假交换机", form="physical", role="switch")
        vm1 = Asset(name="假虚拟机1", form="vm", role="server")
        vm2 = Asset(name="假虚拟机2", form="vm", role="server")
        orphan = Asset(name="假未关联容器", form="container", role="server")
        db.add_all([host, lone, sw, vm1, vm2, orphan])
        db.flush()
        db.add_all([AssetRelation(source_asset_id=host.id, target_asset_id=vm.id, kind="hosts") for vm in (vm1, vm2)])
        db.commit()
        return {a.name: str(a.id) for a in (host, lone, sw, vm1, vm2, orphan)}


def _names(c, url="/api/overview/resources?limit=100"):
    return [(r["name"], r["group"]) for r in c.get(url).json()["items"]]


def test_default_order_groups_then_name_and_viewer_cannot_save(app_factory, factory):
    ids = _seed(factory)
    add_user(factory, "fake-order-viewer", "viewer")
    c = login(app_factory(), "fake-order-viewer")
    assert _names(c) == [("假宿主甲", "server"), ("假独立乙", "server"), ("假虚拟机1", "virtual-linked"), ("假虚拟机2", "virtual-linked"),
                         ("假未关联容器", "virtual-unlinked"), ("假交换机", "role:switch")]
    assert c.get("/api/overview/resources/layout").json()["revision"] == 0
    order = c.get("/api/overview/resources/order").json()
    assert [i["id"] for i in order["items"]][:2] == [ids["假宿主甲"], ids["假独立乙"]] and order["groups"][0] == "server"
    assert c.put("/api/overview/resources/layout", json={"revision": 0, "data": {}}).status_code == 403


def test_saved_order_drives_pagination_children_and_conflicts_without_touching_relations(app_factory, factory):
    ids = _seed(factory)
    add_user(factory, "fake-order-op", "operator")
    c = login(app_factory(), "fake-order-op")
    with factory() as db:
        relations = db.scalar(select(func.count()).select_from(AssetRelation))
    data = {"groups": ["role:switch", "virtual-unlinked", "server"],
            "order": [ids["假独立乙"], ids["假宿主甲"], ids["假未关联容器"]],
            "children": {ids["假宿主甲"]: [ids["假虚拟机2"], ids["假虚拟机1"]]}}
    saved = c.put("/api/overview/resources/layout", json={"revision": 0, "data": data})
    assert saved.status_code == 200 and saved.json()["revision"] == 1
    # 未列出的分组按默认顺序排在后面；未列出的资产按名称排在已排序之后
    assert _names(c) == [("假交换机", "role:switch"), ("假未关联容器", "virtual-unlinked"), ("假独立乙", "server"), ("假宿主甲", "server"),
                         ("假虚拟机1", "virtual-linked"), ("假虚拟机2", "virtual-linked")]
    assert [r["name"] for r in c.get("/api/overview/resources?limit=2&offset=2").json()["items"]] == ["假独立乙", "假宿主甲"]
    assert [r["name"] for r in c.get(f"/api/overview/resources/{ids['假宿主甲']}/children").json()["items"]] == ["假虚拟机2", "假虚拟机1"]
    # 修订冲突：返回当前版本，不静默覆盖
    stale = c.put("/api/overview/resources/layout", json={"revision": 0, "data": {}})
    assert stale.status_code == 409 and stale.json()["detail"]["current"]["revision"] == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AssetRelation)) == relations
        assert db.scalar(select(Dashboard.revision).where(Dashboard.key == "default")) is None  # 仪表盘布局不受影响
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "overview.resources.order.save")) == 1


def test_validation_and_deleted_assets_are_dropped(app_factory, factory):
    ids = _seed(factory)
    add_user(factory, "fake-order-admin", "admin")
    c = login(app_factory(), "fake-order-admin")
    for bad in ({"groups": ["server", "server"]}, {"groups": ["nope"]}, {"order": [ids["假宿主甲"], ids["假宿主甲"]]},
                {"children": {ids["假宿主甲"]: [ids["假虚拟机1"], ids["假虚拟机1"]]}}):
        assert c.put("/api/overview/resources/layout", json={"revision": 0, "data": bad}).status_code == 422
    gone = str(uuid.uuid4())
    r = c.put("/api/overview/resources/layout", json={"revision": 0, "data": {"order": [gone, ids["假交换机"]], "children": {gone: [ids["假虚拟机1"]]}}})
    assert r.status_code == 200 and r.json()["data"] == {"groups": [], "order": [ids["假交换机"]], "children": {}}
