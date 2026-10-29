"""四种接口协议的适配：OpenAI 兼容、Anthropic Claude、Google Gemini、Azure OpenAI。

只用 httpx，不引入各家 SDK。统一的消息格式：
    {"role": "user" | "assistant" | "tool", "content": str,
     "tool_calls": [ToolCall...]（assistant 发起工具调用时）, "tool_call_id": str（tool 结果）, "name": str}
"""
from __future__ import annotations

import json
import random
import string
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

PROTOCOLS = ("openai", "anthropic", "gemini", "azure")
ANTHROPIC_VERSION = "2023-06-01"
AZURE_DEFAULT_API_VERSION = "2024-10-21"
# 结构化输出的四级：native（Claude / Gemini 原生按 schema 输出）、json_schema、json_object、prompt（只靠提示词）
JSON_MODES = ("native", "json_schema", "json_object", "prompt")


class AiError(Exception):
    """kind：auth / not_found / rate_limited / timeout / unreachable / bad_request / content_filter /
    truncated / empty / invalid_output / server / unsupported / disabled / quota"""

    def __init__(self, kind: str, message: str, *, retry_after: float | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.kind, self.retry_after, self.status = kind, retry_after, status


ERROR_TEXT = {
    "auth": "密钥无效或没有权限",
    "not_found": "地址或模型名不对",
    "rate_limited": "调用太频繁，被对方限流",
    "timeout": "等待回复超时",
    "unreachable": "连接不上接口地址",
    "bad_request": "对方拒绝了这个请求格式",
    "content_filter": "内容被厂商的审核拦截",
    "truncated": "回答被截断，可调大输出长度",
    "empty": "模型返回了空内容",
    "invalid_output": "模型输出不符合要求的格式",
    "server": "对方服务出错",
    "unsupported": "该模型不支持这项能力",
    "disabled": "AI 功能未开启",
    "quota": "已达到今日调用上限",
}


@dataclass
class Endpoint:
    protocol: str
    base_url: str
    api_key: str
    model: str
    api_version: str = ""
    timeout: float = 60.0
    quirks: frozenset[str] = frozenset()
    proxy: str = ""

    def __repr__(self) -> str:  # 不把密钥打进日志或异常
        return f"Endpoint({self.protocol} {self.base_url} {self.model})"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatRequest:
    messages: list[dict[str, Any]]
    system: str = ""
    schema: dict[str, Any] | None = None
    schema_name: str = "result"
    json_mode: str | None = None
    tools: list[Tool] = field(default_factory=list)
    max_tokens: int = 2048
    temperature: float | None = 0.2


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


def _client(ep: Endpoint, client: httpx.Client | None) -> httpx.Client:
    if client is not None:
        return client
    return httpx.Client(timeout=ep.timeout, proxy=ep.proxy or None, follow_redirects=False)


def _base(ep: Endpoint) -> str:
    return ep.base_url.rstrip("/")


def _short_id() -> str:
    # Mistral 要求工具调用 ID 为 9 位字母数字（new-api 有同样的转换）
    return "".join(random.choices(string.ascii_letters + string.digits, k=9))


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
        detail = json.dumps(body, ensure_ascii=False)[:500]
    except ValueError:
        body, detail = None, resp.text[:500]
    text = detail.lower()
    if resp.status_code in (401, 403):
        raise AiError("auth", ERROR_TEXT["auth"], status=resp.status_code)
    if resp.status_code == 404:
        raise AiError("not_found", ERROR_TEXT["not_found"], status=404)
    if resp.status_code == 429:
        retry = resp.headers.get("retry-after")
        try:
            after = float(retry) if retry else None
        except ValueError:
            after = None
        raise AiError("rate_limited", ERROR_TEXT["rate_limited"] + (f"，约 {after:g} 秒后可重试" if after else ""), retry_after=after, status=429)
    if any(k in text for k in ("content_filter", "sensitive", "data_inspection_failed", "敏感", "审核", "safety")):
        raise AiError("content_filter", ERROR_TEXT["content_filter"], status=resp.status_code)
    if resp.status_code >= 500:
        raise AiError("server", f"{ERROR_TEXT['server']}（HTTP {resp.status_code}）", status=resp.status_code)
    raise AiError("bad_request", f"{ERROR_TEXT['bad_request']}（HTTP {resp.status_code}）：{detail}", status=resp.status_code)


def _post(ep: Endpoint, client: httpx.Client | None, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    http = _client(ep, client)
    try:
        resp = http.post(url, headers=headers, json=body)
    except httpx.TimeoutException as exc:
        raise AiError("timeout", ERROR_TEXT["timeout"]) from exc
    except httpx.HTTPError as exc:
        raise AiError("unreachable", f"{ERROR_TEXT['unreachable']}：{type(exc).__name__}") from exc
    finally:
        if client is None:
            http.close()
    _raise_for(resp)
    try:
        return resp.json()
    except ValueError as exc:
        raise AiError("server", "接口返回的不是 JSON") from exc


def _get(ep: Endpoint, client: httpx.Client | None, url: str, headers: dict[str, str]) -> dict[str, Any]:
    http = _client(ep, client)
    try:
        resp = http.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise AiError("timeout", ERROR_TEXT["timeout"]) from exc
    except httpx.HTTPError as exc:
        raise AiError("unreachable", f"{ERROR_TEXT['unreachable']}：{type(exc).__name__}") from exc
    finally:
        if client is None:
            http.close()
    _raise_for(resp)
    try:
        return resp.json()
    except ValueError as exc:
        raise AiError("server", "接口返回的不是 JSON") from exc


# ---------------- OpenAI 兼容 / Azure ----------------


def _openai_url(ep: Endpoint, path: str) -> str:
    if ep.protocol == "azure":
        version = ep.api_version or AZURE_DEFAULT_API_VERSION
        if path == "/models":
            return f"{_base(ep)}/openai/models?api-version={version}"
        return f"{_base(ep)}/openai/deployments/{ep.model}{path}?api-version={version}"
    return f"{_base(ep)}{path}"


def _openai_headers(ep: Endpoint) -> dict[str, str]:
    if ep.protocol == "azure":
        return {"api-key": ep.api_key}
    return {"Authorization": f"Bearer {ep.api_key}"} if ep.api_key else {}


def _openai_messages(req: ChatRequest, ep: Endpoint) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if req.system:
        out.append({"role": "system", "content": req.system})
    ids: dict[str, str] = {}

    def tid(value: str) -> str:
        if "mistral_tool_ids" not in ep.quirks:
            return value
        return ids.setdefault(value, _short_id())

    for m in req.messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": [
                {"id": tid(c.id), "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": tid(m["tool_call_id"]), "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def _openai_body(req: ChatRequest, ep: Endpoint, stream: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {"messages": _openai_messages(req, ep)}
    if ep.protocol != "azure":
        body["model"] = ep.model
    json_mode = req.json_mode if req.schema is not None else None
    if not (json_mode in ("json_schema", "json_object") and "no_max_tokens_with_json" in ep.quirks):
        body["max_tokens"] = req.max_tokens
    if req.temperature is not None and "no_temperature" not in ep.quirks:
        body["temperature"] = req.temperature
    if json_mode == "json_schema":
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": req.schema_name, "strict": True, "schema": req.schema}}
    elif json_mode == "json_object":
        body["response_format"] = {"type": "json_object"}
    if req.tools:
        body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in req.tools]
    if stream:
        body["stream"] = True
    return body


def _openai_finish(reason: str | None) -> str:
    return {"length": "length", "tool_calls": "tool_calls", "function_call": "tool_calls", "content_filter": "content_filter"}.get(reason or "stop", "stop")


def _openai_parse(data: dict[str, Any]) -> ChatResult:
    choices = data.get("choices") or []
    if not choices:
        raise AiError("empty", ERROR_TEXT["empty"])
    msg = choices[0].get("message") or {}
    calls = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"_raw": fn.get("arguments")}
        calls.append(ToolCall(id=c.get("id") or _short_id(), name=fn.get("name", ""), arguments=args if isinstance(args, dict) else {"value": args}))
    usage = data.get("usage") or {}
    content = msg.get("content")
    if isinstance(content, list):  # 少数兼容接口返回分段内容
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ChatResult(text=content or "", tool_calls=calls, finish_reason=_openai_finish(choices[0].get("finish_reason")),
                      input_tokens=int(usage.get("prompt_tokens") or 0), output_tokens=int(usage.get("completion_tokens") or 0),
                      model=data.get("model", ""))


# ---------------- Anthropic Claude ----------------


def _anthropic_headers(ep: Endpoint) -> dict[str, str]:
    return {"x-api-key": ep.api_key, "anthropic-version": ANTHROPIC_VERSION}


def _anthropic_body(req: ChatRequest, ep: Endpoint, stream: bool = False) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for m in req.messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            blocks: list[dict[str, Any]] = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m["tool_calls"]]
            messages.append({"role": "assistant", "content": blocks})
        elif m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                messages[-1]["content"].append(block)  # 连续的工具结果合并成一条 user 消息
            else:
                messages.append({"role": "user", "content": [block]})
        else:
            messages.append({"role": m["role"], "content": m["content"]})
    body: dict[str, Any] = {"model": ep.model, "max_tokens": req.max_tokens, "messages": messages}
    if req.system:
        body["system"] = req.system
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.schema is not None and req.json_mode == "native":
        body["output_config"] = {"format": {"type": "json_schema", "schema": req.schema}}
    if req.tools:
        body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in req.tools]
    if stream:
        body["stream"] = True
    return body


