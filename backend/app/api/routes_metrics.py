"""指标读取接口：最新值（带质量状态）与时间序列。"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import metrics
from app.scope import require_asset, scope_for
from app.api.deps import get_db, reader
from app.models import Asset, User

router = APIRouter(prefix="/api/assets", tags=["metrics"])


def _asset(db: Session, asset_id: uuid.UUID, user: User) -> None:
    require_asset(db, scope_for(db, user), asset_id)  # 范围外当作不存在（4.1 P11）
    if db.get(Asset, asset_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在或你无权查看")


@router.get("/{asset_id}/metrics")
def latest_metrics(asset_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    _asset(db, asset_id, user)
    items = [asdict(v) | {"observed_at": v.observed_at.isoformat(), "last_valid_at": v.last_valid_at.isoformat() if v.last_valid_at else None} for v in metrics.latest(db, asset_id)]
    return {"items": items}


@router.get("/{asset_id}/metrics/series")
def metric_series(
    asset_id: uuid.UUID,
    key: str = Query(..., max_length=200),
    start: datetime | None = None,
    end: datetime | None = None,
    resolution: str = Query("auto", pattern="^(auto|raw|minute|hour)$"),
    user: User = Depends(reader),
    db: Session = Depends(get_db),
) -> dict:
    _asset(db, asset_id, user)
    end = end or datetime.now(UTC)
    start = start or end - timedelta(hours=1)
    if start >= end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "开始时间必须早于结束时间")
    return {"key": key, **metrics.series(db, asset_id, key, start, end, resolution=resolution)}
