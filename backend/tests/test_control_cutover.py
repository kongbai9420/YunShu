"""P5 控制切换：默认关闭；切到新平台需要部署开关、检查清单、确认文字与切换前检查；切回旧控制器随时可以。"""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuditEvent, ControlSetting
from tests.api_helpers import add_user, login
from tests.test_control_power import make_asset

ALL = ["legacy_auto", "legacy_stopped", "backup", "catalog"]


@pytest.fixture
def make_client(app_factory, engine):  # type: ignore[no-untyped-def]
    from app.main import create_app

    clients: list[TestClient] = []

    def make(*, enabled: bool, legacy_url: str = "", legacy_alive: bool = False) -> TestClient:
        cfg = dataclasses.replace(app_factory.settings, control_cutover_enabled=enabled, legacy_url=legacy_url)
        app = create_app(cfg, engine=engine, cipher=app_factory.cipher)
        app.state.legacy_probe = lambda url: legacy_alive
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


def owner(client, to, revision, confirm, checklist=ALL):  # type: ignore[no-untyped-def]
    return client.post("/api/control/owner", json={"owner": to, "revision": revision, "confirm": confirm, "checklist": checklist, "note": "演练"})


def test_default_off_cannot_switch(make_client, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "ada", "admin")
    c = login(make_client(enabled=False), "ada")
    info = c.get("/api/control/cutover").json()
    assert info["owner"] == "legacy" and info["cutover_enabled"] is False and info["ready"] is False
    r = owner(c, "new", info["revision"], "切换到新平台")
    assert r.status_code == 422 and "切换开关" in r.json()["detail"]["message"]
    with factory() as db:
        assert db.get(ControlSetting, "default").owner == "legacy"


def test_switch_requires_admin_checklist_confirm_and_legacy_down(make_client, factory, app_factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "ada", "admin")
    add_user(factory, "olivia", "operator")
    alive = login(make_client(enabled=True, legacy_url="http://192.0.2.9:8080", legacy_alive=True), "ada")
    rev = alive.get("/api/control/cutover").json()["revision"]
    r = owner(alive, "new", rev, "切换到新平台")
    assert r.status_code == 422 and "旧版服务已停止响应" in r.json()["detail"]["message"]

    c = login(make_client(enabled=True, legacy_url="http://192.0.2.9:8080", legacy_alive=False), "ada")
    op = login(make_client(enabled=True), "olivia")
    assert owner(op, "new", rev, "切换到新平台").status_code == 403
    assert owner(c, "new", rev, "切换到新平台", checklist=ALL[:3]).json()["detail"]["code"] == "checklist"
    assert owner(c, "new", rev, "切换").json()["detail"]["code"] == "confirm"
    ok = owner(c, "new", rev, "切换到新平台")
    assert ok.status_code == 200 and ok.json()["owner"] == "new"
    assert owner(c, "legacy", rev, "切回旧控制器").status_code == 409  # 修订号已变化
    with factory() as db:
        e = db.scalar(select(AuditEvent).where(AuditEvent.action == "control.owner.change"))
        assert e.detail["from"] == "legacy" and e.detail["to"] == "new"


def test_rollback_always_allowed_and_blocks_queued_ops(make_client, factory, app_factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "ada", "admin")
    c = login(make_client(enabled=True), "ada")
    with factory() as db:
        aid = make_asset(db, app_factory.cipher).id
    owner(c, "new", 1, "切换到新平台")
    q = c.post("/api/control/power", json={"asset_ids": [str(aid)], "action": "on", "request_key": uuid.uuid4().hex}).json()
    assert q["operations"][0]["status"] == "queued"
    # 有排队的操作时不能再切到新平台（这里已经是新平台）；切回旧控制器不受影响，即使部署开关关闭
    off = login(make_client(enabled=False), "ada")
    assert owner(off, "legacy", 2, "切回").json()["detail"]["code"] == "confirm"
    back = owner(off, "legacy", 2, "切回旧控制器")
    assert back.status_code == 200 and back.json()["owner"] == "legacy"
    # 排队中的电源操作执行时会被门禁拒绝（见 test_gate_rechecked_at_execution）
    on_again = login(make_client(enabled=True), "ada")
    r = owner(on_again, "new", 3, "切换到新平台")
    assert r.status_code == 422 and "排队或执行中的控制操作" in r.json()["detail"]["message"]
