"""首次启动向导接口（4.1 P12）。只在设置模式下可用，规则见 app/setup_wizard.py。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from fastapi import APIRouter, HTTPException, Request

from app.setup_wizard import DatabaseChoice, build_url, check_url, save_url

router = APIRouter(prefix="/api/setup", tags=["setup"])


def _state(request: Request, token: str = ""):  # type: ignore[no-untyped-def]
    state = getattr(request.app.state, "setup", None)
    if state is None:
        raise HTTPException(409, "已完成首次设置")
    if not state.allowed(request.client.host if request.client else None, token):
        raise HTTPException(403, "请填写启动日志里的设置口令")
    return state


@router.get("/status")
def status(request: Request) -> dict[str, object]:
    state = getattr(request.app.state, "setup", None)
    if state is None:
        return {"pending": False}
    local = state.allowed(request.client.host if request.client else None, "")
    return {"pending": True, "token_required": not local}


@router.post("/test")
async def test(choice: DatabaseChoice, request: Request) -> dict[str, object]:
    _state(request, choice.token)
    try:
        url = build_url(choice, request.app.state.settings.data_dir)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return (await asyncio.to_thread(check_url, url)).public()


@router.post("/apply")
async def apply(choice: DatabaseChoice, request: Request) -> dict[str, object]:
    state = _state(request, choice.token)
    cfg = request.app.state.settings
    try:
        url = build_url(choice, cfg.data_dir)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not state.lock.acquire(blocking=False):
        raise HTTPException(409, "正在保存，请稍候")
    try:
        result = await asyncio.to_thread(check_url, url)
        if not result.ok:
            raise HTTPException(400, result.message)
        engine = await asyncio.to_thread(_prepare, cfg, url)
        try:
            # 初始化要在事件循环线程里做：定时备份是 asyncio 任务
            request.app.state.initialize(request.app, replace(cfg, database_url=url, setup_pending=False), engine)
        except Exception:
            engine.dispose()
            raise
        request.app.state.setup = None
    finally:
        state.lock.release()
    return {"ok": True, "kind": choice.kind, "message": "已保存，平台已就绪。首次登录 admin / admin123，按提示修改密码。"}


def _prepare(cfg, url: str):  # type: ignore[no-untyped-def]
    from app import migrate
    from app.db import make_engine

    engine = make_engine(url)
    try:
        # 用户在向导里明确选择了这个库：空库直接建表，本平台的旧库升级到当前版本（升级前自动备份）
        migrate.upgrade(engine, backup_dir=cfg.backup_dir)
        save_url(cfg.master_key_file, url)
    except Exception:
        engine.dispose()
        raise
    return engine
