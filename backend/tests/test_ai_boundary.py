"""4.3 P18a：AI 的边界由代码强制。

1. app/ai 包不能引用凭据、加密、控制、采集、任务队列等模块，也不能自己写数据库；
2. AI 用的查询在只读事务里执行，写语句会失败。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.ai.readonly import readonly_session
from app.db import make_session_factory
from app.models import Site

AI_DIR = Path(__file__).resolve().parents[1] / "app" / "ai"
FORBIDDEN = ("app.crypto", "app.services.credentials", "app.control", "app.collect", "app.queue", "app.worker", "app.tasks",
             "app.api", "app.audit", "app.ai_runtime", "app.notifications", "app.notification_queue", "app.backup", "app.migrate")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def test_ai_package_imports_stay_inside_boundary() -> None:
    files = sorted(AI_DIR.glob("*.py"))
    assert files
    for f in files:
        for mod in _imports(f):
            assert not any(mod == bad or mod.startswith(bad + ".") for bad in FORBIDDEN), f"{f.name} 引用了 {mod}"


def test_ai_package_has_no_write_calls() -> None:
    """不导入 SQLAlchemy 的写语句构造，不调用提交类方法（session.add 与 set.add 无法区分，由只读事务兜底）。"""
    for f in AI_DIR.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy"):
                names = {a.name for a in node.names}
                assert not names & {"insert", "update", "delete"}, f"{f.name} 导入了写语句 {names}"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                if attr in ("flush", "merge", "bulk_save_objects", "bulk_insert_mappings", "bulk_update_mappings"):
                    raise AssertionError(f"{f.name} 调用了 {attr}")
                if attr == "commit" and f.name != "readonly.py":  # readonly.py 只在退出时恢复 SQLite 连接的 query_only
                    raise AssertionError(f"{f.name} 调用了 commit")


def test_readonly_session_blocks_writes(engine) -> None:  # type: ignore[no-untyped-def]
    factory = make_session_factory(engine)
    with readonly_session(factory) as db:
        assert db.scalars(select(Site)).all() == []
        db.add(Site(name="fake-site"))
        with pytest.raises(DBAPIError):
            db.flush()
    # 退出后连接恢复可写，不影响连接池里的后续使用
    with factory() as db:
        db.add(Site(name="fake-site"))
        db.commit()
        assert db.scalar(select(Site.name)) == "fake-site"
