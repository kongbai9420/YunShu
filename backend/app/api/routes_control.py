"""P5 控制：控制归属、电源操作、操作记录、“结果不确定”确认解除；4.4 P19 一键开启 BMC 的 SNMP。

写操作要求管理员或运维；只读账号只能查看。所有控制都经过门禁，控制归属仍为旧控制器时一律拒绝。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import audit
from app.api.deps import admin_only, editor, get_db, reader
from app.scope import Scope, can_see, require_all, require_asset, restrict, scope_for
from app.control.power import ACTIONS
from app.control.service import (
    MAX_BATCH, Rejected, acknowledge_target, control_setting, describe_power, describe_thermal, operation_out, request_power,
    request_thermal,
)
from app.control.cutover import change_owner, describe as cutover_describe, probe_legacy
from app.control.policy import DEFAULT_SAFETY_TEMP, FUSE_HYSTERESIS, CurveError, safety_temp, validate_nodes
from app.control.thermal import (
    PRODUCTION_CATALOG, SOURCE_LABEL, TRANSPORT_LABEL, ThermalProfile, _version_tuple, custom_profile, parse_commands,
)
from app.models import Asset, ControlOperation, ControlTarget, ThermalCommandProfile, ThermalCurve, ThermalPolicy, User

router = APIRouter(prefix="/api/control", tags=["control"])


def _require_assets(db: Session, actor: User, asset_ids: list[uuid.UUID]) -> None:
    """控制入队前：所有目标都必须在发起人的范围内；范围外当作不存在（4.1 P11）。"""
    scope = scope_for(db, actor)
    for aid in asset_ids:
        require_asset(db, scope, aid)


def _visible_target_keys(db: Session, scope: Scope) -> set[str]:
    from app.control.base import target_key
    from app.models import ManagementEndpoint

    return {target_key(addr) for addr in db.scalars(restrict(select(ManagementEndpoint.address), ManagementEndpoint.asset_id, scope))}


@router.get("/status")
def control_status(request: Request, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    s = control_setting(db)
    targets = db.scalars(select(ControlTarget).where(ControlTarget.state == "indeterminate")).all()
    scope = scope_for(db, user)
    if not scope.all:  # 范围受限：只显示范围内资产用到的管理地址（4.1 P11）
        keys = _visible_target_keys(db, scope)
        targets = [t for t in targets if t.key in keys]
    return {
        "owner": s.owner, "revision": s.revision, "changed_by": s.changed_by,
        "changed_at": s.changed_at.isoformat() if s.changed_at else None, "note": s.note,
        "power_actions": [{"key": a.key, "label": a.label, "high_risk": a.high_risk} for a in ACTIONS.values()],
        "max_batch": MAX_BATCH,
        "thermal_catalog_size": len(_catalog(request)),
        "indeterminate_targets": [{"key": t.key, "message": t.message, "updated_at": t.updated_at.isoformat()} for t in targets],
    }


@router.get("/assets/{asset_id}/power")
def asset_power(asset_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    require_asset(db, scope_for(db, user), asset_id)
    a = db.get(Asset, asset_id, options=[selectinload(Asset.endpoints)])
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在或你无权查看")
    return {"asset_id": str(a.id), "asset_name": a.name, "confirm_phrase": a.name, **describe_power(db, a)}


class PowerRequest(BaseModel):
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_BATCH)
    action: str
    confirm: str = ""
    reason: str = Field(default="", max_length=500)
    request_key: str = Field(min_length=8, max_length=100)
    # 4.2 P17：关宿主前先正常关闭其上的虚拟机，全部确认关闭后才执行宿主操作
    guests_first: bool = False
    guest_timeout: int = Field(default=300, ge=30, le=1800)


@router.get("/assets/{asset_id}/guest-power")
def asset_guest_power(asset_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    """虚拟机 / 容器能做哪些电源操作（4.2 P17）。"""
    _require_assets(db, user, [asset_id])
    from app.control.service import describe_guest

    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在或你无权查看")
    return describe_guest(db, asset)


@router.get("/assets/{asset_id}/hosted-guests")
def asset_hosted_guests(asset_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    """宿主上承载的虚拟机，关宿主前联动用；只列出当前账号可见的。"""
    _require_assets(db, user, [asset_id])
    from app.control.guest import guest_power_enabled
    from app.control.service import guest_observation, hosted_guests
    from app.scope import can_see, scope_for

    scope = scope_for(db, user)
    guests = []
    for g in hosted_guests(db, asset_id):
        if not can_see(db, scope, g.id):
            continue
        found = guest_observation(db, g)
        guests.append({"id": str(g.id), "name": g.name, "power": found[0].power if found else "unknown", "lifecycle": g.lifecycle})
    return {"enabled": guest_power_enabled(), "guests": guests}


class GuestPowerRequest(BaseModel):
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    action: str
    confirm: str = ""
    reason: str = Field("", max_length=500)
    request_key: str = Field(min_length=1, max_length=100)
    timeout: int = Field(300, ge=30, le=1800)


@router.post("/guest-power", status_code=status.HTTP_202_ACCEPTED)
def guest_power(body: GuestPowerRequest, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    _require_assets(db, actor, body.asset_ids)
    from app.control.service import request_guest_power

    try:
        return request_guest_power(db, actor, body.asset_ids, body.action, confirm=body.confirm, reason=body.reason,
                                   request_key=body.request_key, timeout=body.timeout)
    except Rejected as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": exc.code, "message": str(exc)}) from exc


@router.get("/assets/{asset_id}/bmc-snmp")
def asset_bmc_snmp(asset_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    """能否一键开启 / 关闭这台服务器 BMC 的 SNMP（4.4 P19）。"""
    from app.control.service import describe_bmc_snmp

    require_asset(db, scope_for(db, user), asset_id)
    a = db.get(Asset, asset_id, options=[selectinload(Asset.endpoints)])
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在或你无权查看")
    return {"asset_id": str(a.id), "asset_name": a.name, **describe_bmc_snmp(db, a)}


class BmcSnmpRequest(BaseModel):
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_BATCH)
    action: str
    credential_id: uuid.UUID | None = None
    confirm: str = ""
    reason: str = Field(default="", max_length=500)
    request_key: str = Field(min_length=8, max_length=100)


@router.post("/bmc-snmp", status_code=status.HTTP_202_ACCEPTED)
def bmc_snmp(body: BmcSnmpRequest, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict[str, Any]:
    """修改的是设备设置而不是电源状态，只允许管理员。"""
    from app.control.service import request_bmc_snmp

    _require_assets(db, actor, body.asset_ids)
    try:
        return request_bmc_snmp(db, actor, body.asset_ids, body.action, credential_id=body.credential_id, confirm=body.confirm,
                                reason=body.reason, request_key=body.request_key)
    except Rejected as exc:
        code = status.HTTP_404_NOT_FOUND if exc.code == "not_found" else status.HTTP_422_UNPROCESSABLE_CONTENT
        raise HTTPException(code, {"code": exc.code, "message": str(exc)}) from exc


@router.post("/power", status_code=status.HTTP_202_ACCEPTED)
def power(body: PowerRequest, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    _require_assets(db, actor, body.asset_ids)
    try:
        return request_power(db, actor, body.asset_ids, body.action, confirm=body.confirm, reason=body.reason, request_key=body.request_key,
                             guests_first=body.guests_first, guest_timeout=body.guest_timeout)
    except Rejected as exc:
        code = status.HTTP_404_NOT_FOUND if exc.code == "not_found" else status.HTTP_422_UNPROCESSABLE_CONTENT
        raise HTTPException(code, {"code": exc.code, "message": str(exc)}) from exc


def _catalog(request: Request) -> tuple[ThermalProfile, ...]:
    # 内置命令库；离线测试可以通过 app.state 换成测试用的命令
    return getattr(request.app.state, "thermal_catalog", PRODUCTION_CATALOG)


@router.get("/assets/{asset_id}/thermal")
def asset_thermal(asset_id: uuid.UUID, request: Request, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    require_asset(db, scope_for(db, user), asset_id)
    a = db.get(Asset, asset_id, options=[selectinload(Asset.endpoints)])
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在或你无权查看")
    return {"asset_id": str(a.id), "asset_name": a.name, **describe_thermal(db, a, _catalog(request))}


class ThermalRequest(BaseModel):
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_BATCH)
    action: str
    pwm: int | None = None
    confirm: str = ""
    reason: str = Field(default="", max_length=500)
    request_key: str = Field(min_length=8, max_length=100)


@router.post("/thermal", status_code=status.HTTP_202_ACCEPTED)
def thermal(body: ThermalRequest, request: Request, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    _require_assets(db, actor, body.asset_ids)
    params = {"pwm": body.pwm} if body.pwm is not None else {}
    try:
        return request_thermal(db, actor, body.asset_ids, body.action, params, confirm=body.confirm, reason=body.reason,
                               request_key=body.request_key, catalog=_catalog(request))
    except Rejected as exc:
        code = status.HTTP_404_NOT_FOUND if exc.code == "not_found" else status.HTTP_422_UNPROCESSABLE_CONTENT
        raise HTTPException(code, {"code": exc.code, "message": str(exc)}) from exc


@router.get("/operations")
def operations(
    asset_id: uuid.UUID | None = None, batch_id: uuid.UUID | None = None, limit: int = Query(50, ge=1, le=200),
    user: User = Depends(reader), db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    q = restrict(select(ControlOperation), ControlOperation.asset_id, scope_for(db, user)).order_by(ControlOperation.created_at.desc()).limit(limit)
    if asset_id:
        q = q.where(ControlOperation.asset_id == asset_id)
    if batch_id:
        q = q.where(ControlOperation.batch_id == batch_id)
    return [operation_out(o) for o in db.scalars(q)]


class AckRequest(BaseModel):
    key: str = Field(min_length=1, max_length=300)
    note: str = Field(min_length=1, max_length=500)


@router.post("/targets/acknowledge")
def acknowledge(body: AckRequest, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, str]:
    scope = scope_for(db, actor)
    if not scope.all and body.key not in _visible_target_keys(db, scope):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "管理地址不存在")
    try:
        t = acknowledge_target(db, actor, body.key, body.note)
    except Rejected as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": exc.code, "message": str(exc)}) from exc
    return {"key": t.key, "state": t.state}


# ---- 温控策略与曲线 ----


def _policy_out(p: ThermalPolicy, name: str) -> dict[str, Any]:
    return {
        "asset_id": str(p.asset_id), "asset_name": name, "mode": p.mode, "fixed_pwm": p.fixed_pwm, "curve_key": p.curve_key,
        "enabled": p.enabled, "revision": p.revision, "source": p.source, "legacy_mode": p.legacy_mode, "pending_note": p.pending_note,
        "fuse_state": p.fuse_state, "last_decision": p.last_decision,
        "last_decision_at": p.last_decision_at.isoformat() if p.last_decision_at else None, "updated_by": p.updated_by,
    }


def _curve_out(c: ThermalCurve | None) -> dict[str, Any]:
    if c is None:
        return {"key": "default", "nodes": [], "revision": 0, "safety_temp": DEFAULT_SAFETY_TEMP, "source": "", "updated_by": ""}
    return {"key": c.key, "name": c.name, "nodes": c.nodes, "revision": c.revision, "safety_temp": safety_temp(c.nodes),
            "source": c.source, "updated_by": c.updated_by, "updated_at": c.updated_at.isoformat()}


@router.get("/thermal/policies")
def thermal_policies(user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = restrict(select(ThermalPolicy, Asset.name).join(Asset, Asset.id == ThermalPolicy.asset_id), Asset.id, scope_for(db, user))
    rows = db.execute(stmt.order_by(Asset.name)).all()
    curve = db.scalar(select(ThermalCurve).where(ThermalCurve.key == "default"))
    return {"policies": [_policy_out(p, n) for p, n in rows], "curve": _curve_out(curve), "fuse_hysteresis": FUSE_HYSTERESIS}


class PolicyUpdate(BaseModel):
    revision: int = Field(ge=0)
    mode: str
    fixed_pwm: int | None = Field(default=None, ge=0, le=100)
    enabled: bool


@router.put("/thermal/policies/{asset_id}")
def update_policy(asset_id: uuid.UUID, body: PolicyUpdate, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    require_asset(db, scope_for(db, actor), asset_id)
    a = db.get(Asset, asset_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在或你无权查看")
    if body.mode not in ("auto", "fixed", "curve"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "温控模式只能是原厂自动、固定转速或曲线")
    if body.mode == "fixed" and body.fixed_pwm is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "固定转速模式需要填写转速")
    p = db.scalar(select(ThermalPolicy).where(ThermalPolicy.asset_id == asset_id))
    current = p.revision if p else 0
    if body.revision != current:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": f"温控策略已被其他人修改（当前修订 {current}），请刷新后再改，系统不会覆盖",
                                                      "current": _policy_out(p, a.name) if p else None})
    if p is None:
        p = ThermalPolicy(asset_id=asset_id, revision=0)
        db.add(p)
    p.mode, p.fixed_pwm, p.enabled = body.mode, body.fixed_pwm if body.mode == "fixed" else None, body.enabled
    p.revision += 1
    p.source, p.pending_note, p.updated_by = "user", "", actor.display_name or actor.username
    p.last_request_key = ""
    db.flush()
    audit.record(db, actor, "thermal.policy.update", target_type="asset", target_id=asset_id,
                 summary=f"温控策略改为 {body.mode}" + (f" {body.fixed_pwm}%" if body.mode == "fixed" else "") + ("（启用）" if body.enabled else "（停用）"))
    return _policy_out(p, a.name)


class CurveUpdate(BaseModel):
    revision: int = Field(ge=0)
    nodes: list[dict[str, Any]]


@router.put("/thermal/curves/default")
def update_curve(body: CurveUpdate, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    require_all(scope_for(db, actor), "全局温控曲线")  # 影响所有设备
    try:
        nodes = validate_nodes(body.nodes)
    except CurveError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    c = db.scalar(select(ThermalCurve).where(ThermalCurve.key == "default"))
    current = c.revision if c else 0
    if body.revision != current:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": f"温控曲线已被其他人修改（当前修订 {current}），请刷新后再改，系统不会覆盖",
                                                      "current": _curve_out(c)})
    if c is None:
        c = ThermalCurve(key="default", name="全局曲线", revision=0)
        db.add(c)
    c.nodes, c.revision, c.source, c.updated_by = nodes, c.revision + 1, "user", actor.display_name or actor.username
    db.flush()
    audit.record(db, actor, "thermal.curve.update", target_type="thermal_curve", target_id="default",
                 summary=f"温控曲线改为 {len(nodes)} 个节点，熔断温度 {safety_temp(nodes):g}℃", detail={"nodes": nodes})
    return _curve_out(c)


# ---- 风扇命令库 ----


def _builtin_out(p: ThermalProfile) -> dict[str, Any]:
    return {
        "id": p.profile_id, "label": p.label or p.profile_id, "vendor": p.vendor, "vendor_aliases": list(p.vendor_aliases),
        "model_pattern": " | ".join(p.model_patterns), "firmware_min": p.firmware_min, "firmware_max": p.firmware_max,
        "transport": TRANSPORT_LABEL.get(p.transport, p.transport), "source": p.evidence.source.value,
        "source_label": SOURCE_LABEL.get(p.evidence.source, ""), "safe_min_pwm": p.safe_min_pwm, "safe_max_pwm": p.safe_max_pwm,
        "restore_commands": "\n".join(s.text() for s in p.restore_steps), "set_commands": "\n".join(s.text() for s in p.set_steps),
        "description": p.description,
    }


def _custom_out(r: ThermalCommandProfile) -> dict[str, Any]:
    return {
        "id": str(r.id), "name": r.name, "vendor": r.vendor, "model_pattern": r.model_pattern, "firmware_min": r.firmware_min,
        "firmware_max": r.firmware_max, "restore_commands": r.restore_commands, "set_commands": r.set_commands,
        "pwm_scale": r.pwm_scale, "safe_min_pwm": r.safe_min_pwm, "safe_max_pwm": r.safe_max_pwm, "enabled": r.enabled,
        "note": r.note, "updated_by": r.updated_by, "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


@router.get("/thermal/commands")
def thermal_commands(request: Request, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(ThermalCommandProfile).order_by(ThermalCommandProfile.name)).all()
    return {"builtin": [_builtin_out(p) for p in _catalog(request)], "custom": [_custom_out(r) for r in rows],
            "can_edit": user.role == "admin"}


class CommandProfileBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    vendor: str = Field(min_length=1, max_length=64)
    model_pattern: str = Field(default="", max_length=200)
    firmware_min: str = Field(default="", max_length=32)
    firmware_max: str = Field(default="", max_length=32)
    restore_commands: str = Field(max_length=2000)
    set_commands: str = Field(max_length=2000)
    pwm_scale: str = Field(default="percent", pattern="^(percent|byte)$")
    safe_min_pwm: int = Field(default=20, ge=0, le=100)
    safe_max_pwm: int = Field(default=100, ge=0, le=100)
    enabled: bool = True
    note: str = Field(default="", max_length=1000)


def _check_profile(body: CommandProfileBody) -> None:
    try:
        re.compile(body.model_pattern or ".*")
    except re.error as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"型号匹配规则不是有效的正则表达式：{exc}") from exc
    for label, v in (("最低固件版本", body.firmware_min), ("最高固件版本", body.firmware_max)):
        if v.strip() and _version_tuple(v) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{label}“{v}”里没有数字")
    if body.safe_min_pwm > body.safe_max_pwm:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "安全下限不能高于上限")
    for label, text, needs in (("恢复原厂温控", body.restore_commands, False), ("设定转速", body.set_commands, True)):
        try:
            parse_commands(text, needs_pwm=needs)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{label}的命令：{exc}") from exc


def _apply_profile(row: ThermalCommandProfile, body: CommandProfileBody, actor: User) -> None:
    for k, v in body.model_dump().items():
        setattr(row, k, v.strip() if isinstance(v, str) and k not in ("restore_commands", "set_commands", "note") else v)
    row.vendor = row.vendor.lower()
    row.updated_by = actor.display_name or actor.username
    custom_profile(row)  # 已在 _check_profile 校验；这里再转一次保证能被匹配使用


@router.post("/thermal/commands", status_code=status.HTTP_201_CREATED)
def create_thermal_command(body: CommandProfileBody, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict[str, Any]:
    _check_profile(body)
    row = ThermalCommandProfile()
    _apply_profile(row, body, actor)
    db.add(row)
    db.flush()
    audit.record(db, actor, "thermal.command.create", target_type="thermal_command", target_id=str(row.id),
                 summary=f"新增自定义风扇命令“{row.name}”（厂商 {row.vendor}）", detail=_custom_out(row))
    return _custom_out(row)


@router.put("/thermal/commands/{profile_id}")
def update_thermal_command(profile_id: uuid.UUID, body: CommandProfileBody, actor: User = Depends(admin_only),
                           db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(ThermalCommandProfile, profile_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "自定义风扇命令不存在")
    _check_profile(body)
    before = _custom_out(row)
    _apply_profile(row, body, actor)
    db.flush()
    audit.record(db, actor, "thermal.command.update", target_type="thermal_command", target_id=str(row.id),
                 summary=f"修改自定义风扇命令“{row.name}”", detail={"before": before, "after": _custom_out(row)})
    return _custom_out(row)


@router.delete("/thermal/commands/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_thermal_command(profile_id: uuid.UUID, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> None:
    row = db.get(ThermalCommandProfile, profile_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "自定义风扇命令不存在")
    audit.record(db, actor, "thermal.command.delete", target_type="thermal_command", target_id=str(row.id),
                 summary=f"删除自定义风扇命令“{row.name}”", detail=_custom_out(row))
    db.delete(row)


# ---- 控制切换 ----


def _probe(request: Request):  # type: ignore[no-untyped-def]
    # 离线测试通过 app.state 注入；生产用 HTTP 探测旧版地址
    return getattr(request.app.state, "legacy_probe", probe_legacy)


@router.get("/cutover")
def cutover_status(request: Request, _: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    return cutover_describe(db, request.app.state.settings, _catalog(request), legacy_probe=_probe(request))


class OwnerChange(BaseModel):
    owner: str
    revision: int = Field(ge=0)
    confirm: str = ""
    checklist: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=500)


@router.post("/owner")
def change_control_owner(body: OwnerChange, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return change_owner(db, actor, request.app.state.settings, _catalog(request), owner=body.owner, revision=body.revision,
                            confirm=body.confirm, checklist=body.checklist, note=body.note, legacy_probe=_probe(request))
    except Rejected as exc:
        code = status.HTTP_409_CONFLICT if exc.code == "conflict" else status.HTTP_422_UNPROCESSABLE_CONTENT
        raise HTTPException(code, {"code": exc.code, "message": str(exc)}) from exc
