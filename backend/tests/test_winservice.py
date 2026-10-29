"""Windows 服务版入口（app/winservice.py）：状态机、启动 / 停止、免安装包标记；SCM 用假对象，只用临时目录和假数据。"""
from __future__ import annotations

import signal
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app import winservice as ws
from app.db import make_engine, make_session_factory
from app.models import SchedulerLock


class FakeServer:
    """行为像 uvicorn.Server：run() 一直跑到 should_exit；可模拟启动失败、启动很慢、停不下来。"""

    def __init__(self, *, fail: bool = False, start_delay: float = 0.0, stubborn: bool = False,
                 die_after: float | None = None) -> None:
        self.started = False
        self.should_exit = False
        self.force_exit = False
        self.fail, self.start_delay, self.stubborn, self.die_after = fail, start_delay, stubborn, die_after
        self.cleaned_up = False

    def run(self) -> None:
        if self.fail:
            raise SystemExit(1)  # uvicorn 端口被占用时的做法
        time.sleep(self.start_delay)
        self.started = True
        began = time.monotonic()
        while not self.should_exit or (self.stubborn and not self.force_exit):
            if self.die_after is not None and time.monotonic() - began > self.die_after:
                raise RuntimeError("fake crash")
            time.sleep(0.01)
        self.cleaned_up = True


class FakeScm:
    def __init__(self) -> None:
        self.statuses: list[tuple[int, int, int, int]] = []

    def set_status(self, state: int, *, accepted: int, exit_code: int, specific: int, checkpoint: int,
                   wait_hint: int) -> None:
        self.statuses.append((state, accepted, exit_code, specific))

    @property
    def states(self) -> list[int]:
        out: list[int] = []
        for s in self.statuses:
            if not out or out[-1] != s[0]:
                out.append(s[0])
        return out


def _service(scm: FakeScm, server: FakeServer, **kwargs) -> ws.YunshuService:  # type: ignore[no-untyped-def]
    kwargs.setdefault("prepare", lambda: None)
    kwargs.setdefault("monitor_interval", 0.02)
    return ws.YunshuService(scm, runner_factory=lambda: ws.ServiceRunner(lambda: server, poll=0.01), **kwargs)


def _run_in_thread(svc: ws.YunshuService) -> tuple[threading.Thread, dict[str, int]]:
    result: dict[str, int] = {}
    t = threading.Thread(target=lambda: result.setdefault("code", svc.run()))
    t.start()
    return t, result


