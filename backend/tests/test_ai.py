"""4.3 P18a：四种协议适配、结构化输出逐级降级、脱敏还原、连接自检。全部用伪造接口，不访问外网。"""
from __future__ import annotations

import json

import pytest

from app.ai import selfcheck
from app.ai.presets import presets
from app.ai.protocols import AiError, ChatRequest, Endpoint, Tool, ToolCall, chat, list_models, stream_chat
from app.ai.redact import Redactor
from app.ai.structured import call_json, parse_json, provider_schema, validate
from tests.ai_fakes import FakeLLM

SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer", "minimum": 0}, "unit": {"type": "string", "maxLength": 4}},
          "required": ["answer", "unit"], "additionalProperties": False}
Q = [{"role": "user", "content": "1 加 1 等于几？"}]


def ep(protocol: str = "openai", **kw) -> Endpoint:  # type: ignore[no-untyped-def]
    base = {"openai": "https://llm.example.invalid/v1", "azure": "https://res.example.invalid", "anthropic": "https://claude.example.invalid",
            "gemini": "https://gemini.example.invalid"}[protocol]
    return Endpoint(protocol=protocol, base_url=base + "/", api_key="fake-key", model="fake-model", **kw)


def body(fake: FakeLLM, i: int = -1) -> dict:
    return json.loads(fake.requests[i].content)


# ---------------- 协议 ----------------


def test_openai_chat_url_headers_and_usage() -> None:
    fake = FakeLLM()
    r = chat(ep(), ChatRequest(messages=Q, system="你是助手"), fake.client())
    req = fake.requests[0]
    assert str(req.url) == "https://llm.example.invalid/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer fake-key"
    b = body(fake)
    assert b["model"] == "fake-model" and b["messages"][0] == {"role": "system", "content": "你是助手"} and b["max_tokens"] == 2048
    assert (r.text, r.input_tokens, r.output_tokens) == ("OK", 12, 5)


def test_azure_uses_deployment_url_and_api_key_header() -> None:
    fake = FakeLLM(protocol="azure")
    chat(ep("azure"), ChatRequest(messages=Q), fake.client())
    req = fake.requests[0]
    assert str(req.url) == "https://res.example.invalid/openai/deployments/fake-model/chat/completions?api-version=2024-10-21"
    assert req.headers["api-key"] == "fake-key" and "authorization" not in req.headers
    assert "model" not in body(fake)


def test_openai_tool_round_trip_and_mistral_ids() -> None:
    fake = FakeLLM()
    tool = Tool("get_server_count", "查询", {"type": "object", "properties": {"site": {"type": "string"}}, "required": ["site"]})
    r = chat(ep(), ChatRequest(messages=Q, tools=[tool]), fake.client())
    assert r.finish_reason == "tool_calls" and r.tool_calls[0].name == "get_server_count" and r.tool_calls[0].arguments == {"site": "演示机房 A"}
    follow = [*Q, {"role": "assistant", "content": "", "tool_calls": r.tool_calls}, {"role": "tool", "tool_call_id": "call_abc", "content": "3"}]
    chat(ep(quirks=frozenset({"mistral_tool_ids"})), ChatRequest(messages=follow, tools=[tool]), fake.client())
    msgs = body(fake)["messages"]
    new_id = msgs[1]["tool_calls"][0]["id"]
    assert len(new_id) == 9 and new_id.isalnum() and msgs[2]["tool_call_id"] == new_id


@pytest.mark.parametrize(("status", "payload", "headers", "kind"), [
    (401, {"error": {"message": "bad key"}}, {}, "auth"),
    (404, {"error": {"message": "model not found"}}, {}, "not_found"),
    (429, {"error": {"message": "rate"}}, {"retry-after": "7"}, "rate_limited"),
    (400, {"error": {"code": "data_inspection_failed", "message": "Input data may contain inappropriate content."}}, {}, "content_filter"),
    (400, {"error": {"message": "bad param"}}, {}, "bad_request"),
    (503, {"error": {"message": "overloaded"}}, {}, "server"),
])
def test_errors_are_translated(status, payload, headers, kind) -> None:  # type: ignore[no-untyped-def]
    fake = FakeLLM(error=(status, payload, headers))
    with pytest.raises(AiError) as exc:
        chat(ep(), ChatRequest(messages=Q), fake.client())
    assert exc.value.kind == kind
    if kind == "rate_limited":
        assert exc.value.retry_after == 7 and "7 秒" in str(exc.value)
    assert "fake-key" not in str(exc.value)


