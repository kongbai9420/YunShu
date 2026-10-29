"""用户管理与审计查询（仅管理员）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import admin_only, get_db
from app.auth import hash_password, revoke_user_sessions
from app.models import AuditEvent, User
from app.schemas import AuditOut, Page, UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/api", tags=["admin"])


@router.get("/users", response_model=list[UserOut])
def list_users(_: User = Depends(admin_only), db: Session = Depends(get_db)) -> list[User]:
    return list(db.scalars(select(User).order_by(User.username)))


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> User:
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        display_name=body.display_name,
        require_password_change=True,
    )
    db.add(user)
    db.flush()
    audit.record(db, actor, "user.create", target_type="user", target_id=user.id, summary=f"新建用户 {user.username}", detail={"role": user.role})
    return user


def _active_admins(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))) or 0


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: uuid.UUID, body: UserUpdate, actor: User = Depends(admin_only), db: Session = Depends(get_db)
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    changes = body.model_dump(exclude_unset=True)
    demoting = user.role == "admin" and user.is_active and (
        changes.get("role", "admin") != "admin" or changes.get("is_active", True) is False
    )
    if demoting and _active_admins(db) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "至少需要保留一个启用的管理员")
    if "password" in changes:
        user.password_hash = hash_password(changes.pop("password"))
        user.require_password_change = True
        revoke_user_sessions(db, user)
    for key, value in changes.items():
        setattr(user, key, value)
    if changes.get("is_active") is False or "role" in changes:
        revoke_user_sessions(db, user)
    audit.record(db, actor, "user.update", target_type="user", target_id=user.id, summary=f"修改用户 {user.username}", detail=body.model_dump(mode="json", exclude_unset=True))
    return user


@router.get("/audit", response_model=Page[AuditOut])
def list_audit(
    action: str | None = None,
    target_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(AuditEvent)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if target_id:
        stmt = stmt.where(AuditEvent.target_id == target_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(stmt.order_by(AuditEvent.occurred_at.desc()).limit(limit).offset(offset)).all()
    return {"items": items, "total": total}


# ---- 资产范围（4.1 P11）----


class ScopeIn(BaseModel):
    scope_all: bool
    site_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)


def _scope_out(db: Session, user: User) -> dict:
    from app.models import UserScopeAsset, UserScopeSite

    return {
        "user_id": str(user.id), "role": user.role, "scope_all": user.scope_all, "effective_all": user.role == "admin" or user.scope_all,
        "site_ids": [str(i) for i in db.scalars(select(UserScopeSite.site_id).where(UserScopeSite.user_id == user.id))],
        "asset_ids": [str(i) for i in db.scalars(select(UserScopeAsset.asset_id).where(UserScopeAsset.user_id == user.id))],
    }


def _user(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    return user


@router.get("/users/{user_id}/scope")
def get_scope(user_id: uuid.UUID, _: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    return _scope_out(db, _user(db, user_id))


@router.put("/users/{user_id}/scope")
def set_scope(user_id: uuid.UUID, body: ScopeIn, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    from sqlalchemy import delete

    from app.models import Asset, Site, UserScopeAsset, UserScopeSite

    user = _user(db, user_id)
    if user.role == "admin":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "管理员始终可见全部资产，不能设置范围")
    sites, assets = set(body.site_ids), set(body.asset_ids)
    if sites and len(set(db.scalars(select(Site.id).where(Site.id.in_(sites))))) != len(sites):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "有场所不存在")
    if assets and len(set(db.scalars(select(Asset.id).where(Asset.id.in_(assets))))) != len(assets):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "有资产不存在")
    user.scope_all = body.scope_all
    db.execute(delete(UserScopeSite).where(UserScopeSite.user_id == user.id))
    db.execute(delete(UserScopeAsset).where(UserScopeAsset.user_id == user.id))
    db.add_all([UserScopeSite(user_id=user.id, site_id=s) for s in sites] + [UserScopeAsset(user_id=user.id, asset_id=a) for a in assets])
    db.flush()
    audit.record(db, actor, "user.scope", target_type="user", target_id=user.id, summary=f"设置用户 {user.username} 的资产范围",
                 detail={"scope_all": body.scope_all, "sites": len(sites), "assets": len(assets)})
    return _scope_out(db, user)


@router.get("/users/{user_id}/scope/preview")
def preview_scope(user_id: uuid.UUID, _: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    """账号实际可见的资产：数量与前 100 项（含所在场所），便于管理员核对。"""
    from app.models import Asset, Site
    from app.scope import restrict, scope_for

    user = _user(db, user_id)
    scope = scope_for(db, user)
    stmt = restrict(select(Asset.id, Asset.name, Asset.site_id), Asset.id, scope)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    sites = dict(db.execute(select(Site.id, Site.name)).all())
    rows = db.execute(stmt.order_by(Asset.name, Asset.id).limit(100)).all()
    return {"all": scope.all, "total": total,
            "items": [{"id": str(r.id), "name": r.name, "site_name": sites.get(r.site_id)} for r in rows]}
