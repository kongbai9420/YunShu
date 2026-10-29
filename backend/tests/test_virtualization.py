from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import func, select

from app import virtualization as v
from app.collect.base import AdapterError, CollectResult
from app.collect.runner import collect_asset
from app.models import Asset, AssetRelation, AuditEvent, CollectionStatus, ManagementEndpoint, VirtualizationObservation as Observation, VirtualizationSource as Source
from tests.api_helpers import add_user, login


def get(db, model, key):
    return db.get(model, uuid.UUID(key) if isinstance(key, str) else key)


def snapshot(host="node-a", *, complete=True):
    return {"platform": "pve", "instance_uuid": "", "complete": complete,
            "hosts": [{"external_id": "node-a", "name": "假节点甲"}, {"external_id": "node-b", "name": "假节点乙"}],
            "guests": [{"external_id": "100", "kind": "vm", "name": "同名虚拟机", "host_external_id": host, "power": "on"},
                       {"external_id": "101", "kind": "vm", "name": "同名虚拟机", "host_external_id": "node-a", "power": "off"},
                       {"external_id": "200", "kind": "container", "name": "假容器", "host_external_id": "node-a", "power": "unknown"}]}


@pytest.fixture
def setup(app_factory, factory):
    add_user(factory, "fake-virt-editor", "operator")
    c = login(app_factory(), "fake-virt-editor")
    with factory() as db:
        assets = [Asset(name=name, form=form, role=role, lifecycle="planned") for name, form, role in [
            ("假管理入口", "physical", "other"), ("假宿主甲", "physical", "server"), ("假宿主乙", "physical", "server"),
            ("已有同名 VM", "vm", "server"), ("已有同名 VM", "vm", "server")]]
        db.add_all(assets)
        db.flush()
        ep = ManagementEndpoint(asset_id=assets[0].id, address="192.0.2.180", protocol="pve-api")
        db.add(ep)
        db.flush()
        ids = [str(a.id) for a in assets]
        ep_id = str(ep.id)
        observe(db, ep, snapshot())
        source = db.scalar(select(Source))
        source_id = str(source.id)
        db.commit()
    return c, source_id, ids, ep_id


def observe(db, ep, data, *, at=None):
    now = at or datetime.now(UTC)
    st = db.get(CollectionStatus, ep.id)
    if st is None:
        st = CollectionStatus(endpoint_id=ep.id, asset_id=ep.asset_id, protocol=ep.protocol)
        db.add(st)
    st.state, st.facts, st.last_attempt_at, st.last_success_at = "ok", {"virtualization": data}, now, now
    v.ingest(db, ep, data, now)
    db.flush()


def rows(c, source):
    response = c.get(f"/api/virtualization/sources/{source}/observations")
    assert response.status_code == 200, response.text
    return {(o["kind"], o["external_id"]): o for o in response.json()["items"]}


def decision(c, source, key, action="link", asset_id=None, *, name="假新建资源"):
    o = rows(c, source)[key]
    body = {"action": action, "revision": o["revision"], "name": name, "asset_id": asset_id}
    url = f"/api/virtualization/observations/{o['id']}"
    p = c.post(f"{url}/preview", json=body)
    assert p.status_code == 200, p.text
    return url, body, p.json()


def accept(c, source, key, asset_id=None, action="link", name="假新建资源"):
    url, body, preview = decision(c, source, key, action, asset_id, name=name)
    result = c.post(f"{url}/confirm", json={**body, "preview_token": preview["preview_token"]})
    assert result.status_code == 200, result.text
    return result.json(), preview


