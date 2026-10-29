"""指标存储压测（落地方案 P2 验收：按 100 台设备模拟写入一周，记录库文件大小和清理耗时）。

    python -m app.metrics_bench --devices 100 --metrics 30 --interval 60 --days 7 [--db 路径]

只写临时库（默认在系统临时目录），按模拟时钟推进：每次采集写一行原始数据，
每模拟 1 分钟跑一次汇总，每模拟 1 小时跑一次清理，与 Worker 的定时计划一致。
"""

from __future__ import annotations

import argparse
import random
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app import metrics, migrate
from app.db import make_engine, make_session_factory
from app.metrics import Point, Retention
from app.models import Asset


def run(devices: int, n_metrics: int, interval: int, days: int, db_path: Path) -> dict:
    engine = make_engine(f"sqlite:///{db_path.as_posix()}")
    migrate.upgrade(engine)
    factory = make_session_factory(engine)
    rng = random.Random(42)
    with factory() as db:
        ids = []
        for i in range(devices):
            a = Asset(id=uuid.uuid4(), name=f"压测-{i:03d}", form="physical", role="server")
            db.add(a)
            ids.append(a.id)
        db.commit()
    keys = [f"m{k:02d}" for k in range(n_metrics)]
    start = datetime(2026, 1, 1, tzinfo=UTC)
    steps = days * 86400 // interval
    write_s = rollup_s = retention_s = 0.0
    retention_runs: list[float] = []
    with factory() as db:
        for step in range(steps):
            now = start + timedelta(seconds=step * interval)
            t0 = time.perf_counter()
            for aid in ids:
                pts = [Point(k, round(rng.uniform(20, 80), 1), "x") for k in keys]
                metrics.write_sample(db, aid, "bench", pts, observed_at=now, interval_seconds=interval)
            db.commit()
            write_s += time.perf_counter() - t0
            if (step * interval) % 60 == 0:
                t0 = time.perf_counter()
                metrics.rollup(db, now=now)
                db.commit()
                rollup_s += time.perf_counter() - t0
            if (step * interval) % 3600 == 0:
                t0 = time.perf_counter()
                metrics.enforce_retention(db, Retention(), now=now)
                db.commit()
                dt = time.perf_counter() - t0
                retention_s += dt
                retention_runs.append(dt)
        tables = metrics.raw_tables(db)
    engine.dispose()
    size = sum(p.stat().st_size for p in db_path.parent.glob(db_path.name + "*"))
    return {
        "devices": devices, "metrics": n_metrics, "interval_s": interval, "days": days,
        "samples_written": steps * devices, "values_written": steps * devices * n_metrics,
        "db_bytes": size, "db_mb": round(size / 1024 / 1024, 1), "raw_tables_left": tables,
        "write_seconds_total": round(write_s, 1), "write_ms_per_round": round(write_s / steps * 1000, 1),
        "rollup_seconds_total": round(rollup_s, 1), "retention_seconds_total": round(retention_s, 2),
        "retention_max_ms": round(max(retention_runs) * 1000, 1) if retention_runs else 0,
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="指标存储压测（只写临时库）")
    p.add_argument("--devices", type=int, default=100)
    p.add_argument("--metrics", type=int, default=30)
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--db", type=Path)
    a = p.parse_args(argv)
    db = a.db or Path(tempfile.mkdtemp()) / f"bench-{uuid.uuid4().hex[:6]}.db"
    import json

    print(json.dumps(run(a.devices, a.metrics, a.interval, a.days, db), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
