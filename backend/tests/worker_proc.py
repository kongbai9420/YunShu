"""多 Worker 测试用的独立 Worker 进程（4.1 P9）。只注册假任务，不连接任何设备。

    python tests/worker_proc.py <数据库连接串> <角色> <运行秒数> <租约秒数>
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import queue  # noqa: E402
from app.db import make_engine, make_session_factory  # noqa: E402
from app.worker import Schedule, TaskContext, Worker  # noqa: E402


def sleep_task(ctx: TaskContext) -> dict:
    time.sleep(0.1)
    return {"done": True}


def block_once(ctx: TaskContext) -> dict:
    # 第一次执行时卡住（测试会结束这个进程），之后的重试立即完成
    if ctx.lease.attempt == 1:
        time.sleep(120)
    return {"attempt": ctx.lease.attempt}


def main() -> None:
    url, role, seconds, lease = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
    engine = make_engine(url)
    factory = make_session_factory(engine)
    # 计划：每秒入队一个标记任务。只有调度者运行计划，所以标记数约等于运行秒数，而不是乘以进程数
    mark = Schedule("test.mark", 1, lambda db: queue.enqueue(db, "test.mark", payload={"t": time.time()}))
    worker = Worker(factory, {"test.sleep": sleep_task, "test.block": block_once, "test.mark": lambda ctx: {}},
                    schedules=[mark], role=role, lease_seconds=lease, timeout_seconds=300, poll_interval=0.1, lock_ttl=2)
    threading.Timer(seconds, worker.stop).start()
    worker.run_forever()
    engine.dispose()


if __name__ == "__main__":
    main()
