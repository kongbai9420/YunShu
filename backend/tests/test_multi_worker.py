"""多 Worker（4.1 P9）：PostgreSQL 上三个真实独立进程争抢、退出与恢复；SQLite 保持单实例。"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select

from app import queue
from app.db import make_engine, make_session_factory
from app.models import Task, WorkerHeartbeat
from app.worker import Worker
from tests.conftest import PG_ADMIN_URL

pg_only = pytest.mark.skipif(not PG_ADMIN_URL, reason="只在 PostgreSQL 上运行")
PROC = Path(__file__).with_name("worker_proc.py")


def _spawn(url: str, role: str = "all", seconds: float = 20, lease: float = 3) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(PROC), url, role, str(seconds), str(lease)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait(cond, timeout: float = 60) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.3)
    raise AssertionError("等待超时")


def test_sqlite_keeps_single_worker(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite 专用")
    factory = make_session_factory(engine)
    a, b = Worker(factory, {}), Worker(factory, {})
    assert not a.multi and a.lock_name == "worker"
    assert a.ensure_lock() and not b.ensure_lock()  # 第二个 Worker 必须等待
    with pytest.raises(ValueError):
        Worker(factory, {}, role="other")


@pg_only
def test_three_processes_share_tasks_once_and_schedules_are_not_duplicated(engine: Engine) -> None:
    url = engine.url.render_as_string(hide_password=False)
    factory = make_session_factory(engine)
    with factory() as db:
        for i in range(60):
            queue.enqueue(db, "test.sleep", payload={"i": i})
        db.commit()
    procs = [_spawn(url, seconds=12), _spawn(url, seconds=12), _spawn(url, role="executor", seconds=12)]
    started = time.monotonic()
    try:
        _wait(lambda: _count(factory, kind="test.sleep", status="succeeded") == 60)
        with factory() as db:
            # 在线状态：三个进程都写了心跳，恰好一个是调度者，executor 不参加竞选
            _wait(lambda: _count_hb(factory) == 3, 30)
            beats = db.scalars(select(WorkerHeartbeat)).all()
            assert sum(b.scheduler for b in beats) == 1
            assert not any(b.scheduler for b in beats if b.role == "executor")
    finally:
        for p in procs:
            p.wait(60)
    elapsed = time.monotonic() - started
    with factory() as db:
        tasks = db.scalars(select(Task).where(Task.kind == "test.sleep")).all()
        assert all(t.status == "succeeded" and t.attempts == 1 for t in tasks)  # 每个只执行一次
        assert len({t.lease_owner for t in tasks}) >= 2  # 由多个 Worker 分担
        marks = db.scalar(select(func.count()).select_from(Task).where(Task.kind == "test.mark")) or 0
        assert 1 <= marks <= elapsed + 3  # 计划只由调度者运行，不按进程数翻倍
        assert db.scalar(select(func.count()).select_from(WorkerHeartbeat)) == 0  # 正常退出时清除自己的心跳


@pg_only
def test_killed_worker_task_is_reaped_and_finished_by_another_process(engine: Engine) -> None:
    url = engine.url.render_as_string(hide_password=False)
    factory = make_session_factory(engine)
    with factory() as db:
        task_id = queue.enqueue(db, "test.block").id
        db.commit()
    victim = _spawn(url, seconds=60)
    try:
        _wait(lambda: _status(factory, task_id) == "running")
        others = [_spawn(url, seconds=75), _spawn(url, seconds=75)]
        victim.kill()  # 模拟进程崩溃：不再续租，调度锁也不再续期
        victim.wait(30)
        # 租约（3 秒）与调度锁（2 秒）过期后由新的调度者回收，再按读任务退避（首次 30 秒）重试
        _wait(lambda: _status(factory, task_id) == "succeeded", 70)
        with factory() as db:
            task = db.get(Task, task_id)
            assert task.attempts == 2  # 由新的调度者回收后重试（成功后错误信息会清空）
    finally:
        for p in [victim, *locals().get("others", [])]:
            if p.poll() is None:
                p.kill()
            p.wait(30)


@pg_only
def test_same_target_control_claim_is_exclusive_across_connections(engine: Engine) -> None:
    from app.control.service import _claim_target

    url = engine.url.render_as_string(hide_password=False)
    engines = [make_engine(url) for _ in range(4)]
    results: list[bool] = []
    barrier = threading.Barrier(len(engines))

    def claim(e: Engine) -> None:
        with make_session_factory(e)() as db:
            barrier.wait()
            ok = _claim_target(db, "192.0.2.200", uuid.uuid4())
            db.commit()
            results.append(ok)

    try:
        threads = [threading.Thread(target=claim, args=(e,)) for e in engines]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert sorted(results) == [False, False, False, True]  # 同一管理地址只有一个能执行，首次建行并发也不报错
    finally:
        for e in engines:
            e.dispose()


def _count(factory, *, kind: str, status: str) -> int:  # type: ignore[no-untyped-def]
    with factory() as db:
        return db.scalar(select(func.count()).select_from(Task).where(Task.kind == kind, Task.status == status)) or 0


def _count_hb(factory) -> int:  # type: ignore[no-untyped-def]
    with factory() as db:
        return db.scalar(select(func.count()).select_from(WorkerHeartbeat)) or 0


def _status(factory, task_id: uuid.UUID) -> str:  # type: ignore[no-untyped-def]
    with factory() as db:
        return db.scalar(select(Task.status).where(Task.id == task_id)) or ""


def test_workers_api_reports_heartbeats_queue_and_recent_failures(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    from tests.api_helpers import add_user, login

    for name, role in (("fake-w-admin", "admin"), ("fake-w-viewer", "viewer")):
        add_user(factory, name, role)
    with factory() as db:
        queue.beat_worker(db, "fake-host:1:a", role="all", scheduler=True, started_at=datetime.now(UTC))
        queue.enqueue(db, "test.sleep")
        failed = queue.enqueue(db, "collect.asset")
        failed.status, failed.error, failed.finished_at = "failed", "fake unreachable", datetime.now(UTC)
        db.commit()
    assert login(app_factory(), "fake-w-viewer").get("/api/workers").status_code == 403
    data = login(app_factory(), "fake-w-admin").get("/api/workers").json()
    assert data["online"] == 1 and data["workers"][0]["scheduler"] is True
    assert data["queue"]["queued"] == 1
    assert data["failures_1h"] == [{"kind": "collect.asset", "error": "fake unreachable", "count": 1}]
