"""资产回收站（P22）：删除移入回收站、到处隐藏、恢复、彻底删除、到期自动清理、保留期设置；全部为假数据。"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import recycle
from app.alerting import evaluate
from app.collect.runner import enqueue_due
from app.models import Alert, Asset, AuditEvent, LatestMetric, ManagementEndpoint, Rack, Site, User, UserScopeSite
from tests.api_helpers import add_user, login

SERVER = {"name": "fake-recycle-srv", "asset_no": "FAKE-RB-1", "form": "physical", "role": "server", "platform": "ubuntu",
          "endpoints": [{"address": "192.0.2.81", "protocol": "ssh", "purpose": "monitoring"}]}


@pytest.fixture
def operator(app_factory, factory) -> TestClient:  # type: ignore[no-untyped-def]
    add_user(factory, "fake-rb-op", "operator")
    return login(app_factory(), "fake-rb-op")


@pytest.fixture
def admin(app_factory, factory) -> TestClient:  # type: ignore[no-untyped-def]
    add_user(factory, "fake-rb-admin", "admin")
    return login(app_factory(), "fake-rb-admin")


def _bin(c: TestClient) -> list[dict]:
    resp = c.get("/api/assets/recycle-bin")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def test_delete_moves_to_bin_and_hides_everywhere(operator: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    host = operator.post("/api/assets", json=SERVER).json()
    vm = operator.post("/api/assets", json={"name": "fake-recycle-vm", "form": "vm", "role": "server"}).json()
    assert operator.post("/api/relations", json={"source_asset_id": host["id"], "target_asset_id": vm["id"], "kind": "hosts"}).status_code == 201
    before = operator.get("/api/dashboard/summary").json()["total_assets"]

    assert operator.delete(f"/api/assets/{host['id']}").status_code == 204
    assert operator.get(f"/api/assets/{host['id']}").status_code == 404
    assert [a["name"] for a in operator.get("/api/assets").json()["items"]] == ["fake-recycle-vm"]
    assert operator.get(f"/api/assets/{vm['id']}").json()["relations"] == []
    assert operator.get("/api/dashboard/summary").json()["total_assets"] == before - 1
    assert [i["name"] for i in operator.get("/api/overview/resources").json()["items"]] == ["fake-recycle-vm"]
    assert operator.get(f"/api/assets/{host['id']}/metrics").status_code == 404
    # 已在回收站的不能再删除、修改
    assert operator.delete(f"/api/assets/{host['id']}").status_code == 404
    assert operator.patch(f"/api/assets/{host['id']}", json={"revision": 2, "notes": "x"}).status_code == 404

    items = _bin(operator)
    assert [i["name"] for i in items] == ["fake-recycle-srv"]
    assert items[0]["deleted_by"] == "fake-rb-op" and items[0]["deleted_lifecycle"] == "active"
    deleted_at = datetime.fromisoformat(items[0]["deleted_at"])
    assert datetime.fromisoformat(items[0]["purge_at"]) - deleted_at == timedelta(days=30)
    with factory() as db:
        row = db.get(Asset, uuid.UUID(host["id"]))
        assert row.lifecycle == "retired" and row.deleted_at is not None and row.maintenance_until is None
        summary = db.scalar(select(AuditEvent.summary).where(AuditEvent.action == "asset.delete"))
    assert "回收站" in summary


def test_restore_brings_back_lifecycle_and_visibility(operator: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    a = operator.post("/api/assets", json={**SERVER, "lifecycle": "maintenance"}).json()
    operator.delete(f"/api/assets/{a['id']}")
    resp = operator.post(f"/api/assets/{a['id']}/restore")
    assert resp.status_code == 200, resp.text
    assert resp.json()["lifecycle"] == "maintenance" and resp.json()["notice"] == ""
    detail = operator.get(f"/api/assets/{a['id']}").json()
    assert detail["lifecycle"] == "maintenance" and detail["revision"] == 3
    assert _bin(operator) == []
    with factory() as db:
        row = db.get(Asset, uuid.UUID(a["id"]))
        assert row.deleted_at is None and row.deleted_by == "" and row.maintenance_until is not None
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == "asset.restore")) is not None
    # 不在回收站的不能恢复
    assert operator.post(f"/api/assets/{a['id']}/restore").status_code == 404


def test_open_alerts_close_and_collection_stops(operator: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    a = operator.post("/api/assets", json=SERVER).json()
    aid = uuid.UUID(a["id"])
    now = datetime.now(UTC)
    with factory() as db:
        db.add(LatestMetric(asset_id=aid, metric_key="cpu.load", value=99, unit="%", quality="valid", source="ssh", observed_at=now))
        evaluate(db, now=now)
        db.commit()
        assert db.scalar(select(Alert).where(Alert.asset_id == aid)).state == "active"
        assert enqueue_due(db, now=now) == 1
    assert operator.delete(f"/api/assets/{a['id']}").status_code == 204
    with factory() as db:
        evaluate(db, now=now + timedelta(seconds=15))
        db.commit()
        alert = db.scalar(select(Alert).where(Alert.asset_id == aid))
        assert alert.state == "resolved" and alert.suppression_reason == "资产或指标已移出规则作用范围"
        assert enqueue_due(db, now=now + timedelta(hours=1)) == 0
    assert operator.get("/api/alerts", params={"state": "all"}).json()["total"] == 0


def test_only_admin_purges(operator: TestClient, admin: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    a = operator.post("/api/assets", json=SERVER).json()
    assert admin.delete(f"/api/assets/{a['id']}/purge").status_code == 404  # 在用的不能直接彻底删除
    operator.delete(f"/api/assets/{a['id']}")
    assert operator.delete(f"/api/assets/{a['id']}/purge").status_code == 403
    assert admin.delete(f"/api/assets/{a['id']}/purge").status_code == 204
    with factory() as db:
        assert db.get(Asset, uuid.UUID(a["id"])) is None
        assert db.scalars(select(ManagementEndpoint)).all() == []
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == "asset.purge")) is not None
    assert _bin(admin) == []


def test_auto_purge_after_retention(operator: TestClient, admin: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    old = operator.post("/api/assets", json=SERVER).json()
    new = operator.post("/api/assets", json={**SERVER, "name": "fake-recycle-new", "asset_no": "FAKE-RB-2", "endpoints": []}).json()
    operator.delete(f"/api/assets/{old['id']}")
    operator.delete(f"/api/assets/{new['id']}")
    assert admin.put("/api/assets/recycle-bin/settings", json={"retention_days": 7}).status_code == 200
    now = datetime.now(UTC)
    with factory() as db:
        db.get(Asset, uuid.UUID(old["id"])).deleted_at = now - timedelta(days=7, minutes=1)
        db.get(Asset, uuid.UUID(new["id"])).deleted_at = now - timedelta(days=6)
        db.commit()
        assert recycle.purge_due(db, now=now) == 1
        db.commit()
        assert db.get(Asset, uuid.UUID(old["id"])) is None and db.get(Asset, uuid.UUID(new["id"])) is not None
        assert recycle.purge_due(db, now=now + timedelta(days=1, minutes=1)) == 1
        db.commit()
        assert db.get(Asset, uuid.UUID(new["id"])) is None
        assert len(db.scalars(select(AuditEvent).where(AuditEvent.action == "asset.purge", AuditEvent.actor_id.is_(None))).all()) == 2


def test_retention_setting(operator: TestClient, admin: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    assert operator.get("/api/assets/recycle-bin/settings").json() == {"retention_days": 30}
    assert operator.put("/api/assets/recycle-bin/settings", json={"retention_days": 10}).status_code == 403
    for bad in (0, 366, "x"):
        assert admin.put("/api/assets/recycle-bin/settings", json={"retention_days": bad}).status_code == 422
    assert admin.put("/api/assets/recycle-bin/settings", json={"retention_days": 365}).json() == {"retention_days": 365}
    assert operator.get("/api/assets/recycle-bin").json()["retention_days"] == 365
    with factory() as db:
        assert "30 天 → 365 天" in db.scalar(select(AuditEvent.summary).where(AuditEvent.action == "asset.recycle_settings"))


def test_viewer_has_no_bin(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "fake-rb-viewer", "viewer")
    viewer = login(app_factory(), "fake-rb-viewer")
    assert viewer.get("/api/assets/recycle-bin").status_code == 403
    assert viewer.post(f"/api/assets/{uuid.uuid4()}/restore").status_code == 403


def test_asset_no_conflict_names_bin_asset(operator: TestClient) -> None:
    a = operator.post("/api/assets", json=SERVER).json()
    operator.delete(f"/api/assets/{a['id']}")
    resp = operator.post("/api/assets", json={**SERVER, "name": "fake-other"})
    assert resp.status_code == 409
    assert "回收站" in resp.json()["detail"] and "fake-recycle-srv" in resp.json()["detail"]
    other = operator.post("/api/assets", json={**SERVER, "name": "fake-other", "asset_no": "FAKE-RB-9", "endpoints": []}).json()
    renamed = operator.patch(f"/api/assets/{other['id']}", json={"revision": 1, "asset_no": "FAKE-RB-1"})
    assert renamed.status_code == 409 and "fake-recycle-srv" in renamed.json()["detail"]


def test_rack_slot_freed_and_cleared_on_conflicting_restore(operator: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        site = Site(name="fake-rb-site", kind="datacenter")
        db.add(site)
        db.flush()
        rack = Rack(site_id=site.id, name="fake-rb-rack", height_u=42)
        db.add(rack)
        db.commit()
        rack_id, site_id = str(rack.id), str(site.id)
    place = {"site_id": site_id, "rack_id": rack_id, "rack_unit": 10, "endpoints": []}
    a = operator.post("/api/assets", json={**SERVER, **place}).json()
    operator.delete(f"/api/assets/{a['id']}")
    view = operator.get(f"/api/racks/{rack_id}/elevation").json()
    assert view["items"] == [] and view["free_u"]["front"] == 42
    b = operator.post("/api/assets", json={**SERVER, **place, "name": "fake-rb-new", "asset_no": "FAKE-RB-3"})
    assert b.status_code == 201, b.text  # 回收站里的资产不占 U 位
    resp = operator.post(f"/api/assets/{a['id']}/restore").json()
    assert "U 位" in resp["notice"]
    detail = operator.get(f"/api/assets/{a['id']}").json()
    assert detail["rack_id"] == rack_id and detail["rack_unit"] is None


def test_scoped_operator_sees_and_restores_only_in_scope(app_factory, factory, admin: TestClient) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        a, b = Site(name="fake-rb-A", kind="datacenter"), Site(name="fake-rb-B", kind="datacenter")
        db.add_all([a, b])
        db.flush()
        site_a, site_b = str(a.id), str(b.id)
        db.commit()
    add_user(factory, "fake-rb-scoped", "operator")
    with factory() as db:
        u = db.scalar(select(User).where(User.username == "fake-rb-scoped"))
        u.scope_all = False
        db.add(UserScopeSite(user_id=u.id, site_id=uuid.UUID(site_a)))
        db.commit()
    scoped = login(app_factory(), "fake-rb-scoped")
    in_a = admin.post("/api/assets", json={**SERVER, "site_id": site_a, "endpoints": []}).json()
    in_b = admin.post("/api/assets", json={**SERVER, "name": "fake-rb-b", "asset_no": "FAKE-RB-4", "site_id": site_b, "endpoints": []}).json()
    assert admin.delete(f"/api/assets/{in_a['id']}").status_code == 204
    assert admin.delete(f"/api/assets/{in_b['id']}").status_code == 204
    assert [i["name"] for i in _bin(scoped)] == ["fake-recycle-srv"]
    assert sorted(i["name"] for i in _bin(admin)) == ["fake-rb-b", "fake-recycle-srv"]
    assert scoped.post(f"/api/assets/{in_b['id']}/restore").status_code == 404
    assert scoped.post(f"/api/assets/{in_a['id']}/restore").status_code == 200
    assert [x["name"] for x in scoped.get("/api/assets").json()["items"]] == ["fake-recycle-srv"]
