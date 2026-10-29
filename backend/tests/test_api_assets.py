from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuditEvent, Credential, ManagementEndpoint
from app.services.credentials import reveal_secret
from tests.api_helpers import add_user, login

SERVER = {"name": "R740-计算01", "asset_no": "A-001", "form": "physical", "role": "server", "vendor": "Dell", "model": "R740", "tags": ["机房A"]}


@pytest.fixture
def operator(app_factory, factory) -> TestClient:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    return login(app_factory(), "olivia")


@pytest.fixture
def viewer(app_factory, factory) -> TestClient:  # type: ignore[no-untyped-def]
    add_user(factory, "victor", "viewer")
    return login(app_factory(), "victor")


def test_create_get_list_asset(operator: TestClient) -> None:
    body = {**SERVER, "endpoints": [{"address": "192.0.2.20", "port": 623, "protocol": "ipmi"}]}
    resp = operator.post("/api/assets", json=body)
    assert resp.status_code == 201, resp.text
    asset = resp.json()
    assert asset["revision"] == 1
    assert asset["endpoints"][0]["protocol"] == "ipmi"

    detail = operator.get(f"/api/assets/{asset['id']}").json()
    assert detail["name"] == SERVER["name"]
    assert detail["relations"] == []

    page = operator.get("/api/assets", params={"q": "192.0.2.20"}).json()
    assert page["total"] == 1
    assert operator.get("/api/assets", params={"tag": "机房A"}).json()["total"] == 1
    assert operator.get("/api/assets", params={"tag": "机房"}).json()["total"] == 0
    assert operator.get("/api/assets", params={"role": "switch"}).json()["total"] == 0
    assert operator.get("/api/assets", params={"role": "switch,server"}).json()["total"] == 1
    assert operator.get("/api/assets", params={"form": "vm,container"}).json()["total"] == 0


def test_invalid_vocabulary_rejected(operator: TestClient) -> None:
    assert operator.post("/api/assets", json={**SERVER, "form": "network"}).status_code == 422
    assert operator.post("/api/assets", json={**SERVER, "role": "compute"}).status_code == 422


def test_duplicate_asset_no_conflict(operator: TestClient) -> None:
    assert operator.post("/api/assets", json=SERVER).status_code == 201
    assert operator.post("/api/assets", json={**SERVER, "name": "另一台"}).status_code == 409


def test_same_ip_creates_two_assets(operator: TestClient) -> None:
    for no in ("A-1", "A-2"):
        body = {**SERVER, "asset_no": no, "endpoints": [{"address": "192.0.2.30", "protocol": "snmp"}]}
        assert operator.post("/api/assets", json=body).status_code == 201
    assert operator.get("/api/assets").json()["total"] == 2


def test_update_requires_current_revision(operator: TestClient) -> None:
    asset = operator.post("/api/assets", json=SERVER).json()
    resp = operator.patch(f"/api/assets/{asset['id']}", json={"revision": 1, "name": "改名"})
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2
    # 拿着旧修订号再改：冲突，不静默覆盖
    stale = operator.patch(f"/api/assets/{asset['id']}", json={"revision": 1, "name": "又改名"})
    assert stale.status_code == 409
    assert operator.get(f"/api/assets/{asset['id']}").json()["name"] == "改名"


def test_endpoint_changes_bump_revision(operator: TestClient) -> None:
    asset = operator.post("/api/assets", json=SERVER).json()
    ep = operator.post(f"/api/assets/{asset['id']}/endpoints", json={"address": "192.0.2.40", "protocol": "ssh", "port": 22}).json()
    assert operator.get(f"/api/assets/{asset['id']}").json()["revision"] == 2
    assert operator.patch(f"/api/endpoints/{ep['id']}", json={"port": 2222}).json()["port"] == 2222
    assert operator.delete(f"/api/endpoints/{ep['id']}").status_code == 204
    detail = operator.get(f"/api/assets/{asset['id']}").json()
    assert detail["revision"] == 4
    assert detail["endpoints"] == []


