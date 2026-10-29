"""机柜视图、设备型号库与按 MAC / IP 定位（4.2 P14）。规则见 app/services/racks.py、devicetypes.py、locate.py。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import admin_only, editor, get_db, reader
from app.models import Asset, DeviceType, Rack, User
from app.scope import Scope, require_all, scope_for
from app.services import devicetypes, locate, racks

router = APIRouter(prefix="/api", tags=["racks"])


def _visible(db: Session, scope: Scope) -> set[uuid.UUID] | None:
    return None if scope.all else set(db.scalars(scope.assets()))


def _rack_for(db: Session, rack_id: uuid.UUID, scope: Scope, visible: set[uuid.UUID] | None) -> Rack:
    rack = db.get(Rack, rack_id)
    # 范围受限账号：机柜在授权场所内，或里面有单独授权的资产，才可见
    if rack is None or (visible is not None and rack.site_id not in scope.site_ids
                        and not db.scalar(select(Asset.id).where(Asset.rack_id == rack.id, Asset.id.in_(visible or {uuid.uuid4()})))):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "机柜不存在或你无权查看")
    return rack


@router.get("/racks/summary")
def rack_summary(site_id: uuid.UUID | None = None, user: User = Depends(reader), db: Session = Depends(get_db)) -> list[dict]:
    scope = scope_for(db, user)
    visible = _visible(db, scope)
    stmt = select(Rack).order_by(Rack.name)
    if site_id:
        stmt = stmt.where(Rack.site_id == site_id)
    if visible is not None:
        stmt = stmt.where(or_(Rack.site_id.in_(scope.site_ids), Rack.id.in_(select(Asset.rack_id).where(Asset.id.in_(visible or {uuid.uuid4()})))))
    out = []
    for rack in db.scalars(stmt):
        view = racks.elevation(db, rack, visible)
        out.append({**view["rack"], "free_u": view["free_u"], "power_watts": view["power_watts"], "placed": len(view["items"])})
    return out


@router.get("/racks/{rack_id}/elevation")
def rack_elevation(rack_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    scope = scope_for(db, user)
    visible = _visible(db, scope)
    return racks.elevation(db, _rack_for(db, rack_id, scope, visible), visible)


# ---- 设备型号 ----


class DeviceTypeIn(BaseModel):
    manufacturer: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    part_number: str = Field("", max_length=100)
    u_height: float = Field(1, ge=0, le=60)
    is_full_depth: bool = True


def _out(t: DeviceType) -> dict:
    return {"id": str(t.id), "manufacturer": t.manufacturer, "model": t.model, "part_number": t.part_number, "u_height": t.u_height,
            "is_full_depth": t.is_full_depth, "interfaces": t.interfaces, "power_ports": t.power_ports, "source": t.source}


@router.get("/device-types")
def list_device_types(q: str = Query("", max_length=100), limit: int = Query(50, ge=1, le=200),
                      user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    stmt = select(DeviceType)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(DeviceType.manufacturer.ilike(like), DeviceType.model.ilike(like), DeviceType.part_number.ilike(like)))
    rows = db.scalars(stmt.order_by(DeviceType.manufacturer, DeviceType.model, DeviceType.id).limit(limit)).all()
    return {"items": [_out(t) for t in rows], "total": len(db.scalars(select(DeviceType.id)).all())}


@router.post("/device-types", status_code=status.HTTP_201_CREATED)
def create_device_type(body: DeviceTypeIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    require_all(scope_for(db, actor), "设备型号管理")
    row = DeviceType(**body.model_dump(), source="manual")
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "该厂商下已有同名型号") from exc
    audit.record(db, actor, "device_type.create", target_type="device_type", target_id=row.id, summary=f"新建型号 {row.manufacturer} {row.model}")
    return _out(row)


@router.delete("/device-types/{type_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_device_type(type_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> None:
    require_all(scope_for(db, actor), "设备型号管理")
    row = db.get(DeviceType, type_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "型号不存在")
    if db.scalar(select(Asset.id).where(Asset.device_type_id == type_id).limit(1)):
        raise HTTPException(status.HTTP_409_CONFLICT, "还有资产使用这个型号，不能删除")
    audit.record(db, actor, "device_type.delete", target_type="device_type", target_id=row.id, summary=f"删除型号 {row.manufacturer} {row.model}")
    db.delete(row)


@router.post("/device-types/import")
async def import_device_types(file: UploadFile = File(...), manufacturers: str = Form(""),
                              actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    blob = await file.read(devicetypes.MAX_ZIP_BYTES + 1)
    wanted = {m.strip() for m in manufacturers.split(",") if m.strip()} or None
    try:
        result = devicetypes.import_zip(db, blob, manufacturers=wanted)
    except devicetypes.DeviceTypeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    audit.record(db, actor, "device_type.import", summary=f"导入设备型号库：新增 {result.created}、更新 {result.updated}",
                 detail={"file": (file.filename or "")[:200], **{k: v for k, v in result.public().items() if k != "errors"}})
    return result.public()


# ---- 按 MAC / IP 找端口 ----


@router.get("/locate")
def locate_address(q: str = Query(..., min_length=1, max_length=64), user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    scope = scope_for(db, user)
    return locate.search(db, q, scope.assets())  # 全部可见时也要排除回收站里的资产
