"""外观主题（P23）：平台默认与账号选择、主题包校验与导入、删除主题的回退、玻璃壁纸；全部为假数据。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import appearance
from app.models import AuditEvent
from tests.api_helpers import add_user, login

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

PACKAGE = {
    "format": "yunshu-theme", "version": 1, "id": "fake-ocean", "name": "海洋（测试）", "base": "glass",
    "author": "example.invalid", "description": "测试用主题",
    "light": {"--primary": "#0b6bcb", "app-backdrop": "linear-gradient(135deg, rgba(0, 120, 200, 0.3), #eef6ff)"},
    "dark": {"primary": "#0b6bcb", "card": "rgba(20, 30, 40, 0.6)"},
}


@pytest.fixture
def admin(app_factory, factory) -> TestClient:  # type: ignore[no-untyped-def]
    add_user(factory, "fake-theme-admin", "admin")
    return login(app_factory(), "fake-theme-admin")


@pytest.fixture
def viewer(app_factory, factory) -> TestClient:  # type: ignore[no-untyped-def]
    add_user(factory, "fake-theme-viewer", "viewer")
    return login(app_factory(), "fake-theme-viewer")


def _upload(c: TestClient, package: object, name: str = "theme.json"):  # type: ignore[no-untyped-def]
    body = package if isinstance(package, bytes) else json.dumps(package).encode()
    return c.post("/api/appearance/themes", files={"file": (name, body, "application/json")})


def test_default_and_personal_choice(admin: TestClient, viewer: TestClient, factory) -> None:  # type: ignore[no-untyped-def]
    state = viewer.get("/api/appearance").json()
    assert state["default"] == {"style": "simple", "mode": "system"}
    assert state["mine"] == {"style": None, "mode": None}
    assert state["themes"] == [] and state["wallpaper"] is None

    # 只读账号也能改自己的外观，但不能改平台默认
    r = viewer.put("/api/appearance/mine", json={"style": "glass", "mode": "dark"})
    assert r.status_code == 200 and r.json()["mine"] == {"style": "glass", "mode": "dark"}
    assert viewer.put("/api/appearance/default", json={"style": "glass", "mode": "light"}).status_code == 403

    assert admin.put("/api/appearance/default", json={"style": "glass", "mode": "light"}).status_code == 200
    assert viewer.get("/api/appearance").json()["default"] == {"style": "glass", "mode": "light"}
    # 只改深浅、风格跟平台默认；全部清空回到默认
    assert viewer.put("/api/appearance/mine", json={"mode": "dark"}).json()["mine"] == {"style": None, "mode": "dark"}
    assert viewer.put("/api/appearance/mine", json={}).json()["mine"] == {"style": None, "mode": None}

    assert viewer.put("/api/appearance/mine", json={"style": "nope"}).status_code == 422
    assert viewer.put("/api/appearance/mine", json={"mode": "dim"}).status_code == 422
    assert admin.put("/api/appearance/default", json={"style": "simple", "mode": "auto"}).status_code == 422
    with factory() as db:
        actions = [e.action for e in db.scalars(select(AuditEvent).where(AuditEvent.action.like("appearance.%")))]
    assert actions == ["appearance.default"]


def test_import_theme_and_delete_falls_back(admin: TestClient, viewer: TestClient) -> None:
    r = _upload(admin, PACKAGE)
    assert r.status_code == 201, r.text
    theme = r.json()["theme"]
    assert r.json()["created"] is True
    # 变量名统一去掉前导 --
    assert theme["light"]["primary"] == "#0b6bcb" and "app-backdrop" in theme["light"]
    assert [t["id"] for t in viewer.get("/api/appearance").json()["themes"]] == ["fake-ocean"]
    assert _upload(viewer, PACKAGE).status_code == 403

    # 同 id 再导入是更新
    r = _upload(admin, {**PACKAGE, "name": "海洋 2"})
    assert r.status_code == 201 and r.json()["created"] is False

    assert viewer.put("/api/appearance/mine", json={"style": "fake-ocean", "mode": "dark"}).status_code == 200
    assert admin.put("/api/appearance/default", json={"style": "fake-ocean", "mode": "system"}).status_code == 200
    r = admin.delete("/api/appearance/themes/fake-ocean")
    assert r.status_code == 200 and r.json()["affected_users"] == 1
    state = viewer.get("/api/appearance").json()
    assert state["themes"] == []
    assert state["default"] == {"style": "simple", "mode": "system"}
    assert state["mine"] == {"style": None, "mode": "dark"}  # 深浅选择保留
    assert admin.delete("/api/appearance/themes/fake-ocean").status_code == 404


@pytest.mark.parametrize(("change", "message"), [
    ({"format": "other"}, "不是云枢主题包"),
    ({"id": "Bad Id"}, "id 只能用"),
    ({"id": "glass"}, "内置主题"),
    ({"name": ""}, "name"),
    ({"base": "neon"}, "base"),
    ({"light": {"--not-a-token": "#fff"}}, "不在可用变量"),
    ({"light": {"background": "url(x.png)"}}, "不允许的函数 url"),
    ({"light": {"background": "var(--card)"}}, "不允许的函数 var"),
    ({"light": {"background": "#fff; } body { display: none"}}, "不允许的字符"),
    ({"light": {"background": "\"Arial\""}}, "不允许的字符"),
    ({"light": {"background": "x" * 700}}, "太长"),
    ({"light": {"background": 12}}, "必须是文字"),
    ({"light": {}, "dark": {}}, "至少要有一个"),
])
def test_package_validation(change: dict, message: str) -> None:
    with pytest.raises(appearance.ThemeError, match=message):
        appearance.parse_package({**PACKAGE, **change})


def test_import_rejects_bad_files(admin: TestClient) -> None:
    r = _upload(admin, b"not json")
    assert r.status_code == 422 and "JSON" in r.json()["detail"]
    r = _upload(admin, b"{" + b" " * (65 * 1024) + b"}")
    assert r.status_code == 413
    r = _upload(admin, {**PACKAGE, "light": {"card": "url(x)"}})
    assert r.status_code == 422 and "url" in r.json()["detail"]


def test_wallpaper_upload_serve_and_remove(admin: TestClient, viewer: TestClient) -> None:
    r = admin.post("/api/appearance/wallpaper", files={"file": ("bg.png", PNG, "image/png")})
    assert r.status_code == 200, r.text
    url = r.json()["wallpaper"]["url"]
    assert url.startswith("/api/appearance/wallpaper?v=")
    assert viewer.get("/api/appearance").json()["wallpaper"]["url"] == url
    img = viewer.get(url)
    assert img.status_code == 200 and img.content == PNG
    assert img.headers["content-type"] == "image/png" and img.headers["x-content-type-options"] == "nosniff"

    # 按文件头判断类型：改了扩展名的 HTML 不收；太大的不收；只读账号不能换
    bad = admin.post("/api/appearance/wallpaper", files={"file": ("bg.png", b"<svg onload=alert(1)>", "image/png")})
    assert bad.status_code == 422 and "PNG" in bad.json()["detail"]
    big = admin.post("/api/appearance/wallpaper", files={"file": ("bg.png", PNG + b"\x00" * (4 * 1024 * 1024), "image/png")})
    assert big.status_code == 422 and "太大" in big.json()["detail"]
    assert viewer.post("/api/appearance/wallpaper", files={"file": ("bg.png", PNG, "image/png")}).status_code == 403

    assert admin.delete("/api/appearance/wallpaper").status_code == 204
    assert viewer.get("/api/appearance").json()["wallpaper"] is None
    assert viewer.get(url).status_code == 404


def test_requires_login(app_factory) -> None:  # type: ignore[no-untyped-def]
    c = app_factory()
    assert c.get("/api/appearance").status_code == 401
    assert c.get("/api/appearance/wallpaper").status_code == 401
