"""风扇与温控：按设备读出的厂商 / 型号 / BMC 固件匹配风扇命令。

- 识别只信设备：执行前用只读命令读取厂商、型号和 BMC 固件版本，再到命令库里匹配。
  资产上用户填写的品牌、型号等字段不参与匹配。
- 命令库 = 管理员自定义命令（优先）+ 内置命令。内置命令整理自厂商文档与社区资料，大多没有在本项目实机验证，
  结果里会注明来源；匹配不到的设备一律拒绝，不会挨个厂商去试。
- 转速不在命令允许的安全区间内直接拒绝，绝不静默改成保底值后下发。
- 多步骤命令逐步检查，中间任何一步失败整体失败；超时为“结果不确定”，不换协议重试。
- IPMI 命令无法回读控制模式，结果标为“已接受，无法确认”；Redfish 修改后会再读一次，读到才算“已查询确认”。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Protocol

from app.collect.base import EndpointContext
from app.control.base import ControlError


class ThermalOperation(str, Enum):
    RESTORE_AUTO = "restore_auto"
    SET_SPEED_ALL = "set_speed_all"


OPERATION_LABEL = {ThermalOperation.RESTORE_AUTO: "恢复原厂自动温控", ThermalOperation.SET_SPEED_ALL: "全局固定转速"}
BOTH = frozenset({ThermalOperation.RESTORE_AUTO, ThermalOperation.SET_SPEED_ALL})


class EvidenceSource(str, Enum):
    BUILTIN_AUDITED = "builtin_audited"  # 本项目实机验证过
    COMMUNITY = "community"  # 整理自厂商文档或社区资料，未经本项目实机验证
    CUSTOM = "custom"  # 管理员自定义
    STANDALONE_FIXTURE = "standalone_fixture"  # 只在离线测试里注入
    UNVERIFIED = "unverified"


SOURCE_LABEL = {
    EvidenceSource.BUILTIN_AUDITED: "内置，已实机验证",
    EvidenceSource.COMMUNITY: "内置，来自厂商文档或社区资料，未经本项目实机验证",
    EvidenceSource.CUSTOM: "管理员自定义",
    EvidenceSource.STANDALONE_FIXTURE: "测试用",
    EvidenceSource.UNVERIFIED: "未验证",
}


@dataclass(frozen=True)
class Evidence:
    source: EvidenceSource
    audit_ref: str = ""
    note: str = ""


def _version_tuple(v: str) -> tuple[int, ...] | None:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts) if parts else None


PWM = "pwm"  # 命令数据里的转速占位


@dataclass(frozen=True)
class RawStep:
    netfn: int
    command: int
    data: tuple[int | str, ...]

    def bytes_for(self, value: int) -> list[int]:
        return [value if b == PWM else int(b) for b in self.data]

    def text(self) -> str:
        return " ".join([f"0x{self.netfn:02x}", f"0x{self.command:02x}", *("{pwm}" if b == PWM else f"0x{int(b):02x}" for b in self.data)])


@dataclass(frozen=True)
class ThermalProfile:
    profile_id: str
    vendor: str  # 小写；设备厂商以它开头即算匹配
    model_patterns: tuple[str, ...]  # 匹配型号的正则（整串匹配）
    firmware_min: str  # 空表示不限
    firmware_max: str
    driver_id: str
    operations: frozenset[ThermalOperation]
    safe_min_pwm: int
    safe_max_pwm: int
    evidence: Evidence
    transport: str = "ipmi"  # ipmi / redfish
    label: str = ""
    vendor_aliases: tuple[str, ...] = ()
    restore_steps: tuple[RawStep, ...] = ()
    set_steps: tuple[RawStep, ...] = ()
    pwm_scale: str = "percent"  # percent：0–100；byte：0–255
    description: str = ""

    def firmware_ok(self, firmware: str) -> bool:
        if not self.firmware_min and not self.firmware_max:
            return True
        v = _version_tuple(firmware)
        if v is None:
            return False
        lo, hi = _version_tuple(self.firmware_min), _version_tuple(self.firmware_max)
        return (lo is None or lo <= v) and (hi is None or v <= hi)

    def model_ok(self, model: str) -> bool:
        return any(re.fullmatch(p, model or "", re.IGNORECASE) for p in self.model_patterns)

    def vendor_ok(self, vendor: str) -> bool:
        return any(vendor == v or vendor.startswith(v) for v in (self.vendor, *self.vendor_aliases))

    def device_pwm(self, pwm: int) -> int:
        return round(pwm * 255 / 100) if self.pwm_scale == "byte" else pwm


def _step(text: str) -> RawStep:
    parts = text.split()
    return RawStep(int(parts[0], 16), int(parts[1], 16), tuple(PWM if p == "{pwm}" else int(p, 16) for p in parts[2:]))


COMMUNITY = Evidence(EvidenceSource.COMMUNITY)

# 内置命令库。来源与限制写在 description 里；资料不足以确定命令的厂商（联想 XCC、浪潮、新华三、华擎等）不内置，
# 需要时由管理员按自己核实过的命令自定义。
PRODUCTION_CATALOG: tuple[ThermalProfile, ...] = (
    ThermalProfile(
        profile_id="dell-idrac-ipmi", label="Dell iDRAC 7 / 8 / 9（固件 3.30.30.30 及以前）", vendor="dell",
        model_patterns=(r".*",), firmware_min="", firmware_max="3.30.30.30", driver_id="ipmi_raw_v1", operations=BOTH,
        safe_min_pwm=20, safe_max_pwm=100, evidence=COMMUNITY,
        restore_steps=(_step("0x30 0x30 0x01 0x01"),),
        set_steps=(_step("0x30 0x30 0x01 0x00"), _step("0x30 0x30 0x02 0xff {pwm}")),
        description="Dell 的 OEM 命令：先切到手动，再设定全部风扇的转速百分比。iDRAC9 3.34.34.34 起 Dell 移除了这组命令，改用下面的 Redfish 方式。",
    ),
    ThermalProfile(
        profile_id="dell-idrac9-redfish", label="Dell iDRAC9（固件 3.34.34.34 起）", vendor="dell",
        model_patterns=(r".*",), firmware_min="3.34", firmware_max="", driver_id="dell_redfish_min_fan_v1", operations=BOTH,
        safe_min_pwm=5, safe_max_pwm=100, evidence=COMMUNITY, transport="redfish",
        description="通过 Redfish 修改 iDRAC 属性 ThermalSettings.1.MinimumFanSpeed（最低转速）。这是下限：BMC 仍会按温度调得更高，"
                    "不能压到 BMC 要求的转速以下；低于设备报告的 MFSMinimumLimit 会被拒绝。恢复原厂温控即把下限改回 MFSMinimumLimit。",
    ),
    ThermalProfile(
        profile_id="supermicro-ipmi", label="超微 X10 / X11 / X12 / X13、H11 / H12 / H13 主板", vendor="supermicro",
        vendor_aliases=("super micro",), model_patterns=(r"(?!X9).*",), firmware_min="", firmware_max="", driver_id="ipmi_raw_v1",
        operations=BOTH, safe_min_pwm=25, safe_max_pwm=100, evidence=COMMUNITY,
        restore_steps=(_step("0x30 0x45 0x01 0x02"),),
        set_steps=(_step("0x30 0x45 0x01 0x01"), _step("0x30 0x70 0x66 0x01 0x00 {pwm}"), _step("0x30 0x70 0x66 0x01 0x01 {pwm}")),
        description="先把风扇模式设为 Full（BMC 不再调速），再分别设定 CPU 区（0）和外设区（1）的占空比（0–100）。"
                    "恢复原厂温控设为 Optimal 模式。X9 主板占空比的刻度不同，不在此列。",
    ),
    ThermalProfile(
        profile_id="huawei-ibmc-redfish", label="华为 / 超聚变 iBMC", vendor="huawei", vendor_aliases=("xfusion",),
        model_patterns=(r".*",), firmware_min="", firmware_max="", driver_id="huawei_redfish_fan_v1", operations=BOTH,
        safe_min_pwm=20, safe_max_pwm=100, evidence=COMMUNITY, transport="redfish",
        description="通过 Redfish 修改机箱 Thermal 资源 Oem 里的 FanSpeedAdjustmentMode（Manual / Automatic）和 FanSpeedLevelPercents。"
                    "旧机型的 IPMI 风扇命令在部分固件上会返回错误，所以只用 Redfish。BMC 重启或服务器下电后会自动回到自动模式。",
    ),
    ThermalProfile(
        profile_id="hpe-ilo-redfish", label="HPE iLO 5 / 6", vendor="hpe", vendor_aliases=("hp",),
        model_patterns=(r".*",), firmware_min="", firmware_max="", driver_id="hpe_redfish_cooling_v1", operations=BOTH,
        safe_min_pwm=0, safe_max_pwm=100, evidence=COMMUNITY, transport="redfish",
        description="iLO 不提供按百分比调速，只能选散热档位（Oem.Hpe.ThermalConfiguration）：低于 50% 为“最佳散热”（即原厂自动），"
                    "50–89% 为“增强散热”，90% 及以上为“最大散热”。",
    ),
)


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


TRANSPORT_LABEL = {"ipmi": "IPMI", "redfish": "Redfish"}


def match_profile(catalog: tuple[ThermalProfile, ...], identity: Identity) -> ThermalProfile:
    """按设备只读识别结果在命令库里找命令（按顺序，先匹配先用）；找不到时抛 GateDenied。"""
    if not catalog:
        raise GateDenied("catalog_empty", "风扇命令库是空的，拒绝风扇控制")
    vendor = identity.vendor.strip().lower()
    if not vendor:
        raise GateDenied("vendor_missing", "设备没有报告厂商信息，拒绝风扇控制")
    candidates = [p for p in catalog if p.vendor_ok(vendor)]
    if not candidates:
        raise GateDenied("vendor_unknown", f"厂商“{identity.vendor}”没有可用的风扇命令（不会尝试其他厂商的命令），可由管理员添加自定义命令")
    models = [p for p in candidates if p.model_ok(identity.model)]
    if not models:
        raise GateDenied("model_unknown", f"型号“{identity.model}”没有可用的风扇命令，可由管理员添加自定义命令")
    fw = [p for p in models if p.firmware_ok(identity.firmware)]
    if not fw:
        raise GateDenied("firmware_unsupported", f"BMC 固件“{identity.firmware or '未知'}”不在任何风扇命令的适用范围内")
    p = fw[0]
    if p.evidence.source == EvidenceSource.UNVERIFIED:
        raise GateDenied("not_verified", f"风扇命令 {p.profile_id} 没有来源说明，拒绝风扇控制")
    return p


def check_operation(profile: ThermalProfile, op: ThermalOperation, pwm: int | None) -> None:
    if op not in profile.operations:
        raise GateDenied("operation_unauthorized", f"风扇命令 {profile.profile_id} 不支持“{OPERATION_LABEL[op]}”")
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


# ---- 自定义命令的解析 ----

# 只允许 OEM 段的 netfn（0x2C–0x3F），不能借风扇命令发送电源、用户、网络等标准命令
CUSTOM_NETFN = range(0x2C, 0x40)
MAX_STEPS = 6
MAX_DATA = 32


def parse_commands(text: str, *, needs_pwm: bool) -> tuple[RawStep, ...]:
    """每行一条，写法同 ipmitool raw：netfn command data...，数字可写 0x1f 或 31，{pwm} 为转速占位。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise ValueError("至少要有一条命令")
    if len(lines) > MAX_STEPS:
        raise ValueError(f"最多 {MAX_STEPS} 条命令")
    steps = []
    for n, line in enumerate(lines, 1):
        parts = line.split()
        if parts[0].lower() == "raw":
            parts = parts[1:]
        if len(parts) < 2:
            raise ValueError(f"第 {n} 行至少要有 netfn 和 command 两个数")
        values: list[int | str] = []
        for p in parts:
            if p.lower() == "{pwm}":
                values.append(PWM)
                continue
            try:
                v = int(p, 16) if p.lower().startswith("0x") else int(p)
            except ValueError as exc:
                raise ValueError(f"第 {n} 行的“{p}”不是数字（可写 0x1f 或 31，转速写 {{pwm}}）") from exc
            if not 0 <= v <= 0xFF:
                raise ValueError(f"第 {n} 行的“{p}”超出一个字节（0–255）")
            values.append(v)
        if values[0] == PWM or values[1] == PWM:
            raise ValueError(f"第 {n} 行：{{pwm}} 只能出现在数据部分")
        if int(values[0]) not in CUSTOM_NETFN:
            raise ValueError(f"第 {n} 行：netfn 0x{int(values[0]):02x} 不允许，风扇命令只能用 OEM 段 0x2c–0x3f")
        if len(values) - 2 > MAX_DATA:
            raise ValueError(f"第 {n} 行数据超过 {MAX_DATA} 个字节")
        steps.append(RawStep(int(values[0]), int(values[1]), tuple(values[2:])))
    has_pwm = any(PWM in s.data for s in steps)
    if needs_pwm and not has_pwm:
        raise ValueError("设定转速的命令里要有 {pwm}")
    if not needs_pwm and has_pwm:
        raise ValueError("恢复原厂温控的命令里不能有 {pwm}")
    return tuple(steps)