def _anthropic_parse(data: dict[str, Any]) -> ChatResult:
    text, calls = [], []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append(ToolCall(id=block.get("id") or _short_id(), name=block.get("name", ""), arguments=block.get("input") or {}))
    reason = {"max_tokens": "length", "tool_use": "tool_calls", "refusal": "refusal"}.get(data.get("stop_reason") or "end_turn", "stop")
    usage = data.get("usage") or {}
    return ChatResult(text="".join(text), tool_calls=calls, finish_reason=reason, input_tokens=int(usage.get("input_tokens") or 0),
                      output_tokens=int(usage.get("output_tokens") or 0), model=data.get("model", ""))


# ---------------- Google Gemini ----------------


def _gemini_headers(ep: Endpoint) -> dict[str, str]:
    return {"x-goog-api-key": ep.api_key}


def _gemini_model(ep: Endpoint) -> str:
    return ep.model if ep.model.startswith("models/") else f"models/{ep.model}"


def _gemini_body(req: ChatRequest, ep: Endpoint) -> dict[str, Any]:
    contents: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    for m in req.messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            parts: list[dict[str, Any]] = [{"text": m["content"]}] if m.get("content") else []
            for c in m["tool_calls"]:
                names[c.id] = c.name
                parts.append({"functionCall": {"name": c.name, "args": c.arguments}})
            contents.append({"role": "model", "parts": parts})
        elif m["role"] == "tool":
            try:
                payload = json.loads(m["content"])
            except ValueError:
                payload = m["content"]
            part = {"functionResponse": {"name": m.get("name") or names.get(m["tool_call_id"], "tool"), "response": {"result": payload}}}
            if contents and contents[-1]["role"] == "user" and any("functionResponse" in p for p in contents[-1]["parts"]):
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
        else:
            contents.append({"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]})
    gen: dict[str, Any] = {"maxOutputTokens": req.max_tokens}
    if req.temperature is not None:
        gen["temperature"] = req.temperature
    if req.schema is not None and req.json_mode in ("native", "json_object"):
        gen["responseMimeType"] = "application/json"
        if req.json_mode == "native":
            gen["responseJsonSchema"] = req.schema
    body: dict[str, Any] = {"contents": contents, "generationConfig": gen}
    if req.system:
        body["systemInstruction"] = {"parts": [{"text": req.system}]}
    if req.tools:
        body["tools"] = [{"functionDeclarations": [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in req.tools]}]
    return body


def _gemini_parse(data: dict[str, Any]) -> ChatResult:
    cands = data.get("candidates") or []
    if not cands:
        if (data.get("promptFeedback") or {}).get("blockReason"):
            raise AiError("content_filter", ERROR_TEXT["content_filter"])
        raise AiError("empty", ERROR_TEXT["empty"])
    cand = cands[0]
    text, calls = [], []
    for part in (cand.get("content") or {}).get("parts") or []:
        if "text" in part and not part.get("thought"):
            text.append(part["text"])
        elif "functionCall" in part:
            fc = part["functionCall"]
            calls.append(ToolCall(id=fc.get("id") or _short_id(), name=fc.get("name", ""), arguments=fc.get("args") or {}))
    reason = cand.get("finishReason") or "STOP"
    finish = "tool_calls" if calls else {"MAX_TOKENS": "length", "SAFETY": "content_filter", "PROHIBITED_CONTENT": "content_filter", "BLOCKLIST": "content_filter"}.get(reason, "stop")
    usage = data.get("usageMetadata") or {}
    return ChatResult(text="".join(text), tool_calls=calls, finish_reason=finish, input_tokens=int(usage.get("promptTokenCount") or 0),
                      output_tokens=int(usage.get("candidatesTokenCount") or 0), model=data.get("modelVersion", ""))


# ---------------- 统一入口 ----------------


def chat(ep: Endpoint, req: ChatRequest, client: httpx.Client | None = None) -> ChatResult:
    if ep.protocol not in PROTOCOLS:
        raise AiError("unsupported", f"不支持的协议：{ep.protocol}")
    if ep.protocol in ("openai", "azure"):
        result = _openai_parse(_post(ep, client, _openai_url(ep, "/chat/completions"), _openai_headers(ep), _openai_body(req, ep)))
    elif ep.protocol == "anthropic":
        result = _anthropic_parse(_post(ep, client, f"{_base(ep)}/v1/messages", _anthropic_headers(ep), _anthropic_body(req, ep)))
    else:
        url = f"{_base(ep)}/v1beta/{_gemini_model(ep)}:generateContent"
        result = _gemini_parse(_post(ep, client, url, _gemini_headers(ep), _gemini_body(req, ep)))
    if result.finish_reason == "content_filter":
        raise AiError("content_filter", ERROR_TEXT["content_filter"])
    if result.finish_reason == "refusal" and not result.text:
        raise AiError("content_filter", "模型拒绝回答")
    if not result.text and not result.tool_calls:
        if result.finish_reason == "length":
            raise AiError("truncated", ERROR_TEXT["truncated"])
        raise AiError("empty", ERROR_TEXT["empty"])
    return result


def list_models(ep: Endpoint, client: httpx.Client | None = None) -> list[str]:
    if ep.protocol in ("openai", "azure"):
        data = _get(ep, client, _openai_url(ep, "/models"), _openai_headers(ep))
        return sorted({str(m.get("id")) for m in data.get("data") or [] if m.get("id")})
    if ep.protocol == "anthropic":
        data = _get(ep, client, f"{_base(ep)}/v1/models?limit=100", _anthropic_headers(ep))
        return sorted({str(m.get("id")) for m in data.get("data") or [] if m.get("id")})
    data = _get(ep, client, f"{_base(ep)}/v1beta/models?pageSize=200", _gemini_headers(ep))
    out = set()
    for m in data.get("models") or []:
        if "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"]):
            out.add(str(m.get("name", "")).removeprefix("models/"))
    return sorted(x for x in out if x)


