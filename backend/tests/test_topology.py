"""拓扑三层分存：重新扫描不覆盖人工连线与锁定位置、忽略的不再提出、视图修订号冲突、移出视图不删资产、权限、不按 IP 合并。

全部是伪造数据，地址只用 192.0.2.0/24。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Asset, AuditEvent, CollectionStatus, ManagementEndpoint, NetworkLink
from tests.api_helpers import add_user, login


@pytest.fixture
def operator(app_factory, factory) -> TestClient:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    return login(app_factory(), "olivia")


@pytest.fixture
def viewer(app_factory, factory) -> TestClient:  # type: ignore[no-untyped-def]
    add_user(factory, "victor", "viewer")
    return login(app_factory(), "victor")


def add_asset(factory, name: str, role: str = "server", *, address: str | None = None, facts: dict | None = None,  # type: ignore[no-untyped-def]
              state: str = "ok") -> uuid.UUID:
    """建一台资产；给了 facts 就再建一个 SNMP 端点和对应的采集状态（模拟已采集到的事实）。"""
    with factory() as db:
        asset = Asset(name=name, form="physical", role=role)
        db.add(asset)
        db.flush()
        if address is not None or facts is not None:
            ep = ManagementEndpoint(asset_id=asset.id, address=address or "192.0.2.250", protocol="snmp", port=161)
            db.add(ep)
            db.flush()
            if facts is not None:
                now = datetime.now(UTC)
                db.add(CollectionStatus(endpoint_id=ep.id, asset_id=asset.id, protocol="snmp", state=state, facts=facts,
                                        last_attempt_at=now, last_success_at=now))
        db.commit()
        return asset.id


def set_facts(factory, asset_id: uuid.UUID, facts: dict, state: str = "ok") -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        st = db.scalars(select(CollectionStatus).where(CollectionStatus.asset_id == asset_id)).one()
        st.facts, st.state = facts, state
        db.commit()


def neighbor(local: str, name: str, port: str = "eth0") -> dict[str, str]:
    return {"local_port": local, "remote_name": name, "remote_port": port, "remote_chassis": "fake-chassis"}


def rescan(client: TestClient) -> dict:
    preview = client.post("/api/topology/rescan/preview")
    assert preview.status_code == 200, preview.text
    applied = client.post("/api/topology/rescan/apply", json={"token": preview.json()["token"]})
    assert applied.status_code == 200, applied.text
    return preview.json()


@pytest.fixture
def lab(factory):  # type: ignore[no-untyped-def]
    """核心交换机通过 LLDP 看到两台服务器：一台按采集的主机名匹配，一台按资产名称（带域名后缀）匹配。"""
    srv = add_asset(factory, "计算节点01", facts={"hostname": "fake-srv-01"}, address="192.0.2.21")
    nas = add_asset(factory, "fake-nas", role="nas")
    sw = add_asset(factory, "核心交换机", role="switch", address="192.0.2.2", facts={
        "sys_name": "fake-core-sw",
        "lldp_neighbors": [neighbor("3", "fake-srv-01"), neighbor("7", "fake-nas.example.test", "lan1")],
    })
    return {"sw": sw, "srv": srv, "nas": nas}


def links(client: TestClient) -> list[dict]:
    return client.get("/api/topology/links").json()


def test_default_home_and_datacenter_views(operator: TestClient) -> None:
    views = operator.get("/api/topology/views").json()
    assert {v["kind"] for v in views} == {"home", "datacenter"}
    assert all(v["revision"] == 1 and v["layout"] == {} for v in views)


def test_rescan_preview_then_apply_creates_candidates(operator: TestClient, lab) -> None:  # type: ignore[no-untyped-def]
    preview = operator.post("/api/topology/rescan/preview").json()
    assert links(operator) == []  # 预览不写库
    pairs = {(p["source_asset_id"], p["target_asset_id"]) for p in preview["added"]}
    assert pairs == {(str(lab["sw"]), str(lab["srv"])), (str(lab["sw"]), str(lab["nas"]))}
    assert preview["removed"] == [] and preview["refreshed"] == []
    rescan(operator)
    created = links(operator)
    assert len(created) == 2
    assert all(link["origin"] == "discovered" and link["state"] == "candidate" and not link["locked"] for link in created)
    srv_link = next(link for link in created if link["target_asset_id"] == str(lab["srv"]))
    assert srv_link["source_port"] == "3" and srv_link["target_port"] == "eth0"
    assert srv_link["evidence"]["source"] == "lldp"
    # 再扫一次没有变化
    again = operator.post("/api/topology/rescan/preview").json()
    assert again["added"] == [] and again["removed"] == [] and again["unchanged"] == 2


def test_rescan_never_overwrites_manual_links_or_locked_positions(operator: TestClient, factory, lab) -> None:  # type: ignore[no-untyped-def]
    rescan(operator)
    manual = operator.post("/api/topology/links", json={
        "source_asset_id": str(lab["nas"]), "target_asset_id": str(lab["srv"]), "source_port": "lan2", "target_port": "eno2", "notes": "机柜巡检确认",
    })
    assert manual.status_code == 201, manual.text
    assert manual.json()["origin"] == "manual" and manual.json()["state"] == "confirmed" and manual.json()["locked"]
    manual = next(link for link in links(operator) if link["id"] == manual.json()["id"])  # 读回的时间格式与之后一致
    view = operator.get("/api/topology/views").json()[0]
    layout = {str(lab["sw"]): {"x": 10.0, "y": 20.0, "locked": True}, str(lab["srv"]): {"x": 300.0, "y": 40.0, "locked": False}}
    saved = operator.patch(f"/api/topology/views/{view['id']}", json={"revision": view["revision"], "layout": layout}).json()

    # 新一次采集：交换机不再报告服务器，又看到了 NAS 报告服务器（与人工连线同一对设备）
    set_facts(factory, lab["sw"], {"sys_name": "fake-core-sw", "lldp_neighbors": [neighbor("7", "fake-nas", "lan1")]})
    set_facts(factory, lab["srv"], {"hostname": "fake-srv-01", "lldp_neighbors": [neighbor("eno2", "fake-nas", "lan2")]})
    preview = rescan(operator)
    assert [r["target_asset_id"] for r in preview["removed"]] == [str(lab["srv"])]
    assert preview["added"] == [] and preview["skipped_covered"] == 1
    after = {link["id"]: link for link in links(operator)}
    kept = after[manual["id"]]
    for key in ("source_asset_id", "target_asset_id", "source_port", "target_port", "origin", "state", "locked", "notes", "updated_at"):
        assert kept[key] == manual[key]
    assert operator.get(f"/api/topology/views/{view['id']}").json()["layout"] == saved["layout"] == layout
    assert operator.get(f"/api/topology/views/{view['id']}").json()["revision"] == saved["revision"]


def test_manual_link_pair_is_not_reproposed_as_candidate(operator: TestClient, lab) -> None:  # type: ignore[no-untyped-def]
    operator.post("/api/topology/links", json={"source_asset_id": str(lab["srv"]), "target_asset_id": str(lab["sw"])})
    preview = operator.post("/api/topology/rescan/preview").json()
    assert [p["target_asset_id"] for p in preview["added"]] == [str(lab["nas"])]
    assert preview["skipped_covered"] == 1 and preview["protected"] == 1


def test_rejected_candidates_are_not_reproposed(operator: TestClient, lab) -> None:  # type: ignore[no-untyped-def]
    rescan(operator)
    cand = next(link for link in links(operator) if link["target_asset_id"] == str(lab["srv"]))
    rejected = operator.post(f"/api/topology/links/{cand['id']}/reject")
    assert rejected.status_code == 200 and rejected.json()["state"] == "rejected"
    assert operator.post(f"/api/topology/links/{cand['id']}/reject").status_code == 409
    preview = rescan(operator)
    assert preview["added"] == [] and preview["skipped_rejected"] == 1
    states = sorted(link["state"] for link in links(operator))
    assert states == ["candidate", "rejected"]


def test_confirmed_links_survive_when_no_longer_seen(operator: TestClient, factory, lab) -> None:  # type: ignore[no-untyped-def]
    rescan(operator)
    cand = next(link for link in links(operator) if link["target_asset_id"] == str(lab["srv"]))
    confirmed = operator.post(f"/api/topology/links/{cand['id']}/confirm").json()
    assert confirmed["origin"] == "confirmed" and confirmed["state"] == "confirmed" and confirmed["locked"]
    assert operator.post(f"/api/topology/links/{cand['id']}/confirm").status_code == 409
    set_facts(factory, lab["sw"], {"sys_name": "fake-core-sw", "lldp_neighbors": []})
    preview = rescan(operator)
    assert len(preview["removed"]) == 1  # 只移除仍是候选的 NAS 那条
    remaining = links(operator)
    assert [link["id"] for link in remaining] == [cand["id"]] and remaining[0]["state"] == "confirmed"


def test_unreadable_reporter_keeps_candidates(operator: TestClient, factory, lab) -> None:  # type: ignore[no-untyped-def]
    rescan(operator)
    set_facts(factory, lab["sw"], {"sys_name": "fake-core-sw", "lldp_neighbors": [neighbor("3", "fake-srv-01")]}, state="failed")
    preview = operator.post("/api/topology/rescan/preview").json()
    assert preview["removed"] == [] and preview["kept_unreadable"] == 2


def test_apply_with_stale_preview_returns_409(operator: TestClient, factory, lab) -> None:  # type: ignore[no-untyped-def]
    token = operator.post("/api/topology/rescan/preview").json()["token"]
    set_facts(factory, lab["sw"], {"sys_name": "fake-core-sw", "lldp_neighbors": [neighbor("3", "fake-srv-01")]})
    resp = operator.post("/api/topology/rescan/apply", json={"token": token})
    assert resp.status_code == 409 and "重新预览" in resp.json()["detail"]
    assert links(operator) == []


def test_view_revision_conflict_returns_409(operator: TestClient, app_factory, factory, lab) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "otto", "operator")
    other = login(app_factory(), "otto")
    view = operator.get("/api/topology/views").json()[0]
    mine = {str(lab["sw"]): {"x": 1, "y": 2, "locked": False}}
    theirs = {str(lab["sw"]): {"x": 50, "y": 60, "locked": True}}
    first = other.patch(f"/api/topology/views/{view['id']}", json={"revision": 1, "layout": theirs})
    assert first.status_code == 200 and first.json()["revision"] == 2 and first.json()["updated_by"] == "otto"
    stale = operator.patch(f"/api/topology/views/{view['id']}", json={"revision": 1, "layout": mine})
    assert stale.status_code == 409
    assert "已被他人修改" in stale.json()["detail"] and "2" in stale.json()["detail"]
    latest = operator.get(f"/api/topology/views/{view['id']}").json()
    assert latest["layout"][str(lab["sw"])] == {"x": 50.0, "y": 60.0, "locked": True}
    ok = operator.patch(f"/api/topology/views/{view['id']}", json={"revision": latest["revision"], "layout": mine})
    assert ok.status_code == 200 and ok.json()["revision"] == 3


def test_invalid_layout_rejected(operator: TestClient) -> None:
    view = operator.get("/api/topology/views").json()[0]
    bad = operator.patch(f"/api/topology/views/{view['id']}", json={"revision": 1, "layout": {"not-a-uuid": {"x": 1, "y": 1}}})
    assert bad.status_code == 422
    huge = operator.patch(f"/api/topology/views/{view['id']}", json={"revision": 1, "layout": {str(uuid.uuid4()): {"x": 1e9, "y": 1}}})
    assert huge.status_code == 422


def test_hiding_node_or_deleting_view_never_deletes_asset(operator: TestClient, lab) -> None:  # type: ignore[no-untyped-def]
    rescan(operator)
    view = operator.get("/api/topology/views").json()[0]
    hidden = operator.patch(f"/api/topology/views/{view['id']}", json={"revision": 1, "hidden": [str(lab["srv"]), str(lab["srv"])]})
    assert hidden.status_code == 200 and hidden.json()["hidden"] == [str(lab["srv"])]
    assert operator.get(f"/api/assets/{lab['srv']}").status_code == 200
    graph = operator.get("/api/topology/graph", params={"view_id": view["id"]}).json()
    assert str(lab["srv"]) in {n["id"] for n in graph["nodes"]}
    assert len(graph["links"]) == 2
    assert operator.delete(f"/api/topology/views/{view['id']}").status_code == 204
    assert operator.get(f"/api/assets/{lab['srv']}").status_code == 200
    assert len(links(operator)) == 2


def test_graph_includes_asset_relations_and_site_scope(operator: TestClient, lab) -> None:  # type: ignore[no-untyped-def]
    rel = operator.post("/api/relations", json={"source_asset_id": str(lab["srv"]), "target_asset_id": str(lab["nas"]), "kind": "depends_on"})
    assert rel.status_code == 201
    graph = operator.get("/api/topology/graph").json()
    assert [r["kind"] for r in graph["relations"]] == ["depends_on"]
    site = operator.post("/api/sites", json={"name": "伪造机房", "kind": "datacenter"}).json()
    asset = operator.get(f"/api/assets/{lab['sw']}").json()
    operator.patch(f"/api/assets/{lab['sw']}", json={"revision": asset["revision"], "site_id": site["id"]})
    view = operator.post("/api/topology/views", json={"name": "伪造机房视图", "kind": "datacenter", "site_id": site["id"]})
    assert view.status_code == 201
    scoped = operator.get("/api/topology/graph", params={"view_id": view.json()["id"]}).json()
    assert [n["name"] for n in scoped["nodes"]] == ["核心交换机"] and scoped["nodes"][0]["site_name"] == "伪造机房"
    assert operator.post("/api/topology/views", json={"name": "伪造机房视图"}).status_code == 409


def test_manual_link_rules(operator: TestClient, lab) -> None:  # type: ignore[no-untyped-def]
    body = {"source_asset_id": str(lab["sw"]), "target_asset_id": str(lab["srv"]), "source_port": "3", "target_port": "eth0"}
    assert operator.post("/api/topology/links", json={**body, "target_asset_id": str(lab["sw"])}).status_code == 422
    assert operator.post("/api/topology/links", json={**body, "target_asset_id": str(uuid.uuid4())}).status_code == 422
    manual = operator.post("/api/topology/links", json=body).json()
    reverse = {"source_asset_id": str(lab["srv"]), "target_asset_id": str(lab["sw"]), "source_port": "eth0", "target_port": "3"}
    assert operator.post("/api/topology/links", json=reverse).status_code == 409
    moved = operator.patch(f"/api/topology/links/{manual['id']}", json={"target_asset_id": str(lab["nas"]), "target_port": "lan1"})
    assert moved.status_code == 200 and moved.json()["target_asset_id"] == str(lab["nas"])
    assert operator.post(f"/api/topology/links/{manual['id']}/reject").status_code == 409
    rescan(operator)
    cand = next(link for link in links(operator) if link["origin"] == "discovered")
    assert operator.delete(f"/api/topology/links/{cand['id']}").status_code == 409
    assert operator.patch(f"/api/topology/links/{cand['id']}", json={"source_port": "9"}).status_code == 409
    assert operator.delete(f"/api/topology/links/{manual['id']}").status_code == 204


def test_writes_are_audited(operator: TestClient, factory, lab) -> None:  # type: ignore[no-untyped-def]
    rescan(operator)
    view = operator.get("/api/topology/views").json()[0]
    operator.patch(f"/api/topology/views/{view['id']}", json={"revision": 1, "hidden": [str(lab["nas"])]})
    operator.post("/api/topology/links", json={"source_asset_id": str(lab["srv"]), "target_asset_id": str(lab["nas"])})
    with factory() as db:
        actions = set(db.scalars(select(AuditEvent.action)))
    assert {"topology.rescan.apply", "topology.view.update", "topology.link.create"} <= actions


def test_viewer_can_read_but_not_change(viewer: TestClient, operator: TestClient, lab) -> None:  # type: ignore[no-untyped-def]
    rescan(operator)
    view = viewer.get("/api/topology/views").json()[0]
    assert viewer.get(f"/api/topology/views/{view['id']}").status_code == 200
    assert viewer.get("/api/topology/graph").status_code == 200
    assert len(viewer.get("/api/topology/links").json()) == 2
    link = links(operator)[0]
    for resp in (
        viewer.patch(f"/api/topology/views/{view['id']}", json={"revision": 1, "hidden": []}),
        viewer.post("/api/topology/views", json={"name": "只读视图"}),
        viewer.delete(f"/api/topology/views/{view['id']}"),
        viewer.post("/api/topology/links", json={"source_asset_id": str(lab["sw"]), "target_asset_id": str(lab["nas"])}),
        viewer.post(f"/api/topology/links/{link['id']}/confirm"),
        viewer.post(f"/api/topology/links/{link['id']}/reject"),
        viewer.post("/api/topology/rescan/preview"),
        viewer.post("/api/topology/rescan/apply", json={"token": "x"}),
    ):
        assert resp.status_code == 403


def test_anonymous_cannot_read(client: TestClient) -> None:
    assert client.get("/api/topology/views").status_code == 401


def test_no_ip_matching_or_merging(operator: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    """远端只报 IP、或名称对应多台资产时都不生成候选，也不合并资产。"""
    add_asset(factory, "甲服务器", address="192.0.2.30")
    add_asset(factory, "乙服务器", address="192.0.2.30")  # 同一管理地址的两台资产
    add_asset(factory, "192.0.2.31")  # 用 IP 当名称的资产也不按 IP 匹配
    add_asset(factory, "dup-name")
    add_asset(factory, "DUP-NAME")
    add_asset(factory, "边界交换机", role="switch", facts={"sys_name": "fake-edge", "lldp_neighbors": [
        neighbor("1", "192.0.2.30"), neighbor("2", "192.0.2.31"), neighbor("3", "dup-name"), neighbor("4", "fake-unknown"),
        neighbor("5", "fake-edge"),  # 自己
    ]})
    preview = operator.post("/api/topology/rescan/preview").json()
    assert preview["added"] == []
    reasons = [u["reason"] for u in preview["unmatched"]]
    assert sum("IP" in r for r in reasons) == 2 and any("多台资产" in r for r in reasons) and any("没有名称" in r for r in reasons)
    rescan(operator)
    assert links(operator) == []
    assert operator.get("/api/assets").json()["total"] == 6


def test_deleting_asset_cascades_its_links(app_factory, operator: TestClient, factory, lab) -> None:  # type: ignore[no-untyped-def]
    rescan(operator)
    assert any(str(lab["srv"]) in (x["source_asset_id"], x["target_asset_id"]) for x in operator.get("/api/topology/links").json())
    # 移入回收站：链路还在库里，但不再显示
    assert operator.delete(f"/api/assets/{lab['srv']}").status_code == 204
    assert all(str(lab["srv"]) not in (x["source_asset_id"], x["target_asset_id"]) for x in operator.get("/api/topology/links").json())
    # 彻底删除时链路随之删除
    add_user(factory, "ada", "admin")
    assert login(app_factory(), "ada").delete(f"/api/assets/{lab['srv']}/purge").status_code == 204
    with factory() as db:
        assert db.scalars(select(NetworkLink)).all() and all(
            lab["srv"] not in (link.source_asset_id, link.target_asset_id) for link in db.scalars(select(NetworkLink)))
