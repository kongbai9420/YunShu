"""服务器告警（4.4 P20c）：把 BMC 系统事件日志（SEL）逐条存下来，供“服务器告警”板块查看与处理。

- 来源：Redfish 日志服务（首选）或 IPMI SEL（Redfish 不可用时兜底，由采集计划决定）。只读，从不清空 BMC 日志。
- 去重：同一资产、同一来源按“记录号 + 发生时间”去重；BMC 清空日志后记录号会重用，加上时间不会误判。
  推送（Redfish 事件订阅、SNMP Trap）与定时读取的 BMC 日志可能是同一件事：同一资产 2 分钟内文字相同的只存第一条。
- 首次读到时：近 7 天的严重 / 警告记为“待处理”；更早的或只是提示的记为“已归档”，避免一接入就冒出几百条待处理。
- 分类按日志文字里的关键词粗分（温度、风扇、电源、内存、硬盘、CPU、机箱），只用于筛选，不作判定依据。
- 默认保留 180 天。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import ServerEvent

RETENTION_DAYS = 180
OPEN_WINDOW = timedelta(days=7)
SAME_EVENT_WINDOW = timedelta(minutes=2)
# 告警规则用的合成指标：资产上待处理的严重事件条数（不写指标库，由告警求值时直接统计）
SERVER_EVENT_METRIC = "server_event.open_critical"
# 来源：定时读取的 BMC 日志（redfish / ipmi）与 BMC 主动推送（redfish_event / snmp_trap）
PUSH_SOURCES = ("redfish_event", "snmp_trap")
SEVERITIES = ("critical", "warning", "info")
CATEGORIES = {
    "temperature": "温度", "fan": "风扇", "power": "电源", "memory": "内存", "storage": "硬盘与阵列", "cpu": "CPU",
    "chassis": "机箱", "other": "其他",
}
_KEYWORDS = (
    ("temperature", r"temp|thermal|温度|过热"),
    ("fan", r"\bfan|风扇"),
    ("memory", r"memory|dimm|\becc\b|内存"),
    ("storage", r"disk|drive|raid|storage|volume|backplane|硬盘|阵列"),
    ("power", r"power|psu|voltage|\bac\b|redundan|电源|电压"),
    ("cpu", r"\bcpu|processor|处理器"),
    ("chassis", r"intrusion|chassis|cover|机箱"),
)


def severity_of(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if s in ("critical", "failed", "fatal", "nonrecoverable", "严重"):
        return "critical"
    if s in ("warning", "minor", "major", "noncritical", "警告"):
        return "warning"
    return "info"


def category_of(text: str) -> str:
    low = text.lower()
    return next((key for key, pattern in _KEYWORDS if re.search(pattern, low)), "other")


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(".。").lower()


def _same_elsewhere(db: Session, asset_id: uuid.UUID, source: str, when: datetime, message: str) -> bool:
    if not message:
        return False
    rows = db.scalars(select(ServerEvent.message).where(
        ServerEvent.asset_id == asset_id, ServerEvent.source != source,
        ServerEvent.occurred_at >= when - SAME_EVENT_WINDOW, ServerEvent.occurred_at <= when + SAME_EVENT_WINDOW))
    want = _norm(message)
    return any(_norm(m) == want for m in rows)


def record(db: Session, asset_id: uuid.UUID, source: str, entries: list[dict[str, Any]], now: datetime) -> int:
    """写入新条目，返回新增条数。entries：{"id", "created", "severity", "message"}；时间读不出的跳过。"""
    added = 0
    seen: set[str] = set()
    for e in entries[:500]:
        when = _when(e.get("created"))
        if when is None:
            continue
        key = f"{str(e.get('id') or '')[:100]}@{when.isoformat()}"
        if key in seen:
            continue
        seen.add(key)
        exists = db.scalar(select(ServerEvent.id).where(ServerEvent.asset_id == asset_id, ServerEvent.source == source,
                                                         ServerEvent.record_key == key))
        if exists is not None:
            continue
        sev = severity_of(e.get("severity"))
        message = str(e.get("message") or "").strip()[:1000]
        if _same_elsewhere(db, asset_id, source, when, message):
            continue
        state = "open" if sev != "info" and now - when <= OPEN_WINDOW else "archived"
        db.add(ServerEvent(asset_id=asset_id, source=source, record_key=key, occurred_at=when, severity=sev,
                           category=category_of(message), message=message, first_seen_at=now, state=state))
        added += 1
    if added:
        db.flush()
    return added


def open_critical(db: Session) -> dict[uuid.UUID, tuple[int, datetime]]:
    """每台资产待处理的严重事件条数与最新一条的首次读到时间。"""
    stmt = (select(ServerEvent.asset_id, func.count(), func.max(ServerEvent.first_seen_at))
            .where(ServerEvent.state == "open", ServerEvent.severity == "critical").group_by(ServerEvent.asset_id))
    return {a: (n, t if t.tzinfo else t.replace(tzinfo=UTC)) for a, n, t in db.execute(stmt).all()}


def latest_critical(db: Session, asset_id: uuid.UUID, *, since: datetime | None = None, limit: int = 3) -> list[str]:
    stmt = select(ServerEvent.message).where(ServerEvent.asset_id == asset_id, ServerEvent.state == "open", ServerEvent.severity == "critical")
    if since is not None:
        stmt = stmt.where(ServerEvent.first_seen_at > since)
    return [m or "（BMC 没有给出文字）" for m in db.scalars(stmt.order_by(ServerEvent.first_seen_at.desc(), ServerEvent.occurred_at.desc()).limit(limit))]


def counts_24h(entries: list[dict[str, Any]], now: datetime) -> dict[str, tuple[int, str]]:
    """近 24 小时严重 / 警告条数与最新一条的文字，供 sel.*_24h 指标与告警规则使用。"""
    out: dict[str, tuple[int, str]] = {"critical": (0, ""), "warning": (0, "")}
    latest: dict[str, datetime] = {}
    for e in entries:
        sev, when = severity_of(e.get("severity")), _when(e.get("created"))
        if sev not in out or when is None or now - when > timedelta(hours=24):
            continue
        n, msg = out[sev]
        if sev not in latest or when > latest[sev]:
            latest[sev], msg = when, str(e.get("message") or "")[:200]
        out[sev] = (n + 1, msg)
    return out


def prune(db: Session, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    stmt = delete(ServerEvent).where(ServerEvent.occurred_at < now - timedelta(days=RETENTION_DAYS))
    return db.execute(stmt.execution_options(synchronize_session=False)).rowcount or 0
