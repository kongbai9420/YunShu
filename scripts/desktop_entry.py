"""Windows 免安装包入口；自检始终使用全新的临时库，不读取用户配置。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path


def smoke_test(report: Path, *, native_window: bool = False) -> int:
    import httpx
    import webview.platforms.edgechromium  # noqa: F401 - 实际加载 WebView2 / CLR DLL，不打开窗口
    from sqlalchemy import select
    from app.desktop import LocalServer, desktop_settings
    from app.models import Asset, SchedulerLock
    from app.settings import Settings

    # 时区数据必须真的在包里（不是靠 _local_tz 退回固定 UTC+8）：每日报告、巡检计划按北京时间算
    from zoneinfo import ZoneInfo
    ZoneInfo("Asia/Shanghai")

    frontend = desktop_settings().frontend_dir
    with tempfile.TemporaryDirectory(prefix="yunshu-package-check-") as folder:
        root = Path(folder)
        cfg = Settings(
            data_dir=root / "data", database_url=f"sqlite:///{root.as_posix()}/data/yunshu.db",
            master_key_file=root / "keys" / "master.key", frontend_dir=frontend,
            backup_interval_hours=0, embedded_worker=True,
        )
        with LocalServer(cfg) as server:
            with httpx.Client(base_url=server.url, trust_env=False) as client:
                assert client.get("/api/health").json()["status"] == "ok"
                assert '<div id="root">' in client.get("/").text
                for asset in (frontend / "assets").iterdir():
                    assert client.get("/assets/" + asset.name).status_code == 200
                assert client.get("/api/assets").status_code == 401
                login = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
                assert login.json()["user"]["require_password_change"] is True
            if native_window:
                import webview
                window = webview.create_window("云枢运维平台（离线自检）", server.url, hidden=True)
                errors = []

                def check_render():
                    try:
                        assert window.events.loaded.wait(30), "Native page did not load"
                        deadline = time.monotonic() + 20
                        while not window.evaluate_js("Boolean(document.querySelector('#login-user') && document.querySelector('input[type=password]'))"):
                            assert time.monotonic() < deadline, "React login did not render"
                            time.sleep(0.1)
                    except Exception as exc:
                        errors.append(str(exc))
                    finally:
                        window.destroy()

                webview.start(check_render, gui="edgechromium", private_mode=True, debug=False)
                assert not errors, errors
            factory = server.server.config.app.state.session_factory
            deadline = time.monotonic() + 10
            while True:
                with factory() as db:
                    if db.scalar(select(SchedulerLock).where(SchedulerLock.name == "worker")):
                        break
                assert time.monotonic() < deadline, "Worker did not acquire lock"
                time.sleep(0.05)
            with factory() as db:
                db.add(Asset(name="fake-package-persistence", form="physical", role="server"))
                db.commit()
        assert not server.thread.is_alive()
        with LocalServer(cfg) as restarted:
            with restarted.server.config.app.state.session_factory() as db:
                assert db.scalar(select(Asset).where(Asset.name == "fake-package-persistence"))
        assert not restarted.thread.is_alive()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({
        "status": "ok", "gui_dll_import": True, "frontend_assets": True,
        "login": True, "embedded_worker_lock": True, "restart_persistence": True, "tz_data": True,
        "native_window_tested": native_window,
    }, indent=2), encoding="utf-8")
    return 0


def main() -> int:
    # 双击启动时固定相对路径：数据在 exe 同级 data/，资源在只读的 _internal/。
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).parent)
    for flag in ("--smoke-test", "--smoke-window"):
        if flag in sys.argv:
            report = Path(sys.argv[sys.argv.index(flag) + 1]).resolve()
            return smoke_test(report, native_window=flag == "--smoke-window")
    from app.desktop import main as desktop_main
    return desktop_main()


if __name__ == "__main__":
    # windowed 程序没有控制台；保留启动失败日志供排查，不在普通界面暴露实现细节。
    if sys.stdout is None or sys.stderr is None:
        log_dir = Path(sys.executable).parent / "logs"
        log_dir.mkdir(exist_ok=True)
        stream = (log_dir / "desktop.log").open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
    raise SystemExit(main())
