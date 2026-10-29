"""接收 BMC 推送的 Redfish 事件（4.4 P20c）。

不需要登录：BMC 不能登录平台，凭订阅地址里的随机令牌认出是哪台资产（库里只存令牌哈希，认不出一律 404）。
只把事件写进“服务器告警”，不触发任何设备操作。平台在 BMC 上订阅时才会生成令牌，见“开启 SNMP”里的“订阅 Redfish 事件”。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import server_events
from app.api.deps import get_db
from app.models import Asset, BmcEventSubscription

router = APIRouter(prefix="/api/bmc-events", tags=["bmc-events"])
SOURCE = "redfish_event"
MAX_BODY = 256 * 1024
MAX_EVENTS = 100


def to_entries(body: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Redfish Event 资源 → server_events.record 的条目。没有时间的用收到时间。"""
    out = []
    for e in (body.get("Events") or [])[:MAX_EVENTS]:
        if not isinstance(e, dict):
            continue
        message = str(e.get("Message") or e.get("MessageId") or "").strip()
        if not message:
            continue
        origin = (e.get("OriginOfCondition") or {}).get("@odata.id", "") if isinstance(e.get("OriginOfCondition"), dict) else ""
        ident = str(e.get("EventId") or e.get("MemberId") or e.get("MessageId") or "")
        out.append({"id": f"{ident}|{e.get('MessageId') or ''}"[:100], "created": e.get("EventTimestamp") or now,
                    "severity": e.get("MessageSeverity") or e.get("Severity") or "", "message": message + (f"（{origin}）" if origin else "")})
    return out


@router.post("/{token}")
async def receive(token: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    digest = hashlib.sha256(token.encode()).hexdigest()
    sub = db.scalar(select(BmcEventSubscription).where(BmcEventSubscription.kind == SOURCE, BmcEventSubscription.token_hash == digest))
    asset = db.get(Asset, sub.asset_id) if sub is not None else None
    if sub is None or asset is None or asset.deleted_at is not None:  # 回收站里的资产不再接收事件
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "内容过大")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "不是 JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "格式不正确")
    now = datetime.now(UTC)
    entries = to_entries(body, now)
    added = server_events.record(db, asset.id, SOURCE, entries, now)
    if added and any(server_events.severity_of(e.get("severity")) in ("critical", "warning") for e in entries):
        from app.collect import boost

        boost.on_event(db, asset.id, SOURCE, now)  # 4.4 P20d：立即补采并临时提频
    sub.last_event_at, sub.events_received = now, sub.events_received + 1
    return {"accepted": added}
