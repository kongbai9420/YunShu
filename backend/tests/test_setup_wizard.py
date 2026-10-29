"""首次启动向导（4.1 P12）：进入条件、设置模式的接口门禁、口令、轻量版与 PostgreSQL 选择。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.main import create_app
from app.settings import Settings
from app.setup_wizard import DatabaseChoice, build_url, check_url, config_path
from tests.conftest import PG_ADMIN_URL

pg_only = pytest.mark.skipif(not PG_ADMIN_URL, reason="需要 PostgreSQL")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("YUNSHU_DATABASE_URL", "YUNSHU_SETUP_WIZARD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("YUNSHU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YUNSHU_MASTER_KEY_FILE", str(tmp_path / "keys" / "master.key"))
    return tmp_path


def test_setup_pending_only_on_fresh_install_without_database_choice(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.from_env().setup_pending is True

    monkeypatch.setenv("YUNSHU_SETUP_WIZARD", "0")
    assert Settings.from_env().setup_pending is False
    monkeypatch.delenv("YUNSHU_SETUP_WIZARD")

    # 已在用的轻量版（升级上来的旧部署）不进向导
    (env / "data").mkdir()
    (env / "data" / "yunshu.db").write_bytes(b"")
    assert Settings.from_env().setup_pending is False
    (env / "data" / "yunshu.db").unlink()

    monkeypatch.setenv("YUNSHU_DATABASE_URL", "postgresql+psycopg://u@db.example.invalid/yunshu")
    cfg = Settings.from_env()
    assert cfg.setup_pending is False and cfg.database_url.startswith("postgresql")


def test_saved_choice_is_used_and_kept_out_of_data_dir(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = env / "keys" / "master.key"
    path = config_path(key)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"url": "postgresql+psycopg://u:fake-pass@db.example.invalid/yunshu"}), encoding="utf-8")
    cfg = Settings.from_env()
    assert cfg.setup_pending is False
    assert cfg.database_url == "postgresql+psycopg://u:fake-pass@db.example.invalid/yunshu"
    assert path.parent != cfg.data_dir
    # 环境变量优先于保存的选择
    monkeypatch.setenv("YUNSHU_DATABASE_URL", "sqlite:///x.db")
    assert Settings.from_env().database_url == "sqlite:///x.db"


def _setup_app(env: Path) -> TestClient:
    cfg = Settings.from_env()
    assert cfg.setup_pending
    return TestClient(create_app(cfg))


def test_setup_mode_blocks_other_api_and_requires_token_from_remote(env: Path) -> None:
    with _setup_app(env) as client:
        assert client.get("/api/health").json()["setup"] is True
        blocked = client.get("/api/auth/me")
        assert blocked.status_code == 503 and blocked.json()["setup"] is True
        assert client.get("/api/assets").status_code == 503
        assert client.get("/metrics").status_code == 503
        # TestClient 的来源地址不是本机回环，按远程访问处理
        assert client.get("/api/setup/status").json() == {"pending": True, "token_required": True}
        assert client.post("/api/setup/test", json={"kind": "sqlite"}).status_code == 403
        assert client.post("/api/setup/apply", json={"kind": "sqlite", "token": "wrong"}).status_code == 403
        token = client.app.state.setup.token
        ok = client.post("/api/setup/test", json={"kind": "sqlite", "token": token})
        assert ok.status_code == 200 and ok.json()["ok"] is True


def test_choose_lightweight_initializes_in_process(env: Path) -> None:
    with _setup_app(env) as client:
        token = client.app.state.setup.token
        r = client.post("/api/setup/apply", json={"kind": "sqlite", "token": token})
        assert r.status_code == 200, r.text
        assert client.get("/api/setup/status").json() == {"pending": False}
        assert "setup" not in client.get("/api/health").json()
        assert client.post("/api/setup/apply", json={"kind": "sqlite", "token": token}).status_code == 409
        login = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert login.status_code == 200 and login.json()["user"]["require_password_change"] is True
    saved = json.loads(config_path(env / "keys" / "master.key").read_text(encoding="utf-8"))["url"]
    assert saved.startswith("sqlite:///") and saved.endswith("/data/yunshu.db")
    # 重启后直接使用已选择的库，不再进向导
    assert Settings.from_env().setup_pending is False


def test_postgresql_choice_validates_fields_and_reports_refused_connection(env: Path) -> None:
    with pytest.raises(ValueError, match="主机、数据库名、用户名"):
        build_url(DatabaseChoice(kind="postgresql"), env)
    url = build_url(DatabaseChoice(kind="postgresql", host="127.0.0.1", port=1, database="yunshu", username="u", password="fake-pass"), env)
    assert make_url(url).password == "fake-pass"
    result = check_url(url)
    assert result.ok is False
    assert "fake-pass" not in result.message


def _pg_choice(database: str, token: str) -> dict[str, object]:
    admin = make_url(PG_ADMIN_URL)
    return {"kind": "postgresql", "host": admin.host, "port": admin.port or 5432, "database": database,
            "username": admin.username, "password": admin.password or "", "sslmode": "disable", "token": token}


@pg_only
def test_choose_postgresql_empty_database_and_reject_foreign_tables(env: Path) -> None:
    admin = create_engine(make_url(PG_ADMIN_URL), isolation_level="AUTOCOMMIT")
    empty, foreign = f"yunshu_setup_{uuid.uuid4().hex[:8]}", f"yunshu_setup_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{empty}"'))
        c.execute(text(f'CREATE DATABASE "{foreign}"'))
    other = create_engine(make_url(PG_ADMIN_URL).set(database=foreign))
    with other.begin() as c:
        c.execute(text("CREATE TABLE someone_else (id int)"))
    other.dispose()
    try:
        with _setup_app(env) as client:
            token = client.app.state.setup.token
            bad = client.post("/api/setup/test", json=_pg_choice(foreign, token)).json()
            assert bad["ok"] is False and bad["state"] == "foreign"
            assert client.post("/api/setup/apply", json=_pg_choice(foreign, token)).status_code == 400
            good = client.post("/api/setup/test", json=_pg_choice(empty, token)).json()
            assert good["ok"] is True and good["state"] == "empty"
            assert client.post("/api/setup/apply", json=_pg_choice(empty, token)).status_code == 200
            assert client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).status_code == 200
        cfg = Settings.from_env()
        assert make_url(cfg.database_url).database == empty
        # 已有本平台数据的库可以重新连接
        assert check_url(cfg.database_url).state == "yunshu"
    finally:
        with admin.connect() as c:
            for name in (empty, foreign):
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_postgresql_without_driver_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """免安装桌面版等没有 PostgreSQL 驱动的安装：说明原因，不抛异常。"""
    import sys
    monkeypatch.setitem(sys.modules, "psycopg", None)
    result = check_url("postgresql+psycopg://u:fake-pass@192.0.2.10:5432/yunshu")
    assert result.ok is False and "驱动" in result.message and "fake-pass" not in result.message
