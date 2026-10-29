"""服务器告警板块（4.4 P20c）：查看 BMC 日志里的严重 / 警告事件，标记已处理或重新打开。

只读账号可以查看；运维以上可以处理。按资产范围过滤（4.1 P11），范围外的事件当作不存在。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import editor, get_db, reader
from app.models import Asset, ServerEvent, User
from app.scope import restrict, scope_for
from app.server_events import CATEGORIES, SEVERITIES

router = APIRouter(prefix="/api/server-events", tags=["server-events"])
STATES = ("open", "handled", "archived")


def _out(e: ServerEvent, name: str) -> dict[str, Any]:
    return {
        "id": str(e.id), "asset_id": str(e.asset_id), "asset_name": name, "source": e.source, "occurred_at": e.occurred_at.isoformat(),
        "severity": e.severity, "category": e.category, "category_label": CATEGORIES.get(e.category, e.category), "message": e.message,
        "first_seen_at": e.first_seen_at.isoformat(), "state": e.state, "handled_by": e.handled_by,
        "handled_at": e.handled_at.isoformat() if e.handled_at else None, "note": e.note,
    }


@router.get("")
def list_events(
    state: str = "open", severity: str = "", category: str = "", asset_id: uuid.UUID | None = None, q: str = "",
    days: int = Query(30, ge=1, le=180), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    user: User = Depends(reader), db: Session = Depends(get_db),
) -> dict[str, Any]:
    scope = scope_for(db, user)
    base = restrict(select(ServerEvent, Asset.name).join(Asset, Asset.id == ServerEvent.asset_id), ServerEvent.asset_id, scope)
    base = base.where(ServerEvent.occurred_at >= datetime.now(UTC) - timedelta(days=days))
    if state in STATES:
        base = base.where(ServerEvent.state == state)
    if severity in SEVERITIES:
        base = base.where(ServerEvent.severity == severity)
    elif severity == "alarm":  # 严重 + 警告
        base = base.where(ServerEvent.severity.in_(("critical", "warning")))
    if category in CATEGORIES:
        base = base.where(ServerEvent.category == category)
    if asset_id:
        base = base.where(ServerEvent.asset_id == asset_id)
    if q.strip():
        like = f"%{q.strip()[:100]}%"
        base = base.where(or_(ServerEvent.message.ilike(like), Asset.name.ilike(like)))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(base.order_by(ServerEvent.occurred_at.desc()).limit(limit).offset(offset)).all()
    return {"total": total, "items": [_out(e, n) for e, n in rows], "categories": CATEGORIES}


@router.get("/summary")
def summary(user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    """待处理的严重 / 警告条数与涉及的服务器数，供板块顶部与侧栏提示。"""
    stmt = restrict(select(ServerEvent.severity, func.count(), func.count(func.distinct(ServerEvent.asset_id))), ServerEvent.asset_id,
                    scope_for(db, user)).where(ServerEvent.state == "open").group_by(ServerEvent.severity)
    counts = {sev: {"events": n, "servers": a} for sev, n, a in db.execute(stmt).all()}
    return {"open": {sev: counts.get(sev, {"events": 0, "servers": 0}) for sev in ("critical", "warning")}}


class HandleRequest(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    state: str  # handled / open
    note: str = Field(default="", max_length=500)


@router.post("/state")
def change_state(body: HandleRequest, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.state not in ("handled", "open"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "只能标记为“已处理”或重新打开")
    if body.state == "handled" and not body.note.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "请写明处理结果（例如：已更换 2 号电源）")
    stmt = restrict(select(ServerEvent), ServerEvent.asset_id, scope_for(db, actor)).where(ServerEvent.id.in_(body.ids))
    events = list(db.scalars(stmt))
    if len(events) != len(set(body.ids)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "部分事件不存在或你无权查看")
    who, now = actor.display_name or actor.username, datetime.now(UTC)
    for e in events:
        e.state = body.state
        if body.state == "handled":
            e.handled_by, e.handled_at, e.note = who, now, body.note.strip()
        else:
            e.handled_by, e.handled_at = "", None
    for aid in {e.asset_id for e in events}:
        n = sum(1 for e in events if e.asset_id == aid)
        audit.record(db, actor, f"server_event.{body.state}", target_type="asset", target_id=aid,
                     summary=f"{'标记已处理' if body.state == 'handled' else '重新打开'} {n} 条服务器告警" + (f"：{body.note.strip()[:200]}" if body.note.strip() else ""))
    return {"updated": len(events)}
