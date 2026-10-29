"""场所、机柜与凭据。凭据接口只返回元数据，任何角色都拿不到密文或明文。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.scope import Scope, credentials_condition, require_all, scope_for
from app.api.deps import editor, get_db, reader
from app.crypto import CredentialCipher
from app.models import Asset, Credential, Rack, Site, User
from app.schemas import (
    CredentialIn,
    CredentialOut,
    CredentialUpdate,
    RackIn,
    RackOut,
    RackUpdate,
    SiteIn,
    SiteOut,
    SiteUpdate,
)
from app.services.credentials import create_credential, set_secret

router = APIRouter(prefix="/api", tags=["inventory"])


def _cipher(request: Request) -> CredentialCipher:
    return request.app.state.cipher


def _conflict(db: Session, message: str) -> None:
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, message) from exc


# ---- 场所 ----


@router.get("/sites", response_model=list[SiteOut])
def list_sites(user: User = Depends(reader), db: Session = Depends(get_db)) -> list[Site]:
    scope = scope_for(db, user)
    stmt = select(Site).order_by(Site.name)
    if not scope.all:  # 只显示授权场所及其下级（4.1 P11）
        stmt = stmt.where(Site.id.in_(scope.site_ids))
    return list(db.scalars(stmt))


@router.post("/sites", response_model=SiteOut, status_code=status.HTTP_201_CREATED)
def create_site(body: SiteIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> Site:
    require_all(scope_for(db, actor), "场所管理")  # 场所决定范围边界
    if body.parent_id and db.get(Site, body.parent_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "上级场所不存在")
    site = Site(**body.model_dump())
    db.add(site)
    db.flush()
    audit.record(db, actor, "site.create", target_type="site", target_id=site.id, summary=f"新建场所 {site.name}")
    return site


@router.patch("/sites/{site_id}", response_model=SiteOut)
def update_site(site_id: uuid.UUID, body: SiteUpdate, actor: User = Depends(editor), db: Session = Depends(get_db)) -> Site:
    require_all(scope_for(db, actor), "场所管理")
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "场所不存在")
    changes = body.model_dump(exclude_unset=True)
    parent = changes.get("parent_id")
    # 不能把场所挂到自己或自己的下级下面
    while parent is not None:
        if parent == site.id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "场所层级不能形成循环")
        node = db.get(Site, parent)
        if node is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "上级场所不存在")
        parent = node.parent_id
    for key, value in changes.items():
        setattr(site, key, value)
    audit.record(db, actor, "site.update", target_type="site", target_id=site.id, summary=f"修改场所 {site.name}", detail=body.model_dump(mode="json", exclude_unset=True))
    return site


@router.delete("/sites/{site_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_site(site_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> None:
    require_all(scope_for(db, actor), "场所管理")
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "场所不存在")
    in_use = (
        db.scalar(select(func.count()).select_from(Site).where(Site.parent_id == site_id))
        or db.scalar(select(func.count()).select_from(Rack).where(Rack.site_id == site_id))
        or db.scalar(select(func.count()).select_from(Asset).where(Asset.site_id == site_id, Asset.deleted_at.is_(None)))
    )
    if in_use:
        raise HTTPException(status.HTTP_409_CONFLICT, "场所下还有下级场所、机柜或资产，不能删除")
    # 回收站里的资产恢复后还要回到这个场所，先恢复或彻底删除
    if db.scalar(select(func.count()).select_from(Asset).where(Asset.site_id == site_id, Asset.deleted_at.is_not(None))):
        raise HTTPException(status.HTTP_409_CONFLICT, "回收站里还有这个场所的资产，请先恢复或彻底删除后再删除场所")
    audit.record(db, actor, "site.delete", target_type="site", target_id=site.id, summary=f"删除场所 {site.name}")
    db.delete(site)


# ---- 机柜 ----


@router.get("/racks", response_model=list[RackOut])
def list_racks(site_id: uuid.UUID | None = None, user: User = Depends(reader), db: Session = Depends(get_db)) -> list[Rack]:
    scope = scope_for(db, user)
    stmt = select(Rack).order_by(Rack.name)
    if not scope.all:
        stmt = stmt.where(Rack.site_id.in_(scope.site_ids))
    if site_id:
        stmt = stmt.where(Rack.site_id == site_id)
    return list(db.scalars(stmt))


@router.post("/racks", response_model=RackOut, status_code=status.HTTP_201_CREATED)
def create_rack(body: RackIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> Rack:
    require_all(scope_for(db, actor), "机柜管理")
    if db.get(Site, body.site_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "场所不存在")
    rack = Rack(**body.model_dump())
    db.add(rack)
    _conflict(db, "该场所下已有同名机柜")
    audit.record(db, actor, "rack.create", target_type="rack", target_id=rack.id, summary=f"新建机柜 {rack.name}")
    return rack


@router.patch("/racks/{rack_id}", response_model=RackOut)
def update_rack(rack_id: uuid.UUID, body: RackUpdate, actor: User = Depends(editor), db: Session = Depends(get_db)) -> Rack:
    require_all(scope_for(db, actor), "机柜管理")
    rack = db.get(Rack, rack_id)
    if rack is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "机柜不存在")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(rack, key, value)
    _conflict(db, "该场所下已有同名机柜")
    audit.record(db, actor, "rack.update", target_type="rack", target_id=rack.id, summary=f"修改机柜 {rack.name}", detail=body.model_dump(mode="json", exclude_unset=True))
    return rack


@router.delete("/racks/{rack_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rack(rack_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> None:
    require_all(scope_for(db, actor), "机柜管理")
    rack = db.get(Rack, rack_id)
    if rack is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "机柜不存在")
    audit.record(db, actor, "rack.delete", target_type="rack", target_id=rack.id, summary=f"删除机柜 {rack.name}")
    db.delete(rack)


# ---- 凭据 ----


@router.get("/credentials", response_model=list[CredentialOut])
def list_credentials(user: User = Depends(reader), db: Session = Depends(get_db)) -> list[Credential]:
    stmt = select(Credential).order_by(Credential.alias)
    cond = credentials_condition(scope_for(db, user))  # 范围受限：只看范围内资产在用的凭据
    return list(db.scalars(stmt if cond is None else stmt.where(cond)))


def _get_credential(db: Session, scope: Scope, credential_id: uuid.UUID) -> Credential:
    """范围受限的账号只能修改 / 删除只被自己范围内资产使用的凭据，避免影响看不到的设备。"""
    from app.models import ManagementEndpoint

    cond = credentials_condition(scope)
    cred = db.scalar(select(Credential).where(Credential.id == credential_id, *([cond] if cond is not None else [])))
    if cred is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "凭据不存在")
    if not scope.all:
        outside = db.scalar(select(func.count()).select_from(ManagementEndpoint).where(
            ManagementEndpoint.credential_id == credential_id, ManagementEndpoint.asset_id.not_in(scope.assets()),
            ManagementEndpoint.asset_id.not_in(scope.assets(deleted=True))))  # 范围内回收站里的资产不算范围外
        if outside:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "该凭据也被你范围外的资产使用，不能修改或删除")
    return cred


@router.post("/credentials", response_model=CredentialOut, status_code=status.HTTP_201_CREATED)
def add_credential(
    body: CredentialIn, request: Request, actor: User = Depends(editor), db: Session = Depends(get_db)
) -> Credential:
    # 单独新建的凭据还没有被任何资产使用，范围受限的账号看不到它；请在资产的管理端点里新建
    require_all(scope_for(db, actor), "单独新建凭据")
    cred = create_credential(
        db, _cipher(request), alias=body.alias, kind=body.kind, secret=body.secret, username=body.username, notes=body.notes
    )
    _conflict(db, "凭据别名已存在")
    audit.record(db, actor, "credential.create", target_type="credential", target_id=cred.id, summary=f"新建凭据 {cred.alias}")
    return cred


@router.patch("/credentials/{credential_id}", response_model=CredentialOut)
def update_credential(
    credential_id: uuid.UUID,
    body: CredentialUpdate,
    request: Request,
    actor: User = Depends(editor),
    db: Session = Depends(get_db),
) -> Credential:
    cred = _get_credential(db, scope_for(db, actor), credential_id)
    changes = body.model_dump(exclude_unset=True)
    secret = changes.pop("secret", None)
    if secret is not None:
        set_secret(cred, _cipher(request), secret)
    for key, value in changes.items():
        setattr(cred, key, value)
    _conflict(db, "凭据别名已存在")
    audit.record(db, actor, "credential.update", target_type="credential", target_id=cred.id, summary=f"修改凭据 {cred.alias}", detail={**changes, "rotated": secret is not None})
    return cred


@router.delete("/credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_credential(credential_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> None:
    cred = _get_credential(db, scope_for(db, actor), credential_id)
    audit.record(db, actor, "credential.delete", target_type="credential", target_id=cred.id, summary=f"删除凭据 {cred.alias}")
    db.delete(cred)