def test_relations(operator: TestClient) -> None:
    host = operator.post("/api/assets", json=SERVER).json()
    vm = operator.post("/api/assets", json={"name": "ubuntu-vm", "form": "vm", "role": "server", "platform": "ubuntu"}).json()
    rel = operator.post("/api/relations", json={"source_asset_id": host["id"], "target_asset_id": vm["id"], "kind": "hosts"})
    assert rel.status_code == 201
    dup = operator.post("/api/relations", json={"source_asset_id": host["id"], "target_asset_id": vm["id"], "kind": "hosts"})
    assert dup.status_code == 409
    self_rel = operator.post("/api/relations", json={"source_asset_id": host["id"], "target_asset_id": host["id"], "kind": "hosts"})
    assert self_rel.status_code == 422
    assert len(operator.get(f"/api/assets/{vm['id']}").json()["relations"]) == 1

    # 删除宿主资产（移入回收站），关系不再显示，虚拟机本身保留
    assert operator.delete(f"/api/assets/{host['id']}").status_code == 204
    assert operator.get(f"/api/assets/{vm['id']}").json()["relations"] == []


def test_sites_and_racks(operator: TestClient) -> None:
    site = operator.post("/api/sites", json={"name": "家庭机房", "kind": "home"}).json()
    room = operator.post("/api/sites", json={"name": "书房", "kind": "room", "parent_id": site["id"]}).json()
    assert operator.patch(f"/api/sites/{site['id']}", json={"parent_id": room["id"]}).status_code == 422
    rack = operator.post("/api/racks", json={"site_id": site["id"], "name": "R1", "height_u": 24}).json()
    assert operator.post("/api/racks", json={"site_id": site["id"], "name": "R1"}).status_code == 409

    asset = operator.post("/api/assets", json={**SERVER, "site_id": site["id"], "rack_id": rack["id"], "rack_unit": 10})
    assert asset.status_code == 201
    wrong_site = operator.post("/api/assets", json={**SERVER, "asset_no": "A-9", "site_id": room["id"], "rack_id": rack["id"]})
    assert wrong_site.status_code == 422
    assert operator.delete(f"/api/sites/{site['id']}").status_code == 409


def test_credentials_never_returned(operator: TestClient, factory, app_factory) -> None:  # type: ignore[no-untyped-def]
    secret = "fake-bmc-secret-31337"
    resp = operator.post("/api/credentials", json={"alias": "idrac-default", "kind": "password", "username": "u", "secret": secret})
    assert resp.status_code == 201
    cred = resp.json()
    assert set(cred) == {"id", "alias", "kind", "username", "notes", "key_id", "last_used_at", "updated_at"}

    listed = operator.get("/api/credentials")
    assert secret not in listed.text
    assert "ciphertext" not in listed.text and "nonce" not in listed.text

    patched = operator.patch(f"/api/credentials/{cred['id']}", json={"secret": "fake-rotated-secret"})
    assert patched.status_code == 200
    assert "fake-rotated-secret" not in patched.text

    asset = operator.post("/api/assets", json={**SERVER, "endpoints": [{"address": "192.0.2.50", "protocol": "ipmi", "credential_id": cred["id"]}]})
    assert asset.status_code == 201
    assert secret not in operator.get(f"/api/assets/{asset.json()['id']}").text

    with factory() as db:
        for event in db.scalars(select(AuditEvent)):
            assert secret not in str(event.detail)
            assert "fake-rotated-secret" not in str(event.detail)


def test_unknown_credential_reference_rejected(operator: TestClient) -> None:
    body = {**SERVER, "endpoints": [{"address": "192.0.2.60", "protocol": "ipmi", "credential_id": "00000000-0000-0000-0000-000000000000"}]}
    assert operator.post("/api/assets", json=body).status_code == 422


