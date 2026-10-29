"""Windows 服务版的程序入口（`yunshu.exe`，PyInstaller 打包；也放进免安装 ZIP 的 cli 目录）。

    yunshu.exe service                由服务管理器（SCM）启动，MSI 注册的服务 Yunshu 就是这样运行的
    yunshu.exe service --foreground   在控制台里用同一套启动 / 停止逻辑运行，Ctrl+C 停止（排查用）
    yunshu.exe <其他命令>             与 `yunshu` 命令行完全相同（见 app/cli.py）

服务在本进程的一个线程里运行与 `yunshu api` 相同的 uvicorn 服务（内嵌 Worker 由 yunshu.env 打开）。
收到停止 / 关机通知时让 uvicorn 正常退出：先停内嵌 Worker、释放单实例锁，再报告“已停止”。
启动失败或运行中意外退出时以错误状态结束，由服务的恢复策略（失败后重启）重新拉起。

与 SCM 的交互只用 ctypes 调 advapi32，不额外依赖 pywin32；对外的逻辑（YunshuService、ServiceRunner）
不碰 Windows API，测试用假对象驱动。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any, Protocol

SERVICE_NAME = "Yunshu"
USAGE = "用法：yunshu service [--foreground]（只由服务管理器启动；--foreground 在控制台里运行，Ctrl+C 停止）"
# 免安装 ZIP 里 cli\yunshu.exe 旁边的标记文件：有它时按桌面版的目录约定运行，不读服务版的 %ProgramData% 配置
PORTABLE_MARKER = "desktop-portable.env"

# winsvc.h
SERVICE_WIN32_OWN_PROCESS = 0x10
SERVICE_STOPPED, SERVICE_START_PENDING, SERVICE_STOP_PENDING, SERVICE_RUNNING = 1, 2, 3, 4
SERVICE_ACCEPT_STOP, SERVICE_ACCEPT_SHUTDOWN, SERVICE_ACCEPT_PRESHUTDOWN = 0x1, 0x4, 0x100
SERVICE_CONTROL_STOP, SERVICE_CONTROL_INTERROGATE = 0x1, 0x4
SERVICE_CONTROL_SHUTDOWN, SERVICE_CONTROL_PRESHUTDOWN = 0x5, 0xF
NO_ERROR = 0
ERROR_CALL_NOT_IMPLEMENTED = 120
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063
ERROR_SERVICE_SPECIFIC_ERROR = 1066
# 自定义的服务退出码（dwServiceSpecificExitCode），写进日志与 SCM 事件，便于区分
EXIT_START_FAILED, EXIT_SERVER_DIED, EXIT_STOP_TIMEOUT = 1, 2, 3

log = logging.getLogger(__name__)


class ServerLike(Protocol):
    started: bool
    should_exit: bool
    force_exit: bool

    def run(self) -> None: ...


class ServiceStartError(RuntimeError):
    pass


class ServiceRunner:
    """在后台线程运行 uvicorn 服务；start 等它起来，stop 让它正常退出并等线程结束。"""

    def __init__(self, make_server: Callable[[], ServerLike], *, clock: Callable[[], float] = time.monotonic,
                 poll: float = 0.2) -> None:
        self._make_server = make_server
        self._clock = clock
        self._poll = poll
        self.server: ServerLike | None = None
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def _serve(self) -> None:
        try:
            assert self.server is not None
            self.server.run()
        except BaseException as exc:  # noqa: BLE001 - uvicorn 端口占用时 sys.exit(1)，只结束这个线程
            self.error = exc
            log.exception("服务线程异常退出")

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, *, ready_timeout: float = 20.0, on_wait: Callable[[], None] | None = None) -> bool:
        """返回 True 表示已在监听；False 表示超时仍在启动（例如大库升级结构），由调用方继续监视线程。"""
        self.server = self._make_server()
        self.thread = threading.Thread(target=self._serve, name="yunshu-service-api", daemon=True)
        self.thread.start()
        deadline = self._clock() + ready_timeout
        while not self.server.started:
            if not self.thread.is_alive():
                raise ServiceStartError(f"服务没能启动：{type(self.error).__name__ if self.error else '线程已退出'}")
            if self._clock() >= deadline:
                return False
            if on_wait:
                on_wait()
            self.thread.join(self._poll)
        return True

    def stop(self, *, timeout: float = 90.0, on_wait: Callable[[], None] | None = None) -> bool:
        """请求正常退出（先停内嵌 Worker、释放锁）；超时后强制结束。返回是否在时限内正常结束。"""
        if self.server is None or self.thread is None:
            return True
        self.server.should_exit = True
        deadline = self._clock() + timeout
        while self.thread.is_alive() and self._clock() < deadline:
            if on_wait:
                on_wait()
            self.thread.join(self._poll)
        if not self.thread.is_alive():
            return True
        log.error("服务 %s 秒内没有正常退出，强制结束", timeout)
        self.server.force_exit = True
        self.thread.join(5)
        return False


def make_uvicorn_server(environ: MutableMapping[str, str] | None = None) -> Any:
    """与 `yunshu api`（app/cli.py run_api）相同的监听参数。"""
    import uvicorn

    env = os.environ if environ is None else environ
    config = uvicorn.Config(
        "app.main:create_app", factory=True,
        host=env.get("YUNSHU_BIND", "0.0.0.0"), port=int(env.get("YUNSHU_PORT", "8080")),
        proxy_headers=True, log_config=None, timeout_graceful_shutdown=10,
    )
    return uvicorn.Server(config)


def _program_data(environ: MutableMapping[str, str]) -> Path:
    return Path(environ.get("ProgramData") or environ.get("PROGRAMDATA") or r"C:\ProgramData") / "Yunshu"


def prepare_environment(environ: MutableMapping[str, str] | None = None) -> str | None:
    """服务启动前：读 yunshu.env、补默认日志目录、设好工作目录与日志。返回阻止启动的原因，没有则 None。"""
    from app import cli

    env = os.environ if environ is None else environ
    config = cli.load_config(env)
    env.setdefault("YUNSHU_LOG_DIR", str(_program_data(env) / "logs"))
    cli._frozen_defaults()
    # SCM 启动的进程工作目录是 System32；相对路径（如默认的 data）一律按数据目录的上一级解析
    data_dir = env.get("YUNSHU_DATA_DIR")
    base = Path(data_dir).parent if data_dir else _program_data(env)
    if base.is_dir():
        os.chdir(base)
    cli.setup_logging("service")
    if config is not None:
        log.info("已读取配置文件 %s", config)
    return cli._check_key_location()


class ScmApi(Protocol):
    def set_status(self, state: int, *, accepted: int, exit_code: int, specific: int, checkpoint: int,
                   wait_hint: int) -> None: ...


class YunshuService:
    """服务的状态机：ServiceMain 线程里调 run()，SCM 控制线程里调 handle_control()。"""

    def __init__(self, scm: ScmApi, *, runner_factory: Callable[[], ServiceRunner] | None = None,
                 prepare: Callable[[], str | None] = prepare_environment, ready_timeout: float = 20.0,
                 stop_timeout: float = 90.0, monitor_interval: float = 1.0) -> None:
        self.scm = scm
        self.runner_factory = runner_factory or (lambda: ServiceRunner(make_uvicorn_server))
        self.prepare = prepare
        self.ready_timeout = ready_timeout
        self.stop_timeout = stop_timeout
        self.monitor_interval = monitor_interval
        self.stop_requested = threading.Event()
        self._lock = threading.Lock()
        self._state = SERVICE_STOPPED
        self._checkpoint = 0

    def report(self, state: int, *, exit_code: int = NO_ERROR, specific: int = 0, wait_hint: int = 0) -> None:
        with self._lock:
            pending = state in (SERVICE_START_PENDING, SERVICE_STOP_PENDING)
            self._checkpoint = self._checkpoint + 1 if pending else 0
            self._state = state
            # 只在运行中接受控制；启动 / 停止过程中 SCM 不会再发停止
            accepted = (SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN | SERVICE_ACCEPT_PRESHUTDOWN
                        if state == SERVICE_RUNNING else 0)
            self.scm.set_status(state, accepted=accepted, exit_code=exit_code, specific=specific,
                                checkpoint=self._checkpoint, wait_hint=wait_hint)

    def _fail(self, specific: int) -> int:
        self.report(SERVICE_STOPPED, exit_code=ERROR_SERVICE_SPECIFIC_ERROR, specific=specific)
        return specific

    def run(self) -> int:
        """返回服务退出码：0 正常停止，其余见 EXIT_*。"""
        self.report(SERVICE_START_PENDING, wait_hint=30_000)
        try:
            problem = self.prepare()
            if problem:
                raise ServiceStartError(problem)
            runner = self.runner_factory()
            listening = runner.start(ready_timeout=self.ready_timeout,
                                     on_wait=lambda: self.report(SERVICE_START_PENDING, wait_hint=30_000))
        except Exception:  # noqa: BLE001 - 任何启动错误都要报告给 SCM，交给恢复策略
            log.exception("云枢服务启动失败")
            return self._fail(EXIT_START_FAILED)
        if not listening:
            log.info("服务仍在启动（例如正在升级数据库结构），先报告已运行，就绪情况见 /api/ready")
        self.report(SERVICE_RUNNING)
        log.info("云枢服务已启动")
        while not self.stop_requested.wait(self.monitor_interval):
            if not runner.alive:
                log.error("服务线程意外退出，交给服务恢复策略重启")
                return self._fail(EXIT_SERVER_DIED)
        log.info("正在停止云枢服务")
        self.report(SERVICE_STOP_PENDING, wait_hint=15_000)
        clean = runner.stop(timeout=self.stop_timeout,
                            on_wait=lambda: self.report(SERVICE_STOP_PENDING, wait_hint=15_000))
        if not clean:
            return self._fail(EXIT_STOP_TIMEOUT)
        log.info("云枢服务已停止")
        self.report(SERVICE_STOPPED)
        return 0

    def handle_control(self, control: int) -> int:
        if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN, SERVICE_CONTROL_PRESHUTDOWN):
            if not self.stop_requested.is_set():
                self.report(SERVICE_STOP_PENDING, wait_hint=15_000)
                self.stop_requested.set()
            return NO_ERROR
        if control == SERVICE_CONTROL_INTERROGATE:
            return NO_ERROR
        return ERROR_CALL_NOT_IMPLEMENTED


def _bindings() -> Any:
    """advapi32 的 ctypes 声明，只在 Windows 上调用。"""
    import ctypes
    from ctypes import wintypes

    class SERVICE_STATUS(ctypes.Structure):  # noqa: N801 - 与 winsvc.h 同名
        _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                    ("dwWaitHint", wintypes.DWORD)]

    main_fn = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
    handler_fn = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)

    class SERVICE_TABLE_ENTRYW(ctypes.Structure):  # noqa: N801
        _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", main_fn)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRYW)]
    advapi32.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
    advapi32.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, handler_fn, wintypes.LPVOID]
    advapi32.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
    advapi32.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
    advapi32.SetServiceStatus.restype = wintypes.BOOL
    return ctypes, advapi32, SERVICE_STATUS, SERVICE_TABLE_ENTRYW, main_fn, handler_fn


class _WindowsScm:
    """把 YunshuService 接到真正的服务管理器上。"""

    def __init__(self) -> None:
        (self.ctypes, self.advapi32, self.Status, self.TableEntry,
         self.main_fn, self.handler_fn) = _bindings()
        self.handle = None
        self.service: YunshuService | None = None
        self.exit_code = 0
        # 回调对象必须一直有引用，否则会被回收
        self._main_cb = self.main_fn(self._service_main)
        self._handler_cb = self.handler_fn(self._handler)

    def set_status(self, state: int, *, accepted: int, exit_code: int, specific: int, checkpoint: int,
                   wait_hint: int) -> None:
        if not self.handle:
            return
        status = self.Status(SERVICE_WIN32_OWN_PROCESS, state, accepted, exit_code, specific, checkpoint, wait_hint)
        if not self.advapi32.SetServiceStatus(self.handle, self.ctypes.byref(status)):
            log.warning("SetServiceStatus 失败：%s", self.ctypes.get_last_error())

    def _handler(self, control: int, _event_type: int, _event_data: Any, _context: Any) -> int:
        return self.service.handle_control(control) if self.service else ERROR_CALL_NOT_IMPLEMENTED

    def _service_main(self, _argc: int, _argv: Any) -> None:
        self.handle = self.advapi32.RegisterServiceCtrlHandlerExW(SERVICE_NAME, self._handler_cb, None)
        if not self.handle:
            self.exit_code = EXIT_START_FAILED
            return
        self.service = YunshuService(self)
        self.exit_code = self.service.run()

    def dispatch(self) -> int:
        table = (self.TableEntry * 2)(self.TableEntry(SERVICE_NAME, self._main_cb), self.TableEntry(None, self.main_fn()))
        if not self.advapi32.StartServiceCtrlDispatcherW(table):
            return -self.ctypes.get_last_error() or -1
        return self.exit_code


def _quiet_std_streams() -> None:
    """服务进程没有控制台，sys.stdout / stderr 可能是 None；换成空设备，避免日志和 print 出错。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 - 进程结束才关闭


