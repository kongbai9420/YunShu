"""4.1 P12 容量测量：真实 API（HTTP）+ 真实 Worker + 真实数据库的混合负载，设备采集用伪造数据。

    python -m app.capacity_bench --assets 100 --sessions 5 --seconds 180 --report <输出文件.json>
    python -m app.capacity_bench --url postgresql+psycopg://…/空库 --assets 1000 --sessions 20 --workers 3 --seconds 300

- 只使用临时 SQLite 或调用方给的**空** PostgreSQL 库，不连接任何设备。
- 先写入历史（原始 / 分钟 / 小时汇总，按模拟时钟），再按真实时间运行：每个采集周期为每台资产入队一个任务，
  Worker 写入固定数量的伪造指标；同时 N 个会话循环请求常用页面接口。
- 结果是这台机器上的测量值，不是性能承诺（路线图第 5 节）。
"""

from __future__ import annotations

import argparse
import json
import random
import os
import socket
import subprocess
import statistics
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import func, select

from app import metrics, migrate, queue
from app.alert_defaults import ensure_rules
from app.auth import hash_password
from app.db import make_engine, make_session_factory
from app.metrics import Point
from app.models import Alert, AlertRule, Asset, Task, User
from app.worker import Schedule, TaskContext, Worker, start_in_thread

