"""按资产范围授权（4.1 P11）：两个不相交范围、一个交叉范围与默认全部可见账号的权限矩阵；全部为假数据。"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.events import StreamConfig
from app.models import Alert, AlertRule, Asset, AssetRelation, ManagementEndpoint, Site, Task, User, UserScopeAsset, UserScopeSite
from app.models.control import ControlOperation
from tests.api_helpers import add_user, login
from tests.test_events import named, parse, set_status


@pytest.fixture
def world(app_factory, factory):  # type: ignore[no-untyped-def]
    """场所 A（下级 A1）、B；host_a 在 A1，vm_b 在 B 且由 host_a 承载，b1 在 B，x 没有场所。"""
    cipher = app_factory.cipher
    from app.services.credentials import create_credential

    with factory() as db:
        A = Site(name="假场所A", kind="datacenter")
        B = Site(name="假场所B", kind="datacenter")
        db.add_all([A, B])
        db.flush()
        A1 = Site(name="假机房A1", kind="room", parent_id=A.id)
        db.add(A1)
        db.flush()
        cred_a = create_credential(db, cipher, alias="假凭据A", kind="password", secret="fake-a", username="root")
        cred_shared = create_credential(db, cipher, alias="假共享凭据", kind="password", secret="fake-s", username="root")
        host_a = Asset(name="假宿主A", form="physical", role="server", site_id=A1.id)
        host_a.endpoints.append(ManagementEndpoint(address="192.0.2.61", protocol="ssh", credential_id=cred_a.id))
        host_a.endpoints.append(ManagementEndpoint(address="192.0.2.62", protocol="ipmi", credential_id=cred_shared.id))
        vm_b = Asset(name="假虚拟机B", form="vm", role="server", site_id=B.id)
        b1 = Asset(name="假设备B1", form="physical", role="switch", site_id=B.id)
        b1.endpoints.append(ManagementEndpoint(address="192.0.2.63", protocol="snmp", credential_id=cred_shared.id))
        x = Asset(name="假无场所X", form="physical", role="server")
        db.add_all([host_a, vm_b, b1, x])
        db.flush()
        db.add(AssetRelation(source_asset_id=host_a.id, target_asset_id=vm_b.id, kind="hosts"))
        rule = db.scalar(select(AlertRule))
        now = datetime.now(UTC)
        for a in (host_a, b1):
            db.add(Alert(rule_id=rule.id, asset_id=a.id, asset_name=a.name, metric_key="cpu.temp", title=f"假告警 {a.name}",
                         severity="warning", state="active", first_seen_at=now, last_seen_at=now))
            db.add(Task(kind="collect.asset", asset_id=a.id, payload={}))
        ids = {k: v.id for k, v in {"A": A, "A1": A1, "B": B, "host_a": host_a, "vm_b": vm_b, "b1": b1, "x": x,
                                    "cred_a": cred_a, "cred_shared": cred_shared}.items()}
        db.commit()
    add_user(factory, "fake-scope-admin", "admin")
    for name, role, sites, assets in (("fake-op-a", "operator", ["A"], []), ("fake-op-b", "operator", ["B"], []),
                                      ("fake-cross", "viewer", ["A"], ["x"]), ("fake-op-all", "operator", None, None)):
        add_user(factory, name, role)
        if sites is not None:
            with factory() as db:
                u = db.scalar(select(User).where(User.username == name))
                u.scope_all = False
                db.add_all([UserScopeSite(user_id=u.id, site_id=ids[s]) for s in sites] + [UserScopeAsset(user_id=u.id, asset_id=ids[a]) for a in assets])
                db.commit()
    return ids


def _names(c, path="/api/assets"):  # type: ignore[no-untyped-def]
    return sorted(a["name"] for a in c.get(path).json()["items"])


def test_lists_details_and_relations_follow_scope(app_factory, factory, world) -> None:  # type: ignore[no-untyped-def]
    a, b, cross, every = (login(app_factory(), n) for n in ("fake-op-a", "fake-op-b", "fake-cross", "fake-op-all"))
    assert _names(a) == ["假宿主A"]  # 场所包含下级 A1
    assert _names(b) == ["假虚拟机B", "假设备B1"]
    assert _names(cross) == ["假宿主A", "假无场所X"]  # 场所 + 单独授权
    assert len(_names(every)) == 4  # 未设置范围的账号行为不变
    # 范围外当作不存在；关系只在两端都可见时显示，宿主可见不带出 VM
    assert a.get(f"/api/assets/{world['vm_b']}").status_code == 404
    assert a.get(f"/api/assets/{world['host_a']}").json()["relations"] == []
    assert b.get(f"/api/assets/{world['vm_b']}").json()["relations"] == []
    assert len(every.get(f"/api/assets/{world['host_a']}").json()["relations"]) == 1
    for path in (f"/api/assets/{world['b1']}/metrics", f"/api/assets/{world['b1']}/collection", f"/api/control/assets/{world['b1']}/power"):
        assert a.get(path).status_code == 404, path
    # 资源全景：承载数量与宿主引用只计可见的一端
    host_card = next(r for r in a.get("/api/overview/resources").json()["items"] if r["name"] == "假宿主A")
    assert host_card["children_count"] == 0
    vm_card = next(r for r in b.get("/api/overview/resources").json()["items"] if r["name"] == "假虚拟机B")
    assert vm_card["parents"] == [] and vm_card["parent_count"] == 0 and "假宿主A" not in str(b.get("/api/overview/resources").json())
    assert a.get(f"/api/overview/resources/{world['b1']}/children").status_code == 404
    # 拓扑、场所、告警、任务、仪表盘
    assert sorted(n["name"] for n in a.get("/api/topology/graph").json()["nodes"]) == ["假宿主A"]
    assert sorted(s["name"] for s in a.get("/api/sites").json()) == ["假场所A", "假机房A1"]
    assert [x["asset_name"] for x in a.get("/api/alerts").json()["items"]] == ["假宿主A"]
    assert a.get("/api/alerts/summary").json()["active"] == 1 and every.get("/api/alerts/summary").json()["active"] == 2
    assert {t["asset_name"] for t in a.get("/api/tasks").json()["items"]} == {"假宿主A"}
    summary = a.get("/api/dashboard/summary").json()
    assert summary["total_assets"] == 1 and "假设备B1" not in str(summary)


def test_credentials_and_writes(app_factory, factory, world) -> None:  # type: ignore[no-untyped-def]
    a, b = login(app_factory(), "fake-op-a"), login(app_factory(), "fake-op-b")
    assert sorted(c["alias"] for c in a.get("/api/credentials").json()) == ["假共享凭据", "假凭据A"]
    assert [c["alias"] for c in b.get("/api/credentials").json()] == ["假共享凭据"]
    assert a.patch(f"/api/credentials/{world['cred_a']}", json={"notes": "fake"}).status_code == 200
    assert a.patch(f"/api/credentials/{world['cred_shared']}", json={"notes": "fake"}).status_code == 403  # 也被范围外资产使用
    assert b.patch(f"/api/credentials/{world['cred_a']}", json={"notes": "fake"}).status_code == 404
    # 新建资产只能放在授权场所；不能借用范围外凭据
    assert a.post("/api/assets", json={"name": "假新资产", "form": "physical", "role": "server", "site_id": str(world["B"])}).status_code == 403
    assert a.post("/api/assets", json={"name": "假新资产", "form": "physical", "role": "server", "site_id": str(world["A1"])}).status_code == 201
    assert b.post("/api/assets", json={"name": "假新资产2", "form": "physical", "role": "server", "site_id": str(world["B"]),
                                       "endpoints": [{"address": "192.0.2.64", "protocol": "ssh", "credential_id": str(world["cred_a"])}]}).status_code == 422
    # 修改 / 删除 / 关系 / 控制：范围外当作不存在
    assert a.delete(f"/api/assets/{world['b1']}").status_code == 404
    assert a.post("/api/relations", json={"source_asset_id": str(world["host_a"]), "target_asset_id": str(world["b1"]), "kind": "depends_on"}).status_code == 404
    assert a.post("/api/control/power", json={"asset_ids": [str(world["b1"])], "action": "on", "request_key": "fake-scope-key"}).status_code == 404
    # 只对全部可见开放的功能：灰键由前端处理，后端 403
    for method, path in (("get", "/api/import/batches"), ("get", "/api/discovery/runs"), ("get", "/api/virtualization/sources"),
                         ("get", "/api/workers"), ("get", "/api/alerts/rules"), ("post", "/api/sites")):
        r = getattr(a, method)(path, **({"json": {"name": "fake", "kind": "room"}} if method == "post" else {}))
        assert r.status_code == 403 and "可见全部资产" in r.text, path
    every = login(app_factory(), "fake-op-all")
    assert every.get("/api/discovery/runs").status_code == 200 and every.get("/api/alerts/rules").status_code == 200


def test_control_rechecked_against_current_scope_before_execution(factory, world) -> None:  # type: ignore[no-untyped-def]
    from app.control.service import Rejected, _check_requester_scope

    with factory() as db:
        user = db.scalar(select(User).where(User.username == "fake-op-a"))
        op = ControlOperation(asset_id=world["host_a"], kind="power", action="on", requested_by_id=user.id)
        _check_requester_scope(db, op, world["host_a"])  # 入队时在范围内
        db.execute(UserScopeSite.__table__.delete().where(UserScopeSite.user_id == user.id))
        db.flush()
        with pytest.raises(Rejected, match="范围已收窄"):
            _check_requester_scope(db, op, world["host_a"])  # 撤销后不发送命令


def test_admin_scope_api_and_preview(app_factory, factory, world) -> None:  # type: ignore[no-untyped-def]
    admin = login(app_factory(), "fake-scope-admin")
    users = {u["username"]: u for u in admin.get("/api/users").json()}
    assert users["fake-op-a"]["scope_all"] is False and users["fake-op-all"]["scope_all"] is True
    uid = users["fake-op-b"]["id"]
    assert admin.get(f"/api/users/{uid}/scope").json()["site_ids"] == [str(world["B"])]
    r = admin.put(f"/api/users/{uid}/scope", json={"scope_all": False, "site_ids": [], "asset_ids": [str(world["x"])]})
    assert r.status_code == 200 and r.json()["asset_ids"] == [str(world["x"])]
    preview = admin.get(f"/api/users/{uid}/scope/preview").json()
    assert preview["all"] is False and preview["total"] == 1 and preview["items"][0]["name"] == "假无场所X"
    assert _names(login(app_factory(), "fake-op-b")) == ["假无场所X"]
    # 空范围是明确的空，不是全部
    admin.put(f"/api/users/{uid}/scope", json={"scope_all": False, "site_ids": [], "asset_ids": []})
    assert _names(login(app_factory(), "fake-op-b")) == []
    assert admin.put(f"/api/users/{users['fake-scope-admin']['id']}/scope", json={"scope_all": False}).status_code == 422
    assert login(app_factory(), "fake-op-a").get(f"/api/users/{uid}/scope").status_code == 403


def test_live_events_are_filtered_by_scope(app_factory, factory, world) -> None:  # type: ignore[no-untyped-def]
    c = login(app_factory(), "fake-op-a")
    c.app.state.sse_config = StreamConfig(poll_seconds=0.05, heartbeat_seconds=1, max_seconds=0.3)
    with factory() as db:
        eps = {a: db.scalar(select(ManagementEndpoint).where(ManagementEndpoint.asset_id == world[a])) for a in ("host_a", "b1")}
        hosts = {a: db.get(Asset, world[a]) for a in ("host_a", "b1")}
    for k in ("host_a", "b1"):
        set_status(factory, hosts[k], eps[k], "ok", datetime.now(UTC))
    with c.stream("GET", "/api/events") as resp:
        text = "".join(resp.iter_text())
    snap = named(parse(text), "collection")[0]["data"]
    assert snap["snapshot"] is True and {i["asset_id"] for i in snap["items"]} == {str(world["host_a"])}
    assert str(world["b1"]) not in text and not named(parse(text), "tasks")  # 任务计数只推给全部可见的账号
    assert uuid  # 保持导入
