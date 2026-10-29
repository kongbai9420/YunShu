"""通知出站队列：聚合、有界重试、租约校验与不确定结果。网络操作不持数据库事务。"""
from __future__ import annotations

import html
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import queue
from app.crypto import CredentialCipher
from app.models import Alert, AlertEvent, AlertRule, Asset, Credential, MaintenanceWindow, NotificationChannel, NotificationDelivery, NotificationIntent, Task
from app.notifications import NotificationError, NotificationMessage, send_notification
from app.server_events import SERVER_EVENT_METRIC
from app.services.credentials import reveal_secret
from app.worker import TaskContext, TaskError


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _suppressed(db: Session, event: AlertEvent, now: datetime) -> bool:
    from app.alerting import matches_scope
    alert = db.get(Alert, event.alert_id)
    if not event.notify or alert is None or alert.suppression_reason or alert.asset_id is None:
        return True
    rule = db.get(AlertRule, alert.rule_id)
    asset = db.get(Asset, alert.asset_id)
    from app.alerting import MAINTENANCE_METRIC
    # 维护超期提醒本身就是针对维护中的资产
    if rule is None or rule.deleted or not rule.enabled or asset is None or (asset.lifecycle != "active" and alert.metric_key != MAINTENANCE_METRIC):
        return True
    if event.episode != alert.episode or not matches_scope(asset, rule.asset_ids, rule.tags):
        return True
    if event.kind in {"trigger", "triggered", "repeat", "repeated"} and alert.state not in {"active", "acknowledged", "silenced"}:
        return True
    if event.kind in {"recover", "recovered"} and alert.state != "resolved":
        return True
    if alert.silenced_until and aware(alert.silenced_until) > now:
        return True
    # 已确认的告警只发恢复；服务器告警来了新的严重事件是例外（求值时只为新事件生成这种提醒）
    new_server_event = event.kind in {"repeat", "repeated"} and event.metric_key == SERVER_EVENT_METRIC
    if alert.acknowledged_by and event.kind not in {"recover", "recovered"} and not new_server_event:
        return True
    windows = db.scalars(select(MaintenanceWindow).where(MaintenanceWindow.starts_at <= now, MaintenanceWindow.ends_at > now))
    return any(matches_scope(asset, window.asset_ids, window.tags) for window in windows)


def render_message(db: Session, channel: NotificationChannel, events: list[AlertEvent]) -> NotificationMessage:
    if not events:
        return NotificationMessage("云枢通知测试", "这是一封云枢测试通知。服务端接受不代表收件人一定收到。", "<p>这是一封云枢测试通知。</p>")
    first = events[0]
    alert = db.get(Alert, first.alert_id)
    labels = {"triggered": "触发", "repeated": "重复提醒", "recovered": "恢复", "trigger": "触发", "repeat": "重复提醒", "recover": "恢复"}
    lines = []
    for e in events[:20]:
        if e.metric_key == "availability":
            reading = "端点已可达" if e.value == 1 else "端点不可达"
        elif e.metric_key == SERVER_EVENT_METRIC:
            reading = f"待处理的严重事件 {e.value or 0:g} 条（在“服务器告警”里处理）"
        elif e.metric_key == "asset.maintenance_overdue":
            reading = "维护已超过预计结束时间，仍未改回“在用”" if e.value else "已改回“在用”或延长了预计结束时间"
        elif e.metric_key == "inspection.critical":
            reading = f"最近一次巡检有 {e.value or 0:g} 项严重异常（在“巡检 → 巡检结果”里查看）"
        else:
            reading = f"当前值 {e.value if e.value is not None else '—'} {e.unit or ''} / 阈值 {e.threshold if e.threshold is not None else '—'}"
        severity = {"critical": "严重", "warning": "警告", "info": "提示"}.get(e.severity, e.severity)
        lines.append(f"[{labels.get(e.kind, e.kind)} · {severity}] {e.title[:300]}\n{reading}\n{e.message[:500]}")
    if len(events) > 20:
        lines.append(f"另有 {len(events)-20} 条，请在告警中心查看。")
    values = {
        "title": first.title if len(events) == 1 else f"云枢告警汇总（{len(events)} 条）",
        "message": "\n\n".join(lines), "severity": max((e.severity for e in events), key=lambda s: {"critical": 3, "warning": 2, "info": 1}.get(s, 0)),
        "asset_name": first.asset_name or (alert.asset_name if alert else ""), "metric_key": first.metric_key or (alert.metric_key if alert else ""),
        "value": first.value if first.value is not None else "—", "threshold": first.threshold if first.threshold is not None else "—",
        "unit": first.unit or "", "time": aware(first.created_at).isoformat(), "alert_count": len(events), "event_kind": first.kind,
    }
    cfg = channel.config
    title = cfg.get("subject_template", "{title}").format_map(values).replace("\r", " ").replace("\n", " ")[:400]
    text = cfg.get("text_template", "{message}").format_map(values)[:24000]
    html_template = cfg.get("html_template", "")
    rich = html_template.format_map({k: html.escape(str(v)) for k, v in values.items()})[:32000] if html_template else ""
    return NotificationMessage(title, text, rich)