def test_empty_truncated_and_filtered_results() -> None:
    with pytest.raises(AiError) as exc:
        chat(ep(), ChatRequest(messages=Q), FakeLLM(empty_times=1).client())
    assert exc.value.kind == "empty"
    with pytest.raises(AiError) as exc:
        chat(ep(), ChatRequest(messages=Q), FakeLLM(finish="content_filter").client())
    assert exc.value.kind == "content_filter"
    fake = FakeLLM(protocol="gemini", finish="content_filter")
    with pytest.raises(AiError) as exc:
        chat(ep("gemini"), ChatRequest(messages=Q), fake.client())
    assert exc.value.kind == "content_filter"


def test_anthropic_headers_output_config_and_tools() -> None:
    fake = FakeLLM(protocol="anthropic")
    r = chat(ep("anthropic"), ChatRequest(messages=Q, system="sys", schema=SCHEMA, json_mode="native"), fake.client())
    req = fake.requests[0]
    assert str(req.url) == "https://claude.example.invalid/v1/messages"
    b = body(fake)
    assert b["system"] == "sys" and b["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}} and b["max_tokens"] == 2048
    assert parse_json(r.text) == {"answer": 2, "unit": "个"}
    tool = Tool("get_server_count", "查询", {"type": "object", "properties": {}})
    r = chat(ep("anthropic"), ChatRequest(messages=Q, tools=[tool]), fake.client())
    assert r.tool_calls == [ToolCall(id="toolu_01", name="get_server_count", arguments={"site": "演示机房 A"})]
    follow = [*Q, {"role": "assistant", "content": "", "tool_calls": r.tool_calls},
              {"role": "tool", "tool_call_id": "toolu_01", "content": "3"}, {"role": "tool", "tool_call_id": "toolu_02", "content": "4"}]
    chat(ep("anthropic"), ChatRequest(messages=follow, tools=[tool]), fake.client())
    msgs = body(fake)["messages"]
    assert msgs[1]["content"][0]["type"] == "tool_use"
    assert [blk["tool_use_id"] for blk in msgs[2]["content"]] == ["toolu_01", "toolu_02"]  # 连续工具结果合并成一条 user 消息


def test_anthropic_wrong_key_is_auth_error() -> None:
    with pytest.raises(AiError) as exc:
        chat(Endpoint("anthropic", "https://claude.example.invalid", "wrong", "m"), ChatRequest(messages=Q), FakeLLM(protocol="anthropic").client())
    assert exc.value.kind == "auth"


def test_gemini_url_schema_and_function_call() -> None:
    fake = FakeLLM(protocol="gemini")
    chat(ep("gemini"), ChatRequest(messages=Q, system="sys", schema=SCHEMA, json_mode="native"), fake.client())
    req = fake.requests[0]
    assert str(req.url) == "https://gemini.example.invalid/v1beta/models/fake-model:generateContent"
    b = body(fake)
    assert b["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert b["generationConfig"]["responseMimeType"] == "application/json" and b["generationConfig"]["responseJsonSchema"] == SCHEMA
    tool = Tool("get_server_count", "查询", {"type": "object", "properties": {}})
    r = chat(ep("gemini"), ChatRequest(messages=Q, tools=[tool]), fake.client())
    assert r.tool_calls[0].name == "get_server_count" and r.finish_reason == "tool_calls"
    follow = [*Q, {"role": "assistant", "content": "", "tool_calls": r.tool_calls}, {"role": "tool", "tool_call_id": r.tool_calls[0].id, "content": "3"}]
    chat(ep("gemini"), ChatRequest(messages=follow, tools=[tool]), fake.client())
    contents = body(fake)["contents"]
    assert contents[1]["role"] == "model" and contents[2]["parts"][0]["functionResponse"] == {"name": "get_server_count", "response": {"result": 3}}


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini"])
def test_models_and_stream(protocol: str) -> None:
    fake = FakeLLM(protocol=protocol)
    assert list_models(ep(protocol), fake.client()) == ["fake-model", "fake-small"]
    assert "".join(stream_chat(ep(protocol), ChatRequest(messages=Q), fake.client())) == "1, 2, 3, 4, 5"


# ---------------- 结构化输出 ----------------


def test_validate_and_parse() -> None:
    assert validate({"answer": 2, "unit": "个"}, SCHEMA) == []
    errs = validate({"answer": -1, "unit": "太长的单位", "x": 1}, SCHEMA)
    assert any("不应小于" in e for e in errs) and any("超过" in e for e in errs) and any("不应出现" in e for e in errs)
    assert validate({"answer": True, "unit": "个"}, SCHEMA) == ["$.answer 应为 integer"]
    assert parse_json('好的，结果如下：{"a": {"b": "}"}} 以上') == {"a": {"b": "}"}}
    assert provider_schema(SCHEMA)["properties"]["answer"] == {"type": "integer"}


def test_ladder_falls_back_when_json_schema_rejected() -> None:
    fake = FakeLLM(json_schema=False)  # 模拟 DeepSeek / 智谱：只有 json_object
    value, mode, _ = call_json(ep(), Q, SCHEMA, client=fake.client())
    assert (value, mode) == ({"answer": 2, "unit": "个"}, "json_object")
    assert body(fake, 0)["response_format"]["type"] == "json_schema" and body(fake, 1)["response_format"] == {"type": "json_object"}
    assert "JSON" in body(fake, 1)["messages"][0]["content"]  # json_object 要求提示词里出现 JSON
    assert "minimum" not in json.dumps(body(fake, 0)["response_format"])  # 约束只在本地校验


def test_ladder_repairs_once_and_retries_empty() -> None:
    fake = FakeLLM(bad_json_times=1, empty_times=1)
    value, mode, _ = call_json(ep(), Q, SCHEMA, modes=["json_schema"], client=fake.client())
    assert value == {"answer": 2, "unit": "个"} and mode == "json_schema" and len(fake.requests) == 3
    assert "不符合要求" in body(fake)["messages"][-1]["content"]


def test_ladder_gives_up_with_invalid_output() -> None:
    with pytest.raises(AiError) as exc:
        call_json(ep(), Q, SCHEMA, modes=["json_schema"], client=FakeLLM(bad_json_times=5).client())
    assert exc.value.kind == "invalid_output"


def test_ladder_does_not_hide_auth_errors() -> None:
    with pytest.raises(AiError) as exc:
        call_json(ep(), Q, SCHEMA, client=FakeLLM(error=(401, {}, {})).client())
    assert exc.value.kind == "auth"


def test_quirk_no_max_tokens_with_json() -> None:
    fake = FakeLLM()
    call_json(ep(quirks=frozenset({"no_max_tokens_with_json"})), Q, SCHEMA, modes=["json_object"], client=fake.client())
    assert "max_tokens" not in body(fake)


# ---------------- 脱敏 ----------------


def test_redactor_round_trip() -> None:
    r = Redactor()
    assert r.register("db-01（数据库）", "asset") == "ASSET_1"
    text = r.text("db-01（数据库） 在 192.0.2.10，MAC 00:11:22:AA:bb:cc，网段 198.51.100.0/24，IPv6 2001:db8::1，iDRAC 2.60.60.60")
    assert "db-01" not in text and "192.0.2.10" not in text and "00:11:22" not in text and "2001:db8" not in text
    payload = r.data({"desc": "主机 192.0.2.10", "version": "2.60.60.60", "fixed": "3.21.21.21"})
    assert payload["version"] == "2.60.60.60" and payload["fixed"] == "3.21.21.21" and "192.0.2.10" not in payload["desc"]
    assert r.restore({"say": f"{r.register('db-01（数据库）', 'asset')} 和 IP_1 受影响"}) == {"say": "db-01（数据库） 和 192.0.2.10 受影响"}
    off = Redactor(enabled=False)
    assert off.text("192.0.2.10") == "192.0.2.10" and off.register("x") == "x"


# ---------------- 连接自检与预设 ----------------


def test_selfcheck_full_capabilities() -> None:
    caps, results = selfcheck.run(ep(), FakeLLM().client())
    assert caps == {"chat": True, "models": True, "json_modes": ["json_schema", "json_object", "prompt"], "best_json": "json_schema", "tools": True, "stream": True}
    assert all(r["ok"] for r in results)


def test_selfcheck_records_limits() -> None:
    caps, _ = selfcheck.run(ep(), FakeLLM(json_schema=False, tools=False, stream=False).client())
    assert caps["json_modes"] == ["json_object", "prompt"] and caps["tools"] is False and caps["stream"] is False and caps["chat"] is True
    caps, _ = selfcheck.run(ep("anthropic"), FakeLLM(protocol="anthropic", native=False).client())
    assert caps["json_modes"] == ["prompt"]
    caps, _ = selfcheck.run(ep(quirks=frozenset({"json_object_only"})), FakeLLM().client())
    assert caps["json_modes"] == ["json_object", "prompt"]


def test_selfcheck_stops_after_auth_failure() -> None:
    fake = FakeLLM(error=(401, {"error": {"message": "bad"}}, {}))
    caps, results = selfcheck.run(ep(), fake.client())
    assert caps["chat"] is False and caps["error"] == "auth" and len(fake.requests) == 1
    assert [r["kind"] for r in results[1:]] == ["skipped"] * 4


def test_presets_are_valid() -> None:
    items = presets()
    ids = [p["id"] for p in items]
    assert len(ids) == len(set(ids)) and {"openai", "anthropic", "gemini", "azure", "deepseek", "dashscope", "ollama"} <= set(ids)
    for p in items:
        assert p["base_url"] == "" or p["base_url"].startswith(("https://", "http://127.0.0.1"))
        assert p["tested"] is False  # 维护者真机冒烟后才改为 true
