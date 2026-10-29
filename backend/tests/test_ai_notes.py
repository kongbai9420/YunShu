"""4.3 P18f：AI 告警解读与巡检摘要。伪造模型接口，不访问外网。"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import ai_notes, ai_runtime, inspection, metrics
from app.ai.protocols import AiError
from app.crypto import CredentialCipher, KeyRing
from app.metrics import Point
from app.models import AiNote, AiProvider, Alert, AlertRule, Asset, AssetRelation, InspectionReport, Task
from app.services.credentials import create_credential
from tests.ai_fakes import FakeLLM
from tests.api_helpers import add_user, login

NOW = datetime.now(UTC).replace(microsecond=0)
EXPLAIN = {"summary": "ASSET_1 的 CPU 温度持续升高", "likely_causes": [{"cause": "风扇转速偏低", "confidence": "中", "evidence": "同一时间 fan.rpm 下降"}],
           "checks": ["检查风扇与进风口"], "urgency": "尽快处理", "note": ""}
SUMMARY = {"overall": "需关注", "headline": "ASSET_1 温度偏高，其余正常", "top_issues": [{"title": "CPU 高温", "why": "超过 80 ℃", "action": "检查散热"}],
           "changes": ""}


def _ai(session, cipher: CredentialCipher) -> None:  # type: ignore[no-untyped-def]
    cred = create_credential(session, cipher, alias="fake-ai-key", kind="token", secret="fake-key")
    session.flush()
    p = AiProvider(name="fake", protocol="openai", base_url="https://llm.example.invalid/v1", model="fake-model", credential_id=cred.id,
                   quirks=[], capabilities={}, check_result=[])
    session.add(p)
    session.flush()
    s = ai_runtime.settings_row(session)
    s.enabled, s.default_provider_id = True, p.id
    s.features = {"alert_explain": {"enabled": True, "provider_id": None}, "inspection_summary": {"enabled": True, "provider_id": None}}
    session.flush()


def _alert(session) -> Alert:  # type: ignore[no-untyped-def]
    host = Asset(name="fake-pve-01", form="physical", role="server", lifecycle="active")
    vm = Asset(name="fake-vm-01", form="vm", role="server", lifecycle="active")
    session.add_all([host, vm])
    session.flush()
    session.add(AssetRelation(source_asset_id=host.id, target_asset_id=vm.id, kind="hosts"))
    for i in range(6):
        metrics.write_sample(session, host.id, "fake", [Point("cpu.temp", 70 + i * 3, "°C"), Point("fan.rpm:FAN1", 5000 - i * 600, "RPM")],
                             observed_at=NOW - timedelta(minutes=30 - i * 5))
    rule = AlertRule(name="CPU 高温", metric_key="cpu.temp", threshold=80, recovery_threshold=75, severity="critical")
    session.add(rule)
    session.flush()
    a = Alert(rule_id=rule.id, asset_id=host.id, asset_name=host.name, metric_key="cpu.temp", state="active", severity="critical",
              title="fake-pve-01 · CPU 高温（192.0.2.10）", value=85, unit="°C", threshold=80, episode=1, sample_count=1, first_seen_at=NOW,
              last_seen_at=NOW, last_sample_at=NOW, acknowledged_by="", suppression_reason="")
    session.add(a)
    session.flush()
    return a


def test_explain_alert_context_redaction_and_cache(session) -> None:  # type: ignore[no-untyped-def]
    cipher = CredentialCipher(KeyRing.generate())
    a = _alert(session)
    with pytest.raises(AiError) as exc:
        ai_notes.explain_alert(session, cipher, a.id, user_name="u", client=FakeLLM(answer=EXPLAIN).client())
    assert exc.value.kind == "disabled"
    _ai(session, cipher)
    fake = FakeLLM(answer=EXPLAIN)
    row = ai_notes.explain_alert(session, cipher, a.id, user_name="fake-admin", client=fake.client(), now=NOW)
    assert row.result["summary"] == "fake-pve-01 的 CPU 温度持续升高" and row.result["urgency"] == "尽快处理"
    body = json.loads(fake.requests[0].content)["messages"][-1]["content"]
    assert "fake-pve-01" not in body and "fake-vm-01" not in body and "192.0.2.10" not in body
    assert '"relation": "hosts"' in body and "fan.rpm:FAN1" in body and "trend_24h" in body
    ai_notes.explain_alert(session, cipher, a.id, user_name="fake-admin", client=fake.client(), now=NOW)
    assert len(fake.requests) == 1  # 告警没变化：复用
    a.value = 88
    ai_notes.explain_alert(session, cipher, a.id, user_name="fake-admin", client=fake.client(), now=NOW)
    assert len(fake.requests) == 2


def test_inspection_summary_and_daily_task(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cipher = CredentialCipher(KeyRing.generate())
    _alert(session)
    report = inspection.generate(session, kind="manual", by="fake")
    _ai(session, cipher)
    fake = FakeLLM(answer=SUMMARY)
    row = ai_notes.summarize_inspection(session, cipher, report.id, user_name="fake", client=fake.client())
    assert row.result["overall"] == "需关注" and "fake-pve-01" in row.result["headline"]
    assert "fake-pve-01" not in fake.requests[0].content.decode()
    # 每日巡检生成后，开启摘要时入队一个 Worker 任务
    from app.settings import Settings
    from app.tasks import build_schedules

    monkeypatch.setattr(inspection, "daily_due", lambda db, hour, now=None: True)
    sched = {s.name: s for s in build_schedules(Settings.from_env())}["inspection.daily"]
    sched.fn(session)
    tasks = list(session.scalars(select(Task).where(Task.kind == ai_notes.SUMMARY_TASK)))
    assert len(tasks) == 1 and tasks[0].payload["report_id"]
    ai_runtime.settings_row(session).features = {}
    sched.fn(session)
    assert len(list(session.scalars(select(Task).where(Task.kind == ai_notes.SUMMARY_TASK)))) == 1  # 关闭摘要后不再入队


def test_api(client, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        a = _alert(db)
        report = inspection.generate(db, kind="manual", by="fake")
        db.commit()
        alert_id, report_id = a.id, report.id
    add_user(factory, "fake-admin", "admin")
    c = login(client, "fake-admin")
    assert c.get(f"/api/alerts/{alert_id}/explain").json() == {"enabled": False, "note": None}
    r = c.post(f"/api/alerts/{alert_id}/explain")
    assert r.status_code == 422 and r.json()["detail"]["kind"] == "disabled"
    with factory() as db:
        _ai(db, client.app.state.cipher)
        db.commit()
    client.app.state.ai_http_client = FakeLLM(answer=EXPLAIN).client()
    note = c.post(f"/api/alerts/{alert_id}/explain").json()["note"]
    assert note["result"]["summary"].startswith("fake-pve-01") and note["model"] == "fake-model"
    assert c.get(f"/api/alerts/{alert_id}/explain").json()["note"]["result"] == note["result"]
    client.app.state.ai_http_client = FakeLLM(answer=SUMMARY).client()
    s = c.post(f"/api/inspections/{report_id}/summary").json()
    assert s["note"]["result"]["overall"] == "需关注"
    assert c.get(f"/api/inspections/{report_id}/summary").json()["enabled"] is True
    with factory() as db:
        assert db.scalar(select(AiNote.kind).where(AiNote.ref == str(report_id))) == "inspection"
        assert db.get(InspectionReport, report_id) is not None
