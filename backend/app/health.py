"""就绪与 Worker 存活检查（部署兼容方案 D1）。

- /api/health 只说明进程活着（编排工具据此判断要不要重启），不碰数据库；
- /api/ready 说明能正常服务：数据库连得上、结构版本与本程序一致、内嵌 Worker（若开启）线程还在；
- `python -m app.worker --check`（镜像里 Worker 容器的健康检查）：本机的 Worker 在 CHECK_SECONDS 内写过心跳。
  Worker 每轮循环都写心跳；SQLite 下等别的 Worker 让出锁时，也每半个锁周期写一次。
"""

from __future__ import annotations

import socket
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app import migrate, queue

CHECK_SECONDS = 90


def readiness(engine: Engine, worker_thread: Any = None, *, embedded: bool = False) -> tuple[bool, dict[str, Any]]:
    """返回（是否就绪，说明）。说明只含结构版本和失败原因，不含连接串等配置。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        current, head = migrate.current_revision(engine), migrate.head_revision()
    except Exception as exc:  # noqa: BLE001 - 任何数据库错误都算未就绪
        return False, {"status": "not_ready", "reason": f"数据库不可用：{type(exc).__name__}"}
    if current != head:
        return False, {"status": "not_ready", "reason": f"数据库结构版本为 {current or '未初始化'}，需要 {head}，请先运行迁移", "schema": current}
    if embedded and (worker_thread is None or not worker_thread.is_alive()):
        return False, {"status": "not_ready", "reason": "内嵌 Worker 没有在运行", "schema": current}
    return True, {"status": "ready", "schema": current}


def worker_alive(db: Session, host: str | None = None, *, seconds: int = CHECK_SECONDS) -> bool:
    """本机（容器里就是本容器）是否有 Worker 在 seconds 秒内写过心跳。"""
    from app.models import WorkerHeartbeat

    host = (host or socket.gethostname())[:200]
    since = queue.now_utc() - timedelta(seconds=seconds)
    return db.scalar(select(WorkerHeartbeat.owner).where(WorkerHeartbeat.host == host, WorkerHeartbeat.last_seen >= since).limit(1)) is not None
