"""结构化（JSON）输出：按能力逐级降级，本地一律按 JSON Schema 校验。

各家支持差别很大（见 dev 文档 2.4 节）：OpenAI / Claude / Gemini 和少数最新国产模型支持按 schema 严格输出；
DeepSeek、智谱只有 json_object；本地模型因模型而异。所以不假设，按能力表从高到低尝试，
输出不合格最多修复重试一次，仍不合格就报 invalid_output，由调用方只显示程序算出的结果。

本模块的 schema 约定（同时满足 OpenAI 严格模式）：对象一律 additionalProperties: false，
所有属性都列入 required，可选值用 ["string", "null"] 表示。
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from app.ai.protocols import JSON_MODES, AiError, ChatRequest, ChatResult, Endpoint, chat

# 发给厂商时去掉的约束关键字：Claude 等不支持数值 / 长度约束，严格模式下会报 400；本地校验仍然检查
_LOCAL_ONLY = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems", "pattern", "format"}


def provider_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: provider_schema(v) for k, v in schema.items() if k not in _LOCAL_ONLY}
    if isinstance(schema, list):
        return [provider_schema(v) for v in schema]
    return schema


def modes_for(protocol: str) -> tuple[str, ...]:
    """每种协议可用的结构化方式（从高到低）。"""
    if protocol in ("openai", "azure"):
        return ("json_schema", "json_object", "prompt")
    if protocol == "anthropic":
        return ("native", "prompt")
    return ("native", "json_object", "prompt")  # gemini：json_object = 只指定 application/json


_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """够用的 JSON Schema 子集校验（type / properties / required / additionalProperties / items / enum / 数值与长度约束）。"""
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not any(_TYPES.get(t, lambda _v: True)(value) for t in allowed):
            return [f"{path} 应为 {'/'.join(allowed)}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} 只能是 {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}.{key} 缺失")
        if schema.get("additionalProperties") is False:
            errors.extend(f"{path}.{k} 不应出现" for k in value if k not in props)
        for k, sub in props.items():
            if k in value:
                errors.extend(validate(value[k], sub, f"{path}.{k}"))
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path} 最多 {schema['maxItems']} 项")
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path} 至少 {schema['minItems']} 项")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path} 超过 {schema['maxLength']} 字")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path} 少于 {schema['minLength']} 字")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} 不应小于 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} 不应大于 {schema['maximum']}")
    return errors


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def parse_json(text: str) -> Any:
    """容忍 ```json 代码块和前后说明文字；只取第一个完整的 JSON 对象。"""
    s = text.strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1)
    try:
        return json.loads(s)
    except ValueError:
        start = s.find("{")
        if start < 0:
            raise
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(s[start:i + 1])
        raise


def _instruction(schema: dict[str, Any]) -> str:
    return ("请只输出一个 JSON 对象，不要输出任何其他文字或代码块标记。JSON 必须符合下面的 JSON Schema：\n"
            + json.dumps(schema, ensure_ascii=False))


def call_json(
    ep: Endpoint,
    messages: list[dict[str, Any]],
    schema: dict[str, Any],
    *,
    system: str = "",
    modes: Sequence[str] | None = None,
    max_tokens: int = 2048,
    client: httpx.Client | None = None,
    schema_name: str = "result",
) -> tuple[Any, str, ChatResult]:
    """返回 (校验通过的对象, 实际使用的方式, 最后一次的原始结果)。"""
    allowed = [m for m in (modes or modes_for(ep.protocol)) if m in modes_for(ep.protocol) and m in JSON_MODES]
    if not allowed:
        allowed = ["prompt"]
    sent_schema = provider_schema(schema)
    # 不论哪一级都在系统提示里写明结构：json_object 模式下 DeepSeek、通义要求提示词含 “JSON”
    sys_text = (system + "\n\n" if system else "") + _instruction(schema)
    last_error: AiError | None = None
    for mode in allowed:
        req = ChatRequest(messages=list(messages), system=sys_text, schema=sent_schema, schema_name=schema_name,
                          json_mode=mode, max_tokens=max_tokens)
        try:
            result = _chat_retry_empty(ep, req, client)
        except AiError as exc:
            # 这一级不被支持（400）就降一级；鉴权、限流、超时等直接报出
            if exc.kind in ("bad_request", "unsupported"):
                last_error = exc
                continue
            raise
        for attempt in range(2):
            try:
                value = parse_json(result.text)
                errors = validate(value, schema)
            except ValueError:
                value, errors = None, ["输出不是合法的 JSON"]
            if not errors:
                return value, mode, result
            if attempt == 1:
                break
            req.messages = [*messages, {"role": "assistant", "content": result.text},
                            {"role": "user", "content": "上面的输出不符合要求：" + "；".join(errors[:8]) + "。请只输出修正后的 JSON。"}]
            result = _chat_retry_empty(ep, req, client)
        last_error = AiError("invalid_output", "模型输出不符合要求的格式：" + "；".join(errors[:5]))
    raise last_error or AiError("invalid_output", "模型输出不符合要求的格式")


def _chat_retry_empty(ep: Endpoint, req: ChatRequest, client: httpx.Client | None) -> ChatResult:
    # DeepSeek 官方说明 JSON 模式偶尔返回空内容：空内容重试一次
    try:
        return chat(ep, req, client)
    except AiError as exc:
        if exc.kind != "empty":
            raise
        return chat(ep, req, client)
