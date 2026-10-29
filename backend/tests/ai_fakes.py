"""伪造的大模型接口（4.3 P18a 测试用）：按四种协议返回与真实接口同结构的响应，不访问外网。

能力可配置：是否支持 json_schema / json_object、工具调用、流式；可注入错误（401、404、429、400、审核拦截、空内容、截断）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class FakeLLM:
    protocol: str = "openai"
    json_schema: bool = True
    json_object: bool = True
    native: bool = True
    tools: bool = True
    stream: bool = True
    models: list[str] = field(default_factory=lambda: ["fake-model", "fake-small"])
    error: tuple[int, dict[str, Any], dict[str, str]] | None = None
    empty_times: int = 0          # 前几次返回空内容（模拟 DeepSeek）
    bad_json_times: int = 0       # 前几次返回不合格的 JSON
    answer: dict[str, Any] | None = None
    answers: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # 提示词里含关键词时用对应回答
    tool_plan: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # 依次发起的工具调用，用完后回答 final_text
    final_text: str = "OK"
    finish: str = ""              # 强制结束原因：length / content_filter / refusal
    requests: list[httpx.Request] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport())

    # ---- 通用 ----

    def _json_answer(self, body_text: str) -> dict[str, Any]:
        if "1 加 1" in body_text:  # 连接自检的固定问题
            return {"answer": 2, "unit": "个"}
        for keyword, ans in self.answers:
            if keyword in body_text:
                return ans
        if self.answer is not None:
            return self.answer
        return {"summary": "fake 结论", "items": []}

    def _content(self, body: dict[str, Any], wants_json: bool) -> str:
        if self.empty_times > 0:
            self.empty_times -= 1
            return ""
        if wants_json:
            if self.bad_json_times > 0:
                self.bad_json_times -= 1
                return '{"answer": "two"}'
            return "```json\n" + json.dumps(self._json_answer(json.dumps(body, ensure_ascii=False)), ensure_ascii=False) + "\n```"
        return "OK"

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            status, payload, headers = self.error
            return httpx.Response(status, json=payload, headers=headers)
        if self.protocol in ("openai", "azure"):
            return self._openai(request)
        if self.protocol == "anthropic":
            return self._anthropic(request)
        return self._gemini(request)

    # ---- OpenAI 兼容 ----

    def _openai(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": m, "object": "model"} for m in self.models]})
        body = json.loads(request.content)
        fmt = (body.get("response_format") or {}).get("type")
        if fmt == "json_schema" and not self.json_schema:
            return httpx.Response(400, json={"error": {"message": "response_format json_schema is not supported", "type": "invalid_request_error"}})
        if fmt == "json_object" and not self.json_object:
            return httpx.Response(400, json={"error": {"message": "response_format is not supported"}})
        if body.get("tools") and not self.tools:
            return httpx.Response(400, json={"error": {"message": "tools not supported"}})
        system = next((m["content"] for m in body["messages"] if m["role"] == "system"), "")
        wants_json = bool(fmt) or "JSON Schema" in system
        if body.get("stream"):
            if not self.stream:
                return httpx.Response(400, json={"error": {"message": "stream not supported"}})
            chunks = [{"choices": [{"delta": {"content": p}}]} for p in ("1, ", "2, ", "3, 4, 5")]
            text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        msg: dict[str, Any]
        finish = self.finish or "stop"
        done = sum(1 for m in body["messages"] if m["role"] == "tool")
        offered = {t["function"]["name"] for t in body.get("tools") or []}
        if self.tool_plan and ((offered and self.tool_plan[0][0] in offered) or (not offered and done > 0)):  # 只在助手对话里按计划走
            if body.get("tools") and done < len(self.tool_plan):
                name, args = self.tool_plan[done]
                msg = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"call_{done}", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
                return httpx.Response(200, json={"id": "x", "model": "fake", "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls"}],
                                                 "usage": {"prompt_tokens": 30, "completion_tokens": 5}})
            final = {"role": "assistant", "content": self.final_text}
            return httpx.Response(200, json={"id": "x", "model": "fake", "choices": [{"index": 0, "message": final, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 40, "completion_tokens": 10}})
        if body.get("tools") and not any(m["role"] == "tool" for m in body["messages"]):
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_abc", "type": "function", "function": {"name": body["tools"][0]["function"]["name"], "arguments": json.dumps({"site": "演示机房 A"}, ensure_ascii=False)}}]}
            finish = "tool_calls"
        else:
            msg = {"role": "assistant", "content": self._content(body, wants_json)}
        return httpx.Response(200, json={"id": "chatcmpl-fake", "model": body.get("model", "deployment"), "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                         "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}})

    # ---- Anthropic ----

    def _anthropic(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != "fake-key" or request.headers.get("anthropic-version") != "2023-06-01":
            return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": m, "type": "model"} for m in self.models], "has_more": False})
        body = json.loads(request.content)
        if "output_config" in body and not self.native:
            return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "output_config is not supported for this model"}})
        if body.get("stream"):
            events = [{"type": "message_start"}, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "1, 2, "}},
                      {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "3, 4, 5"}}, {"type": "message_stop"}]
            return httpx.Response(200, text="".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events))
        if body.get("tools") and not any(isinstance(m["content"], list) and any(b.get("type") == "tool_result" for b in m["content"]) for m in body["messages"]):
            content = [{"type": "tool_use", "id": "toolu_01", "name": body["tools"][0]["name"], "input": {"site": "演示机房 A"}}]
            stop = "tool_use"
        else:
            wants = "output_config" in body or "JSON Schema" in body.get("system", "")
            text = self._content(body, wants)
            content = [{"type": "text", "text": text}] if text else []
            stop = {"length": "max_tokens", "refusal": "refusal"}.get(self.finish, "end_turn")
        return httpx.Response(200, json={"id": "msg_fake", "type": "message", "role": "assistant", "model": body["model"], "content": content,
                                         "stop_reason": stop, "usage": {"input_tokens": 20, "output_tokens": 8}})

    # ---- Gemini ----

    def _gemini(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-goog-api-key") != "fake-key":
            return httpx.Response(403, json={"error": {"code": 403, "message": "API key not valid", "status": "PERMISSION_DENIED"}})
        path = request.url.path
        if path.endswith("/v1beta/models"):
            return httpx.Response(200, json={"models": [{"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]} for m in self.models]
                                             + [{"name": "models/fake-embedding", "supportedGenerationMethods": ["embedContent"]}]})
        body = json.loads(request.content)
        gen = body.get("generationConfig") or {}
        if "responseJsonSchema" in gen and not self.native:
            return httpx.Response(400, json={"error": {"code": 400, "message": "Unknown name \"responseJsonSchema\"", "status": "INVALID_ARGUMENT"}})
        if path.endswith(":streamGenerateContent"):
            parts = [{"candidates": [{"content": {"parts": [{"text": t}], "role": "model"}}]} for t in ("1, 2, ", "3, 4, 5")]
            return httpx.Response(200, text="".join(f"data: {json.dumps(p)}\n\n" for p in parts))
        if self.finish == "content_filter":
            return httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]})
        if body.get("tools") and not any(any("functionResponse" in p for p in c["parts"]) for c in body["contents"]):
            parts = [{"functionCall": {"name": body["tools"][0]["functionDeclarations"][0]["name"], "args": {"site": "演示机房 A"}}}]
        else:
            wants = gen.get("responseMimeType") == "application/json" or "JSON Schema" in json.dumps(body.get("systemInstruction"), ensure_ascii=False)
            parts = [{"text": self._content(body, wants)}]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": parts, "role": "model"}, "finishReason": "MAX_TOKENS" if self.finish == "length" else "STOP"}],
                                         "usageMetadata": {"promptTokenCount": 15, "candidatesTokenCount": 6}, "modelVersion": "fake-gemini"})
