"""巡检报告（4.2 P16）：按固定规则检查当前状态，生成一份可查看、可导出的报告。只读数据库，不连接设备。

检查项（数值来自最新采集，过期数据不参与判断）：
- 采集：端点采集失败或证书 / 主机密钥不受信任；全部指标都已过期。
- 温度与容量按告警规则判断：有启用的阈值规则覆盖该资产与指标时，超过阈值按规则级别、
  距阈值 10% 以内记“警告”（与临时提频的“接近阈值”一致）；没有规则覆盖时按下面的固定值。
- 温度：CPU 温度 ≥ 80 ℃ 严重、≥ 70 ℃ 警告。
- 硬件健康：整机、电源模块、内存、硬盘、RAID、UPS 电池（1 正常 / 0.5 警告 / 0 严重）；风扇转速为 0。
- 容量：磁盘或存储池使用率 ≥ 90% 严重、≥ 80% 警告。
- 保护：24 小时内备份失败、距上次成功备份超过 48 小时、复制任务出错、UPS 正由电池供电。
- BMC 日志：“服务器告警”里仍待处理的严重事件（Redfish、IPMI、推送来源都算，标记已处理后不再列出）。
- 告警：仍未处理的告警。
- 维护：维护中已超过预计结束时间、仍未改回在用的资产。
没有数据来源的项目（证书到期、时钟漂移）不检查，报告里也不显示为“正常”。
每天定时生成（默认本地时间 8 点后第一次运行时，YUNSHU_INSPECTION_HOUR 可改，-1 关闭），也可手动生成。
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metrics
from app.models import Alert, AlertRule, Asset, CollectionStatus, InspectionReport, Site

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
HEALTH_PREFIXES = {
    "health.system": "整机健康", "psu.health": "电源模块", "mem.health": "内存", "disk.health": "硬盘",
    "raid.health": "RAID / 存储池", "ups.battery_health": "UPS 电池",
}


log = logging.getLogger(__name__)
# 没有时区数据时的最后退路：北京时间没有夏令时，固定 UTC+8 与 Asia/Shanghai 等价
_BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
_warned_no_tzdata = False


def _zone(key: str) -> tzinfo | None:
    try:
        return ZoneInfo(key)
    except Exception:  # noqa: BLE001 - 名字无效或缺时区数据
        return None


def _host_zone_key() -> str | None:
    """Linux 主机的时区名：/etc/timezone，或 /etc/localtime 指向的 zoneinfo 文件。取不到返回 None。"""
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if name:
            return name
    except OSError:
        pass
    target = os.path.realpath("/etc/localtime")
    return target.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in target else None


def _host_tz() -> tzinfo | None:
    """主机时区。Windows 只能拿到当前的 UTC 偏移（中国没有夏令时，结果一样；夏令时地区请设 TZ）。"""
    if sys.platform == "win32":
        offset = datetime.now().astimezone().utcoffset()
        return timezone(offset) if offset is not None else None
    key = _host_zone_key()
    return _zone(key) if key else None


def _local_tz() -> tzinfo:
    """平台时区：TZ；没设（或名字无效）时跟随主机；主机也取不到时用北京时间。

    Windows 没有系统时区库，zoneinfo 靠 tzdata 包（已在全部锁文件里）。万一缺了时区数据，
    退回固定的 UTC+8，不让每日报告、巡检计划、维护超期提醒、漏洞汇总因此报错。
    """
    name = os.environ.get("TZ")
    zone = (_zone(name) if name else None) or _host_tz() or _zone("Asia/Shanghai")
    if zone is not None:
        return zone
    global _warned_no_tzdata
    if not _warned_no_tzdata:  # 调用频繁，只提示一次
        _warned_no_tzdata = True
        log.warning("找不到时区数据（缺少 tzdata），按固定 UTC+8 计算")
    return _BEIJING


def _item(asset: Asset, sites: dict, category: str, severity: str, title: str, detail: str = "", key: str = "", value: Any = None) -> dict:
    return {"asset_id": str(asset.id), "asset_name": asset.name, "site": sites.get(asset.site_id, ""), "category": category,
            "severity": severity, "title": title, "detail": detail[:300], "metric_key": key, "value": value}


RULED = ("cpu.temp", "disk.used_pct", "storage.used_pct")


def _by_rules(rules: list[AlertRule], asset: Asset, key: str, value: float) -> tuple[bool, str | None]:
    """(有规则覆盖, 级别)；级别为空表示正常。多条规则取最严重的结论。"""
    from app.alerting import _metric_matches, matches_scope
    from app.collect.boost import near_threshold

    covered, found = False, None
    for r in rules:
        if not _metric_matches(r.metric_key, key) or not matches_scope(asset, r.asset_ids, r.tags):
            continue
        covered = True
        over = value <= r.threshold if r.comparison == "lte" else value >= r.threshold
        level = r.severity if over else "warning" if near_threshold(r.comparison, r.threshold, value) else None
        if level and (found is None or SEVERITY_ORDER.get(level, 9) < SEVERITY_ORDER.get(found, 9)):
            found = level
    return covered, found


def inspect(db: Session, now: datetime | None = None) -> tuple[list[dict[str, Any]], int]:
    from app.server_events import latest_critical, open_critical

    now = now or datetime.now(UTC)
    rules = [r for r in db.scalars(select(AlertRule).where(AlertRule.enabled.is_(True), AlertRule.deleted.is_(False), AlertRule.kind == "threshold"))
             if r.metric_key.split(":", 1)[0] in RULED]
    assets = db.scalars(select(Asset).where(Asset.lifecycle == "active")).all()
    sites = {s.id: s.name for s in db.scalars(select(Site))}
    latest = metrics.latest_many(db, [a.id for a in assets], now=now)
    statuses: dict[uuid.UUID, list[CollectionStatus]] = {}
    for st in db.scalars(select(CollectionStatus)):
        statuses.setdefault(st.asset_id, []).append(st)
    items: list[dict[str, Any]] = []
    for a in assets:
        for st in statuses.get(a.id, []):
            if st.state in ("failed", "untrusted"):
                items.append(_item(a, sites, "采集", "warning", f"{st.protocol.upper()} 采集失败" if st.state == "failed" else f"{st.protocol.upper()} 身份校验未通过", st.message))
        views = latest.get(a.id, [])
        if views and all(v.quality == "stale" for v in views):
            items.append(_item(a, sites, "采集", "warning", "全部指标已过期", "最近没有成功采集，以下检查对该资产不适用"))
            continue
        for v in views:
            if v.value is None:
                continue
            k, val = v.key, v.value
            base = k.split(":", 1)[0]
            label = k.split(":", 1)[1] if ":" in k else ""
            if base in RULED:
                covered, level = _by_rules(rules, a, k, val)
                if not covered:
                    fixed = (70, 80) if base == "cpu.temp" else (80, 90)
                    level = "critical" if val >= fixed[1] else "warning" if val >= fixed[0] else None
                if level is None:
                    continue
                if base == "cpu.temp":
                    items.append(_item(a, sites, "温度", level, f"CPU 温度 {val:g} ℃", key=k, value=val))
                else:
                    items.append(_item(a, sites, "容量", level, f"{'存储池' if base == 'storage.used_pct' else '磁盘'}“{label}”已用 {val:g}%", key=k, value=val))
            elif base in HEALTH_PREFIXES and val < 1:
                items.append(_item(a, sites, "硬件", "critical" if val == 0 else "warning",
                                   f"{HEALTH_PREFIXES[base]}{('“' + label + '”') if label else ''}{'故障' if val == 0 else '告警'}", v.message, k, val))
            elif base == "fan.rpm" and val == 0:
                items.append(_item(a, sites, "硬件", "warning", f"风扇“{label}”转速为 0", key=k, value=val))
            elif k == "backup.failed_24h" and val >= 1:
                items.append(_item(a, sites, "保护", "warning", f"24 小时内有 {val:g} 次备份失败", v.message, k, val))
            elif k == "backup.last_ok_age_h" and val > 48:
                items.append(_item(a, sites, "保护", "warning", f"已有 {val:g} 小时没有成功备份", key=k, value=val))
            elif k == "replication.failed" and val >= 1:
                items.append(_item(a, sites, "保护", "warning", f"{val:g} 个复制任务出错", v.message, k, val))
            elif k == "ups.on_mains" and val == 0:
                items.append(_item(a, sites, "电源", "critical", "UPS 正由电池供电", v.message, k, val))
    names = {a.id: a for a in assets}
    # 维护中超过预计结束时间：维护中的资产不参与其他检查，只提醒这一项
    for a in db.scalars(select(Asset).where(Asset.lifecycle == "maintenance", Asset.maintenance_until.is_not(None))):
        until = a.maintenance_until if a.maintenance_until.tzinfo else a.maintenance_until.replace(tzinfo=UTC)
        if until <= now:
            items.append(_item(a, sites, "维护", "warning", "维护已超过预计结束时间",
                               f"预计 {until.astimezone(_local_tz()):%Y-%m-%d %H:%M} 结束，仍是“维护中”：期间不采集、不告警。修好了请改回“在用”，没修好请延长预计结束时间",
                               "asset.maintenance_overdue", 1))
    for asset_id, (count, _) in open_critical(db).items():
        if asset_id in names:
            items.append(_item(names[asset_id], sites, "硬件", "critical", f"{count} 条严重硬件事件待处理（服务器告警）",
                               "；".join(latest_critical(db, asset_id)), "server_event.open_critical", count))
    # 4.3 P18e：待处理的高危及以上漏洞（按资产汇总一条）
    from app.models import VulnMatch, VulnRecord

    vulns: dict[uuid.UUID, list[VulnRecord]] = {}
    for asset_id, rec in db.execute(select(VulnMatch.asset_id, VulnRecord).join(VulnRecord, VulnRecord.id == VulnMatch.record_id)
                                    .where(VulnMatch.active.is_(True), VulnMatch.status.in_(("open", "affected")))):
        if rec.kev or rec.severity in ("critical", "high"):
            vulns.setdefault(asset_id, []).append(rec)
    for asset_id, recs in vulns.items():
        if asset_id not in names:
            continue
        kev = sum(1 for r in recs if r.kev)
        worst = "critical" if kev or any(r.severity == "critical" for r in recs) else "warning"
        top = "、".join(r.id for r in sorted(recs, key=lambda r: (not r.kev, -(r.cvss or 0)))[:3])
        items.append(_item(names[asset_id], sites, "漏洞", worst, f"{len(recs)} 个高危及以上漏洞待处理" + (f"（其中已被利用 {kev} 个）" if kev else ""),
                           top, "vuln.open", len(recs)))
    for alert in db.scalars(select(Alert).where(Alert.state.in_(["active", "acknowledged"]))):
        if alert.asset_id in names:
            items.append(_item(names[alert.asset_id], sites, "告警", alert.severity if alert.severity in SEVERITY_ORDER else "warning",
                               f"未处理告警：{alert.title}", "已确认" if alert.state == "acknowledged" else "", alert.metric_key, alert.value))
    items.sort(key=lambda i: (SEVERITY_ORDER.get(i["severity"], 9), i["category"], i["asset_name"], i["title"]))
    return items, len(assets)


def generate(db: Session, *, kind: str, by: str = "", now: datetime | None = None) -> InspectionReport:
    items, count = inspect(db, now)
    report = InspectionReport(kind=kind, created_by=by, asset_count=count, items=items, created_at=now or datetime.now(UTC))
    db.add(report)
    db.flush()
    return report


def daily_due(db: Session, hour: int, now: datetime | None = None) -> bool:
    if hour < 0:
        return False
    now = now or datetime.now(UTC)
    local = now.astimezone(_local_tz())
    if local.hour < hour:
        return False
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    return db.scalar(select(InspectionReport.id).where(InspectionReport.kind == "daily", InspectionReport.created_at >= start)) is None


def prune(db: Session, keep_days: int = 90, now: datetime | None = None) -> int:
    from datetime import timedelta

    from sqlalchemy import delete

    cutoff = (now or datetime.now(UTC)) - timedelta(days=keep_days)
    return db.execute(delete(InspectionReport).where(InspectionReport.created_at < cutoff)
                      .execution_options(synchronize_session=False)).rowcount or 0


def view(report: InspectionReport, visible: set[uuid.UUID] | None) -> dict[str, Any]:
    """按资产范围过滤后的报告（范围外的条目与计数都不出现）。"""
    items = [i for i in report.items if visible is None or uuid.UUID(i["asset_id"]) in visible]
    by_sev = {s: sum(1 for i in items if i["severity"] == s) for s in SEVERITY_ORDER}
    return {"id": str(report.id), "kind": report.kind, "created_at": report.created_at, "created_by": report.created_by,
            "asset_count": report.asset_count if visible is None else None,
            "affected_assets": len({i["asset_id"] for i in items}), "by_severity": by_sev, "items": items}


def to_csv(data: dict[str, Any]) -> str:
    """UTF-8 带 BOM，Excel 直接打开不乱码。"""
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf)
    w.writerow(["级别", "类别", "资产", "场所", "问题", "说明", "指标", "数值"])
    labels = {"critical": "严重", "warning": "警告", "info": "提示"}
    for i in data["items"]:
        w.writerow([labels.get(i["severity"], i["severity"]), i["category"], _cell(i["asset_name"]), _cell(i["site"]), _cell(i["title"]),
                    _cell(i["detail"]), i["metric_key"], "" if i["value"] is None else i["value"]])
    return buf.getvalue()


def _cell(text: Any) -> str:
    """防止表格公式注入：以 = + - @ 开头的文字前加单引号。"""
    s = str(text or "")
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s
