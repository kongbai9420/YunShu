"""pywebview 桌面入口：同一进程内运行 FastAPI 与内嵌 Worker。

    python -m app.desktop

只监听回环地址，系统分配端口；界面仍使用登录、角色与 CSRF 校验。
导入本模块不会启动服务，也不导入 GUI 库。
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import uvicorn
from sqlalchemy.engine import make_url

from app.main import create_app
from app.settings import Settings


class DesktopError(RuntimeError):
    pass


def desktop_settings() -> Settings:
    cfg = Settings.from_env()
    return replace(
        cfg,
        embedded_worker=True,
        frontend_dir=cfg.frontend_dir or (
            Path(sys._MEIPASS) / "frontend" if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[2] / "frontend" / "dist"
        ),
    )


class LocalServer:
    def __init__(self, settings: Settings, *, startup_timeout: float = 60):
        if make_url(settings.database_url).get_backend_name() != "sqlite":
            raise DesktopError("桌面版仅支持 SQLite。")
        if settings.frontend_dir is None or not (settings.frontend_dir / "index.html").is_file():
            raise DesktopError("缺少前端构建文件，请先在 frontend 运行 npm ci 和 npm run build。")
        if settings.secure_cookies:
            raise DesktopError("桌面版使用本机 HTTP，请取消 YUNSHU_SECURE_COOKIES=1。")
        self.settings = replace(settings, embedded_worker=True)
        self.startup_timeout = startup_timeout
        self.server = uvicorn.Server(uvicorn.Config(
            create_app(self.settings), host="127.0.0.1", port=0,
            access_log=False, timeout_graceful_shutdown=3,
        ))
        self.thread: threading.Thread | None = None
        self.socket = None
        self.url = ""

    def __enter__(self) -> LocalServer:
        try:
            self.socket = self.server.config.bind_socket()
            self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
            self.thread = threading.Thread(
                target=self._serve, name="yunshu-desktop-api", daemon=True,
            )
            self.thread.start()
            deadline = time.monotonic() + self.startup_timeout
            while not self.server.started:
                if not self.thread.is_alive():
                    raise DesktopError("本机服务启动失败，请检查数据目录、主密钥与启动日志。")
                if time.monotonic() >= deadline:
                    raise DesktopError("本机服务启动超时。")
                time.sleep(0.02)
            return self
        except BaseException:
            self.close()
            raise

    def _serve(self) -> None:
        # Uvicorn 在启动失败时可能抛出 SystemExit；主线程通过存活状态判断失败。
        try:
            self.server.run(sockets=[self.socket])
        except SystemExit:
            pass

    def close(self) -> None:
        self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=110)
            if self.thread.is_alive():
                raise DesktopError("本机服务尚未完成关闭，请查看日志后再重启。")
        if self.socket is not None:
            self.socket.close()

    def __exit__(self, *_args) -> None:
        self.close()


def run_desktop(settings: Settings, webview) -> None:
    with LocalServer(settings) as server:
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.create_window(
            "云枢运维平台", url=server.url, width=1280, height=850,
            min_size=(960, 640), text_select=True, zoomable=True,
        )
        webview.start(gui="edgechromium" if sys.platform == "win32" else None,
                      debug=False, private_mode=True)


def main() -> int:
    try:
        import webview
    except ImportError:
        print("请先安装桌面依赖：python -m pip install --require-hashes -r requirements-desktop.lock", file=sys.stderr)
        return 1
    try:
        run_desktop(desktop_settings(), webview)
    except DesktopError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
