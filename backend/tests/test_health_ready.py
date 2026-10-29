"""就绪检查、Worker 心跳检查、主密钥文件权限（部署兼容方案 D1）；只用临时库和假数据。"""
from __future__ import annotations

import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import health, queue, worker
from app.crypto import KeyFileError, export_keyring, load_or_create_keyring
from app.models import WorkerHeartbeat


def test_ready_when_schema_current(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/api/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready" and body["schema"] and "version" in body
    assert client.get("/api/health").json()["status"] == "ok"


def test_not_ready_when_schema_behind(client, engine) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        conn.execute(text("UPDATE alembic_version SET version_num = '0035'"))
    try:
        r = client.get("/api/ready")
        assert r.status_code == 503
        assert "0035" in r.json()["reason"] and "迁移" in r.json()["reason"]
        # 进程本身仍然活着：存活检查不受影响，编排工具不会因此反复重启
        assert client.get("/api/health").status_code == 200
    finally:
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": head})


def test_not_ready_when_database_unreachable(tmp_path: Path) -> None:
    broken = create_engine(f"sqlite:///{(tmp_path / 'missing' / 'no.db').as_posix()}")
    ok, body = health.readiness(broken)
    assert not ok and body["reason"].startswith("数据库不可用")


def test_embedded_worker_thread_must_be_alive(engine) -> None:  # type: ignore[no-untyped-def]
    done = threading.Thread(target=lambda: None)
    done.start()
    done.join()
    assert health.readiness(engine, done, embedded=True) == (False, {"status": "not_ready", "reason": "内嵌 Worker 没有在运行", "schema": health.migrate.current_revision(engine)})
    assert health.readiness(engine, None, embedded=False)[0] is True


def test_worker_alive_reads_own_host_heartbeat(factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        assert not health.worker_alive(db)
        queue.beat_worker(db, "fake-worker-1", role="all", scheduler=True, started_at=queue.now_utc())
        db.commit()
        assert health.worker_alive(db)
        assert not health.worker_alive(db, host="fake-other-host")
        hb = db.get(WorkerHeartbeat, "fake-worker-1")
        hb.last_seen = queue.now_utc() - timedelta(seconds=health.CHECK_SECONDS + 5)
        db.commit()
        assert not health.worker_alive(db)


def test_worker_check_command(app_factory, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    client = app_factory()
    settings = app_factory.settings
    monkeypatch.setenv("YUNSHU_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("YUNSHU_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("YUNSHU_MASTER_KEY_FILE", str(settings.master_key_file))
    monkeypatch.setattr(sys, "argv", ["app.worker", "--check"])
    assert worker.main() == 1
    assert "没有心跳" in capsys.readouterr().out
    with client.app.state.session_factory() as db:
        queue.beat_worker(db, "fake-worker-2", role="all", scheduler=False, started_at=queue.now_utc())
        db.commit()
    assert worker.main() == 0
    assert "Worker 在运行" in capsys.readouterr().out


def test_key_inside_data_dir_names_the_path(tmp_path: Path) -> None:
    data = tmp_path / "var-lib-yunshu"
    with pytest.raises(KeyFileError, match="YUNSHU_MASTER_KEY_FILE") as err:
        load_or_create_keyring(data / ".config" / "yunshu" / "master.key", data_dir=data)
    assert str(data) in str(err.value)


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上检查 ACL")
def test_windows_key_file_acl_is_restricted(tmp_path: Path) -> None:
    key = tmp_path / "keys" / "master.key"
    load_or_create_keyring(key)
    copy = tmp_path / "export" / "master.key"
    export_keyring(key, copy)
    for path in (key, copy):
        acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
        # 不再继承（没有 (I) 标记），只剩当前账号、SYSTEM、Administrators 三条
        entries = [line.replace(str(path), "").rsplit(":(", 1)[0].strip() for line in acl.splitlines() if ":(" in line]
        assert "(I)" not in acl, acl
        assert len(entries) == 3, acl
        assert "NT AUTHORITY\\SYSTEM" in entries and "BUILTIN\\Administrators" in entries, acl
