"""拓扑：画布视图、网络链路、重新扫描（预览 + 应用）。

三个平面分开：采集观察只读；链路的确认 / 忽略 / 人工新建在这里改；
视图只存布局与“移出视图”，带修订号，旧修订号保存返回 409。删除视图或移出节点都不删资产。
三个角色都能查看；改动仅管理员 / 运维，全部记审计。重新扫描只读数据库里已采集的 LLDP 邻居表，不连接设备。
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import editor, get_db, reader
from app.scope import Scope, require_all, restrict, scope_for
from app.models import Asset, AssetRelation, NetworkLink, Rack, Site, TopologyView, User
from app.schemas import ORM, Name
from app.services import topology as svc

router = APIRouter(prefix="/api/topology", tags=["topology"])

ViewKind = Literal["home", "datacenter"]
LinkKind = Literal["ethernet", "fiber", "wifi", "virtual", "other"]
Port = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
MAX_NODES = 5000


# ---- 请求与响应 ----


class Position(BaseModel):
    x: float
    y: float
    locked: bool = False

    @field_validator("x", "y")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v) or abs(v) > 1_000_000:
            raise ValueError("坐标超出范围")
        return v


class ViewCreate(BaseModel):
    name: Name
    kind: ViewKind = "home"
    site_id: uuid.UUID | None = None


class ViewUpdate(BaseModel):
    revision: int
    name: Name | None = None
    kind: ViewKind | None = None
    site_id: uuid.UUID | None = None
    layout: dict[uuid.UUID, Position] | None = Field(None, max_length=MAX_NODES)
    hidden: list[uuid.UUID] | None = Field(None, max_length=MAX_NODES)


class ViewOut(ORM):
    id: uuid.UUID
    name: str
    kind: str
    site_id: uuid.UUID | None
    layout: dict[str, Any]
    hidden: list[str]
    revision: int
    updated_by: str
    updated_at: datetime


class LinkOut(ORM):
    id: uuid.UUID
    source_asset_id: uuid.UUID
    target_asset_id: uuid.UUID
    source_port: str
    target_port: str
    kind: str
    origin: str
    state: str
    evidence: dict[str, Any]
    locked: bool
    notes: str
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ManualLinkIn(BaseModel):
    source_asset_id: uuid.UUID
    target_asset_id: uuid.UUID
    source_port: Port = ""
    target_port: Port = ""
    kind: LinkKind = "ethernet"
    notes: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""


class ManualLinkUpdate(BaseModel):
    source_asset_id: uuid.UUID | None = None
    target_asset_id: uuid.UUID | None = None
    source_port: Port | None = None
    target_port: Port | None = None
    kind: LinkKind | None = None
    notes: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class RescanApplyIn(BaseModel):
    token: Annotated[str, StringConstraints(min_length=1, max_length=64)]


# ---- 视图 ----


def _get_view(db: Session, view_id: uuid.UUID) -> TopologyView:
    view = db.get(TopologyView, view_id)
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "拓扑视图不存在")
    return view


def _check_site(db: Session, site_id: uuid.UUID | None) -> None:
    if site_id is not None and db.get(Site, site_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "场所不存在")


def _flush_name(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "已有同名拓扑视图") from exc


@router.get("/views", response_model=list[ViewOut])
def list_views(user: User = Depends(reader), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    scope = scope_for(db, user)
    return [_view_out(db, v, scope) for v in db.scalars(select(TopologyView).order_by(TopologyView.created_at, TopologyView.name))]


def _view_out(db: Session, view: TopologyView, scope: Scope) -> Any:
    """范围受限时只返回可见资产的位置与隐藏项（复制一份，不改数据库里的布局）（4.1 P11）。"""
    if scope.all:
        return view
    visible = {str(i) for i in db.scalars(scope.assets())}
    data = ViewOut.model_validate(view).model_dump()
    data["layout"] = {k: v for k, v in (view.layout or {}).items() if str(k) in visible}
    data["hidden"] = [x for x in (view.hidden or []) if str(x) in visible]
    return data


@router.post("/views", response_model=ViewOut, status_code=status.HTTP_201_CREATED)
def create_view(body: ViewCreate, actor: User = Depends(editor), db: Session = Depends(get_db)) -> TopologyView:
    require_all(scope_for(db, actor), "拓扑编辑")
    _check_site(db, body.site_id)
    view = TopologyView(**body.model_dump(), layout={}, hidden=[], updated_by=actor.display_name or actor.username)
    db.add(view)
    _flush_name(db)
    audit.record(db, actor, "topology.view.create", target_type="topology_view", target_id=view.id, summary=f"新建拓扑视图 {view.name}")
    return view


@router.get("/views/{view_id}", response_model=ViewOut)
def get_view(view_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> Any:
    return _view_out(db, _get_view(db, view_id), scope_for(db, user))


@router.patch("/views/{view_id}", response_model=ViewOut)
def update_view(view_id: uuid.UUID, body: ViewUpdate, actor: User = Depends(editor), db: Session = Depends(get_db)) -> TopologyView:
    require_all(scope_for(db, actor), "拓扑编辑")
    view = _get_view(db, view_id)
    if body.revision != view.revision:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"拓扑视图已被他人修改（当前修订号 {view.revision}，更新者 {view.updated_by or '未知'}），请载入最新后重试",
        )
    changes = body.model_dump(mode="json", exclude_unset=True, exclude={"revision"})
    if "site_id" in changes:
        _check_site(db, body.site_id)
        view.site_id = body.site_id
    for key in ("name", "kind", "layout"):
        if key in changes and changes[key] is not None:
            setattr(view, key, changes[key])
    if changes.get("hidden") is not None:
        view.hidden = list(dict.fromkeys(changes["hidden"]))
    view.revision += 1
    view.updated_by = actor.display_name or actor.username
    _flush_name(db)
    audit.record(
        db, actor, "topology.view.update", target_type="topology_view", target_id=view.id,
        summary=f"保存拓扑视图 {view.name}（修订号 {view.revision}）",
        detail={"fields": sorted(changes), "nodes": len(view.layout or {}), "hidden": len(view.hidden or []), "revision": view.revision},
    )
    return view


@router.delete("/views/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_view(view_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> None:
    require_all(scope_for(db, actor), "拓扑编辑")
    view = _get_view(db, view_id)
    audit.record(db, actor, "topology.view.delete", target_type="topology_view", target_id=view.id, summary=f"删除拓扑视图 {view.name}（资产与链路不受影响）")
    db.delete(view)


def _site_scope(db: Session, site_id: uuid.UUID) -> set[uuid.UUID]:
    """场所及其下级场所。"""
    children: dict[uuid.UUID | None, list[uuid.UUID]] = {}
    for sid, parent in db.execute(select(Site.id, Site.parent_id)):
        children.setdefault(parent, []).append(sid)
    scope, todo = set(), [site_id]
    while todo:
        cur = todo.pop()
        if cur in scope:
            continue
        scope.add(cur)
        todo.extend(children.get(cur, []))
    return scope


@router.get("/graph")
def get_graph(view_id: uuid.UUID | None = None, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    """画布数据：节点（资产摘要）、链路（含已忽略，前端按状态区分）、资产关系（承载 / 安装于等，只读）。
    节点只含范围内资产；链路与关系只在两端都可见时返回（4.1 P11）。"""
    stmt = restrict(select(Asset), Asset.id, scope_for(db, user)).order_by(Asset.name)
    if view_id is not None:
        view = _get_view(db, view_id)
        if view.site_id is not None:
            stmt = stmt.where(Asset.site_id.in_(_site_scope(db, view.site_id)))
    assets = db.scalars(stmt).all()
    ids = {a.id for a in assets}
    sites = {s.id: s.name for s in db.scalars(select(Site))}
    racks = {r.id: r.name for r in db.scalars(select(Rack))}
    links = db.scalars(select(NetworkLink).where(NetworkLink.source_asset_id.in_(ids), NetworkLink.target_asset_id.in_(ids)).order_by(NetworkLink.created_at)).all()
    relations = db.scalars(select(AssetRelation).where(AssetRelation.source_asset_id.in_(ids), AssetRelation.target_asset_id.in_(ids))).all()
    return {
        "nodes": [
            {"id": str(a.id), "name": a.name, "form": a.form, "role": a.role, "platform": a.platform, "lifecycle": a.lifecycle,
             "site_id": str(a.site_id) if a.site_id else None, "site_name": sites.get(a.site_id) if a.site_id else None,
             "rack_id": str(a.rack_id) if a.rack_id else None, "rack_name": racks.get(a.rack_id) if a.rack_id else None,
             "rack_unit": a.rack_unit}
            for a in assets
        ],
        "links": [LinkOut.model_validate(link).model_dump(mode="json") for link in links],
        "relations": [
            {"id": str(r.id), "source_asset_id": str(r.source_asset_id), "target_asset_id": str(r.target_asset_id), "kind": r.kind, "origin": r.origin}
            for r in relations
        ],
    }


# ---- 链路 ----


def _get_link(db: Session, link_id: uuid.UUID) -> NetworkLink:
    link = db.get(NetworkLink, link_id)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "链路不存在")
    return link


def _names(db: Session, *ids: uuid.UUID) -> list[str]:
    out = []
    for aid in ids:
        asset = db.get(Asset, aid)
        if asset is None or asset.deleted_at is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "资产不存在或你无权查看")
        out.append(asset.name)
    return out


def _link_detail(link: NetworkLink) -> dict[str, Any]:
    return {"source_asset_id": str(link.source_asset_id), "target_asset_id": str(link.target_asset_id),
            "source_port": link.source_port, "target_port": link.target_port, "kind": link.kind,
            "origin": link.origin, "state": link.state}


@router.get("/links", response_model=list[LinkOut])
def list_links(
    state: Literal["candidate", "confirmed", "rejected"] | None = None,
    asset_id: uuid.UUID | None = None,
    limit: int = Query(1000, ge=1, le=5000),
    user: User = Depends(reader),
    db: Session = Depends(get_db),
) -> list[NetworkLink]:
    scope = scope_for(db, user)
    stmt = restrict(restrict(select(NetworkLink), NetworkLink.source_asset_id, scope), NetworkLink.target_asset_id, scope)
    if state:
        stmt = stmt.where(NetworkLink.state == state)
    if asset_id:
        stmt = stmt.where(or_(NetworkLink.source_asset_id == asset_id, NetworkLink.target_asset_id == asset_id))
    return list(db.scalars(stmt.order_by(NetworkLink.created_at).limit(limit)))


def _check_manual(db: Session, source: uuid.UUID, target: uuid.UUID, sport: str, tport: str, exclude: uuid.UUID | None = None) -> list[str]:
    if source == target:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "资产不能与自身连线")
    names = _names(db, source, target)
    lo, hi = svc.pair_key(source, target)
    for other in db.scalars(select(NetworkLink).where(NetworkLink.origin == "manual")):
        if other.id == exclude or svc.pair_key(other.source_asset_id, other.target_asset_id) != (lo, hi):
            continue
        same = (other.source_asset_id, other.source_port, other.target_port) == (source, sport, tport) or (
            other.source_asset_id == target and (other.source_port, other.target_port) == (tport, sport))
        if same:
            raise HTTPException(status.HTTP_409_CONFLICT, "这两台资产之间已有相同端口的人工连线")
    return names


@router.post("/links", response_model=LinkOut, status_code=status.HTTP_201_CREATED)
def create_manual_link(body: ManualLinkIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> NetworkLink:
    require_all(scope_for(db, actor), "拓扑编辑")
    names = _check_manual(db, body.source_asset_id, body.target_asset_id, body.source_port, body.target_port)
    link = NetworkLink(**body.model_dump(), origin="manual", state="confirmed", fingerprint="", evidence={"source": "manual"}, locked=True)
    db.add(link)
    db.flush()
    audit.record(db, actor, "topology.link.create", target_type="network_link", target_id=link.id,
                 summary=f"新增人工连线 {names[0]} → {names[1]}", detail=_link_detail(link))
    return link


@router.patch("/links/{link_id}", response_model=LinkOut)
def update_manual_link(link_id: uuid.UUID, body: ManualLinkUpdate, actor: User = Depends(editor), db: Session = Depends(get_db)) -> NetworkLink:
    require_all(scope_for(db, actor), "拓扑编辑")
    link = _get_link(db, link_id)
    if link.origin != "manual":
        raise HTTPException(status.HTTP_409_CONFLICT, "只能修改人工连线；扫描得到的链路请确认或忽略")
    changes = body.model_dump(exclude_unset=True)
    for key in ("source_asset_id", "target_asset_id", "kind"):
        if key in changes and changes[key] is None:
            changes.pop(key)
    merged = {k: changes.get(k, getattr(link, k)) for k in ("source_asset_id", "target_asset_id", "source_port", "target_port")}
    names = _check_manual(db, merged["source_asset_id"], merged["target_asset_id"], merged["source_port"] or "", merged["target_port"] or "", exclude=link.id)
    for key, value in changes.items():
        setattr(link, key, value if value is not None else "")
    db.flush()
    audit.record(db, actor, "topology.link.update", target_type="network_link", target_id=link.id,
                 summary=f"修改人工连线 {names[0]} → {names[1]}", detail=_link_detail(link))
    return link


@router.post("/links/{link_id}/confirm", response_model=LinkOut)
def confirm_link(link_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> NetworkLink:
    require_all(scope_for(db, actor), "拓扑编辑")
    link = _get_link(db, link_id)
    if link.state == "confirmed":
        raise HTTPException(status.HTTP_409_CONFLICT, "该链路已确认")
    link.origin, link.state, link.locked = "confirmed", "confirmed", True
    names = _names(db, link.source_asset_id, link.target_asset_id)
    db.flush()
    audit.record(db, actor, "topology.link.confirm", target_type="network_link", target_id=link.id,
                 summary=f"确认链路 {names[0]} → {names[1]}", detail=_link_detail(link))
    return link


@router.post("/links/{link_id}/reject", response_model=LinkOut)
def reject_link(link_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> NetworkLink:
    require_all(scope_for(db, actor), "拓扑编辑")
    link = _get_link(db, link_id)
    if link.origin == "manual":
        raise HTTPException(status.HTTP_409_CONFLICT, "人工连线不能忽略，请直接删除")
    if link.state == "rejected":
        raise HTTPException(status.HTTP_409_CONFLICT, "该链路已忽略")
    # 已忽略的链路作为记录保留：重新扫描看到同一对设备也不会再提出
    link.state, link.locked = "rejected", False
    names = _names(db, link.source_asset_id, link.target_asset_id)
    db.flush()
    audit.record(db, actor, "topology.link.reject", target_type="network_link", target_id=link.id,
                 summary=f"忽略链路 {names[0]} → {names[1]}", detail=_link_detail(link))
    return link


@router.delete("/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_manual_link(link_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> None:
    require_all(scope_for(db, actor), "拓扑编辑")
    link = _get_link(db, link_id)
    if link.origin != "manual":
        raise HTTPException(status.HTTP_409_CONFLICT, "只有人工连线可以删除；扫描得到的链路请忽略")
    names = _names(db, link.source_asset_id, link.target_asset_id)
    audit.record(db, actor, "topology.link.delete", target_type="network_link", target_id=link.id,
                 summary=f"删除人工连线 {names[0]} → {names[1]}", detail=_link_detail(link))
    db.delete(link)


# ---- 重新扫描 ----


def _plan_out(db: Session, plan: svc.Plan) -> dict[str, Any]:
    names = {a.id: a.name for a in db.scalars(select(Asset))}

    def prop(p: svc.Proposal) -> dict[str, Any]:
        return {"fingerprint": p.fingerprint, "source_asset_id": str(p.source_asset_id), "target_asset_id": str(p.target_asset_id),
                "source_name": names.get(p.source_asset_id, ""), "target_name": names.get(p.target_asset_id, ""),
                "source_port": p.source_port, "target_port": p.target_port, "evidence": p.evidence()}

    def link(item: NetworkLink) -> dict[str, Any]:
        return {"id": str(item.id), "source_asset_id": str(item.source_asset_id), "target_asset_id": str(item.target_asset_id),
                "source_name": names.get(item.source_asset_id, ""), "target_name": names.get(item.target_asset_id, ""),
                "source_port": item.source_port, "target_port": item.target_port, "evidence": item.evidence}

    return {
        "token": plan.token,
        "added": [prop(p) for p in plan.added],
        "refreshed": [{"link": link(item), "proposal": prop(p)} for item, p in plan.refreshed],
        "removed": [link(item) for item in plan.removed],
        "unchanged": len(plan.unchanged),
        "kept_unreadable": len(plan.kept_unreadable),
        "skipped_rejected": len(plan.skipped_rejected),
        "skipped_covered": len(plan.skipped_covered),
        "protected": plan.protected,
        "readable_reporters": plan.readable_reporters,
        "unmatched": plan.unmatched[:200],
    }


@router.post("/rescan/preview")
def rescan_preview(actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    require_all(scope_for(db, actor), "拓扑编辑")
    """计算差异，不写库。"""
    return _plan_out(db, svc.build_plan(db))


@router.post("/rescan/apply")
def rescan_apply(body: RescanApplyIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    require_all(scope_for(db, actor), "拓扑编辑")
    plan = svc.build_plan(db)
    if plan.token != body.token:
        raise HTTPException(status.HTTP_409_CONFLICT, "扫描结果在预览之后有变化（新的采集或他人已合并），请重新预览后再合并")
    summary = svc.apply_plan(db, plan)
    audit.record(db, actor, "topology.rescan.apply", target_type="topology", target_id="links",
                 summary=f"合并扫描结果：新增 {summary['added']}，更新 {summary['refreshed']}，移除 {summary['removed']}", detail=summary)
    return summary
