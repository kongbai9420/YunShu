"""风扇与温控能力门禁。

- 默认拒绝：生产能力目录为空。没有经实机验证、登记在目录里的机型，一律拒绝风扇控制，零写调用。
- 识别只信设备：执行前用只读命令读取 FRU 厂商 / 型号和 BMC 固件版本，与目录比对。
  资产上用户填写的品牌、型号、“已验证”等字段不能解锁控制。
- 画像只引用审核过的驱动编号（driver_id），不保存任何 raw 命令模板。
- 转速不在画像允许的安全区间内直接拒绝，绝不静默改成保底值后下发。
- 驱动多步骤操作逐步检查，中间任何一步失败整体失败；超时为“结果不确定”，不换协议重试。
- 风扇转速变化不能证明模式或占空比已生效：设备不支持回读控制模式时，结果标为“已接受，无法确认”。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from app.collect.base import EndpointContext
from app.control.base import ControlError


class ThermalOperation(str, Enum):
    RESTORE_AUTO = "restore_auto"
    SET_SPEED_ALL = "set_speed_all"


OPERATION_LABEL = {ThermalOperation.RESTORE_AUTO: "恢复原厂自动温控", ThermalOperation.SET_SPEED_ALL: "全局固定转速"}


class EvidenceSource(str, Enum):
    BUILTIN_AUDITED = "builtin_audited"  # 仓库审核并有实机实验记录
    STANDALONE_FIXTURE = "standalone_fixture"  # 只在离线测试里注入
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class Evidence:
    source: EvidenceSource
    audit_ref: str = "<AUDIT_REF_PLACEHOLDER>"
    note: str = ""


def _version_tuple(v: str) -> tuple[int, ...] | None:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts) if parts else None


@dataclass(frozen=True)
class ThermalProfile:
    profile_id: str
    vendor: str  # 小写
    model_patterns: tuple[str, ...]  # 匹配 FRU 产品名的正则
    firmware_min: str
    firmware_max: str
    driver_id: str
    operations: frozenset[ThermalOperation]
    safe_min_pwm: int
    safe_max_pwm: int
    evidence: Evidence

    def firmware_ok(self, firmware: str) -> bool:
        v, lo, hi = _version_tuple(firmware), _version_tuple(self.firmware_min), _version_tuple(self.firmware_max)
        return v is not None and lo is not None and hi is not None and lo <= v <= hi

    def model_ok(self, model: str) -> bool:
        return any(re.fullmatch(p, model or "", re.IGNORECASE) for p in self.model_patterns)


# 生产能力目录：为空即默认拒绝一切风扇控制。只有经过实机验证并留有实验记录的机型才能登记进来。
PRODUCTION_CATALOG: tuple[ThermalProfile, ...] = ()


@dataclass(frozen=True)
class Identity:
    vendor: str
    model: str
    firmware: str

    def as_dict(self) -> dict[str, str]:
        return {"vendor": self.vendor, "model": self.model, "firmware": self.firmware}


class GateDenied(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def match_profile(catalog: tuple[ThermalProfile, ...], identity: Identity) -> ThermalProfile:
    """按设备只读识别结果在目录里找画像；找不到或证据不足时抛 GateDenied。"""
    if not catalog:
        raise GateDenied("catalog_empty", "能力目录中还没有任何经过实机验证的机型，风扇控制默认拒绝")
    vendor = identity.vendor.strip().lower()
    if not vendor:
        raise GateDenied("vendor_missing", "设备没有报告厂商信息，拒绝风扇控制")
    candidates = [p for p in catalog if p.vendor == vendor or vendor.startswith(p.vendor)]
    if not candidates:
        raise GateDenied("vendor_unknown", f"厂商“{identity.vendor}”不在能力目录中，拒绝风扇控制（不会尝试其他厂商的命令）")
    models = [p for p in candidates if p.model_ok(identity.model)]
    if not models:
        raise GateDenied("model_unknown", f"型号“{identity.model}”不在能力目录中，拒绝风扇控制")
    fw = [p for p in models if p.firmware_ok(identity.firmware)]
    if not fw:
        raise GateDenied("firmware_unsupported", f"BMC 固件“{identity.firmware or '未知'}”不在已验证范围内，拒绝风扇控制")
    p = fw[0]
    if p.evidence.source == EvidenceSource.UNVERIFIED:
        raise GateDenied("not_verified", f"画像 {p.profile_id} 没有实机验证证据，拒绝风扇控制")
    return p


def check_operation(profile: ThermalProfile, op: ThermalOperation, pwm: int | None) -> None:
    if op not in profile.operations:
        raise GateDenied("operation_unauthorized", f"画像 {profile.profile_id} 未授权“{OPERATION_LABEL[op]}”")
    if op == ThermalOperation.SET_SPEED_ALL:
        if pwm is None:
            raise GateDenied("pwm_missing", "缺少目标转速")
        if pwm < profile.safe_min_pwm:
            raise GateDenied("pwm_below_safe", f"目标转速 {pwm}% 低于该机型的安全下限 {profile.safe_min_pwm}%，拒绝执行（不会自动改成下限）")
        if pwm > profile.safe_max_pwm:
            raise GateDenied("pwm_above_safe", f"目标转速 {pwm}% 高于该机型允许的上限 {profile.safe_max_pwm}%，拒绝执行")


def validate_pwm(value: Any) -> int:
    """只接受 0–100 的整数；小数、NaN、越界一律报错，不截断。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GateDenied("pwm_invalid", "目标转速必须是 0 到 100 的整数百分比")
    if not 0 <= value <= 100:
        raise GateDenied("pwm_invalid", "目标转速必须在 0 到 100 之间")
    return value


