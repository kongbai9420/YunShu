from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import backup, migrate
from app.backup import BackupError, create_backup, list_backups, restore_backup, verify_backup
from app.crypto import CredentialCipher, KeyRing
from app.db import make_engine, make_session_factory
from app.models import Asset, Credential
from tests.conftest import sqlite_only

# 文件复制式备份只支持 SQLite；PostgreSQL 的 pg_dump 备份由 P8-4 另行测试
pytestmark = sqlite_only
from app.services.credentials import create_credential, reveal_secret
from tests.api_helpers import add_user, login

FAKE_SECRET = "fake-backup-secret-42"


def db_file(engine: Engine) -> Path:
    return Path(engine.url.database)


def test_backup_contains_data_and_metadata(engine: Engine, session: Session, tmp_path: Path) -> None:
    cipher = CredentialCipher(KeyRing.generate())
    session.add(Asset(name="甲", form="physical", role="server"))
    create_credential(session, cipher, alias="c", kind="password", secret=FAKE_SECRET)
    session.commit()

    info = create_backup(db_file(engine), tmp_path / "b", "manual")
    assert info.asset_count == 1
    assert info.key_ids == [cipher.current_key_id]
    assert info.alembic_revision == migrate.head_revision()
    path = tmp_path / "b" / info.name
    assert verify_backup(path) is not None
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["sha256"] == info.sha256
    # 备份文件里没有明文，也没有密钥
    assert FAKE_SECRET.encode() not in path.read_bytes()
    assert not list((tmp_path / "b").glob("*.key"))


def test_restore_round_trip_keeps_credentials(engine: Engine, tmp_path: Path) -> None:
    ring = KeyRing.generate()
    cipher = CredentialCipher(ring)
    factory = make_session_factory(engine)
    with factory() as db:
        db.add(Asset(name="原始", form="physical", role="server", asset_no="R-1"))
        create_credential(db, cipher, alias="c", kind="password", secret=FAKE_SECRET)
        db.commit()
    bdir = tmp_path / "b"
    info = create_backup(db_file(engine), bdir, "manual")

    with factory() as db:
        db.query(Asset).delete()
        db.add(Asset(name="之后新增", form="vm", role="server"))
        db.commit()
    engine.dispose()

    result = restore_backup(bdir / info.name, db_file(engine), bdir, set(ring.keys))
    assert result.missing_key_ids == []
    assert result.safety_backup and (bdir / result.safety_backup).exists()

    eng2 = make_engine(str(engine.url))
    try:
        migrate.upgrade(eng2)
        with make_session_factory(eng2)() as db:
            assert [a.name for a in db.query(Asset)] == ["原始"]
            assert reveal_secret(db.query(Credential).one(), cipher) == FAKE_SECRET
    finally:
        eng2.dispose()


def test_restore_reports_missing_key(engine: Engine, session: Session, tmp_path: Path) -> None:
    cipher = CredentialCipher(KeyRing.generate())
    create_credential(session, cipher, alias="c", kind="password", secret=FAKE_SECRET)
    session.commit()
    info = create_backup(db_file(engine), tmp_path / "b", "manual")
    session.close()
    engine.dispose()
    result = restore_backup(tmp_path / "b" / info.name, db_file(engine), tmp_path / "b", available_key_ids={"someotherkey"})
    assert result.missing_key_ids == [cipher.current_key_id]


def test_tampered_or_corrupt_backup_rejected(engine: Engine, tmp_path: Path) -> None:
    info = create_backup(db_file(engine), tmp_path / "b", "manual")
    path = tmp_path / "b" / info.name
    data = bytearray(path.read_bytes())
    data[-10] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(BackupError):
        verify_backup(path)
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all" * 100)
    with pytest.raises(BackupError):
        verify_backup(junk)


def test_retention(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(backup.KEEP, "scheduled", 3)
    for _ in range(5):
        create_backup(db_file(engine), tmp_path / "b", "scheduled")
    create_backup(db_file(engine), tmp_path / "b", "manual")
    kinds = [b.reason for b in list_backups(tmp_path / "b")]
    assert kinds.count("scheduled") == 3 and kinds.count("manual") == 1
    assert len(list((tmp_path / "b").glob("*.db"))) == 4


def test_pre_migration_backup_only_when_behind(tmp_path: Path) -> None:
    eng = make_engine(f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    bdir = tmp_path / "b"
    try:
        migrate.upgrade(eng, backup_dir=bdir)  # 新库：不备份
        assert list_backups(bdir) == []
        migrate.upgrade(eng, backup_dir=bdir)  # 已是最新：不备份
        assert list_backups(bdir) == []
        migrate.downgrade(eng, "0001")
        migrate.upgrade(eng, backup_dir=bdir)  # 版本落后：先备份
        got = list_backups(bdir)
        assert [b.reason for b in got] == ["pre-migrate"]
        assert got[0].alembic_revision == "0001"
        with eng.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == migrate.head_revision()
    finally:
        eng.dispose()


def test_in_memory_database_not_backed_up() -> None:
    with pytest.raises(BackupError):
        backup.sqlite_path("sqlite://")


def test_backup_api_admin_only(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "alice", "admin")
    add_user(factory, "olivia", "operator")
    admin = login(app_factory(), "alice")
    op = login(app_factory(), "olivia")
    assert op.get("/api/backups").status_code == 403
    assert op.post("/api/backups").status_code == 403

    made = admin.post("/api/backups")
    assert made.status_code == 201, made.text
    listing = admin.get("/api/backups").json()
    assert [b["name"] for b in listing["items"]] == [made.json()["name"]]
    assert "不含主密钥" in listing["key_hint"]
    dl = admin.get(f"/api/backups/{made.json()['name']}")
    assert dl.status_code == 200 and dl.content[:16] == b"SQLite format 3\x00"
    assert admin.get("/api/backups/..%2Fyunshu.db").status_code == 404
    assert op.get(f"/api/backups/{made.json()['name']}").status_code == 403


def test_cli_restore_requires_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("YUNSHU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YUNSHU_MASTER_KEY_FILE", str(tmp_path / "keys" / "master.key"))
    monkeypatch.delenv("YUNSHU_DATABASE_URL", raising=False)
    eng = make_engine(f"sqlite:///{(tmp_path / 'data' / 'yunshu.db').as_posix()}")
    migrate.upgrade(eng)
    eng.dispose()
    assert backup.main(["create"]) == 0
    name = list_backups(tmp_path / "data" / "backups")[0].name
    assert backup.main(["verify", name]) == 0
    assert backup.main(["restore", name]) == 2
    assert backup.main(["restore", name, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "不含主密钥" in out and "已从" in out


def test_client_fixture_starts_and_stops_scheduler(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200
