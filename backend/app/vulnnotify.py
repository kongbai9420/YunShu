"""漏洞情报通知（4.3 P18e）。

- 即时：命中本地资产，且已被利用（KEV）或 CVSS ≥ 9.0（默认值）；
  同一漏洞一条消息，列出受影响资产与发布来源。新漏洞第一次出现后等一个汇总窗口（默认 5 分钟），
  让几乎同时发布的其他来源并入同一条；之后才出现的来源只更新详情与每日汇总，除非漏洞升级（新列入 KEV）。
- 订阅命中预警：新漏洞命中订阅的产品或关键词、同样严重，但还没有版本数据能确认本地影响时，先发“待确认”；
  之后版本比对确认命中本地时再发“漏洞确认”。拉到新数据或收到推送后立即检查，不等定时。
- 每日汇总：订阅范围内的新漏洞（每行带来源列表）、新命中、待处理与到期复查数量。
- 发送走现有通知渠道与出站队列（投递记录里可查），处于维护窗口或非在用的资产不发即时通知。
- 开启“漏洞影响分析”时，即时通知附 AI 一句话结论；AI 失败不影响发送。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.crypto import CredentialCipher
from app.inspection import _local_tz
from app.models import Asset, MaintenanceWindow, NotificationChannel, NotificationDelivery, VulnAffect, VulnMatch, VulnRecord, VulnSighting, VulnSubscription
from app.notification_queue import aware, enqueue_delivery
from app.vulndb import remediation, subscriptions
from app.vulndb.normalize import SEVERITY_RANK, SEVERITY_TEXT, SOURCE_LABEL
from app.vulndb.sync import settings_row
from app.vulnmatch import ALERT_STATUSES, is_alertable

log = logging.getLogger(__name__)
MAX_LINES = 30


def _channels(db: Session) -> list[NotificationChannel]:
    s = settings_row(db)
    chans = list(db.scalars(select(NotificationChannel).where(NotificationChannel.enabled.is_(True), NotificationChannel.deleted.is_(False))))
    if s.notify_channel_ids:
        chans = [c for c in chans if str(c.id) in s.notify_channel_ids]
    return chans


def _send(db: Session, title: str, text: str, now: datetime) -> int:
    n = 0
    for ch in _channels(db):
        d = NotificationDelivery(channel_id=ch.id, channel_name=ch.name, event_ids=[], status="queued", error="", title=title[:400], text=text[:24000],
                                 html="", channel_revision=ch.revision, created_at=now, kind="vuln", is_test=False)
        db.add(d)
        enqueue_delivery(db, d, now=now)
        n += 1
    return n


def _sources(db: Session, record_id: str) -> list[str]:
    return [SOURCE_LABEL.get(s, s) for (s,) in db.execute(select(VulnSighting.source).where(VulnSighting.record_id == record_id).order_by(VulnSighting.seen_at, VulnSighting.source))]


def _in_maintenance(asset: Asset, windows: list[MaintenanceWindow]) -> bool:
    from app.alerting import matches_scope

    return any(matches_scope(asset, w.asset_ids, w.tags) for w in windows)


def _score_text(rec: VulnRecord) -> str:
    parts = [SEVERITY_TEXT.get(rec.severity, rec.severity) + (f" {rec.cvss:g}" if rec.cvss is not None else "")]
    if rec.kev:
        parts.append("已被利用（CISA KEV）" + ("，已用于勒索" if rec.kev_ransomware else ""))
    return "｜".join(parts)


def instant(db: Session, *, now: datetime | None = None, cipher: CredentialCipher | None = None, ai_client=None) -> int:  # type: ignore[no-untyped-def]
    """发送到期的即时通知，返回发出的消息数（按漏洞计）。"""
    now = now or datetime.now(UTC)
    s = settings_row(db)
    if not s.notify_enabled:
        return 0
    window = timedelta(minutes=max(0, s.aggregation_minutes))
    windows = [w for w in db.scalars(select(MaintenanceWindow).where(MaintenanceWindow.starts_at <= now, MaintenanceWindow.ends_at > now))]
    rows = db.execute(select(VulnMatch, VulnRecord, Asset).join(VulnRecord, VulnRecord.id == VulnMatch.record_id).join(Asset, Asset.id == VulnMatch.asset_id)
                      .where(VulnMatch.active.is_(True), VulnMatch.status.in_(ALERT_STATUSES), Asset.lifecycle == "active",
                             or_(VulnMatch.notified_at.is_(None), VulnMatch.notified_kev.is_(False) & VulnRecord.kev.is_(True)))).all()
    groups: dict[str, list[tuple[VulnMatch, Asset]]] = defaultdict(list)
    records: dict[str, VulnRecord] = {}
    for m, rec, asset in rows:
        if not is_alertable(rec) or _in_maintenance(asset, windows):
            continue
        if m.notified_at is None and aware(rec.first_seen_at) + window > now:
            continue  # 新漏洞：等汇总窗口，让其他来源的发布并入同一条
        groups[rec.id].append((m, asset))
        records[rec.id] = rec
    sent = 0
    for rid, items in groups.items():
        rec = records[rid]
        upgrade = all(m.notified_at is not None for m, _ in items)
        names = sorted({a.name for _, a in items})
        fixed = sorted({m.fixed_version for m, _ in items if m.fixed_version})
        possible = sum(1 for m, _ in items if m.confidence == "possible")
        ai_line = _ai_conclusion(db, cipher, rid, ai_client) if cipher is not None else ""
        head = "【漏洞升级】" if upgrade else "【漏洞确认】" if rec.early_notified_at else "【漏洞预警】"
        title = f"{head}{rid} {rec.title}"[:400]
        lines = [
            f"{rid} {rec.title}",
            f"等级：{_score_text(rec)}",
            f"发布来源：{'、'.join(_sources(db, rid)) or '—'}",
            f"影响本地 {len(names)} 台：{'、'.join(names[:10])}{' 等' if len(names) > 10 else ''}" + (f"（其中 {possible} 台为可能受影响，需人工核对）" if possible else ""),
        ]
        if fixed:
            lines.append(f"修复版本：{'、'.join(fixed[:5])}")
        advice = remediation.headline(rec)
        if advice:
            lines.append(advice)
        if rec.kev and rec.kev_due:
            lines.append(f"CISA 要求处置期限：{rec.kev_due}")
        if upgrade:
            lines.append("变化：该漏洞已被列入 CISA KEV（已被真实利用）")
        if ai_line:
            lines.append(f"AI 结论（仅供参考）：{ai_line}")
        lines.append(f"详情：#/vulns?id={rid}")
        _send(db, title, "\n".join(lines), now)
        for m, _ in items:
            m.notified_at = m.notified_at or now
            m.notified_kev = m.notified_kev or rec.kev
        sent += 1
    if s.early_warning:
        sent += early(db, now=now, window=window)
    db.flush()
    return sent


EARLY_MAX_AGE = timedelta(days=2)
EARLY_PER_RUN = 10


def is_urgent(rec: VulnRecord) -> bool:
    """预警的门槛与即时通知相同；国内来源常常只有等级没有 CVSS，此时按“严重”算。"""
    return is_alertable(rec) or (rec.cvss is None and rec.severity == "critical")


def early(db: Session, *, now: datetime, window: timedelta) -> int:
    """订阅命中预警：还没有本地命中、命中了订阅、足够严重的新漏洞，先发一条“待确认”。每个漏洞只发一次。"""
    subs = subscriptions.enabled_subscriptions(db)
    if not subs:
        return 0
    matched = select(VulnMatch.record_id).where(VulnMatch.active.is_(True))
    rows = db.scalars(select(VulnRecord).where(VulnRecord.early_notified_at.is_(None), VulnRecord.first_seen_at >= now - EARLY_MAX_AGE,
                                               VulnRecord.first_seen_at <= now - window, VulnRecord.id.not_in(matched))
                      .order_by(VulnRecord.first_seen_at)).all()
    sent = 0
    for rec in rows:
        if sent >= EARLY_PER_RUN:
            break
        if not is_urgent(rec):
            continue
        products = {p for (p,) in db.execute(select(VulnAffect.product).where(VulnAffect.record_id == rec.id))}
        rel = subscriptions.relevance(subs, title=rec.title, summary=rec.summary, products=products, severity=rec.severity, kev=rec.kev)
        if not rel.labels:
            continue
        lines = [
            f"{rec.id} {rec.title}",
            f"等级：{_score_text(rec)}",
            f"发布来源：{'、'.join(_sources(db, rec.id)) or '—'}",
            f"命中订阅：{'、'.join(rel.labels[:5])}" + ("（按受影响产品）" if rel.by_version_data else "（标题或描述提到）"),
            "本地影响：待确认。还没有能与本地版本比对的数据，确认命中后会再发一条“漏洞确认”。",
        ]
        advice = remediation.headline(rec)
        if advice:
            lines.append(advice)
        if rec.kev and rec.kev_due:
            lines.append(f"CISA 要求处置期限：{rec.kev_due}")
        lines.append(f"详情：#/vulns?id={rec.id}")
        _send(db, f"【漏洞预警·待确认】{rec.id} {rec.title}"[:400], "\n".join(lines), now)
        rec.early_notified_at = now
        sent += 1
    return sent


def _ai_conclusion(db: Session, cipher: CredentialCipher, record_id: str, client) -> str:  # type: ignore[no-untyped-def]
    from app import ai_runtime, vuln_ai
    from app.ai.protocols import AiError

    if not ai_runtime.feature_enabled(ai_runtime.settings_row(db), vuln_ai.FEATURE):
        return ""
    try:
        return str(vuln_ai.analyze(db, cipher, record_id, user_name="漏洞通知", client=client).result.get("conclusion", ""))[:160]
    except AiError as exc:
        log.info("AI 分析失败，通知照常发送：%s", exc.kind)
        return ""


# ---------------- 每日汇总 ----------------


def subscribed(db: Session, rec: VulnRecord, subs: list[VulnSubscription], affects: dict[str, list[VulnAffect]]) -> VulnSubscription | None:
    text = f"{rec.title}\n{rec.summary}".lower()
    for s in subs:
        if s.kev_only and not rec.kev:
            continue
        if SEVERITY_RANK.get(rec.severity, 4) > SEVERITY_RANK.get(s.min_severity, 4) and s.min_severity != "unknown":
            continue
        if s.kind == "keyword" and s.value.lower() in text:
            return s
        if s.kind == "product" and any(a.kind == "cpe" and a.product == s.value for a in affects.get(rec.id, [])):
            return s
        if s.kind == "ecosystem" and any(a.kind == "pkg" and a.ecosystem == s.value for a in affects.get(rec.id, [])):
            return s
    return None


def digest_due(db: Session, now: datetime) -> bool:
    s = settings_row(db)
    local = now.astimezone(_local_tz())
    if local.hour < s.digest_hour:
        return False
    if s.last_digest_at is None:
        return True
    return aware(s.last_digest_at).astimezone(_local_tz()).date() < local.date()


def digest(db: Session, *, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    s = settings_row(db)
    if not force and (not s.notify_enabled or not digest_due(db, now)):
        return {"sent": False}
    since = aware(s.last_digest_at) if s.last_digest_at else now - timedelta(days=1)
    recent = now - timedelta(days=30)
    subs = list(db.scalars(select(VulnSubscription).where(VulnSubscription.enabled.is_(True))))
    new_recs = list(db.scalars(select(VulnRecord).where(VulnRecord.first_seen_at > since)
                               .where(or_(VulnRecord.published.is_(None), VulnRecord.published >= recent, VulnRecord.kev_added >= recent.date().isoformat()))
                               .order_by(VulnRecord.kev.desc(), VulnRecord.cvss.desc().nulls_last()).limit(2000)))
    affects: dict[str, list[VulnAffect]] = defaultdict(list)
    if new_recs:
        for a in db.scalars(select(VulnAffect).where(VulnAffect.record_id.in_([r.id for r in new_recs]))):
            affects[a.record_id].append(a)
    lines_new = []
    for rec in new_recs:
        if subscribed(db, rec, subs, affects) is None:
            continue
        lines_new.append(f"· {rec.id} {rec.title[:80]}（{_score_text(rec)}；来源：{'、'.join(_sources(db, rec.id))}）")
    new_hits = db.execute(select(VulnMatch.record_id, func.count(func.distinct(VulnMatch.asset_id))).where(VulnMatch.first_seen_at > since, VulnMatch.active.is_(True))
                          .group_by(VulnMatch.record_id)).all()
    pending = db.scalar(select(func.count(func.distinct(VulnMatch.record_id))).where(VulnMatch.active.is_(True), VulnMatch.status.in_(ALERT_STATUSES))) or 0
    review = db.scalar(select(func.count(VulnMatch.id)).where(VulnMatch.status.in_(("accepted", "mitigated")), VulnMatch.review_at.is_not(None),
                                                               VulnMatch.review_at <= (now + timedelta(days=7)).date())) or 0
    if not lines_new and not new_hits and not force:
        s.last_digest_at = now
        return {"sent": False, "reason": "没有新内容"}
    text = [f"云枢漏洞情报每日汇总（{now.astimezone(_local_tz()):%Y-%m-%d}）", ""]
    text.append(f"订阅范围内的新漏洞：{len(lines_new)} 个")
    text += lines_new[:MAX_LINES] + ([f"……另有 {len(lines_new) - MAX_LINES} 个，请到平台查看"] if len(lines_new) > MAX_LINES else [])
    text += ["", f"新命中本地资产的漏洞：{len(new_hits)} 个"]
    text += [f"· {rid}（{n} 台）" for rid, n in new_hits[:MAX_LINES]]
    text += ["", f"待处理的漏洞：{pending} 个；7 天内到期复查的处置：{review} 条", "详情：#/vulns"]
    sent = _send(db, f"云枢漏洞情报每日汇总：新漏洞 {len(lines_new)} 个，新命中 {len(new_hits)} 个", "\n".join(text), now)
    s.last_digest_at = now
    db.flush()
    return {"sent": bool(sent), "new": len(lines_new), "hits": len(new_hits), "pending": pending, "channels": sent}


def make_handler(cipher_factory):  # type: ignore[no-untyped-def]
    def handle(ctx) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        with ctx.session_factory() as db:
            n = instant(db, cipher=cipher_factory())
            d = digest(db)
            db.commit()
            return {"instant": n, "digest": d}

    return handle


NOTIFY_TASK = "vuln.notify"


def enqueue_due(db: Session, *, now: datetime | None = None) -> int:
    from app import queue
    from app.models import Task

    now = now or datetime.now(UTC)
    key = f"vulnnotify:{int(now.timestamp()) // 60}"
    if db.scalar(select(Task.id).where(Task.idempotency_key == key)) is not None:
        return 0
    queue.enqueue(db, NOTIFY_TASK, payload={}, idempotency_key=key, max_attempts=1)
    return 1


def enqueue_now(db: Session) -> int:
    """拉到新数据或收到推送后立即检查一次（不与定时检查去重）。"""
    import uuid

    from app import queue

    queue.enqueue(db, NOTIFY_TASK, payload={}, idempotency_key=f"vulnnotify:e:{uuid.uuid4()}", max_attempts=1)
    return 1
