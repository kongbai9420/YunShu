from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.auth import hash_password
from app.models import User

FAKE_PASSWORD = "fake-pass-1234"


def add_user(factory: sessionmaker[Session], username: str, role: str, *, must_change: bool = False) -> None:
    with factory() as db:
        db.add(
            User(
                username=username,
                role=role,
                password_hash=hash_password(FAKE_PASSWORD),
                require_password_change=must_change,
            )
        )
        db.commit()


def login(client: TestClient, username: str, password: str = FAKE_PASSWORD) -> TestClient:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    client.headers["X-CSRF-Token"] = resp.json()["csrf_token"]
    return client
