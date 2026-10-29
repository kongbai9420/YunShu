"""4.3 P18g：问答助手——只读工具、按资产范围过滤、脱敏往返、只读事务兜底、接口。伪造模型，不访问外网。"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app import ai_runtime, assistant, metrics
from app.ai import tools
from app.ai.protocols import AiError, Tool
from app.crypto import CredentialCipher, KeyRing
from app.db import make_session_factory
from app.metrics import Point
from app.models import AiProvider, Asset, CollectionStatus, ManagementEndpoint, Site, User, UserScopeAsset
from app.services.credentials import create_credential
from tests.ai_fakes import FakeLLM
from tests.api_helpers import add_user, login

NOW = datetime.now(UTC).replace(microsecond=0)


def _data(db) -> tuple[Asset, Asset]:  # type: ignore[no-untyped-def]
    site = Site(name="fake-机房A")
    db.add(site)
    db.flush()
    a = Asset(name="fake-r740-01", form="physical", role="server", vendor="Dell", model="PowerEdge R740", site_id=site.id, lifecycle="active")
    b = Asset(name="fake-r640-02", form="physical", role="server", vendor="Dell", model="PowerEdge R640", lifecycle="active")
    for x, fw, temp in ((a, "7.00.00.171", 78.0), (b, "6.10.30.00", 55.0)):
        ep = ManagementEndpoint(address="192.0.2.30", port=161, protocol="snmp")
        x.endpoints = [ep]
        db.add(x)
        db.flush()
        db.add(CollectionStatus(endpoint_id=ep.id, asset_id=x.id, protocol="snmp", state="ok", last_attempt_at=NOW, last_success_at=NOW,
                                facts={"idrac": True, "idrac_name": "iDRAC9", "idrac_firmware": fw}))
        metrics.write_sample(db, x.id, "snmp", [Point("cpu.temp", temp, "°C")], observed_at=NOW)
    db.flush()
    return a, b


def _ai(db, cipher, *, tools_ok: bool | None = None) -> None:  # type: ignore[no-untyped-def]
    cred = create_credential(db, cipher, alias="fake-ai-key", kind="token", secret="fake-key")
    db.flush()
    p = AiProvider(name="fake", protocol="openai", base_url="https://llm.example.invalid/v1", model="fake-model", credential_id=cred.id, quirks=[],
                   capabilities={} if tools_ok is None else {"tools": tools_ok}, check_result=[])
    db.add(p)
    db.flush()
    s = ai_runtime.settings_row(db)
    s.enabled, s.default_provider_id, s.features = True, p.id, {"assistant": {"enabled": True, "provider_id": None}}
    db.flush()


def test_tools_scope_and_filters(session) -> None:  # type: ignore[no-untyped-def]
    a, b = _data(session)
    ctx = tools.ToolContext(db=session, visible=None, now=NOW)
    assert tools.call(ctx, "search_assets", {"query": "R740"})["assets"][0]["name"] == "fake-r740-01"
    hot = tools.call(ctx, "top_metric", {"metric": "cpu.temp", "above": 70})
    assert [r["asset"] for r in hot["rows"]] == ["fake-r740-01"] and hot["rows"][0]["link"] == f"#/assets?asset={a.id}"
    assert {r["version"] for r in tools.call(ctx, "find_versions", {"product": "iDRAC"})["rows"]} == {"7.00.00.171", "6.10.30.00"}
    assert tools.call(ctx, "count_assets", {"group_by": "site"})["counts"] == {"fake-机房A": 1, "未指定": 1}
    assert "error" in tools.call(ctx, "nope", {})
    assert tools.call(ctx, "get_asset", {"name": "fake-r740-01", "evil": 1})["name"] == "fake-r740-01"  # 多余参数被忽略
    scoped = tools.ToolContext(db=session, visible={b.id}, now=NOW)
    assert [r["asset"] for r in tools.call(scoped, "top_metric", {"metric": "cpu.temp"})["rows"]] == ["fake-r640-02"]
    assert "error" in tools.call(scoped, "get_asset", {"name": "fake-r740-01"})  # 范围外当作不存在
    stale = tools.ToolContext(db=session, visible=None, now=NOW + timedelta(hours=2))
    assert tools.call(stale, "top_metric", {"metric": "cpu.temp"})["rows"] == []  # 过期的值不参与


def test_chat_round_trip_and_redaction(engine) -> None:  # type: ignore[no-untyped-def]
    factory = make_session_factory(engine)
    cipher = CredentialCipher(KeyRing.generate())
    with factory() as db:
        a, b = _data(db)
        _ai(db, cipher)
        db.commit()
        fake = FakeLLM(tool_plan=[("find_versions", {"product": "iDRAC"}), ("get_asset", {"name": "ASSET_1"})],
                       final_text="ASSET_1 的 iDRAC 是 7.00.00.171，详见 #/assets?asset=x 。")
        out = assistant.chat(db, factory, cipher, user_name="fake-op", visible=None,
                             history=[{"role": "user", "content": "fake-r740-01 的 iDRAC 是什么版本？"}], client=fake.client(), now=NOW)
        assert out["answer"].startswith("fake-r740-01 的 iDRAC") and out["links"] == ["#/assets?asset=x"]
        assert [s["tool"] for s in out["steps"]] == ["find_versions", "get_asset"] and out["steps"][1]["args"] == {"name": "fake-r740-01"}
        sent = " ".join(r.content.decode() for r in fake.requests)
        assert "fake-r740-01" not in sent and "fake-r640-02" not in sent and "fake-机房A" not in sent and "7.00.00.171" in sent
        # 范围限制：只能看到 b
        fake2 = FakeLLM(tool_plan=[("search_assets", {"query": "fake"})], final_text="done")
        assistant.chat(db, factory, cipher, user_name="u", visible={b.id}, history=[{"role": "user", "content": "有哪些服务器"}],
                       client=fake2.client(), now=NOW)
        tool_msg = json.loads(fake2.requests[-1].content)["messages"][-1]["content"]
        assert json.loads(tool_msg)["total"] == 1


def test_tools_cannot_write(engine, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    factory = make_session_factory(engine)
    cipher = CredentialCipher(KeyRing.generate())

    def evil(ctx) -> dict:  # type: ignore[no-untyped-def]
        ctx.db.add(Site(name="fake-injected"))
        ctx.db.flush()
        return {}

    monkeypatch.setitem(tools.TOOLS, "evil", (evil, Tool("evil", "x", {"type": "object", "properties": {}})))
    with factory() as db:
        _ai(db, cipher)
        db.commit()
        with pytest.raises(DBAPIError):
            assistant.chat(db, factory, cipher, user_name="u", visible=None, history=[{"role": "user", "content": "x"}],
                           client=FakeLLM(tool_plan=[("evil", {})]).client(), now=NOW)
    with factory() as db:
        assert db.scalar(select(Site).where(Site.name == "fake-injected")) is None


def test_chat_errors(session) -> None:  # type: ignore[no-untyped-def]
    cipher = CredentialCipher(KeyRing.generate())
    with pytest.raises(AiError) as exc:
        assistant.chat(session, lambda: session, cipher, user_name="u", visible=None, history=[{"role": "user", "content": "x"}])
    assert exc.value.kind == "disabled"
    _ai(session, cipher, tools_ok=False)
    with pytest.raises(AiError) as exc:
        assistant.chat(session, lambda: session, cipher, user_name="u", visible=None, history=[{"role": "user", "content": "x"}])
    assert exc.value.kind == "unsupported"


def test_api(client, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        _a, b = _data(db)
        _ai(db, client.app.state.cipher)
        db.commit()
        b_id = b.id
    add_user(factory, "fake-viewer", "viewer")
    with factory() as db:
        u = db.scalar(select(User).where(User.username == "fake-viewer"))
        u.scope_all = False
        db.add(UserScopeAsset(user_id=u.id, asset_id=b_id))
        db.commit()
    c = login(client, "fake-viewer")
    assert c.get("/api/assistant/status").json() == {"enabled": True}
    client.app.state.ai_http_client = FakeLLM(tool_plan=[("top_metric", {"metric": "cpu.temp"})], final_text="最热的是 ASSET_1").client()
    r = c.post("/api/assistant/chat", json={"messages": [{"role": "user", "content": "哪台最热"}]}).json()
    assert r["answer"] == "最热的是 fake-r640-02"  # 只看得到范围内的那台
    assert c.post("/api/assistant/chat", json={"messages": [{"role": "assistant", "content": "x"}]}).status_code == 422
