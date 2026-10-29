"""Worker：从数据库队列认领任务并执行。

    python -m app.worker          # 独立进程（Docker 里与 API 同一镜像、不同入口）
    python -m app.worker --check  # 健康检查：本机 Worker 90 秒内有心跳则返回 0（镜像的 HEALTHCHECK 用）

桌面版或单进程部署设 YUNSHU_EMBEDDED_WORKER=1，由 API 在 lifespan 里以后台线程拉起，
import 本模块不会启动任何东西。

- SQLite 只允许一个 Worker：先拿 scheduler_locks 里的 "worker" 锁，拿不到就等待，不并行执行。
- PostgreSQL 可运行多个 Worker（4.1 P9）：所有 Worker 并行认领任务；
  只有 "scheduler" 锁的持有者运行定时计划、回收过期租约。YUNSHU_WORKER_ROLE=executor 的 Worker 不参加调度竞选。
- 只有锁的持有者回收过期租约、运行定时计划（schedules）。
- 每轮写 worker_heartbeats，供 /api/workers 显示在线 Worker、调度者与当前任务。
- 每个任务在单独线程里执行；执行期间定期续租；超过 timeout 视为超时：
  read 任务按退避重试，control 任务标为 indeterminate。线程无法强杀，处理函数自己的
  网络调用必须设超时（采集适配器都带超时参数）。
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app import queue
from app.queue import Lease

log = logging.getLogger(__name__)
LOCK_NAME = "worker"  # SQLite：整个 Worker 单实例
SCHEDULER_LOCK = "scheduler"  # PostgreSQL：只有调度职责单实例
ROLES = ("all", "executor")
# 任务种类 → 最长执行秒数（默认 timeout_seconds）
LONG_TASKS = {"control.guest_power": 2000.0, "control.power_linked": 2100.0, "vuln.sync": 1800.0, "vuln.match": 900.0, "vuln.notify": 600.0, "ai.inspection_summary": 600.0}


class TaskError(Exception):
    """处理函数报告的失败。retryable=False 表示重试也没用（如配置错误、凭据缺失）。"""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class TaskContext:
    lease: Lease
    session_factory: sessionmaker[Session]
    _beat: Callable[[], bool]

    def heartbeat(self) -> bool:
        """长任务可主动续租；返回 False 说明租约已被回收，应尽快放弃。"""
        return self._beat()


Handler = Callable[[TaskContext], dict[str, Any] | None]


@dataclass
class Schedule:
    """由持有锁的 Worker 每隔 interval 秒调用一次，通常用来按计划入队采集任务。"""

    name: str
    interval: float
    fn: Callable[[Session], None]
    last_run: float = float("-inf")


@dataclass
class Worker:
    session_factory: sessionmaker[Session]
    handlers: dict[str, Handler]
    owner: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}")
    lease_seconds: float = 120
    timeout_seconds: float = 90
    poll_interval: float = 2.0
    lock_ttl: float = 30
    schedules: list[Schedule] = field(default_factory=list)
    clock: Callable[[], float] = field(default=lambda: __import__("time").monotonic())
    role: str = "all"
    # 个别任务需要更长时间（4.2 P17：虚拟机正常关机、宿主等其上虚拟机关完）；期间照常续租
    kind_timeouts: dict[str, float] = field(default_factory=lambda: dict(LONG_TASKS))

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"未知的 Worker 角色：{self.role}")
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yunshu-task")
        self._stop = threading.Event()
        self.has_lock = False
        self.started_at = queue.now_utc()
        bind = self.session_factory.kw.get("bind")
        # SQLite 只有一个写者：整个 Worker 单实例；PostgreSQL 只让调度职责单实例
        self.multi = bind is not None and bind.dialect.name == "postgresql"
        self.lock_name = SCHEDULER_LOCK if self.multi else LOCK_NAME

    # -- 单实例 --

    def ensure_lock(self) -> bool:
        if self.multi and self.role == "executor":
            self.has_lock = False
            return False
        with self.session_factory() as db:
            self.has_lock = queue.acquire_lock(db, self.lock_name, self.owner, ttl_seconds=self.lock_ttl)
        return self.has_lock

    def release(self) -> None:
        try:
            with self.session_factory() as db:
                queue.release_lock(db, self.lock_name, self.owner)
                queue.drop_heartbeat(db, self.owner)
        except Exception:  # noqa: BLE001 - 退出时释放失败：锁会按 TTL 过期，由其他 Worker 接手
            log.exception("释放调度锁失败，等待锁过期")
        self.has_lock = False

    def heartbeat(self, lease: Lease | None = None) -> None:
        """在线状态；失败只记日志（数据库短暂不可用时不影响主循环）。"""
        try:
            with self.session_factory() as db:
                queue.beat_worker(db, self.owner, role=self.role, scheduler=self.has_lock, started_at=self.started_at, lease=lease)
                db.commit()
        except Exception:  # noqa: BLE001
            log.exception("写 Worker 心跳失败")

    # -- 一轮 --

    def run_schedules(self) -> None:
        t = self.clock()
        for s in self.schedules:
            if t - s.last_run >= s.interval:
                s.last_run = t
                try:
                    with self.session_factory() as db:
                        s.fn(db)
                        db.commit()
                except Exception:  # noqa: BLE001 - 计划失败只记日志，不影响任务执行
                    log.exception("定时计划 %s 执行失败", s.name)

    def run_once(self, *, now: datetime | None = None, reap: bool = True) -> str | None:
        """回收过期租约（只有调度者），认领并执行一个任务。返回任务最终状态；没有任务时返回 None。"""
        with self.session_factory() as db:
            if reap:
                reaped = queue.reap_expired(db, now=now)
                db.commit()
                if reaped:
                    log.warning("回收过期租约：%s", reaped)
            lease = queue.claim(db, self.owner, kinds=tuple(self.handlers), lease_seconds=self.lease_seconds, now=now)
        if lease is None:
            return None
        self.heartbeat(lease)
        try:
            return self._execute(lease)
        finally:
            self.heartbeat()

    def _beat(self, lease: Lease) -> bool:
        with self.session_factory() as db:
            ok = queue.heartbeat(db, lease, lease_seconds=self.lease_seconds)
            db.commit()
        return ok

    def _execute(self, lease: Lease) -> str | None:
        handler = self.handlers[lease.kind]
        ctx = TaskContext(lease=lease, session_factory=self.session_factory, _beat=lambda: self._beat(lease))
        future = self._pool.submit(handler, ctx)
        beat_every = max(1.0, self.lease_seconds / 3)
        limit = self.kind_timeouts.get(lease.kind, self.timeout_seconds)
        waited = 0.0
        status: str | None
        try:
            while True:
                step = min(beat_every, limit - waited)
                try:
                    result = future.result(timeout=max(0.01, step))
                    break
                except FutureTimeout:
                    waited += step
                    if waited >= limit:
                        raise
                    if not self._beat(lease):
                        log.warning("任务 %s 的租约已被回收，放弃结果", lease.task_id)
                        return None
        except FutureTimeout:
            with self.session_factory() as db:
                status = queue.fail(db, lease, f"执行超过 {limit:.0f} 秒", timed_out=True)
                db.commit()
            # 超时的线程还在跑：换一个新线程池，不让它挡住后面的任务
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yunshu-task")
            return status
        except TaskError as exc:
            with self.session_factory() as db:
                status = queue.fail(db, lease, str(exc), retryable=exc.retryable)
                db.commit()
            return status
        except Exception as exc:  # noqa: BLE001
            log.exception("任务 %s（%s）执行出错", lease.task_id, lease.kind)
            with self.session_factory() as db:
                status = queue.fail(db, lease, f"{type(exc).__name__}: {exc}")
                db.commit()
            return status
        with self.session_factory() as db:
            ok = queue.complete(db, lease, result or {})
            db.commit()
        return "succeeded" if ok else None

    # -- 主循环 --

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        log.info("Worker %s 启动（%s，%s）", self.owner, "多 Worker" if self.multi else "单实例", self.role)
        try:
            while not self._stop.is_set():
                try:
                    scheduler = self.ensure_lock()
                    self.heartbeat()
                except Exception:  # noqa: BLE001 - 数据库忙（SQLite 写锁超时）或短暂断开：退避后重试，Worker 不能因此退出（4.1 P12 容量测量发现）
                    log.exception("取得调度锁或写心跳失败，稍后重试")
                    self._stop.wait(max(self.poll_interval, 1))
                    continue
                if not scheduler and not self.multi:
                    # 另一个 Worker 在运行（SQLite 只允许一个），等它退出或锁过期
                    self._stop.wait(self.lock_ttl / 2)
                    continue
                if scheduler:
                    try:
                        self.run_schedules()
                    except Exception:  # noqa: BLE001 - 同上：本轮计划下次再跑
                        log.exception("运行定时计划失败")
                try:
                    status = self.run_once(reap=scheduler)
                except Exception:  # noqa: BLE001 - 数据库短暂不可用等：记录后等下一轮，不退出
                    log.exception("认领任务失败")
                    status = None
                if status is None:
                    self._stop.wait(self.poll_interval)
        finally:
            self.release()
            self._pool.shutdown(wait=False, cancel_futures=True)
            log.info("Worker %s 已停止", self.owner)


def start_in_thread(worker: Worker) -> threading.Thread:
    thread = threading.Thread(target=worker.run_forever, name="yunshu-worker", daemon=True)
    thread.start()
    return thread


def check() -> int:
    """`python -m app.worker --check`：镜像里 Worker 容器的健康检查。不迁移、不取锁，只读心跳。"""
    from app.db import make_engine, make_session_factory
    from app.health import CHECK_SECONDS, worker_alive
    from app.settings import Settings

    settings = Settings.from_env()
    if settings.setup_pending:
        print("首次启动向导尚未完成，Worker 在等待")
        return 1
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as db:
            alive = worker_alive(db)
    except Exception as exc:  # noqa: BLE001
        print(f"读取 Worker 心跳失败：{type(exc).__name__}")
        return 1
    finally:
        engine.dispose()
    print("Worker 在运行" if alive else f"本机的 Worker 超过 {CHECK_SECONDS} 秒没有心跳")
    return 0 if alive else 1


def main() -> int:
    from app.db import make_engine, make_session_factory
    from app.migrate import prepare
    from app.settings import Settings
    from app.tasks import build_handlers, build_schedules

    if "--check" in sys.argv[1:]:
        return check()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    while settings.setup_pending:
        # 全新安装：等 API 的首次启动向导保存数据库选择（4.1 P12），保存后自动继续，不需要重启
        log.info("等待首次启动向导完成数据库选择…")
        time.sleep(5)
        settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    prepare(engine, auto=settings.auto_migrate, backup_dir=settings.backup_dir)
    factory = make_session_factory(engine)
    role = os.environ.get("YUNSHU_WORKER_ROLE", "all").strip() or "all"
    worker = Worker(factory, build_handlers(settings), schedules=build_schedules(settings), role=role)
    if role != "executor":
        from app.crypto import CredentialCipher, load_or_create_keyring
        from app.snmp_trap import start_if_enabled

        # 4.4 P20c：SNMP Trap 接收（默认关闭）
        start_if_enabled(factory, lambda: CredentialCipher(load_or_create_keyring(settings.master_key_file, data_dir=settings.data_dir)))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop())
    try:
        worker.run_forever()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
