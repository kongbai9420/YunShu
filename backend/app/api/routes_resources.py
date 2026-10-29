"""资源全景的只读批量投影与显示排序。

- 投影：承载只投影一层，不改关系、不触发采集或控制。
- 排序（p7-resource-order）：分组顺序、主卡顺序、每台宿主下的子资源顺序作为一份共享布局存数据库并带 revision，
  与仪表盘布局分开存放（dashboards 表 key=overview-resources）。只改变显示位置，不改宿主关系、类别或场所；
  admin / operator 可保存，viewer 只读；修订不一致返回 409 与当前版本，由用户选择载入或覆盖。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, get_args

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from app import audit, dialect, metrics
from app.api.deps import editor, get_db, reader
from app.api.routes_dashboard import _asset_state
from app.collect.runner import resolve_profile
from app.models import Alert, Asset, AssetRelation, CollectionStatus, Dashboard, ManagementEndpoint, Site, User
from app.schemas import AssetRole, Form, Lifecycle
from app.scope import ALL, Scope, can_see, restrict, scope_for

router = APIRouter(prefix="/api/overview/resources", tags=["overview"])
METRIC_KEYS = ("power.on", "cpu.temp", "cpu.load", "mem.used_pct", "power.watts")
ORDER_KEY = "overview-resources"
# 分组与页面一致：物理主机 / 独立系统、已关联与未关联的虚拟资源，其余按资产类别
GROUPS = ("server", "virtual-linked", "virtual-unlinked", *(f"role:{r}" for r in get_args(AssetRole) if r != "server"))
MAX_ORDER = 20000
MAX_CHILD_HOSTS = 5000


def group_of(form: str, role: str, has_parent: bool) -> str:
    if form in ("vm", "container"):
        return "virtual-linked" if has_parent else "virtual-unlinked"
    return "server" if role == "server" else f"role:{role}"


class OrderData(BaseModel):
    groups: list[str] = Field(default_factory=list, max_length=len(GROUPS))
    order: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_ORDER)
    children: dict[uuid.UUID, list[uuid.UUID]] = Field(default_factory=dict, max_length=MAX_CHILD_HOSTS)

    @field_validator("groups")
    @classmethod
    def _groups(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)) or not set(v) <= set(GROUPS):
            raise ValueError("分组顺序里有重复或未知的分组")
        return v

    @field_validator("order")
    @classmethod
    def _order(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(v) != len(set(v)):
            raise ValueError("资源顺序里有重复的资产")
        return v

    @field_validator("children")
    @classmethod
    def _children(cls, v: dict[uuid.UUID, list[uuid.UUID]]) -> dict[uuid.UUID, list[uuid.UUID]]:
        for ids in v.values():
            if len(ids) != len(set(ids)) or len(ids) > MAX_ORDER:
                raise ValueError("子资源顺序里有重复的资产")
        return v


class OrderIn(BaseModel):
    revision: int
    data: OrderData


class Layout:
    """读取时的排序：分组名次、主卡位置、宿主下的子资源位置；未出现的按名称 / UUID 稳定排在后面。"""

    def __init__(self, data: dict | None):
        data = data or {}
        groups = [g for g in data.get("groups", []) if g in GROUPS]
        self.groups = groups + [g for g in GROUPS if g not in groups]
        self.group_rank = {g: i for i, g in enumerate(self.groups)}
        self.position = {s: i for i, s in enumerate(data.get("order", []))}
        self.children = {h: {s: i for i, s in enumerate(ids)} for h, ids in data.get("children", {}).items()}

    def key(self, asset_id: uuid.UUID, name: str, group: str | None = None, host: uuid.UUID | None = None) -> tuple:
        positions = self.children.get(str(host), {}) if host else self.position
        rank = self.group_rank.get(group, len(GROUPS)) if group else 0
        return (rank, positions.get(str(asset_id), MAX_ORDER), name, str(asset_id))


def _record(db: Session) -> Dashboard | None:
    return db.scalar(select(Dashboard).where(Dashboard.key == ORDER_KEY))


def _layout(db: Session) -> Layout:
    record = _record(db)
    return Layout(record.data if record else None)


def _layout_out(d: Dashboard | None) -> dict[str, Any]:
    if d is None:
        return {"revision": 0, "data": None, "updated_by": "", "updated_at": None}
    at = d.updated_at if d.updated_at.tzinfo else d.updated_at.replace(tzinfo=UTC)
    return {"revision": d.revision, "data": d.data, "updated_by": d.updated_by, "updated_at": at.isoformat()}


def _has_parent(scope: Scope = ALL) -> Any:
    cond = scope.condition(AssetRelation.source_asset_id)
    return exists().where(AssetRelation.target_asset_id == Asset.id, AssetRelation.kind == "hosts", *([cond] if cond is not None else []))


def _page(db: Session, stmt: Any, limit: int, offset: int, layout: Layout | None = None, host: uuid.UUID | None = None,
          scope: Scope = ALL) -> dict:
    """先取匹配资产的轻量字段按排序分页，再批量读取本页详情；SQL 数不随卡片数增长。
    范围受限时（4.1 P11）：只列可见资产；宿主引用、宿主数量与承载数量都只计可见的一端，不泄漏范围外名称或数量。"""
    layout = layout or Layout(None)
    stmt = restrict(stmt, Asset.id, scope)
    rows = db.execute(stmt.with_only_columns(Asset.id, Asset.name, Asset.form, Asset.role, _has_parent(scope).label("has_parent"))).all()
    keyed = []
    for row in rows:
        group = group_of(row.form, row.role, row.has_parent)
        keyed.append((layout.key(row.id, row.name, None if host else group, host), row.id, group))
    keyed.sort(key=lambda item: item[0])
    window = keyed[offset:offset + limit]
    groups = {asset_id: group for _, asset_id, group in window}
    now = datetime.now(UTC)
    if not window:
        return {"items": [], "total": len(keyed), "generated_at": now.isoformat()}
    by_id = {a.id: a for a in db.scalars(select(Asset).options(selectinload(Asset.endpoints)).where(Asset.id.in_(list(groups))))}
    assets = [by_id[asset_id] for _, asset_id, _ in window if asset_id in by_id]
    ids = [a.id for a in assets]

    # 每项附带有限的关系引用；所有关系查看走已有资产详情，不递归取图。
    host_asset = aliased(Asset)
    ranked = select(
        AssetRelation.target_asset_id.label("child_id"), host_asset.id.label("id"), host_asset.name.label("name"),
        func.row_number().over(partition_by=AssetRelation.target_asset_id, order_by=(host_asset.name, host_asset.id)).label("position"),
        func.count().over(partition_by=AssetRelation.target_asset_id).label("total"),
    ).join(host_asset, host_asset.id == AssetRelation.source_asset_id).where(
        AssetRelation.kind == "hosts", AssetRelation.target_asset_id.in_(ids),
        *([c] if (c := scope.condition(host_asset.id)) is not None else []),
    ).subquery()
    parents: dict[uuid.UUID, list[dict]] = {}
    parent_counts: dict[uuid.UUID, int] = {}
    for row in db.execute(select(ranked).where(ranked.c.position <= 3).order_by(ranked.c.child_id, ranked.c.position)):
        parents.setdefault(row.child_id, []).append({"id": str(row.id), "name": row.name})
        parent_counts[row.child_id] = row.total
    children = dict(db.execute(restrict(select(AssetRelation.source_asset_id, func.count()).where(
        AssetRelation.kind == "hosts", AssetRelation.source_asset_id.in_(ids),
    ), AssetRelation.target_asset_id, scope).group_by(AssetRelation.source_asset_id)).all())
    statuses: dict[uuid.UUID, list[CollectionStatus]] = {}
    for s in db.scalars(select(CollectionStatus).where(CollectionStatus.asset_id.in_(ids))):
        statuses.setdefault(s.asset_id, []).append(s)
    values = metrics.latest_many(db, ids, keys=METRIC_KEYS, now=now)
    alerts = dict(db.execute(select(Alert.asset_id, func.count()).where(
        Alert.asset_id.in_(ids), Alert.state.in_(["active", "acknowledged", "silenced"]),
    ).group_by(Alert.asset_id)).all())
    site_ids = {a.site_id for a in assets if a.site_id}
    sites = dict(db.execute(select(Site.id, Site.name).where(Site.id.in_(site_ids))).all()) if site_ids else {}
    items = []
    for a in assets:
        lv = values[a.id]
        sts = statuses.get(a.id, [])
        profile = resolve_profile(a)
        relevant = [s for s in sts if s.state != "skipped"]
        attempts_stale = bool(relevant) and all(
            (now - (s.last_attempt_at if s.last_attempt_at.tzinfo else s.last_attempt_at.replace(tzinfo=UTC))).total_seconds()
            > metrics.STALE_FACTOR * max(1, profile.interval if profile else 60) for s in relevant
        )
        items.append({
            "id": str(a.id), "name": a.name, "asset_no": a.asset_no,
            "form": a.form, "role": a.role, "platform": a.platform, "lifecycle": a.lifecycle, "tags": a.tags,
            "site_id": str(a.site_id) if a.site_id else None, "site_name": sites.get(a.site_id),
            "endpoints": [{"protocol": e.protocol, "address": e.address, "port": e.port} for e in sorted(a.endpoints, key=lambda e: str(e.id))],
            "collection_state": _asset_state(a, sts, attempts_stale or (bool(lv) and all(v.quality == "stale" for v in lv))),
            "collection_at": max((s.last_attempt_at for s in sts), default=None),
            "metrics": {v.key: asdict(v) for v in lv}, "active_alerts": alerts.get(a.id, 0),
            "parents": parents.get(a.id, []), "parent_count": parent_counts.get(a.id, 0),
            "children_count": children.get(a.id, 0), "group": groups[a.id],
        })
    return {"items": items, "total": len(keyed), "generated_at": now.isoformat()}


@router.get("")
def resources(
    q: str = Query("", max_length=100), role: AssetRole | None = None, form: Form | None = None,
    lifecycle: Lifecycle | None = None, platform: str | None = Query(None, max_length=64),
    site_id: uuid.UUID | None = None, tag: str = Query("", max_length=50),
    limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0),
    user: User = Depends(reader), db: Session = Depends(get_db),
) -> dict:
    stmt = select(Asset)
    if q:
        like = f"%{q}%"
        addresses = select(ManagementEndpoint.asset_id).where(ManagementEndpoint.address.ilike(like))
        stmt = stmt.where(or_(Asset.name.ilike(like), Asset.asset_no.ilike(like), Asset.platform.ilike(like), Asset.id.in_(addresses)))
    for col, value in ((Asset.role, role), (Asset.form, form), (Asset.lifecycle, lifecycle), (Asset.platform, platform), (Asset.site_id, site_id)):
        if value is not None:
            stmt = stmt.where(col == value)
    if tag:
        stmt = stmt.where(dialect.has_tag(db, Asset.tags, tag))
    return _page(db, stmt, limit, offset, _layout(db), scope=scope_for(db, user))


@router.get("/layout")
def get_order_layout(_: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _layout_out(_record(db))


@router.get("/order")
def effective_order(user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    """编辑排序时的完整当前顺序（只有 ID 与分组），保存时据此保留筛选外、其他页资产的位置。范围受限时只含可见资产。"""
    record = _record(db)
    layout = Layout(record.data if record else None)
    scope = scope_for(db, user)
    rows = db.execute(restrict(select(Asset.id, Asset.name, Asset.form, Asset.role, _has_parent(scope).label("has_parent")), Asset.id, scope)).all()
    items = sorted((layout.key(r.id, r.name, g), str(r.id), g) for r in rows for g in [group_of(r.form, r.role, r.has_parent)])
    existing = {asset_id for _, asset_id, _ in items}
    saved_children = record.data.get("children", {}) if record else {}
    children = {h: [c for c in ids if c in existing] for h, ids in saved_children.items() if h in existing}
    return {"revision": record.revision if record else 0, "groups": layout.groups, "default_groups": list(GROUPS),
            "items": [{"id": asset_id, "group": g} for _, asset_id, g in items], "children": children}


@router.put("/layout")
def save_order_layout(body: OrderIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    d = _record(db)
    current = d.revision if d else 0
    if body.revision != current:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": "排序在你编辑期间已被更新，请选择载入最新版本或用你的版本覆盖", "current": _layout_out(d)})
    # 编辑期间被删除的资产不写入；只保存 ID，不保存名称或状态
    existing = set(db.scalars(select(Asset.id)))
    data = {"groups": body.data.groups, "order": [str(i) for i in body.data.order if i in existing],
            "children": {str(h): [str(c) for c in ids if c in existing] for h, ids in body.data.children.items() if h in existing and ids}}
    scope = scope_for(db, actor)
    if d is not None and not scope.all:
        data = _merge_hidden(db, scope, d.data or {}, data)
    who = actor.display_name or actor.username
    if d is None:
        d = Dashboard(key=ORDER_KEY, name="资源全景排序", data=data, revision=1, updated_by=who)
        db.add(d)
    else:
        d.data, d.revision, d.updated_by = data, d.revision + 1, who
        d.updated_at = datetime.now(UTC)
    db.flush()
    audit.record(db, actor, "overview.resources.order.save", target_type="dashboard", target_id=ORDER_KEY,
                 summary=f"保存资源全景排序，修订 {d.revision}", detail={"assets": len(data["order"]), "hosts": len(data["children"])})
    return _layout_out(d)


@router.get("/{asset_id}/children")
def resource_children(
    asset_id: uuid.UUID, limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0),
    user: User = Depends(reader), db: Session = Depends(get_db),
) -> dict:
    if db.get(Asset, asset_id) is None:
        raise HTTPException(404, "资产不存在或你无权查看")
    scope = scope_for(db, user)
    if not can_see(db, scope, asset_id):
        raise HTTPException(404, "资产不存在或你无权查看")
    related = select(AssetRelation.target_asset_id).where(AssetRelation.source_asset_id == asset_id, AssetRelation.kind == "hosts")
    return _page(db, select(Asset).where(Asset.id.in_(related)), limit, offset, _layout(db), host=asset_id, scope=scope)


def _merge_hidden(db: Session, scope: Scope, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """共享排序：范围受限的账号只调整自己看得到的资产，看不到的保持原来的位置（4.1 P11）。"""
    visible = {str(i) for i in db.scalars(scope.assets())}

    def merge(old_ids: list[str], new_ids: list[str]) -> list[str]:
        mine = iter([i for i in new_ids if i in visible])
        out = [next(mine, None) if i in visible else i for i in old_ids]
        out = [i for i in out if i is not None]
        return out + [i for i in mine if i not in out]

    children = {h: ids for h, ids in (old.get("children") or {}).items() if h not in visible}
    for h, ids in new.get("children", {}).items():
        children[h] = merge(old.get("children", {}).get(h, []), ids)
    return {"groups": new["groups"], "order": merge(old.get("order", []), new["order"]), "children": children}
