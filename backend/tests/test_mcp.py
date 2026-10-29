"""4.3 P18h：只读 MCP 接口（Streamable HTTP，JSON-RPC 2.0）。"""
from __future__ import annotations

import json

from sqlalchemy import select

from app import ai_runtime
from app.models import AiUsage, Asset, User, UserScopeAsset
from tests.api_helpers import add_user, login


def _rpc(c, method: str, params: dict | None = None, id_: int | None = 1, token: str = "", origin: str = ""):  # type: ignore[no-untyped-def]
    body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {}), **({"id": id_} if id_ is not None else {})}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    if origin:
        headers["Origin"] = origin
    return c.post("/mcp", json=body, headers=headers)


def _setup(client, factory) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    add_user(factory, "fake-viewer", "viewer")
    with factory() as db:
        mine = Asset(name="fake-mine", form="physical", role="server", lifecycle="active")
        other = Asset(name="fake-other", form="physical", role="server", lifecycle="active")
        db.add_all([mine, other])
        db.flush()
        u = db.scalar(select(User).where(User.username == "fake-viewer"))
        u.scope_all = False
        db.add(UserScopeAsset(user_id=u.id, asset_id=mine.id))
        db.commit()
    c = login(client, "fake-viewer")
    token = c.post("/api/tokens", json={"name": "fake-mcp"}).json()["token"]
    return token, "fake-mine"


def test_mcp(client, factory) -> None:  # type: ignore[no-untyped-def]
    token, _ = _setup(client, factory)
    anon = client.__class__(client.app)
    assert _rpc(anon, "initialize", {"protocolVersion": "2025-06-18"}, token=token).status_code == 403  # 默认关闭
    with factory() as db:
        ai_runtime.settings_row(db).features = {"mcp": {"enabled": True, "provider_id": None}}
        db.commit()
    assert _rpc(anon, "initialize", {}, token="ys_wrong").status_code == 401
    init = _rpc(anon, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake"}}, token=token).json()["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["capabilities"] == {"tools": {"listChanged": False}} and init["serverInfo"]["name"] == "yunshu-ops"
    assert _rpc(anon, "initialize", {"protocolVersion": "1999-01-01"}, token=token).json()["result"]["protocolVersion"] == "2025-06-18"
    assert _rpc(anon, "notifications/initialized", id_=None, token=token).status_code == 202
    tl = _rpc(anon, "tools/list", token=token).json()["result"]["tools"]
    assert {"search_assets", "top_metric", "list_vulnerabilities"} <= {t["name"] for t in tl}
    assert all(t["annotations"]["readOnlyHint"] and t["inputSchema"]["type"] == "object" for t in tl)
    r = _rpc(anon, "tools/call", {"name": "search_assets", "arguments": {"query": "fake"}}, token=token).json()["result"]
    data = json.loads(r["content"][0]["text"])
    assert r["isError"] is False and [a["name"] for a in data["assets"]] == ["fake-mine"]  # 只看得到令牌账号范围内的
    assert _rpc(anon, "tools/call", {"name": "get_asset", "arguments": {"name": "fake-other"}}, token=token).json()["result"]["isError"] is True
    assert _rpc(anon, "tools/call", {"name": "drop_table"}, token=token).json()["error"]["code"] == -32602
    assert _rpc(anon, "resources/list", token=token).json()["error"]["code"] == -32601
    assert _rpc(anon, "ping", token=token).json()["result"] == {}
    assert _rpc(anon, "tools/list", token=token, origin="https://evil.example.invalid").status_code == 403
    assert anon.get("/mcp").status_code == 405
    assert anon.post("/mcp", content=b"{not json", headers={"Authorization": f"Bearer {token}"}).status_code == 400
    with factory() as db:
        rows = db.scalars(select(AiUsage).where(AiUsage.feature == "mcp")).all()
        assert len(rows) == 2 and {r.model for r in rows} == {"search_assets", "get_asset"} and rows[0].user_name == "fake-viewer"
