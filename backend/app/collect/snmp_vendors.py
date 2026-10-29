"""SNMP 厂商 / 标准扩展（4.2 P15）：UPS（UPS-MIB，RFC 1628）、MikroTik RouterOS、群晖 NAS。只读。

依据公开 MIB 定义编写，没有真实设备或固件验证记录；读不到的字段记为“不支持”，不猜测。
- UPS：只要设备实现 UPS-MIB 就适用，与品牌无关（多数 UPS 网卡都实现）。
- MikroTik：sysObjectID 以 1.3.6.1.4.1.14988 开头时读 MIKROTIK-MIB 的温度、电压与 DHCP 租约数；
  接口、CPU、内存仍由通用 IF-MIB / HOST-RESOURCES-MIB 采集。
- 群晖：读 SYNOLOGY-SYSTEM / DISK / RAID-MIB 的整机、硬盘与存储池状态；通用指标照常采集。
"""

from __future__ import annotations

from typing import Any, Protocol

from app.metrics import Point

SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"

# UPS-MIB
UPS = "1.3.6.1.2.1.33.1"
UPS_BATTERY_STATUS = UPS + ".2.1.0"        # 1 未知 2 正常 3 电量低 4 耗尽
UPS_MINUTES_LEFT = UPS + ".2.3.0"
UPS_CHARGE = UPS + ".2.4.0"                # %
UPS_OUTPUT_SOURCE = UPS + ".4.1.0"         # 3 市电正常 5 电池供电 4 旁路 …
UPS_LOAD = UPS + ".4.4.1.5"                # 每相输出负载 %（表）
UPS_INPUT_VOLTAGE = UPS + ".3.3.1.3"       # 每相输入电压 V（表）

# MikroTik（MIKROTIK-MIB）
MIKROTIK = "1.3.6.1.4.1.14988"
MT_TEMP = MIKROTIK + ".1.1.3.10.0"         # 0.1 ℃
MT_VOLTAGE = MIKROTIK + ".1.1.3.8.0"       # 0.1 V
MT_CPU_TEMP = MIKROTIK + ".1.1.3.11.0"     # 0.1 ℃
MT_DHCP_LEASES = MIKROTIK + ".1.1.6.1.0"
MT_VERSION = MIKROTIK + ".1.1.4.4.0"        # mtxrLicVersion：RouterOS 版本（4.3 P18 漏洞比对用）

# 群晖（SYNOLOGY-*-MIB）
SYNO = "1.3.6.1.4.1.6574"
SYNO_SYSTEM_STATUS = SYNO + ".1.1.0"       # 1 正常 2 故障
SYNO_TEMP = SYNO + ".1.2.0"                # ℃
SYNO_POWER_STATUS = SYNO + ".1.3.0"        # 1 正常 2 故障
SYNO_DISK = SYNO + ".2.1.1"                # .2 名称 / .5 状态 / .6 温度
SYNO_RAID = SYNO + ".3.1.1"                # .2 名称 / .3 状态
SYNO_MODEL = SYNO + ".1.5.1.0"             # modelName
SYNO_VERSION = SYNO + ".1.5.3.0"           # version，如 “DSM 7.2-64570 Update 3”

PROBE_OIDS = [UPS_BATTERY_STATUS, SYNO_SYSTEM_STATUS]


class Client(Protocol):
    def get(self, oids: list[str]) -> dict[str, Any]: ...

    def walk(self, oid: str) -> dict[str, Any]: ...


def _index(table: dict[str, Any], base: str) -> dict[str, Any]:
    prefix = base + "."
    return {k[len(prefix):]: v for k, v in table.items() if k.startswith(prefix) and v is not None}


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ups(client: Client, probe: dict[str, Any]) -> list[Point] | None:
    status = probe.get(UPS_BATTERY_STATUS)
    if status is None:
        return None
    head = client.get([UPS_MINUTES_LEFT, UPS_CHARGE, UPS_OUTPUT_SOURCE])
    battery = {2: (1, "正常"), 3: (0.5, "电量低"), 4: (0, "电量耗尽")}.get(int(status))
    pts = [Point("ups.battery_health", battery[0], "", message=battery[1]) if battery
           else Point("ups.battery_health", None, "", quality="unsupported", message="电池状态未知")]
    source = head.get(UPS_OUTPUT_SOURCE)
    if source is not None:
        # 1 为正常市电供电；电池供电时为 0，便于告警规则“≤ 0 持续 N 次”
        pts.append(Point("ups.on_mains", 1 if int(source) == 3 else 0, "",
                         message={3: "市电", 4: "旁路", 5: "电池", 6: "升压", 7: "降压"}.get(int(source), "其他")))
    for key, oid, unit in (("ups.charge_pct", UPS_CHARGE, "%"), ("ups.minutes_left", UPS_MINUTES_LEFT, "min")):
        value = _num(head.get(oid))
        pts.append(Point(key, value, unit) if value is not None else Point(key, None, unit, quality="unsupported"))
    loads = [v for v in (_num(x) for x in _index(client.walk(UPS_LOAD), UPS_LOAD).values()) if v is not None]
    if loads:
        pts.append(Point("ups.load_pct", max(loads), "%"))
    volts = [v for v in (_num(x) for x in _index(client.walk(UPS_INPUT_VOLTAGE), UPS_INPUT_VOLTAGE).values()) if v is not None]
    if volts:
        pts.append(Point("ups.input_volts", min(volts), "V"))
    return pts


