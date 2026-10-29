"""4.1 P12 容量测量发现的两处并发问题：只读请求不应写会话；Worker 遇到数据库忙不能退出。"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.auth import SESSION_RENEW_AFTER, SESSION_TTL, issue_session, resolve_session
from app.models import User
from app.worker import Worker


def test_resolving_a_fresh_session_does_not_write(session: Session) -> None:
    user = User(username="fake-reader", role="viewer", password_hash="x")
    session.add(user)
    session.flush()
    issued = issue_session(session, user, client_ip="192.0.2.1")
    session.commit()
    row, _ = resolve_session(session, issued.token)
    assert row not in session.dirty  # 刚登录：不顺延，GET 请求保持只读事务

    row.expires_at = datetime.now(UTC) + SESSION_TTL - SESSION_RENEW_AFTER - timedelta(seconds=1)
    session.commit()
    row, _ = resolve_session(session, issued.token)
    assert row in session.dirty and row.expires_at > datetime.now(UTC) + SESSION_TTL - timedelta(seconds=5)


def test_worker_survives_database_busy_while_taking_the_lock(engine) -> None:  # type: ignore[no-untyped-def]
    from app.db import make_session_factory

    worker = Worker(make_session_factory(engine), {}, poll_interval=0.01)
    calls = {"n": 0}
    real = worker.ensure_lock

    def flaky() -> bool:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("database is locked (fake)")
        if calls["n"] >= 4:
            worker.stop()
        return real()

    worker.ensure_lock = flaky  # type: ignore[method-assign]
    t = threading.Thread(target=worker.run_forever)
    t.start()
    t.join(20)
    assert not t.is_alive() and calls["n"] >= 4