def _sse_lines(resp: httpx.Response) -> Iterator[dict[str, Any]]:
    for line in resp.iter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except ValueError:
            continue


def stream_chat(ep: Endpoint, req: ChatRequest, client: httpx.Client | None = None, *, deadline: float | None = None) -> Iterator[str]:
    """流式输出文字片段（问答助手用）。工具调用不走流式。"""
    if ep.protocol in ("openai", "azure"):
        url, headers, body = _openai_url(ep, "/chat/completions"), _openai_headers(ep), _openai_body(req, ep, stream=True)
    elif ep.protocol == "anthropic":
        url, headers, body = f"{_base(ep)}/v1/messages", _anthropic_headers(ep), _anthropic_body(req, ep, stream=True)
    else:
        url, headers, body = f"{_base(ep)}/v1beta/{_gemini_model(ep)}:streamGenerateContent?alt=sse", _gemini_headers(ep), _gemini_body(req, ep)
    http = _client(ep, client)
    try:
        with http.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                resp.read()
                _raise_for(resp)
            for event in _sse_lines(resp):
                if deadline is not None and time.monotonic() > deadline:
                    raise AiError("timeout", ERROR_TEXT["timeout"])
                if ep.protocol in ("openai", "azure"):
                    for ch in event.get("choices") or []:
                        piece = (ch.get("delta") or {}).get("content")
                        if piece:
                            yield piece
                elif ep.protocol == "anthropic":
                    if event.get("type") == "content_block_delta" and (event.get("delta") or {}).get("type") == "text_delta":
                        yield event["delta"].get("text", "")
                    elif event.get("type") == "error":
                        raise AiError("server", ERROR_TEXT["server"])
                else:
                    for cand in event.get("candidates") or []:
                        for part in (cand.get("content") or {}).get("parts") or []:
                            if part.get("text") and not part.get("thought"):
                                yield part["text"]
    except httpx.TimeoutException as exc:
        raise AiError("timeout", ERROR_TEXT["timeout"]) from exc
    except httpx.HTTPError as exc:
        raise AiError("unreachable", f"{ERROR_TEXT['unreachable']}：{type(exc).__name__}") from exc
    finally:
        if client is None:
            http.close()
