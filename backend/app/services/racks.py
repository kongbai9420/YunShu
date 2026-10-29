"""机柜 U 位（4.2 P14）：按型号的 U 高与是否全深计算占用，放置时检查越界与冲突，生成机柜正 / 背面视图。

- 占用：从 rack_unit（最下面一个 U）往上占 ceil(u_height) 个 U；0U 设备（如竖装 PDU）不占 U 位。
- 安装面：全深设备前后两面都占；非全深设备只占 rack_face 那一面，前后可以各装一台。
- 没有指定型号的资产按 1U、全深计算（与升级前一致的保守估计）。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, DeviceType, LatestMetric, Rack


@dataclass(frozen=True)
class Footprint:
    height: int
    faces: frozenset[str]


def footprint(asset: Asset, dtype: DeviceType | None) -> Footprint:
    if dtype is None:
        return Footprint(1, frozenset({"front", "rear"}))
    height = math.ceil(dtype.u_height) if dtype.u_height > 0 else 0
    faces = frozenset({"front", "rear"}) if dtype.is_full_depth else frozenset({asset.rack_face or "front"})
    return Footprint(height, faces)


class PlacementError(ValueError):
    pass


def check_placement(db: Session, asset: Asset) -> None:
    """资产已放进机柜且有 U 位时，检查是否越界、是否与同机柜其他资产重叠。"""
    if asset.rack_id is None or asset.rack_unit is None:
        return
    rack = db.get(Rack, asset.rack_id)
    if rack is None:
        return
    types = _types(db)
    mine = footprint(asset, types.get(asset.device_type_id))
    if mine.height == 0:
        return
    top = asset.rack_unit + mine.height - 1
    if top > rack.height_u:
        raise PlacementError(f"超出机柜高度：从 U{asset.rack_unit} 起占 {mine.height}U，需要到 U{top}，机柜只有 {rack.height_u}U")
    for other in db.scalars(select(Asset).where(Asset.rack_id == asset.rack_id, Asset.rack_unit.is_not(None), Asset.id != asset.id,
                                              Asset.deleted_at.is_(None))):
        theirs = footprint(other, types.get(other.device_type_id))
        if theirs.height == 0 or not (mine.faces & theirs.faces):
            continue
        lo, hi = other.rack_unit, other.rack_unit + theirs.height - 1
        if asset.rack_unit <= hi and lo <= top:
            raise PlacementError(f"U 位冲突：U{max(asset.rack_unit, lo)} 已被“{other.name}”占用")


def _types(db: Session) -> dict[uuid.UUID | None, DeviceType]:
    return {t.id: t for t in db.scalars(select(DeviceType))}


def elevation(db: Session, rack: Rack, visible: set[uuid.UUID] | None) -> dict:
    """机柜正 / 背面视图。visible 为 None 表示全部可见；范围外的资产只显示“已占用”，不显示名称（4.1 P11）。"""
    types = _types(db)
    # 回收站里的资产不占 U 位，也不显示
    assets = db.scalars(select(Asset).where(Asset.rack_id == rack.id, Asset.deleted_at.is_(None))).all()
    watts = {m.asset_id: m.value for m in db.scalars(select(LatestMetric).where(
        LatestMetric.asset_id.in_([a.id for a in assets]), LatestMetric.metric_key == "power.watts"))} if assets else {}
    items, unplaced = [], []
    used: dict[str, set[int]] = {"front": set(), "rear": set()}
    for a in sorted(assets, key=lambda x: (x.rack_unit or 0, x.name)):
        shown = visible is None or a.id in visible
        fp = footprint(a, types.get(a.device_type_id))
        dtype = types.get(a.device_type_id)
        if a.rack_unit is None:
            if shown:
                unplaced.append({"id": str(a.id), "name": a.name})
            continue
        for face in fp.faces:
            used[face].update(range(a.rack_unit, a.rack_unit + fp.height))
        items.append({
            "id": str(a.id) if shown else None, "name": a.name if shown else "已占用（无权查看）",
            "unit": a.rack_unit, "height": fp.height, "faces": sorted(fp.faces), "full_depth": len(fp.faces) == 2,
            "model": f"{dtype.manufacturer} {dtype.model}" if dtype and shown else (a.model if shown else ""),
            "power_watts": watts.get(a.id) if shown else None,
        })
    free = {face: rack.height_u - len({u for u in units if u <= rack.height_u}) for face, units in used.items()}
    total_watts = sum(w for w in (i["power_watts"] for i in items) if w is not None)
    return {"rack": {"id": str(rack.id), "name": rack.name, "site_id": str(rack.site_id), "height_u": rack.height_u},
            "items": items, "unplaced": unplaced, "free_u": free, "power_watts": round(total_watts, 1)}
