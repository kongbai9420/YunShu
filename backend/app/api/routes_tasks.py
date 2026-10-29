"""任务队列与 Worker 状态（只读）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, reader
from app.models import Asset, SchedulerLock, Task, User
from app.worker import LOCK_NAME

router = APIRouter(prefix="/api", tags=["tasks"])


class TaskOut(BaseModel):
    id: uuid.UUID
    kind: str
    risk: str
    asset_id: uuid.UUID | None
    status: str
    attempts: int
    max_attempts: int
    available_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str
    result: dict


@router.get("/tasks")
def list_tasks(
    status: str | None = None,
    kind: str | None = None,
    asset_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(reader),
    db: Session = Depends(get_db),
) -> dict:
    from app.scope import restrict, scope_for

    scope = scope_for(db, user)
    # 范围外资产的任务与不属于任何资产的任务（发现、通知等）只有全部可见的账号能看到（4.1 P11）
    stmt = restrict(select(Task), Task.asset_id, scope)
    if status:
        stmt = stmt.where(Task.status.in_(status.split(",")))
    if kind:
        stmt = stmt.where(Task.kind == kind)
    if asset_id:
        stmt = stmt.where(Task.asset_id == asset_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Task.created_at.desc()).limit(limit)).all()
    counts = dict(db.execute(restrict(select(Task.status, func.count()), Task.asset_id, scope).group_by(Task.status)).all())
    names = dict(db.execute(select(Asset.id, Asset.name).where(Asset.id.in_({t.asset_id for t in rows if t.asset_id}))).all())
    return {"items": [TaskOut.model_validate(t, from_attributes=True).model_dump(mode="json") | {"asset_name": names.get(t.asset_id)} for t in rows], "total": total, "counts": counts}


@router.get("/worker/status")
def worker_status(_: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    # 单实例（SQLite）的锁名为 worker；多 Worker（PostgreSQL，4.1 P9）只有调度职责持锁，锁名为 scheduler
    lock = db.get(SchedulerLock, LOCK_NAME) or db.get(SchedulerLock, "scheduler")
    if lock is None:
        return {"running": False, "owner": None, "expires_at": None}
    expires = lock.expires_at if lock.expires_at.tzinfo else lock.expires_at.replace(tzinfo=UTC)
    return {"running": expires > datetime.now(UTC), "owner": lock.owner, "expires_at": expires.isoformat()}
