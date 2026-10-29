"""服务器 BMC 的采集计划（4.4 P20a）。

同一台 BMC 往往同时有 SNMP、Redfish、IPMI 三个端点。按协议排先后会让同一项数据从几个协议各采一份，
或者在首选协议读不到某项数据时一直缺着。这里改为按“数据项”安排：

1. 探测：每个协议各完整采集一次（只读），记下它实际读到了哪些数据项。
2. 计划：每项数据按偏好顺序挑能读到它的协议，第一个是主来源，其余是兜底。
3. 执行：每个周期只运行“是某项到期数据主来源”的协议，并只保留它负责的数据；
   主来源失败时，它负责的数据交给下一个兜底协议，指标里标出“降级”。

高频数据（温度、风扇、功耗、健康）每周期一次；低频数据（硬盘与阵列、BMC 日志、型号与固件）每 SLOW_EVERY 个周期一次。
端点变化、超过 REPROBE_DAYS 天或页面要求时重新探测。没有对应数据项的指标（电压、BMC 运行时间等）随所在协议一起保留。

双来源核对（4.4 P20e）：温度与功耗每天（以及每次探测时）用主来源和另一个协议各读一次比对，温度相差超过 3℃、
功耗相差超过 10% 时，如果主来源不是 Redfish 而 Redfish 也能读到，该项固定改由 Redfish 提供，并提示“该品牌 MIB 对应可能有误”
（模板都没有实机验证过，用来兜底）。固定会在之后的自动探测中保留，页面上点“重新探测”时清除、重新核对。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.metrics import Point
from app.models import Asset, CollectionPlan

# 参与采集计划的模板与协议
PLAN_PROFILES = frozenset({"dell-server", "generic-bmc"})
BMC_PROTOCOLS = ("snmp", "redfish", "ipmi")
SLOW_EVERY = 10
REPROBE_DAYS = 7
CHECK_ITEMS = ("temp", "power")
CHECK_EVERY = timedelta(days=1)
TEMP_TOLERANCE = 3.0
POWER_TOLERANCE = 0.10


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    slow: bool
    prefer: tuple[str, ...]


# 高频数据优先 SNMP（无登录会话、负担最轻），低频数据优先 Redfish（信息最全）；IPMI 都排最后
FAST = ("snmp", "redfish", "ipmi")
SLOW = ("redfish", "snmp", "ipmi")
ITEMS: dict[str, Item] = {
    i.key: i for i in (
        Item("temp", "温度", False, FAST),
        Item("fan", "风扇", False, FAST),
        Item("power", "功耗与电源状态", False, FAST),
        Item("health", "整机与部件健康", False, FAST),
        Item("storage", "硬盘与阵列", True, SLOW),
        Item("sel", "BMC 日志（SEL）", True, SLOW),
        Item("inventory", "内存容量等清单", True, SLOW),
    )
}

_PREFIX = {
    "temp": "temp", "cpu.temp": "temp", "fan.rpm": "fan", "fan.pct": "fan", "power.watts": "power", "power.on": "power",
    "health.system": "health", "psu.health": "health", "mem.health": "health", "disk.health": "storage", "raid.health": "storage",
    "mem.total_bytes": "inventory",
}


def item_of(metric_key: str) -> str | None:
    """指标属于哪个数据项；不属于任何数据项的返回 None（随所在协议保留）。"""
    base = metric_key.split(":", 1)[0]
    if base.startswith("sel."):
        return "sel"
    return _PREFIX.get(base)


def items_seen(points: list[Point]) -> list[str]:
    """一次采集里真正读到值的数据项（全部 unsupported / 空值的不算）。"""
    return sorted({k for p in points if p.value is not None and p.quality == "valid" and (k := item_of(p.key))})


def build(probed: dict[str, list[str]]) -> dict[str, list[str]]:
    """数据项 → 按偏好排列、且探测时确实读到它的协议。没有协议读到的项不出现在计划里。"""
    out: dict[str, list[str]] = {}
    for item in ITEMS.values():
        order = [p for p in item.prefer if item.key in probed.get(p, [])]
        if order:
            out[item.key] = order
    return out


def signature(asset: Asset) -> str:
    rows = sorted(f"{e.protocol}|{e.address}|{e.port}|{e.credential_id}" for e in asset.endpoints
                  if e.protocol in BMC_PROTOCOLS and e.purpose != "control")
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()[:64]


def applies(asset: Asset, profile_name: str | None) -> bool:
    """服务器 BMC 模板且至少有两个 BMC 协议的端点才需要计划；只有一个协议时就是它，不必探测。"""
    protocols = {e.protocol for e in asset.endpoints if e.protocol in BMC_PROTOCOLS and e.purpose != "control"}
    return profile_name in PLAN_PROFILES and len(protocols) >= 2


def needs_probe(plan: CollectionPlan | None, asset: Asset, now: datetime) -> str:
    """需要重新探测时返回原因，否则返回空串。"""
    if plan is None:
        return "首次采集"
    if plan.reprobe:
        return "页面要求重新探测"
    if plan.signature != signature(asset):
        return "管理端点有变化"
    probed_at = plan.probed_at if plan.probed_at.tzinfo else plan.probed_at.replace(tzinfo=now.tzinfo)
    if now - probed_at > timedelta(days=REPROBE_DAYS):
        return f"距上次探测超过 {REPROBE_DAYS} 天"
    down = [p for p, seen in plan.probed.items() if seen is None]
    if down and now - probed_at > timedelta(hours=1):
        return f"上次探测时 {'、'.join(down)} 不可用，每小时重试"
    return ""


def apply_pins(items: dict[str, list[str]], pins: dict[str, str]) -> dict[str, list[str]]:
    out = {k: list(v) for k, v in items.items()}
    for item, proto in pins.items():
        if proto in out.get(item, []):
            out[item] = [proto] + [p for p in out[item] if p != proto]
    return out


def check_due(plan: CollectionPlan, now: datetime) -> bool:
    if plan.checked_at is None:
        return True
    at = plan.checked_at if plan.checked_at.tzinfo else plan.checked_at.replace(tzinfo=now.tzinfo)
    return now - at >= CHECK_EVERY


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _temps(points: list[Point]) -> tuple[dict[str, float], float | None]:
    named: dict[str, float] = {}
    cpu: float | None = None
    for p in points:
        if p.value is None or p.quality != "valid":
            continue
        if p.key.startswith("temp:"):
            named[_norm(p.key[5:])] = float(p.value)
        elif p.key == "cpu.temp":
            cpu = float(p.value)
    if cpu is None:
        cpus = [v for k, v in named.items() if "cpu" in k]
        cpu = max(cpus) if cpus else None
    return named, cpu


def comparable(item: str, a: list[Point], b: list[Point]) -> list[tuple[str, float, float]]:
    """两个来源能对上的读数：(名称, 主来源值, 另一来源值)。"""
    if item == "power":
        va = next((float(p.value) for p in a if p.key == "power.watts" and p.value is not None and p.quality == "valid"), None)
        vb = next((float(p.value) for p in b if p.key == "power.watts" and p.value is not None and p.quality == "valid"), None)
        return [("整机功耗", va, vb)] if va is not None and vb is not None else []
    na, ca = _temps(a)
    nb, cb = _temps(b)
    shared = sorted(set(na) & set(nb))
    if shared:
        return [(k, na[k], nb[k]) for k in shared[:10]]
    return [("CPU 温度", ca, cb)] if ca is not None and cb is not None else []


def _off(item: str, va: float, vb: float) -> bool:
    if item == "power":
        return abs(va - vb) > POWER_TOLERANCE * max(abs(va), abs(vb), 1.0)
    return abs(va - vb) > TEMP_TOLERANCE


LABEL = {"snmp": "SNMP", "redfish": "Redfish", "ipmi": "IPMI"}


def cross_check(plan: CollectionPlan, captured: dict[str, list[Point]], now: datetime) -> None:
    """比对温度与功耗的主来源和另一个协议；不一致且 Redfish 能读到时，该项固定改由 Redfish 提供。"""
    state = dict(plan.check or {})
    results = dict(state.get("items") or {})
    pins = dict(state.get("pinned") or {})
    checked = False
    for item in CHECK_ITEMS:
        order = plan.items.get(item) or []
        if len(order) < 2 or order[0] not in captured:
            continue
        primary = order[0]
        other = next((p for p in order[1:] if p in captured), None)
        if other is None:
            continue
        checked = True
        pairs = comparable(item, captured[primary], captured[other])
        unit = "W" if item == "power" else "℃"
        if not pairs:
            results[item] = {"ok": None, "primary": primary, "other": other, "at": now.isoformat(), "detail": "两个来源没有能对上的读数"}
            continue
        bad = [(n, va, vb) for n, va, vb in pairs if _off(item, va, vb)]
        shown = bad or pairs
        text = "；".join(f"{n} {LABEL.get(primary, primary)} {va:g}{unit} / {LABEL.get(other, other)} {vb:g}{unit}" for n, va, vb in shown[:3])
        if not bad:
            results[item] = {"ok": True, "primary": primary, "other": other, "at": now.isoformat(), "detail": f"一致：{text}"}
            continue
        detail = f"不一致：{text}"
        if primary != "redfish" and "redfish" in order:
            pins[item] = "redfish"
            detail += f"。已改由 Redfish 提供，{LABEL.get(primary, primary)} 的该品牌 MIB 对应可能有误"
        results[item] = {"ok": False, "primary": primary, "other": other, "at": now.isoformat(), "detail": detail}
    if checked:
        plan.check = {"items": results, "pinned": pins}
        plan.items = apply_pins(plan.items, pins)
        plan.checked_at = now


def due_items(plan_items: dict[str, list[str]], slot: int) -> list[str]:
    """“立即采集”也不提前读低频数据，与原来补充步骤的降频一致。"""
    return [k for k in plan_items if not ITEMS[k].slow or slot % SLOW_EVERY == 0]


def describe(plan: CollectionPlan | None) -> dict:
    """页面用：每项数据的主来源与兜底，以及各协议探测结果。"""
    if plan is None:
        return {"exists": False, "items": [], "probed": {}, "probed_at": None, "reprobe": False}
    rows = []
    for item in ITEMS.values():
        order = plan.items.get(item.key, [])
        rows.append({"key": item.key, "label": item.label, "slow": item.slow, "primary": order[0] if order else None,
                     "fallbacks": order[1:]})
    check = plan.check or {}
    return {"exists": True, "items": rows, "probed": plan.probed, "probed_at": plan.probed_at.isoformat(), "reprobe": plan.reprobe,
            "slow_every": SLOW_EVERY, "checked_at": plan.checked_at.isoformat() if plan.checked_at else None,
            "check": [{"key": k, "label": ITEMS[k].label, **v} for k, v in (check.get("items") or {}).items() if k in ITEMS],
            "pinned": check.get("pinned") or {}}
