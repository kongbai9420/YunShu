"""只读会话不能把连接池里的连接留在只读状态（本机浏览器全量测试间歇“attempt to write a readonly database”的根因）。

旧实现先 rollback（连接已还回连接池）再执行 PRAGMA query_only = OFF，后者可能拿到另一条连接，
设了只读的那条留在池里，之后拿到它的请求写库失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.ai.readonly import readonly_session
from app.db import make_engine, make_session_factory


def test_readonly_session_leaves_every_pooled_connection_writable(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{(tmp_path / 'fake.db').as_posix()}")
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE fake (n INTEGER)")
    factory = make_session_factory(engine)
    # 连接池里先有多条空闲连接（取出顺序先进先出）
    held = [engine.connect() for _ in range(3)]
    for c in held:
        c.close()
    for _ in range(3):
        with readonly_session(factory) as db:
            assert db.execute(text("PRAGMA query_only")).scalar() == 1
            with pytest.raises(OperationalError):
                db.execute(text("INSERT INTO fake VALUES (1)"))
    held = [engine.connect() for _ in range(4)]
    try:
        assert [c.exec_driver_sql("PRAGMA query_only").scalar() for c in held] == [0, 0, 0, 0]
    finally:
        for c in held:
            c.close()
    with factory() as db:
        db.execute(text("INSERT INTO fake VALUES (2)"))
        db.commit()
    engine.dispose()