def custom_profile(row: Any) -> ThermalProfile:
    """把数据库里的自定义命令转成命令库条目；解析失败的条目不参与匹配。"""
    return ThermalProfile(
        profile_id=f"custom:{row.id}", label=row.name, vendor=row.vendor.strip().lower(),
        model_patterns=(row.model_pattern.strip() or ".*",), firmware_min=row.firmware_min.strip(), firmware_max=row.firmware_max.strip(),
        driver_id="ipmi_raw_v1", operations=BOTH, safe_min_pwm=row.safe_min_pwm, safe_max_pwm=row.safe_max_pwm,
        evidence=Evidence(EvidenceSource.CUSTOM, note=row.note), restore_steps=parse_commands(row.restore_commands, needs_pwm=False),
        set_steps=parse_commands(row.set_commands, needs_pwm=True), pwm_scale=row.pwm_scale, description=row.note,
    )


# ---- 设备客户端 ----


class ThermalClient(Protocol):
    def identity(self) -> Identity:
        """只读：FRU 厂商 / 型号与 BMC 固件版本。"""
        ...

    def raw(self, netfn: int, command: int, data: list[int]) -> None:
        """发送一条命令；超时抛 ControlError(timeout)，设备返回错误抛 ControlError(protocol)。"""
        ...

    def close(self) -> None: ...


