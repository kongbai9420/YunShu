"""PostgreSQL 备份与恢复（4.1 P8，pg_dump）。工具查找与类型检查本机即可运行；完整往返只在 PostgreSQL 上运行。"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from app import backup, backup_pg, migrate
from app.crypto import CredentialCipher, KeyRing
from app.db import make_session_factory
from app.models import Asset, Credential
from app.services.credentials import create_credential, reveal_secret
from tests.conftest import PG_ADMIN_URL

pg_only = pytest.mark.skipif(not PG_ADMIN_URL, reason="只在 PostgreSQL 上运行")


def test_tool_must_not_be_older_than_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pg_dump").write_text("", encoding="utf-8")
    monkeypatch.setenv("YUNSHU_PG_BIN_DIR", str(tmp_path))
    monkeypatch.setattr(backup_pg.shutil, "which", lambda _name: None)
    monkeypatch.setattr(backup_pg, "_tool_major", lambda _p: 16)
    if Path("/usr/lib/postgresql").is_dir():
        pytest.skip("本机装有系统 PostgreSQL 客户端，候选不可控")
    with pytest.raises(backup_pg.PgToolError, match="PostgreSQL 17，需要 17 或更新"):
        backup_pg.find_tool("pg_dump", 17)
    assert backup_pg.find_tool("pg_dump", 16) == str(tmp_path / "pg_dump")
    monkeypatch.setattr(backup_pg, "_tool_major", lambda _p: 17)
    assert backup_pg.find_tool("pg_dump", 16) == str(tmp_path / "pg_dump")  # 新客户端可备份旧服务器


def test_backup_kind_must_match_database(tmp_path: Path) -> None:
    dump = tmp_path / "yunshu-x-manual.dump"
    dump.write_bytes(b"not a real archive")
    with pytest.raises(backup.BackupError, match="备份类型与当前数据库不一致"):
        backup.restore_database(dump, f"sqlite:///{(tmp_path / 'a.db').as_posix()}", tmp_path, set())
    assert backup.database_kind("postgresql+psycopg://u@h/db") == "postgresql"
    assert backup.database_kind(f"sqlite:///{(tmp_path / 'a.db').as_posix()}") == "sqlite"


def test_backup_api_reports_database_kind(client, factory) -> None:  # type: ignore[no-untyped-def]
    from tests.api_helpers import add_user, login

    add_user(factory, "fake-backup-admin", "admin")
    c = login(client, "fake-backup-admin")
    assert c.get("/api/backups").json()["database"] == ("postgresql" if PG_ADMIN_URL else "sqlite")


@pg_only
def test_pg_backup_restore_round_trip_keeps_assets_and_credentials(engine: Engine, tmp_path: Path) -> None:
    cipher = CredentialCipher(KeyRing.generate())
    factory = make_session_factory(engine)
    with factory() as db:
        db.add(Asset(name="假备份资产", form="physical", role="server"))
        create_credential(db, cipher, alias="假备份凭据", kind="password", secret="fake-backup-secret")
        db.commit()
    url = engine.url.render_as_string(hide_password=False)
    info = backup.backup_database(url, tmp_path, "manual")
    assert info.name.endswith(".dump") and info.asset_count == 1 and info.alembic_revision == migrate.head_revision()
    assert backup.verify_backup(tmp_path / info.name) is not None
    assert [b.name for b in backup.list_backups(tmp_path)] == [info.name]
    with factory() as db:
        db.execute(Asset.__table__.delete())
        db.commit()
    engine.dispose()  # 恢复要求没有其他连接
    result = backup.restore_database(tmp_path / info.name, url, tmp_path, {cipher.current_key_id})
    assert result.missing_key_ids == [] and result.safety_backup and result.safety_backup.endswith("-pre-restore.dump")
    with factory() as db:
        assert db.scalar(select(Asset.name)) == "假备份资产"
        assert reveal_secret(db.scalar(select(Credential)), cipher) == "fake-backup-secret"
    # 缺少主密钥时提示，而不是假装凭据可用
    engine.dispose()
    assert backup.restore_database(tmp_path / info.name, url, tmp_path, set()).missing_key_ids == [cipher.current_key_id]


@pg_only
def test_pg_tampered_backup_rejected_and_pre_migrate_backup(engine: Engine, tmp_path: Path) -> None:
    url = engine.url.render_as_string(hide_password=False)
    info = backup.backup_database(url, tmp_path, "manual")
    path = tmp_path / info.name
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(backup.BackupError):
        backup.verify_backup(path)
    with make_session_factory(engine)() as db:
        db.add(Asset(name="假迁移前资产", form="physical", role="server"))
        db.commit()
    migrate.downgrade(engine, "-1")
    pre = backup.backup_before_migration(engine, tmp_path, migrate.head_revision())
    assert pre is not None and pre.reason == "pre-migrate" and pre.name.endswith(".dump")
    migrate.upgrade(engine)
    assert backup.backup_before_migration(engine, tmp_path, migrate.head_revision()) is None


FAKE_PG_RESTORE = """
import sys
assert sys.argv[1:5] == ["--no-owner", "--no-privileges", "--file", "-"], sys.argv
sys.stdout.write("SET statement_timeout = 0;\\n")
sys.stdout.write("SET transaction_timeout = 0;\\n")
sys.stdout.write("CREATE TABLE fake_t (id int);\\n")
"""

FAKE_PSQL = """
import sys
data = sys.stdin.read()
open(sys.argv[-1], "w", encoding="utf-8").write(data)
if "transaction_timeout" in data:
    sys.stderr.write("ERROR:  unrecognized configuration parameter transaction_timeout")
    sys.exit(3)
"""

FAKE_PSQL_FAILS = """
import sys
sys.stdin.read()
sys.stderr.write("ERROR:  fake syntax error")
sys.exit(3)
"""


def _fake_tool(tmp_path: Path, name: str, body: str) -> str:
    """用当前 Python 写一个假的命令行工具（Windows 用 .cmd 包一层），供恢复流程调用。"""
    import os
    import sys
    script = tmp_path / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        wrapper = tmp_path / f"{name}.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        wrapper = tmp_path / name
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        os.chmod(wrapper, 0o755)
    return str(wrapper)


def test_restore_to_older_server_drops_newer_only_settings(tmp_path: Path) -> None:
    """17 版客户端恢复到 16 版服务器：去掉 16 不认识的 SET transaction_timeout，其余 SQL 原样交给 psql。"""
    import os
    out = tmp_path / "received.sql"
    archive = tmp_path / "fake.dump"
    archive.write_bytes(b"fake")
    backup_pg._restore_via_sql(_fake_tool(tmp_path, "fake_pg_restore", FAKE_PG_RESTORE),
                               _fake_tool(tmp_path, "fake_psql", FAKE_PSQL), [str(out)], dict(os.environ), archive)
    sent = out.read_text(encoding="utf-8")
    assert "transaction_timeout" not in sent
    assert "SET statement_timeout = 0;" in sent and "CREATE TABLE fake_t (id int);" in sent


def test_restore_via_sql_reports_psql_failure(tmp_path: Path) -> None:
    import os
    archive = tmp_path / "fake.dump"
    archive.write_bytes(b"fake")
    with pytest.raises(backup_pg.PgToolError, match="fake syntax error"):
        backup_pg._restore_via_sql(_fake_tool(tmp_path, "fake_pg_restore", FAKE_PG_RESTORE),
                                   _fake_tool(tmp_path, "fake_psql", FAKE_PSQL_FAILS), [], dict(os.environ), archive)
