"""备份接口（仅管理员）：列出、立即备份、下载。恢复必须停服后用命令行执行，不提供接口。"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from app import audit
from app.api.deps import admin_only, get_db
from app.backup import BackupError, backup_database, database_kind, list_backups
from app.models import User

router = APIRouter(prefix="/api/backups", tags=["backup"])

RESTORE_HINT = "恢复需要先停止服务，再在 backend 目录执行：python -m app.backup restore <备份文件名> --yes"
KEY_HINT = "备份不含主密钥。请在 backend 目录运行 python -m app.keytool export <文件> 单独备份密钥文件，并与数据库备份分开保存。"


@router.get("")
def get_backups(request: Request, _: User = Depends(admin_only)) -> dict:
    settings = request.app.state.settings
    return {
        "items": [asdict(b) for b in list_backups(settings.backup_dir)],
        # 管理页据此区分本地文件库（SQLite）与外部数据库（PostgreSQL，pg_dump）
        "database": database_kind(settings.database_url),
        "interval_hours": settings.backup_interval_hours,
        "restore_hint": RESTORE_HINT,
        "key_hint": KEY_HINT,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def backup_now(request: Request, actor: User = Depends(admin_only), db=Depends(get_db)) -> dict:  # type: ignore[no-untyped-def]
    settings = request.app.state.settings
    try:
        info = backup_database(settings.database_url, settings.backup_dir, "manual")
    except BackupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    audit.record(db, actor, "backup.create", target_type="backup", target_id=info.name, summary="手动备份数据库")
    return asdict(info)


@router.get("/{name}")
def download_backup(name: str, request: Request, actor: User = Depends(admin_only), db=Depends(get_db)) -> FileResponse:  # type: ignore[no-untyped-def]
    settings = request.app.state.settings
    known = {b.name for b in list_backups(settings.backup_dir)}
    if name not in known:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "备份不存在")
    audit.record(db, actor, "backup.download", target_type="backup", target_id=name, summary="下载数据库备份")
    db.commit()
    return FileResponse(settings.backup_dir / name, filename=name, media_type="application/octet-stream")
