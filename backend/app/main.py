"""FastAPI 应用工厂。

遵守 “API 与 Worker 不在 import 时启动”：本模块只定义工厂函数，
不在模块级创建应用、连接数据库或拉起后台任务。启动方式：

    uvicorn app.main:create_app --factory

数据库连接、迁移、主密钥和初始管理员都在 lifespan 里完成；测试可传入现成的
engine 与 cipher，完全离线运行。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from app import __version__, migrate
from app.api import routes_admin, routes_assets, routes_auth, routes_backup, routes_collect, routes_dashboard, routes_import, routes_inventory, routes_metrics, routes_tasks
from app.auth import LoginThrottle
from app.api import routes_discovery, routes_events, routes_topology, routes_alerts, routes_asset_information, routes_control, routes_racks, routes_reports, routes_resources, routes_setup, routes_sso, routes_virtualization, routes_workers, routes_ai, routes_vuln, routes_assistant, routes_mcp, routes_server_events, routes_bmc_events, routes_command_inspection, routes_appearance
from app.alert_defaults import ensure_rules
from app.bootstrap import ensure_initial_admin
from app.crypto import CredentialCipher, load_or_create_keyring
from app.db import make_engine, make_session_factory
from app.settings import Settings

log = logging.getLogger(__name__)


def start_backup_schedule(engine: Engine, cfg: Settings) -> asyncio.Task[None] | None:
    """按 backup_interval_hours 定时在线备份；0 表示关闭，内存库不备份。PostgreSQL 用 pg_dump（4.1 P8）。"""
    from app.backup import BackupError, backup_database, database_kind, sqlite_path

    if cfg.backup_interval_hours <= 0:
        return None
    if database_kind(engine) == "sqlite":
        try:
            sqlite_path(engine)
        except BackupError:
            return None

    async def loop() -> None:
        while True:
            await asyncio.sleep(cfg.backup_interval_hours * 3600)
            try:
                await asyncio.to_thread(backup_database, engine, cfg.backup_dir, "scheduled")
            except Exception:  # noqa: BLE001 - 备份失败只记日志，不影响服务
                log.exception("定时备份失败")

    return asyncio.create_task(loop())


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    cipher: CredentialCipher | None = None,
) -> FastAPI:
    def initialize(app: FastAPI, cfg: Settings, eng: Engine) -> None:
        """连接好数据库之后的启动步骤；首次启动向导保存后也走这里（4.1 P12）。"""
        migrate.prepare(eng, auto=cfg.auto_migrate, backup_dir=cfg.backup_dir)
        app.state.settings = cfg
        app.state.engine = eng
        app.state.session_factory = make_session_factory(eng)
        app.state.cipher = cipher or CredentialCipher(load_or_create_keyring(cfg.master_key_file, data_dir=cfg.data_dir))
        app.state.login_throttle = LoginThrottle()
        with app.state.session_factory() as db:
            ensure_initial_admin(db)
            ensure_rules(db)
            from app.command_inspection import ensure_builtin

            ensure_builtin(db)  # 4.4 P21 内置巡检模板
            db.commit()
        app.state.scheduler = start_backup_schedule(eng, cfg)
        if cfg.embedded_worker:
            from app.tasks import build_handlers, build_schedules
            from app.worker import Worker, start_in_thread

            app.state.worker = Worker(app.state.session_factory, build_handlers(cfg), schedules=build_schedules(cfg))
            app.state.worker_thread = start_in_thread(app.state.worker)
            from app.snmp_trap import start_if_enabled

            start_if_enabled(app.state.session_factory, lambda: app.state.cipher)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or Settings.from_env()
        app.state.initialize = initialize
        app.state.worker = app.state.worker_thread = app.state.scheduler = app.state.engine = None
        app.state.setup = None
        if cfg.setup_pending and engine is None:
            from app.setup_wizard import SetupState

            app.state.settings = cfg
            app.state.setup = SetupState()
            log.warning("首次启动：尚未选择数据库，请打开页面完成设置。从本机以外访问时需要设置口令：%s", app.state.setup.token)
        else:
            initialize(app, cfg, engine or make_engine(cfg.database_url))
        try:
            yield
        finally:
            worker, worker_thread, scheduler = app.state.worker, app.state.worker_thread, app.state.scheduler
            if worker is not None:
                worker.stop()
                # 等待当前任务结束及单实例锁释放，再销毁引擎；桌面关闭后可立即重启。
                await asyncio.to_thread(worker_thread.join, worker.timeout_seconds + 5)
            if scheduler is not None:
                scheduler.cancel()
                with suppress(asyncio.CancelledError):
                    await scheduler
            if engine is None and app.state.engine is not None:
                app.state.engine.dispose()

    app = FastAPI(title="云枢运维平台", version=__version__, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc: RequestValidationError) -> JSONResponse:
        # 校验错误的 input / ctx 可能含嵌套密码，接口只返回字段位置和错误说明。
        return JSONResponse(status_code=422, content={"detail": [
            {k: error[k] for k in ("type", "loc", "msg")} for error in exc.errors()
        ]})

    @app.middleware("http")
    async def setup_gate(request: Request, call_next):  # type: ignore[no-untyped-def]
        # 首次启动向导未完成时，除健康检查和向导接口外的接口都不可用
        path = request.url.path
        if getattr(app.state, "setup", None) is not None and (path.startswith("/api/") or path in ("/metrics", "/mcp")) and not path.startswith(("/api/setup", "/api/health")):
            return JSONResponse(status_code=503, content={"detail": "首次设置尚未完成，请先在页面上选择数据库", "setup": True})
        return await call_next(request)

    @app.get("/api/health")
    def health() -> dict[str, object]:
        if getattr(app.state, "setup", None) is not None:
            return {"status": "ok", "version": __version__, "setup": True}
        return {"status": "ok", "version": __version__}

    @app.get("/api/ready")
    def ready() -> JSONResponse:
        # 就绪：数据库可用、结构版本一致、内嵌 Worker 在运行（见 app/health.py）。首次设置未完成时由上面的中间件返回 503
        from app.health import readiness

        ok, body = readiness(app.state.engine, app.state.worker_thread, embedded=app.state.settings.embedded_worker)
        return JSONResponse(status_code=200 if ok else 503, content={**body, "version": __version__})

    for module in (routes_auth, routes_admin, routes_assets, routes_inventory, routes_import, routes_backup, routes_tasks, routes_metrics, routes_collect, routes_discovery, routes_dashboard, routes_events, routes_topology, routes_alerts, routes_asset_information, routes_control):
        app.include_router(module.router)
    app.include_router(routes_resources.router)
    app.include_router(routes_virtualization.router)
    app.include_router(routes_workers.router)
    app.include_router(routes_setup.router)
    app.include_router(routes_racks.router)
    app.include_router(routes_reports.router)
    app.include_router(routes_sso.router)
    app.include_router(routes_ai.router)
    app.include_router(routes_vuln.router)
    app.include_router(routes_assistant.router)
    app.include_router(routes_mcp.router)
    app.include_router(routes_server_events.router)
    app.include_router(routes_bmc_events.router)
    app.include_router(routes_command_inspection.router)
    app.include_router(routes_appearance.router)
    mount_frontend(app, settings or Settings.from_env())
    return app


def mount_frontend(app: FastAPI, cfg: Settings) -> None:
    """有构建好的前端时由 API 一并提供（Docker 镜像）。前端用 hash 路由，只需要 index.html 和静态资源。
    必须在所有 /api 路由之后挂载，接口优先。"""
    if cfg.frontend_dir is None:
        return
    from fastapi.staticfiles import StaticFiles

    index = cfg.frontend_dir / "index.html"
    if not index.is_file():
        log.warning("YUNSHU_FRONTEND_DIR=%s 里没有 index.html，不提供页面", cfg.frontend_dir)
        return
    app.mount("/", StaticFiles(directory=cfg.frontend_dir, html=True), name="frontend")