@pytest.mark.parametrize("protocol", ["pve-api", "esxi-api"])
def test_observations_are_read_only_and_scoped_to_endpoint(setup, factory, protocol):
    c, source, ids, ep_id = setup
    assert len(rows(c, source)) == 5
    with factory() as db:
        ep = get(db, ManagementEndpoint, ep_id)
        other = ManagementEndpoint(asset_id=ep.asset_id, address=ep.address, protocol=protocol)
        db.add(other)
        db.flush()
        observe(db, other, {**snapshot(), "platform": "pve" if protocol == "pve-api" else "vmware"})
        db.commit()
        assert db.scalar(select(func.count()).select_from(Asset)) == 5
        assert db.scalar(select(func.count()).select_from(AssetRelation)) == 0
        assert db.scalar(select(func.count()).select_from(Source)) == 2
        assert db.scalar(select(func.count()).select_from(Observation)) == 10
    assert c.get("/api/virtualization/sources?limit=1").json()["total"] == 2
    assert len(c.get(f"/api/virtualization/sources/{source}/observations?limit=2&offset=2").json()["items"]) == 2


def test_explicit_hosts_then_create_link_and_duplicate_confirmation(setup, factory):
    c, source, ids, _ = setup
    guest = rows(c, source)["vm", "100"]
    assert c.post(f"/api/virtualization/observations/{guest['id']}/preview", json={"action": "create", "name": "假 VM", "revision": guest["revision"]}).status_code == 409
    accept(c, source, ("host", "node-a"), ids[1])
    url, body, preview = decision(c, source, ("vm", "100"), "create", name="假新 VM")
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Asset)) == 5  # 预览没有写入
    saved = c.post(url + "/confirm", json={**body, "preview_token": preview["preview_token"]})
    assert saved.status_code == 200
    assert c.post(url + "/confirm", json={**body, "preview_token": preview["preview_token"]}).status_code == 409
    asset = c.get(f"/api/assets/{saved.json()['asset_id']}").json()
    assert asset["lifecycle"] == "planned" and asset["form"] == "vm" and asset["endpoints"] == []
    accept(c, source, ("vm", "101"), ids[3])
    assert c.get(f"/api/assets/{ids[3]}").json()["name"] == "已有同名 VM"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Asset)) == 6
        assert db.scalar(select(func.count()).select_from(AssetRelation)) == 2
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "virtualization.create")) == 1


def test_migration_removes_only_owned_relation_and_preserves_manual(setup, factory):
    c, source, ids, ep_id = setup
    accept(c, source, ("host", "node-a"), ids[1])
    accept(c, source, ("host", "node-b"), ids[2])
    accept(c, source, ("vm", "100"), ids[3])
    with factory() as db:
        manual = AssetRelation(source_asset_id=uuid.UUID(ids[0]), target_asset_id=uuid.UUID(ids[3]), kind="hosts", origin="manual")
        db.add(manual)
        db.flush()
        manual_id = str(manual.id)
        observe(db, get(db, ManagementEndpoint, ep_id), snapshot("node-b"))
        db.commit()
        assert db.scalar(select(func.count()).select_from(AssetRelation)) == 2  # 重扫不改关系
    assert rows(c, source)["vm", "100"]["state"] == "review"
    _, preview = accept(c, source, ("vm", "100"), ids[3])
    assert preview["remove_relation"] and preview["preserved_relations"][0]["id"] == manual_id
    with factory() as db:
        pairs = {(str(r.source_asset_id), str(r.target_asset_id), r.origin) for r in db.scalars(select(AssetRelation))}
        assert pairs == {(ids[0], ids[3], "manual"), (ids[2], ids[3], "virtualization")}


def test_existing_manual_relation_is_never_adopted_or_deleted(setup, factory):
    c, source, ids, ep_id = setup
    accept(c, source, ("host", "node-a"), ids[1])
    accept(c, source, ("host", "node-b"), ids[2])
    assert c.post("/api/relations", json={"source_asset_id": ids[1], "target_asset_id": ids[3], "kind": "hosts"}).status_code == 201
    _, initial_preview = accept(c, source, ("vm", "100"), ids[3])
    assert initial_preview["preserved_relations"][0]["origin"] == "manual"  # 首次绑定已有资产也展示其人工关系
    with factory() as db:
        observe(db, get(db, ManagementEndpoint, ep_id), snapshot("node-b"))
        db.commit()
    _, preview = accept(c, source, ("vm", "100"), ids[3])
    assert preview["remove_relation"] is None and preview["preserved_relations"][0]["origin"] == "manual"
    accept(c, source, ("vm", "100"), action="unlink")
    a = c.get(f"/api/assets/{ids[3]}").json()
    assert len(a["relations"]) == 1 and a["relations"][0]["origin"] == "manual"