ThermalClientFactory = Callable[[EndpointContext], ThermalClient]


class RedfishClient(Protocol):
    def get(self, path: str) -> tuple[dict[str, Any], str]: ...

    def patch(self, path: str, body: dict[str, Any], etag: str) -> None: ...

    def close(self) -> None: ...


RedfishClientFactory = Callable[[EndpointContext], RedfishClient]


def default_redfish_factory(ctx: EndpointContext) -> RedfishClient:
    from app.control.bmc_snmp import RedfishConfigClient

    return RedfishConfigClient(ctx)


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


def _first_member(c: RedfishClient, path: str) -> str:
    data, _ = c.get(path)
    for m in data.get("Members") or []:
        if isinstance(m, dict) and m.get("@odata.id"):
            return str(m["@odata.id"])
    return ""


def redfish_identity(c: RedfishClient) -> Identity:
    """只读：Systems 的厂商、型号与 Managers 的固件版本。"""
    system = _first_member(c, "/redfish/v1/Systems")
    manager = _first_member(c, "/redfish/v1/Managers")
    s = c.get(system)[0] if system else {}
    m = c.get(manager)[0] if manager else {}
    return Identity(str(s.get("Manufacturer") or ""), str(s.get("Model") or ""), str(m.get("FirmwareVersion") or ""))


