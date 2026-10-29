"""巡检报告、只读 API 令牌与 Prometheus 指标导出（4.2 P16）。"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, inspection, metrics
from app.api.deps import editor, get_db, reader
from app.models import ApiToken, Asset, InspectionReport, User
from app.scope import Scope, scope_for

router = APIRouter(tags=["reports"])
TOKEN_PREFIX = "ys_"
MAX_TOKENS_PER_USER = 10


def _visible(db: Session, scope: Scope) -> set[uuid.UUID] | None:
    return None if scope.all else set(db.scalars(scope.assets()))


# ---- 巡检报告 ----


@router.get("/api/inspections")
def list_reports(limit: int = Query(30, ge=1, le=200), user: User = Depends(reader), db: Session = Depends(get_db)) -> list[dict]:
    visible = _visible(db, scope_for(db, user))
    out = []
    for r in db.scalars(select(InspectionReport).order_by(InspectionReport.created_at.desc()).limit(limit)):
        v = inspection.view(r, visible)
        v.pop("items")
        out.append(v)
    return out


@router.post("/api/inspections", status_code=status.HTTP_201_CREATED)
def run_report(actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    report = inspection.generate(db, kind="manual", by=actor.username)
    audit.record(db, actor, "inspection.run", target_type="inspection", target_id=report.id, summary="手动生成巡检报告")
    return inspection.view(report, _visible(db, scope_for(db, actor)))


def _report(db: Session, report_id: uuid.UUID) -> InspectionReport:
    report = db.get(InspectionReport, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "报告不存在")
    return report


@router.get("/api/inspections/{report_id}")
def get_report(report_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    return inspection.view(_report(db, report_id), _visible(db, scope_for(db, user)))


@router.get("/api/inspections/{report_id}/export.csv")
def export_report(report_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> Response:
    report = _report(db, report_id)
    data = inspection.view(report, _visible(db, scope_for(db, user)))
    stamp = report.created_at.astimezone(inspection._local_tz()).strftime("%Y%m%d-%H%M")
    return Response(inspection.to_csv(data), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="inspection-{stamp}.csv"'})


# ---- 只读 API 令牌 ----


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@router.get("/api/tokens")
def list_tokens(user: User = Depends(reader), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at)).all()
    return [{"id": str(t.id), "name": t.name, "purpose": t.purpose, "created_at": t.created_at, "last_used_at": t.last_used_at} for t in rows]


@router.post("/api/tokens", status_code=status.HTTP_201_CREATED)
def create_token(body: TokenIn, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    if len(db.scalars(select(ApiToken.id).where(ApiToken.user_id == user.id)).all()) >= MAX_TOKENS_PER_USER:
        raise HTTPException(status.HTTP_409_CONFLICT, f"每个账号最多 {MAX_TOKENS_PER_USER} 个令牌，请先删除不用的")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(user_id=user.id, name=body.name.strip(), token_hash=_hash(token), purpose="metrics")
    db.add(row)
    db.flush()
    audit.record(db, user, "token.create", target_type="api_token", target_id=row.id, summary=f"新建只读令牌 {row.name}")
    # 令牌只在创建时返回一次，数据库只存哈希
    return {"id": str(row.id), "name": row.name, "token": token}


@router.delete("/api/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_token(token_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> None:
    row = db.get(ApiToken, token_id)
    if row is None or (row.user_id != user.id and user.role != "admin"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "令牌不存在")
    audit.record(db, user, "token.delete", target_type="api_token", target_id=row.id, summary=f"删除只读令牌 {row.name}")
    db.delete(row)


# ---- Prometheus 指标导出 ----


def _name(key: str) -> str:
    return "yunshu_" + re.sub(r"[^a-zA-Z0-9_]", "_", key.split(":", 1)[0])


def _label(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def export_metrics(authorization: str = Header(""), db: Session = Depends(get_db)) -> PlainTextResponse:
    """Prometheus 文本格式。用“Authorization: Bearer 令牌”访问；只导出令牌所属账号可见资产的有效数值，过期或失败的不导出。"""
    raw = authorization.removeprefix("Bearer ").strip()
    token = db.scalar(select(ApiToken).where(ApiToken.token_hash == _hash(raw))) if raw.startswith(TOKEN_PREFIX) else None
    user = db.get(User, token.user_id) if token else None
    if token is None or user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效", headers={"WWW-Authenticate": "Bearer"})
    now = datetime.now(UTC)
    if token.last_used_at is None or (now - metrics._aware(token.last_used_at)).total_seconds() > 300:
        token.last_used_at = now  # 最多 5 分钟写一次，抓取频繁时不产生写入压力
    scope = scope_for(db, user)
    stmt = select(Asset).where(Asset.lifecycle == "active")
    if not scope.all:
        stmt = stmt.where(Asset.id.in_(scope.assets()))
    assets = {a.id: a for a in db.scalars(stmt)}
    lines: dict[str, list[str]] = {}
    for aid, views in metrics.latest_many(db, list(assets), now=now).items():
        a = assets[aid]
        for v in views:
            if v.value is None or v.quality not in metrics.NUMERIC_QUALITIES:
                continue
            item = v.key.split(":", 1)[1] if ":" in v.key else ""
            labels = f'asset="{_label(a.name)}",asset_id="{aid}",item="{_label(item)}",unit="{_label(v.unit)}"'
            lines.setdefault(_name(v.key), []).append(f"{_name(v.key)}{{{labels}}} {v.value:g} {int(v.observed_at.timestamp() * 1000)}")
    body = []
    for name in sorted(lines):
        body.append(f"# TYPE {name} gauge")
        body.extend(lines[name])
    return PlainTextResponse("\n".join(body) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")



# ---- 4.3 P18f：AI 巡检摘要 ----


@router.get("/api/inspections/{report_id}/summary")
def get_summary(report_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    from app import ai_notes, ai_runtime
    from app.scope import require_all

    require_all(scope_for(db, user), "AI 巡检摘要")
    enabled = ai_runtime.feature_enabled(ai_runtime.settings_row(db), ai_notes.INSPECTION_FEATURE)
    return {"enabled": enabled, "note": ai_notes.out(ai_notes.latest(db, "inspection", str(report_id)))}


@router.post("/api/inspections/{report_id}/summary")
def run_summary(report_id: uuid.UUID, request: Request, force: bool = False, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    from app import ai_notes
    from app.ai.protocols import AiError
    from app.scope import require_all

    require_all(scope_for(db, actor), "AI 巡检摘要")
    try:
        row = ai_notes.summarize_inspection(db, request.app.state.cipher, report_id, user_name=actor.username, force=force,
                                            client=getattr(request.app.state, "ai_http_client", None))
    except AiError as exc:
        db.commit()
        raise HTTPException(422 if exc.kind in ("disabled", "quota", "not_found") else 502, {"kind": exc.kind, "message": str(exc)}) from None
    audit.record(db, actor, "inspection.ai.summary", target_type="inspection", target_id=report_id, summary=f"AI 巡检摘要（{row.model}）")
    return {"enabled": True, "note": ai_notes.out(row)}