def _channel_allows(db: Session, event: AlertEvent, channel: NotificationChannel) -> bool:
    from app.alerting import matches_scope
    alert = db.get(Alert, event.alert_id)
    if alert is None or alert.asset_id is None:
        return False
    rule = db.get(AlertRule, alert.rule_id)
    asset = db.get(Asset, alert.asset_id)
    return (rule is not None and asset is not None
            and (not rule.channel_ids or str(channel.id) in rule.channel_ids)
            and event.severity in (channel.config.get("severities") or ["info", "warning", "critical"])
            and matches_scope(asset, [], channel.config.get("tags", [])))


def enqueue_delivery(db: Session, delivery: NotificationDelivery, *, now: datetime | None = None) -> Task:
    db.flush()
    return queue.enqueue(db, "notification.send", payload={"delivery_id": str(delivery.id), "generation": delivery.generation},
                         idempotency_key=f"notify:{delivery.id}:{delivery.generation}", max_attempts=3, available_at=now)


def plan_notifications(db: Session, *, now: datetime | None = None) -> dict:
    """路由未处理事件，每渠道每聚合窗口最多一封、每封最多聚合 500 条。"""
    from app.alerting import matches_scope
    now = now or datetime.now(UTC)
    channels = list(db.scalars(select(NotificationChannel).where(NotificationChannel.enabled.is_(True), NotificationChannel.deleted.is_(False))))
    routed = 0
    events = list(db.scalars(select(AlertEvent).where(AlertEvent.routed.is_(False)).order_by(AlertEvent.created_at).limit(1000)))
    for event in events:
        event.routed = True
        if _suppressed(db, event, now):
            continue
        alert = db.get(Alert, event.alert_id)
        rule = db.get(AlertRule, alert.rule_id)
        asset = db.get(Asset, alert.asset_id)
        for channel in channels:
            if rule.channel_ids and str(channel.id) not in rule.channel_ids:
                continue
            if event.severity not in (channel.config.get("severities") or ["info", "warning", "critical"]):
                continue
            if not matches_scope(asset, [], channel.config.get("tags", [])):
                continue
            db.add(NotificationIntent(event_id=event.id, channel_id=channel.id, due_at=now + timedelta(seconds=channel.config.get("group_seconds", 30))))
            routed += 1
    db.flush()
    created = 0
    for channel in channels:
        interval = channel.config.get("group_seconds", 30)
        previous = db.scalar(select(NotificationDelivery).where(NotificationDelivery.channel_id == channel.id, NotificationDelivery.is_test.is_(False),
                                                                NotificationDelivery.kind == "alert").order_by(NotificationDelivery.created_at.desc()).limit(1))
        if previous and (now - aware(previous.created_at)).total_seconds() < interval:
            continue
        intents = list(db.scalars(select(NotificationIntent).where(NotificationIntent.channel_id == channel.id, NotificationIntent.delivery_id.is_(None), NotificationIntent.due_at <= now).order_by(NotificationIntent.due_at).limit(500)))
        if not intents:
            continue
        kept = [event for intent in intents if (event := db.get(AlertEvent, intent.event_id)) is not None and not _suppressed(db, event, now) and _channel_allows(db, event, channel)]
        # 将已被静默/维护抑制的意图记入取消投递，避免在窗口结束后迟发旧告警。
        message = render_message(db, channel, kept)
        delivery = NotificationDelivery(channel_id=channel.id, channel_name=channel.name, event_ids=[str(e.id) for e in kept],
                                        status="queued" if kept else "cancelled", error="" if kept else "通知已被静默或维护策略抑制",
                                        title=message.title, text=message.text, html=message.html, channel_revision=channel.revision, created_at=now,
                                        finished_at=None if kept else now)
        db.add(delivery)
        db.flush()
        for intent in intents:
            intent.delivery_id = delivery.id
        if kept:
            enqueue_delivery(db, delivery, now=now)
            created += 1
    _reconcile(db, now)
    return {"routed": routed, "deliveries": created}


def _reconcile(db: Session, now: datetime) -> None:
    for delivery in db.scalars(select(NotificationDelivery).where(NotificationDelivery.status == "sending")):
        task = db.scalar(select(Task).where(Task.kind == "notification.send", Task.lease_token == delivery.lease_token, Task.status == "running", Task.lease_expires_at > now))
        if task is None:
            delivery.status = "uncertain"
            delivery.error = "Worker 租约丢失，无法确定服务端是否已接受；不会自动重发"
            delivery.finished_at = now