# ---- 驱动 ----


@dataclass
class DriverResult:
    steps: list[str] = field(default_factory=list)
    verified: bool = False
    mode: str = ""  # auto / manual；空表示按操作推断
    pwm: int | None = None
    note: str = ""


class IpmiRawDriver:
    """按命令库里的 raw 命令逐条发送。第一条之后失败时设备可能停在手动模式，提示恢复。"""

    def run(self, client: ThermalClient, profile: ThermalProfile, op: ThermalOperation, pwm: int | None) -> DriverResult:
        steps = profile.restore_steps if op == ThermalOperation.RESTORE_AUTO else profile.set_steps
        if not steps:
            raise GateDenied("operation_unauthorized", f"风扇命令 {profile.profile_id} 没有“{OPERATION_LABEL[op]}”的命令")
        value = profile.device_pwm(pwm or 0)
        r = DriverResult(pwm=pwm)
        for i, s in enumerate(steps):
            try:
                client.raw(s.netfn, s.command, s.bytes_for(value))
            except ControlError as exc:
                if i == 0:
                    raise
                raise ControlError(exc.kind, f"前 {i} 条命令已执行，第 {i + 1} 条失败：{exc}。设备可能停在手动模式，请恢复原厂自动温控") from exc
            r.steps.append(s.text().replace("{pwm}", str(value)))
        return r


