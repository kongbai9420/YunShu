"""P4 告警中心；读写权限、CSRF、配置修订和审计均在后端执行。"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app import audit
from app.alerts_schemas import AlertAction, ChannelIn, ChannelPatch, MaintenanceIn, RetryIn, RuleIn, RulePatch
from app.api.deps import editor, get_db, reader
from app.scope import require_all, restrict, scope_for
from app.models import Alert, AlertEvent, AlertRule, Asset, Credential, MaintenanceWindow, NotificationChannel, NotificationDelivery, User
from app.notification_queue import aware, enqueue_delivery, render_message
from app.services.credentials import create_credential, reveal_secret, set_secret

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _get(db, model, ident):
    value = db.get(model, ident)
    if value is None or getattr(value, "deleted", False):
        raise HTTPException(404, "记录不存在")
    return value


def _out(record, *, exclude=()):
    return {column.key: getattr(record, column.key) for column in record.__table__.columns if column.key not in exclude}


def _channel_out(channel):
    return {**_out(channel, exclude={"credential_id", "deleted"}), "has_secret": channel.credential_id is not None}


def _validate(model, data):
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(422, [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]) from None


def _refs(db, asset_ids=(), channel_ids=()):
    for ident in asset_ids:
        a = db.get(Asset, uuid.UUID(str(ident)))
        if a is None or a.deleted_at is not None:
            raise HTTPException(422, "所选资产不存在")
    for ident in channel_ids:
        _get(db, NotificationChannel, uuid.UUID(str(ident)))


@router.get("/summary")
def summary(user: User = Depends(reader), db: Session = Depends(get_db)):
    scope = scope_for(db, user)  # 范围外资产的告警全部隐藏（4.1 P11）
    counts = dict(db.execute(restrict(select(Alert.state, func.count()), Alert.asset_id, scope).group_by(Alert.state)).all())
    result = {state: counts.get(state, 0) for state in ("pending", "active", "acknowledged", "silenced", "resolved")}
    result["suppressed"] = db.scalar(restrict(select(func.count()).select_from(Alert), Alert.asset_id, scope).where(Alert.suppression_reason != "", Alert.state != "resolved")) or 0
    result["failed_deliveries"] = 0 if not scope.all else db.scalar(select(func.count()).select_from(NotificationDelivery).where(NotificationDelivery.status.in_(["failed", "uncertain"]))) or 0
    return result


@router.get("")
def alerts(state: str | None = None, asset_id: uuid.UUID | None = None, q: str | None = Query(None, max_length=100), limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), user: User = Depends(reader), db: Session = Depends(get_db)):
    stmt = restrict(select(Alert), Alert.asset_id, scope_for(db, user))
    if asset_id is not None:
        stmt = stmt.where(Alert.asset_id == asset_id)
    if state and state != "all":
        if state == "suppressed":
            stmt = stmt.where(Alert.suppression_reason != "", Alert.state != "resolved")
        else:
            stmt = stmt.where(Alert.state.in_(state.split(",")))
    if q:
        stmt = stmt.where(or_(Alert.asset_name.ilike(f"%{q}%"), Alert.title.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(stmt.order_by(Alert.last_seen_at.desc(), Alert.id).offset(offset).limit(limit))
    return {"items": [_out(a) for a in items], "total": total}


@router.get("/events")
def events(alert_id: uuid.UUID | None = None, q: str | None = Query(None, max_length=100), limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), user: User = Depends(reader), db: Session = Depends(get_db)):
    scope = scope_for(db, user)
    # 全部可见时也要过滤：回收站里资产的告警事件不显示
    stmt = select(AlertEvent).where(AlertEvent.alert_id.in_(restrict(select(Alert.id), Alert.asset_id, scope)))
    if alert_id:
        stmt = stmt.where(AlertEvent.alert_id == alert_id)
    if q:
        stmt = stmt.where(or_(AlertEvent.title.ilike(f"%{q}%"), AlertEvent.message.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    return {"items": [_out(e, exclude={"routed", "notify"}) for e in db.scalars(stmt.order_by(AlertEvent.created_at.desc(), AlertEvent.id).offset(offset).limit(limit))], "total": total}


@router.post("/{alert_id}/action")
def action(alert_id: uuid.UUID, body: AlertAction, actor: User = Depends(editor), db: Session = Depends(get_db)):
    from app.scope import can_see

    alert = _get(db, Alert, alert_id)
    if not can_see(db, scope_for(db, actor), alert.asset_id):
        raise HTTPException(404, "告警不存在")
    if alert.state in {"pending", "resolved"}:
        raise HTTPException(409, "仅能操作已触发且尚未恢复的告警")
    now = datetime.now(UTC)
    if body.action == "acknowledge":
        alert.acknowledged_by = actor.display_name or actor.username
        alert.state = "silenced" if alert.silenced_until and aware(alert.silenced_until) > now else "acknowledged"
    elif body.action == "silence":
        alert.silenced_until = now + timedelta(minutes=body.minutes)
        alert.state = "silenced"
    else:
        alert.silenced_until = None
        alert.state = "acknowledged" if alert.acknowledged_by else "active"
    action_text = {"acknowledge": "确认告警", "silence": f"静默告警 {body.minutes} 分钟", "unsilence": "解除静默"}[body.action]
    db.add(AlertEvent(alert_id=alert.id, kind=body.action, title=alert.title, message=f"{actor.display_name or actor.username} 已{action_text}", value=alert.value,
                      threshold=alert.threshold, unit=alert.unit, metric_key=alert.metric_key, asset_name=alert.asset_name,
                      severity=alert.severity, episode=alert.episode, notify=False))
    audit.record(db, actor, f"alert.{body.action}", target_type="alert", target_id=alert.id, summary=alert.title)
    db.flush()
    return _out(alert)


@router.get("/rules")
def rules(user: User = Depends(reader), db: Session = Depends(get_db)):
    require_all(scope_for(db, user), "告警规则")
    return [_out(r, exclude={"deleted"}) for r in db.scalars(select(AlertRule).where(AlertRule.deleted.is_(False)).order_by(AlertRule.created_at, AlertRule.id))]


@router.post("/rules", status_code=201)
def add_rule(body: RuleIn, actor: User = Depends(editor), db: Session = Depends(get_db)):
    require_all(scope_for(db, actor), "告警规则")
    _refs(db, body.asset_ids, body.channel_ids)
    rule = AlertRule(**body.model_dump(mode="json"))
    db.add(rule)
    db.flush()
    audit.record(db, actor, "alert.rule.create", target_type="alert_rule", target_id=rule.id, summary=rule.name)
    return _out(rule, exclude={"deleted"})


@router.patch("/rules/{rule_id}")
def update_rule(rule_id: uuid.UUID, body: RulePatch, actor: User = Depends(editor), db: Session = Depends(get_db)):
    require_all(scope_for(db, actor), "告警规则")
    rule = _get(db, AlertRule, rule_id)
    changes = body.model_dump(exclude_unset=True, exclude={"revision"}, mode="json")
    merged = {k: getattr(rule, k) for k in RuleIn.model_fields}
    validated = _validate(RuleIn, {**merged, **changes})
    _refs(db, validated.asset_ids, validated.channel_ids)
    data = validated.model_dump(mode="json")
    result = db.execute(update(AlertRule).where(AlertRule.id == rule_id, AlertRule.revision == body.revision, AlertRule.deleted.is_(False)).values(**data, revision=AlertRule.revision + 1))
    if result.rowcount != 1:
        raise HTTPException(409, "规则已被修改，请刷新后重试")
    db.expire(rule)
    audit.record(db, actor, "alert.rule.update", target_type="alert_rule", target_id=rule.id, summary=rule.name)
    return _out(rule, exclude={"deleted"})


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)):
    require_all(scope_for(db, actor), "告警规则")
    rule = _get(db, AlertRule, rule_id)
    rule.deleted, rule.enabled = True, False
    rule.revision += 1
    audit.record(db, actor, "alert.rule.delete", target_type="alert_rule", target_id=rule.id, summary=rule.name)


def _channel_secret(db, channel, request):
    credential = db.get(Credential, channel.credential_id) if channel.credential_id else None
    if credential is None:
        return {}
    try:
        return json.loads(reveal_secret(credential, request.app.state.cipher))
    except Exception:
        raise HTTPException(422, "渠道凭据无法解密，请重新填写") from None


def _check_secret(kind, secret):
    if kind == "email":
        if bool(secret.get("username")) != bool(secret.get("password")):
            raise HTTPException(422, "SMTP 用户名与授权码须同时填写，或同时留空使用已授权的匿名中继")
        return
    if kind in {"serverchan", "pushplus"} and (secret.get("secret") or secret.get("url")):
        return
    url = secret.get("url", "")
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme == "https" and parsed.hostname and parsed.username is None and parsed.password is None and not parsed.fragment and not any(c.isspace() for c in url)
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(422, "webhook 需要完整 HTTPS 地址，不允许在地址中填写登录用户名与密码")


def _store_secret(db, channel, secret, request):
    encoded = json.dumps(secret, ensure_ascii=False)
    if len(encoded) > 16000:
        raise HTTPException(422, "渠道凭据过长")
    if channel.credential_id:
        credential = db.get(Credential, channel.credential_id)
    else:
        credential = None
    if credential is None:
        credential = create_credential(db, request.app.state.cipher, alias=f"通知渠道-{uuid.uuid4()}", kind="token", secret=encoded, notes="由告警中心管理的通知凭据")
        db.flush()
        channel.credential_id = credential.id
    else:
        set_secret(credential, request.app.state.cipher, encoded)


@router.get("/channels")
def channels(user: User = Depends(reader), db: Session = Depends(get_db)):
    require_all(scope_for(db, user), "通知渠道")
    return [_channel_out(c) for c in db.scalars(select(NotificationChannel).where(NotificationChannel.deleted.is_(False)).order_by(NotificationChannel.created_at, NotificationChannel.id))]


@router.post("/channels", status_code=201)
def add_channel(body: ChannelIn, request: Request, actor: User = Depends(editor), db: Session = Depends(get_db)):
    require_all(scope_for(db, actor), "通知渠道")
    secret = body.secret.model_dump() if body.secret else {}
    if body.enabled or body.secret:
        _check_secret(body.kind, secret)
    channel = NotificationChannel(name=body.name, kind=body.kind, enabled=body.enabled, config=body.config.model_dump(mode="json"))
    db.add(channel)
    if body.secret:
        _store_secret(db, channel, secret, request)
    db.flush()
    audit.record(db, actor, "notification.channel.create", target_type="notification_channel", target_id=channel.id, summary=channel.name)
    return _channel_out(channel)


@router.patch("/channels/{channel_id}")
def update_channel(channel_id: uuid.UUID, body: ChannelPatch, request: Request, actor: User = Depends(editor), db: Session = Depends(get_db)):
    require_all(scope_for(db, actor), "通知渠道")
    channel = _get(db, NotificationChannel, channel_id)
    if channel.revision != body.revision:
        raise HTTPException(409, "通知渠道已被修改，请刷新后重试")
    changes = body.model_dump(exclude_unset=True, exclude={"revision", "secret"})
    validated = _validate(ChannelIn, {"name": changes.get("name", channel.name), "kind": channel.kind, "enabled": changes.get("enabled", channel.enabled), "config": changes.get("config", channel.config)})
    secret = None
    if body.secret:
        replacements = {key: value for key, value in body.secret.model_dump(exclude_unset=True).items() if value != ""}
        secret = {**_channel_secret(db, channel, request), **replacements}
    if validated.enabled or body.secret:
        _check_secret(channel.kind, secret if secret is not None else _channel_secret(db, channel, request))
    result = db.execute(update(NotificationChannel).where(NotificationChannel.id == channel_id, NotificationChannel.revision == body.revision, NotificationChannel.deleted.is_(False)).values(name=validated.name, enabled=validated.enabled, config=validated.config.model_dump(mode="json"), revision=NotificationChannel.revision + 1))
    if result.rowcount != 1:
        raise HTTPException(409, "通知渠道已被修改，请刷新后重试")
    db.expire(channel)
    if body.secret:
        _store_secret(db, channel, secret, request)
    audit.record(db, actor, "notification.channel.update", target_type="notification_channel", target_id=channel.id, summary=channel.name)
    db.flush()
    return _channel_out(channel)


@router.delete("/channels/{channel_id}", status_code=204)
def delete_channel(channel_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)):
    require_all(scope_for(db, actor), "通知渠道")
    channel = _get(db, NotificationChannel, channel_id)
    channel.deleted, channel.enabled = True, False
    channel.revision += 1
    audit.record(db, actor, "notification.channel.delete", target_type="notification_channel", target_id=channel.id, summary=channel.name)


@router.post("/channels/{channel_id}/test", status_code=202)
def test_channel(channel_id: uuid.UUID, request: Request, actor: User = Depends(editor), db: Session = Depends(get_db)):
    require_all(scope_for(db, actor), "通知渠道")
    channel = _get(db, NotificationChannel, channel_id)
    _check_secret(channel.kind, _channel_secret(db, channel, request))
    pending = db.scalar(select(NotificationDelivery).where(NotificationDelivery.channel_id == channel_id, NotificationDelivery.is_test.is_(True), NotificationDelivery.status.in_(["queued", "retrying", "sending"])))
    if pending:
        raise HTTPException(409, "已有测试正在排队或投递，请先查看投递结果")
    message = render_message(db, channel, [])
    delivery = NotificationDelivery(channel_id=channel_id, channel_name=channel.name, is_test=True, channel_revision=channel.revision, title=message.title, text=message.text, html=message.html)
    db.add(delivery)
    enqueue_delivery(db, delivery)
    audit.record(db, actor, "notification.test", target_type="notification_delivery", target_id=delivery.id, summary=f"向已保存的 {channel.name} 目标排队测试通知")
    return _out(delivery, exclude={"lease_token"})


@router.get("/deliveries")
def deliveries(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), user: User = Depends(reader), db: Session = Depends(get_db)):
    require_all(scope_for(db, user), "通知发送记录")
    total = db.scalar(select(func.count()).select_from(NotificationDelivery)) or 0
    return {"items": [_out(d, exclude={"lease_token"}) for d in db.scalars(select(NotificationDelivery).order_by(NotificationDelivery.created_at.desc(), NotificationDelivery.id).offset(offset).limit(limit))], "total": total}


@router.post("/deliveries/{delivery_id}/retry", status_code=202)
def retry_delivery(delivery_id: uuid.UUID, request: Request, body: RetryIn = Body(default=RetryIn()), actor: User = Depends(editor), db: Session = Depends(get_db)):
    require_all(scope_for(db, actor), "通知发送记录")
    delivery = _get(db, NotificationDelivery, delivery_id)
    if delivery.status not in {"failed", "uncertain", "cancelled"}:
        raise HTTPException(409, "仅能重试失败、取消或结果不确定的投递")
    if delivery.status == "uncertain" and not body.confirm_uncertain:
        raise HTTPException(409, "前次可能已被服务端接受；请确认允许重复后再手动重试")
    channel = _get(db, NotificationChannel, delivery.channel_id)
    if not delivery.is_test and not channel.enabled:
        raise HTTPException(409, "请先启用通知渠道")
    _check_secret(channel.kind, _channel_secret(db, channel, request))
    old_generation = delivery.generation
    result = db.execute(update(NotificationDelivery).where(NotificationDelivery.id == delivery_id, NotificationDelivery.generation == old_generation, NotificationDelivery.status.in_(["failed", "uncertain", "cancelled"])).values(status="queued", attempts=0, error="", finished_at=None, lease_token="", generation=old_generation+1, channel_revision=channel.revision))
    if result.rowcount != 1:
        raise HTTPException(409, "该投递已被其他请求重试，请刷新查看")
    db.expire(delivery)
    enqueue_delivery(db, delivery)
    audit.record(db, actor, "notification.retry", target_type="notification_delivery", target_id=delivery.id, summary="手动重试通知", detail={"confirmed_uncertain": body.confirm_uncertain})
    return _out(delivery, exclude={"lease_token"})


@router.get("/maintenance")
def maintenance(user: User = Depends(reader), db: Session = Depends(get_db)):
    require_all(scope_for(db, user), "维护窗口")
    return [_out(w) for w in db.scalars(select(MaintenanceWindow).order_by(MaintenanceWindow.starts_at.desc()).limit(500))]


@router.post("/maintenance", status_code=201)
def add_maintenance(body: MaintenanceIn, actor: User = Depends(editor), db: Session = Depends(get_db)):
    require_all(scope_for(db, actor), "维护窗口")
    _refs(db, body.asset_ids)
    data = body.model_dump(exclude={"all_assets"})
    data["asset_ids"] = [str(ident) for ident in body.asset_ids]
    window = MaintenanceWindow(**data)
    db.add(window)
    db.flush()
    audit.record(db, actor, "alert.maintenance.create", target_type="maintenance", target_id=window.id, summary=window.name,
                 detail={"all_assets": body.all_assets})
    return _out(window)


@router.put("/maintenance/{window_id}")
def update_maintenance(window_id: uuid.UUID, body: MaintenanceIn, actor: User = Depends(editor), db: Session = Depends(get_db)):
    require_all(scope_for(db, actor), "维护窗口")
    window = _get(db, MaintenanceWindow, window_id)
    _refs(db, body.asset_ids)
    window.name, window.starts_at, window.ends_at = body.name, body.starts_at, body.ends_at
    window.asset_ids, window.tags = [str(ident) for ident in body.asset_ids], body.tags
    audit.record(db, actor, "alert.maintenance.update", target_type="maintenance", target_id=window.id, summary=window.name,
                 detail={"all_assets": body.all_assets})
    db.flush()
    return _out(window)


@router.delete("/maintenance/{window_id}", status_code=204)
def delete_maintenance(window_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)):
    require_all(scope_for(db, actor), "维护窗口")
    window = _get(db, MaintenanceWindow, window_id)
    audit.record(db, actor, "alert.maintenance.delete", target_type="maintenance", target_id=window.id, summary=window.name)
    db.delete(window)



# ---- 4.3 P18f：AI 告警解读 ----


def _ai_allowed(db: Session, user: User) -> None:
    require_all(scope_for(db, user), "AI 告警解读")  # 解读会带上上下游资产的信息


@router.get("/{alert_id}/explain")
def get_explain(alert_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    from app import ai_notes, ai_runtime

    _ai_allowed(db, user)
    enabled = ai_runtime.feature_enabled(ai_runtime.settings_row(db), ai_notes.ALERT_FEATURE)
    return {"enabled": enabled, "note": ai_notes.out(ai_notes.latest(db, "alert", str(alert_id)))}


@router.post("/{alert_id}/explain")
def run_explain(alert_id: uuid.UUID, request: Request, force: bool = False, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    from app import ai_notes
    from app.ai.protocols import AiError

    _ai_allowed(db, actor)
    try:
        row = ai_notes.explain_alert(db, request.app.state.cipher, alert_id, user_name=actor.username, force=force,
                                     client=getattr(request.app.state, "ai_http_client", None))
    except AiError as exc:
        db.commit()
        raise HTTPException(422 if exc.kind in ("disabled", "quota", "not_found") else 502, {"kind": exc.kind, "message": str(exc)}) from None
    audit.record(db, actor, "alert.ai.explain", target_type="alert", target_id=alert_id, summary=f"AI 告警解读（{row.model}）")
    return {"enabled": True, "note": ai_notes.out(row)}
