from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app import queue
from app.db import make_session_factory
from app.models import Task
from app.worker import Schedule, TaskError, Worker
from tests.api_helpers import add_user, login


@pytest.fixture
def factory_(engine: Engine):  # type: ignore[no-untyped-def]
    return make_session_factory(engine)


def T0() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


EARLY = datetime(2025, 12, 31, tzinfo=UTC)


def test_enqueue_idempotent(session: Session) -> None:
    a = queue.enqueue(session, "x", idempotency_key="k1")
    b = queue.enqueue(session, "x", idempotency_key="k1")
    session.commit()
    assert a.id == b.id
    assert session.query(Task).count() == 1


def test_control_tasks_never_retry(session: Session) -> None:
    t = queue.enqueue(session, "power.cycle", risk="control", max_attempts=5)
    assert t.max_attempts == 1
    with pytest.raises(ValueError):
        queue.enqueue(session, "x", risk="dangerous")


def test_long_background_tasks_wait_behind_collection(session: Session) -> None:
    """一大批巡检先入队，之后到期的采集仍先执行（单 Worker 时采集不会被挤到过期作废）。"""
    for i in range(3):
        queue.enqueue(session, "inspection.asset", available_at=EARLY, priority=queue.BACKGROUND_PRIORITY, idempotency_key=f"fake-inspect-{i}")
    queue.enqueue(session, "collect.asset", available_at=T0(), idempotency_key="fake-collect")
    queue.enqueue(session, "control.power", available_at=T0(), priority=20, idempotency_key="fake-control")
    session.commit()
    order = [queue.claim(session, "w1", now=T0()).kind for _ in range(5)]
    assert order == ["control.power", "collect.asset", "inspection.asset", "inspection.asset", "inspection.asset"]


def test_claim_complete_and_stale_token(session: Session) -> None:
    queue.enqueue(session, "x", available_at=EARLY, payload={"a": 1})
    session.commit()
    lease = queue.claim(session, "w1", now=T0())
    assert lease is not None and lease.attempt == 1 and lease.payload == {"a": 1}
    assert queue.claim(session, "w2", now=T0()) is None  # 已被认领

    stale = queue.Lease(**{**lease.__dict__, "token": "wrong"})
    assert queue.complete(session, stale, {"x": 1}) is False
    assert queue.complete(session, lease, {"ok": True, "password": "fake"}) is True
    session.commit()
    task = session.get(Task, lease.task_id)
    assert task.status == "succeeded"
    assert task.result == {"ok": True, "password": "***"}  # 结果写入前脱敏


def test_concurrent_claims_never_share_a_task(factory_) -> None:  # type: ignore[no-untyped-def]
    with factory_() as db:
        for i in range(20):
            queue.enqueue(db, "x", payload={"i": i})
        db.commit()
    got: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        with factory_() as db:
            while True:
                lease = queue.claim(db, name)
                if lease is None:
                    return
                with lock:
                    got.append(str(lease.task_id))

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == 20 and len(set(got)) == 20


def test_retry_backoff_then_failed(session: Session) -> None:
    queue.enqueue(session, "x", available_at=EARLY, max_attempts=2)
    session.commit()
    lease = queue.claim(session, "w", now=T0())
    assert queue.fail(session, lease, "boom", now=T0()) == "queued"
    session.commit()
    assert queue.claim(session, "w", now=T0()) is None  # 退避期内不可认领
    lease2 = queue.claim(session, "w", now=T0() + queue.retry_delay(1) + timedelta(seconds=1))
    assert lease2 is not None and lease2.attempt == 2
    assert queue.fail(session, lease2, "boom again") == "failed"


def test_non_retryable_error_fails_immediately(session: Session) -> None:
    queue.enqueue(session, "x", max_attempts=5)
    session.commit()
    lease = queue.claim(session, "w")
    assert queue.fail(session, lease, "凭据缺失", retryable=False) == "failed"


def test_control_timeout_is_indeterminate(session: Session) -> None:
    queue.enqueue(session, "power.cycle", risk="control")
    session.commit()
    lease = queue.claim(session, "w")
    assert queue.fail(session, lease, "超时", timed_out=True) == "indeterminate"


def test_reap_expired_leases(session: Session) -> None:
    queue.enqueue(session, "collect", available_at=EARLY, max_attempts=3)
    queue.enqueue(session, "power.cycle", available_at=EARLY, risk="control")
    session.commit()
    l1 = queue.claim(session, "dead-worker", lease_seconds=10, now=T0())
    l2 = queue.claim(session, "dead-worker", lease_seconds=10, now=T0())
    assert l1 and l2
    assert queue.reap_expired(session, now=T0() + timedelta(seconds=5)) == {}
    out = queue.reap_expired(session, now=T0() + timedelta(seconds=11))
    session.commit()
    assert out == {"queued": 1, "indeterminate": 1}
    by_kind = {t.kind: t for t in session.scalars(select(Task))}
    assert by_kind["collect"].status == "queued"
    assert by_kind["power.cycle"].status == "indeterminate"
    # 过期 Worker 回来写结果：token 已失效，不能覆盖
    assert queue.complete(session, l1, {"late": True}) is False


