"""P6 离线持续运行演练：真实 Worker / SQLite，假任务，无设备连接。

python -m app.stability_bench --seconds 600 --report <输出文件.json>
只新建临时数据库。实际运行时间与加速后的指标时间分别记录，不作为生产容量承诺。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, text

from app import metrics, migrate, queue
from app.db import make_engine, make_session_factory
from app.metrics import Point
from app.models import Asset, LatestMetric, MetricRollup, SchedulerLock, Task
from app.worker import Schedule, TaskContext, Worker, start_in_thread

START = datetime(2026, 1, 1, tzinfo=UTC)
STEP = timedelta(hours=2)
CRASH_EXIT = 23


def crash_fixture(path: Path) -> None:
    """在专用临时库里留下已认领任务后直接退出，模拟进程未做任何清理。"""
    engine = make_engine(f"sqlite:///{path.as_posix()}")
    factory = make_session_factory(engine)
    past = datetime.now(UTC) - timedelta(minutes=2)
    with factory() as db:
        for risk in ("read", "control"):
            kind = f"p6.resume.{risk}"
            queue.enqueue(db, kind, risk=risk, available_at=past)
            db.commit()
            assert queue.claim(db, "fake-crashed-worker", kinds=(kind,), now=past, lease_seconds=1)
        assert queue.acquire_lock(db, "worker", "fake-crashed-worker", now=past, ttl_seconds=1)
    os._exit(CRASH_EXIT)  # 故意跳过连接池 / Worker / WAL 的正常清理


def run(path: Path, *, seconds: float = 600, devices: int = 10, n_metrics: int = 30) -> dict:
    if seconds <= 0 or devices < 1 or n_metrics < 1:
        raise ValueError("时长、设备数和指标数必须为正数")
    if path.exists():
        raise ValueError("演练必须使用不存在的新数据库")
    engine = make_engine(f"sqlite:///{path.as_posix()}")
    migrate.upgrade(engine)
    factory = make_session_factory(engine)
    ids = [uuid.uuid4() for _ in range(devices)]
    with factory() as db:
        db.add_all(Asset(id=aid, name=f"fake-soak-{i}", form="physical", role="server") for i, aid in enumerate(ids))
        # 超过分钟 / 小时默认保留期的哨兵，后续由真实清理函数删除。
        for resolution, age, span in ((60, 31, 3600), (3600, 400, 86400)):
            bucket = int((START - timedelta(days=age)).timestamp()) // span * span
            db.add(MetricRollup(asset_id=ids[0], resolution=resolution, bucket=bucket, data="{}"))
        db.commit()
    engine.dispose()
    crashed = subprocess.run([sys.executable, "-m", "app.stability_bench", "--crash-fixture", str(path)],
                             capture_output=True, timeout=30)
    if crashed.returncode != CRASH_EXIT:
        raise RuntimeError("异常退出夹具没有按预期退出")

    stats = {"rounds": 0, "samples_written": 0, "read_calls": 0, "control_calls": 0,
             "raw_tables_dropped": 0, "minute_rows_deleted": 0, "hour_rows_deleted": 0}
    timings: list[float] = []
    sizes: list[dict] = []
    begin = time.monotonic()

    def collect(ctx: TaskContext) -> dict:
        tick = time.perf_counter()
        observed = START + stats["rounds"] * STEP
        points = [Point(f"fake.m{i:02}", float(20 + stats["rounds"] % 60), "x") for i in range(n_metrics)]
        with ctx.session_factory() as db:
            for aid in ids:
                metrics.write_sample(db, aid, "fake-soak", points, observed_at=observed, interval_seconds=7200)
            metrics.rollup(db, now=observed + timedelta(minutes=1))
            deleted = metrics.enforce_retention(db, now=observed)
            db.commit()
        stats["rounds"] += 1
        stats["samples_written"] += devices
        stats["raw_tables_dropped"] += deleted["raw_tables"]
        stats["minute_rows_deleted"] += deleted["minute"]
        stats["hour_rows_deleted"] += deleted["hour"]
        timings.append(time.perf_counter() - tick)
        return {"fake": True}

    def resumed_read(ctx: TaskContext) -> dict:
        stats["read_calls"] += 1
        return {"fake": True, "resumed": True}

    def forbidden_control(ctx: TaskContext) -> dict:
        stats["control_calls"] += 1
        raise AssertionError("租约过期的假控制任务不应重发")

    def enqueue(db) -> None:
        queue.enqueue(db, "p6.sample", idempotency_key=f"fake-soak-{stats['rounds']}")

    def measure(phase: int) -> None:
        sizes.append({"phase": phase, "elapsed_s": round(time.monotonic() - begin, 2),
                      "rounds": stats["rounds"], **{
                          name: p.stat().st_size if p.exists() else 0
                          for name, p in (("db_bytes", path), ("wal_bytes", Path(str(path) + "-wal")))}})

    # 两段各运行一半时长，中间完全关闭 Worker 和引擎，重新打开同一个库。
    for phase in (1, 2):
        engine = make_engine(f"sqlite:///{path.as_posix()}")
        factory = make_session_factory(engine)
        if phase == 2:
            with factory() as db:
                assert db.scalar(select(func.count()).select_from(Asset)) == devices
                assert db.scalar(select(func.count()).select_from(LatestMetric)) == devices * n_metrics
                assert db.get(SchedulerLock, "worker") is None
        worker = Worker(factory, {"p6.sample": collect, "p6.resume.read": resumed_read,
                                  "p6.resume.control": forbidden_control}, poll_interval=0.05,
                        schedules=[Schedule("fake.enqueue", 1, enqueue)])
        thread = start_in_thread(worker)
        try:
            deadline = time.monotonic() + seconds / 2
            while time.monotonic() < deadline:
                if not thread.is_alive():
                    raise RuntimeError("Worker 提前退出")
                measure(phase)
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        finally:
            worker.stop()
            thread.join(100)
            if thread.is_alive():
                raise RuntimeError("Worker 未能停止")
            measure(phase)
            engine.dispose()

    engine = make_engine(f"sqlite:///{path.as_posix()}")
    try:
        with make_session_factory(engine)() as db:
            read = db.scalar(select(Task).where(Task.kind == "p6.resume.read"))
            control = db.scalar(select(Task).where(Task.kind == "p6.resume.control"))
            assert read.status == "succeeded" and read.attempts == 2 and stats["read_calls"] == 1
            assert control.status == "indeterminate" and control.attempts == 1 and stats["control_calls"] == 0
            assert db.get(SchedulerLock, "worker") is None
            assert not db.scalar(select(func.count()).select_from(Task).where(Task.status.in_(("failed", "running", "queued"))))
            assert db.scalar(text("PRAGMA integrity_check")) == "ok"
            tables = metrics.raw_tables(db)
            assert len(tables) <= 3
            now = START + (stats["rounds"] - 1) * STEP
            assert all(datetime.strptime(n[-8:], "%Y%m%d").replace(tzinfo=UTC) + timedelta(days=1) > now - timedelta(hours=48)
                       for n in tables)
            assert stats["minute_rows_deleted"] >= 1 and stats["hour_rows_deleted"] >= 1
            rows = db.scalar(select(func.count()).select_from(MetricRollup))
    finally:
        engine.dispose()
    return {"status": "ok", "wall_seconds": round(time.monotonic() - begin, 2),
            "devices": devices, "metrics_per_device": n_metrics, "simulated_hours_per_round": 2,
            "simulated_days": round(stats["rounds"] / 12, 2), **stats,
            "worker_restart_count": 1, "crash_exit_code": crashed.returncode,
            "read_status": read.status, "read_attempts": read.attempts,
            "control_status": control.status, "control_attempts": control.attempts,
            "lock_released": True, "integrity_check": "ok", "rollup_rows": rows,
            "raw_tables_left": tables, "peak_db_wal_bytes": max(s["db_bytes"] + s["wal_bytes"] for s in sizes),
            "final_db_bytes": path.stat().st_size,
            "max_round_ms": round(max(timings) * 1000, 2), "growth_samples": sizes}


def main() -> int:
    parser = argparse.ArgumentParser(description="P6 假任务持续运行与崩溃恢复（只写临时库）")
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--devices", type=int, default=10)
    parser.add_argument("--metrics", type=int, default=30)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--crash-fixture", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.crash_fixture:
        crash_fixture(args.crash_fixture)
    with tempfile.TemporaryDirectory(prefix="yunshu-p6-soak-") as folder:
        result = run(Path(folder) / "soak.db", seconds=args.seconds, devices=args.devices, n_metrics=args.metrics)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