def make_handler(cipher_provider: Callable[[], CredentialCipher], sender=None):
    sender = sender or send_notification

    def handle(ctx: TaskContext) -> dict:
        now = datetime.now(UTC)
        delivery_id = uuid.UUID(ctx.lease.payload["delivery_id"])
        generation = ctx.lease.payload.get("generation")
        with ctx.session_factory() as db:
            task = db.get(Task, ctx.lease.task_id)
            delivery = db.get(NotificationDelivery, delivery_id)
            if (task is None or task.status != "running" or task.lease_token != ctx.lease.token or task.lease_expires_at is None
                    or aware(task.lease_expires_at) <= now or delivery is None or delivery.generation != generation):
                return {"status": "superseded"}
            if delivery.status not in {"queued", "retrying", "sending"}:
                return {"status": delivery.status}
            if delivery.status == "sending":
                delivery.status = "uncertain"
                delivery.error = "前一投递的结果不确定；不会自动重发"
                delivery.finished_at = now
                db.commit()
                return {"status": "uncertain"}
            channel = db.get(NotificationChannel, delivery.channel_id)
            if channel is None or channel.deleted or channel.revision != delivery.channel_revision or (not delivery.is_test and not channel.enabled):
                delivery.status = "cancelled"
                delivery.error = "通知渠道已修改或停用，请核对新配置后重新测试"
                delivery.finished_at = now
                db.commit()
                return {"status": "cancelled"}
            if not delivery.is_test and delivery.kind != "vuln":
                current_events = [e for eid in delivery.event_ids if (e := db.get(AlertEvent, uuid.UUID(eid))) is not None and not _suppressed(db, e, now) and _channel_allows(db, e, channel)]
                if not current_events:
                    delivery.status = "cancelled"
                    delivery.error = "告警已静默、确认、维护或规则已停用，未发送"
                    delivery.finished_at = now
                    db.commit()
                    return {"status": "cancelled"}
                message = render_message(db, channel, current_events)
                delivery.event_ids = [str(e.id) for e in current_events]
                delivery.title, delivery.text, delivery.html = message.title, message.text, message.html
            else:
                message = NotificationMessage(delivery.title, delivery.text, delivery.html)
            secrets = {}
            try:
                credential = db.get(Credential, channel.credential_id) if channel.credential_id else None
                if credential is not None:
                    secrets = json.loads(reveal_secret(credential, cipher_provider()))
                elif channel.kind != "email":
                    raise ValueError("missing")
            except Exception:
                delivery.status = "failed"
                delivery.error = "通知凭据缺失或无法解密，请重新保存渠道凭据"
                delivery.finished_at = now
                db.commit()
                return {"status": "failed"}
            kind, config = channel.kind, dict(channel.config)
            delivery.status = "sending"
            delivery.lease_token = ctx.lease.token
            delivery.attempts += 1
            db.commit()
        if not ctx.heartbeat():
            return {"status": "superseded"}
        result = None
        failure = None
        try:
            result = sender(kind, config, secrets, message)
        except NotificationError as exc:
            failure = exc
        except Exception:
            failure = NotificationError("投递结果不确定，请核对服务端记录", uncertain=True)
        retry = False
        with ctx.session_factory() as db:
            task = db.get(Task, ctx.lease.task_id)
            delivery = db.get(NotificationDelivery, delivery_id)
            if (task is None or task.status != "running" or task.lease_token != ctx.lease.token
                    or task.lease_expires_at is None or aware(task.lease_expires_at) <= datetime.now(UTC)
                    or delivery is None or delivery.generation != generation or delivery.status != "sending" or delivery.lease_token != ctx.lease.token):
                return {"status": "superseded"}
            if failure is not None:
                retry = failure.retryable and not failure.uncertain and delivery.attempts < 3
                delivery.status = "uncertain" if failure.uncertain else "retrying" if retry else "failed"
                delivery.error = str(failure)[:500]
                if not retry:
                    delivery.finished_at = datetime.now(UTC)
            elif result and result.get("accepted"):
                delivery.status = "accepted"
                delivery.finished_at = datetime.now(UTC)
                refused = result.get("refused_count", 0)
                delivery.error = f"服务端接受了部分收件人；{refused} 个收件人被拒绝，未自动重发" if refused else ""
            else:
                delivery.status = "failed"
                delivery.error = "通知服务端未明确接受"
                delivery.finished_at = datetime.now(UTC)
            status = delivery.status
            db.commit()
        if retry:
            raise TaskError("通知暂时失败，等待退避重试", retryable=True)
        return {"status": status}

    return handle
