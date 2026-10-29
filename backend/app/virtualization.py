"""虚拟化观察与人工确认。采集只更新观察；关系只在预览后显式确认时改变。"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import case, select, update
from sqlalchemy.orm import Session, selectinload

from app import audit, metrics
from app.collect.runner import resolve_profile
from app.models import Asset, AssetRelation, CollectionStatus, ManagementEndpoint, User, VirtualizationObservation as Observation, VirtualizationSource as Source

ORIGIN = "virtualization"


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def target_of(ep: ManagementEndpoint) -> str:
    return f"{ep.protocol}://{ep.address}:{ep.port or (8006 if ep.protocol == 'pve-api' else 443)}"


def ingest(db: Session, ep: ManagementEndpoint, snapshot: dict, now: datetime) -> None:
    platform = {"pve-api": "pve", "esxi-api": "vmware"}.get(ep.protocol)
    if not platform or snapshot.get("platform") != platform:
        return
    source = db.scalar(select(Source).where(Source.endpoint_id == ep.id))
    native = str(snapshot.get("instance_uuid") or "")
    if len(native) > 200:
        return
    if source is None:
        source = Source(endpoint_id=ep.id, platform=platform, target=target_of(ep), native_uuid=native, revision=0)
        db.add(source)
        db.flush()
    elif aware(source.observed_at) > aware(now):
        return  # 迟到的采集不能覆盖更新的观察
    source.revision += 1
    source.reported_uuid = native
    source.identity_conflict = source.target != target_of(ep) or source.platform != platform or bool(source.native_uuid and source.native_uuid != native)
    source.observed_at = now
    if source.identity_conflict:
        source.complete = False
        return  # 端点目标/服务身份变化，保留旧观察与绑定供复核
    if not source.native_uuid:
        source.native_uuid = native
    rows = []
    complete = snapshot.get("complete") is True
    for field in ("hosts", "guests"):
        items = snapshot.get(field)
        if not isinstance(items, list):
            complete = False
            continue
        for raw in items:
            if not isinstance(raw, dict):
                complete = False
                continue
            kind = "host" if field == "hosts" else raw.get("kind")
            ext = raw.get("external_id")
            host = raw.get("host_external_id") or ""
            if kind not in ("host", "vm", "container") or not isinstance(ext, str) or not ext or len(ext) > 200 or not isinstance(host, str) or len(host) > 200:
                complete = False
                continue
            rows.append({"kind": kind, "external_id": ext, "name": str(raw.get("name") or ext)[:200], "host_external_id": host,
                         "power": raw.get("power") if raw.get("power") in ("on", "off") else "unknown",
                         "connection": str(raw.get("connection") or "unknown")[:32]})
    frequencies = Counter((r["kind"], r["external_id"]) for r in rows)
    if any(n > 1 for n in frequencies.values()):
        complete = False
    existing = {(o.kind, o.external_id): o for o in db.scalars(select(Observation).where(Observation.source_id == source.id))}
    seen = set()
    for row in rows:
        key = row["kind"], row["external_id"]
        if frequencies[key] != 1:
            continue
        seen.add(key)
        o = existing.get(key)
        if o is None:
            db.add(Observation(source_id=source.id, **row, observed_at=now))
        else:
            if not o.present or o.name != row["name"] or o.host_external_id != row["host_external_id"]:
                o.revision += 1
            for k, value in row.items():
                setattr(o, k, value)
            o.present, o.observed_at = True, now
    if complete:
        for key, o in existing.items():
            if key not in seen and o.present:
                o.present = False
                o.revision += 1
    source.complete = complete


def quality(db: Session, source: Source) -> str:
    ep = db.get(ManagementEndpoint, source.endpoint_id)
    if ep is None or source.target != target_of(ep) or source.identity_conflict:
        return "identity_conflict"
    st = db.get(CollectionStatus, ep.id)
    if st is None or st.state != "ok":
        return st.state if st else "unavailable"
    if not isinstance(st.facts.get("virtualization"), dict):
        return "unavailable"
    asset = db.get(Asset, ep.asset_id, options=[selectinload(Asset.endpoints)])
    profile = resolve_profile(asset) if asset else None
    if (datetime.now(UTC) - aware(source.observed_at)).total_seconds() > metrics.STALE_FACTOR * max(1, profile.interval if profile else 60):
        return "stale"
    return "valid"


def source_out(db: Session, source: Source) -> dict:
    ep = db.get(ManagementEndpoint, source.endpoint_id)
    asset = db.get(Asset, ep.asset_id) if ep else None
    return {"id": str(source.id), "endpoint_id": str(source.endpoint_id), "asset_id": str(asset.id) if asset else None,
            "name": asset.name if asset else "已删除来源", "platform": source.platform, "target": source.target,
            "native_uuid": source.native_uuid, "reported_uuid": source.reported_uuid, "quality": quality(db, source),
            "complete": source.complete, "revision": source.revision, "observed_at": aware(source.observed_at).isoformat()}


def observation_page(db: Session, source: Source, stmt, limit: int, offset: int) -> dict:
    from sqlalchemy import func
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(stmt.order_by(case((Observation.kind == "host", 0), (Observation.kind == "vm", 1), else_=2), Observation.name, Observation.id).limit(limit).offset(offset)).all()
    host_ids = {o.host_external_id for o in items if o.host_external_id}
    hosts = {o.external_id: o for o in db.scalars(select(Observation).where(Observation.source_id == source.id, Observation.kind == "host", Observation.external_id.in_(host_ids)))}
    asset_ids = {o.asset_id for o in [*items, *hosts.values()] if o.asset_id}
    assets = {a.id: a for a in db.scalars(select(Asset).where(Asset.id.in_(asset_ids)))}
    pairs = set(db.execute(select(AssetRelation.source_asset_id, AssetRelation.target_asset_id).where(AssetRelation.target_asset_id.in_(asset_ids), AssetRelation.kind == "hosts")).all())
    q = quality(db, source)
    out = []
    for o in items:
        host = hosts.get(o.host_external_id)
        bound = assets.get(o.asset_id)
        host_asset = assets.get(host.asset_id) if host else None
        state = "ignored" if o.ignored else "unseen" if not o.present else "candidate" if not bound else "confirmed"
        if state == "confirmed" and (o.confirmed_revision != o.revision or (o.kind != "host" and (
            not host or not host.present or host.confirmed_revision != host.revision or aware(host.observed_at) != aware(source.observed_at)
            or not host_asset or host_asset.id != o.confirmed_host_asset_id or (host_asset.id, bound.id) not in pairs
        ))):
            state = "review"
        item_quality = "not_seen" if q == "valid" and aware(o.observed_at) != aware(source.observed_at) else q
        out.append({"id": str(o.id), "kind": o.kind, "external_id": o.external_id, "name": o.name, "host_external_id": o.host_external_id,
                    "present": o.present, "state": state, "revision": o.revision, "observed_at": aware(o.observed_at).isoformat(),
                    "power": o.power if item_quality == "valid" and o.present else "unknown", "last_reported_power": o.power, "quality": item_quality,
                    "asset": {"id": str(bound.id), "name": bound.name} if bound else None,
                    "host_asset": {"id": str(host_asset.id), "name": host_asset.name} if host_asset else None})
    return {"items": out, "total": total, "source": source_out(db, source)}


def _owned_relation(db: Session, o: Observation) -> AssetRelation | None:
    rel = db.get(AssetRelation, o.owned_relation_id) if o.owned_relation_id else None
    return rel if rel and rel.origin == ORIGIN and rel.kind == "hosts" and rel.target_asset_id == o.asset_id and rel.source_asset_id == o.confirmed_host_asset_id else None


def plan(db: Session, observation_id: uuid.UUID, body: dict) -> tuple[dict, Source, Observation, Asset | None, Asset | None]:
    o = db.get(Observation, observation_id)
    if o is None:
        raise HTTPException(404, "虚拟资源观察不存在")
    source = db.get(Source, o.source_id)
    if body["revision"] != o.revision:
        raise HTTPException(409, "观察已变化，请刷新后重新预览")
    action = body["action"]
    if action not in ("ignore", "unlink"):
        if quality(db, source) != "valid" or not o.present or aware(o.observed_at) != aware(source.observed_at):
            raise HTTPException(409, "来源失联、过期、身份变化或本次未见该资源，请重新采集后预览")
    asset = db.get(Asset, o.asset_id) if o.asset_id else None
    target = asset
    if action == "ignore" and asset:
        raise HTTPException(409, "已关联资源不能忽略，请先显式解除绑定")
    if action == "unlink" and not asset:
        raise HTTPException(409, "该观察没有资产绑定")
    if action == "create" and asset:
        raise HTTPException(409, "该观察已有绑定，不能重复新建")
    if action == "link":
        target = db.get(Asset, body.get("asset_id")) if body.get("asset_id") else None
        if target is None or target.deleted_at is not None:
            raise HTTPException(422, "请选择已有资产")
        if asset and target.id != asset.id:
            raise HTTPException(409, "请先预览解除原绑定，再关联另一资产；原资产不会删除")
        expected_forms = ("physical", "vm") if o.kind == "host" else (o.kind,)
        if target.role != "server" or target.form not in expected_forms:
            raise HTTPException(422, "资产形态或角色与原生资源不符：宿主应为服务器，虚拟资源应为对应 VM / 容器")
        other = db.scalar(select(Observation.id).where(Observation.source_id == source.id, Observation.asset_id == target.id, Observation.id != o.id))
        if other:
            raise HTTPException(409, "该资产已绑定此实例范围内的另一原生身份，不能自动合并")
    host_asset = None
    host = None
    if action in ("create", "link") and o.kind != "host":
        host = db.scalar(select(Observation).where(Observation.source_id == source.id, Observation.kind == "host", Observation.external_id == o.host_external_id))
        if not host or not host.present or not host.asset_id or host.confirmed_revision != host.revision or aware(host.observed_at) != aware(source.observed_at):
            raise HTTPException(409, "请先确认当前原生宿主与资产的关联；不能把管理端点直接当成宿主")
        host_asset = db.get(Asset, host.asset_id)
        if not host_asset or host_asset.deleted_at is not None or host_asset.id == (target.id if target else None):
            raise HTTPException(409, "宿主资产不存在或不能与子资源为同一资产")
    relations = list(db.scalars(select(AssetRelation).where(AssetRelation.target_asset_id == target.id, AssetRelation.kind == "hosts"))) if target else []
    old = _owned_relation(db, o)
    remove = old if old and (action == "unlink" or (host_asset and old.source_asset_id != host_asset.id)) else None
    keep = [r for r in relations if not remove or r.id != remove.id]
    parent_assets = {a.id: a for a in db.scalars(select(Asset).where(Asset.id.in_([r.source_asset_id for r in keep])))}
    payload = {
        "observation_id": str(o.id), "source_id": str(source.id), "source_revision": source.revision,
        "revision": o.revision, "action": action, "native_name": o.name, "external_id": o.external_id, "kind": o.kind,
        "name": body.get("name", ""), "asset": {"id": str(target.id), "name": target.name, "revision": target.revision} if target else None,
        "host_asset": {"id": str(host_asset.id), "name": host_asset.name, "revision": host_asset.revision} if host_asset else None,
        "host_observation_revision": host.revision if host else None,
        "remove_relation": str(remove.id) if remove else None,
        "remove_host": db.get(Asset, remove.source_asset_id).name if remove else None,
        "preserved_relations": [{"id": str(r.id), "origin": r.origin, "host_id": str(r.source_asset_id), "host_name": parent_assets[r.source_asset_id].name} for r in sorted(keep, key=lambda r: str(r.id))],
        "new_lifecycle": "planned" if action == "create" else None,
    }
    payload["preview_token"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return payload, source, o, target, host_asset


def confirm(db: Session, observation_id: uuid.UUID, body: dict, actor: User) -> dict:
    preview, source, o, asset, host_asset = plan(db, observation_id, body)
    if body.get("preview_token") != preview["preview_token"]:
        raise HTTPException(409, "预览后数据已变化，请重新预览；没有写入任何资产或关系")
    locked = db.execute(update(Source).where(Source.id == source.id, Source.revision == preview["source_revision"]).values(revision=Source.revision + 1))
    if locked.rowcount != 1:
        raise HTTPException(409, "该实例范围已被更新，请重新预览")
    action = body["action"]
    if preview["remove_relation"]:
        rel = _owned_relation(db, o)
        if rel is None or str(rel.id) != preview["remove_relation"]:
            raise HTTPException(409, "原关系已变化，请重新预览")
        o.owned_relation_id = None
        db.delete(rel)
        db.flush()
    if action == "ignore":
        o.ignored = True
    elif action == "unlink":
        o.asset_id = o.confirmed_host_asset_id = o.owned_relation_id = None
        o.confirmed_revision = 0
    else:
        if action == "create":
            asset = Asset(name=body["name"], form="physical" if o.kind == "host" else o.kind, role="server", lifecycle="planned")
            db.add(asset)
            db.flush()
        o.asset_id, o.ignored = asset.id, False
        if host_asset:
            existing = db.scalar(select(AssetRelation).where(AssetRelation.source_asset_id == host_asset.id, AssetRelation.target_asset_id == asset.id, AssetRelation.kind == "hosts"))
            if existing is None:
                existing = AssetRelation(source_asset_id=host_asset.id, target_asset_id=asset.id, kind="hosts", origin=ORIGIN)
                db.add(existing)
                db.flush()
                o.owned_relation_id = existing.id
            elif o.owned_relation_id != existing.id:
                o.owned_relation_id = None  # 不接管已有人工或其他观察创建的关系
            o.confirmed_host_asset_id = host_asset.id
        o.confirmed_revision = o.revision + 1
    o.revision += 1
    db.flush()
    audit.record(db, actor, f"virtualization.{action}", target_type="virtualization_observation", target_id=o.id,
                 summary=f"显式确认虚拟化观察：{action}", detail={"source_id": str(source.id), "asset_id": str(o.asset_id) if o.asset_id else None,
                 "host_asset_id": str(o.confirmed_host_asset_id) if o.confirmed_host_asset_id else None,
                 "removed_relation_id": preview["remove_relation"], "preserved_relation_ids": [r["id"] for r in preview["preserved_relations"]]})
    return {"asset_id": str(o.asset_id) if o.asset_id else None, "revision": o.revision, "action": action}
