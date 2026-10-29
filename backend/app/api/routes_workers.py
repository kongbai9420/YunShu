"""Worker 状态（4.1 P9）：在线 Worker、调度者、队列等待与近期失败原因，用来判断扩容是否有效。只读。"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import editor, get_db
from app.scope import require_all, scope_for
from app.models import Task, User, WorkerHeartbeat

router = APIRouter(prefix="/api/workers", tags=["workers"])
ONLINE_SECONDS = 60  # 超过这么久没有心跳视为离线（Worker 每轮循环都会写心跳）


def _aware(dt: datetime | None) -> datetime | None:
    return None if dt is None else dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@router.get("")
def workers(user: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    require_all(scope_for(db, user), "Worker 状态")
    now = datetime.now(UTC)
    items = []
    for hb in db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.started_at, WorkerHeartbeat.owner)):
        seen = _aware(hb.last_seen)
        items.append({
            "owner": hb.owner, "host": hb.host, "pid": hb.pid, "role": hb.role, "version": hb.version,
            "started_at": _aware(hb.started_at).isoformat(), "last_seen": seen.isoformat(),
            "online": (now - seen).total_seconds() <= ONLINE_SECONDS, "scheduler": hb.scheduler,
            "task_kind": hb.task_kind, "task_id": str(hb.task_id) if hb.task_id else None,
        })
    counts = dict(db.execute(select(Task.status, func.count()).where(Task.status.in_(["queued", "running"])).group_by(Task.status)).all())
    oldest = _aware(db.scalar(select(func.min(Task.available_at)).where(Task.status == "queued", Task.available_at <= now)))
    since = now - timedelta(hours=1)
    # 在 Python 里按“类型 + 错误前 120 字”分组：两种数据库对带参数的表达式分组规则不同
    groups = Counter(
        (kind, (error or "")[:120])
        for kind, error in db.execute(
            select(Task.kind, Task.error).where(Task.status.in_(["failed", "indeterminate"]), Task.finished_at >= since).limit(5000)
        )
    )
    return {
        "workers": items,
        "online": sum(i["online"] for i in items),
        "queue": {"queued": counts.get("queued", 0), "running": counts.get("running", 0),
                  "oldest_wait_seconds": int((now - oldest).total_seconds()) if oldest else 0},
        "failures_1h": [{"kind": k, "error": e, "count": c} for (k, e), c in groups.most_common(10)],
        "multi_worker": db.get_bind().dialect.name == "postgresql",
    }
