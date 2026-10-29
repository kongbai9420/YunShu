"""数据库任务队列（持久任务约束）。

只依赖 SQLAlchemy；所有时间按 UTC。调用方负责提交事务（每个函数内部不 commit，
claim 除外：认领必须立刻提交，才能让别的 Worker 看到）。
"""

from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import redact
from app.models import SchedulerLock, Task

RISKS = ("read", "control")
TERMINAL = ("succeeded", "failed", "indeterminate", "cancelled")
# 优先级：控制 20 > 推送事件补采 5 > 采集、通知 0 > 耗时的后台任务（命令巡检、软件清单、漏洞同步与匹配、AI 摘要）。
# 单 Worker 时，一次大批量巡检不会把定时采集挤到过期作废。
BACKGROUND_PRIORITY = -10


def now_utc() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def enqueue(
    db: Session,
    kind: str,
    *,
    payload: dict[str, Any] | None = None,
    asset_id: uuid.UUID | None = None,
    risk: str = "read",
    idempotency_key: str | None = None,
    max_attempts: int = 3,
    priority: int = 0,
    available_at: datetime | None = None,
) -> Task:
    """入队；同一 idempotency_key 已存在时返回已有任务，不重复创建。"""
    if risk not in RISKS:
        raise ValueError(f"未知风险级别：{risk}")
    if risk == "control":
        # 控制任务不自动重试：设备协议不保证幂等，超时后必须先查状态
        max_attempts = 1
    if idempotency_key:
        existing = db.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
        if existing is not None:
            return existing
    task = Task(
        kind=kind,
        risk=risk,
        asset_id=asset_id,
        payload=payload or {},
        idempotency_key=idempotency_key,
        max_attempts=max(1, max_attempts),
        priority=priority,
        available_at=available_at or now_utc(),
    )
    db.add(task)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        # 并发入队同一个幂等键：以先入队的为准
        existing = db.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
        if existing is None:
            raise
        return existing
    return task


@dataclass(frozen=True)
class Lease:
    task_id: uuid.UUID
    token: str
    kind: str
    risk: str
    asset_id: uuid.UUID | None
    payload: dict[str, Any]
    attempt: int


def claim(
    db: Session,
    owner: str,
    *,
    kinds: tuple[str, ...] | None = None,
    lease_seconds: float = 120,
    now: datetime | None = None,
) -> Lease | None:
    """认领一个可执行的任务。单条 UPDATE … WHERE status='queued' … RETURNING，原子完成。"""
    now = now or now_utc()
    candidate = select(Task.id).where(Task.status == "queued", Task.available_at <= now)
    if kinds:
        candidate = candidate.where(Task.kind.in_(kinds))
    candidate = candidate.order_by(Task.priority.desc(), Task.available_at, Task.created_at).limit(1)
    if db.get_bind().dialect.name == "postgresql":
        # 多个 Worker 同时认领时跳过别人正在锁定的候选，避免都争同一行（4.1 P9）
        candidate = candidate.with_for_update(skip_locked=True)
    candidate = candidate.scalar_subquery()
    token = secrets.token_hex(16)
    row = db.execute(
        update(Task)
        .where(Task.id == candidate, Task.status == "queued")
        .values(
            status="running",
            lease_owner=owner,
            lease_token=token,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            attempts=Task.attempts + 1,
            started_at=now,
        )
        .returning(Task.id, Task.kind, Task.risk, Task.asset_id, Task.payload, Task.attempts)
        .execution_options(synchronize_session=False)
    ).first()
    db.commit()
    if row is None:
        return None
    return Lease(task_id=row.id, token=token, kind=row.kind, risk=row.risk, asset_id=row.asset_id, payload=row.payload, attempt=row.attempts)


def _owned(task_id: uuid.UUID, token: str):  # type: ignore[no-untyped-def]
    return and_(Task.id == task_id, Task.lease_token == token, Task.status == "running")


def heartbeat(db: Session, lease: Lease, *, lease_seconds: float = 120, now: datetime | None = None) -> bool:
    """续租；租约已被回收（token 不再匹配）时返回 False，Worker 应放弃该任务。"""
    now = now or now_utc()
    res = db.execute(
        update(Task).where(_owned(lease.task_id, lease.token)).values(lease_expires_at=now + timedelta(seconds=lease_seconds))
    )
    return res.rowcount == 1


def complete(db: Session, lease: Lease, result: dict[str, Any] | None = None, *, now: datetime | None = None) -> bool:
    """写入成功结果。token 不匹配（租约已过期并被回收或重派）时不写，返回 False。"""
    res = db.execute(
        update(Task)
        .where(_owned(lease.task_id, lease.token))
        .values(status="succeeded", result=redact(result or {}), error="", finished_at=now or now_utc(), lease_token="")
    )
    return res.rowcount == 1


