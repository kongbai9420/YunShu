"""按资产范围授权（4.1 P11）。

所有接口都通过这里判断“当前账号能看到哪些资产”，不在各处各写一套：
- admin 与“全部可见”的账号不加任何限制；
- 其余账号可见：授权场所及其全部下级场所里的资产 + 单独授权的资产；宿主与子资源各自判断；
- 范围外的资产一律当作不存在（404），列表、计数、关系、事件都按同一集合过滤；
- 回收站里的资产（deleted_at 非空）对所有账号都不可见，包括 admin（P22 回收站）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import false, or_, select
from sqlalchemy.orm import Session

from app.models import Asset, Site, User, UserScopeAsset, UserScopeSite

NOT_FOUND = "资产不存在或你无权查看"


@dataclass(frozen=True)
class Scope:
    all: bool
    site_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)  # 已展开下级
    asset_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)  # 单独授权

    def assets(self, *, deleted: bool = False) -> Any:
        """可见资产 ID 的子查询（回收站外）；deleted=True 时为范围内回收站里的资产。"""
        state = Asset.deleted_at.is_not(None) if deleted else Asset.deleted_at.is_(None)
        if self.all:
            return select(Asset.id).where(state)
        conds = []
        if self.site_ids:
            conds.append(Asset.site_id.in_(self.site_ids))
        if self.asset_ids:
            conds.append(Asset.id.in_(self.asset_ids))
        return select(Asset.id).where(state, or_(*conds) if conds else false())

    def condition(self, column: Any) -> Any:
        """给任意“资产 ID 列”加范围条件。全部可见时只排除回收站里的资产，列为空的行（与资产无关）照旧保留。"""
        if self.all:
            return or_(column.is_(None), column.not_in(deleted_assets()))
        return column.in_(self.assets())


def deleted_assets() -> Any:
    """回收站里的资产 ID 子查询。"""
    return select(Asset.id).where(Asset.deleted_at.is_not(None))


def live(column: Any = None) -> Any:
    """“不在回收站”的条件：不传列时直接作用于 Asset；传资产 ID 列时用子查询。"""
    return Asset.deleted_at.is_(None) if column is None else column.not_in(deleted_assets())


ALL = Scope(all=True)


def _descendants(db: Session, roots: set[uuid.UUID]) -> set[uuid.UUID]:
    children: dict[uuid.UUID | None, list[uuid.UUID]] = {}
    for sid, parent in db.execute(select(Site.id, Site.parent_id)):
        children.setdefault(parent, []).append(sid)
    out, stack = set(), list(roots)
    while stack:
        sid = stack.pop()
        if sid in out:
            continue
        out.add(sid)
        stack += children.get(sid, [])
    return out


def scope_for(db: Session, user: User) -> Scope:
    if user.role == "admin" or user.scope_all:
        return ALL
    sites = set(db.scalars(select(UserScopeSite.site_id).where(UserScopeSite.user_id == user.id)))
    assets = frozenset(db.scalars(select(UserScopeAsset.asset_id).where(UserScopeAsset.user_id == user.id)))
    return Scope(all=False, site_ids=frozenset(_descendants(db, sites)), asset_ids=assets)


def restrict(stmt: Any, column: Any, scope: Scope) -> Any:
    cond = scope.condition(column)
    return stmt if cond is None else stmt.where(cond)


def can_see(db: Session, scope: Scope, asset_id: uuid.UUID | None, *, deleted: bool = False) -> bool:
    """deleted=False：范围内且不在回收站；deleted=True：范围内且在回收站（恢复、彻底删除用）。"""
    if asset_id is None:
        return scope.all and not deleted
    row = db.execute(select(Asset.site_id, Asset.deleted_at).where(Asset.id == asset_id)).first()
    if row is None:
        return scope.all and not deleted  # 不存在的资产交给调用方按 404 处理，与以前一致
    if (row.deleted_at is not None) != deleted:
        return False
    return scope.all or asset_id in scope.asset_ids or (row.site_id is not None and row.site_id in scope.site_ids)


def require_asset(db: Session, scope: Scope, asset_id: uuid.UUID | None, *, deleted: bool = False) -> None:
    """范围外或回收站里的资产当作不存在，避免泄漏存在性。"""
    if not can_see(db, scope, asset_id, deleted=deleted):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)


def require_all(scope: Scope, what: str) -> None:
    """只对全部可见的账号开放的操作（会产生或暴露范围外数据）。"""
    if not scope.all:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{what}只对可见全部资产的账号开放")


def credentials_condition(scope: Scope) -> Any:
    """可见凭据：被范围内资产的管理端点引用的凭据；全部可见时为 None。"""
    from app.models import Credential, ManagementEndpoint

    if scope.all:
        return None  # 凭据本身不随资产进回收站，全部可见的账号照旧看全部
    used = select(ManagementEndpoint.credential_id).where(ManagementEndpoint.asset_id.in_(scope.assets()), ManagementEndpoint.credential_id.is_not(None))
    return Credential.id.in_(used)


def credential_visible(db: Session, scope: Scope, credential_id: uuid.UUID) -> bool:
    from app.models import Credential

    if scope.all:
        return True
    return db.scalar(select(Credential.id).where(Credential.id == credential_id, credentials_condition(scope))) is not None


def visible_site_ids(scope: Scope) -> frozenset[uuid.UUID] | None:
    return None if scope.all else scope.site_ids
