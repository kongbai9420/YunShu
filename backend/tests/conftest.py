from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import migrate
from app.db import make_engine, make_session_factory

# 设置后在 PostgreSQL 上跑后端测试（4.1 P8）：值为可建库的管理连接，例如
# postgresql+psycopg://postgres@127.0.0.1:5432/postgres。未设置时用 SQLite（默认）。
PG_ADMIN_URL = os.environ.get("YUNSHU_TEST_DATABASE_URL", "")


@pytest.fixture(autouse=True)
def _beijing_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """没设 TZ 时平台跟随主机时区；测试的期望值按北京时间写，固定下来，不随运行测试的机器（CI 是 UTC）变化。"""
    monkeypatch.setenv("TZ", "Asia/Shanghai")


# 只对 SQLite 文件库有意义的测试（WAL / PRAGMA、文件复制式备份）在 PostgreSQL 上跳过
sqlite_only = pytest.mark.skipif(bool(PG_ADMIN_URL), reason="只适用于 SQLite 轻量版")


def _url(database: str) -> str:
    return make_url(PG_ADMIN_URL).set(database=database).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def pg_template() -> Iterator[tuple[Engine, str] | None]:
    """整个会话建一次升级到最新版本的模板库，每个测试从它克隆，避免每项都跑全部迁移。"""
    if not PG_ADMIN_URL:
        yield None
        return
    admin = create_engine(make_url(PG_ADMIN_URL), isolation_level="AUTOCOMMIT")
    name = f"yunshu_tpl_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    eng = make_engine(_url(name))
    migrate.upgrade(eng)
    eng.dispose()
    yield admin, name
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def db_url(tmp_path: Path, pg_template: tuple[Engine, str] | None) -> Iterator[str]:
    if pg_template is None:
        yield f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
        return
    admin, template = pg_template
    name = f"yunshu_t_{uuid.uuid4().hex[:12]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
    yield _url(name)
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    eng = make_engine(db_url)
    migrate.upgrade(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture
def app_factory(engine: Engine, tmp_path: Path):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from app.crypto import CredentialCipher, KeyRing
    from app.main import create_app
    from app.settings import Settings

    settings = Settings(
        data_dir=tmp_path / "data",
        # str(url) 会把密码替换成 ***，PostgreSQL 上会连不上
        database_url=engine.url.render_as_string(hide_password=False),
        master_key_file=tmp_path / "keys" / "master.key",
    )
    cipher = CredentialCipher(KeyRing.generate())
    clients: list[TestClient] = []

    def make() -> TestClient:
        client = TestClient(create_app(settings, engine=engine, cipher=cipher))
        client.__enter__()
        clients.append(client)
        return client

    make.cipher = cipher  # type: ignore[attr-defined]
    make.settings = settings  # type: ignore[attr-defined]
    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(app_factory):  # type: ignore[no-untyped-def]
    return app_factory()


@pytest.fixture
def factory(client):  # type: ignore[no-untyped-def]
    return client.app.state.session_factory
