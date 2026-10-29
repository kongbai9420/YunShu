from __future__ import annotations

import pytest
from sqlalchemy import select

from app.collect.base import AdapterError, ProbeResult
from app.discovery import expand_scope, make_handler, probe_address
from app.models import Asset, DiscoveryRun, Task
from app.worker import Worker
from tests.api_helpers import add_user, login


@pytest.fixture
def operator(app_factory, factory):
    add_user(factory, "discovery-operator", "operator")
    return login(app_factory(), "discovery-operator")


def start(client, **changes):
    response = client.post("/api/discovery/runs", json={"scope": ["192.0.2.0/29"], "ports": [443], **changes})
    assert response.status_code == 202, response.text
    return response.json()


def fake_probe(address, protocol, ports, timeout, credential):
    return {"address": address, "ports": ports, "facts": {"sys_name": "测试设备"}}


def worker(app_factory, factory, probe=fake_probe):
    return Worker(factory, {"discovery.batch": make_handler(lambda: app_factory.cipher, probe)})


def test_scope_deduplicates_excludes_and_bounds():
    assert expand_scope(["192.0.2.0/29", "192.0.2.1"], ["192.0.2.2/31"]) == ["192.0.2.1", "192.0.2.4", "192.0.2.5", "192.0.2.6"]
    assert expand_scope(["192.0.2.1"], []) == ["192.0.2.1"]
    for scope, excludes in [(["192.0.0.0/16"], []), (["::1"], []), (["example.invalid"], []), (["224.0.0.1"], []), (["192.0.2.1"], ["192.0.2.0/24"])]:
        with pytest.raises(ValueError):
            expand_scope(scope, excludes)


def test_preview_is_offline_and_scope_validated(operator, factory):
    body = {"scope": ["192.0.2.0/29"], "excludes": ["192.0.2.2"], "ports": [443]}
    assert operator.post("/api/discovery/preview", json=body).json()["total"] == 5
    with factory() as db:
        assert db.query(Task).count() == db.query(DiscoveryRun).count() == db.query(Asset).count() == 0
    for changes in ({"ports": [0]}, {"ports": [443, 443]}, {"concurrency": 17}, {"timeout": 10}, {"scope": ["192.0.0.0/16"]}, {"protocol": "snmp"}):
        assert operator.post("/api/discovery/runs", json={**body, **changes}).status_code == 422


def test_discovery_requires_editor_and_csrf(app_factory, factory, operator):
    add_user(factory, "discovery-viewer", "viewer")
    viewer = login(app_factory(), "discovery-viewer")
    assert viewer.get("/api/discovery/runs").status_code == 200
    assert viewer.post("/api/discovery/preview", json={"scope": ["192.0.2.1"]}).status_code == 403
    assert viewer.post("/api/discovery/runs", json={"scope": ["192.0.2.1"]}).status_code == 403
    run = start(operator)
    assert viewer.post(f"/api/discovery/runs/{run['id']}/state", json={"action": "cancel", "generation": 1}).status_code == 403
    operator.headers["X-CSRF-Token"] = "invalid"
    assert operator.post("/api/discovery/runs", json={"scope": ["192.0.2.1"]}).status_code == 403


def test_batches_only_create_candidates_then_confirm_once(operator, app_factory, factory):
    run = start(operator, scope=["192.0.2.0/27"], excludes=["192.0.2.2"])
    w = worker(app_factory, factory)
    assert w.run_once() == "succeeded"
    assert operator.get(f"/api/discovery/runs/{run['id']}").json()["processed"] == 16
    assert w.run_once() == "succeeded"
    detail = operator.get(f"/api/discovery/runs/{run['id']}").json()
    assert detail["state"] == "completed" and detail["responded"] == 29
    assert "192.0.2.2" not in {o["address"] for o in detail["items"]}
    with factory() as db:
        assert db.query(Asset).count() == 0
    o = detail["items"][0]
    path = f"/api/discovery/observations/{o['id']}/confirm"
    body = {"action": "create", "name": "测试交换机", "asset_no": "FAKE-D01", "port": 443}
    confirmed = operator.post(path, json=body)
    assert confirmed.status_code == 200, confirmed.text
    assert operator.post(path, json=body).status_code == 409
    asset = operator.get(f"/api/assets/{confirmed.json()['asset_id']}").json()
    assert asset["collection_profile"] == "reachability" and asset["endpoints"][0]["address"] == o["address"]
    with factory() as db:
        assert db.query(Asset).count() == 1


def test_same_ip_never_automatically_merges_and_link_preserves_asset(operator, app_factory, factory):
    existing = operator.post("/api/assets", json={"name": "原资产", "form": "physical", "role": "server", "endpoints": [{"address": "192.0.2.1", "protocol": "ssh"}]}).json()
    before = operator.get(f"/api/assets/{existing['id']}").json()
    run = start(operator, scope=["192.0.2.1"])
    worker(app_factory, factory).run_once()
    item = operator.get(f"/api/discovery/runs/{run['id']}").json()["items"][0]
    assert item["matches"][0]["id"] == existing["id"]
    path = f"/api/discovery/observations/{item['id']}/confirm"
    assert operator.post(path, json={"action": "create", "port": 443}).status_code == 409
    assert operator.post(path, json={"action": "link", "asset_id": existing["id"]}).status_code == 200
    assert operator.get(f"/api/assets/{existing['id']}").json() == before
    run2 = start(operator, scope=["192.0.2.1"])
    worker(app_factory, factory).run_once()
    item2 = operator.get(f"/api/discovery/runs/{run2['id']}").json()["items"][0]
    assert operator.post(f"/api/discovery/observations/{item2['id']}/confirm", json={"action": "create", "port": 443, "allow_duplicate": True}).status_code == 200
    with factory() as db:
        assert db.query(Asset).count() == 2