# ---- 设备客户端与驱动 ----


class ThermalClient(Protocol):
    def identity(self) -> Identity:
        """只读：FRU 厂商 / 型号与 BMC 固件版本。"""
        ...

    def raw(self, netfn: int, command: int, data: list[int]) -> None:
        """发送一条命令；超时抛 ControlError(timeout)，设备返回错误抛 ControlError(protocol)。"""
        ...

    def close(self) -> None: ...


ThermalClientFactory = Callable[[EndpointContext], ThermalClient]


class PyghmiThermalClient:
    def __init__(self, ctx: EndpointContext) -> None:
        if not ctx.username or not ctx.secret:
            raise ControlError("unconfigured", "IPMI 端点没有关联用户名和密码的凭据")
        try:
            from pyghmi.ipmi import command as cmd

            self.cmd = cmd.Command(bmc=ctx.address, userid=ctx.username, password=ctx.secret, port=ctx.port or 623, keepalive=False)
        except ImportError as exc:
            raise ControlError("unsupported", "没有安装 pyghmi，无法执行 IPMI 风扇控制") from exc
        except Exception as exc:  # noqa: BLE001 - 登录阶段失败：控制命令还没发出
            low = str(exc).lower()
            if "unauthorized" in low or "password" in low or "auth" in low or "rakp" in low:
                raise ControlError("auth", "BMC 用户名或密码不正确") from exc
            raise ControlError("unreachable", f"无法登录 BMC：{str(exc)[:200]}") from exc

    def identity(self) -> Identity:
        try:
            info = self.cmd.get_inventory_of_component("System") or {}
            dev = self.cmd.xraw_command(netfn=6, command=1)  # Get Device ID，只读
            data = bytes(dev["data"])
            firmware = f"{data[2] & 0x7F}.{data[3]:02x}" if len(data) >= 4 else ""
        except Exception as exc:  # noqa: BLE001
            raise ControlError("protocol", f"读取设备识别信息失败：{str(exc)[:200]}") from exc
        return Identity(str(info.get("Manufacturer") or ""), str(info.get("Product name") or ""), firmware)

    def raw(self, netfn: int, command: int, data: list[int]) -> None:
        try:
            # retry=False：控制命令不重发
            res = self.cmd.raw_command(netfn=netfn, command=command, data=data, retry=False)
        except Exception as exc:  # noqa: BLE001
            low = str(exc).lower()
            if "timeout" in low or "timed out" in low or "no response" in low:
                raise ControlError("timeout", "风扇控制命令已发出但 BMC 没有应答，设备当前状态未知") from exc
            raise ControlError("protocol", f"BMC 拒绝了风扇控制命令：{str(exc)[:200]}") from exc
        if isinstance(res, dict) and res.get("code"):
            raise ControlError("protocol", f"BMC 返回错误码 0x{int(res['code']):02x}：{res.get('error', '')}")

    def close(self) -> None:
        try:
            self.cmd.ipmi_session.logout()
        except Exception:  # noqa: BLE001
            pass