def retry_delay(attempt: int) -> timedelta:
    """采集重试退避：30 秒起翻倍，最长 15 分钟。"""
    return timedelta(seconds=min(30 * 2 ** max(0, attempt - 1), 900))


def fail(
    db: Session,
    lease: Lease,
    error: str,
    *,
    retryable: bool = True,
    timed_out: bool = False,
    now: datetime | None = None,
) -> str | None:
    """记录失败，返回任务的新状态；token 不匹配时返回 None。

    - control 任务超时 → indeterminate，不重试；其他失败 → failed。
    - read 任务可重试且次数未用完 → 回到 queued，按退避推迟；否则 failed。
    """
    now = now or now_utc()
    task = db.get(Task, lease.task_id)
    if task is None or task.lease_token != lease.token or task.status != "running":
        return None
    task.error = error[:2000]
    task.lease_token = ""
    task.lease_expires_at = None
    if task.risk == "control":
        task.status = "indeterminate" if timed_out else "failed"
        task.finished_at = now
    elif retryable and task.attempts < task.max_attempts:
        task.status = "queued"
        task.available_at = now + retry_delay(task.attempts)
    else:
        task.status = "failed"
        task.finished_at = now
    return task.status


def reap_expired(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """回收租约过期的 running 任务（Worker 崩溃或卡死）。返回各新状态的数量。"""
    now = now or now_utc()
    out: dict[str, int] = {}
    stale = db.scalars(select(Task).where(Task.status == "running", Task.lease_expires_at < now)).all()
    for task in stale:
        task.lease_token = ""
        task.lease_expires_at = None
        task.error = (task.error + "\n" if task.error else "") + "租约过期，Worker 未按时完成"
        if task.risk == "control":
            task.status = "indeterminate"
            task.finished_at = now
        elif task.attempts < task.max_attempts:
            task.status = "queued"
            task.available_at = now + retry_delay(task.attempts)
        else:
            task.status = "failed"
            task.finished_at = now
        out[task.status] = out.get(task.status, 0) + 1
    return out


def cancel(db: Session, task_id: uuid.UUID) -> bool:
    """取消还没开始的任务；已在执行的不能取消。"""
    res = db.execute(update(Task).where(Task.id == task_id, Task.status == "queued").values(status="cancelled", finished_at=now_utc()))
    return res.rowcount == 1


# ---- Worker 在线状态（4.1 P9）----


def beat_worker(db: Session, owner: str, *, role: str, scheduler: bool, started_at: datetime, lease: Lease | None = None) -> None:
    import socket

    from app import __version__
    from app.models import WorkerHeartbeat

    now = now_utc()
    hb = db.get(WorkerHeartbeat, owner)
    if hb is None:
        hb = WorkerHeartbeat(owner=owner, host=socket.gethostname()[:200], pid=os.getpid(), role=role,
                             version=__version__, started_at=started_at, last_seen=now, scheduler=scheduler, task_kind="")
        db.add(hb)
    hb.last_seen, hb.scheduler, hb.role = now, scheduler, role
    hb.task_id, hb.task_kind = (lease.task_id, lease.kind) if lease else (None, "")


def drop_heartbeat(db: Session, owner: str) -> None:
    from app.models import WorkerHeartbeat

    hb = db.get(WorkerHeartbeat, owner)
    if hb is not None:
        db.delete(hb)
        db.commit()


def prune_heartbeats(db: Session, *, days: int = 7) -> int:
    from sqlalchemy import delete

    from app.models import WorkerHeartbeat

    res = db.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.last_seen < now_utc() - timedelta(days=days)))
    return res.rowcount or 0


# ---- 单实例锁 ----


def acquire_lock(db: Session, name: str, owner: str, *, ttl_seconds: float = 60, now: datetime | None = None) -> bool:
    """获取或续期单实例锁。别的持有者还没过期时返回 False。"""
    now = now or now_utc()
    lock = db.get(SchedulerLock, name)
    if lock is None:
        db.add(SchedulerLock(name=name, owner=owner, expires_at=now + timedelta(seconds=ttl_seconds)))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return False
        return True
    if lock.owner != owner and _aware(lock.expires_at) > now:  # type: ignore[operator]
        db.rollback()
        return False
    res = db.execute(
        update(SchedulerLock)
        .where(SchedulerLock.name == name, SchedulerLock.owner == lock.owner)
        .values(owner=owner, expires_at=now + timedelta(seconds=ttl_seconds))
    )
    db.commit()
    return res.rowcount == 1


def release_lock(db: Session, name: str, owner: str) -> None:
    lock = db.get(SchedulerLock, name)
    if lock is not None and lock.owner == owner:
        db.delete(lock)
        db.commit()
