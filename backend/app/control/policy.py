"""温控策略：旧版温控曲线与每台温控模式的迁入、曲线计算、策略执行（82℃ 熔断）与脱管告警。

执行原则（旧版 _process_dynamic_curve）：
- 只有控制归属为新平台、策略启用、资产在用时才会请求下发；所有下发都走控制门禁和控制任务。
- 82℃ 熔断：最新有效 CPU 温度达到曲线最高节点温度（默认 82℃）时请求恢复原厂自动温控；
  降到熔断温度减 5℃ 以下才恢复按策略调速。
- 温度过期、缺失或无效时绝不降低转速；不知道当前已下发转速（例如重启后）时不下发任何调速。
- 同样的请求被拒绝或失败后 10 分钟内不重复提交，避免刷屏。
- 新平台负责控制、策略要求固定转速或曲线、但门禁不允许执行时，记为“温控脱管”并告警：
  设备可能停在旧版留下的手动转速，失去原厂自动升速保护，需要人工处理。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import audit, metrics
from app.control.service import (
    PRODUCTION_CATALOG, Rejected, ThermalProfile, new_platform_controls, request_thermal_system, thermal_gate, thermal_target,
)
from app.metrics import Point
from app.models import Asset, ControlOperation, ControlTarget, LegacyMapping, ThermalCurve, ThermalPolicy

DEFAULT_SAFETY_TEMP = 82.0
FUSE_HYSTERESIS = 5.0
MIN_STEP = 2
RETRY_AFTER = timedelta(minutes=10)
UNGOVERNED_METRIC = "control.thermal_ungoverned"
FUSE_METRIC = "control.thermal_fuse"


def now_utc() -> datetime:
    return datetime.now(UTC)


# ---- 曲线 ----


class CurveError(ValueError):
    pass


def validate_nodes(nodes: Any) -> list[dict[str, int | float]]:
    """至少两个节点；温度 0–120 的有限数、不重复；转速 0–100 的整数。不修正、不截断，有问题就报错。"""
    if not isinstance(nodes, list) or len(nodes) < 2:
        raise CurveError("温控曲线至少需要两个节点")
    out: list[dict[str, int | float]] = []
    for n in nodes:
        if not isinstance(n, dict):
            raise CurveError("节点格式不正确")
        t, s = n.get("temp"), n.get("speed")
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not 0 <= t <= 120:
            raise CurveError("节点温度必须是 0–120 的数字")
        if isinstance(s, bool) or not isinstance(s, int) or not 0 <= s <= 100:
            raise CurveError("节点转速必须是 0–100 的整数")
        out.append({"temp": t, "speed": s})
    out.sort(key=lambda n: n["temp"])
    if len({n["temp"] for n in out}) != len(out):
        raise CurveError("节点温度不能重复")
    return out


def safety_temp(nodes: list[dict[str, Any]]) -> float:
    return float(nodes[-1]["temp"]) if nodes else DEFAULT_SAFETY_TEMP


def interpolate(nodes: list[dict[str, Any]], temp: float) -> int:
    """分段线性插值（与旧版相同）。结果不再做 5–100 的截断：节点本身就是合法转速。"""
    if temp <= nodes[0]["temp"]:
        return int(nodes[0]["speed"])
    for a, b in zip(nodes, nodes[1:]):
        if a["temp"] <= temp <= b["temp"]:
            f = (temp - a["temp"]) / (b["temp"] - a["temp"])
            return int(round(a["speed"] + f * (b["speed"] - a["speed"])))
    return int(nodes[-1]["speed"])


# ---- 旧版迁入 ----


def migrate_legacy(db: Session) -> dict[str, int]:
    """把导入器原样保存的旧版温控曲线与每台温控模式转成新的曲线与策略。只做一次，不覆盖人工修改。"""
    counts = {"curve": 0, "policies": 0, "pending": 0}
    m = db.scalar(select(LegacyMapping).where(LegacyMapping.legacy_kind == "curve_nodes", LegacyMapping.legacy_id == "global"))
    if m is not None and m.target_type == "pending":
        items = m.payload.get("items") if isinstance(m.payload, dict) else None
        try:
            nodes = validate_nodes([{"temp": n.get("temp"), "speed": n.get("speed")} for n in (items or [])])
        except (CurveError, AttributeError) as exc:
            audit.record(db, None, "thermal.legacy.pending", summary=f"旧版温控曲线无法安全转换，需人工确认：{exc}")
            m.target_type = "thermal_curve_pending"
            counts["pending"] += 1
        else:
            if db.scalar(select(ThermalCurve).where(ThermalCurve.key == "default")) is None:
                db.add(ThermalCurve(key="default", name="旧版全局曲线", nodes=nodes, source="legacy", updated_by="旧版导入"))
                counts["curve"] += 1
            m.target_type = "thermal_curve"
            audit.record(db, None, "thermal.legacy.migrate", summary=f"已迁入旧版温控曲线（{len(nodes)} 个节点，熔断温度 {safety_temp(nodes):g}℃）")
    has_curve = db.scalar(select(ThermalCurve.id).where(ThermalCurve.key == "default")) is not None
    for hm in db.scalars(select(LegacyMapping).where(LegacyMapping.legacy_kind == "hardware_node", LegacyMapping.target_id.is_not(None))):
        if db.scalar(select(ThermalPolicy.id).where(ThermalPolicy.asset_id == hm.target_id)) is not None:
            continue
        if db.get(Asset, hm.target_id) is None:
            continue
        rec = hm.payload if isinstance(hm.payload, dict) else {}
        old = str(rec.get("mode") or "auto")
        p = ThermalPolicy(asset_id=hm.target_id, mode="auto", source="legacy", legacy_mode=old, enabled=False, updated_by="旧版导入")
        if old == "auto":
            p.enabled = True
        elif old == "manual":
            try:
                speed = int(str(rec.get("manual_speed")).strip())
                if not 0 <= speed <= 100:
                    raise ValueError
            except ValueError:
                p.pending_note = f"旧版手动转速“{rec.get('manual_speed')}”无效，未迁入，需人工确认"
            else:
                p.mode, p.fixed_pwm, p.enabled = "fixed", speed, True
        elif old == "dynamic":
            if has_curve:
                p.mode, p.enabled = "curve", True
            else:
                p.pending_note = "旧版使用温控曲线，但曲线无法迁入，需人工确认"
        elif old == "preset":
            p.pending_note = f"旧版情景方案“{rec.get('preset_key') or ''}”按风扇通道分别设定转速，无法安全转换为新策略，需人工确认"
        else:
            p.pending_note = f"旧版温控模式“{old}”无法识别，需人工确认"
        if p.pending_note:
            counts["pending"] += 1
            audit.record(db, None, "thermal.legacy.pending", target_type="asset", target_id=hm.target_id, summary=p.pending_note)
        db.add(p)
        counts["policies"] += 1
    db.flush()
    return counts


# ---- 策略执行 ----


@dataclass
class Decision:
    action: str | None  # restore_auto / set_speed_all / None
    pwm: int | None
    message: str
    fuse: str | None = None  # 新的熔断状态


def decide(policy: ThermalPolicy, nodes: list[dict[str, Any]], temp: float | None, fresh: bool, target: ControlTarget | None) -> Decision:
    """纯函数：根据策略、曲线、最新 CPU 温度与已下发状态决定下一步。"""
    mode = target.thermal_mode if target else "unknown"
    applied = target.applied_pwm if target else None
    limit = safety_temp(nodes)
    if fresh and temp is not None:
        if temp >= limit:
            if policy.fuse_state == "tripped" and mode == "auto":
                return Decision(None, None, f"熔断中：CPU {temp:g}℃ ≥ {limit:g}℃，已交给原厂自动温控")
            return Decision("restore_auto", None, f"熔断：CPU {temp:g}℃ ≥ {limit:g}℃，请求恢复原厂自动温控", fuse="tripped")
        if policy.fuse_state == "tripped":
            if temp > limit - FUSE_HYSTERESIS:
                return Decision(None, None, f"熔断中：CPU {temp:g}℃，降到 {limit - FUSE_HYSTERESIS:g}℃ 以下才恢复策略")
            return Decision(None, None, f"CPU {temp:g}℃ 已回落，解除熔断，下一轮恢复策略", fuse="normal")
    elif policy.fuse_state == "tripped":
        return Decision(None, None, "熔断中，温度读数过期或缺失，保持原厂自动温控")
    want = policy.fixed_pwm if policy.mode == "fixed" else (interpolate(nodes, temp) if temp is not None else None)
    if want is None:
        return Decision(None, None, "没有可用的 CPU 温度，保持当前状态，不调速")
    if not fresh:
        if applied is None or mode != "manual":
            return Decision(None, None, "CPU 温度过期或缺失，且不知道当前已下发转速：不下发任何调速")
        if want < applied:
            return Decision(None, None, f"CPU 温度过期或缺失：拒绝降速（目标 {want}% < 当前 {applied}%）")
    if mode == "manual" and applied is not None and abs(want - applied) < MIN_STEP:
        return Decision(None, None, f"当前 {applied}%，目标 {want}%，变化小于 {MIN_STEP}%，不调整")
    return Decision("set_speed_all", want, f"按{'固定转速' if policy.mode == 'fixed' else '曲线'}调到 {want}%" + (f"（CPU {temp:g}℃）" if temp is not None else ""))


def _cpu_temp(db: Session, asset_id: uuid.UUID, now: datetime) -> tuple[float | None, bool]:
    for v in metrics.latest(db, asset_id, now=now):
        if v.key == "cpu.temp":
            return (v.value, v.quality == "valid" and v.value is not None)
    return None, False


def run_policies(db: Session, *, now: datetime | None = None, catalog: tuple[ThermalProfile, ...] = PRODUCTION_CATALOG) -> dict[str, int]:
    """Worker 定时调用。控制归属不是新平台时什么也不做（旧控制器负责）。"""
    now = now or now_utc()
    out = {"requested": 0, "skipped": 0, "ungoverned": 0}
    if not new_platform_controls(db):
        return out
    curves = {c.key: c.nodes for c in db.scalars(select(ThermalCurve))}
    for policy in db.scalars(select(ThermalPolicy).where(ThermalPolicy.enabled.is_(True), ThermalPolicy.mode.in_(("fixed", "curve")))):
        asset = db.get(Asset, policy.asset_id, options=[selectinload(Asset.endpoints)])
        if asset is None or asset.lifecycle != "active":
            continue
        nodes = curves.get(policy.curve_key) or []
        if policy.mode == "curve" and len(nodes) < 2:
            _note(policy, now, "曲线不存在或节点不足，不调速")
            continue
        target = thermal_target(db, asset)
        temp, fresh = _cpu_temp(db, asset.id, now)
        d = decide(policy, nodes if policy.mode == "curve" else (curves.get("default") or []), temp, fresh, target)
        if d.fuse:
            policy.fuse_state = d.fuse
        if d.action is None:
            _note(policy, now, d.message)
            out["skipped"] += 1
            continue
        params = {"pwm": d.pwm} if d.pwm is not None else {}
        try:
            thermal_gate(db, asset, d.action, params, catalog=catalog)
        except Rejected as exc:
            _note(policy, now, f"{d.message}，但无法执行：{exc}")
            out["ungoverned"] += 1
            continue
        req = f"{d.action}:{d.pwm}"
        if policy.last_request_key == req and policy.last_request_at and _aware(policy.last_request_at) > now - RETRY_AFTER:
            last = db.scalar(select(ControlOperation).where(ControlOperation.asset_id == asset.id, ControlOperation.kind == "thermal")
                             .order_by(ControlOperation.created_at.desc()).limit(1))
            if last is not None and last.status in ("rejected", "failed"):
                _note(policy, now, f"{d.message}；上次同样的请求{('被拒绝' if last.status == 'rejected' else '失败')}，10 分钟内不重复提交")
                continue
        request_thermal_system(db, asset.id, d.action, params, reason=d.message, catalog=catalog,
                               request_key=f"policy-{asset.id}-{uuid.uuid4().hex[:12]}")
        policy.last_request_key, policy.last_request_at = req, now
        _note(policy, now, d.message)
        out["requested"] += 1
    return out


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _note(policy: ThermalPolicy, now: datetime, message: str) -> None:
    if policy.last_decision != message:
        policy.last_decision = message
    policy.last_decision_at = now


def governance_metrics(db: Session, *, now: datetime | None = None, catalog: tuple[ThermalProfile, ...] = PRODUCTION_CATALOG) -> int:
    """写入“温控脱管”和“温控熔断”指标，交给告警规则判断。旧控制器负责时不写。

    脱管：新平台负责控制，但设备可能处于手动转速（旧版模式非自动，或新平台下发过手动 / 结果未知），
    而门禁不允许新平台执行策略 —— 没人在管这台设备的风扇。
    """
    now = now or now_utc()
    if not new_platform_controls(db):
        return 0
    n = 0
    for policy in db.scalars(select(ThermalPolicy)):
        asset = db.get(Asset, policy.asset_id, options=[selectinload(Asset.endpoints)])
        if asset is None or asset.lifecycle != "active":
            continue
        target = thermal_target(db, asset)
        maybe_manual = policy.legacy_mode in ("manual", "dynamic", "preset") or (target is not None and target.thermal_mode in ("manual", "unknown") and target.thermal_updated_at is not None)
        governed = True
        reason = ""
        if maybe_manual:
            action = "restore_auto" if policy.mode == "auto" or not policy.enabled else "set_speed_all"
            try:
                thermal_gate(db, asset, action, {"pwm": 100} if action == "set_speed_all" else {}, catalog=catalog)
            except Rejected as exc:
                if exc.code != "busy":
                    governed, reason = False, str(exc)
            if target is not None and target.state == "indeterminate":
                governed, reason = False, "上次控制结果不确定"
        msg = "" if governed else f"设备可能停在手动风扇转速，新平台无法接管：{reason}。请恢复原厂自动温控或人工处理"
        metrics.write_sample(db, asset.id, "control", [
            Point(UNGOVERNED_METRIC, 0.0 if governed else 1.0, "", message=msg),
            Point(FUSE_METRIC, 1.0 if policy.fuse_state == "tripped" else 0.0, "", message=policy.last_decision if policy.fuse_state == "tripped" else ""),
        ], observed_at=now, interval_seconds=60)
        n += 1
    return n