def test_pause_resume_cancel_and_old_generation_do_not_probe(operator, app_factory, factory):
    seen = []
    def probe(*args):
        seen.append(args[0])
        return fake_probe(*args)
    run = start(operator)
    path = f"/api/discovery/runs/{run['id']}/state"
    assert operator.post(path, json={"action": "pause", "generation": 1}).json()["state"] == "paused"
    w = worker(app_factory, factory, probe)
    assert w.run_once() == "succeeded" and seen == []
    assert operator.post(path, json={"action": "resume", "generation": 1}).status_code == 409
    assert operator.post(path, json={"action": "resume", "generation": 2}).status_code == 200
    assert operator.post(path, json={"action": "cancel", "generation": 3}).status_code == 200
    assert w.run_once() == "succeeded" and seen == []
    assert operator.post(path, json={"action": "resume", "generation": 4}).status_code == 409


def test_pause_during_batch_stops_waiting_probes_and_discards_old_results(operator, app_factory, factory):
    run = start(operator, concurrency=1)
    seen = []
    def pause_inside(*args):
        seen.append(args[0])
        assert operator.post(f"/api/discovery/runs/{run['id']}/state", json={"action": "pause", "generation": 1}).status_code == 200
        return fake_probe(*args)
    assert worker(app_factory, factory, pause_inside).run_once() == "succeeded"
    detail = operator.get(f"/api/discovery/runs/{run['id']}").json()
    assert len(seen) == 1 and detail["processed"] == 0 and detail["items"] == []
    assert operator.post(f"/api/discovery/runs/{run['id']}/state", json={"action": "resume", "generation": 2}).status_code == 200
    w = worker(app_factory, factory)
    while w.run_once() is not None:
        pass
    assert operator.get(f"/api/discovery/runs/{run['id']}").json()["responded"] == 6


def test_lost_lease_does_not_write_observations(operator, app_factory, factory):
    run = start(operator, scope=["192.0.2.1"])
    def invalidate(*args):
        with factory() as db:
            task = db.scalar(select(Task).where(Task.status == "running"))
            task.lease_token = "new-token"
            db.commit()
        return fake_probe(*args)
    worker(app_factory, factory, invalidate).run_once()
    detail = operator.get(f"/api/discovery/runs/{run['id']}").json()
    assert detail["items"] == [] and detail["processed"] == 0


def test_snmp_probe_filters_facts_and_tcp_only_uses_selected_ports():
    class FakeSnmp:
        def probe(self, ctx):
            assert ctx.address == "192.0.2.10" and ctx.secret == "fake-community"
            return ProbeResult(True, facts={"sys_name": "fake-device", "password": "fake-community"})
    result = probe_address("192.0.2.10", "snmp", [161], 0.2, ("", "fake-community", "snmp_community"), snmp_factory=FakeSnmp)
    assert result["facts"] == {"sys_name": "fake-device"}
    seen = []
    class Conn:
        def close(self): pass
    def connect(addr, timeout):
        seen.append(addr)
        if addr[1] == 22:
            raise TimeoutError()
        return Conn()
    result = probe_address("192.0.2.10", "tcp", [22, 443], 0.2, None, connect=connect)
    assert seen == [("192.0.2.10", 22), ("192.0.2.10", 443)] and result["ports"] == [443]


def test_ignore_and_snmp_confirmation_evidence(operator, app_factory, factory):
    cred = operator.post("/api/credentials", json={"alias": "fake-snmp", "kind": "snmp_community", "secret": "fake-community"}).json()
    run = start(operator, scope=["192.0.2.1", "192.0.2.2"], protocol="snmp", credential_id=cred["id"], ports=[161])
    worker(app_factory, factory).run_once()
    items = operator.get(f"/api/discovery/runs/{run['id']}").json()["items"]
    path = f"/api/discovery/observations/{items[0]['id']}/confirm"
    assert operator.post(path, json={"action": "create", "port": 161}).status_code == 422
    assert operator.post(path, json={"action": "create", "port": 443, "endpoint_protocol": "snmp"}).status_code == 422
    created = operator.post(path, json={"action": "create", "port": 161, "endpoint_protocol": "snmp"})
    assert created.status_code == 200
    asset = operator.get(f"/api/assets/{created.json()['asset_id']}").json()
    assert asset["endpoints"][0]["credential_id"] == cred["id"]
    ignored = operator.post(f"/api/discovery/observations/{items[1]['id']}/confirm", json={"action": "ignore"})
    assert ignored.json()["status"] == "ignored"
    assert "fake-community" not in operator.get(f"/api/discovery/runs/{run['id']}").text


def test_deleted_discovery_credential_fails_before_network_and_can_be_cancelled(operator, app_factory, factory):
    cred = operator.post("/api/credentials", json={"alias": "fake-deleted", "kind": "snmp_community", "secret": "fake-community"}).json()
    run = start(operator, scope=["192.0.2.1"], protocol="snmp", credential_id=cred["id"], ports=[161])
    assert operator.delete(f"/api/credentials/{cred['id']}").status_code == 204
    seen = []
    def probe(*args):
        seen.append(args)
        return fake_probe(*args)
    assert worker(app_factory, factory, probe).run_once() == "failed"
    detail = operator.get(f"/api/discovery/runs/{run['id']}").json()
    assert detail["state"] == "failed" and detail["processed"] == 0 and seen == []
    assert operator.post(f"/api/discovery/runs/{run['id']}/state", json={"action": "cancel", "generation": 1}).status_code == 200
