"""连接自检：不信文档，直接对用户配置的接口逐项试一遍，得出这条配置的能力表。

步骤：普通回复（含鉴权与地址）→ 模型列表 → 结构化输出（逐级尝试，记下全部可用的方式）→ 工具调用 → 流式输出。
鉴权或地址错误时后面的步骤跳过。每一步都很短，自检一次大约 6～10 次调用。
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.ai.protocols import AiError, ChatRequest, Endpoint, Tool, chat, list_models, stream_chat
from app.ai.structured import call_json, modes_for

_JSON_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}, "unit": {"type": "string"}},
    "required": ["answer", "unit"],
    "additionalProperties": False,
}
_TOOL = Tool(name="get_server_count", description="查询某个机房的服务器数量",
             parameters={"type": "object", "properties": {"site": {"type": "string", "description": "机房名称"}}, "required": ["site"]})


def _step(results: list[dict[str, Any]], name: str, label: str, fn) -> Any:  # type: ignore[no-untyped-def]
    t0 = time.perf_counter()
    try:
        value, detail = fn()
        results.append({"step": name, "label": label, "ok": True, "detail": detail, "ms": int((time.perf_counter() - t0) * 1000)})
        return value
    except AiError as exc:
        results.append({"step": name, "label": label, "ok": False, "detail": str(exc), "kind": exc.kind, "ms": int((time.perf_counter() - t0) * 1000)})
        return exc


def run(ep: Endpoint, client: httpx.Client | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    caps: dict[str, Any] = {"chat": False, "models": False, "json_modes": [], "best_json": None, "tools": False, "stream": False}

    def plain():  # type: ignore[no-untyped-def]
        r = chat(ep, ChatRequest(messages=[{"role": "user", "content": "请只回复两个字母：OK"}], max_tokens=16, temperature=0), client)
        return r, f"回复“{r.text.strip()[:20]}”，用 {r.input_tokens + r.output_tokens} 个 token"

    first = _step(results, "chat", "普通回复（地址与密钥）", plain)
    if isinstance(first, AiError):
        caps["error"] = first.kind
        if first.kind in ("auth", "not_found", "unreachable", "timeout"):
            for name, label in (("models", "模型列表"), ("json", "结构化输出"), ("tools", "工具调用"), ("stream", "流式输出")):
                results.append({"step": name, "label": label, "ok": False, "detail": "上一步未通过，已跳过", "kind": "skipped", "ms": 0})
            return caps, results
    else:
        caps["chat"] = True

    def models():  # type: ignore[no-untyped-def]
        names = list_models(ep, client)
        found = ep.model in names or any(n.endswith("/" + ep.model) for n in names)
        return names, f"共 {len(names)} 个模型" + ("，包含当前模型" if found else "，列表里没有当前模型名（部分厂商不列出全部模型，可忽略）")

    caps["models"] = not isinstance(_step(results, "models", "模型列表", models), AiError)

    working: list[str] = []
    for mode in modes_for(ep.protocol):
        if mode in ("json_schema", "native") and "json_object_only" in ep.quirks:
            continue

        def try_mode(m: str = mode):  # type: ignore[no-untyped-def]
            value, used, _ = call_json(ep, [{"role": "user", "content": "1 加 1 等于几？unit 填“个”。"}], _JSON_SCHEMA, modes=[m], max_tokens=128, client=client)
            if value.get("answer") != 2:
                raise AiError("invalid_output", f"格式正确但答案是 {value.get('answer')}")
            return used, f"{m} 可用"

        if not isinstance(_step(results, f"json:{mode}", f"结构化输出（{mode}）", try_mode), AiError):
            working.append(mode)
    caps["json_modes"] = working
    caps["best_json"] = working[0] if working else None

    def tools():  # type: ignore[no-untyped-def]
        r = chat(ep, ChatRequest(messages=[{"role": "user", "content": "演示机房 A 有几台服务器？请调用工具查询。"}], tools=[_TOOL], max_tokens=256, temperature=0), client)
        if not r.tool_calls or r.tool_calls[0].name != _TOOL.name:
            raise AiError("unsupported", "模型没有调用工具")
        return True, f"调用了 {r.tool_calls[0].name}，参数 {r.tool_calls[0].arguments}"

    caps["tools"] = not isinstance(_step(results, "tools", "工具调用", tools), AiError)

    def stream():  # type: ignore[no-untyped-def]
        pieces = list(stream_chat(ep, ChatRequest(messages=[{"role": "user", "content": "从 1 数到 5，用逗号分隔。"}], max_tokens=64, temperature=0), client))
        if not pieces:
            raise AiError("empty", "没有收到流式片段")
        return True, f"收到 {len(pieces)} 个片段"

    caps["stream"] = not isinstance(_step(results, "stream", "流式输出", stream), AiError)
    return caps, results
