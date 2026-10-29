"""只读 MCP 接口（4.3 P18h）：让 Claude、Cursor、Dify 等外部 AI 工具用只读 API 令牌查询云枢的数据。

- 传输：MCP Streamable HTTP（POST /mcp，JSON-RPC 2.0，单条请求单条 JSON 响应；不提供 SSE 推送，GET 返回 405）；
- 鉴权：“Authorization: Bearer ys_…” 只读 API 令牌，权限与资产范围跟随令牌所属账号；
- 工具：与问答助手相同的只读工具（app/ai/tools.py），每次调用都在只读事务里执行；没有任何写操作；
- 默认关闭：在“平台设置 → AI”里开启“只读 MCP 接口”后可用；每次调用记入 AI 调用记录（功能 mcp）。
数据直接返回给调用方自己的 AI 工具，不经过本平台配置的模型，也不做脱敏。
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from app import __version__, ai_runtime
from app.ai import tools
from app.ai.readonly import readonly_session
from app.api.routes_reports import TOKEN_PREFIX, _hash
from app.models import ApiToken, User
from app.scope import scope_for

router = APIRouter(tags=["mcp"])
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
FEATURE = "mcp"
INSTRUCTIONS = ("云枢运维平台的只读查询接口：资产、指标、告警、软件版本与漏洞。只能查询，不能执行任何操作；"
                "结果按令牌所属账号的资产范围过滤。返回的 link 是平台页面的相对地址（#/…）。")


def _rpc_error(id_: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}, status_code=status)


def _result(id_: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})


@router.get("/mcp", include_in_schema=False)
def mcp_get() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp", include_in_schema=False)
async def mcp(request: Request, authorization: str = Header(""), origin: str = Header("")) -> Response:
    # 防 DNS 重绑定：浏览器带来的 Origin 必须与本站一致（命令行与桌面客户端通常不带 Origin）
    if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
        return _rpc_error(None, -32000, "Origin 不被允许", 403)
    factory = request.app.state.session_factory
    with factory() as db:
        if not (ai_runtime.settings_row(db).features or {}).get(FEATURE, {}).get("enabled"):
            db.commit()
            return _rpc_error(None, -32000, "只读 MCP 接口未开启（平台设置 → AI）", 403)
        raw = authorization.removeprefix("Bearer ").strip()
        token = db.scalar(select(ApiToken).where(ApiToken.token_hash == _hash(raw))) if raw.startswith(TOKEN_PREFIX) else None
        user = db.get(User, token.user_id) if token else None
        if token is None or user is None or not user.is_active:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "令牌无效"}}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        now = datetime.now(UTC)
        if token.last_used_at is None or (now - (token.last_used_at if token.last_used_at.tzinfo else token.last_used_at.replace(tzinfo=UTC))).total_seconds() > 300:
            token.last_used_at = now
        scope = scope_for(db, user)
        visible = None if scope.all else set(db.scalars(scope.assets()))
        user_name = user.username
        db.commit()
    try:
        body = json.loads(await request.body())
    except ValueError:
        return _rpc_error(None, -32700, "Parse error", 400)
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
        return _rpc_error(body.get("id") if isinstance(body, dict) else None, -32600, "Invalid Request", 400)
    method, id_, params = body["method"], body.get("id"), body.get("params") or {}
    if id_ is None:  # 通知（如 notifications/initialized）：不需要响应
        return Response(status_code=202)
    if method == "initialize":
        requested = params.get("protocolVersion")
        return _result(id_, {"protocolVersion": requested if requested in PROTOCOLS else PROTOCOLS[0],
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "yunshu-ops", "title": "云枢运维平台（只读）", "version": __version__},
                             "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": [{"name": t.name, "description": t.description, "inputSchema": {"type": "object", **t.parameters},
                                        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}}
                                       for t in tools.definitions()]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in tools.TOOLS:
            return _rpc_error(id_, -32602, f"没有这个工具：{name}")
        started = time.perf_counter()
        with readonly_session(factory) as rdb:
            out = tools.call(tools.ToolContext(db=rdb, visible=visible, now=datetime.now(UTC)), name, args if isinstance(args, dict) else {})
        with factory() as db:
            from app.models import AiUsage

            db.add(AiUsage(at=datetime.now(UTC), user_name=user_name, feature=FEATURE, model=str(name)[:200], mode="tool", ok="error" not in out,
                           error_kind="tool" if "error" in out else "", latency_ms=int((time.perf_counter() - started) * 1000)))
            db.commit()
        text = json.dumps(out, ensure_ascii=False, default=str)
        return _result(id_, {"content": [{"type": "text", "text": text}], "structuredContent": json.loads(text), "isError": "error" in out})
    return _rpc_error(id_, -32601, f"Method not found: {method}")