def run_foreground(*, service: YunshuService | None = None) -> int:
    """在控制台里跑与服务相同的启动 / 停止流程；Ctrl+C 或 Ctrl+Break 停止。

    信号处理函数只发出停止请求、不抛异常：在 Thread.join 里抛 KeyboardInterrupt 会让 Python 3.11
    误以为线程已结束，主线程提前退出而来不及释放 Worker 锁。
    """

    class _ConsoleScm:
        def set_status(self, state: int, **_: int) -> None:
            names = {SERVICE_START_PENDING: "启动中", SERVICE_RUNNING: "运行中",
                     SERVICE_STOP_PENDING: "停止中", SERVICE_STOPPED: "已停止"}
            log.info("服务状态：%s", names.get(state, state))

    svc = service or YunshuService(_ConsoleScm())
    result: dict[str, int] = {}

    def _request_stop(_sig: int, _frame: Any) -> None:
        svc.handle_control(SERVICE_CONTROL_STOP)

    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGBREAK"):
            if hasattr(signal, name):
                previous[getattr(signal, name)] = signal.signal(getattr(signal, name), _request_stop)
    worker = threading.Thread(target=lambda: result.setdefault("code", svc.run()), name="yunshu-service-main")
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.5)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return result.get("code", 1)


def service_command(args: list[str]) -> int:
    if any(a in ("-h", "--help") for a in args):
        print(USAGE)
        return 0
    if "--foreground" in args:
        return run_foreground()
    if sys.platform != "win32":
        print("服务模式只在 Windows 上可用", file=sys.stderr)
        return 2
    _quiet_std_streams()
    result = _WindowsScm().dispatch()
    if result < 0:
        if -result == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            print("这个命令只能由 Windows 服务管理器启动；在控制台里排查请用 yunshu service --foreground", file=sys.stderr)
        else:
            print(f"连接服务管理器失败（错误 {-result}）", file=sys.stderr)
        return 1
    return result


