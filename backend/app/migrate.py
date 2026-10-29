"""在代码里执行 Alembic 迁移（应用启动、测试、命令行共用）。

    python -m app.migrate            # 升级到最新（镜像：yunshu-entrypoint migrate）
    python -m app.migrate downgrade base

单一入口（4.1 P8）：
- 升级在一个事务里执行；PostgreSQL 上先取 advisory 锁，多个进程同时升级时只有一个真正执行，其余等待后发现已是最新。
- 轻量版（SQLite）的 API / Worker 启动时自动升级，保持解压即用；标准版（PostgreSQL）启动时只检查版本，
  不一致就拒绝启动并提示先运行迁移命令（可用 YUNSHU_AUTO_MIGRATE=1 改为自动升级）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, text

BACKEND_DIR = Path(__file__).resolve().parent.parent
# PostgreSQL advisory 锁编号（“YUNS”），只用于串行化结构升级
MIGRATION_LOCK = 0x59554E53


class SchemaMismatch(RuntimeError):
    """数据库结构版本与程序不一致：标准版启动时不自动升级。"""


def alembic_config(engine: Engine | None = None) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.attributes["configure_logger"] = False
    if engine is not None:
        cfg.attributes["engine"] = engine
    return cfg


def _run(engine: Engine, action: str, revision: str) -> None:
    cfg = alembic_config()
    if engine.dialect.name == "sqlite":
        _run_sqlite(engine, cfg, action, revision)
        return
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # 事务结束自动释放；后到的进程在这里等待，拿到锁时版本已是最新，升级为空操作
            conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK})
        cfg.attributes["connection"] = conn
        getattr(command, action)(cfg, revision)


class ForeignKeyViolation(RuntimeError):
    """迁移后外键校验失败：事务已回滚，数据库保持迁移前的状态。"""


def _run_sqlite(engine: Engine, cfg: Config, action: str, revision: str) -> None:
    """SQLite 修改列 / 约束时 Alembic 会重建整张表（复制、删除旧表、改名）。外键打开时删除旧表会级联删除
    引用它的行（关系、端点、资产范围、会话……，4.2 P14 测试发现），所以按 Alembic 的建议迁移期间关闭外键，
    提交前用 foreign_key_check 校验，有违反就整体回滚。PRAGMA foreign_keys 在事务内无效，必须在事务外切换。"""
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()
        try:
            with conn.begin():
                cfg.attributes["connection"] = conn
                getattr(command, action)(cfg, revision)
                bad = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if bad:
                    raise ForeignKeyViolation(f"迁移后有 {len(bad)} 处外键引用不存在的记录（首个：{tuple(bad[0])}），已回滚")
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.commit()


def head_revision() -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def ensure_current(engine: Engine) -> None:
    current, head = current_revision(engine), head_revision()
    if current != head:
        raise SchemaMismatch(
            f"数据库结构版本为 {current or '未初始化'}，本程序需要 {head}。"
            "请先运行迁移（python -m app.migrate，镜像为 yunshu-entrypoint migrate），再启动 API / Worker。"
        )


def prepare(engine: Engine, *, auto: bool | None, backup_dir: Path | None) -> None:
    """API / Worker 启动时调用：轻量版或显式开启时自动升级，否则只检查。"""
    if auto if auto is not None else engine.dialect.name == "sqlite":
        upgrade(engine, backup_dir=backup_dir)
    else:
        ensure_current(engine)


def upgrade(engine: Engine, revision: str = "head", *, backup_dir: Path | None = None) -> None:
    """升级迁移。给了 backup_dir 时，已有数据且版本落后就先备份。"""
    if backup_dir is not None:
        from app.backup import backup_before_migration

        backup_before_migration(engine, backup_dir, head_revision())
    _run(engine, "upgrade", revision)


def downgrade(engine: Engine, revision: str) -> None:
    _run(engine, "downgrade", revision)


def main(argv: list[str]) -> None:
    from app.db import make_engine
    from app.settings import Settings

    action = argv[0] if argv else "upgrade"
    revision = argv[1] if len(argv) > 1 else "head"
    if action not in ("upgrade", "downgrade"):
        raise SystemExit("用法：python -m app.migrate [upgrade|downgrade] [revision]")
    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    try:
        if action == "upgrade":
            upgrade(engine, revision, backup_dir=settings.backup_dir)
        else:
            _run(engine, action, revision)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main(sys.argv[1:])
