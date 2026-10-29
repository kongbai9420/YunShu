from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bootstrap import INITIAL_PASSWORD, INITIAL_USERNAME
from app.models import AuditEvent, LoginSession, User
from tests.api_helpers import FAKE_PASSWORD, add_user, login


def test_first_start_creates_admin_with_legacy_initial_password(client: TestClient, app_factory) -> None:  # type: ignore[no-untyped-def]
    assert not (app_factory.settings.data_dir / "initial-admin-password.txt").exists()
    resp = client.post("/api/auth/login", json={"username": INITIAL_USERNAME, "password": INITIAL_PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["user"]["require_password_change"] is True

    # 改密码之前不能做任何业务操作
    client.headers["X-CSRF-Token"] = resp.json()["csrf_token"]
    assert client.get("/api/assets").status_code == 403
    assert client.post("/api/auth/password", json={"old_password": INITIAL_PASSWORD, "new_password": "new-fake-pass-99"}).status_code == 204
    assert client.get("/api/assets").status_code == 200


def test_initial_admin_created_only_once(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    app_factory()
    with factory() as db:
        assert db.scalars(select(User)).all().__len__() == 1


def test_wrong_password_rejected(client: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    resp = client.post("/api/auth/login", json={"username": "olivia", "password": "wrong-fake"})
    assert resp.status_code == 401
    resp = client.post("/api/auth/login", json={"username": "nobody", "password": "wrong-fake"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "用户名或密码错误"


def test_lockout_after_five_failures(client: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "olivia", "password": "wrong-fake"})
    resp = client.post("/api/auth/login", json={"username": "olivia", "password": FAKE_PASSWORD})
    assert resp.status_code == 429
    # 同一来源的其他账号不受影响（反向代理后多人共用一个来源地址）
    add_user(factory, "peter", "operator")
    assert client.post("/api/auth/login", json={"username": "peter", "password": FAKE_PASSWORD}).status_code == 200


def test_source_is_locked_after_many_failures_across_accounts(client: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    for i in range(30):  # 换着用户名试，每个账号都没到 5 次
        assert client.post("/api/auth/login", json={"username": f"fake-user-{i % 10}", "password": "wrong-fake"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "olivia", "password": FAKE_PASSWORD}).status_code == 429


def test_throttle_unlocks_after_lockout_period() -> None:
    from app.auth import LOCKOUT_SECONDS, LoginThrottle

    now = [0.0]
    t = LoginThrottle(clock=lambda: now[0])
    for _ in range(5):
        t.record_failure("192.0.2.9", "Olivia")
    assert t.remaining_lock("192.0.2.9", "olivia") > 0 and t.remaining_lock("192.0.2.9", "peter") == 0
    assert t.remaining_lock("198.51.100.9", "olivia") == 0
    now[0] += LOCKOUT_SECONDS + 1
    assert t.remaining_lock("192.0.2.9", "olivia") == 0


def test_session_token_stored_only_as_hash(client: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    login(client, "olivia")
    token = client.cookies.get("yunshu_session")
    assert token
    with factory() as db:
        rows = db.scalars(select(LoginSession)).all()
        assert len(rows) == 1
        assert rows[0].token_hash != token
        assert token not in rows[0].token_hash


def test_me_and_logout(client: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    login(client, "olivia")
    assert client.get("/api/auth/me").json()["user"]["username"] == "olivia"
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_unauthenticated_requests_rejected(client: TestClient) -> None:
    assert client.get("/api/assets").status_code == 401
    assert client.post("/api/assets", json={}).status_code == 401


def test_write_without_csrf_header_rejected(client: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    login(client, "olivia")
    del client.headers["X-CSRF-Token"]
    resp = client.post("/api/assets", json={"name": "甲", "form": "physical", "role": "server"})
    assert resp.status_code == 403
    client.headers["X-CSRF-Token"] = "forged"
    resp = client.post("/api/assets", json={"name": "甲", "form": "physical", "role": "server"})
    assert resp.status_code == 403


def test_password_hash_compatible_with_legacy_format() -> None:
    from app.auth import hash_password, verify_password

    # 旧版 auth_manager 的格式：pbkdf2_sha256$<salt>$<hex>，十万次
    legacy = hash_password(FAKE_PASSWORD, salt="0123456789abcdef0123456789abcdef")
    assert legacy.startswith("pbkdf2_sha256$0123456789abcdef0123456789abcdef$")
    assert verify_password(FAKE_PASSWORD, legacy)
    assert not verify_password("other", legacy)


def test_login_failures_are_audited_without_password(client: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    client.post("/api/auth/login", json={"username": "olivia", "password": "wrong-fake"})
    with factory() as db:
        events = db.scalars(select(AuditEvent).where(AuditEvent.action == "auth.login_failed")).all()
        assert len(events) == 1
        assert "wrong-fake" not in str(events[0].detail)


def test_admin_can_manage_users_but_not_remove_last_admin(client: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        db.query(User).delete()
        db.commit()
    add_user(factory, "alice", "admin")
    login(client, "alice")
    resp = client.post("/api/users", json={"username": "victor", "password": "fake-pass-5678", "role": "viewer"})
    assert resp.status_code == 201
    assert resp.json()["require_password_change"] is True

    alice = next(u for u in client.get("/api/users").json() if u["username"] == "alice")
    resp = client.patch(f"/api/users/{alice['id']}", json={"role": "viewer"})
    assert resp.status_code == 409
