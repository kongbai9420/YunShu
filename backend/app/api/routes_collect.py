"""采集：模板列表、资产的采集状态、立即采集、重新信任端点指纹。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import audit, queue
from app.api.deps import editor, get_db, reader
from app.collect import boost, plan as plans
from app.collect.base import PROFILES
from app.collect.runner import resolve_profile, retry_after
from app.models import Asset, CollectionBoost, CollectionPlan, CollectionStatus, ManagementEndpoint, Task, User

router = APIRouter(prefix="/api", tags=["collect"])


@router.get("/collection/profiles")
def profiles(_: User = Depends(reader)) -> list[dict]:
    return [
        asdict(p)
        for p in PROFILES.values()
    ]


def _asset(db: Session, asset_id: uuid.UUID, user: User) -> Asset:
    from app.scope import require_asset, scope_for

    require_asset(db, scope_for(db, user), asset_id)  # 范围外当作不存在（4.1 P11）
    a = db.get(Asset, asset_id, options=[selectinload(Asset.endpoints)])
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在或你无权查看")
    return a


@router.get("/assets/{asset_id}/collection")
def asset_collection(asset_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    a = _asset(db, asset_id, user)
    p = resolve_profile(a)
    primary_protocols = {s.protocol for s in p.steps if s.mode == "primary"} if p else set()
    rows = db.scalars(select(CollectionStatus).where(CollectionStatus.asset_id == asset_id)).all()
    last = db.scalars(select(Task).where(Task.asset_id == asset_id, Task.kind == "collect.asset").order_by(Task.created_at.desc()).limit(1)).first()
    return {
        "profile": {"name": p.name, "label": p.label, "verified": p.verified, "inferred": not a.collection_profile} if p else None,
        "endpoints": [
            {"endpoint_id": str(r.endpoint_id), "protocol": r.protocol, "state": r.state, "error_kind": r.error_kind,
             "message": r.message, "capabilities": r.capabilities, "facts": r.facts,
             "last_attempt_at": r.last_attempt_at.isoformat(), "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
             "retry_after": next_try.isoformat() if r.protocol in primary_protocols and (next_try := retry_after(r, p.interval if p else 60)) and next_try > datetime.now(UTC) else None,
             "consecutive_failures": r.consecutive_failures, "duration_ms": r.duration_ms}
            for r in rows
        ],
        "last_task": {"status": last.status, "result": last.result, "error": last.error,
                      "finished_at": last.finished_at.isoformat() if last.finished_at else None} if last else None,
        # 4.4 P20a：服务器 BMC 有两个以上协议时的采集计划；不适用时为 null
        "plan": plans.describe(db.get(CollectionPlan, asset_id)) if p and plans.applies(a, p.name) else None,
        # 4.4 P20d：临时提频（告警、接近阈值或收到推送事件）；没有时为 null
        "boost": boost.describe(db.get(CollectionBoost, asset_id), datetime.now(UTC)) if p and p.interval > boost.BOOST_INTERVAL else None,
    }


@router.post("/assets/{asset_id}/collection-plan/reprobe", status_code=status.HTTP_202_ACCEPTED)
def reprobe_plan(asset_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    """重新探测各协议能读到哪些数据：下次采集时执行，这里同时排一次立即采集。只读，不改设备。"""
    a = _asset(db, asset_id, actor)
    p = resolve_profile(a)
    if p is None or not plans.applies(a, p.name):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "只有同时配置了两个以上 BMC 协议（SNMP / Redfish / IPMI）的服务器才有采集计划")
    row = db.get(CollectionPlan, asset_id)
    if row is not None:
        row.reprobe = True
    t = queue.enqueue(db, "collect.asset", asset_id=a.id, payload={"asset_id": str(a.id), "force": True}, priority=10, max_attempts=1)
    audit.record(db, actor, "collection.reprobe", target_type="asset", target_id=a.id, summary="请求重新探测采集计划（只读）")
    return {"task_id": str(t.id)}


@router.post("/assets/{asset_id}/collect", status_code=status.HTTP_202_ACCEPTED)
def collect_now(asset_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    a = _asset(db, asset_id, actor)
    if resolve_profile(a) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "该资产没有可用的采集模板：请添加管理端点或指定采集模板")
    t = queue.enqueue(db, "collect.asset", asset_id=a.id, payload={"asset_id": str(a.id), "force": True}, priority=10, max_attempts=1)
    audit.record(db, actor, "collection.enqueue", target_type="asset", target_id=a.id, summary="请求立即只读采集")
    return {"task_id": str(t.id)}


@router.post("/endpoints/{endpoint_id}/trust/reset", status_code=status.HTTP_204_NO_CONTENT)
def reset_trust(endpoint_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> None:
    from app.scope import can_see, scope_for

    ep = db.get(ManagementEndpoint, endpoint_id)
    if ep is None or not can_see(db, scope_for(db, actor), ep.asset_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "管理端点不存在")
    old = {"tls": ep.tls_fingerprint, "ssh": ep.ssh_host_key[:64]}
    ep.tls_fingerprint, ep.ssh_host_key, ep.trust_pinned_at = "", "", None
    previous = db.get(CollectionStatus, endpoint_id)
    if previous is not None:
        previous.state, previous.error_kind, previous.consecutive_failures = "skipped", "", 0
        previous.message = "固定指纹已清除，等待重新采集"
    audit.record(db, actor, "endpoint.trust_reset", target_type="asset", target_id=ep.asset_id, summary="清除固定的证书指纹 / 主机密钥，下次连接重新信任", detail=old)
