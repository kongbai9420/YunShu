"""模板批量导入接口：下载模板、上传预览、提交、撤销、批次与结果。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import audit, template_import
from app.api.deps import editor, get_db, reader
from app.scope import require_all, scope_for
from app.models import ImportBatch, User
from app.template_import import TEMPLATES, ImportFileError

router = APIRouter(prefix="/api/import", tags=["import"])


class TemplateInfo(BaseModel):
    kind: str
    label: str
    headers: list[str]


class ImportItemOut(BaseModel):
    row_no: int
    asset_no: str
    name: str
    action: str
    messages: list[str]
    asset_id: uuid.UUID | None
    undo_result: str


class BatchOut(BaseModel):
    id: uuid.UUID
    source: str
    template_kind: str
    filename: str
    status: str
    created_at: datetime
    created_by: str
    applied_at: datetime | None
    undone_at: datetime | None
    summary: dict[str, Any]


class BatchDetail(BatchOut):
    items: list[ImportItemOut]


def _batch(db: Session, batch_id: uuid.UUID) -> ImportBatch:
    batch = db.get(ImportBatch, batch_id, options=[selectinload(ImportBatch.items)])
    if batch is None or batch.source != "template":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "导入批次不存在")
    return batch


def _detail(batch: ImportBatch) -> BatchDetail:
    return BatchDetail(
        **BatchOut.model_validate(batch, from_attributes=True).model_dump(),
        items=[ImportItemOut.model_validate(i, from_attributes=True) for i in batch.items],
    )


@router.get("/templates", response_model=list[TemplateInfo])
def list_templates(_: User = Depends(reader)) -> list[TemplateInfo]:
    return [TemplateInfo(kind=k, label=t.label, headers=template_import.template_headers(k)) for k, t in TEMPLATES.items()]


@router.get("/templates/{kind}.{fmt}")
def download_template(kind: str, fmt: str, _: User = Depends(reader)) -> Response:
    if kind not in TEMPLATES or fmt not in ("csv", "xlsx"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有这个模板")
    label = TEMPLATES[kind].label
    if fmt == "csv":
        body, media = template_import.template_csv(kind), "text/csv; charset=utf-8"
    else:
        body, media = template_import.template_xlsx(kind), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    from urllib.parse import quote

    filename = quote(f"资产导入模板-{label}.{fmt}")
    return Response(body, media_type=media, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})


@router.post("/preview", response_model=BatchDetail, status_code=status.HTTP_201_CREATED)
async def preview(
    file: UploadFile = File(...),
    kind: str = Form(...),
    mapping: str | None = Form(None),
    actor: User = Depends(editor),
    db: Session = Depends(get_db),
) -> BatchDetail:
    require_all(scope_for(db, actor), "批量导入")
    if kind not in TEMPLATES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "模板类型不正确")
    content = await file.read(template_import.MAX_FILE_BYTES + 1)
    custom: dict[str, str | None] | None = None
    if mapping:
        try:
            custom = json.loads(mapping)
            if not isinstance(custom, dict):
                raise ValueError
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "字段映射格式不正确") from exc
        unknown = {v for v in custom.values() if v and v not in template_import.COLUMNS}
        if unknown:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"未知字段：{'、'.join(sorted(unknown))}")
    try:
        parsed = template_import.parse_file(file.filename or "", content)
        used_mapping, plans = template_import.build_preview(db, parsed, kind, custom)
    except ImportFileError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    batch = template_import.save_preview(db, kind, file.filename or "", actor.username, used_mapping, plans)
    return _detail(batch)


@router.post("/{batch_id}/commit", response_model=BatchDetail)
def commit(batch_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> BatchDetail:
    require_all(scope_for(db, actor), "批量导入")
    batch = _batch(db, batch_id)
    try:
        result = template_import.commit_batch(db, batch)
    except ImportFileError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    audit.record(db, actor, "import.commit", target_type="import_batch", target_id=batch.id, summary=f"模板导入 {batch.filename}", detail=result)
    return _detail(batch)


@router.post("/{batch_id}/undo", response_model=BatchDetail)
def undo(batch_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> BatchDetail:
    require_all(scope_for(db, actor), "批量导入")
    batch = _batch(db, batch_id)
    try:
        result = template_import.undo_batch(db, batch)
    except ImportFileError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    audit.record(db, actor, "import.undo", target_type="import_batch", target_id=batch.id, summary=f"撤销模板导入 {batch.filename}", detail=result)
    return _detail(batch)


@router.get("/batches", response_model=dict)
def list_batches(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(reader),
    db: Session = Depends(get_db),
) -> dict:
    require_all(scope_for(db, user), "批量导入")
    stmt = select(ImportBatch).where(ImportBatch.source == "template", ImportBatch.status != "preview")
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(ImportBatch.created_at.desc()).limit(limit).offset(offset)).all()
    return {"items": [BatchOut.model_validate(b, from_attributes=True).model_dump(mode="json") for b in rows], "total": total}


@router.get("/{batch_id}", response_model=BatchDetail)
def get_batch(batch_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> BatchDetail:
    require_all(scope_for(db, user), "批量导入")
    return _detail(_batch(db, batch_id))


@router.get("/{batch_id}/results.csv")
def batch_results(batch_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> Response:
    require_all(scope_for(db, user), "批量导入")
    batch = _batch(db, batch_id)
    return Response(
        template_import.results_csv(batch),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=import-{batch.id}.csv"},
    )
