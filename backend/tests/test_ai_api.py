"""4.3 P18a：AI 设置接口、密钥加密保存、连接自检、开关 / 配额 / 用量。伪造模型接口，不访问外网。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import ai_runtime
from app.ai.protocols import AiError
from app.ai.redact import Redactor
from app.models import AiProvider, AiUsage, AuditEvent, Credential
from tests.ai_fakes import FakeLLM
from tests.api_helpers import add_user, login

SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}}},
          "required": ["summary", "items"], "additionalProperties": False}


@pytest.fixture
def admin(client, factory):  # type: ignore[no-untyped-def]
    add_user(factory, "fake-admin", "admin")
    return login(client, "fake-admin")


def _create(c, **kw):  # type: ignore[no-untyped-def]
    body = {"name": "fake-deepseek", "preset_id": "deepseek", "protocol": "openai", "base_url": "https://llm.example.invalid/v1",
            "model": "fake-model", "api_key": "fake-secret-key", "quirks": ["json_object_only"], **kw}
    r = c.post("/api/ai/providers", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_provider_crud_keeps_key_encrypted(admin, factory) -> None:  # type: ignore[no-untyped-def]
    p = _create(admin)
    assert p["has_key"] is True and "fake-secret-key" not in str(p) and p["preset_name"] == "DeepSeek" and p["tested_preset"] is False
    with factory() as db:
        row = db.get(AiProvider, __import__("uuid").UUID(p["id"]))
        cred = db.get(Credential, row.credential_id)
        assert b"fake-secret-key" not in cred.secret_ciphertext and cred.kind == "token"
    listed = admin.get("/api/ai/providers").json()
    assert [x["name"] for x in listed] == ["fake-deepseek"] and "fake-secret-key" not in str(listed)
    r = admin.patch(f"/api/ai/providers/{p['id']}", json={"model": "fake-small", "revision": p["revision"]})
    assert r.status_code == 200 and r.json()["model"] == "fake-small"
    assert admin.patch(f"/api/ai/providers/{p['id']}", json={"model": "x", "revision": p["revision"]}).status_code == 409
    assert admin.delete(f"/api/ai/providers/{p['id']}").status_code == 204
    with factory() as db:
        assert db.scalar(select(Credential.id)) is None  # 删除接口时一并删除密钥
        actions = [a for (a,) in db.execute(select(AuditEvent.action)).all()]
        assert {"ai.provider.create", "ai.provider.update", "ai.provider.delete"} <= set(actions)


@pytest.mark.parametrize(("url", "ok"), [
    ("http://api.example.invalid/v1", False),   # 公网明文 http：拒绝
    ("http://127.0.0.1:11434/v1", True),
    ("http://192.168.10.5:8000/v1", True),
    ("https://<资源名>.openai.azure.com", False),
    ("ftp://llm.example.invalid", False),
    ("https://user:pw@llm.example.invalid/v1", False),
])
def test_base_url_rules(admin, url, ok) -> None:  # type: ignore[no-untyped-def]
    r = admin.post("/api/ai/providers", json={"name": f"fake-{abs(hash(url))}", "protocol": "openai", "base_url": url, "model": "m"})
    assert (r.status_code == 201) is ok, r.text


def test_only_admin_can_manage(client, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "fake-op", "operator")
    c = login(client, "fake-op")
    assert c.get("/api/ai/providers").status_code == 403
    assert c.get("/api/ai/settings").status_code == 403


def test_check_and_models_use_fake_endpoint(admin, client) -> None:  # type: ignore[no-untyped-def]
    fake = FakeLLM(json_schema=False)
    client.app.state.ai_http_client = fake.client()
    p = _create(admin, quirks=[])
    r = admin.post(f"/api/ai/providers/{p['id']}/check").json()
    assert r["capabilities"]["json_modes"] == ["json_object", "prompt"] and r["capabilities"]["tools"] is True and r["checked_at"]
    assert all(req.headers["authorization"] == "Bearer fake-secret-key" for req in fake.requests)
    # 改了地址后，原来的自检结果作废
    r2 = admin.patch(f"/api/ai/providers/{p['id']}", json={"base_url": "https://llm2.example.invalid/v1", "revision": r["revision"]}).json()
    assert r2["capabilities"] == {} and r2["checked_at"] is None
    models = admin.post("/api/ai/models", json={"protocol": "openai", "base_url": "https://llm.example.invalid/v1", "provider_id": p["id"]})
    assert models.json() == ["fake-model", "fake-small"]
    client.app.state.ai_http_client = FakeLLM(error=(401, {}, {})).client()
    bad = admin.post("/api/ai/models", json={"protocol": "openai", "base_url": "https://llm.example.invalid/v1", "api_key": "wrong"})
    assert bad.status_code == 502 and bad.json()["detail"]["kind"] == "auth"


def test_settings_validation_and_revision(admin) -> None:  # type: ignore[no-untyped-def]
    s = admin.get("/api/ai/settings").json()
    assert s["enabled"] is False and s["privacy"] == "redact"  # 默认关闭、默认脱敏
    assert set(s["features"]) == set(ai_runtime.FEATURES) and all(f["enabled"] is False for f in s["features"].values())
    p = _create(admin)
    body = {**{k: s[k] for k in ("privacy", "daily_call_limit", "daily_token_limit", "timeout_seconds", "proxy_url", "log_content", "revision")},
            "enabled": True, "default_provider_id": p["id"], "features": {"cve_analysis": {"enabled": True, "provider_id": None}}}
    r = admin.put("/api/ai/settings", json=body)
    assert r.status_code == 200 and r.json()["features"]["cve_analysis"]["enabled"] is True
    assert admin.put("/api/ai/settings", json=body).status_code == 409
    assert admin.put("/api/ai/settings", json={**body, "revision": r.json()["revision"], "features": {"nope": {"enabled": True}}}).status_code == 422
    assert admin.put("/api/ai/settings", json={**body, "revision": r.json()["revision"], "proxy_url": "file:///x"}).status_code == 422


def test_runtime_switches_quota_redaction_and_usage(admin, client, factory) -> None:  # type: ignore[no-untyped-def]
    fake = FakeLLM(answer={"summary": "ASSET_1 位于 IP_1，需要升级", "items": []})
    p = _create(admin, quirks=[])
    cipher = client.app.state.cipher
    with factory() as db:
        with pytest.raises(AiError) as exc:  # 总开关关闭
            ai_runtime.run_json(db, cipher, "cve_analysis", user_name="fake-admin", messages=[{"role": "user", "content": "x"}], schema=SCHEMA, client=fake.client())
        assert exc.value.kind == "disabled"
    s = admin.get("/api/ai/settings").json()
    admin.put("/api/ai/settings", json={**{k: s[k] for k in ("privacy", "daily_token_limit", "timeout_seconds", "proxy_url", "log_content", "revision")},
                                        "enabled": True, "daily_call_limit": 2, "default_provider_id": p["id"],
                                        "features": {"cve_analysis": {"enabled": True, "provider_id": None}}})
    with factory() as db:
        red = ai_runtime.redactor(db)
        assert isinstance(red, Redactor) and red.enabled
        red.register("db-01", "asset")
        prompt = red.text("db-01 在 192.0.2.10 上")
        out = ai_runtime.run_json(db, cipher, "cve_analysis", user_name="fake-admin", messages=[{"role": "user", "content": prompt}],
                                  schema=SCHEMA, redact=red, client=fake.client())
        assert out["summary"] == "db-01 位于 192.0.2.10，需要升级"
        assert "192.0.2.10" not in fake.requests[-1].content.decode() and "db-01" not in fake.requests[-1].content.decode()
        with pytest.raises(AiError) as exc:  # 未开启的功能
            ai_runtime.run_json(db, cipher, "assistant", user_name="u", messages=[], schema=SCHEMA, client=fake.client())
        assert exc.value.kind == "disabled"
        ai_runtime.run_json(db, cipher, "cve_analysis", user_name="fake-admin", messages=[{"role": "user", "content": "y"}], schema=SCHEMA, client=fake.client())
        with pytest.raises(AiError) as exc:  # 每日 2 次上限
            ai_runtime.run_json(db, cipher, "cve_analysis", user_name="fake-admin", messages=[{"role": "user", "content": "z"}], schema=SCHEMA, client=fake.client())
        assert exc.value.kind == "quota"
        rows = db.scalars(select(AiUsage)).all()
        assert len(rows) == 2 and all(r.ok and r.content is None and r.input_tokens == 12 for r in rows)  # 默认不记录内容
        rows[0].at = datetime.now(UTC) - timedelta(days=2)
        db.commit()
    u = admin.get("/api/ai/usage").json()
    assert u["today"]["calls"] == 1 and u["by_feature"][0]["name"] == "漏洞影响分析" and len(u["recent"]) == 2