def test_viewer_is_read_only(operator: TestClient, viewer: TestClient) -> None:
    asset = operator.post("/api/assets", json=SERVER).json()
    assert viewer.get("/api/assets").status_code == 200
    assert viewer.get(f"/api/assets/{asset['id']}").status_code == 200
    assert viewer.get("/api/credentials").status_code == 200
    # 后端拒绝，而不是只靠前端隐藏按钮
    assert viewer.post("/api/assets", json={**SERVER, "asset_no": "V-1"}).status_code == 403
    assert viewer.patch(f"/api/assets/{asset['id']}", json={"revision": 1, "name": "x"}).status_code == 403
    assert viewer.delete(f"/api/assets/{asset['id']}").status_code == 403
    assert viewer.post("/api/credentials", json={"alias": "a", "kind": "password", "secret": "fake"}).status_code == 403
    assert viewer.post("/api/sites", json={"name": "x"}).status_code == 403


def test_only_admin_sees_users_and_audit(operator: TestClient, viewer: TestClient) -> None:
    assert operator.get("/api/users").status_code == 403
    assert operator.get("/api/audit").status_code == 403
    assert viewer.get("/api/audit").status_code == 403


def test_writes_are_audited(operator: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    asset = operator.post("/api/assets", json=SERVER).json()
    operator.patch(f"/api/assets/{asset['id']}", json={"revision": 1, "notes": "备注"})
    operator.delete(f"/api/assets/{asset['id']}")
    with factory() as db:
        actions = [e.action for e in db.scalars(select(AuditEvent).where(AuditEvent.target_id == asset["id"]))]
    assert sorted(actions) == ["asset.create", "asset.delete", "asset.update"]


INLINE = {"alias": "fake-linux-ssh", "kind": "password", "username": "fake-monitor", "secret": "fake-device-password-123"}


def inline_asset(**overrides):  # type: ignore[no-untyped-def]
    return {**SERVER, "endpoints": [{"protocol": "ssh", "address": "192.0.2.71", "port": 2222, "new_credential": INLINE}], **overrides}


def test_inline_credential_encrypted_and_linked(operator, factory, app_factory):  # type: ignore[no-untyped-def]
    resp = operator.post("/api/assets", json=inline_asset())
    assert resp.status_code == 201, resp.text
    ep = resp.json()["endpoints"][0]
    assert ep["port"] == 2222
    assert INLINE["secret"] not in resp.text
    with factory() as db:
        cred = db.get(Credential, uuid.UUID(ep["credential_id"]))
        assert cred.alias == INLINE["alias"] and cred.username == INLINE["username"]
        assert reveal_secret(cred, app_factory.cipher) == INLINE["secret"]
        assert INLINE["secret"].encode() not in cred.secret_ciphertext
        events = list(db.scalars(select(AuditEvent)))
        assert {"asset.create", "credential.create"} <= {e.action for e in events}
        assert INLINE["secret"] not in str([(e.summary, e.detail) for e in events])


def test_inline_failure_rolls_back_asset_endpoint_and_credentials(operator, factory):  # type: ignore[no-untyped-def]
    # 先制造资产编号冲突；即使新凭据已 flush 也必须整体回滚。
    assert operator.post("/api/assets", json=SERVER).status_code == 201
    assert operator.post("/api/assets", json=inline_asset()).status_code == 409
    with factory() as db:
        assert list(db.scalars(select(Credential))) == []
        assert list(db.scalars(select(ManagementEndpoint))) == []
        assert not list(db.scalars(select(AuditEvent).where(AuditEvent.action == "credential.create")))
    cred = operator.post("/api/credentials", json=INLINE).json()
    failed = operator.post("/api/assets", json=inline_asset(asset_no="A-NEW"))
    assert failed.status_code == 409 and "凭据别名" in failed.text
    assert operator.get("/api/assets").json()["total"] == 1
    # 改为引用已有凭据，重试成功，不创建多余凭据。
    retry = inline_asset(asset_no="A-NEW", endpoints=[{"protocol": "ssh", "address": "192.0.2.71", "credential_id": cred["id"]}])
    assert operator.post("/api/assets", json=retry).status_code == 201
    assert len(operator.get("/api/credentials").json()) == 1


@pytest.mark.parametrize("changes", [
    {"credential_id": "00000000-0000-0000-0000-000000000000"},
    {"port": 0},
    {"protocol": "snmp"},
    {"new_credential": {**INLINE, "username": " "}},
    {"new_credential": {**INLINE, "secret": "fake-oversize-" * 700}},
])
def test_inline_validation_never_echoes_secrets(operator, changes):  # type: ignore[no-untyped-def]
    body = inline_asset()
    body["endpoints"][0].update(changes)
    response = operator.post("/api/assets", json=body)
    assert response.status_code == 422
    assert INLINE["secret"] not in response.text and "fake-oversize-" not in response.text
    assert operator.get("/api/assets").json()["total"] == 0
    assert operator.get("/api/credentials").json() == []


def test_inline_permission_and_csrf(operator, viewer):  # type: ignore[no-untyped-def]
    assert viewer.post("/api/assets", json=inline_asset()).status_code == 403
    assert operator.post("/api/assets", json=inline_asset(), headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert operator.get("/api/credentials").json() == []


@pytest.mark.parametrize("protocol,kind,secret", [
    ("ssh", "ssh_key", "-----BEGIN FAKE PRIVATE KEY-----\nfake-multiline\n-----END FAKE PRIVATE KEY-----"),
    ("snmp", "snmp_community", "fake-community"),
    ("snmp", "snmpv3", '{"auth_key":"fake-auth-key","priv_key":"fake-priv-key"}'),
    ("pve-api", "token", "fake-api-token"),
])
def test_inline_protocol_secrets_preserve_exact_content(operator, factory, app_factory, protocol, kind, secret):  # type: ignore[no-untyped-def]
    cred = {**INLINE, "kind": kind, "secret": secret}
    body = inline_asset(endpoints=[{"protocol": protocol, "address": "192.0.2.75", "new_credential": cred}])
    response = operator.post("/api/assets", json=body)
    assert response.status_code == 201, response.text
    with factory() as db:
        saved = db.scalar(select(Credential))
        assert saved.kind == kind
        assert reveal_secret(saved, app_factory.cipher) == secret


def test_inline_multiple_endpoints_conflict_rolls_back_first_credential(operator):  # type: ignore[no-untyped-def]
    body = inline_asset()
    body["endpoints"] *= 2
    assert operator.post("/api/assets", json=body).status_code == 409
    assert operator.get("/api/credentials").json() == []
    assert operator.get("/api/assets").json()["total"] == 0


def test_list_sort_and_order(operator: TestClient) -> None:
    for name, no, vendor in (("乙-存储", "A-200", ""), ("甲-计算", None, "Dell"), ("丙-交换", "A-100", "H3C")):
        body = {"name": name, "asset_no": no, "form": "physical", "role": "server", "vendor": vendor}
        assert operator.post("/api/assets", json=body).status_code == 201

    def names(**params: str) -> list[str]:
        resp = operator.get("/api/assets", params=params)
        assert resp.status_code == 200, resp.text
        return [a["name"] for a in resp.json()["items"]]

    all_names = ["乙-存储", "甲-计算", "丙-交换"]
    assert names() == sorted(all_names)  # 默认按名称升序
    assert names(sort="name", order="desc") == sorted(all_names, reverse=True)
    # 无资产编号 / 未填品牌的排在最后，升序降序都一样
    assert names(sort="asset_no") == ["丙-交换", "乙-存储", "甲-计算"]
    assert names(sort="asset_no", order="desc") == ["乙-存储", "丙-交换", "甲-计算"]
    assert names(sort="vendor") == ["甲-计算", "丙-交换", "乙-存储"]
    assert names(sort="vendor", order="desc") == ["丙-交换", "甲-计算", "乙-存储"]
    # 分页与排序一起用
    page = operator.get("/api/assets", params={"sort": "asset_no", "limit": 1, "offset": 1}).json()
    assert page["total"] == 3 and page["items"][0]["name"] == "乙-存储"
    assert len(names(sort="updated_at", order="desc")) == 3


@pytest.mark.parametrize("params", [{"sort": "notes"}, {"sort": "name; drop table assets"}, {"order": "random"}])
def test_list_sort_whitelist(operator: TestClient, params: dict[str, str]) -> None:
    assert operator.get("/api/assets", params=params).status_code == 422