def _rf_patch(c: RedfishClient, path: str, body: dict[str, Any], etag: str) -> None:
    try:
        c.patch(path, body, etag)
    except ControlError as exc:
        if exc.kind == "timeout":
            raise ControlError("timeout", "风扇设置已发出但 BMC 没有应答，设备当前状态未知") from exc
        if exc.kind == "auth":
            raise ControlError("auth", "Redfish 账号没有修改风扇设置的权限") from exc
        raise


DELL_ATTRIBUTE_PATHS = ("/redfish/v1/Managers/iDRAC.Embedded.1/Oem/Dell/DellAttributes/System.Embedded.1",
                        "/redfish/v1/Managers/System.Embedded.1/Attributes")
MFS = "ThermalSettings.1.MinimumFanSpeed"
MFS_MIN = "ThermalSettings.1.MFSMinimumLimit"


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class DellRedfishMinFanDriver:
    def run(self, c: RedfishClient, profile: ThermalProfile, op: ThermalOperation, pwm: int | None) -> DriverResult:
        for path in DELL_ATTRIBUTE_PATHS:
            data, etag = c.get(path)
            attrs = data.get("Attributes") or {}
            if MFS in attrs:
                break
        else:
            raise GateDenied("unsupported", "这台 iDRAC 的 Redfish 没有 ThermalSettings.1.MinimumFanSpeed 属性（可能是许可证或固件不支持）")
        floor = _int(attrs.get(MFS_MIN)) or profile.safe_min_pwm
        if op == ThermalOperation.SET_SPEED_ALL and (pwm or 0) < floor:
            raise GateDenied("pwm_below_safe", f"目标转速 {pwm}% 低于设备允许的最低值 {floor}%（MFSMinimumLimit），拒绝执行")
        value = floor if op == ThermalOperation.RESTORE_AUTO else int(pwm or 0)
        _rf_patch(c, path, {"Attributes": {MFS: value}}, etag)
        after = _int((c.get(path)[0].get("Attributes") or {}).get(MFS))
        note = "" if op == ThermalOperation.RESTORE_AUTO else "这是最低转速下限，BMC 仍会按温度调得更高"
        return DriverResult([f"{MFS} = {value}"], verified=after == value, pwm=pwm, note=note)


def _thermal_resource(c: RedfishClient) -> tuple[str, dict[str, Any], str]:
    chassis = _first_member(c, "/redfish/v1/Chassis")
    if not chassis:
        raise GateDenied("unsupported", "Redfish 里没有机箱资源，无法设置风扇")
    data, _ = c.get(chassis)
    path = str((data.get("Thermal") or {}).get("@odata.id") or f"{chassis.rstrip('/')}/Thermal")
    thermal, etag = c.get(path)
    if not thermal:
        raise GateDenied("unsupported", "Redfish 里没有 Thermal 资源，无法设置风扇")
    return path, thermal, etag