@pytest.mark.parametrize("change", ["failed", "stale", "target", "missing", "partial", "native_uuid"])
def test_stale_missing_or_changed_source_cannot_confirm(setup, factory, change):
    c, source, ids, ep_id = setup
    accept(c, source, ("host", "node-a"), ids[1])
    url, body, preview = decision(c, source, ("vm", "100"), "link", ids[3])
    with factory() as db:
        ep = get(db, ManagementEndpoint, ep_id)
        if change == "failed":
            db.get(CollectionStatus, ep.id).state = "failed"
        elif change == "stale":
            get(db, Source, source).observed_at = datetime.now(UTC) - timedelta(hours=1)
        elif change == "target":
            ep.address = "192.0.2.181"
        elif change == "native_uuid":
            get(db, Source, source).native_uuid = "fake-original-service"
            observe(db, ep, {**snapshot(), "instance_uuid": "fake-replaced-service"})
        else:
            observe(db, ep, {**snapshot(complete=change != "partial"), "guests": []})
        db.commit()
    assert c.post(url + "/confirm", json={**body, "preview_token": preview["preview_token"]}).status_code == 409
    result = rows(c, source)["vm", "100"]
    assert result["power"] == "unknown"
    assert result["asset"] is None


def test_reappearance_and_rename_require_review_without_asset_renaming(setup, factory):
    c, source, ids, ep_id = setup
    accept(c, source, ("host", "node-a"), ids[1])
    accept(c, source, ("vm", "100"), ids[3])
    with factory() as db:
        observe(db, get(db, ManagementEndpoint, ep_id), {**snapshot(), "guests": []})
        db.commit()
    assert rows(c, source)["vm", "100"]["state"] == "unseen"
    with factory() as db:
        data = snapshot()
        data["guests"][0]["name"] = "平台已改名"
        observe(db, get(db, ManagementEndpoint, ep_id), data)
        db.commit()
    assert rows(c, source)["vm", "100"]["state"] == "review"
    assert c.get(f"/api/assets/{ids[3]}").json()["name"] == "已有同名 VM"


def test_preview_detects_asset_edits_and_source_confirmations(setup, factory):
    c, source, ids, _ = setup
    accept(c, source, ("host", "node-a"), ids[1])
    url, body, preview = decision(c, source, ("vm", "100"), "link", ids[3])
    with factory() as db:
        get(db, Asset, ids[3]).revision += 1
        db.commit()
    assert c.post(url + "/confirm", json={**body, "preview_token": preview["preview_token"]}).status_code == 409
    url, body, preview = decision(c, source, ("vm", "100"), "link", ids[3])
    accept(c, source, ("host", "node-b"), ids[2])
    assert c.post(url + "/confirm", json={**body, "preview_token": preview["preview_token"]}).status_code == 409


def test_three_roles_and_csrf(setup, app_factory, factory):
    c, source, ids, _ = setup
    add_user(factory, "fake-virt-reader", "viewer")
    viewer = login(app_factory(), "fake-virt-reader")
    o = rows(viewer, source)["host", "node-a"]
    url = f"/api/virtualization/observations/{o['id']}/preview"
    assert viewer.post(url, json={"revision": o["revision"], "action": "create"}).status_code == 403
    assert app_factory().get("/api/virtualization/sources").status_code == 401
    c.headers.pop("X-CSRF-Token")
    assert c.post(url, json={"revision": o["revision"], "action": "ignore"}).status_code == 403


def test_real_collection_hook_creates_observations_only(setup, app_factory, factory):
    c, source, ids, ep_id = setup
    class Fake:
        def collect(self, ctx): return CollectResult(facts={"virtualization": snapshot("node-b")})
        def capabilities(self): return {"vms"}
    with factory() as db:
        asset = get(db, Asset, ids[0])
        asset.collection_profile = "pve"
        collect_asset(db, app_factory.cipher, asset.id, {"pve-api": Fake()}, force=True)
        db.commit()
        assert db.scalar(select(func.count()).select_from(Asset)) == 5
        assert db.scalar(select(func.count()).select_from(AssetRelation)) == 0
    assert rows(c, source)["vm", "100"]["host_external_id"] == "node-b"