def _wait_for(predicate, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def test_start_then_stop_reports_states_and_stops_server_cleanly() -> None:
    scm, server = FakeScm(), FakeServer()
    svc = _service(scm, server)
    t, result = _run_in_thread(svc)
    _wait_for(lambda: ws.SERVICE_RUNNING in scm.states)
    running = [s for s in scm.statuses if s[0] == ws.SERVICE_RUNNING][0]
    assert running[1] == ws.SERVICE_ACCEPT_STOP | ws.SERVICE_ACCEPT_SHUTDOWN | ws.SERVICE_ACCEPT_PRESHUTDOWN
    assert svc.handle_control(ws.SERVICE_CONTROL_STOP) == ws.NO_ERROR
    assert svc.handle_control(ws.SERVICE_CONTROL_STOP) == ws.NO_ERROR  # 重复的停止请求无害
    t.join(5)
    assert result["code"] == 0 and server.cleaned_up
    assert scm.states == [ws.SERVICE_START_PENDING, ws.SERVICE_RUNNING, ws.SERVICE_STOP_PENDING, ws.SERVICE_STOPPED]
    assert scm.statuses[-1] == (ws.SERVICE_STOPPED, 0, ws.NO_ERROR, 0)


@pytest.mark.parametrize("control", [ws.SERVICE_CONTROL_SHUTDOWN, ws.SERVICE_CONTROL_PRESHUTDOWN])
def test_shutdown_controls_also_stop(control: int) -> None:
    scm, server = FakeScm(), FakeServer()
    svc = _service(scm, server)
    t, result = _run_in_thread(svc)
    _wait_for(lambda: ws.SERVICE_RUNNING in scm.states)
    svc.handle_control(control)
    t.join(5)
    assert result["code"] == 0 and server.cleaned_up


def test_interrogate_and_unknown_controls() -> None:
    svc = _service(FakeScm(), FakeServer())
    assert svc.handle_control(ws.SERVICE_CONTROL_INTERROGATE) == ws.NO_ERROR
    assert svc.handle_control(0x80) == ws.ERROR_CALL_NOT_IMPLEMENTED
    assert not svc.stop_requested.is_set()


def test_start_failure_reports_service_specific_error() -> None:
    scm = FakeScm()
    svc = _service(scm, FakeServer(fail=True))
    assert svc.run() == ws.EXIT_START_FAILED
    assert scm.states == [ws.SERVICE_START_PENDING, ws.SERVICE_STOPPED]
    assert scm.statuses[-1][2:] == (ws.ERROR_SERVICE_SPECIFIC_ERROR, ws.EXIT_START_FAILED)


def test_prepare_problem_is_not_started() -> None:
    scm, server = FakeScm(), FakeServer()
    svc = _service(scm, server, prepare=lambda: "主密钥文件不能放在数据目录里")
    assert svc.run() == ws.EXIT_START_FAILED
    assert not server.started


def test_slow_start_reports_running_after_timeout_with_checkpoints() -> None:
    scm, server = FakeScm(), FakeServer(start_delay=0.3)
    svc = _service(scm, server, ready_timeout=0.05)
    t, result = _run_in_thread(svc)
    _wait_for(lambda: ws.SERVICE_RUNNING in scm.states)
    svc.handle_control(ws.SERVICE_CONTROL_STOP)
    t.join(5)
    assert result["code"] == 0
    pending = [s for s in scm.statuses if s[0] == ws.SERVICE_START_PENDING]
    assert len(pending) >= 2  # 等待期间不断推进 checkpoint


def test_server_dying_while_running_is_reported_for_recovery() -> None:
    scm = FakeScm()
    svc = _service(scm, FakeServer(die_after=0.05))
    assert svc.run() == ws.EXIT_SERVER_DIED
    assert scm.statuses[-1][2:] == (ws.ERROR_SERVICE_SPECIFIC_ERROR, ws.EXIT_SERVER_DIED)


def test_stop_timeout_forces_exit() -> None:
    scm, server = FakeScm(), FakeServer(stubborn=True)
    svc = _service(scm, server, stop_timeout=0.1)
    t, result = _run_in_thread(svc)
    _wait_for(lambda: ws.SERVICE_RUNNING in scm.states)
    svc.handle_control(ws.SERVICE_CONTROL_STOP)
    t.join(10)
    assert result["code"] == ws.EXIT_STOP_TIMEOUT
    assert server.force_exit


def test_checkpoints_increase_and_reset() -> None:
    seen: list[tuple[int, int]] = []

    class Recorder:
        def set_status(self, state: int, *, checkpoint: int, **_: int) -> None:
            seen.append((state, checkpoint))

    svc = ws.YunshuService(Recorder(), prepare=lambda: None)
    svc.report(ws.SERVICE_START_PENDING)
    svc.report(ws.SERVICE_START_PENDING)
    svc.report(ws.SERVICE_RUNNING)
    svc.report(ws.SERVICE_STOP_PENDING)
    assert seen == [(ws.SERVICE_START_PENDING, 1), (ws.SERVICE_START_PENDING, 2), (ws.SERVICE_RUNNING, 0),
                    (ws.SERVICE_STOP_PENDING, 1)]


def test_foreground_mode_stops_on_ctrl_c(monkeypatch: pytest.MonkeyPatch) -> None:
    scm, server = FakeScm(), FakeServer()
    svc = _service(scm, server)
    joins = {"n": 0}
    real_join = threading.Thread.join

    def interrupting_join(self, timeout=None):  # type: ignore[no-untyped-def]
        if self.name == "yunshu-service-main" and timeout == 0.5:
            joins["n"] += 1
            if joins["n"] == 3:
                signal.raise_signal(signal.SIGINT)  # 控制台里按 Ctrl+C
        return real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", interrupting_join)
    before = signal.getsignal(signal.SIGINT)
    assert ws.run_foreground(service=svc) == 0
    assert server.cleaned_up
    assert signal.getsignal(signal.SIGINT) is before  # 处理函数已还原


def test_service_command_help_and_non_windows(capsys, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    assert ws.main(["service", "--help"]) == 0
    assert "--foreground" in capsys.readouterr().out
    monkeypatch.setattr(sys, "platform", "linux")
    assert ws.service_command([]) == 2


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上声明 advapi32")
def test_dispatch_outside_scm_explains(capsys) -> None:  # type: ignore[no-untyped-def]
    # 不注册任何服务：在普通进程里调 StartServiceCtrlDispatcher 会返回 1063
    assert ws.service_command([]) == 1
    assert "service --foreground" in capsys.readouterr().err


def test_portable_marker_uses_desktop_layout(tmp_path: Path) -> None:
    root = tmp_path / "Yunshu"
    cli_dir = root / "cli"
    cli_dir.mkdir(parents=True)
    env: dict[str, str] = {}
    assert ws.apply_portable_defaults(env, cli_dir) is False and env == {}
    assert ws.apply_portable_defaults(env, None) is False
    (cli_dir / ws.PORTABLE_MARKER).write_text("# 桌面版\nYUNSHU_CONTROL_CUTOVER=0\n", encoding="utf-8")
    assert ws.apply_portable_defaults(env, cli_dir) is True
    assert env["YUNSHU_CONFIG"] == str(cli_dir / ws.PORTABLE_MARKER)
    assert env["YUNSHU_DATA_DIR"] == str(root / "data")
    assert env["YUNSHU_CONTROL_CUTOVER"] == "0"
    # 标记文件或进程里指定的数据目录优先
    (cli_dir / ws.PORTABLE_MARKER).write_text("YUNSHU_DATA_DIR=D:\\fake-data\n", encoding="utf-8")
    env2: dict[str, str] = {}
    ws.apply_portable_defaults(env2, cli_dir)
    assert env2["YUNSHU_DATA_DIR"] == "D:\\fake-data"


def test_prepare_environment_reads_config_and_defaults_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conf = tmp_path / "yunshu.env"
    conf.write_text(f"YUNSHU_DATA_DIR={tmp_path / 'pd' / 'data'}\nYUNSHU_MASTER_KEY_FILE={tmp_path / 'pd' / 'keys' / 'master.key'}\n",
                    encoding="utf-8")
    (tmp_path / "pd").mkdir()
    for key in ("YUNSHU_DATA_DIR", "YUNSHU_MASTER_KEY_FILE", "YUNSHU_LOG_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("YUNSHU_CONFIG", str(conf))
    monkeypatch.setenv("ProgramData", str(tmp_path / "programdata"))
    monkeypatch.chdir(tmp_path)
    import logging
    import logging.handlers
    import os

    try:
        assert ws.prepare_environment() is None
        assert os.environ["YUNSHU_LOG_DIR"] == str(tmp_path / "programdata" / "Yunshu" / "logs")
        assert Path.cwd() == tmp_path / "pd"
    finally:
        for h in list(logging.getLogger().handlers):
            if isinstance(h, logging.handlers.RotatingFileHandler):
                logging.getLogger().removeHandler(h)
                h.close()
    # 主密钥放进数据目录时拒绝启动（与 yunshu api 相同的检查）
    monkeypatch.setenv("YUNSHU_MASTER_KEY_FILE", str(tmp_path / "pd" / "data" / "master.key"))
    try:
        assert "主密钥" in (ws.prepare_environment() or "")
    finally:
        for h in list(logging.getLogger().handlers):
            if isinstance(h, logging.handlers.RotatingFileHandler):
                logging.getLogger().removeHandler(h)
                h.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_uvicorn_server_stops_and_releases_worker_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真正的 uvicorn + 内嵌 Worker：服务停止后单实例锁已释放（假任务，不访问设备）。"""
    monkeypatch.setattr("app.tasks.build_schedules", lambda _: [])
    monkeypatch.setattr("app.tasks.build_handlers", lambda _: {})
    port = _free_port()
    db_url = f"sqlite:///{tmp_path.as_posix()}/data/yunshu.db"
    env = {
        "YUNSHU_DATA_DIR": str(tmp_path / "data"), "YUNSHU_DATABASE_URL": db_url,
        "YUNSHU_MASTER_KEY_FILE": str(tmp_path / "keys" / "master.key"), "YUNSHU_EMBEDDED_WORKER": "1",
        "YUNSHU_SETUP_WIZARD": "0", "YUNSHU_BACKUP_INTERVAL_HOURS": "0", "YUNSHU_BIND": "127.0.0.1",
        "YUNSHU_PORT": str(port),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("YUNSHU_FRONTEND_DIR", raising=False)
    scm = FakeScm()
    svc = ws.YunshuService(scm, prepare=lambda: None, monitor_interval=0.05, ready_timeout=60)
    t, result = _run_in_thread(svc)
    _wait_for(lambda: ws.SERVICE_RUNNING in scm.states or ws.SERVICE_STOPPED in scm.states, timeout=60)
    assert ws.SERVICE_RUNNING in scm.states
    base = f"http://127.0.0.1:{port}"
    _wait_for(lambda: httpx.get(base + "/api/ready", trust_env=False).status_code == 200, timeout=30)
    login = httpx.post(base + "/api/auth/login", json={"username": "admin", "password": "admin123"}, trust_env=False)
    assert login.json()["user"]["require_password_change"] is True
    engine = make_engine(db_url)
    factory = make_session_factory(engine)
    with factory() as db:
        assert db.scalar(select(SchedulerLock).where(SchedulerLock.name == "worker"))
    svc.handle_control(ws.SERVICE_CONTROL_STOP)
    t.join(60)
    assert result["code"] == 0
    with factory() as db:
        assert db.scalars(select(SchedulerLock)).all() == []
    engine.dispose()
    with pytest.raises(httpx.TransportError):
        httpx.get(base + "/api/health", timeout=1, trust_env=False)


def test_redirected_output_switches_to_utf8() -> None:
    import io

    piped = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    console.isatty = lambda: True  # type: ignore[method-assign]
    ws.utf8_redirected_streams((piped, console, None))
    assert piped.encoding == "utf-8" and console.encoding == "cp1252"
    piped.write("备份完成")  # 英文代码页下原本会 UnicodeEncodeError
    piped.flush()


def test_harvest_lists_every_file_except_service_exe(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "harvest", Path(__file__).resolve().parents[2] / "packaging" / "windows" / "harvest.py")
    harvest = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(harvest)  # type: ignore[union-attr]
    app = tmp_path / "yunshu"
    (app / "_internal" / "tzdata" / "zoneinfo").mkdir(parents=True)
    (app / "yunshu.exe").write_bytes(b"fake")
    (app / "_internal" / "python311.dll").write_bytes(b"fake")
    (app / "_internal" / "tzdata" / "zoneinfo" / "UTC").write_bytes(b"fake")
    (app / "_internal" / "a$b.txt").write_bytes(b"fake")
    text = harvest.harvest(app)
    assert "yunshu.exe" not in text
    assert text.count("<Component ") == 3 and text.count("<File ") == 3
    assert 'Name="_internal"' in text and 'Name="zoneinfo"' in text
    assert "a$$b.txt" in text  # WiX 预处理器的 $ 已转义
    with pytest.raises(SystemExit):
        harvest.harvest(tmp_path)
