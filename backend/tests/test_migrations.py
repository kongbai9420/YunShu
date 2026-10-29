from __future__ import annotations

import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import migrate
from app.db import make_engine
from app.models import Asset, Base, ManagementEndpoint
from tests.conftest import sqlite_only

P1_TABLES = {
    "assets",
    "asset_relations",
    "audit_events",
    "credentials",
    "login_sessions",
    "management_endpoints",
    "racks",
    "sites",
    "users",
}


def test_upgrade_creates_p1_tables(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert P1_TABLES <= tables
    assert "alembic_version" in tables


def test_migrations_match_models(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_downgrade_to_base_and_upgrade_again(engine: Engine) -> None:
    migrate.downgrade(engine, "base")
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set()

    migrate.upgrade(engine)
    assert P1_TABLES <= set(inspect(engine).get_table_names())


def test_upgrade_is_idempotent(engine: Engine) -> None:
    migrate.upgrade(engine)
    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert current == ScriptDirectory.from_config(migrate.alembic_config()).get_current_head()


@sqlite_only
def test_file_database_uses_wal_and_foreign_keys(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_in_memory_database_works() -> None:
    eng = make_engine("sqlite://")
    try:
        migrate.upgrade(eng)
        assert P1_TABLES <= set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def test_asset_primary_key_is_uuid(session: Session) -> None:
    asset = Asset(name="测试服务器", form="physical", role="server")
    session.add(asset)
    session.commit()
    assert isinstance(asset.id, uuid.UUID)
    assert asset.revision == 1
    assert asset.tags == []


def test_same_address_does_not_merge_assets(session: Session) -> None:
    """不按 IP 合并资产：两台资产可以有相同管理地址。"""
    for name in ("甲", "乙"):
        asset = Asset(name=name, form="physical", role="server")
        asset.endpoints.append(ManagementEndpoint(address="192.0.2.10", protocol="ipmi"))
        session.add(asset)
    session.commit()
    assert session.query(Asset).count() == 2


def test_asset_no_is_unique(session: Session) -> None:
    session.add(Asset(asset_no="A-001", name="甲", form="physical", role="server"))
    session.commit()
    session.add(Asset(asset_no="A-001", name="乙", form="physical", role="server"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_asset_cascades_to_endpoints(session: Session) -> None:
    asset = Asset(name="甲", form="physical", role="server")
    asset.endpoints.append(ManagementEndpoint(address="192.0.2.11", protocol="snmp"))
    session.add(asset)
    session.commit()

    session.delete(asset)
    session.commit()
    assert session.query(ManagementEndpoint).count() == 0


def test_prepare_auto_upgrades_sqlite_but_check_only_refuses_stale_schema(tmp_path) -> None:  # type: ignore[no-untyped-def]
    eng = make_engine(f"sqlite:///{(tmp_path / 'p.db').as_posix()}")
    try:
        with pytest.raises(migrate.SchemaMismatch, match="未初始化"):
            migrate.prepare(eng, auto=False, backup_dir=None)  # 标准版行为：只检查，不升级
        migrate.prepare(eng, auto=None, backup_dir=None)  # SQLite 默认自动升级
        assert migrate.current_revision(eng) == migrate.head_revision()
        migrate.downgrade(eng, "-1")
        with pytest.raises(migrate.SchemaMismatch, match="请先运行迁移"):
            migrate.prepare(eng, auto=False, backup_dir=None)
        migrate.prepare(eng, auto=True, backup_dir=None)
        migrate.ensure_current(eng)
    finally:
        eng.dispose()


def test_auto_migrate_setting_from_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.settings import Settings

    for value, expected in (("", None), ("1", True), ("0", False)):
        monkeypatch.setenv("YUNSHU_AUTO_MIGRATE", value)
        assert Settings.from_env().auto_migrate is expected


def test_concurrent_upgrades_on_postgres_run_once() -> None:
    """多个进程（这里用线程模拟）同时升级同一个空库：advisory 锁保证只有一个真正执行，其余为空操作。"""
    import threading

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from tests.conftest import PG_ADMIN_URL

    if not PG_ADMIN_URL:
        pytest.skip("只在 PostgreSQL 上运行")
    admin = create_engine(make_url(PG_ADMIN_URL), isolation_level="AUTOCOMMIT")
    name = f"yunshu_race_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    engines = [make_engine(make_url(PG_ADMIN_URL).set(database=name).render_as_string(hide_password=False)) for _ in range(3)]
    errors: list[BaseException] = []

    def run(e) -> None:  # type: ignore[no-untyped-def]
        try:
            migrate.upgrade(e)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        threads = [threading.Thread(target=run, args=(e,)) for e in engines]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        assert errors == []
        assert migrate.current_revision(engines[0]) == migrate.head_revision()
    finally:
        for e in engines:
            e.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_sqlite_table_rebuilds_keep_rows_that_reference_the_rebuilt_table(engine: Engine) -> None:
    """4.2 P14 / P16：0019 改 assets、0020 改 users，SQLite 上 Alembic 会重建整张表。
    迁移期间必须关闭外键，否则删除旧表会级联删除关系、端点、资产范围和登录会话。"""
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import JSON, Boolean, DateTime, Integer, MetaData, Table, func, insert, select

    migrate.downgrade(engine, "0018")
    meta = MetaData()
    tables = {n: Table(n, meta, autoload_with=engine) for n in ("assets", "asset_relations", "management_endpoints", "users", "user_scope_assets", "login_sessions")}
    sqlite = engine.dialect.name == "sqlite"
    now = datetime.now(UTC)

    def row(table: str, **values):  # type: ignore[no-untyped-def]
        # 只填必填列，其余用默认值；SQLite 反射出的 ID 列是字符串
        out = {}
        for col in tables[table].columns:
            if col.name in values:
                v = values[col.name]
                out[col.name] = v.hex if sqlite and isinstance(v, uuid.UUID) else v
            elif not col.nullable and col.server_default is None:
                t = col.type
                out[col.name] = (now if isinstance(t, DateTime) else [] if isinstance(t, JSON) else False if isinstance(t, Boolean)
                                 else 0 if isinstance(t, Integer) else "")
        return insert(tables[table]).values(**out)

    a, b, u = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(row("assets", id=a, name="fake-a", form="physical", role="server", revision=1))
        conn.execute(row("assets", id=b, name="fake-b", form="vm", role="server", revision=1))
        conn.execute(row("asset_relations", id=uuid.uuid4(), source_asset_id=a, target_asset_id=b, kind="hosts", origin="manual"))
        conn.execute(row("management_endpoints", id=uuid.uuid4(), asset_id=a, address="192.0.2.5", protocol="snmp", purpose="monitoring"))
        conn.execute(row("users", id=u, username="fake-u", role="viewer", password_hash="x", scope_all=False))
        conn.execute(row("user_scope_assets", user_id=u, asset_id=a))
        conn.execute(row("login_sessions", id=uuid.uuid4(), user_id=u, token_hash="fake-hash", csrf_token="fake", expires_at=now))

    def counts() -> dict[str, int]:
        with engine.connect() as conn:
            return {n: conn.scalar(select(func.count()).select_from(t)) for n, t in tables.items()}

    before = counts()
    assert before == {"assets": 2, "asset_relations": 1, "management_endpoints": 1, "users": 1, "user_scope_assets": 1, "login_sessions": 1}
    migrate.upgrade(engine)
    assert counts() == before
    migrate.downgrade(engine, "0018")
    assert counts() == before
    migrate.upgrade(engine)
    if sqlite:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1  # 迁移结束后外键重新打开