def test_ignore_create_and_asset_shape_conflicts(setup):
    c, source, ids, _ = setup
    accept(c, source, ("container", "200"), action="ignore")
    assert rows(c, source)["container", "200"]["state"] == "ignored"
    host, _ = accept(c, source, ("host", "node-a"), action="create", name="假新宿主")
    asset = c.get(f"/api/assets/{host['asset_id']}").json()
    assert asset["form"] == "physical" and asset["lifecycle"] == "planned" and not asset["endpoints"]
    o = rows(c, source)["vm", "100"]
    assert c.post(f"/api/virtualization/observations/{o['id']}/preview", json={"action": "link", "revision": o["revision"], "asset_id": ids[1]}).status_code == 422
    accept(c, source, ("vm", "100"), ids[3])
    o = rows(c, source)["vm", "101"]
    assert c.post(f"/api/virtualization/observations/{o['id']}/preview", json={"action": "link", "revision": o["revision"], "asset_id": ids[3]}).status_code == 409


def test_duplicate_native_ids_and_late_snapshots_do_not_overwrite(setup, factory):
    c, source, ids, ep_id = setup
    with factory() as db:
        ep = get(db, ManagementEndpoint, ep_id)
        data = snapshot()
        data["guests"].append({**data["guests"][0], "name": "矛盾原生身份"})
        observe(db, ep, data)
        db.commit()
        saved_at = get(db, Source, source).observed_at
        v.ingest(db, ep, snapshot("node-b"), datetime.now(UTC) - timedelta(hours=1))
        db.commit()
        assert get(db, Source, source).observed_at == saved_at
    o = rows(c, source)["vm", "100"]
    assert o["name"] == "同名虚拟机" and o["quality"] == "not_seen" and o["power"] == "unknown"


def test_concurrent_confirmations_create_one_asset_only(setup, factory):
    from concurrent.futures import ThreadPoolExecutor
    c, source, ids, _ = setup
    accept(c, source, ("host", "node-a"), ids[1])
    url, body, preview = decision(c, source, ("vm", "100"), "create", name="假并发新 VM")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: c.post(url + "/confirm", json={**body, "preview_token": preview["preview_token"]}).status_code, range(2)))
    assert sorted(results) == [200, 409]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Asset).where(Asset.name == "假并发新 VM")) == 1


def test_migration_round_trip_keeps_existing_assets_and_manual_relations(engine):
    from sqlalchemy.orm import Session
    from app import migrate
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import MetaData, Table, insert

    migrate.downgrade(engine, "0015")
    # 按 0015 时的真实表结构写入，不经过当前 ORM（后续版本给 assets 加了列，4.2 P14）
    meta = MetaData()
    assets, relations = (Table(n, meta, autoload_with=engine) for n in ("assets", "asset_relations"))
    now = datetime.now(UTC)
    # SQLite 反射出的 ID 列是字符串，PostgreSQL 是 uuid
    u = (lambda x: x.hex) if engine.dialect.name == "sqlite" else (lambda x: x)
    a, b = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        for aid, name, form in ((a, "假升级宿主", "physical"), (b, "假升级 VM", "vm")):
            conn.execute(insert(assets).values(id=u(aid), name=name, form=form, role="server", platform="", vendor="", model="", serial_no="",
                                               collection_profile="", lifecycle="active", tags=[], notes="", revision=1, created_at=now, updated_at=now))
        conn.execute(insert(relations).values(id=u(uuid.uuid4()), source_asset_id=u(a), target_asset_id=u(b), kind="hosts", origin="manual",
                                              created_at=now, updated_at=now))
    migrate.upgrade(engine)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 0
        assert db.scalar(select(func.count()).select_from(Asset)) == 2
    migrate.downgrade(engine, "0015")
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Asset)) == 2
        assert db.scalar(select(AssetRelation.origin)) == "manual"
    migrate.upgrade(engine)
