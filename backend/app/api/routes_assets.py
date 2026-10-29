"""资产、管理端点、资产关系。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import audit, dialect, recycle
from app.scope import ALL, Scope, require_asset, restrict, scope_for
from app.api.deps import admin_only, editor, get_db, reader
from app.models import Asset, AssetRelation, Credential, ManagementEndpoint, Rack, Site, User
from app.schemas import (
    AssetCreate,
    AssetDetail,
    AssetOut,
    AssetUpdate,
    EndpointIn,
    EndpointOut,
    EndpointUpdate,
    Page,
    RelationIn,
    RelationOut,
)
from app.services.credentials import create_credential

router = APIRouter(prefix="/api", tags=["assets"])

# 列表可排序的列（白名单，其他值返回 422）
SORT_COLUMNS = {
    "name": Asset.name, "asset_no": Asset.asset_no, "form": Asset.form, "role": Asset.role,
    "platform": Asset.platform, "vendor": Asset.vendor, "lifecycle": Asset.lifecycle, "updated_at": Asset.updated_at,
}


def _get_asset(db: Session, asset_id: uuid.UUID, scope: Scope) -> Asset:
    require_asset(db, scope, asset_id)  # 范围外当作不存在（4.1 P11）
    asset = db.get(Asset, asset_id, options=[selectinload(Asset.endpoints)])
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在或你无权查看")
    return asset


def _check_site_in_scope(scope: Scope, site_id: uuid.UUID | None) -> None:
    """范围受限的账号只能把资产放在授权场所内（否则资产会立刻离开自己的范围）。"""
    if not scope.all and site_id not in scope.site_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能在授权的场所内新建或移动资产")


def _check_credential_visible(db: Session, scope: Scope, credential_id: uuid.UUID | None) -> None:
    from app.scope import credential_visible

    if credential_id is not None and not credential_visible(db, scope, credential_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "凭据不存在")


def _check_refs(db: Session, *, site_id=None, rack_id=None, credential_id=None) -> None:  # type: ignore[no-untyped-def]
    if site_id is not None and db.get(Site, site_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "场所不存在")
    if rack_id is not None:
        rack = db.get(Rack, rack_id)
        if rack is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "机柜不存在")
        if site_id is not None and rack.site_id != site_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "机柜不属于所选场所")
    if credential_id is not None and db.get(Credential, credential_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "凭据不存在")


def _check_rack(db: Session, asset: Asset) -> None:
    """型号存在、U 位不越界不重叠（4.2 P14）。"""
    from app.models import DeviceType
    from app.services.racks import PlacementError, check_placement

    if asset.device_type_id is not None and db.get(DeviceType, asset.device_type_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "设备型号不存在")
    try:
        check_placement(db, asset)
    except PlacementError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


def _check_asset_no(db: Session, asset_no: str | None, exclude: uuid.UUID | None = None) -> None:
    message = recycle.deleted_conflict(db, asset_no, exclude)
    if message:
        raise HTTPException(status.HTTP_409_CONFLICT, message)


def _flush_unique(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "资产编号已存在") from exc


@router.get("/assets", response_model=Page[AssetOut])
def list_assets(
    q: str | None = Query(None, max_length=100),
    form: str | None = None,
    role: str | None = None,
    platform: str | None = None,
    lifecycle: str | None = None,
    site_id: uuid.UUID | None = None,
    tag: str | None = None,
    sort: str = Query("name", pattern="^(" + "|".join(SORT_COLUMNS) + ")$"),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(reader),
    db: Session = Depends(get_db),
) -> dict:
    stmt = restrict(select(Asset), Asset.id, scope_for(db, user))
    if q:
        like = f"%{q}%"
        addr = select(ManagementEndpoint.asset_id).where(ManagementEndpoint.address.ilike(like))
        stmt = stmt.where(or_(Asset.name.ilike(like), Asset.asset_no.ilike(like), Asset.model.ilike(like), Asset.serial_no.ilike(like), Asset.id.in_(addr)))
    # form / role / lifecycle 可用逗号传多个值（前端“四类入口”预设用）
    for column, value in ((Asset.form, form), (Asset.role, role), (Asset.lifecycle, lifecycle)):
        if value:
            values = [v for v in value.split(",") if v]
            stmt = stmt.where(column.in_(values))
    if platform is not None:
        stmt = stmt.where(Asset.platform == platform)
    if site_id is not None:
        stmt = stmt.where(Asset.site_id == site_id)
    if tag:
        # tags 是 JSON 数组，精确匹配某个标签（两种数据库写法不同）
        stmt = stmt.where(dialect.has_tag(db, Asset.tags, tag))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    column = SORT_COLUMNS[sort]
    # 未填写（空值或空字符串，如无资产编号、未填平台）无论升降序都排在最后；同值按名称、再按 id，翻页顺序稳定
    primary = column.desc() if order == "desc" else column.asc()
    ordering = [func.coalesce(column, "") == "", primary, Asset.name, Asset.id] if sort != "updated_at" else [primary, Asset.name, Asset.id]
    items = db.scalars(stmt.options(selectinload(Asset.endpoints)).order_by(*ordering).limit(limit).offset(offset)).all()
    return {"items": items, "total": total}


@router.post("/assets", response_model=AssetOut, status_code=status.HTTP_201_CREATED)
def create_asset(body: AssetCreate, request: Request, actor: User = Depends(editor), db: Session = Depends(get_db)) -> Asset:
    scope = scope_for(db, actor)
    _check_refs(db, site_id=body.site_id, rack_id=body.rack_id)
    _check_site_in_scope(scope, body.site_id)
    for ep in body.endpoints:
        _check_refs(db, credential_id=ep.credential_id)
        _check_credential_visible(db, scope, ep.credential_id)
    _check_asset_no(db, body.asset_no)
    asset = Asset(**body.model_dump(exclude={"endpoints"}))
    _maintenance_window(asset, set(body.model_fields_set), was="")
    asset.endpoints = []
    for ep in body.endpoints:
        data = ep.model_dump(exclude={"new_credential"})
        if ep.new_credential is not None:
            cred = create_credential(db, request.app.state.cipher, **ep.new_credential.model_dump())
            try:
                db.flush()
            except IntegrityError as exc:
                db.rollback()
                raise HTTPException(status.HTTP_409_CONFLICT, "凭据别名已存在，请选择已有凭据或更换别名") from exc
            data["credential_id"] = cred.id
            audit.record(db, actor, "credential.create", target_type="credential", target_id=cred.id, summary=f"新建凭据 {cred.alias}")
        if data.get("tls_fingerprint") or data.get("ssh_host_key"):
            data["trust_pinned_at"] = datetime.now(UTC)
        asset.endpoints.append(ManagementEndpoint(**data))
    _check_rack(db, asset)
    db.add(asset)
    _flush_unique(db)
    audit.record(db, actor, "asset.create", target_type="asset", target_id=asset.id, summary=f"新建资产 {asset.name}")
    return asset


# ---- 回收站（P22）：路径写在 /assets/{asset_id} 之前，避免被当成资产 ID ----


class RecycleSettings(BaseModel):
    retention_days: int = Field(ge=recycle.MIN_DAYS, le=recycle.MAX_DAYS)


@router.get("/assets/recycle-bin")
def recycle_bin(user: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    scope = scope_for(db, user)
    days = recycle.retention_days(db)
    rows = db.scalars(select(Asset).where(Asset.id.in_(scope.assets(deleted=True))).order_by(Asset.deleted_at.desc(), Asset.name)).all()
    return {"retention_days": days, "items": [
        {"id": str(a.id), "name": a.name, "asset_no": a.asset_no, "form": a.form, "role": a.role,
         "deleted_at": recycle.aware(a.deleted_at).isoformat(), "deleted_by": a.deleted_by, "deleted_lifecycle": a.deleted_lifecycle,
         "purge_at": recycle.purge_at(a, days).isoformat()} for a in rows if a.deleted_at is not None]}


@router.get("/assets/recycle-bin/settings")
def recycle_settings(_: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    return {"retention_days": recycle.retention_days(db)}


@router.put("/assets/recycle-bin/settings")
def update_recycle_settings(body: RecycleSettings, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    before = recycle.retention_days(db)
    recycle.set_retention_days(db, body.retention_days, recycle.who(actor))
    audit.record(db, actor, "asset.recycle_settings", target_type="platform_setting", target_id=recycle.SETTING_KEY,
                 summary=f"回收站保留期 {before} 天 → {body.retention_days} 天", detail={"before": before, "after": body.retention_days})
    return {"retention_days": body.retention_days}


@router.post("/assets/{asset_id}/restore")
def restore_asset(asset_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    require_asset(db, scope_for(db, actor), asset_id, deleted=True)  # 只能恢复自己范围内、确实在回收站里的资产
    asset = db.get(Asset, asset_id)
    assert asset is not None
    notice = recycle.restore(db, asset)
    audit.record(db, actor, "asset.restore", target_type="asset", target_id=asset.id,
                 summary=f"从回收站恢复资产 {asset.name}" + (f"；{notice}" if notice else ""))
    return {"id": str(asset.id), "name": asset.name, "lifecycle": asset.lifecycle, "revision": asset.revision, "notice": notice}


@router.delete("/assets/{asset_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
def purge_asset(asset_id: uuid.UUID, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> None:
    require_asset(db, ALL, asset_id, deleted=True)  # 只彻底删除回收站里的资产；在用的先移入回收站
    asset = db.get(Asset, asset_id)
    assert asset is not None
    audit.record(db, actor, "asset.purge", target_type="asset", target_id=asset.id, summary=f"彻底删除回收站里的资产 {asset.name}")
    recycle.purge(db, asset)


@router.get("/assets/{asset_id}", response_model=AssetDetail)
def get_asset(asset_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    scope = scope_for(db, user)
    asset = _get_asset(db, asset_id, scope)
    stmt = select(AssetRelation).where(or_(AssetRelation.source_asset_id == asset_id, AssetRelation.target_asset_id == asset_id))
    # 关系只在两端都可见时显示，不泄漏范围外资产的存在
    stmt = restrict(restrict(stmt, AssetRelation.source_asset_id, scope), AssetRelation.target_asset_id, scope)
    relations = db.scalars(stmt).all()
    data = AssetOut.model_validate(asset).model_dump()
    data["relations"] = relations
    return data


@router.patch("/assets/{asset_id}", response_model=AssetOut)
def update_asset(
    asset_id: uuid.UUID, body: AssetUpdate, actor: User = Depends(editor), db: Session = Depends(get_db)
) -> Asset:
    scope = scope_for(db, actor)
    asset = _get_asset(db, asset_id, scope)
    if body.revision != asset.revision:
        raise HTTPException(status.HTTP_409_CONFLICT, f"资产已被他人修改（当前修订号 {asset.revision}），请刷新后重试")
    changes = body.model_dump(exclude_unset=True, exclude={"revision"})
    site_id = changes.get("site_id", asset.site_id)
    rack_id = changes.get("rack_id", asset.rack_id)
    _check_refs(db, site_id=site_id, rack_id=rack_id)
    if "site_id" in changes and not scope.all and asset.id not in scope.asset_ids:
        _check_site_in_scope(scope, site_id)
    if "asset_no" in changes:
        _check_asset_no(db, changes["asset_no"], asset.id)
    was = asset.lifecycle
    for key, value in changes.items():
        setattr(asset, key, value)
    _maintenance_window(asset, set(changes), was=was)
    if changes.keys() & {"rack_id", "rack_unit", "rack_face", "device_type_id"}:
        with db.no_autoflush:
            _check_rack(db, asset)
    asset.revision += 1
    _flush_unique(db)
    audit.record(db, actor, "asset.update", target_type="asset", target_id=asset.id, summary=f"修改资产 {asset.name}", detail=body.model_dump(mode="json", exclude_unset=True))
    return asset


MAINTENANCE_DEFAULT = timedelta(days=7)


def _maintenance_window(asset: Asset, sent: set[str], *, was: str) -> None:
    """维护中才有预计结束时间：刚改为维护中且没填时默认 7 天后；改成别的状态时清空。"""
    if asset.lifecycle != "maintenance":
        asset.maintenance_until = None
        return
    until = asset.maintenance_until
    if "maintenance_until" in sent and until is not None:  # 只检查这次提交的值（库里读回的时间不带时区）
        if until.tzinfo is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "预计结束时间须包含时区")
        if until <= datetime.now(UTC):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "预计结束时间必须晚于现在")
    if until is None and (was != "maintenance" or "maintenance_until" in sent):
        asset.maintenance_until = datetime.now(UTC) + MAINTENANCE_DEFAULT


@router.delete("/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset(asset_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> None:
    asset = _get_asset(db, asset_id, scope_for(db, actor))
    # 移入回收站而不是直接删除；保留期内可恢复，过期由 Worker 彻底删除
    recycle.soft_delete(db, asset, recycle.who(actor))
    days = recycle.retention_days(db)
    audit.record(db, actor, "asset.delete", target_type="asset", target_id=asset.id, summary=f"删除资产 {asset.name}（移入回收站，保留 {days} 天）")


# ---- 管理端点 ----


@router.post("/assets/{asset_id}/endpoints", response_model=EndpointOut, status_code=status.HTTP_201_CREATED)
def add_endpoint(
    asset_id: uuid.UUID, body: EndpointIn, actor: User = Depends(editor), db: Session = Depends(get_db)
) -> ManagementEndpoint:
    scope = scope_for(db, actor)
    asset = _get_asset(db, asset_id, scope)
    _check_refs(db, credential_id=body.credential_id)
    _check_credential_visible(db, scope, body.credential_id)
    ep = ManagementEndpoint(asset_id=asset.id, **body.model_dump())
    db.add(ep)
    asset.revision += 1
    db.flush()
    audit.record(db, actor, "endpoint.create", target_type="asset", target_id=asset.id, summary=f"新增管理端点 {ep.protocol}")
    return ep


def _get_endpoint(db: Session, endpoint_id: uuid.UUID, scope: Scope) -> ManagementEndpoint:
    ep = db.get(ManagementEndpoint, endpoint_id)
    if ep is None or not _visible(db, scope, ep.asset_id):  # 全部可见时也排除回收站里资产的端点
        raise HTTPException(status.HTTP_404_NOT_FOUND, "管理端点不存在")
    return ep


def _visible(db: Session, scope: Scope, asset_id: uuid.UUID) -> bool:
    from app.scope import can_see

    return can_see(db, scope, asset_id)


@router.patch("/endpoints/{endpoint_id}", response_model=EndpointOut)
def update_endpoint(
    endpoint_id: uuid.UUID, body: EndpointUpdate, actor: User = Depends(editor), db: Session = Depends(get_db)
) -> ManagementEndpoint:
    scope = scope_for(db, actor)
    ep = _get_endpoint(db, endpoint_id, scope)
    changes = body.model_dump(exclude_unset=True)
    if "credential_id" in changes:
        _check_refs(db, credential_id=changes["credential_id"])
        _check_credential_visible(db, scope, changes["credential_id"])
    for key, value in changes.items():
        setattr(ep, key, value)
    ep.asset.revision += 1
    audit.record(db, actor, "endpoint.update", target_type="asset", target_id=ep.asset_id, summary="修改管理端点", detail=body.model_dump(mode="json", exclude_unset=True))
    return ep


@router.delete("/endpoints/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_endpoint(endpoint_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> None:
    ep = _get_endpoint(db, endpoint_id, scope_for(db, actor))
    ep.asset.revision += 1
    audit.record(db, actor, "endpoint.delete", target_type="asset", target_id=ep.asset_id, summary="删除管理端点")
    db.delete(ep)


# ---- 资产关系 ----


@router.post("/relations", response_model=RelationOut, status_code=status.HTTP_201_CREATED)
def create_relation(body: RelationIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> AssetRelation:
    if body.source_asset_id == body.target_asset_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "资产不能与自身建立关系")
    scope = scope_for(db, actor)
    for aid in (body.source_asset_id, body.target_asset_id):
        _get_asset(db, aid, scope)
    rel = AssetRelation(**body.model_dump(), origin="manual")
    db.add(rel)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "该关系已存在") from exc
    audit.record(db, actor, "relation.create", target_type="relation", target_id=rel.id, summary=f"新建关系 {rel.kind}", detail=body.model_dump(mode="json"))
    return rel


@router.delete("/relations/{relation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_relation(relation_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> None:
    rel = db.get(AssetRelation, relation_id)
    scope = scope_for(db, actor)
    if rel is None or not (_visible(db, scope, rel.source_asset_id) and _visible(db, scope, rel.target_asset_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "关系不存在")
    audit.record(db, actor, "relation.delete", target_type="relation", target_id=rel.id, summary=f"删除关系 {rel.kind}")
    db.delete(rel)


# ---- 4.3 P18b 软件清单 ----


@router.get("/assets/{asset_id}/software")
def asset_software(asset_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    from app import inventory

    return inventory.asset_software(db, _get_asset(db, asset_id, scope_for(db, user)))


@router.get("/assets/{asset_id}/packages")
def asset_packages(asset_id: uuid.UUID, q: str = Query("", max_length=100), limit: int = Query(200, ge=1, le=2000),
                   user: User = Depends(reader), db: Session = Depends(get_db)) -> list[dict]:
    from app import inventory

    asset = _get_asset(db, asset_id, scope_for(db, user))
    return inventory.list_packages(db, asset.id, q.strip(), limit)


@router.post("/assets/{asset_id}/software/scan", status_code=status.HTTP_202_ACCEPTED)
def scan_software(asset_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    from app import inventory

    asset = _get_asset(db, asset_id, scope_for(db, actor))
    n = inventory.request_scan(db, asset)
    if n == 0:
        raise HTTPException(422, "这台资产没有打开“读取软件包清单”的 SSH 端点")
    audit.record(db, actor, "inventory.packages.scan", target_type="asset", target_id=asset.id, summary=f"读取 {asset.name} 的软件包清单")
    return {"queued": n}
