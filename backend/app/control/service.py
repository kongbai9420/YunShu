"""控制服务：门禁、请求、执行与对账。API 和 Worker 都经过这里。

电源（kind=power）、风扇温控（kind=thermal）与 BMC 的 SNMP 设置（kind=bmc_snmp）共用同一套流程：基础门禁 → 各自的能力门禁 →
建操作并入队 → Worker 执行前再过一次门禁 → 目标互斥 → 执行 → 记录结果与审计。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import audit, queue
from app.collect.base import AdapterError, EndpointContext
from app.collect.runner import _context
from app.control.base import ControlError, target_key
from app import bmc_gate
from app.control.bmc_snmp import (
    BMC_SNMP_TASK, EVENTS_PATH, PUSH_ACTIONS, SNMP_ACTIONS, SNMP_CREDENTIAL_KINDS, ConfigClientFactory, RedfishConfigClient, SnmpProbe,
    bmc_snmp_enabled, default_probe, events_base_url, execute_bmc_push, execute_bmc_snmp, snmp_credential,
)
from app.control.guest import (
    DEFAULT_SHUTDOWN_TIMEOUT, GUEST_ACTIONS, GUEST_TASK, LINKED_POWER_TASK, GuestClientFactory, GuestRef, execute_guest,
    guest_power_enabled,
)
from app.control.guest import default_factory as default_guest_factory
from app.control.power import ACTIONS, PowerClientFactory, PyghmiPowerClient, execute_power
from app.control.thermal import (
    OPERATION_LABEL, PRODUCTION_CATALOG, GateDenied, PyghmiThermalClient, ThermalClientFactory, ThermalOperation, ThermalProfile,
    execute_thermal, validate_pwm,
)
from app.crypto import CredentialCipher
from app.models import (
    Asset, AssetRelation, BmcEventSubscription, ControlOperation, ControlSetting, ControlTarget, Credential, ManagementEndpoint, Task, User,
    VirtualizationObservation, VirtualizationSource,
)

SETTING_KEY = "default"
POWER_TASK = "control.power"
THERMAL_TASK = "control.thermal"
MAX_BATCH = 20
OPEN_STATES = ("queued", "running")
# 控制归属不是 new 时的统一提示
LEGACY_OWNER_MESSAGE = "控制仍由旧控制器负责：新平台在完成切换前不会向设备发送任何控制命令"


def now_utc() -> datetime:
    return datetime.now(UTC)


# ---- 控制归属 ----


def control_setting(db: Session) -> ControlSetting:
    row = db.get(ControlSetting, SETTING_KEY)
    if row is None:
        row = ControlSetting(key=SETTING_KEY, owner="legacy", revision=1, changed_by="", note="")
        db.add(row)
        db.flush()
    return row


def new_platform_controls(db: Session) -> bool:
    row = db.get(ControlSetting, SETTING_KEY)
    return row is not None and row.owner == "new"


# ---- 门禁 ----


class Rejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def control_endpoint(asset: Asset) -> ManagementEndpoint | None:
    """控制用的 IPMI 端点：优先用途为“控制”的，其次“管理”；“仅监控”的端点不用于控制。"""
    eps = [e for e in asset.endpoints if e.protocol == "ipmi" and e.purpose in ("control", "management")]
    eps.sort(key=lambda e: 0 if e.purpose == "control" else 1)
    return eps[0] if eps else None


@dataclass
class Gate:
    endpoint: ManagementEndpoint
    key: str


def base_gate(db: Session, asset: Asset, *, operation_id: uuid.UUID | None = None) -> Gate:
    """所有控制共用的检查；不满足时抛 Rejected，且没有任何设备调用。"""
    if not new_platform_controls(db):
        raise Rejected("legacy_owner", LEGACY_OWNER_MESSAGE)
    # 只允许“在用”：旧版里停用的设备导入后是“维护中”，旧版对它们零写，这里保持一致
    if asset.lifecycle != "active":
        raise Rejected("lifecycle", "资产不在“在用”状态（维护中、规划中、已退役都不允许控制）")
    ep = control_endpoint(asset)
    if ep is None:
        raise Rejected("no_endpoint", "资产没有可用于控制的 IPMI 端点（用途为“仅监控”的端点不能用于控制）")
    if ep.credential_id is None:
        raise Rejected("unconfigured", "IPMI 端点没有关联凭据")
    key = target_key(ep.address)
    if key is None:
        raise Rejected("bad_address", "IPMI 端点地址无效")
    _check_target(db, key, operation_id)
    return Gate(ep, key)


def _check_target(db: Session, key: str, operation_id: uuid.UUID | None) -> None:
    """同一控制目标：上次结果不确定时须先核对解除；有未完成的操作时不再排队。"""
    target = db.get(ControlTarget, key)
    if target is not None and target.state == "indeterminate":
        raise Rejected("indeterminate", "这台设备上一次控制结果不确定：请先核对设备实际状态，再在页面上确认解除")
    busy = db.scalar(
        select(ControlOperation.id).where(
            ControlOperation.target_key == key, ControlOperation.status.in_(OPEN_STATES),
            *([ControlOperation.id != operation_id] if operation_id else []),
        ).limit(1)
    )
    if busy is not None:
        raise Rejected("busy", "同一管理地址上还有未完成的控制操作，请等它结束")


def power_gate(db: Session, asset: Asset, action: str, *, operation_id: uuid.UUID | None = None) -> Gate:
    if action not in ACTIONS:
        raise Rejected("unknown_action", f"不支持的电源操作：{action}")
    return base_gate(db, asset, operation_id=operation_id)


@dataclass
class GuestGate(Gate):
    ref: GuestRef | None = None


def guest_observation(db: Session, asset: Asset) -> tuple[VirtualizationObservation, VirtualizationSource] | None:
    row = db.execute(
        select(VirtualizationObservation, VirtualizationSource)
        .join(VirtualizationSource, VirtualizationObservation.source_id == VirtualizationSource.id)
        .where(VirtualizationObservation.asset_id == asset.id, VirtualizationObservation.present.is_(True),
               VirtualizationObservation.ignored.is_(False))
        .order_by(VirtualizationObservation.observed_at.desc()).limit(1)
    ).first()
    return (row[0], row[1]) if row else None


def guest_gate(db: Session, asset: Asset, action: str, *, operation_id: uuid.UUID | None = None) -> GuestGate:
    """虚拟机 / 容器电源（4.2 P17）：经发现它的虚拟化平台端点下命令，不借用宿主 IPMI。"""
    if action not in GUEST_ACTIONS:
        raise Rejected("unknown_action", f"不支持的虚拟机电源操作：{action}")
    if not guest_power_enabled():
        raise Rejected("disabled", "虚拟机电源控制未开启：部署设置 YUNSHU_GUEST_POWER=1 后才可用")
    if not new_platform_controls(db):
        raise Rejected("legacy_owner", LEGACY_OWNER_MESSAGE)
    if asset.form not in ("vm", "container"):
        raise Rejected("not_guest", "只有虚拟机或容器资产可以通过虚拟化平台控制电源；物理机请用 IPMI 电源控制")
    if asset.lifecycle != "active":
        raise Rejected("lifecycle", "资产不在“在用”状态（从虚拟化发现新建的资产默认是“规划中”，确认后改为“在用”才能控制）")
    found = guest_observation(db, asset)
    if found is None:
        raise Rejected("no_source", "没有找到发现这台虚拟机的虚拟化平台记录：请先在“资源全景 → 虚拟资源发现与关联”里关联它")
    obs, source = found
    if source.identity_conflict:
        raise Rejected("identity", "虚拟化平台的身份发生了变化，关联已暂停：请重新确认后再控制")
    seen = db.get(ManagementEndpoint, source.endpoint_id)
    if seen is None:
        raise Rejected("no_endpoint", "发现这台虚拟机的平台端点已删除")
    # 只用同一地址、同一协议、用途为“控制 / 管理”的端点；“仅监控”的端点不用于控制（与 IPMI 规则一致）
    candidates = [e for e in db.scalars(select(ManagementEndpoint).where(ManagementEndpoint.asset_id == seen.asset_id))
                  if e.protocol == seen.protocol and e.address == seen.address and e.purpose in ("control", "management")]
    candidates.sort(key=lambda e: 0 if e.purpose == "control" else 1)
    platform = "PVE" if source.platform == "pve" else "vSphere"
    if not candidates:
        raise Rejected("no_endpoint", f"发现这台虚拟机的 {platform} 端点用途是“仅监控”：请在该端点所属资产上为同一地址添加用途为“控制”的端点，"
                                      "并关联有电源管理权限的凭据")
    ep = candidates[0]
    if ep.credential_id is None:
        raise Rejected("unconfigured", f"{platform} 控制端点没有关联凭据")
    key = f"guest:{source.id}:{obs.kind}:{obs.external_id}"[:300]
    _check_target(db, key, operation_id)
    return GuestGate(ep, key, GuestRef(platform=source.platform, kind=obs.kind, external_id=obs.external_id, node=obs.host_external_id))


def describe_guest(db: Session, asset: Asset) -> dict[str, Any]:
    found = guest_observation(db, asset)
    items = [(a.key, a.label, a.high_risk) for a in GUEST_ACTIONS.values()]
    return {
        "enabled": guest_power_enabled(),
        "source": {"platform": found[1].platform, "target": found[1].target, "power": found[0].power,
                   "observed_at": found[0].observed_at.isoformat()} if found else None,
        "actions": _actions_out(items, lambda k: guest_gate(db, asset, k)),
    }


def hosted_guests(db: Session, host_id: uuid.UUID) -> list[Asset]:
    """承载在这台宿主上的虚拟机 / 容器（hosts 关系），关宿主前联动用。"""
    ids = db.scalars(select(AssetRelation.target_asset_id).where(AssetRelation.source_asset_id == host_id, AssetRelation.kind == "hosts")).all()
    if not ids:
        return []
    # 回收站里的虚拟机不联动（界面上已看不到它）
    return list(db.scalars(select(Asset).where(Asset.id.in_(ids), Asset.form.in_(("vm", "container")), Asset.deleted_at.is_(None))
                           .order_by(Asset.name)))


@dataclass
class SnmpGate(Gate):
    credential_id: uuid.UUID | None = None
    snmp_ctx: EndpointContext | None = None
    push_target: str = ""  # Trap 目标：平台地址（部署设置），没设置时用之前配置时记下的地址


def redfish_endpoint(asset: Asset) -> ManagementEndpoint | None:
    """修改 BMC 设置用的 Redfish 端点：与 IPMI 控制相同，只用用途为“控制 / 管理”的，优先“控制”。"""
    eps = [e for e in asset.endpoints if e.protocol == "redfish" and e.purpose in ("control", "management")]
    eps.sort(key=lambda e: 0 if e.purpose == "control" else 1)
    return eps[0] if eps else None


def bmc_snmp_gate(db: Session, asset: Asset, action: str, params: dict[str, Any] | None, *,
                  operation_id: uuid.UUID | None = None) -> SnmpGate:
    """一键开启 / 关闭 BMC 的 SNMP（4.4 P19）。params 为 None 时（页面查看能力）不检查所选凭据。"""
    if action not in SNMP_ACTIONS:
        raise Rejected("unknown_action", f"不支持的 SNMP 操作：{action}")
    if not bmc_snmp_enabled():
        raise Rejected("disabled", "一键开启 SNMP 未开启：部署设置 YUNSHU_BMC_SNMP=1 后才可用")
    if not new_platform_controls(db):
        raise Rejected("legacy_owner", LEGACY_OWNER_MESSAGE)
    if asset.lifecycle != "active":
        raise Rejected("lifecycle", "资产不在“在用”状态（维护中、规划中、已退役都不允许修改设备设置）")
    ep = redfish_endpoint(asset)
    if ep is None:
        raise Rejected("no_endpoint", "资产没有用途为“控制”或“管理”的 Redfish 端点：开启 SNMP 要通过 Redfish 修改 BMC 设置")
    if ep.credential_id is None:
        raise Rejected("unconfigured", "Redfish 端点没有关联凭据")
    key = target_key(ep.address)
    if key is None:
        raise Rejected("bad_address", "Redfish 端点地址无效")
    _push_gate(db, asset, action)
    cred_id = None
    if action in ("enable", "trap_on") and params is not None:
        try:
            cred_id = uuid.UUID(str(params.get("credential_id")))
        except ValueError as exc:
            raise Rejected("credential", "请选择开启后平台用来采集的 SNMP 凭据" if action == "enable"
                           else "请选择 BMC 发送 Trap 时使用的团体名凭据") from exc
        cred = db.get(Credential, cred_id)
        if cred is None or cred.kind not in SNMP_CREDENTIAL_KINDS:
            raise Rejected("credential", "所选凭据不存在或不是 SNMP 凭据（团体名或 SNMPv3）")
        if action == "trap_on" and cred.kind != "snmp_community":
            raise Rejected("credential", "平台只接收 v1 / v2c Trap，请选择团体名凭据")
    _check_target(db, key, operation_id)
    return SnmpGate(ep, key, cred_id)


def _subscription(db: Session, asset_id: uuid.UUID, kind: str) -> BmcEventSubscription | None:
    return db.scalar(select(BmcEventSubscription).where(BmcEventSubscription.asset_id == asset_id, BmcEventSubscription.kind == kind))


def _push_gate(db: Session, asset: Asset, action: str) -> None:
    """4.4 P20c：推送目标需要平台这一端已经能接收。"""
    from app.snmp_trap import trap_enabled, trap_target

    if action == "trap_on" and not (trap_enabled() and trap_target()):
        raise Rejected("trap_disabled", "平台还没有打开 SNMP Trap 接收：部署设置 YUNSHU_SNMP_TRAP=1，并用 YUNSHU_SNMP_TRAP_TARGET "
                                        "填写 BMC 能访问到的平台 IP 后才可用")
    if action == "trap_off" and not trap_target() and _subscription(db, asset.id, "snmp_trap") is None:
        raise Rejected("trap_disabled", "平台没有在这台 BMC 上配置过 Trap 目标")
    if action == "events_on" and not events_base_url():
        raise Rejected("events_disabled", "没有设置事件接收地址：部署设置 YUNSHU_BMC_EVENTS_URL（BMC 能访问到的平台 HTTPS 地址）后才可用")
    if action == "events_off" and not events_base_url() and _subscription(db, asset.id, "redfish_event") is None:
        raise Rejected("events_disabled", "平台没有在这台 BMC 上订阅过 Redfish 事件")


def _subscription_out(sub: BmcEventSubscription | None) -> dict[str, Any] | None:
    if sub is None:
        return None
    return {"destination": sub.destination, "created_at": sub.created_at.isoformat(), "created_by": sub.created_by,
            "last_event_at": sub.last_event_at.isoformat() if sub.last_event_at else None, "events_received": sub.events_received}


def describe_bmc_snmp(db: Session, asset: Asset) -> dict[str, Any]:
    ep = redfish_endpoint(asset)
    items = [(a.key, a.label, a.high_risk) for a in SNMP_ACTIONS.values()]
    return {
        "enabled": bmc_snmp_enabled(),
        "endpoint": {"id": str(ep.id), "address": ep.address, "purpose": ep.purpose} if ep else None,
        "snmp_endpoints": [{"id": str(e.id), "address": e.address} for e in asset.endpoints if e.protocol == "snmp"],
        "actions": _actions_out(items, lambda k: bmc_snmp_gate(db, asset, k, None)),
        "note": "执行时先只读识别 BMC 的 SNMP 设置位置：Dell iDRAC、HPE iLO 和按 Redfish 标准提供 SNMP 设置的 BMC 可以自动开启；"
                "联想、新华三、浪潮等读不到设置的会被拒绝，并提示到 BMC 网页手动开启。",
        "push": _push_out(db, asset),
    }


def _push_out(db: Session, asset: Asset) -> dict[str, Any]:
    from app.snmp_trap import trap_enabled, trap_port, trap_target

    return {
        "trap": {"enabled": trap_enabled(), "target": trap_target(), "port": trap_port(),
                 "subscription": _subscription_out(_subscription(db, asset.id, "snmp_trap"))},
        "events": {"url": events_base_url(), "subscription": _subscription_out(_subscription(db, asset.id, "redfish_event"))},
    }


def thermal_params(action: str, params: dict[str, Any]) -> tuple[ThermalOperation, int | None]:
    try:
        op = ThermalOperation(action)
    except ValueError as exc:
        raise Rejected("unknown_action", f"不支持的温控操作：{action}") from exc
    if op != ThermalOperation.SET_SPEED_ALL:
        return op, None
    try:
        return op, validate_pwm(params.get("pwm"))
    except GateDenied as exc:
        raise Rejected(exc.code, str(exc)) from exc


def thermal_gate(
    db: Session, asset: Asset, action: str, params: dict[str, Any], *, catalog: tuple[ThermalProfile, ...],
    operation_id: uuid.UUID | None = None,
) -> Gate:
    thermal_params(action, params)
    g = base_gate(db, asset, operation_id=operation_id)
    if not catalog:
        raise Rejected("catalog_empty", "能力目录中还没有任何经过实机验证的机型，风扇控制默认拒绝")
    return g


def _actions_out(items: list[tuple[str, str, bool]], check: Callable[[str], Gate]) -> list[dict[str, Any]]:
    out = []
    for key, label, high in items:
        try:
            check(key)
            out.append({"key": key, "label": label, "high_risk": high, "allowed": True, "reason": ""})
        except Rejected as exc:
            out.append({"key": key, "label": label, "high_risk": high, "allowed": False, "reason": str(exc), "code": exc.code})
    return out


def _target_out(db: Session, asset: Asset) -> dict[str, Any]:
    ep = control_endpoint(asset)
    key = target_key(ep.address) if ep else None
    t = db.get(ControlTarget, key) if key else None
    return {
        "endpoint": {"id": str(ep.id), "address": ep.address, "purpose": ep.purpose} if ep else None,
        "target": {
            "key": key, "state": t.state if t else "ok", "message": t.message if t else "",
            "thermal_mode": t.thermal_mode if t else "unknown", "applied_pwm": t.applied_pwm if t else None,
            "thermal_updated_at": t.thermal_updated_at.isoformat() if t and t.thermal_updated_at else None,
        } if key else None,
    }


def describe_power(db: Session, asset: Asset) -> dict[str, Any]:
    """页面用：这台资产能做哪些电源操作，不能做的原因。"""
    items = [(a.key, a.label, a.high_risk) for a in ACTIONS.values()]
    return {**_target_out(db, asset), "actions": _actions_out(items, lambda k: power_gate(db, asset, k))}


THERMAL_ACTIONS = [(ThermalOperation.RESTORE_AUTO.value, OPERATION_LABEL[ThermalOperation.RESTORE_AUTO], False),
                   (ThermalOperation.SET_SPEED_ALL.value, OPERATION_LABEL[ThermalOperation.SET_SPEED_ALL], True)]


def describe_thermal(db: Session, asset: Asset, catalog: tuple[ThermalProfile, ...]) -> dict[str, Any]:
    probe = {ThermalOperation.SET_SPEED_ALL.value: {"pwm": 100}}
    return {
        **_target_out(db, asset), "catalog_size": len(catalog),
        "actions": _actions_out(THERMAL_ACTIONS, lambda k: thermal_gate(db, asset, k, probe.get(k, {}), catalog=catalog)),
        "note": "能否控制最终取决于执行时读取到的设备厂商、型号与固件是否与能力目录中经实机验证的画像一致；资产上填写的品牌型号不作为依据。",
    }


# ---- 请求 ----


def action_label(op: ControlOperation) -> str:
    if op.kind == "power" and op.action in ACTIONS:
        return ACTIONS[op.action].label
    if op.kind == "thermal":
        try:
            label = OPERATION_LABEL[ThermalOperation(op.action)]
        except ValueError:
            return op.action
        return label + (f" {op.params['pwm']}%" if op.params.get("pwm") is not None else "")
    if op.kind == "bmc_snmp" and op.action in SNMP_ACTIONS:
        return SNMP_ACTIONS[op.action].label
    return op.action


def operation_out(op: ControlOperation) -> dict[str, Any]:
    return {
        "id": str(op.id), "asset_id": str(op.asset_id) if op.asset_id else None, "asset_name": op.asset_name,
        "kind": op.kind, "action": op.action, "action_label": action_label(op), "params": op.params,
        "status": op.status, "message": op.message, "observed": op.observed, "requested_by": op.requested_by,
        "reason": op.reason, "batch_id": str(op.batch_id) if op.batch_id else None, "target_key": op.target_key,
        "created_at": op.created_at.isoformat(), "started_at": op.started_at.isoformat() if op.started_at else None,
        "finished_at": op.finished_at.isoformat() if op.finished_at else None,
    }


def confirm_phrase(names: list[str]) -> str:
    """高风险操作要求输入的确认文字：单台为资产名称，多台为“N 台”。"""
    return names[0] if len(names) == 1 else f"{len(names)} 台"


def _request(
    db: Session, actor: User | None, asset_ids: list[uuid.UUID], *, kind: str, task: str, action: str, params: dict[str, Any],
    label: str, high_risk: bool, confirm: str, reason: str, request_key: str, gate: Callable[[Asset], Gate],
    system_name: str = "",
) -> dict[str, Any]:
    """为每台资产创建一个控制操作。门禁不通过的记为 rejected（不入队）。

    request_key 由页面在打开确认框时生成；同一个 key 重复提交（双击、网络重试）返回同一批操作，不重复执行。
    """
    ids = list(dict.fromkeys(asset_ids))
    if not ids:
        raise Rejected("empty", "没有选择资产")
    if len(ids) > MAX_BATCH:
        raise Rejected("too_many", f"一次最多 {MAX_BATCH} 台")
    if not request_key or len(request_key) > 100:
        raise Rejected("request_key", "缺少请求标识")
    existing = db.scalars(select(ControlOperation).where(ControlOperation.request_key.like(f"{request_key}:%"))).all()
    if existing:
        return {"batch_id": str(existing[0].batch_id), "duplicate": True, "operations": [operation_out(o) for o in existing]}
    assets = {a.id: a for a in db.scalars(select(Asset).where(Asset.id.in_(ids)).options(selectinload(Asset.endpoints)))}
    if any(i not in assets for i in ids):
        raise Rejected("not_found", "部分资产不存在，操作已拒绝")
    names = [assets[i].name for i in ids]
    # 系统发起（温控策略）的请求没有人工确认环节，由策略本身和门禁约束
    if high_risk and actor is not None and confirm.strip() != confirm_phrase(names):
        raise Rejected("confirm", f"高风险操作需要输入“{confirm_phrase(names)}”确认")
    batch_id = uuid.uuid4()
    ops: list[ControlOperation] = []
    seen_keys: set[str] = set()
    for i in ids:
        a = assets[i]
        op = ControlOperation(
            asset_id=a.id, asset_name=a.name, kind=kind, action=action, params=params, status="queued", message="", observed={},
            requested_by_id=actor.id if actor else None,
            requested_by=(actor.display_name or actor.username) if actor else system_name, reason=reason[:500],
            batch_id=batch_id, request_key=f"{request_key}:{a.id}",
        )
        try:
            g = gate(a)
            if g.key in seen_keys:
                raise Rejected("busy", "同一批里有另一台资产使用同一个管理地址，只执行第一台")
            seen_keys.add(g.key)
            op.endpoint_id, op.target_key = g.endpoint.id, g.key
            op.message = "已排队，等待执行"
        except Rejected as exc:
            op.status, op.message, op.finished_at = "rejected", str(exc), now_utc()
        db.add(op)
        db.flush()
        if op.status == "queued":
            t = queue.enqueue(db, task, asset_id=a.id, payload={"operation_id": str(op.id)}, risk="control",
                              idempotency_key=f"control:{op.id}", priority=20)
            op.task_id = t.id
        audit.record(
            db, actor, f"control.{kind}.request", target_type="asset", target_id=a.id,
            summary=f"请求{label}：{'已排队' if op.status == 'queued' else '已拒绝 — ' + op.message}",
            detail={"operation_id": str(op.id), "action": action, "params": params, "batch_id": str(batch_id), "reason": reason[:500]},
        )
        ops.append(op)
    return {"batch_id": str(batch_id), "duplicate": False, "operations": [operation_out(o) for o in ops]}


def request_power(
    db: Session, actor: User, asset_ids: list[uuid.UUID], action: str, *, confirm: str, reason: str, request_key: str,
    guests_first: bool = False, guest_timeout: int = DEFAULT_SHUTDOWN_TIMEOUT,
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise Rejected("unknown_action", f"不支持的电源操作：{action}")
    act = ACTIONS[action]
    if not guests_first or action == "on":
        return _request(db, actor, asset_ids, kind="power", task=POWER_TASK, action=action, params={}, label=act.label,
                        high_risk=act.high_risk, confirm=confirm, reason=reason, request_key=request_key,
                        gate=lambda a: power_gate(db, a, action))
    # 宿主联动（4.2 P17）：先正常关闭其上的虚拟机，全部确认关闭后再执行宿主操作；任一台没关成，宿主操作取消
    if not guest_power_enabled():
        raise Rejected("disabled", "先关虚拟机再关宿主需要开启虚拟机电源控制（YUNSHU_GUEST_POWER=1）")
    if not 30 <= guest_timeout <= 1800:
        raise Rejected("timeout", "等待虚拟机关机的时间需在 30–1800 秒之间")
    if high_risk_confirm_missing(act.high_risk, actor, confirm, db, asset_ids):
        raise Rejected("confirm", "高风险操作需要输入确认语（宿主名称），确认错误时不会关闭任何虚拟机")
    # 先检查每台宿主自己能不能控制：有一台不行就整批不做，免得宿主没关、虚拟机却被关了
    check_host_gates(db, asset_ids, lambda a: power_gate(db, a, action))
    guests =list({g.id: g for h in dict.fromkeys(asset_ids) for g in hosted_guests(db, h)}.values())
    guest_batch = None
    if guests:
        res = _request(db, actor, [g.id for g in guests], kind="guest_power", task=GUEST_TASK, action="shutdown",
                       params={"timeout": guest_timeout, "for_host": True}, label="虚拟机正常关机（宿主联动）", high_risk=False,
                       confirm="", reason=f"关闭宿主前联动：{reason}", request_key=f"{request_key}-guests",
                       gate=lambda a: guest_gate(db, a, "shutdown"))
        guest_batch = res["batch_id"]
    host = _request(db, actor, asset_ids, kind="power", task=LINKED_POWER_TASK if guest_batch else POWER_TASK, action=action,
                    params={"after_batch": guest_batch, "wait": guest_timeout + 60} if guest_batch else {}, label=act.label,
                    high_risk=act.high_risk, confirm=confirm, reason=reason, request_key=request_key,
                    gate=lambda a: power_gate(db, a, action))
    host["guest_batch_id"] = guest_batch
    host["guests"] = [{"id": str(g.id), "name": g.name} for g in guests]
    return host


def check_host_gates(db: Session, asset_ids: list[uuid.UUID], gate: Callable[[Asset], Gate]) -> None:
    """宿主联动排队虚拟机关机之前，宿主门禁必须全部通过（同一批里管理地址也不能重复）。"""
    ids = list(dict.fromkeys(asset_ids))
    assets = {a.id: a for a in db.scalars(select(Asset).where(Asset.id.in_(ids)).options(selectinload(Asset.endpoints)))}
    if any(i not in assets for i in ids):
        raise Rejected("not_found", "部分资产不存在，操作已拒绝")
    keys: set[str] = set()
    for i in ids:
        try:
            g = gate(assets[i])
        except Rejected as exc:
            raise Rejected(exc.code, f"宿主 {assets[i].name} 不能执行：{exc}。没有关闭任何虚拟机") from None
        if g.key in keys:
            raise Rejected("busy", f"宿主 {assets[i].name} 与同一批里另一台宿主使用同一个管理地址。没有关闭任何虚拟机")
        keys.add(g.key)


def high_risk_confirm_missing(high_risk: bool, actor: User | None, confirm: str, db: Session, asset_ids: list[uuid.UUID]) -> bool:
    """宿主联动先排队虚拟机关机：先校验宿主的确认文字，避免确认错了却已经关了虚拟机。"""
    if not high_risk or actor is None:
        return False
    ids = list(dict.fromkeys(asset_ids))
    by_id = {a.id: a.name for a in db.scalars(select(Asset).where(Asset.id.in_(ids)))}
    # 与 _request 一样按请求里的顺序拼确认语
    return confirm.strip() != confirm_phrase([by_id[i] for i in ids if i in by_id])


def request_guest_power(
    db: Session, actor: User, asset_ids: list[uuid.UUID], action: str, *, confirm: str, reason: str, request_key: str,
    timeout: int = DEFAULT_SHUTDOWN_TIMEOUT,
) -> dict[str, Any]:
    if action not in GUEST_ACTIONS:
        raise Rejected("unknown_action", f"不支持的虚拟机电源操作：{action}")
    if not 30 <= timeout <= 1800:
        raise Rejected("timeout", "等待时间需在 30–1800 秒之间")
    act = GUEST_ACTIONS[action]
    return _request(db, actor, asset_ids, kind="guest_power", task=GUEST_TASK, action=action, params={"timeout": timeout},
                    label=f"虚拟机{act.label}", high_risk=act.high_risk, confirm=confirm, reason=reason, request_key=request_key,
                    gate=lambda a: guest_gate(db, a, action))


def request_thermal(
    db: Session, actor: User, asset_ids: list[uuid.UUID], action: str, params: dict[str, Any], *, confirm: str, reason: str,
    request_key: str, catalog: tuple[ThermalProfile, ...] = PRODUCTION_CATALOG,
) -> dict[str, Any]:
    op, pwm = thermal_params(action, params)
    clean = {"pwm": pwm} if pwm is not None else {}
    label = OPERATION_LABEL[op] + (f" {pwm}%" if pwm is not None else "")
    return _request(db, actor, asset_ids, kind="thermal", task=THERMAL_TASK, action=op.value, params=clean, label=label,
                    high_risk=op == ThermalOperation.SET_SPEED_ALL, confirm=confirm, reason=reason, request_key=request_key,
                    gate=lambda a: thermal_gate(db, a, op.value, clean, catalog=catalog))


def request_bmc_snmp(
    db: Session, actor: User, asset_ids: list[uuid.UUID], action: str, *, credential_id: uuid.UUID | None, confirm: str, reason: str,
    request_key: str,
) -> dict[str, Any]:
    if action not in SNMP_ACTIONS:
        raise Rejected("unknown_action", f"不支持的 SNMP 操作：{action}")
    act = SNMP_ACTIONS[action]
    params = {"credential_id": str(credential_id)} if action in ("enable", "trap_on") and credential_id else {}
    return _request(db, actor, asset_ids, kind="bmc_snmp", task=BMC_SNMP_TASK, action=action, params=params, label=act.label,
                    high_risk=act.high_risk, confirm=confirm, reason=reason, request_key=request_key,
                    gate=lambda a: bmc_snmp_gate(db, a, action, params))


SYSTEM_POLICY = "温控策略（自动）"


def request_thermal_system(
    db: Session, asset_id: uuid.UUID, action: str, params: dict[str, Any], *, reason: str, request_key: str,
    catalog: tuple[ThermalProfile, ...] = PRODUCTION_CATALOG,
) -> dict[str, Any]:
    """温控策略（曲线、熔断）发起的下发：与人工请求同样经过门禁、互斥、任务与审计。"""
    op, pwm = thermal_params(action, params)
    clean = {"pwm": pwm} if pwm is not None else {}
    label = OPERATION_LABEL[op] + (f" {pwm}%" if pwm is not None else "")
    return _request(db, None, [asset_id], kind="thermal", task=THERMAL_TASK, action=op.value, params=clean, label=label,
                    high_risk=False, confirm="", reason=reason, request_key=request_key, system_name=SYSTEM_POLICY,
                    gate=lambda a: thermal_gate(db, a, op.value, clean, catalog=catalog))


# ---- 执行（Worker）----

# 同一进程内的目标锁：超时后执行线程可能还在跑，新任务不能在它结束前碰同一台设备
_local_locks: dict[str, threading.Lock] = {}
_local_guard = threading.Lock()


def _local_lock(key: str) -> threading.Lock:
    with _local_guard:
        return _local_locks.setdefault(key, threading.Lock())


@dataclass
class RunResult:
    status: str
    message: str
    observed: dict[str, Any] = field(default_factory=dict)
    # 设备接受了温控命令后的模式：auto / manual；None 表示不改（失败、不确定时由 _finish 记为未知）
    thermal_mode: str | None = None
    applied_pwm: int | None = None


def _finish(db: Session, op: ControlOperation, r: RunResult) -> None:
    op.status, op.message, op.finished_at = r.status, r.message[:2000], now_utc()
    if r.observed:
        op.observed = r.observed
    target = db.get(ControlTarget, op.target_key) if op.target_key else None
    if target is not None:
        target.last_operation_id = op.id
        if r.status == "indeterminate":
            target.state, target.message = "indeterminate", r.message[:2000]
        if op.kind == "thermal":
            # 只有设备接受了命令才推进“已下发”状态；失败或不确定时模式记为未知，不保留旧值冒充
            if r.thermal_mode is not None:
                target.thermal_mode, target.applied_pwm, target.thermal_updated_at = r.thermal_mode, r.applied_pwm, now_utc()
            elif r.status in ("failed", "indeterminate"):
                target.thermal_mode, target.applied_pwm, target.thermal_updated_at = "unknown", None, now_utc()
    audit.record(
        db, None, f"control.{op.kind}.result", target_type="asset", target_id=op.asset_id or "",
        summary=f"{action_label(op)}（{op.requested_by} 请求）：{r.status} — {r.message[:200]}",
        detail={"operation_id": str(op.id), "status": r.status},
    )


def _check_requester_scope(db: Session, op: ControlOperation, asset_id: uuid.UUID) -> None:
    """执行前按发起人当前的资产范围复核（4.1 P11）：撤销后未执行的操作被拒绝，不发送命令。"""
    from app.scope import can_see, scope_for

    requester = db.get(User, op.requested_by_id) if op.requested_by_id else None
    if requester is None or not requester.is_active or not can_see(db, scope_for(db, requester), asset_id):
        raise Rejected("scope", "发起人已无权控制该资产（账号被停用或资产范围已收窄）")


def _claim_target(db: Session, key: str, op_id: uuid.UUID) -> bool:
    if db.get(ControlTarget, key) is None:
        try:
            with db.begin_nested():
                db.add(ControlTarget(key=key, state="ok", message=""))
        except IntegrityError:
            pass  # 多个 Worker 同时第一次控制同一地址（4.1 P9）：另一方已建好，下面的条件更新决定谁执行
    res = db.execute(
        update(ControlTarget).where(ControlTarget.key == key, ControlTarget.busy_operation_id.is_(None), ControlTarget.state == "ok")
        .values(busy_operation_id=op_id)
    )
    return res.rowcount == 1


def _release_target(db: Session, key: str, op_id: uuid.UUID) -> None:
    db.execute(update(ControlTarget).where(ControlTarget.key == key, ControlTarget.busy_operation_id == op_id).values(busy_operation_id=None))


Executor = Callable[[EndpointContext, ControlOperation, Gate], RunResult]
GateCheck = Callable[[Session, Asset, ControlOperation], Gate]


def _make_handler(cipher_factory: Callable[[], CredentialCipher], gate: GateCheck, run: Executor,
                  on_result: Callable[[Session, ControlOperation, RunResult], None] | None = None):  # type: ignore[no-untyped-def]
    from app.worker import TaskContext, TaskError

    def handler(ctx: TaskContext) -> dict[str, Any]:
        try:
            op_id = uuid.UUID(str(ctx.lease.payload.get("operation_id")))
        except ValueError as exc:
            raise TaskError("控制任务缺少操作编号", retryable=False) from exc
        with ctx.session_factory() as db:
            op = db.get(ControlOperation, op_id)
            if op is None or op.status != "queued":
                raise TaskError("控制操作不存在或已处理", retryable=False)
            after_batch, wait = op.params.get("after_batch"), float(op.params.get("wait") or 0)
        if after_batch:
            # 宿主联动：等其上虚拟机的关机操作全部结束且都确认关闭才继续；否则不发送宿主命令
            refused = _wait_for_guests(ctx, uuid.UUID(str(after_batch)), wait)
            if refused:
                with ctx.session_factory() as db:
                    op = db.get(ControlOperation, op_id)
                    _finish(db, op, RunResult("rejected", refused))
                    db.commit()
                return {"status": "rejected"}
        with ctx.session_factory() as db:
            op = db.get(ControlOperation, op_id)
            if op is None or op.status != "queued":
                raise TaskError("控制操作不存在或已处理", retryable=False)
            asset = db.get(Asset, op.asset_id, options=[selectinload(Asset.endpoints)]) if op.asset_id else None
            # 执行前再过一次门禁：排队期间可能切回了旧控制器、资产被退役、设备进入了不确定状态，或发起人的范围被收窄
            try:
                if asset is None:
                    raise Rejected("not_found", "资产已删除")
                _check_requester_scope(db, op, asset.id)
                g = gate(db, asset, op)
                if g.key != op.target_key:
                    raise Rejected("changed", "排队期间控制端点已改变，请重新发起")
            except Rejected as exc:
                _finish(db, op, RunResult("rejected", f"执行前检查未通过，没有发送任何命令：{exc}"))
                db.commit()
                return {"status": "rejected"}
            lock = _local_lock(op.target_key)
            got = lock.acquire(blocking=False)
            if not got or not _claim_target(db, op.target_key, op.id):
                if got:
                    lock.release()
                _finish(db, op, RunResult("rejected", "同一管理地址上另一个控制操作仍在执行，没有发送任何命令"))
                db.commit()
                return {"status": "rejected"}
            op.status, op.started_at, op.message = "running", now_utc(), "正在执行"
            try:
                ectx = _context(db, cipher_factory(), g.endpoint, g.endpoint.protocol)
            except AdapterError as exc:
                _release_target(db, op.target_key, op.id)
                lock.release()
                _finish(db, op, RunResult("failed", f"读取凭据失败，没有发送任何命令：{exc}"))
                db.commit()
                return {"status": "failed"}
            db.commit()
        try:
            # 4.4 P20b：与采集、获取资料共用每台 BMC 的队列；等正在进行的采集结束（最多 90 秒），等不到不发命令
            bmc_addresses = [g.endpoint.address] if g.endpoint.protocol in bmc_gate.BMC_PROTOCOLS else []
            engine = bmc_gate.engine_of(ctx.session_factory)
            with bmc_gate.hold(engine, bmc_addresses, "control", wait=bmc_gate.CONTROL_WAIT):
                bmc_gate.allow(engine, g.endpoint.address, g.endpoint.protocol, "control")
                result = run(ectx, op, g)
        except bmc_gate.BmcBusy as exc:
            result = RunResult("rejected", f"{exc}，等待超过 {int(bmc_gate.CONTROL_WAIT)} 秒，没有发送任何命令")
        except GateDenied as exc:
            # 能力门禁在只读识别之后拒绝：没有任何写调用
            result = RunResult("rejected", f"设备识别后能力检查未通过，没有发送任何控制命令：{exc}")
        except ControlError as exc:
            if exc.kind == "timeout":
                result = RunResult("indeterminate", f"{exc}。不会自动重试；请核对设备实际状态后在页面上确认解除")
            else:
                result = RunResult("failed", str(exc))
        except Exception as exc:  # noqa: BLE001 - 适配器内部错误：无法确定是否已发送 → 不确定
            result = RunResult("indeterminate", f"执行出错（{type(exc).__name__}），无法确定命令是否已送达：{str(exc)[:200]}")
        finally:
            lock.release()
        # 租约已被回收（执行超时）：任务已是“不确定”，对账会处理，这里不覆盖结果
        still_owner = ctx.heartbeat()
        with ctx.session_factory() as db:
            op = db.get(ControlOperation, op_id)
            _release_target(db, op.target_key, op.id)
            if op.status == "running" and still_owner:
                if on_result is not None:
                    on_result(db, op, result)
                _finish(db, op, result)
            db.commit()
        return {"status": result.status}

    return handler


GUEST_POLL_SECONDS = 3.0


def _wait_for_guests(ctx, batch_id: uuid.UUID, wait: float) -> str:  # type: ignore[no-untyped-def]
    """空串表示可以继续，否则是拒绝原因。等待期间 Worker 照常续租。"""
    deadline = time.monotonic() + max(wait, 1)
    while True:
        with ctx.session_factory() as db:
            ops = db.scalars(select(ControlOperation).where(ControlOperation.batch_id == batch_id)).all()
        if not any(o.status in OPEN_STATES for o in ops):
            bad = [o for o in ops if o.status != "verified"]
            if not bad:
                return ""
            names = "、".join(f"{o.asset_name}（{o.message[:60]}）" for o in bad[:5])
            return f"其上虚拟机没有全部确认关闭，宿主操作已取消，没有发送任何命令：{names}"
        if time.monotonic() > deadline:
            return f"等待其上虚拟机关机超过 {int(wait)} 秒，宿主操作已取消，没有发送任何命令"
        time.sleep(GUEST_POLL_SECONDS)


def make_guest_handler(cipher_factory: Callable[[], CredentialCipher], client_factory: GuestClientFactory | None = None):  # type: ignore[no-untyped-def]
    factory = client_factory or default_guest_factory

    def run(ectx: EndpointContext, op: ControlOperation, g: Gate) -> RunResult:
        if not isinstance(g, GuestGate) or g.ref is None:
            raise ControlError("unsupported", "内部错误：缺少虚拟机定位信息")
        out = execute_guest(ectx, g.ref, GUEST_ACTIONS[op.action], factory=factory,
                            timeout=float(op.params.get("timeout") or DEFAULT_SHUTDOWN_TIMEOUT))
        return RunResult(out.status, out.message, out.observed)

    return _make_handler(cipher_factory, lambda db, a, op: guest_gate(db, a, op.action, operation_id=op.id), run)


def make_bmc_snmp_handler(cipher_factory: Callable[[], CredentialCipher], client_factory: ConfigClientFactory | None = None,
                          probe: SnmpProbe | None = None):  # type: ignore[no-untyped-def]
    factory = client_factory or RedfishConfigClient

    def gate(db: Session, a: Asset, op: ControlOperation) -> Gate:
        from app.crypto import DecryptionError
        from app.services.credentials import reveal_secret

        g = bmc_snmp_gate(db, a, op.action, op.params, operation_id=op.id)
        if op.action in ("trap_on", "trap_off"):
            from app.snmp_trap import trap_target

            sub = _subscription(db, a.id, "snmp_trap")
            g.push_target = trap_target() or (sub.destination if sub else "")
        cred = db.get(Credential, g.credential_id) if g.credential_id else None
        if cred is not None:
            try:
                secret = reveal_secret(cred, cipher_factory())
            except DecryptionError as exc:
                raise Rejected("credential", f"SNMP 凭据“{cred.alias}”无法解密") from exc
            g.snmp_ctx = EndpointContext(address=g.endpoint.address, port=None, protocol="snmp", username=cred.username, secret=secret,
                                         credential_kind=cred.kind)
        return g

    # 事件订阅令牌只在执行期间留在内存里，结果写库时存哈希（4.4 P20c）
    secrets_for: dict[uuid.UUID, tuple[str, str]] = {}

    def run(ectx: EndpointContext, op: ControlOperation, g: Gate) -> RunResult:
        sctx = g.snmp_ctx if isinstance(g, SnmpGate) else None
        cred = snmp_credential(sctx.credential_kind, sctx.username, sctx.secret) if sctx else None
        if op.action in PUSH_ACTIONS:
            target = g.push_target if isinstance(g, SnmpGate) else ""
            token = "ysb_" + secrets.token_urlsafe(32)
            secrets_for[op.id] = (token, cred.community if cred else "")
            out = execute_bmc_push(ectx, op.action, cred=cred, target=target, events_url=events_base_url(), token=token, factory=factory)
            out.observed["target"] = out.observed.get("target") or target
            return RunResult(out.status, out.message, out.observed)
        out = execute_bmc_snmp(ectx, op.action, cred=cred, snmp_ctx=sctx, factory=factory, probe=probe or default_probe)
        return RunResult(out.status, out.message, out.observed)

    def record_push(db: Session, op: ControlOperation, r: RunResult) -> None:
        token, community = secrets_for.pop(op.id, ("", ""))
        if op.asset_id is None:
            return
        kind = "redfish_event" if op.action.startswith("events") else "snmp_trap"
        sub = _subscription(db, op.asset_id, kind)
        if op.action in ("trap_off", "events_off"):
            if sub is not None and r.status == "verified":
                db.delete(sub)
            return
        # 超时（结果不确定）时 BMC 可能已经建好订阅：也保存令牌，推送到达时能认出来
        if r.status not in ("verified", "accepted", "indeterminate"):
            return
        if sub is None:
            sub = BmcEventSubscription(asset_id=op.asset_id, kind=kind)
            db.add(sub)
        sub.created_at, sub.created_by, sub.remote_id = now_utc(), op.requested_by, str(r.observed.get("remote") or r.observed.get("slot") or "")[:300]
        sub.last_event_at, sub.events_received = None, 0
        if kind == "redfish_event":
            sub.destination, sub.token_hash = events_base_url() + EVENTS_PATH, hashlib.sha256(token.encode()).hexdigest()
        else:
            # Trap 用团体名认来源：存哈希，资产上没有对应的团体名凭据端点时也能接收
            sub.destination, sub.token_hash = str(r.observed.get("target") or ""), hashlib.sha256(community.encode()).hexdigest()

    def add_endpoint(db: Session, op: ControlOperation, r: RunResult) -> None:
        """开启并读通后，在资产上补一个 SNMP 监控端点（同地址已有的不改动），之后按采集模板定时采集。"""
        if op.action in PUSH_ACTIONS:
            record_push(db, op, r)
            return
        if op.action != "enable" or r.status != "verified" or not op.params.get("credential_id"):
            return
        bmc = db.get(ManagementEndpoint, op.endpoint_id) if op.endpoint_id else None
        if bmc is None:
            return
        exists = db.scalar(select(ManagementEndpoint.id).where(ManagementEndpoint.asset_id == bmc.asset_id, ManagementEndpoint.protocol == "snmp",
                                                               ManagementEndpoint.address == bmc.address).limit(1))
        if exists is not None:
            r.message += "。资产上已有这个地址的 SNMP 端点，未改动"
            return
        db.add(ManagementEndpoint(asset_id=bmc.asset_id, address=bmc.address, port=None, protocol="snmp", purpose="monitoring",
                                  credential_id=uuid.UUID(op.params["credential_id"]), notes="一键开启 SNMP 后自动添加"))
        r.observed["endpoint_added"] = True
        r.message += "。已在资产上添加 SNMP 监控端点"

    return _make_handler(cipher_factory, gate, run, add_endpoint)


def make_power_handler(cipher_factory: Callable[[], CredentialCipher], client_factory: PowerClientFactory | None = None, **verify: Any):  # type: ignore[no-untyped-def]
    factory = client_factory or PyghmiPowerClient

    def run(ectx: EndpointContext, op: ControlOperation, _g: Gate) -> RunResult:
        out = execute_power(ectx, ACTIONS[op.action], factory=factory, **verify)
        return RunResult(out.status, out.message, out.observed)

    return _make_handler(cipher_factory, lambda db, a, op: power_gate(db, a, op.action, operation_id=op.id), run)


def make_thermal_handler(
    cipher_factory: Callable[[], CredentialCipher], client_factory: ThermalClientFactory | None = None,
    catalog: tuple[ThermalProfile, ...] = PRODUCTION_CATALOG, drivers: dict[str, Any] | None = None,
):  # type: ignore[no-untyped-def]
    factory = client_factory or PyghmiThermalClient

    def run(ectx: EndpointContext, op: ControlOperation, _g: Gate) -> RunResult:
        top, pwm = thermal_params(op.action, op.params)
        out = execute_thermal(ectx, top, pwm, catalog=catalog, factory=factory, drivers=drivers)
        return RunResult(out.status, out.message, out.observed, thermal_mode=out.mode, applied_pwm=out.pwm)

    gate: GateCheck = lambda db, a, op: thermal_gate(db, a, op.action, op.params, catalog=catalog, operation_id=op.id)  # noqa: E731
    return _make_handler(cipher_factory, gate, run)


def reconcile(db: Session) -> int:
    """任务已结束但操作还停在“执行中 / 排队”的（Worker 超时、崩溃、取消）：按任务结果收尾。"""
    n = 0
    rows = db.execute(
        select(ControlOperation, Task).join(Task, Task.id == ControlOperation.task_id)
        .where(ControlOperation.status.in_(OPEN_STATES), Task.status.in_(("failed", "indeterminate", "cancelled")))
    ).all()
    for op, task in rows:
        if op.status == "queued" and task.status in ("failed", "cancelled"):
            _finish(db, op, RunResult("cancelled" if task.status == "cancelled" else "failed", task.error or "任务未执行"))
        else:
            _finish(db, op, RunResult("indeterminate", f"执行超时或 Worker 中断（{task.error[:200]}），命令可能已送达。不会自动重试；请核对设备实际状态后确认解除"))
            if not _local_lock(op.target_key).locked():
                _release_target(db, op.target_key, op.id)
        n += 1
    return n


def acknowledge_target(db: Session, actor: User, key: str, note: str) -> ControlTarget:
    target = db.get(ControlTarget, key)
    if target is None or target.state != "indeterminate":
        raise Rejected("not_indeterminate", "这个管理目标没有处于“结果不确定”状态")
    if not note.strip():
        raise Rejected("note", "请说明核对结果（例如：已登录 BMC 确认电源已关闭）")
    target.state, target.message = "ok", ""
    audit.record(db, actor, "control.target.acknowledge", target_type="control_target", target_id=key[:64],
                 summary=f"确认解除“结果不确定”：{note.strip()[:200]}")
    return target