# 手动模式的超时时间（秒）：iBMC 会在超时后自动回到自动模式。只改模式和转速时 iBMC 会把它设成很短（约 30 秒），
# 所以一并写入一个很大的值，与其他厂商“设了就保持”的行为一致；BMC 重启或服务器下电后仍会回到自动模式。
HUAWEI_MANUAL_TIMEOUT = 100_000_000


class HuaweiRedfishFanDriver:
    def run(self, c: RedfishClient, profile: ThermalProfile, op: ThermalOperation, pwm: int | None) -> DriverResult:
        path, thermal, etag = _thermal_resource(c)
        oem = thermal.get("Oem") or {}
        key = next((k for k, v in oem.items() if isinstance(v, dict) and "FanSpeedAdjustmentMode" in v), None)
        if key is None:
            raise GateDenied("unsupported", "这台 BMC 的 Redfish 没有手动风扇设置（Oem.FanSpeedAdjustmentMode）")
        if op == ThermalOperation.RESTORE_AUTO:
            body: dict[str, Any] = {"FanSpeedAdjustmentMode": "Automatic"}
        else:
            current = oem[key].get("FanSpeedLevelPercents")
            level: Any = [int(pwm or 0)] * len(current) if isinstance(current, list) and current else int(pwm or 0)
            body = {"FanSpeedAdjustmentMode": "Manual", "FanManualModeTimeoutSeconds": HUAWEI_MANUAL_TIMEOUT, "FanSpeedLevelPercents": level}
        _rf_patch(c, path, {"Oem": {key: body}}, etag)
        after = (c.get(path)[0].get("Oem") or {}).get(key) or {}
        ok = after.get("FanSpeedAdjustmentMode") == body["FanSpeedAdjustmentMode"]
        return DriverResult([f"Oem.{key}.FanSpeedAdjustmentMode = {body['FanSpeedAdjustmentMode']}"], verified=ok, pwm=pwm)


HPE_LEVELS = (("MaximumCooling", "最大散热", 90), ("IncreasedCooling", "增强散热", 50), ("OptimalCooling", "最佳散热", 0))


class HpeRedfishCoolingDriver:
    def run(self, c: RedfishClient, profile: ThermalProfile, op: ThermalOperation, pwm: int | None) -> DriverResult:
        path, thermal, etag = _thermal_resource(c)
        hpe = (thermal.get("Oem") or {}).get("Hpe") or {}
        if "ThermalConfiguration" not in hpe:
            raise GateDenied("unsupported", "这台 iLO 的 Redfish 没有散热档位设置（Oem.Hpe.ThermalConfiguration）")
        level, name = "OptimalCooling", "最佳散热"
        if op == ThermalOperation.SET_SPEED_ALL:
            level, name = next((k, n) for k, n, low in HPE_LEVELS if (pwm or 0) >= low)
        _rf_patch(c, path, {"Oem": {"Hpe": {"ThermalConfiguration": level}}}, etag)
        after = ((c.get(path)[0].get("Oem") or {}).get("Hpe") or {}).get("ThermalConfiguration")
        # 固定转速 / 曲线下发的档位都记为“平台在管”（manual + 请求的转速），即使落在最佳散热档：
        # 策略据此判断已下发、不再重复提交；恢复原厂温控才记为 auto
        if op == ThermalOperation.RESTORE_AUTO:
            return DriverResult([f"散热档位 = {name}（{level}）"], verified=after == level, mode="auto")
        return DriverResult([f"散热档位 = {name}（{level}）"], verified=after == level, mode="manual", pwm=pwm,
                            note=f"iLO 只能按档位设置，{pwm}% 对应“{name}”")


DRIVERS: dict[str, Any] = {
    "ipmi_raw_v1": IpmiRawDriver(),
    "dell_redfish_min_fan_v1": DellRedfishMinFanDriver(),
    "huawei_redfish_fan_v1": HuaweiRedfishFanDriver(),
    "hpe_redfish_cooling_v1": HpeRedfishCoolingDriver(),
}

