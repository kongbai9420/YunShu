from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app.db import make_engine
from app.models import Base
from app.settings import Settings

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    # 原始指标按天分表（metric_raw_YYYYMMDD），由代码按需创建和整表删除，不归 Alembic 管
    return not (type_ == "table" and name and name.startswith("metric_raw_"))


def _configure(connection) -> None:  # type: ignore[no-untyped-def]
    # SQLite 改表需要批量模式（复制表）
    context.configure(
        connection=connection, target_metadata=target_metadata, render_as_batch=True, include_object=include_object
    )


def run_migrations_offline() -> None:
    context.configure(
        url=Settings.from_env().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = make_engine(Settings.from_env().database_url)
    with engine.connect() as conn:
        _configure(conn)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
