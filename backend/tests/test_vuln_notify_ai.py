"""4.3 P18e：漏洞 AI 影响分析（缓存、脱敏、预览）、即时通知（汇总窗口、多来源、升级、维护窗口）、每日汇总、投递。

模型接口与通知发送都是伪造的，不访问外网。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import ai_runtime, inspection, vuln_ai, vulnmatch, vulnnotify
from app.ai.protocols import AiError
from app.crypto import CredentialCipher, KeyRing
from app.db import make_session_factory
from app.models import (AiProvider, Asset, AssetPackage, CollectionStatus, MaintenanceWindow, ManagementEndpoint, NotificationChannel, NotificationDelivery,
                        VulnAnalysis, VulnMatch, VulnRecord, VulnSubscription)
from app.notification_queue import make_handler
from app.services.credentials import create_credential
from app.vulndb.normalize import Affect, NormRecord, upsert
from app.vulndb.sync import settings_row
from app.worker import Worker
from tests.ai_fakes import FakeLLM
from tests.api_helpers import add_user, login

NOW = datetime(2026, 10, 19, 2, 0, tzinfo=UTC)  # 北京时间 10:00
ANSWER = {"conclusion": "ASSET_1 的 iDRAC 固件在受影响范围内，建议尽快升级", "impact": "ASSET_1 管理口暴露时可被远程利用", "exploit_conditions": ["能访问 iDRAC 网页"],
          "priority": "尽快处理", "actions": ["升级 iDRAC 固件到 3.21.21.21 或更高"], "evidence": ["NVD：3.21.21.21 之前的版本受影响"]}


def _setup(session, *, kev: bool = False, cvss: float = 9.8, first_seen: datetime = NOW) -> tuple[Asset, CredentialCipher]:  # type: ignore[no-untyped-def]
    cipher = CredentialCipher(KeyRing.generate())
    a = Asset(name="fake-idrac-01", form="physical", role="server", vendor="Dell", model="PowerEdge R740", lifecycle="active")
    ep = ManagementEndpoint(address="192.0.2.30", port=161, protocol="snmp")
    a.endpoints = [ep]
    session.add(a)
    session.flush()
    session.add(CollectionStatus(endpoint_id=ep.id, asset_id=a.id, protocol="snmp", state="ok", last_attempt_at=NOW, last_success_at=NOW,
                                 facts={"idrac": True, "idrac_name": "iDRAC9", "idrac_firmware": "3.00.00.00"}))
    rng = [{"end_excl": "3.21.21.21", "and_other": False}]
    upsert(session, [NormRecord(key="CVE-2026-50001", source="nvd", title="Fake iDRAC RCE", summary="Fake remote code execution in iDRAC web UI.",
                                cvss=cvss, severity="critical" if cvss >= 9 else "high", affects=[Affect("cpe", "dell:idrac9_firmware", rng)],
                                kev={"dateAdded": "2026-10-18"} if kev else None),
                     NormRecord(key="CVE-2026-50001", source="chaitin", native_id="CT-2026-50001", title="Fake iDRAC 远程代码执行漏洞", severity="critical")], first_seen)
    session.add(NotificationChannel(name="fake-dingtalk", kind="dingtalk", enabled=True, config={}))
    session.flush()
    vulnmatch.run(session, now=first_seen)
    return a, cipher


def _ai_on(session, cipher: CredentialCipher) -> None:  # type: ignore[no-untyped-def]
    cred = create_credential(session, cipher, alias="fake-ai-key", kind="token", secret="fake-key")
    session.flush()
    p = AiProvider(name="fake", protocol="openai", base_url="https://llm.example.invalid/v1", model="fake-model", credential_id=cred.id,
                   quirks=[], capabilities={"json_modes": ["json_schema"]}, check_result=[])
    session.add(p)
    session.flush()
    s = ai_runtime.settings_row(session)
    s.enabled, s.default_provider_id, s.features = True, p.id, {"cve_analysis": {"enabled": True, "provider_id": None}}
    session.flush()


def test_analysis_cache_redaction_and_preview(session) -> None:  # type: ignore[no-untyped-def]
    _, cipher = _setup(session)
    with pytest.raises(AiError) as exc:
        vuln_ai.analyze(session, cipher, "CVE-2026-50001", user_name="fake-admin", client=FakeLLM(answer=ANSWER).client())
    assert exc.value.kind == "disabled"
    _ai_on(session, cipher)
    fake = FakeLLM(answer=ANSWER)
    row = vuln_ai.analyze(session, cipher, "CVE-2026-50001", user_name="fake-admin", client=fake.client())
    assert row.result["conclusion"].startswith("fake-idrac-01 的 iDRAC 固件")  # 代号已换回原名
    sent = fake.requests[0].content.decode()
    assert "fake-idrac-01" not in sent and "ASSET_1" in sent and "3.00.00.00" in sent  # 名称脱敏，版本号保留
    assert "fake-key" not in sent and "192.0.2.30" not in sent
    vuln_ai.analyze(session, cipher, "CVE-2026-50001", user_name="fake-admin", client=fake.client())
    assert len(fake.requests) == 1  # 影响情况没变：用缓存
    vuln_ai.analyze(session, cipher, "CVE-2026-50001", user_name="fake-admin", force=True, client=fake.client())
    assert len(fake.requests) == 2 and session.scalar(select(VulnAnalysis.id).where(VulnAnalysis.record_id == "CVE-2026-50001")) is not None
    pv = vuln_ai.preview(session, "CVE-2026-50001")
    assert "fake-idrac-01" not in json.dumps(pv["payload"], ensure_ascii=False) and pv["mapping"]["ASSET_1"] == "fake-idrac-01"
    bad = FakeLLM(answer={"conclusion": "x"})  # 不符合结构：不保存、不显示
    with pytest.raises(AiError):
        vuln_ai.analyze(session, cipher, "CVE-2026-50001", user_name="u", force=True, client=bad.client())


def test_instant_waits_for_window_merges_sources_and_upgrades(session) -> None:  # type: ignore[no-untyped-def]
    a, cipher = _setup(session, cvss=9.8)
    assert vulnnotify.instant(session, now=NOW + timedelta(minutes=3), cipher=cipher) == 0  # 汇总窗口 5 分钟内先不发
    assert vulnnotify.instant(session, now=NOW + timedelta(minutes=6), cipher=cipher) == 1
    d = session.scalar(select(NotificationDelivery))
    assert d.kind == "vuln" and d.title.startswith("【漏洞预警】CVE-2026-50001 Fake iDRAC 远程代码执行漏洞")
    assert "发布来源：长亭漏洞库、NVD" in d.text and "影响本地 1 台：fake-idrac-01" in d.text and "修复版本：3.21.21.21" in d.text
    assert vulnnotify.instant(session, now=NOW + timedelta(minutes=40), cipher=cipher) == 0  # 同一资产同一漏洞只发一次
    session.get(VulnRecord, "CVE-2026-50001").kev = True
    assert vulnnotify.instant(session, now=NOW + timedelta(hours=2), cipher=cipher) == 1
    up = session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.created_at)).all()[-1]
    assert up.title.startswith("【漏洞升级】") and "已被列入 CISA KEV" in up.text


def test_instant_rules(session) -> None:  # type: ignore[no-untyped-def]
    a, cipher = _setup(session, cvss=8.0)
    assert vulnnotify.instant(session, now=NOW + timedelta(hours=1), cipher=cipher) == 0  # 8.0 分且未被利用：不即时通知
    session.get(VulnRecord, "CVE-2026-50001").kev = True
    session.add(MaintenanceWindow(name="fake-maint", starts_at=NOW, ends_at=NOW + timedelta(hours=3), asset_ids=[str(a.id)], tags=[]))
    session.flush()
    assert vulnnotify.instant(session, now=NOW + timedelta(hours=1), cipher=cipher) == 0  # 维护窗口内不发
    m = session.scalar(select(VulnMatch))
    m.status, m.reason = "not_affected", "fake"
    assert vulnnotify.instant(session, now=NOW + timedelta(hours=4), cipher=cipher) == 0  # 已处置为不受影响
    m.status = "open"
    settings_row(session).notify_enabled = False
    assert vulnnotify.instant(session, now=NOW + timedelta(hours=4), cipher=cipher) == 0


def test_instant_includes_ai_conclusion(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _, cipher = _setup(session, kev=True)
    _ai_on(session, cipher)
    fake = FakeLLM(answer=ANSWER)
    assert vulnnotify.instant(session, now=NOW + timedelta(hours=1), cipher=cipher, ai_client=fake.client()) == 1
    assert "AI 结论（仅供参考）：fake-idrac-01 的 iDRAC 固件在受影响范围内" in session.scalar(select(NotificationDelivery)).text
    # AI 失败时照常发送
    session.add(Asset(name="x", form="physical", role="server"))
    rec = session.get(VulnRecord, "CVE-2026-50001")
    m = session.scalar(select(VulnMatch))
    m.notified_at, m.notified_kev = None, False
    assert vulnnotify.instant(session, now=NOW + timedelta(hours=2), cipher=cipher, ai_client=FakeLLM(error=(500, {}, {})).client()) == 1
    assert rec.kev


def test_digest(session) -> None:  # type: ignore[no-untyped-def]
    _setup(session, cvss=7.5)
    session.add(VulnSubscription(kind="keyword", value="iDRAC", label="iDRAC", auto=False, enabled=True))
    old = NormRecord(key="CVE-2010-0001", source="kev", title="Old iDRAC thing", published=datetime(2010, 1, 1, tzinfo=UTC))
    upsert(session, [old], NOW)
    session.flush()
    early = NOW.replace(hour=0)  # 北京时间 8:00，早于默认 9 点
    assert vulnnotify.digest(session, now=early) == {"sent": False}
    r = vulnnotify.digest(session, now=NOW + timedelta(minutes=1))
    assert r["sent"] and r["new"] == 1 and r["hits"] == 1  # 2010 年的旧漏洞不算“新漏洞”
    d = session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.created_at)).all()[-1]
    assert "CVE-2026-50001" in d.text and "来源：长亭漏洞库、NVD" in d.text and "Old iDRAC" not in d.text
    assert vulnnotify.digest(session, now=NOW + timedelta(hours=2)) == {"sent": False}  # 当天已发
    assert vulnnotify.digest(session, now=NOW + timedelta(days=1, minutes=5))["sent"] is False  # 第二天没有新内容


def test_vuln_delivery_is_sent_as_rendered(engine) -> None:  # type: ignore[no-untyped-def]
    factory = make_session_factory(engine)
    with factory() as db:
        _, cipher = _setup(db, kev=True, first_seen=datetime.now(UTC) - timedelta(hours=1))
        ch = db.scalar(select(NotificationChannel))
        cred = create_credential(db, cipher, alias="fake-hook", kind="token", secret=json.dumps({"url": "https://hook.example.invalid/fake"}))
        db.flush()
        ch.credential_id = cred.id
        assert vulnnotify.instant(db, cipher=cipher) == 1
        db.commit()
    sent = []
    worker = Worker(factory, {"notification.send": make_handler(lambda: cipher, lambda kind, cfg, secrets, msg: (sent.append(msg), {"accepted": True})[1])},
                    owner="fake-worker")
    try:
        assert worker.run_once() == "succeeded"
    finally:
        worker._pool.shutdown(wait=True, cancel_futures=True)
    assert sent and sent[0].title.startswith("【漏洞预警】") and "影响本地 1 台" in sent[0].text


def test_inspection_and_dashboard(session, client, factory) -> None:  # type: ignore[no-untyped-def]
    _setup(session, kev=True)
    items, _ = inspection.inspect(session, now=NOW)
    v = [i for i in items if i["category"] == "漏洞"]
    assert v and v[0]["severity"] == "critical" and "已被利用 1 个" in v[0]["title"] and v[0]["detail"] == "CVE-2026-50001"
    session.commit()
    add_user(factory, "fake-viewer", "viewer")
    c = login(client, "fake-viewer")
    s = c.get("/api/dashboard/summary").json()["vulns"]
    assert s["open"] == 1 and s["kev"] == 1 and s["top"][0]["id"] == "CVE-2026-50001"


def test_api(client, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        _setup(db, kev=True)
        db.commit()
    add_user(factory, "fake-admin", "admin")
    c = login(client, "fake-admin")
    st = c.get("/api/vuln/settings").json()
    assert st["notify_enabled"] is True and st["digest_hour"] == 9 and st["notify_channel_ids"] == []
    r = c.put("/api/vuln/settings", json={"online_enabled": False, "aggregation_minutes": 10, "digest_hour": 18, "revision": st["revision"]}).json()
    assert r["digest_hour"] == 18 and r["aggregation_minutes"] == 10
    assert c.get("/api/vuln/records/CVE-2026-50001/analysis").json() == {"enabled": False, "analysis": None}
    bad = c.post("/api/vuln/records/CVE-2026-50001/analysis")
    assert bad.status_code == 422 and bad.json()["detail"]["kind"] == "disabled"
    pv = c.get("/api/vuln/records/CVE-2026-50001/analysis/preview").json()
    assert pv["privacy"] == "redact" and "ASSET_1" in json.dumps(pv["payload"])
    assert c.post("/api/vuln/digest-now").json()["sent"] is True