def mikrotik(client: Client, object_id: Any) -> list[Point]:
    if not str(object_id or "").lstrip(".").startswith(MIKROTIK):
        return []
    values = client.get([MT_TEMP, MT_VOLTAGE, MT_CPU_TEMP, MT_DHCP_LEASES])
    pts = []
    for key, oid, unit, scale in (("temp:board", MT_TEMP, "°C", 10), ("cpu.temp", MT_CPU_TEMP, "°C", 10), ("volt:input", MT_VOLTAGE, "V", 10)):
        value = _num(values.get(oid))
        if value is not None:
            pts.append(Point(key, value / scale, unit))
    leases = _num(values.get(MT_DHCP_LEASES))
    if leases is not None:
        pts.append(Point("dhcp.leases", leases, ""))
    return pts


SYNO_DISK_STATUS = {1: (1, "正常"), 2: (1, "已初始化"), 3: (0.5, "未初始化"), 4: (0, "系统分区故障"), 5: (0, "损坏")}
SYNO_RAID_STATUS = {1: (1, "正常"), 11: (0.5, "降级"), 12: (0, "损坏")}


def synology(client: Client, probe: dict[str, Any]) -> list[Point]:
    status = probe.get(SYNO_SYSTEM_STATUS)
    if status is None:
        return []
    head = client.get([SYNO_TEMP, SYNO_POWER_STATUS])
    pts = [Point("health.system", 1 if int(status) == 1 else 0, "", message="正常" if int(status) == 1 else "故障")]
    temp = _num(head.get(SYNO_TEMP))
    if temp is not None:
        pts.append(Point("temp:system", temp, "°C"))
    if head.get(SYNO_POWER_STATUS) is not None:
        pts.append(Point("psu.health:system", 1 if int(head[SYNO_POWER_STATUS]) == 1 else 0, ""))
    names = _index(client.walk(SYNO_DISK + ".2"), SYNO_DISK + ".2")
    states = _index(client.walk(SYNO_DISK + ".5"), SYNO_DISK + ".5")
    temps = _index(client.walk(SYNO_DISK + ".6"), SYNO_DISK + ".6")
    for idx in sorted(set(names) | set(states))[:64]:
        name = str(names.get(idx) or f"disk{idx}").strip().replace(" ", "_")[:120]
        mapped = SYNO_DISK_STATUS.get(int(states[idx])) if idx in states else None
        pts.append(Point(f"disk.health:{name}", mapped[0], "", message=mapped[1]) if mapped
                   else Point(f"disk.health:{name}", None, "", quality="unsupported"))
        t = _num(temps.get(idx))
        if t is not None:
            pts.append(Point(f"disk.temp:{name}", t, "°C"))
    raid_names = _index(client.walk(SYNO_RAID + ".2"), SYNO_RAID + ".2")
    raid_states = _index(client.walk(SYNO_RAID + ".3"), SYNO_RAID + ".3")
    for idx in sorted(set(raid_names) | set(raid_states))[:32]:
        name = str(raid_names.get(idx) or f"volume{idx}").strip().replace(" ", "_")[:120]
        code = int(raid_states[idx]) if idx in raid_states else None
        # 2–10 为扩容、修复、校验等进行中状态：数据可用，记为 0.5 并带原状态码
        mapped = SYNO_RAID_STATUS.get(code) if code is not None else None
        if mapped is None and code is not None and 2 <= code <= 10:
            mapped = (0.5, f"进行中（{code}）")
        pts.append(Point(f"raid.health:{name}", mapped[0], "", message=mapped[1]) if mapped
                   else Point(f"raid.health:{name}", None, "", quality="unsupported"))
    return pts


def versions(client: Client, object_id: Any, probe: dict[str, Any]) -> dict[str, str]:
    """系统版本（4.3 P18）：RouterOS 与群晖 DSM 各读一个标量；读不到就不写，不影响指标采集。"""
    out: dict[str, str] = {}
    if str(object_id or "").lstrip(".").startswith(MIKROTIK):
        value = client.get([MT_VERSION]).get(MT_VERSION)
        if value:
            out["routeros_version"] = str(value).strip()
    if probe.get(SYNO_SYSTEM_STATUS) is not None:
        values = client.get([SYNO_MODEL, SYNO_VERSION])
        if values.get(SYNO_VERSION):
            out["dsm_version"] = str(values[SYNO_VERSION]).strip()
        if values.get(SYNO_MODEL):
            out["dsm_model"] = str(values[SYNO_MODEL]).strip()
    return out