KIND = "bench.collect"
PASSWORD = "fake-bench-password-123"
ENDPOINTS = (
    ("资产列表", "/api/assets?limit=50"),
    ("资产搜索", "/api/assets?limit=50&q=fake-cap-01"),
    ("资源全景", "/api/overview/resources?limit=25"),
    ("仪表盘汇总", "/api/dashboard/summary"),
    ("告警汇总", "/api/alerts/summary"),
    ("告警列表", "/api/alerts?limit=50"),
    ("任务列表", "/api/tasks?limit=50"),
    ("拓扑图", "/api/topology/graph"),
    ("指标曲线", "/api/assets/{asset}/metrics/series?key=fake.m00&resolution=auto"),
)


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))] * 1000, 1)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seed(factory, n_assets: int, n_metrics: int, history_hours: float, interval: int) -> tuple[list[uuid.UUID], dict]:
    rng = random.Random(42)
    ids = [uuid.uuid4() for _ in range(n_assets)]
    t0 = time.perf_counter()
    with factory() as db:
        db.add_all(Asset(id=aid, name=f"fake-cap-{i:04d}", form="physical", role="server") for i, aid in enumerate(ids))
        db.add(User(username="bench", role="operator", password_hash=hash_password(PASSWORD), require_password_change=False))
        ensure_rules(db)
        db.commit()
        rule = db.scalars(select(AlertRule)).first()
        now = datetime.now(UTC)
        for i, aid in enumerate(ids[: max(1, n_assets // 10)]):  # 一成资产带历史告警
            db.add(Alert(rule_id=rule.id, asset_id=aid, asset_name=f"fake-cap-{i:04d}", metric_key="fake.m00",
                         state="resolved" if i % 3 else "firing", title="fake 容量测试告警", value=90.0,
                         first_seen_at=now - timedelta(hours=1), last_seen_at=now))
        db.commit()
    keys = [f"fake.m{k:02d}" for k in range(n_metrics)]
    rounds = int(history_hours * 3600 // interval)
    start = datetime.now(UTC) - timedelta(hours=history_hours)
    with factory() as db:
        for step in range(rounds):
            observed = start + timedelta(seconds=step * interval)
            for aid in ids:
                metrics.write_sample(db, aid, "bench", [Point(k, round(rng.uniform(20, 80), 1), "x") for k in keys],
                                     observed_at=observed, interval_seconds=interval)
            db.commit()
            if step % max(1, 600 // interval) == 0:
                metrics.rollup(db, now=observed)
                db.commit()
        metrics.rollup(db, now=datetime.now(UTC))
        db.commit()
    return ids, {"history_rounds": rounds, "history_values": rounds * n_assets * n_metrics,
                 "seed_seconds": round(time.perf_counter() - t0, 1)}


def run(url: str | None, *, n_assets: int, n_metrics: int, sessions: int, workers: int, seconds: float,
        interval: int, history_hours: float) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="yunshu-cap-"))
    url = url or f"sqlite:///{(tmp / 'cap.db').as_posix()}"
    engine = make_engine(url)
    if engine.dialect.name == "postgresql":
        from sqlalchemy import inspect

        if inspect(engine).get_table_names():
            raise SystemExit("容量测量只使用空的 PostgreSQL 库")
    elif workers > 1:
        raise SystemExit("SQLite 只允许一个 Worker（单写者）")
    migrate.upgrade(engine)
    factory = make_session_factory(engine)
    ids, seeded = seed(factory, n_assets, n_metrics, history_hours, interval)

    # ---- Worker：每个周期为每台资产入队一个伪造采集任务 ----
    stop = threading.Event()
    waits: list[float] = []
    durations: list[float] = []
    cycles: list[int] = []
    lock = threading.Lock()
    keys = [f"fake.m{k:02d}" for k in range(n_metrics)]

    def collect(ctx: TaskContext) -> dict:
        tick = time.perf_counter()
        payload = ctx.lease.payload or {}
        with ctx.session_factory() as db:
            metrics.write_sample(db, uuid.UUID(payload["asset"]), "bench",
                                 [Point(k, 42.0, "x") for k in keys], observed_at=datetime.now(UTC), interval_seconds=interval)
            db.commit()
        with lock:
            waits.append(time.time() - payload["enqueued"])
            durations.append(time.perf_counter() - tick)
        return {"fake": True}

    def enqueue(db) -> None:
        if stop.is_set():
            return  # 测量结束后不再入队，收尾只等已入队的周期完成
        cycle = len(cycles)
        cycles.append(cycle)
        stamp = time.time()
        for aid in ids:
            queue.enqueue(db, KIND, payload={"asset": str(aid), "enqueued": stamp}, asset_id=aid,
                          idempotency_key=f"bench-{cycle}-{aid}")

    pool = [Worker(factory, {KIND: collect}, poll_interval=0.05,
                   schedules=[Schedule("bench.enqueue", interval, enqueue)] if i == 0 else [],
                   role="all" if i == 0 else "executor") for i in range(workers)]

    # ---- API：独立进程里的真实 uvicorn（与部署相同；和会话 / Worker 同进程会争 GIL，延迟失真）----
    port = _free_port()
    env = {**os.environ, "YUNSHU_DATABASE_URL": url, "YUNSHU_DATA_DIR": str(tmp / "data"),
           "YUNSHU_MASTER_KEY_FILE": str(tmp / "keys" / "master.key"), "YUNSHU_BACKUP_INTERVAL_HOURS": "0",
           "YUNSHU_SETUP_WIZARD": "0", "YUNSHU_EMBEDDED_WORKER": "0", "YUNSHU_AUTO_MIGRATE": "0"}
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1",
                            "--port", str(port), "--log-level", "warning"], cwd=Path(__file__).resolve().parent.parent, env=env)
    deadline = time.monotonic() + 60
    while True:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline or api.poll() is not None:
            api.kill()
            raise SystemExit("API 进程没有启动")
        time.sleep(0.3)
    base = f"http://127.0.0.1:{port}"
    latency: dict[str, list[float]] = {name: [] for name, _ in ENDPOINTS}
    errors: dict[str, int] = {name: 0 for name, _ in ENDPOINTS}

    def session(idx: int) -> None:
        rng = random.Random(idx)
        with httpx.Client(base_url=base, timeout=30) as c:
            assert c.post("/api/auth/login", json={"username": "bench", "password": PASSWORD}).status_code == 200
            while not stop.is_set():
                name, path = ENDPOINTS[rng.randrange(len(ENDPOINTS))]
                t = time.perf_counter()
                try:
                    ok = c.get(path.format(asset=ids[rng.randrange(len(ids))])).status_code == 200
                except httpx.HTTPError:
                    ok = False
                with lock:
                    latency[name].append(time.perf_counter() - t)
                    if not ok:
                        errors[name] += 1

    threads = [start_in_thread(w) for w in pool]
    clients = [threading.Thread(target=session, args=(i,), daemon=True) for i in range(sessions)]
    begin = time.monotonic()
    for c in clients:
        c.start()
    time.sleep(seconds)
    stop.set()
    for c in clients:
        c.join(60)
    # 给最后一个周期留出完成时间，再统计完成比例
    drain_deadline = time.monotonic() + interval
    while time.monotonic() < drain_deadline:
        with factory() as db:
            # 计划是先计数再在一个事务里入队 1000 个任务：要等全部入队任务都已提交且没有排队 / 执行中的才算结束
            committed = db.scalar(select(func.count()).select_from(Task).where(Task.kind == KIND))
            pending = db.scalar(select(func.count()).select_from(Task).where(Task.kind == KIND, Task.status.in_(("queued", "running"))))
            if committed >= len(cycles) * n_assets and not pending:
                break
        time.sleep(1)
    wall = time.monotonic() - begin
    for w in pool:
        w.stop()
    for t in threads:
        t.join(120)
    api.terminate()
    try:
        api.wait(30)
    except subprocess.TimeoutExpired:
        api.kill()

    with factory() as db:
        status = dict(db.execute(select(Task.status, func.count()).where(Task.kind == KIND).group_by(Task.status)).all())
        duplicates = db.scalar(select(func.count()).select_from(
            select(Task.idempotency_key).where(Task.kind == KIND).group_by(Task.idempotency_key).having(func.count() > 1).subquery()))
        if engine.dialect.name == "postgresql":
            size = int(db.scalar(select(func.pg_database_size(func.current_database()))))
        else:
            size = sum(p.stat().st_size for p in tmp.glob("cap.db*"))
    engine.dispose()
    expected = len(cycles) * n_assets
    all_lat = [v for vs in latency.values() for v in vs]
    total_req = len(all_lat)
    return {
        "database": engine.dialect.name, "assets": n_assets, "metrics_per_asset": n_metrics, "sessions": sessions,
        "workers": workers, "interval_s": interval, "run_seconds": seconds, "wall_seconds": round(wall, 1),
        **seeded,
        "api": {
            "requests": total_req, "errors": sum(errors.values()),
            "error_rate": round(sum(errors.values()) / total_req, 4) if total_req else 0,
            "p50_ms": _pct(all_lat, 50), "p95_ms": _pct(all_lat, 95), "p99_ms": _pct(all_lat, 99),
            "by_endpoint": {name: {"n": len(v), "errors": errors[name], "p50_ms": _pct(v, 50), "p95_ms": _pct(v, 95), "p99_ms": _pct(v, 99)}
                            for name, v in latency.items()},
        },
        "collection": {
            "cycles": len(cycles), "tasks_expected": expected, "tasks_by_status": status,
            "completed_ratio": round(status.get("succeeded", 0) / expected, 4) if expected else 0,
            "duplicate_keys": duplicates,
            "queue_wait_p50_ms": _pct(waits, 50), "queue_wait_p95_ms": _pct(waits, 95), "queue_wait_max_ms": _pct(waits, 100),
            "task_p50_ms": _pct(durations, 50), "task_p95_ms": _pct(durations, 95),
            "mean_task_ms": round(statistics.fmean(durations) * 1000, 1) if durations else 0,
        },
        "database_bytes": size, "database_mb": round(size / 1024 / 1024, 1),
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="4.1 P12 容量测量（伪造设备数据，只用临时库或空库）")
    p.add_argument("--url", help="空的 PostgreSQL 库；不填时用临时 SQLite")
    p.add_argument("--assets", type=int, default=100)
    p.add_argument("--metrics", type=int, default=30)
    p.add_argument("--sessions", type=int, default=5)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--seconds", type=float, default=180)
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--history-hours", type=float, default=2)
    p.add_argument("--report", type=Path)
    a = p.parse_args(argv)
    result = run(a.url, n_assets=a.assets, n_metrics=a.metrics, sessions=a.sessions, workers=a.workers,
                 seconds=a.seconds, interval=a.interval, history_hours=a.history_hours)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if a.report:
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
