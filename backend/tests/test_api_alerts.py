"""告警中心 API 权限、事务、秘密和真实持久队列；从不进行外部投递。"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.alert_defaults import ensure_rules
from app.alerting import evaluate
from app.models import Alert, AlertEvent, AlertRule, Asset, AuditEvent, CollectionStatus, Credential, LatestMetric, LegacyMapping, NotificationChannel, NotificationDelivery, Task
from app.notification_queue import make_handler, plan_notifications
from app.services.credentials import reveal_secret
from app.worker import Worker
from tests.api_helpers import add_user, login


@pytest.fixture
def operator(app_factory, factory):
    add_user(factory, "fake_alert_operator", "operator")
    return login(app_factory(), "fake_alert_operator")


@pytest.fixture
def viewer(app_factory, factory):
    add_user(factory, "fake_alert_viewer", "viewer")
    return login(app_factory(), "fake_alert_viewer")


EMAIL = {"name": "fake-mail", "kind": "email", "config": {"host": "smtp.example.invalid", "from_address": "sender@example.invalid", "recipients": ["receiver@example.invalid"]},
         "secret": {"username": "fake-smtp-user", "password": "fake-mail-password"}}


def test_seed_rules_idempotent_and_migrate_old_five_without_overwriting_edits(operator, factory):
    initial = operator.get("/api/alerts/rules").json()
    assert len(initial) == 14  # 维护超期+ 4.4 P21 巡检严重异常 + 旧五条 + 端点不可达 + P5 温控脱管 / 熔断 + 4.2 P13 BMC 日志（停用）+ P15 备份失败 / UPS 电池供电 + 4.4 P20c 服务器严重事件
    cpu = next(r for r in initial if r["legacy_id"] == "server_cpu")
    assert operator.patch(f"/api/alerts/rules/{cpu['id']}", json={"revision": 1, "threshold": 77, "recovery_threshold": 70}).status_code == 200
    with factory() as db:
        db.add(LegacyMapping(source="yunshu-3.x", legacy_kind="alert_settings", legacy_id="global", target_type="pending",
                             payload={"node_cpu_temp_threshold": "81", "server_cpu_threshold": "91", "server_mem_enabled": "false", "cooldown_sec": "120"}))
        db.commit()
        ensure_rules(db)
        ensure_rules(db)
        db.commit()
    rules = operator.get("/api/alerts/rules").json()
    assert len(rules) == 14
    by_key = {r["legacy_id"]: r for r in rules}
    assert by_key["server_cpu"]["threshold"] == 77
    assert by_key["node_cpu_temp"]["threshold"] == 81
    assert by_key["node_cpu_temp"]["recovery_threshold"] == 81
    assert by_key["node_cpu_temp"]["cooldown_seconds"] == 120
    assert by_key["server_mem"]["enabled"] is False
    channels = operator.get("/api/alerts/channels").json()
    assert len(channels) == 1 and channels[0]["kind"] == "custom_webhook"
    assert channels[0]["enabled"] is False and channels[0]["has_secret"] is False


def test_availability_rule_normalizes_its_evidence_key(operator):
    response = operator.post("/api/alerts/rules", json={"name": "fake-offline", "kind": "availability", "consecutive_samples": 3})
    assert response.status_code == 201
    assert response.json()["metric_key"] == "availability"


def test_rule_validation_revision_and_soft_delete_keeps_history(operator, factory):
    response = operator.post("/api/alerts/rules", json={"name": "fake-rule", "threshold": 80, "recovery_threshold": 90})
    assert response.status_code == 422
    rule = operator.post("/api/alerts/rules", json={"name": "fake-rule"}).json()
    assert operator.patch(f"/api/alerts/rules/{rule['id']}", json={"revision": 1, "enabled": False}).status_code == 200
    assert operator.patch(f"/api/alerts/rules/{rule['id']}", json={"revision": 1, "name": "stale"}).status_code == 409
    assert operator.delete(f"/api/alerts/rules/{rule['id']}").status_code == 204
    assert rule["id"] not in [r["id"] for r in operator.get("/api/alerts/rules").json()]
    with factory() as db:
        assert db.get(AlertRule, uuid.UUID(rule["id"])).deleted


def test_channel_encrypts_secrets_preserves_empty_edit_and_masks_errors(operator, factory, app_factory):
    url = "https://192.0.2.61/robot/send?access_token=fake-secret-in-url"
    response = operator.post("/api/alerts/channels", json={"name": "fake-ding", "kind": "dingtalk", "secret": {"url": url, "secret": "fake-signing-key"}})
    assert response.status_code == 201, response.text
    channel = response.json()
    assert channel["has_secret"]
    assert "fake-secret-in-url" not in response.text and "fake-signing-key" not in response.text
    patch = operator.patch(f"/api/alerts/channels/{channel['id']}", json={"revision": 1, "name": "fake-renamed"})
    assert patch.status_code == 200
    patch = operator.patch(f"/api/alerts/channels/{channel['id']}", json={"revision": 2, "secret": {"secret": "fake-new-sign-key"}})
    assert patch.status_code == 200
    with factory() as db:
        saved = db.get(NotificationChannel, uuid.UUID(channel["id"]))
        cred = db.get(Credential, saved.credential_id)
        revealed = json.loads(reveal_secret(cred, app_factory.cipher))
        assert revealed["url"] == url and revealed["secret"] == "fake-new-sign-key"
        for event in db.scalars(select(AuditEvent)):
            assert url not in str(event.detail) and "fake-new-sign-key" not in str(event.detail)
    listed = operator.get("/api/alerts/channels")
    assert url not in listed.text and "fake-new-sign-key" not in listed.text
    invalid = operator.patch(f"/api/alerts/channels/{channel['id']}", json={"revision": 3, "secret": {"url": "http://192.0.2.61/fake-secret-in-url"}})
    assert invalid.status_code == 422
    assert "fake-secret-in-url" not in invalid.text


def test_partial_smtp_secret_rotation_keeps_username(operator, factory, app_factory):
    channel = operator.post("/api/alerts/channels", json=EMAIL).json()
    rotated = operator.patch(f"/api/alerts/channels/{channel['id']}", json={"revision": 1, "secret": {"password": "fake-rotated-mail-password"}})
    assert rotated.status_code == 200, rotated.text
    blank = operator.patch(f"/api/alerts/channels/{channel['id']}", json={"revision": 2, "secret": {"username": "", "password": ""}})
    assert blank.status_code == 200, blank.text
    with factory() as db:
        channel_row = db.get(NotificationChannel, uuid.UUID(channel["id"]))
        secret = json.loads(reveal_secret(db.get(Credential, channel_row.credential_id), app_factory.cipher))
        assert secret["username"] == "fake-smtp-user" and secret["password"] == "fake-rotated-mail-password"


@pytest.mark.parametrize("patch", [
    {"config": {**EMAIL["config"], "password": "fake-forbidden-password"}},
    {"secret": {"password": "fake-oversize-" * 300}},
    {"config": {**EMAIL["config"], "subject_template": "{message.__class__}"}},
    {"config": {**EMAIL["config"], "text_template": "{message:999999999}"}},
])
def test_bad_channel_input_has_no_secret_echo(operator, patch):
    response = operator.post("/api/alerts/channels", json={**EMAIL, **patch})
    assert response.status_code == 422
    assert "fake-forbidden-password" not in response.text and "fake-oversize-" not in response.text


def test_notification_test_is_durable_and_reaches_fake_sender_only(operator, factory, app_factory):
    channel = operator.post("/api/alerts/channels", json=EMAIL).json()
    response = operator.post(f"/api/alerts/channels/{channel['id']}/test", json={})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert operator.post(f"/api/alerts/channels/{channel['id']}/test", json={}).status_code == 409
    calls = []
    worker = Worker(factory, {"notification.send": make_handler(lambda: app_factory.cipher, lambda *args: calls.append(args) or {"accepted": True})})
    try:
        assert worker.run_once() == "succeeded"
    finally:
        worker._pool.shutdown(wait=True)
    assert len(calls) == 1 and calls[0][2]["password"] == EMAIL["secret"]["password"]
    delivered = operator.get("/api/alerts/deliveries").json()["items"][0]
    assert delivered["status"] == "accepted" and "lease_token" not in delivered
    assert operator.post(f"/api/alerts/deliveries/{delivered['id']}/retry", json={}).status_code == 409


def test_uncertain_manual_retry_requires_explicit_confirmation(operator, factory):
    channel = operator.post("/api/alerts/channels", json=EMAIL).json()
    response = operator.post(f"/api/alerts/channels/{channel['id']}/test", json={}).json()
    with factory() as db:
        db.get(NotificationDelivery, uuid.UUID(response["id"])).status = "uncertain"
        db.commit()
    assert operator.post(f"/api/alerts/deliveries/{response['id']}/retry", json={}).status_code == 409
    retried = operator.post(f"/api/alerts/deliveries/{response['id']}/retry", json={"confirm_uncertain": True})
    assert retried.status_code == 202
    assert retried.json()["generation"] == 2


def test_alert_lifecycle_actions_and_restart_persistence(operator, factory, app_factory):
    now = datetime.now(UTC)
    with factory() as db:
        asset = Asset(name="fake-hot-server", form="physical", role="server")
        db.add(asset)
        db.flush()
        db.add(LatestMetric(asset_id=asset.id, metric_key="cpu.load", value=99, unit="%", quality="valid", source="ssh", observed_at=now))
        evaluate(db, now=now)
        db.commit()
    alert = operator.get("/api/alerts", params={"state": "active"}).json()["items"][0]
    assert operator.post(f"/api/alerts/{alert['id']}/action", json={"action": "acknowledge"}).json()["state"] == "acknowledged"
    assert operator.post(f"/api/alerts/{alert['id']}/action", json={"action": "silence", "minutes": 60}).json()["state"] == "silenced"
    assert operator.post(f"/api/alerts/{alert['id']}/action", json={"action": "unsilence"}).json()["state"] == "acknowledged"
    restarted = login(app_factory(), "fake_alert_operator")
    assert restarted.get("/api/alerts", params={"state": "acknowledged"}).json()["total"] == 1
    assert restarted.get("/api/alerts/events", params={"alert_id": alert["id"]}).json()["total"] >= 4


def test_maintenance_validation_scope_and_permissions(operator, viewer):
    now = datetime.now(UTC)
    body = {"name": "fake-maintenance", "starts_at": now.isoformat(), "ends_at": (now+timedelta(hours=1)).isoformat()}
    assert operator.post("/api/alerts/maintenance", json={**body, "ends_at": now.isoformat()}).status_code == 422
    # 不选资产、不填标签：必须明确勾选“对全部资产生效”，否则不保存
    assert operator.post("/api/alerts/maintenance", json=body).status_code == 422
    assert operator.post("/api/alerts/maintenance", json={**body, "tags": ["lab"], "all_assets": True}).status_code == 422
    window = operator.post("/api/alerts/maintenance", json={**body, "all_assets": True}).json()
    assert window["asset_ids"] == [] and window["tags"] == []
    # 可以修改
    changed = operator.put(f"/api/alerts/maintenance/{window['id']}", json={**body, "name": "fake-maintenance-2", "tags": ["lab"]})
    assert changed.status_code == 200 and changed.json()["name"] == "fake-maintenance-2" and changed.json()["tags"] == ["lab"]
    assert operator.put(f"/api/alerts/maintenance/{window['id']}", json=body).status_code == 422
    assert viewer.put(f"/api/alerts/maintenance/{window['id']}", json={**body, "all_assets": True}).status_code == 403
    for path in ("/api/alerts", "/api/alerts/events", "/api/alerts/summary", "/api/alerts/rules", "/api/alerts/channels", "/api/alerts/deliveries", "/api/alerts/maintenance"):
        assert viewer.get(path).status_code == 200
    assert viewer.post("/api/alerts/rules", json={"name": "fake-denied"}).status_code == 403
    assert viewer.post("/api/alerts/channels", json=EMAIL).status_code == 403
    assert viewer.delete(f"/api/alerts/maintenance/{window['id']}").status_code == 403
    assert operator.post("/api/alerts/rules", json={"name": "fake-csrf-denied"}, headers={"X-CSRF-Token": "fake-wrong"}).status_code == 403
    assert operator.delete(f"/api/alerts/maintenance/{window['id']}").status_code == 204


def test_suppressed_filter_and_event_pagination(operator, factory):
    now = datetime.now(UTC)
    with factory() as db:
        rule = db.scalar(select(AlertRule).where(AlertRule.legacy_id == "server_cpu"))
        asset = Asset(name="fake-maintenance-asset", form="physical", role="server")
        db.add(asset)
        db.flush()
        alert = Alert(rule_id=rule.id, asset_id=asset.id, metric_key="cpu.load", title="fake-history", state="active", suppression_reason="维护窗口")
        db.add(alert)
        db.flush()
        db.add_all([AlertEvent(alert_id=alert.id, kind="repeat", title=f"fake-history-{i}", message="fake-value", notify=False, created_at=now+timedelta(seconds=i), value=i, threshold=90) for i in range(120)])
        db.commit()
    assert operator.get("/api/alerts", params={"state": "suppressed"}).json()["total"] == 1
    response = operator.get("/api/alerts/events", params={"q": "fake-history", "limit": 50, "offset": 100}).json()
    assert response["total"] == 120 and len(response["items"]) == 20
    assert response["items"][0]["value"] == 19 and response["items"][0]["threshold"] == 90


def test_outage_to_mail_and_dingtalk_then_recovery_survives_restart(operator, factory, app_factory):
    now = datetime.now(UTC)
    mail = operator.post("/api/alerts/channels", json={**EMAIL, "enabled": True, "config": {**EMAIL["config"], "group_seconds": 10, "severities": []}}).json()
    ding = operator.post("/api/alerts/channels", json={"name": "fake-ding", "kind": "dingtalk", "enabled": True, "config": {"group_seconds": 10}, "secret": {"url": "https://192.0.2.81/robot/send?access_token=fake-token"}}).json()
    asset = operator.post("/api/assets", json={"name": "fake-outage-device", "form": "physical", "role": "server", "endpoints": [{"protocol": "ssh", "address": "192.0.2.82", "purpose": "monitoring"}]}).json()
    response = operator.post("/api/alerts/rules", json={"name": "fake-outage-rule", "kind": "availability", "asset_ids": [asset["id"]], "channel_ids": [mail["id"], ding["id"]], "consecutive_samples": 1})
    assert response.status_code == 201, response.text
    endpoint_id = uuid.UUID(asset["endpoints"][0]["id"])
    with factory() as db:
        db.add(CollectionStatus(endpoint_id=endpoint_id, asset_id=uuid.UUID(asset["id"]), protocol="ssh", state="failed", error_kind="unreachable", last_attempt_at=now, consecutive_failures=1))
        evaluate(db, now=now)
        plan_notifications(db, now=now)
        plan_notifications(db, now=now+timedelta(seconds=10))
        for task in db.scalars(select(Task).where(Task.kind == "notification.send")):
            task.available_at = now-timedelta(seconds=1)
        db.commit()
    calls = []
    worker = Worker(factory, {"notification.send": make_handler(lambda: app_factory.cipher, lambda *args: calls.append(args) or {"accepted": True})})
    try:
        assert worker.run_once() == "succeeded"
        assert worker.run_once() == "succeeded"
        with factory() as db:
            status = db.get(CollectionStatus, endpoint_id)
            status.state, status.error_kind = "ok", ""
            status.last_attempt_at = now+timedelta(seconds=60)
            evaluate(db, now=now+timedelta(seconds=60))
            plan_notifications(db, now=now+timedelta(seconds=60))
            plan_notifications(db, now=now+timedelta(seconds=70))
            for task in db.scalars(select(Task).where(Task.status == "queued")):
                task.available_at = now-timedelta(seconds=1)
            db.commit()
        assert worker.run_once() == "succeeded"
        assert worker.run_once() == "succeeded"
    finally:
        worker._pool.shutdown(wait=True)
    assert len(calls) == 4
    assert {call[0] for call in calls} == {"email", "dingtalk"}
    assert sum("[恢复 ·" in call[3].text for call in calls) == 2
    restarted = login(app_factory(), "fake_alert_operator")
    resolved = restarted.get("/api/alerts", params={"asset_id": asset["id"], "state": "resolved"}).json()["items"]
    assert len([item for item in resolved if item["rule_id"] == response.json()["id"]]) == 1
    deliveries = restarted.get("/api/alerts/deliveries").json()
    assert deliveries["total"] == 4 and all(item["status"] == "accepted" for item in deliveries["items"])
