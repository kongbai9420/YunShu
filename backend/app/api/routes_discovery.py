"""发现任务与候选确认。范围预览不联网，只有显式开始才入队。"""
from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, recycle
from app.api.deps import editor, get_db, reader
from app.scope import require_all, scope_for
from app.discovery import enqueue_batch, expand_scope
from app.models import Asset, Credential, DiscoveryObservation, DiscoveryRun, ManagementEndpoint, Site, Task, User
from app.schemas import AssetRole, Form, Name, Protocol, Short

router = APIRouter(prefix="/api/discovery", tags=["discovery"])
ScopeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class DiscoveryIn(BaseModel):
    scope: list[ScopeText] = Field(min_length=1, max_length=32)
    excludes: list[ScopeText] = Field(default_factory=list, max_length=32)
    protocol: Literal["tcp", "snmp"] = "tcp"
    ports: list[int] = Field(default_factory=lambda: [22, 443, 8006], min_length=1, max_length=4)
    credential_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    concurrency: int = Field(8, ge=1, le=16)
    timeout: float = Field(1, ge=0.2, le=2)

    @model_validator(mode="after")
    def validate_scope(self):
        if any(not 1 <= p <= 65535 for p in self.ports) or len(set(self.ports)) != len(self.ports):
            raise ValueError("端口必须是 1–65535 的非重复整数")
        if self.protocol == "snmp" and (self.credential_id is None or len(self.ports) != 1):
            raise ValueError("SNMP 发现需要选择凭据和一个 UDP 端口")
        expand_scope(self.scope, self.excludes)
        return self


def _run(db: Session, run_id: uuid.UUID) -> DiscoveryRun:
    run = db.get(DiscoveryRun, run_id)
    if run is None:
        raise HTTPException(404, "发现任务不存在")
    return run


def _out(db: Session, run: DiscoveryRun) -> dict:
    last = db.scalars(select(Task).where(
        Task.idempotency_key.like(f"discovery:{run.id}:{run.generation}:%"),
    ).order_by(Task.created_at.desc()).limit(1)).first()
    failed = run.state in ("queued", "running") and last is not None and last.status == "failed"
    return {
        "id": str(run.id), "created_at": run.created_at.isoformat(), "created_by": run.created_by,
        "scope": run.scope, "excludes": run.excludes, "protocol": run.protocol, "ports": run.ports,
        "site_id": str(run.site_id) if run.site_id else None, "concurrency": run.concurrency,
        "timeout": run.timeout, "state": "failed" if failed else run.state,
        "generation": run.generation, "total": len(run.addresses), "processed": run.processed,
        "responded": run.responded, "error": "发现任务失败，可检查配置后继续" if failed else run.error,
    }


