"""只读会话：AI 用到的查询工具都在这里面执行，代码出错也写不进数据库。

- PostgreSQL：SET TRANSACTION READ ONLY，写语句直接报错；
- SQLite：PRAGMA query_only = ON，写语句报 “attempt to write a readonly database”。
整个会话固定使用同一条连接：退出时在这条连接上回滚并把 query_only 恢复，之后才还回连接池。
（旧实现先 rollback 已把连接还回池，再执行 query_only = OFF 时可能拿到另一条连接，
设了只读的那条留在池里，之后拿到它的普通请求写库失败。）
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session


@contextmanager
def readonly_session(session_factory: Callable[[], Session]) -> Iterator[Session]:
    probe = session_factory()
    engine = probe.get_bind()
    probe.close()
    dialect = engine.dialect.name
    with engine.connect() as conn:
        try:
            if dialect == "postgresql":
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            elif dialect == "sqlite":
                conn.exec_driver_sql("PRAGMA query_only = ON")
            db = Session(bind=conn, expire_on_commit=False)
            try:
                yield db
            finally:
                db.close()
        finally:
            conn.rollback()
            if dialect == "sqlite":
                conn.exec_driver_sql("PRAGMA query_only = OFF")
                conn.commit()
