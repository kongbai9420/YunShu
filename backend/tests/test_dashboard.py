from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app import metrics
from app.metrics import Point
from app.models import Asset, CollectionStatus, ManagementEndpoint
from tests.api_helpers import add_user, login


def layout(**over):  # type: ignore[no-untyped-def]
    items = [{"i": w, "x": 0, "y": n * 3, "w": 4, "h": 3} for n, w in enumerate(("health", "issues", "temps", "power", "quality", "watch"))]
    return {"layouts": {"lg": items, "md": [{**it, "w": 3} for it in items], "sm": [{**it, "w": 1} for it in items]}, "watch": [], **over}


def test_layout_revision_and_conflict(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    add_user(factory, "oscar", "operator")
    a, b = login(app_factory(), "olivia"), login(app_factory(), "oscar")
    first = a.get("/api/dashboard/layout").json()
    assert first == {"revision": 0, "data": None, "updated_by": "", "updated_at": None}

    saved = a.put("/api/dashboard/layout", json={"revision": 0, "data": layout()}).json()
    assert saved["revision"] == 1 and saved["updated_by"] == "olivia"

    # b 基于旧修订号保存：409，带回当前版本，不静默覆盖
    stale = b.put("/api/dashboard/layout", json={"revision": 0, "data": layout()})
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["current"]["revision"] == 1 and "覆盖" in detail["message"]

    # 用户选择“用我的版本覆盖”：带上当前修订号再存
    over = b.put("/api/dashboard/layout", json={"revision": 1, "data": layout()})
    assert over.status_code == 200 and over.json()["revision"] == 2


def test_viewer_cannot_save_and_layout_is_validated(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "victor", "viewer")
    add_user(factory, "olivia", "operator")
    v, op = login(app_factory(), "victor"), login(app_factory(), "olivia")
    assert v.get("/api/dashboard/layout").status_code == 200
    assert v.put("/api/dashboard/layout", json={"revision": 0, "data": layout()}).status_code == 403
    bad = layout()
    bad["layouts"]["md"][0]["x"] = 5  # x + w 超出 6 列
    assert op.put("/api/dashboard/layout", json={"revision": 0, "data": bad}).status_code == 422
    dup = layout()
    dup["layouts"]["lg"].append(dup["layouts"]["lg"][0])
    assert op.put("/api/dashboard/layout", json={"revision": 0, "data": dup}).status_code == 422
    assert op.put("/api/dashboard/layout", json={"revision": 0, "data": {**layout(), "layouts": {"lg": [{"i": "nope", "x": 0, "y": 0, "w": 1, "h": 1}]}}}).status_code == 422


def test_summary_uses_real_data_and_never_zero_for_missing(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    c = login(app_factory(), "olivia")
    now = datetime.now(UTC)
    with factory() as db:
        ok = Asset(name="甲", form="physical", role="server", platform="dell-idrac")
        ok.endpoints.append(ManagementEndpoint(address="192.0.2.80", protocol="ipmi"))
        bad = Asset(name="乙", form="physical", role="server", platform="dell-idrac")
        bad.endpoints.append(ManagementEndpoint(address="192.0.2.81", protocol="ipmi"))
        nocred = Asset(name="戊", form="physical", role="server", platform="dell-idrac")
        nocred.endpoints.append(ManagementEndpoint(address="192.0.2.82", protocol="ipmi"))
        bare = Asset(name="丙", form="vm", role="server")
        retired = Asset(name="丁", form="vm", role="server", lifecycle="retired")
        db.add_all([ok, bad, nocred, bare, retired])
        db.flush()
        db.add(CollectionStatus(endpoint_id=ok.endpoints[0].id, asset_id=ok.id, protocol="ipmi", state="ok", last_attempt_at=now, consecutive_failures=0, duration_ms=1, message="", error_kind="", capabilities=[], facts={}))
        db.add(CollectionStatus(endpoint_id=bad.endpoints[0].id, asset_id=bad.id, protocol="ipmi", state="failed", message="BMC 无响应", last_attempt_at=now, consecutive_failures=3, duration_ms=1, error_kind="timeout", capabilities=[], facts={}))
        db.add(CollectionStatus(endpoint_id=nocred.endpoints[0].id, asset_id=nocred.id, protocol="ipmi", state="unconfigured", message="IPMI 端点没有关联用户名和密码的凭据", last_attempt_at=now, consecutive_failures=1, duration_ms=1, error_kind="unconfigured", capabilities=[], facts={}))
        for i in range(0, 600, 60):
            metrics.write_sample(db, ok.id, "ipmi", [Point("cpu.temp", 50.0 + i / 60, "°C"), Point("power.watts", 200.0, "W")], observed_at=now - timedelta(seconds=600 - i))
        metrics.write_sample(db, bad.id, "ipmi", [Point("cpu.temp", None, "°C", quality="failed"), Point("power.watts", None, "W", quality="failed")], observed_at=now)
        db.commit()
        ok_id = str(ok.id)
    c.put("/api/dashboard/layout", json={"revision": 0, "data": layout(watch=[ok_id])})
    s = c.get("/api/dashboard/summary").json()
    assert s["total_assets"] == 4  # 已退役不计
    assert s["health"] == {"ok": 1, "failed": 1, "unconfigured": 1, "unmonitored": 1}  # 缺凭据不算采集失败
    assert [i["name"] for i in s["issues"]] == ["乙"] and s["issues"][0]["message"] == "BMC 无响应"
    assert [t["name"] for t in s["temps"]] == ["甲"] and s["temps"][0]["value"] == 59.0  # 失败的不进排行，也不当 0
    assert s["power"]["current_watts"] == 200.0 and s["power"]["reporting_assets"] == 1
    assert len(s["power"]["series"]) >= 9 and all(p["watts"] == 200.0 for p in s["power"]["series"])
    assert s["quality"]["valid"] == 2 and s["quality"]["failed"] == 2
    assert s["watch"][0]["name"] == "甲" and s["watch"][0]["metrics"]["cpu.temp"]["value"] == 59.0