def apply_portable_defaults(environ: MutableMapping[str, str], exe_dir: Path | None) -> bool:
    """免安装 ZIP 里的 cli\\yunshu.exe：配置读旁边的标记文件，数据目录默认是桌面版的 <解压目录>\\data。

    这样即使本机也装了服务版，桌面版的维护命令也不会读到 %ProgramData%\\Yunshu 的配置和数据。
    主密钥沿用桌面版默认的 %APPDATA%\\yunshu\\master.key，不需要设置。
    """
    if exe_dir is None or not (exe_dir / PORTABLE_MARKER).is_file():
        return False
    from app import cli

    environ.setdefault("YUNSHU_CONFIG", str(exe_dir / PORTABLE_MARKER))
    cli.load_config(environ)
    environ.setdefault("YUNSHU_DATA_DIR", str(exe_dir.parent / "data"))
    return True


def utf8_redirected_streams(streams: tuple[Any, ...] | None = None) -> None:
    """输出被重定向到文件或管道时改用 UTF-8。

    英文版 Windows 的默认代码页（如 cp1252）写不出中文，`yunshu backup list > 文件` 会报 UnicodeEncodeError；
    直接在控制台窗口里运行时 Python 走控制台的 Unicode 接口，不受影响，保持不动。
    """
    for stream in streams if streams is not None else (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure") and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False):
        utf8_redirected_streams()
    if args[:1] == ["service"]:
        return service_command(args[1:])
    exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None
    apply_portable_defaults(os.environ, exe_dir)
    from app.cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