def test_heartbeat_extends_lease(session: Session) -> None:
    queue.enqueue(session, "x", available_at=EARLY)
    session.commit()
    lease = queue.claim(session, "w", lease_seconds=10, now=T0())
    assert queue.heartbeat(session, lease, lease_seconds=60, now=T0() + timedelta(seconds=8))
    session.commit()
    assert queue.reap_expired(session, now=T0() + timedelta(seconds=30)) == {}


def test_cancel_only_queued(session: Session) -> None:
    t = queue.enqueue(session, "x")
    session.commit()
    assert queue.cancel(session, t.id)
    t2 = queue.enqueue(session, "x")
    session.commit()
    queue.claim(session, "w")
    assert not queue.cancel(session, t2.id)


def test_single_instance_lock(session: Session) -> None:
    assert queue.acquire_lock(session, "worker", "a", ttl_seconds=30, now=T0())
    assert not queue.acquire_lock(session, "worker", "b", ttl_seconds=30, now=T0() + timedelta(seconds=10))
    assert queue.acquire_lock(session, "worker", "a", ttl_seconds=30, now=T0() + timedelta(seconds=10))  # 续期
    assert queue.acquire_lock(session, "worker", "b", ttl_seconds=30, now=T0() + timedelta(seconds=100))  # 过期接管
    queue.release_lock(session, "worker", "b")
    assert queue.acquire_lock(session, "worker", "c")


def test_worker_runs_handlers(factory_) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def ok(ctx):  # type: ignore[no-untyped-def]
        calls.append(ctx.lease.kind)
        return {"done": True}

    def bad(ctx):  # type: ignore[no-untyped-def]
        raise TaskError("配置错误", retryable=False)

    def crash(ctx):  # type: ignore[no-untyped-def]
        raise RuntimeError("意外")

    w = Worker(factory_, {"ok": ok, "bad": bad, "crash": crash})
    with factory_() as db:
        queue.enqueue(db, "ok", priority=3)
        queue.enqueue(db, "bad", priority=2)
        queue.enqueue(db, "crash", priority=1, max_attempts=1)
        queue.enqueue(db, "unknown-kind")
        db.commit()
    assert w.run_once() == "succeeded"
    assert w.run_once() == "failed"
    assert w.run_once() == "failed"
    assert w.run_once() is None  # 没有处理函数的任务不会被认领
    assert calls == ["ok"]
    with factory_() as db:
        crashed = db.scalar(select(Task).where(Task.kind == "crash"))
        assert "RuntimeError" in crashed.error


def test_worker_timeout(factory_) -> None:  # type: ignore[no-untyped-def]
    release = threading.Event()

    def slow(ctx):  # type: ignore[no-untyped-def]
        release.wait(5)
        return {}

    w = Worker(factory_, {"slow": slow, "power.slow": slow}, timeout_seconds=0.3, lease_seconds=3)
    with factory_() as db:
        queue.enqueue(db, "slow", priority=1)
        queue.enqueue(db, "power.slow", risk="control")
        db.commit()
    assert w.run_once() == "queued"  # 读任务超时：退避后重试
    assert w.run_once() == "indeterminate"  # 控制任务超时：结果不确定，不重试
    release.set()


def test_worker_schedules_and_lock(factory_) -> None:  # type: ignore[no-untyped-def]
    ran: list[int] = []
    clock = iter([0.0, 5.0, 61.0])
    w = Worker(factory_, {}, schedules=[Schedule("enqueue", 60, lambda db: ran.append(1))], clock=lambda: next(clock))
    w.run_schedules()
    w.run_schedules()
    w.run_schedules()
    assert ran == [1, 1]
    assert w.ensure_lock()
    other = Worker(factory_, {})
    assert not other.ensure_lock()
    w.release()
    assert other.ensure_lock()


def test_worker_run_forever_stops(factory_) -> None:  # type: ignore[no-untyped-def]
    w = Worker(factory_, {"system.ping": lambda ctx: {"pong": True}}, poll_interval=0.05)
    with factory_() as db:
        queue.enqueue(db, "system.ping")
        db.commit()
    t = threading.Thread(target=w.run_forever)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        with factory_() as db:
            if db.scalar(select(Task.status)) == "succeeded":
                break
        time.sleep(0.05)
    w.stop()
    t.join(5)
    assert not t.is_alive()
    with factory_() as db:
        assert db.scalar(select(Task.status)) == "succeeded"


def test_tasks_api(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "victor", "viewer")
    c = login(app_factory(), "victor")
    with factory() as db:
        queue.enqueue(db, "system.ping")
        db.commit()
    data = c.get("/api/tasks").json()
    assert data["total"] == 1 and data["counts"] == {"queued": 1}
    assert c.get("/api/worker/status").json()["running"] is False
