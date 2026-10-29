"""两种数据库写法不同的地方集中在这里（4.1 P8）。

轻量版用 SQLite，标准版用 PostgreSQL；业务代码只调用这里的函数，不直接判断方言。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import cast, exists, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

# “表不存在”：SQLite 报 OperationalError，PostgreSQL 报 ProgrammingError（UndefinedTable）
MISSING_TABLE = (OperationalError, ProgrammingError)


def name(db: Session) -> str:
    return db.get_bind().dialect.name


def is_postgres(db: Session) -> bool:
    return name(db) == "postgresql"


def insert(db: Session, table: Any) -> Any:
    """支持 on_conflict_do_update 的 INSERT（两种库语法一致，构造函数不同）。"""
    return (postgresql.insert if is_postgres(db) else sqlite.insert)(table)


def has_tag(db: Session, column: Any, tag: str) -> Any:
    """JSON 字符串数组里精确包含某个标签。"""
    if is_postgres(db):
        return cast(column, postgresql.JSONB).contains([tag])
    values = func.json_each(column).table_valued("value")
    return exists(select(1).select_from(values).where(values.c.value == tag))
