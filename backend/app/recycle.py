"""资产回收站（P22）。

删除资产只是移入回收站：记下删除时间、删除人和原生命周期，生命周期改为“已退役”，
这样采集、控制、命令巡检、漏洞比对、软件清单这些只处理“在用”资产的后台任务自然跳过它；
界面与接口通过 app.scope 一律把它当作不存在。保留期（默认 30 天，管理员可改 1–365 天）
过后由 Worker 每小时彻底删除，与以前的删除相同。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import Asset, PlatformSetting, User

SETTING_KEY = "recycle_bin_days"
DEFAULT_DAYS = 30
MIN_DAYS, MAX_DAYS = 1, 365
# 从维护中删除的资产恢复后给一个预计结束时间，维护到期提醒才有依据
RESTORED_MAINTENANCE = timedelta(days=7)


def aware(dt: datetime) -> datetime:
    """SQLite 读回的时间不带时区，按 UTC 处理。"""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def retention_days(db: Session) -> int:
    row = db.get(PlatformSetting, SETTING_KEY)
    try:
        days = int(row.value) if row is not None else DEFAULT_DAYS
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return days if MIN_DAYS <= days <= MAX_DAYS else DEFAULT_DAYS


def set_retention_days(db: Session, days: int, by: str) -> None:
    row = db.get(PlatformSetting, SETTING_KEY)
    if row is None:
        db.add(PlatformSetting(key=SETTING_KEY, value=days, updated_by=by))
    else:
        row.value, row.updated_by = days, by
    db.flush()


def purge_at(asset: Asset, days: int) -> datetime:
    assert asset.deleted_at is not None
    return aware(asset.deleted_at) + timedelta(days=days)


def deleted_conflict(db: Session, asset_no: str | None, exclude: Any = None) -> str | None:
    """资产编号唯一：回收站里的资产仍占着编号。返回给用户的提示，没有冲突时返回 None。"""
    if not asset_no:
        return None
    stmt = select(Asset).where(Asset.asset_no == asset_no, Asset.deleted_at.is_not(None))
    if exclude is not None:
        stmt = stmt.where(Asset.id != exclude)
    other = db.scalar(stmt)
    if other is None:
        return None
    return f"资产编号“{asset_no}”被回收站里的资产“{other.name}”占用，请先在回收站恢复或彻底删除它"


def soft_delete(db: Session, asset: Asset, by: str, now: datetime | None = None) -> None:
    asset.deleted_at = now or datetime.now(UTC)
    asset.deleted_by = by[:100]
    asset.deleted_lifecycle = asset.lifecycle
    asset.lifecycle = "retired"
    asset.maintenance_until = None
    asset.revision += 1
    db.flush()


def restore(db: Session, asset: Asset, now: datetime | None = None) -> str:
    """恢复资产；原 U 位已被占用时移出 U 位（仍在原机柜），返回给用户的提示。"""
    from app.services.racks import PlacementError, check_placement

    now = now or datetime.now(UTC)
    asset.lifecycle = asset.deleted_lifecycle or "active"
    asset.maintenance_until = now + RESTORED_MAINTENANCE if asset.lifecycle == "maintenance" else None
    asset.deleted_at, asset.deleted_by, asset.deleted_lifecycle = None, "", ""
    asset.revision += 1
    notice = ""
    if asset.rack_id is not None and asset.rack_unit is not None:
        try:
            with db.no_autoflush:
                check_placement(db, asset)
        except PlacementError as exc:
            notice = f"原来的 U 位已不可用（{exc}），已移出 U 位，请在机柜里重新安排"
            asset.rack_unit = None
    db.flush()
    return notice


def purge(db: Session, asset: Asset) -> None:
    """彻底删除，与 P22 之前的删除相同（管理端点等随资产级联删除）。"""
    db.delete(asset)
    db.flush()


def purge_due(db: Session, now: datetime | None = None) -> int:
    """Worker 每小时调用：彻底删除超过保留期的资产。"""
    now = now or datetime.now(UTC)
    days = retention_days(db)
    cutoff = now - timedelta(days=days)
    n = 0
    for asset in db.scalars(select(Asset).where(Asset.deleted_at.is_not(None), Asset.deleted_at < cutoff)):
        if purge_at(asset, days) > now:  # 数据库比较不带时区时再按 UTC 核对一遍
            continue
        audit.record(db, None, "asset.purge", target_type="asset", target_id=asset.id,
                     summary=f"回收站保留期（{days} 天）已过，自动彻底删除资产 {asset.name}")
        purge(db, asset)
        n += 1
    return n


def who(user: User) -> str:
    return user.display_name or user.username