@dataclass
class DriverResult:
    steps: list[str] = field(default_factory=list)


class Driver(Protocol):
    def restore_auto(self, client: ThermalClient) -> DriverResult: ...

    def set_speed_all(self, client: ThermalClient, pwm: int) -> DriverResult: ...


class DellIdracDriver:
    """Dell iDRAC 风扇手动 / 自动切换（与旧版 src/ipmi_core.py 相同的命令）。

    只有登记在能力目录里、经实机验证的 Dell 机型才会用到；生产目录为空时不可达。
    """

    def restore_auto(self, client: ThermalClient) -> DriverResult:
        client.raw(0x30, 0x30, [0x01, 0x01])
        return DriverResult(["恢复自动模式"])

    def set_speed_all(self, client: ThermalClient, pwm: int) -> DriverResult:
        r = DriverResult()
        client.raw(0x30, 0x30, [0x01, 0x00])
        r.steps.append("切换手动模式")
        # 第一步成功、第二步失败时整体失败：设备停在手动模式，调用方据此标记并提示恢复自动
        try:
            client.raw(0x30, 0x30, [0x02, 0xFF, pwm])
        except ControlError as exc:
            raise ControlError(exc.kind, f"已切换到手动模式，但设定转速失败：{exc}。设备可能停在手动模式，请恢复原厂自动温控") from exc
        r.steps.append(f"全局转速 {pwm}%")
        return r


DRIVERS: dict[str, Driver] = {"dell_idrac_raw_v1": DellIdracDriver()}


@dataclass
class ThermalOutcome:
    status: str  # verified / unconfirmed
    message: str
    observed: dict[str, Any]
    mode: str  # auto / manual
    pwm: int | None


def execute_thermal(
    ctx: EndpointContext,
    op: ThermalOperation,
    pwm: int | None,
    *,
    catalog: tuple[ThermalProfile, ...],
    factory: ThermalClientFactory = PyghmiThermalClient,
    drivers: dict[str, Driver] | None = None,
) -> ThermalOutcome:
    """只读识别 → 匹配画像 → 检查操作与转速 → 驱动执行。门禁不通过抛 GateDenied（没有写调用）。"""
    if not catalog:
        raise GateDenied("catalog_empty", "能力目录中还没有任何经过实机验证的机型，风扇控制默认拒绝")
    client = factory(ctx)
    try:
        ident = client.identity()
        profile = match_profile(catalog, ident)
        check_operation(profile, op, pwm)
        driver = (drivers or DRIVERS).get(profile.driver_id)
        if driver is None:
            raise GateDenied("driver_missing", f"画像引用的驱动 {profile.driver_id} 不存在")
        res = driver.restore_auto(client) if op == ThermalOperation.RESTORE_AUTO else driver.set_speed_all(client, pwm or 0)
        observed = {"identity": ident.as_dict(), "profile": profile.profile_id, "steps": res.steps}
        label = OPERATION_LABEL[op] + (f" {pwm}%" if pwm is not None else "")
        # 已登记的驱动都不能回读控制模式，所以只能是“已接受，无法确认”
        msg = f"{label}：BMC 已接受；该机型不支持回读控制模式，无法确认已生效（风扇转速变化不能证明已生效）"
        return ThermalOutcome("unconfirmed", msg, observed, "auto" if op == ThermalOperation.RESTORE_AUTO else "manual", pwm)
    finally:
        client.close()
