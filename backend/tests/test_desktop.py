from __future__ import annotations

import threading
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select

from app import queue
from app.db import make_engine, make_session_factory
from app.desktop import DesktopError, LocalServer, desktop_settings, run_desktop
from app.models import SchedulerLock, Task
from app.settings import Settings


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<!doctype html><title>fake desktop</title>")
    # 测试只执行假任务，没有采集、通知或设备控制传输。
    monkeypatch.setattr("app.tasks.build_schedules", lambda _: [])
    monkeypatch.setattr("app.tasks.build_handlers", lambda _: {"fake.desktop": lambda ctx: {"ok": True}})
    return Settings(
        data_dir=tmp_path / "data", database_url=f"sqlite:///{tmp_path.as_posix()}/data/yunshu.db",
        master_key_file=tmp_path / "keys" / "master.key", frontend_dir=frontend,
        backup_interval_hours=0,
    )


def test_real_http_worker_shutdown_and_restart(cfg, monkeypatch):
    completed = threading.Event()
    monkeypatch.setattr("app.tasks.build_handlers", lambda _: {"fake.desktop": lambda ctx: completed.set() or {"ok": True}})
    with LocalServer(cfg) as server:
        assert server.socket.getsockname()[0] == "127.0.0.1"
        assert httpx.get(server.url + "/api/health").json()["status"] == "ok"
        assert "fake desktop" in httpx.get(server.url).text
        assert httpx.get(server.url + "/api/assets").status_code == 401
        response = httpx.post(server.url + "/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert response.json()["user"]["require_password_change"] is True
        eng = make_engine(cfg.database_url)
        factory = make_session_factory(eng)
        with factory() as db:
            task_id = queue.enqueue(db, "fake.desktop").id
            db.commit()
        assert completed.wait(10)
    assert not server.thread.is_alive()
    with factory() as db:
        assert db.get(Task, task_id).status == "succeeded"
        assert db.scalars(select(SchedulerLock)).all() == []
    with pytest.raises(httpx.TransportError):
        httpx.get(server.url + "/api/health", timeout=1)
    # 复用同一数据库立即重启，任务结果、密钥与初始账号保持。
    with LocalServer(cfg) as restarted:
        assert httpx.get(restarted.url + "/api/health").status_code == 200
        with factory() as db:
            assert db.get(Task, task_id).status == "succeeded"
    eng.dispose()


def test_gui_error_still_closes_api_and_worker(cfg):
    class FakeWebview:
        url = ""
        settings = {}

        def create_window(self, _title, *, url, **_kwargs):
            self.url = url
            assert httpx.get(url).status_code == 200

        def start(self, **_kwargs):
            assert self.settings["ALLOW_DOWNLOADS"] is True
            raise RuntimeError("fake GUI failure")

    gui = FakeWebview()
    with pytest.raises(RuntimeError, match="fake GUI"):
        run_desktop(cfg, gui)
    with pytest.raises(httpx.TransportError):
        httpx.get(gui.url, timeout=1)
    assert not any(t.name in {"yunshu-worker", "yunshu-desktop-api"} for t in threading.enumerate())


def test_missing_frontend_refused_before_database_write(cfg):
    with pytest.raises(DesktopError, match="前端"):
        LocalServer(replace(cfg, frontend_dir=cfg.data_dir / "missing"))
    assert not cfg.data_dir.exists()


def test_invalid_key_location_fails_and_cleans_server(cfg):
    server = LocalServer(replace(cfg, master_key_file=cfg.data_dir / "master.key"))
    with pytest.raises(DesktopError, match="启动失败"):
        with server:
            pytest.fail("must not open a window after startup failure")
    assert not server.thread.is_alive()
    assert server.socket.fileno() == -1


def test_desktop_preserves_env_paths_and_control_gate(cfg, monkeypatch):
    monkeypatch.setenv("YUNSHU_DATA_DIR", str(cfg.data_dir))
    monkeypatch.setenv("YUNSHU_MASTER_KEY_FILE", str(cfg.master_key_file))
    monkeypatch.setenv("YUNSHU_FRONTEND_DIR", str(cfg.frontend_dir))
    monkeypatch.setenv("YUNSHU_EMBEDDED_WORKER", "0")
    monkeypatch.delenv("YUNSHU_CONTROL_CUTOVER", raising=False)
    settings = desktop_settings()
    assert settings.embedded_worker
    # 切换开关默认打开（真正切换仍需管理员确认）；显式设为 0 才关闭
    assert settings.control_cutover_enabled
    monkeypatch.setenv("YUNSHU_CONTROL_CUTOVER", "0")
    assert not desktop_settings().control_cutover_enabled
    assert settings.data_dir == cfg.data_dir
    assert settings.master_key_file == cfg.master_key_file
    assert settings.frontend_dir == cfg.frontend_dir
    # PyInstaller 资源在 _MEIPASS，不能按源码目录层级寻找 frontend/dist。
    import sys
    monkeypatch.delenv("YUNSHU_FRONTEND_DIR")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(cfg.frontend_dir.parent), raising=False)
    assert desktop_settings().frontend_dir == cfg.frontend_dir
