"""问答助手的只读查询工具（4.3 P18g）。也供只读 MCP 接口（P18h）复用。

规则（tests/test_ai_boundary.py 与 tests/test_assistant.py 强制）：
- 只查询，不写库；调用方必须在 app.ai.readonly.readonly_session 里执行；
- 结果按调用账号的资产范围过滤（visible = None 表示可见全部）；
- 不返回凭据、地址、序列号；返回的页面链接只指向平台内部路由。
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import inventory, metrics
from app.ai.protocols import Tool
from app.models import Alert, Asset, CollectionStatus, LatestMetric, Site, VulnMatch, VulnRecord

MAX_ROWS = 50


@dataclass
class ToolContext:
    db: Session
    visible: set[uuid.UUID] | None
    now: datetime
    deleted: set[uuid.UUID] | None = None  # 回收站里的资产，首次用到时查一次


def _visible(ctx: ToolContext, asset_id: uuid.UUID | None) -> bool:
    if ctx.visible is not None:  # 范围内集合已排除回收站
        return asset_id is not None and asset_id in ctx.visible
    if ctx.deleted is None:
        ctx.deleted = set(ctx.db.scalars(select(Asset.id).where(Asset.deleted_at.is_not(None))))
    return asset_id not in ctx.deleted


def _link(asset: Asset) -> str:
    return f"#/assets?asset={asset.id}"


def _asset_brief(a: Asset, sites: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {"name": a.name, "role": a.role, "form": a.form, "platform": a.platform, "vendor": a.vendor, "model": a.model,
            "site": sites.get(a.site_id, "") if a.site_id else "", "lifecycle": a.lifecycle, "tags": a.tags or [], "link": _link(a)}


def _sites(db: Session) -> dict[uuid.UUID, str]:
    return {s.id: s.name for s in db.scalars(select(Site))}


def _find_asset(ctx: ToolContext, name: str) -> Asset | None:
    name = (name or "").strip()
    try:
        a = ctx.db.get(Asset, uuid.UUID(name))
    except ValueError:
        a = ctx.db.scalar(select(Asset).where(Asset.name == name)) or ctx.db.scalar(select(Asset).where(Asset.name.ilike(f"%{name}%")).limit(1))
    return a if a is not None and _visible(ctx, a.id) else None


# ---------------- 工具实现 ----------------


def search_assets(ctx: ToolContext, query: str = "", role: str = "", lifecycle: str = "", limit: int = 20) -> dict[str, Any]:
    stmt = select(Asset)
    if query:
        like = f"%{query}%"
        stmt = stmt.where(or_(Asset.name.ilike(like), Asset.model.ilike(like), Asset.vendor.ilike(like), Asset.platform.ilike(like)))
    if role:
        stmt = stmt.where(Asset.role == role)
    if lifecycle:
        stmt = stmt.where(Asset.lifecycle == lifecycle)
    sites = _sites(ctx.db)
    rows = [a for a in ctx.db.scalars(stmt.order_by(Asset.name).limit(500)) if _visible(ctx, a.id)]
    return {"total": len(rows), "assets": [_asset_brief(a, sites) for a in rows[:min(limit, MAX_ROWS)]]}


def count_assets(ctx: ToolContext, group_by: str = "role") -> dict[str, Any]:
    if group_by not in ("role", "form", "platform", "lifecycle", "site", "vendor"):
        group_by = "role"
    sites = _sites(ctx.db)
    out: dict[str, int] = {}
    for a in ctx.db.scalars(select(Asset)):
        if not _visible(ctx, a.id):
            continue
        key = (sites.get(a.site_id, "未指定") if a.site_id else "未指定") if group_by == "site" else (getattr(a, group_by) or "未填写")
        out[key] = out.get(key, 0) + 1
    return {"group_by": group_by, "counts": dict(sorted(out.items(), key=lambda kv: -kv[1]))}


def get_asset(ctx: ToolContext, name: str) -> dict[str, Any]:
    a = _find_asset(ctx, name)
    if a is None:
        return {"error": "没有找到这台资产，或你没有权限查看"}
    latest = metrics.latest_many(ctx.db, [a.id], now=ctx.now).get(a.id, [])
    statuses = list(ctx.db.scalars(select(CollectionStatus).where(CollectionStatus.asset_id == a.id)))
    return {**_asset_brief(a, _sites(ctx.db)),
            "collection": [{"protocol": s.protocol, "state": s.state, "message": (s.message or "")[:200]} for s in statuses],
            "metrics": [{"key": v.key, "value": v.value, "unit": v.unit, "quality": v.quality} for v in latest][:60],
            "active_alerts": [t for (t,) in ctx.db.execute(select(Alert.title).where(Alert.asset_id == a.id, Alert.state.in_(("active", "acknowledged"))))]}


def top_metric(ctx: ToolContext, metric: str, order: str = "desc", above: float | None = None, below: float | None = None, limit: int = 10) -> dict[str, Any]:
    """按指标排序找资产，如 cpu.temp 最高、disk.used_pct:* 超过 80。只用新鲜的有效值。"""
    pattern = metric if any(c in metric for c in "*?[") else metric
    rows = []
    sites = _sites(ctx.db)
    names = {a.id: a for a in ctx.db.scalars(select(Asset))}
    for m in ctx.db.scalars(select(LatestMetric).where(LatestMetric.value.is_not(None))):
        if not (fnmatchcase(m.metric_key, pattern) or m.metric_key.startswith(pattern + ":") or m.metric_key == pattern):
            continue
        if not _visible(ctx, m.asset_id) or m.asset_id not in names:
            continue
        observed = m.observed_at if m.observed_at.tzinfo else m.observed_at.replace(tzinfo=UTC)
        if m.quality not in metrics.NUMERIC_QUALITIES or (ctx.now - observed).total_seconds() > metrics.STALE_FACTOR * max(1, m.interval_seconds):
            continue
        if above is not None and m.value <= above or below is not None and m.value >= below:
            continue
        a = names[m.asset_id]
        rows.append({"asset": a.name, "site": sites.get(a.site_id, "") if a.site_id else "", "metric": m.metric_key, "value": m.value, "unit": m.unit, "link": _link(a)})
    rows.sort(key=lambda r: r["value"], reverse=order != "asc")
    return {"metric": metric, "count": len(rows), "rows": rows[:min(limit, MAX_ROWS)]}


def metric_history(ctx: ToolContext, name: str, metric: str, hours: int = 24) -> dict[str, Any]:
    a = _find_asset(ctx, name)
    if a is None:
        return {"error": "没有找到这台资产，或你没有权限查看"}
    hours = max(1, min(int(hours), 24 * 30))
    pts = [p["v"] for p in metrics.series(ctx.db, a.id, metric, ctx.now - timedelta(hours=hours), ctx.now)["points"] if p["v"] is not None]
    if not pts:
        return {"asset": a.name, "metric": metric, "hours": hours, "points": 0}
    return {"asset": a.name, "metric": metric, "hours": hours, "points": len(pts), "min": min(pts), "max": max(pts),
            "avg": round(sum(pts) / len(pts), 2), "last": pts[-1], "link": _link(a)}


def list_alerts(ctx: ToolContext, state: str = "active", severity: str = "", limit: int = 20) -> dict[str, Any]:
    states = {"active": ("active", "acknowledged", "silenced"), "resolved": ("resolved",)}.get(state, ("active", "acknowledged", "silenced"))
    stmt = select(Alert).where(Alert.state.in_(states))
    if severity:
        stmt = stmt.where(Alert.severity == severity)
    rows = [a for a in ctx.db.scalars(stmt.order_by(Alert.last_seen_at.desc()).limit(500)) if _visible(ctx, a.asset_id)]
    return {"count": len(rows), "alerts": [{"title": a.title, "asset": a.asset_name, "severity": a.severity, "state": a.state, "metric": a.metric_key,
                                            "value": a.value, "since": a.activated_at or a.first_seen_at, "link": "#/alerts"} for a in rows[:min(limit, MAX_ROWS)]]}


def alert_ranking(ctx: ToolContext, days: int = 7, limit: int = 10) -> dict[str, Any]:
    since = ctx.now - timedelta(days=max(1, min(int(days), 90)))
    rows = ctx.db.execute(select(Alert.asset_id, Alert.asset_name, func.count()).where(Alert.first_seen_at >= since, Alert.asset_id.is_not(None))
                          .group_by(Alert.asset_id, Alert.asset_name).order_by(func.count().desc()).limit(200)).all()
    out = [{"asset": n, "alerts": c, "link": f"#/assets?asset={aid}"} for aid, n, c in rows if _visible(ctx, aid)]
    return {"days": days, "rows": out[:min(limit, MAX_ROWS)]}


def get_software(ctx: ToolContext, name: str) -> dict[str, Any]:
    a = _find_asset(ctx, name)
    if a is None:
        return {"error": "没有找到这台资产，或你没有权限查看"}
    sts = list(ctx.db.scalars(select(CollectionStatus).where(CollectionStatus.asset_id == a.id)))
    items = [{k: i[k] for k in ("kind", "label", "version", "detail")} for i in inventory.software_for(a, sts)]
    return {"asset": a.name, "software": items, "link": _link(a)}


def find_versions(ctx: ToolContext, product: str) -> dict[str, Any]:
    """所有资产上某类软件 / 固件的版本，如 “iDRAC”“ESXi”“DSM”。"""
    want = product.lower()
    sts: dict[uuid.UUID, list[CollectionStatus]] = {}
    for s in ctx.db.scalars(select(CollectionStatus)):
        sts.setdefault(s.asset_id, []).append(s)
    rows = []
    for a in ctx.db.scalars(select(Asset).order_by(Asset.name)):
        if not _visible(ctx, a.id):
            continue
        for i in inventory.software_for(a, sts.get(a.id, [])):
            if want in i["label"].lower() or want in i["product"].lower():
                rows.append({"asset": a.name, "software": i["label"], "version": i["version"], "link": _link(a)})
    return {"product": product, "count": len(rows), "rows": rows[:MAX_ROWS]}


def list_vulnerabilities(ctx: ToolContext, only_local: bool = True, kev_only: bool = False, severity: str = "", limit: int = 15) -> dict[str, Any]:
    per: dict[str, set[uuid.UUID]] = {}
    for rid, aid in ctx.db.execute(select(VulnMatch.record_id, VulnMatch.asset_id).where(VulnMatch.active.is_(True),
                                                                                          VulnMatch.status.in_(("open", "affected")))):
        if _visible(ctx, aid):
            per.setdefault(rid, set()).add(aid)
    stmt = select(VulnRecord)
    if only_local:
        stmt = stmt.where(VulnRecord.id.in_(list(per) or [""]))
    if kev_only:
        stmt = stmt.where(VulnRecord.kev.is_(True))
    if severity:
        stmt = stmt.where(VulnRecord.severity == severity)
    rows = ctx.db.scalars(stmt.order_by(VulnRecord.kev.desc(), VulnRecord.cvss.desc().nulls_last()).limit(min(limit, MAX_ROWS)))
    return {"vulnerabilities": [{"id": r.id, "title": r.title, "severity": r.severity, "cvss": r.cvss, "kev": r.kev, "local_assets": len(per.get(r.id, ())),
                                 "link": f"#/vulns?id={r.id}"} for r in rows]}


# ---------------- 注册表 ----------------

_S = "string"
TOOLS: dict[str, tuple[Callable[..., dict[str, Any]], Tool]] = {
    "search_assets": (search_assets, Tool("search_assets", "按关键词（名称、型号、品牌、平台）和角色查找资产。",
                      {"type": "object", "properties": {"query": {"type": _S}, "role": {"type": _S, "description": "server / switch / router / ups / nas 等"},
                                                        "lifecycle": {"type": _S}, "limit": {"type": "integer"}}})),
    "count_assets": (count_assets, Tool("count_assets", "按角色、形态、平台、生命周期、场所或品牌统计资产数量。",
                     {"type": "object", "properties": {"group_by": {"type": _S, "enum": ["role", "form", "platform", "lifecycle", "site", "vendor"]}}})),
    "get_asset": (get_asset, Tool("get_asset", "查看一台资产的详情：最新指标、采集状态、当前告警。",
                  {"type": "object", "properties": {"name": {"type": _S}}, "required": ["name"]})),
    "top_metric": (top_metric, Tool("top_metric", "按某个指标的最新值给资产排序或筛选，例如 cpu.temp、cpu.load、mem.used_pct、disk.used_pct、power.watts。",
                   {"type": "object", "properties": {"metric": {"type": _S}, "order": {"type": _S, "enum": ["desc", "asc"]},
                                                     "above": {"type": "number"}, "below": {"type": "number"}, "limit": {"type": "integer"}},
                    "required": ["metric"]})),
    "metric_history": (metric_history, Tool("metric_history", "某台资产某个指标在最近若干小时的最小、最大、平均和最新值。",
                       {"type": "object", "properties": {"name": {"type": _S}, "metric": {"type": _S}, "hours": {"type": "integer"}}, "required": ["name", "metric"]})),
    "list_alerts": (list_alerts, Tool("list_alerts", "列出当前或已恢复的告警。",
                    {"type": "object", "properties": {"state": {"type": _S, "enum": ["active", "resolved"]}, "severity": {"type": _S}, "limit": {"type": "integer"}}})),
    "alert_ranking": (alert_ranking, Tool("alert_ranking", "最近若干天哪些资产告警最多。",
                      {"type": "object", "properties": {"days": {"type": "integer"}, "limit": {"type": "integer"}}})),
    "get_software": (get_software, Tool("get_software", "一台资产的系统、固件、虚拟化版本。",
                     {"type": "object", "properties": {"name": {"type": _S}}, "required": ["name"]})),
    "find_versions": (find_versions, Tool("find_versions", "所有资产上某类软件或固件的版本，例如 iDRAC、ESXi、DSM、RouterOS、Proxmox。",
                      {"type": "object", "properties": {"product": {"type": _S}}, "required": ["product"]})),
    "list_vulnerabilities": (list_vulnerabilities, Tool("list_vulnerabilities", "列出漏洞，默认只列命中本地资产且待处理的。",
                             {"type": "object", "properties": {"only_local": {"type": "boolean"}, "kev_only": {"type": "boolean"}, "severity": {"type": _S},
                                                               "limit": {"type": "integer"}}})),
}


def definitions() -> list[Tool]:
    return [t for _, t in TOOLS.values()]


def call(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    entry = TOOLS.get(name)
    if entry is None:
        return {"error": f"没有这个工具：{name}"}
    fn, tool = entry
    allowed = set((tool.parameters.get("properties") or {}).keys())
    args = {k: v for k, v in (arguments or {}).items() if k in allowed}
    try:
        return fn(ctx, **args)
    except (TypeError, ValueError) as exc:
        return {"error": f"参数不正确：{exc}"}
