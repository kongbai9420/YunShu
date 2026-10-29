"""SSE 实时推送接口 GET /api/events（三个角色都可读，需要已登录且已改初始密码）。

不使用 get_db 依赖：事件流可能持续十几分钟，鉴权用一个短会话做完就关闭，流里每轮查询各自开关会话。
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.deps import SESSION_COOKIE
from app.auth import resolve_session
from app.events import DEFAULT_CONFIG, Cursor, StreamConfig, event_stream

router = APIRouter(prefix="/api", tags=["events"])
READ_ROLES = ("admin", "operator", "viewer")


def _check(request: Request, token: str | None, *, commit: bool) -> tuple[set[str] | None, bool]:
    """鉴权；返回（可见资产 ID 集合，None 为全部可见）与是否推送任务计数（4.1 P11）。"""
    from sqlalchemy import select

    from app.scope import scope_for

    with request.app.state.session_factory() as db:
        resolved = resolve_session(db, token)
        if resolved is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录或会话已过期")
        user = resolved[1]
        if user.role not in READ_ROLES:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "当前角色无权执行此操作")
        if user.require_password_change:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "请先修改初始密码")
        scope = scope_for(db, user)
        visible = None if scope.all else {str(i) for i in db.scalars(select(scope.assets().subquery().c.id))}
        if commit:
            db.commit()  # 与其他读接口一样续期会话
        else:
            db.rollback()  # 流中的定期检查只读，不续期
        return visible, scope.all


@router.get("/events")
def events(
    request: Request,
    last_event_id: str | None = Query(None, max_length=64, description="浏览器重新建立连接时用；与 Last-Event-ID 头等价"),
    last_event_header: str | None = Header(None, alias="Last-Event-ID", max_length=64),
) -> StreamingResponse:
    token = request.cookies.get(SESSION_COOKIE)
    visible, tasks_allowed = _check(request, token, commit=True)
    view = {"visible": visible, "tasks": tasks_allowed}

    def still_authorized() -> bool:
        try:
            # 每次复核同时刷新范围：管理员收窄范围后，已连接的推送随之收窄
            view["visible"], view["tasks"] = _check(request, token, commit=False)
        except HTTPException:
            return False
        return True

    def allow(name: str, data: dict) -> dict | None:
        if name == "tasks":
            return data if view["tasks"] else None
        if view["visible"] is None:
            return data
        if name == "metrics":
            return data if data.get("asset_id") in view["visible"] else None
        if name == "collection":
            items = [i for i in data.get("items", []) if i.get("asset_id") in view["visible"]]
            # 快照帧即使过滤后为空也要发：页面靠它结束“读取中”
            return {**data, "items": items} if items or data.get("snapshot") else None
        return data

    cfg: StreamConfig = getattr(request.app.state, "sse_config", DEFAULT_CONFIG)
    cursor = Cursor.parse(last_event_header or last_event_id)
    return StreamingResponse(
        event_stream(request.app.state.session_factory, cursor=cursor, cfg=cfg, still_authorized=still_authorized, allow=allow),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