@router.post("/preview")
def preview(body: DiscoveryIn, user: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    require_all(scope_for(db, user), "网段发现")
    addresses = expand_scope(body.scope, body.excludes)
    return {"total": len(addresses), "addresses": addresses, "protocol": body.protocol, "ports": body.ports}


@router.post("/runs", status_code=202)
def create_run(body: DiscoveryIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    require_all(scope_for(db, actor), "网段发现")
    if body.site_id and db.get(Site, body.site_id) is None:
        raise HTTPException(422, "场所不存在")
    if body.protocol == "snmp":
        cred = db.get(Credential, body.credential_id)
        if cred is None or cred.kind not in ("snmp_community", "snmpv3"):
            raise HTTPException(422, "请选择 SNMP v2c/v3 凭据")
    data = body.model_dump()
    if body.protocol == "tcp":
        data["credential_id"] = None  # TCP 只连端口，不发送凭据
    run = DiscoveryRun(**data, addresses=expand_scope(body.scope, body.excludes), created_by=actor.username)
    db.add(run)
    db.flush()
    enqueue_batch(db, run)
    audit.record(db, actor, "discovery.start", target_type="discovery", target_id=run.id,
                 summary=f"开始只读发现，共 {len(run.addresses)} 个地址", detail=body.model_dump(mode="json"))
    return _out(db, run)


@router.get("/runs")
def list_runs(limit: int = Query(20, ge=1, le=100), user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    require_all(scope_for(db, user), "网段发现")
    runs = db.scalars(select(DiscoveryRun).order_by(DiscoveryRun.created_at.desc()).limit(limit)).all()
    return {"items": [_out(db, r) for r in runs], "total": db.scalar(select(func.count()).select_from(DiscoveryRun))}


class TransitionIn(BaseModel):
    action: Literal["pause", "resume", "cancel"]
    generation: int


@router.post("/runs/{run_id}/state")
def transition(run_id: uuid.UUID, body: TransitionIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    require_all(scope_for(db, actor), "网段发现")
    run = _run(db, run_id)
    state = _out(db, run)["state"]
    allowed = {"pause": ("queued", "running"), "resume": ("paused", "failed"), "cancel": ("queued", "running", "paused", "failed")}
    if state not in allowed[body.action]:
        raise HTTPException(409, "当前状态不支持此操作，请刷新")
    target = {"pause": "paused", "resume": "queued", "cancel": "cancelled"}[body.action]
    changed = db.execute(update(DiscoveryRun).where(
        DiscoveryRun.id == run_id, DiscoveryRun.generation == body.generation,
        DiscoveryRun.state == run.state,
    ).values(state=target, generation=body.generation + 1))
    if changed.rowcount != 1:
        raise HTTPException(409, "发现任务已变化，请刷新后重试")
    db.refresh(run)
    if body.action == "resume":
        enqueue_batch(db, run)
    audit.record(db, actor, f"discovery.{body.action}", target_type="discovery", target_id=run.id, summary=f"发现任务：{target}")
    return _out(db, run)


@router.get("/runs/{run_id}")
def get_run(run_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    require_all(scope_for(db, user), "网段发现")
    run = _run(db, run_id)
    observations = db.scalars(select(DiscoveryObservation).where(DiscoveryObservation.run_id == run_id).order_by(DiscoveryObservation.address)).all()
    items = []
    for o in observations:
        matches = db.execute(select(Asset.id, Asset.name, Asset.site_id).join(ManagementEndpoint)
                             .where(ManagementEndpoint.address == o.address, Asset.deleted_at.is_(None)).distinct()).all()
        items.append({
            "id": str(o.id), "address": o.address, "ports": o.ports, "facts": o.facts,
            "status": o.status, "asset_id": str(o.asset_id) if o.asset_id else None,
            "observed_at": o.observed_at.isoformat(),
            "matches": [{"id": str(m.id), "name": m.name, "site_id": str(m.site_id) if m.site_id else None} for m in matches],
        })
    return {**_out(db, run), "items": items}


class ConfirmIn(BaseModel):
    action: Literal["create", "link", "ignore"]
    asset_id: uuid.UUID | None = None
    name: Name = "发现设备"
    asset_no: Short | None = None
    form: Form = "physical"
    role: AssetRole = "other"
    platform: Annotated[str, StringConstraints(max_length=64)] = ""
    endpoint_protocol: Protocol = "other"
    port: int = Field(443, ge=1, le=65535)
    allow_duplicate: bool = False


@router.post("/observations/{observation_id}/confirm")
def confirm(observation_id: uuid.UUID, body: ConfirmIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    require_all(scope_for(db, actor), "网段发现")
    # 先原子锁定候选；重复确认不能重复建资产。
    locked = db.execute(update(DiscoveryObservation).where(
        DiscoveryObservation.id == observation_id, DiscoveryObservation.status == "candidate",
    ).values(status="processing"))
    o = db.get(DiscoveryObservation, observation_id)
    if o is None:
        raise HTTPException(404, "观察记录不存在")
    if locked.rowcount != 1:
        raise HTTPException(409, "该候选已被处理，请刷新")
    run = _run(db, o.run_id)
    if body.action == "ignore":
        o.status = "ignored"
    elif body.action == "link":
        target = db.get(Asset, body.asset_id) if body.asset_id else None
        if target is None or target.deleted_at is not None:
            raise HTTPException(422, "请选择已有资产")
        o.asset_id, o.status = body.asset_id, "confirmed"
        # 用户明确关联观察记录，不据 IP 改写已有资产、端点或人工关系。
    else:
        duplicate = db.scalar(select(ManagementEndpoint.id).where(ManagementEndpoint.address == o.address).limit(1))
        if duplicate and not body.allow_duplicate:
            raise HTTPException(409, "已有资产使用此地址，请关联已有资产，或明确确认同 IP 新建")
        if body.port not in o.ports:
            raise HTTPException(422, "请选择本次实际探测到的端口")
        if (run.protocol == "snmp") != (body.endpoint_protocol == "snmp"):
            raise HTTPException(422, "SNMP 与 TCP 探测证据不匹配")
        if conflict := recycle.deleted_conflict(db, body.asset_no or None):
            raise HTTPException(409, conflict)
        asset = Asset(
            name=body.name, asset_no=body.asset_no or None, form=body.form, role=body.role,
            platform=body.platform, site_id=run.site_id,
            collection_profile="" if body.endpoint_protocol != "other" else "reachability",
            notes="通过只读发现确认创建；设备类型与协议由用户确认，尚未实机验证",
        )
        asset.endpoints = [ManagementEndpoint(
            address=o.address, port=body.port, protocol=body.endpoint_protocol, purpose="monitoring",
            credential_id=run.credential_id if run.protocol == "snmp" else None,
        )]
        db.add(asset)
        try:
            db.flush()
        except IntegrityError as exc:
            raise HTTPException(409, "资产编号已存在，请修改后重试") from exc
        o.asset_id, o.status = asset.id, "confirmed"
    audit.record(db, actor, f"discovery.{body.action}", target_type="discovery", target_id=o.run_id,
                 summary="处理发现候选", detail={"observation_id": str(o.id), "asset_id": str(o.asset_id) if o.asset_id else None})
    return {"status": o.status, "asset_id": str(o.asset_id) if o.asset_id else None}
