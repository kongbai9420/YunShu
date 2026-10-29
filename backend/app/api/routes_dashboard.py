"""总览仪表盘：布局（带修订号）与小组件数据。

- 布局：所有人共用一份（key=default）。只读用户可以看、不能改；保存时必须带上读取时的 revision，
  不一致返回 409 和当前版本，由用户选择载入最新或用自己的版本覆盖（覆盖时带上新的 revision 再存一次）。
- 小组件数据：一次请求返回资产健康、最近采集问题、CPU 温度最高、功耗、采集质量和关注列表，
  全部来自真实的采集状态与指标；过期、失败、不支持的值不显示成 0。告警在 P4 提供。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session, selectinload

from app import audit, metrics
from app.api.deps import editor, get_db, reader
from app.collect.runner import resolve_profile
from app.models import Alert, Asset, CollectionStatus, Dashboard, LatestMetric, User

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

DEFAULT_KEY = "default"
WIDGETS = ("health", "issues", "temps", "power", "quality", "watch")
COLS = {"lg": 12, "md": 6, "sm": 1}
MAX_WATCH = 50
POWER_SERIES_ASSETS = 50


class LayoutItem(BaseModel):
    i: Literal["health", "issues", "temps", "power", "quality", "watch", "vulns"]
    x: int = Field(ge=0, le=11)
    y: int = Field(ge=0, le=500)
    w: int = Field(ge=1, le=12)
    h: int = Field(ge=1, le=20)
    minW: int | None = Field(None, ge=1, le=12)
    minH: int | None = Field(None, ge=1, le=20)


class DashboardData(BaseModel):
    layouts: dict[Literal["lg", "md", "sm"], list[LayoutItem]]
    watch: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_WATCH)

    @field_validator("layouts")
    @classmethod
    def _fits(cls, v: dict[str, list[LayoutItem]]) -> dict[str, list[LayoutItem]]:
        for bp, items in v.items():
            cols = COLS[bp]
            ids = [it.i for it in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{bp} 布局里有重复的小组件")
            for it in items:
                if it.x + it.w > cols:
                    raise ValueError(f"{bp} 布局里的「{it.i}」超出 {cols} 列")
        return v


class SaveIn(BaseModel):
    revision: int
    data: DashboardData


def _out(d: Dashboard | None) -> dict[str, Any]:
    if d is None:
        return {"revision": 0, "data": None, "updated_by": "", "updated_at": None}
    at = d.updated_at if d.updated_at.tzinfo else d.updated_at.replace(tzinfo=UTC)
    return {"revision": d.revision, "data": d.data, "updated_by": d.updated_by, "updated_at": at.isoformat()}


@router.get("/layout")
def get_layout(_: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _out(db.scalar(select(Dashboard).where(Dashboard.key == DEFAULT_KEY)))


@router.put("/layout")
def save_layout(body: SaveIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    d = db.scalar(select(Dashboard).where(Dashboard.key == DEFAULT_KEY))
    current = d.revision if d else 0
    if body.revision != current:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": "布局在你编辑期间已被更新，请选择载入最新版本或用你的版本覆盖", "current": _out(d)})
    data = body.data.model_dump(mode="json")
    from app.scope import can_see, scope_for

    scope = scope_for(db, actor)
    if d is not None and not scope.all:
        # 共享布局：范围受限的账号看不到的关注项原样保留，不会因为他保存而被删掉（4.1 P11）
        hidden = [x for x in d.data.get("watch", []) if not can_see(db, scope, uuid.UUID(x))]
        data["watch"] = (data["watch"] + [x for x in hidden if x not in data["watch"]])[:MAX_WATCH]
    if d is None:
        d = Dashboard(key=DEFAULT_KEY, data=data, revision=1, updated_by=actor.display_name or actor.username)
        db.add(d)
    else:
        d.data, d.revision, d.updated_by = data, d.revision + 1, actor.display_name or actor.username
        d.updated_at = datetime.now(UTC)
    db.flush()
    audit.record(db, actor, "dashboard.save", target_type="dashboard", target_id=DEFAULT_KEY, summary=f"保存总览布局，修订 {d.revision}")
    return _out(d)


# ---- 小组件数据 ----


def _asset_state(asset: Asset, sts: list[CollectionStatus], stale: bool) -> str:
    """ok / degraded（部分端点失败）/ failed / untrusted / unconfigured（缺凭据，未连设备）/ stale / unmonitored"""
    if resolve_profile(asset) is None:
        return "unmonitored"
    if any(s.state == "untrusted" for s in sts):
        return "untrusted"
    states = {s.state for s in sts if s.state != "skipped"}
    if not states:
        return "pending"
    if states == {"failed"}:
        return "failed"
    if "failed" in states:
        return "degraded"
    if "unconfigured" in states and "ok" not in states:
        return "unconfigured"
    return "stale" if stale else "ok"


@router.get("/summary")
def summary(user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.scope import restrict, scope_for

    now = datetime.now(UTC)
    scope = scope_for(db, user)
    # 所有统计都从这份资产清单派生：只统计范围内资产（4.1 P11）
    assets = db.scalars(restrict(select(Asset).where(Asset.lifecycle != "retired"), Asset.id, scope).options(selectinload(Asset.endpoints))).all()
    names = {a.id: a.name for a in assets}
    statuses: dict[uuid.UUID, list[CollectionStatus]] = {}
    for s in db.scalars(select(CollectionStatus)):
        statuses.setdefault(s.asset_id, []).append(s)

    # 只取需要的列、一次遍历算完统计（4.1 P12 容量测量：1000 台 × 30 项时逐条构造对象要数秒并拖慢整个 API）
    wanted = ("cpu.temp", "cpu.load", "power.watts", "mem.used_pct")
    small: dict[uuid.UUID, dict[str, dict[str, Any]]] = {a.id: {} for a in assets}
    has_metrics: set[uuid.UUID] = set()
    fresh: set[uuid.UUID] = set()
    quality: dict[str, int] = {}
    if assets:
        ids = list(small)
        numeric = list(metrics.NUMERIC_QUALITIES)
        # 质量与过期按采集周期分组在数据库里聚合：过期 = 数值类质量且超过 3 个周期没有新值
        for interval in db.scalars(select(LatestMetric.interval_seconds).where(LatestMetric.asset_id.in_(ids)).distinct()):
            cutoff = now - timedelta(seconds=metrics.STALE_FACTOR * max(1, interval or 1))
            is_stale = and_(LatestMetric.quality.in_(numeric), LatestMetric.observed_at < cutoff)
            rows = db.execute(select(LatestMetric.asset_id, LatestMetric.quality, func.count(), func.sum(case((is_stale, 1), else_=0)))
                              .where(LatestMetric.asset_id.in_(ids), LatestMetric.interval_seconds == interval)
                              .group_by(LatestMetric.asset_id, LatestMetric.quality))
            for aid, q, n, n_stale in rows:
                has_metrics.add(aid)
                n_stale = int(n_stale or 0)
                if n > n_stale:
                    fresh.add(aid)
                    quality[q] = quality.get(q, 0) + n - n_stale
                if n_stale:
                    quality["stale"] = quality.get("stale", 0) + n_stale
        for aid, key, value, unit, q, observed, interval in db.execute(
                select(LatestMetric.asset_id, LatestMetric.metric_key, LatestMetric.value, LatestMetric.unit,
                       LatestMetric.quality, LatestMetric.observed_at, LatestMetric.interval_seconds)
                .where(LatestMetric.asset_id.in_(ids), LatestMetric.metric_key.in_(wanted))):
            if q in metrics.NUMERIC_QUALITIES and (now - metrics._aware(observed)).total_seconds() > metrics.STALE_FACTOR * max(1, interval):
                q, value = "stale", None
            small[aid][key] = {"value": value, "unit": unit, "quality": q}

    # 资产健康
    health: dict[str, int] = {}
    per_asset_state: dict[uuid.UUID, str] = {}
    for a in assets:
        stale = a.id in has_metrics and a.id not in fresh
        st = _asset_state(a, statuses.get(a.id, []), stale)
        per_asset_state[a.id] = st
        health[st] = health.get(st, 0) + 1

    # 当前告警与最近采集问题分开返回，采集失败不等于设备离线。
    issues = sorted(
        (s for ss in statuses.values() for s in ss if s.state in ("failed", "untrusted") and s.asset_id in names),
        key=lambda s: s.last_attempt_at, reverse=True,
    )[:8]

    # CPU 温度最高（只取有效或估算值）
    temps = sorted(
        ({"asset_id": str(aid), "name": names[aid], **lv["cpu.temp"]}
         for aid, lv in small.items() if "cpu.temp" in lv and lv["cpu.temp"]["value"] is not None),
        key=lambda t: t["value"], reverse=True,
    )[:8]

    # 功耗：当前合计与近 1 小时（按分钟合计；某分钟缺数据的资产不计入，并给出覆盖台数）
    power_assets = [aid for aid, lv in small.items() if "power.watts" in lv][:POWER_SERIES_ASSETS]
    current_watts = sum(lv["power.watts"]["value"] for lv in small.values() if "power.watts" in lv and lv["power.watts"]["value"] is not None)
    reporting = sum(1 for lv in small.values() if "power.watts" in lv and lv["power.watts"]["value"] is not None)
    buckets: dict[int, list[float]] = {}
    for aid in power_assets:
        for p in metrics.series(db, aid, "power.watts", now - timedelta(hours=1), now, resolution="raw")["points"]:
            if p["v"] is not None:
                buckets.setdefault(p["t"] // 60 * 60, []).append(p["v"])
    power_series = [{"t": t, "watts": round(sum(v), 1), "assets": len(v)} for t, v in sorted(buckets.items())]

    # 采集质量：上面遍历时已统计

    # 关注列表
    d = db.scalar(select(Dashboard).where(Dashboard.key == DEFAULT_KEY))
    watch_ids = [uuid.UUID(x) for x in (d.data.get("watch", []) if d else [])]
    watch = []
    for aid in watch_ids:
        if aid not in names:
            continue
        watch.append({"asset_id": str(aid), "name": names[aid], "state": per_asset_state[aid], "metrics": dict(small[aid])})

    return {
        "generated_at": now.isoformat(),
        "health": health,
        "total_assets": len(assets),
        "alerts": [{"id": str(a.id), "asset_id": str(a.asset_id) if a.asset_id else None, "name": a.asset_name, "title": a.title,
                    "severity": a.severity, "state": a.state, "suppression_reason": a.suppression_reason}
                   for a in db.scalars(restrict(select(Alert).where(Alert.state.in_(["active", "acknowledged", "silenced"])), Alert.asset_id, scope).order_by(Alert.last_seen_at.desc()).limit(8))],
        "issues": [{"asset_id": str(s.asset_id), "name": names[s.asset_id], "protocol": s.protocol, "state": s.state,
                    "message": s.message, "at": (s.last_attempt_at if s.last_attempt_at.tzinfo else s.last_attempt_at.replace(tzinfo=UTC)).isoformat()} for s in issues],
        "temps": temps,
        "power": {"current_watts": round(current_watts, 1), "reporting_assets": reporting, "series": power_series},
        "quality": quality,
        "watch": watch,
        "vulns": _vuln_summary(db, set(names)),
    }


def _vuln_summary(db: Session, visible: set[uuid.UUID]) -> dict[str, Any]:
    """4.3 P18e：范围内资产命中、待处理的漏洞。"""
    from app.models import VulnMatch, VulnRecord

    per: dict[str, tuple[VulnRecord, set[uuid.UUID]]] = {}
    for asset_id, rec in db.execute(select(VulnMatch.asset_id, VulnRecord).join(VulnRecord, VulnRecord.id == VulnMatch.record_id)
                                    .where(VulnMatch.active.is_(True), VulnMatch.status.in_(("open", "affected")))):
        if asset_id in visible:
            per.setdefault(rec.id, (rec, set()))[1].add(asset_id)
    recs = [r for r, _ in per.values()]
    top = sorted(per.values(), key=lambda x: (not x[0].kev, -(x[0].cvss or 0), -len(x[1])))[:6]
    return {"open": len(recs), "kev": sum(1 for r in recs if r.kev), "critical": sum(1 for r in recs if r.severity == "critical"),
            "high": sum(1 for r in recs if r.severity == "high"),
            "top": [{"id": r.id, "title": r.title, "kev": r.kev, "cvss": r.cvss, "severity": r.severity, "assets": len(a)} for r, a in top]}
