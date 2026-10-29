from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import virtualization
from app.api.deps import editor, get_db, reader
from app.scope import require_all, scope_for
from app.models import User, VirtualizationObservation as Observation, VirtualizationSource as Source
from app.schemas import Name

router = APIRouter(prefix="/api/virtualization", tags=["virtualization"])


def _source(db: Session, source_id: uuid.UUID) -> Source:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "虚拟化来源不存在")
    return source


@router.get("/sources")
def sources(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    require_all(scope_for(db, user), "虚拟资源发现与关联")
    total = db.scalar(select(func.count()).select_from(Source)) or 0
    rows = db.scalars(select(Source).order_by(Source.id).limit(limit).offset(offset)).all()
    return {"items": [virtualization.source_out(db, s) for s in rows], "total": total}


@router.get("/sources/{source_id}/observations")
def observations(source_id: uuid.UUID, kind: Literal["host", "vm", "container"] | None = None,
                 q: str = Query("", max_length=100), limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0),
                 user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    require_all(scope_for(db, user), "虚拟资源发现与关联")
    source = _source(db, source_id)
    stmt = select(Observation).where(Observation.source_id == source.id)
    if kind:
        stmt = stmt.where(Observation.kind == kind)
    if q:
        stmt = stmt.where(or_(Observation.name.ilike(f"%{q}%"), Observation.external_id.ilike(f"%{q}%")))
    return virtualization.observation_page(db, source, stmt, limit, offset)


class Decision(BaseModel):
    action: Literal["create", "link", "ignore", "unlink"]
    revision: int = Field(ge=1)
    asset_id: uuid.UUID | None = None
    name: Name = "发现资源"


class Confirm(Decision):
    preview_token: str = Field(min_length=64, max_length=64)


@router.post("/observations/{observation_id}/preview")
def preview(observation_id: uuid.UUID, body: Decision, user: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    require_all(scope_for(db, user), "虚拟资源发现与关联")
    return virtualization.plan(db, observation_id, body.model_dump())[0]


@router.post("/observations/{observation_id}/confirm")
def confirm(observation_id: uuid.UUID, body: Confirm, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    require_all(scope_for(db, actor), "虚拟资源发现与关联")
    try:
        return virtualization.confirm(db, observation_id, body.model_dump(), actor)
    except IntegrityError as exc:
        raise HTTPException(409, "资产或关系已变化，操作已整体回滚，请刷新后重新预览") from exc