# 旧画像编号：与 Dell 内置命令相同的 raw 命令
DELL_RAW_STEPS = (PRODUCTION_CATALOG[0].restore_steps, PRODUCTION_CATALOG[0].set_steps)


def _with_default_steps(profile: ThermalProfile) -> ThermalProfile:
    if profile.driver_id == "dell_idrac_raw_v1" and not profile.restore_steps:
        return replace(profile, driver_id="ipmi_raw_v1", restore_steps=DELL_RAW_STEPS[0], set_steps=DELL_RAW_STEPS[1])
    return profile


@dataclass
class ThermalOutcome:
    status: str  # verified / unconfirmed
    message: str
    observed: dict[str, Any]
    mode: str  # auto / manual
    pwm: int | None


def execute_thermal(
    ctxs: dict[str, EndpointContext] | EndpointContext,
    op: ThermalOperation,
    pwm: int | None,
    *,
    catalog: tuple[ThermalProfile, ...],
    factory: ThermalClientFactory = PyghmiThermalClient,
    redfish_factory: RedfishClientFactory = default_redfish_factory,
    drivers: dict[str, Any] | None = None,
) -> ThermalOutcome:
    """只读识别 → 匹配命令 → 检查操作与转速 → 执行。门禁不通过抛 GateDenied（没有写调用）。

    ctxs 是可用的控制端点（按协议）；用第一个识别设备，再按匹配到的命令所需的协议执行。
    """
    if not catalog:
        raise GateDenied("catalog_empty", "风扇命令库是空的，拒绝风扇控制")
    if isinstance(ctxs, EndpointContext):
        ctxs = {"ipmi": ctxs}
    clients: dict[str, Any] = {}

    def client(proto: str) -> Any:
        if proto not in clients:
            clients[proto] = factory(ctxs[proto]) if proto == "ipmi" else redfish_factory(ctxs[proto])
        return clients[proto]

    try:
        first = next(iter(ctxs))
        ident = client(first).identity() if first == "ipmi" else redfish_identity(client(first))
        profile = _with_default_steps(match_profile(catalog, ident))
        check_operation(profile, op, pwm)
        if profile.transport not in ctxs:
            need = TRANSPORT_LABEL.get(profile.transport, profile.transport)
            raise GateDenied("transport_missing", f"“{profile.label or profile.profile_id}”的风扇命令通过 {need} 下发，"
                                                  f"请为资产添加用途为“控制”或“管理”的 {need} 端点")
        driver = (drivers or DRIVERS).get(profile.driver_id)
        if driver is None:
            raise GateDenied("driver_missing", f"风扇命令引用的驱动 {profile.driver_id} 不存在")
        res = driver.run(client(profile.transport), profile, op, pwm)
        source = SOURCE_LABEL.get(profile.evidence.source, "")
        observed = {"identity": ident.as_dict(), "profile": profile.profile_id, "profile_label": profile.label,
                    "source": profile.evidence.source.value, "transport": profile.transport, "steps": res.steps}
        label = OPERATION_LABEL[op] + (f" {pwm}%" if pwm is not None else "")
        extra = "".join(f"；{t}" for t in (res.note, f"命令来源：{profile.label or profile.profile_id}（{source}）") if t)
        mode = res.mode or ("auto" if op == ThermalOperation.RESTORE_AUTO else "manual")
        if res.verified:
            return ThermalOutcome("verified", f"{label}：已查询确认{extra}", observed, mode, res.pwm)
        if profile.transport == "redfish":
            msg = f"{label}：BMC 已接受，但再次读取的设置与下发的不一致，无法确认已生效{extra}"
        else:
            msg = f"{label}：BMC 已接受；IPMI 无法回读控制模式，无法确认已生效（风扇转速变化不能证明已生效）{extra}"
        return ThermalOutcome("unconfirmed", msg, observed, mode, res.pwm)
    finally:
        for c in clients.values():
            c.close()
