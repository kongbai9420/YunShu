"""4.3 P18 补充：尽快推送。CVE 官方记录来源（解析、分支区间、增量与补齐）、KEV 条件请求、订阅命中预警与“漏洞确认”、推送后立即比对。

全部使用伪造接口与样例数据，不访问外网。
"""
from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path

import httpx
from sqlalchemy import select

from app import vulnmatch, vulnnotify
from app.models import NotificationDelivery, Task, VulnRecord
from app.vulndb import cve5, sources, subscriptions, sync
from app.vulndb.normalize import Affect, NormRecord, upsert
from tests.api_helpers import add_user, login
from tests.test_vuln_notify_ai import NOW, _setup

FIX = Path(__file__).parent / "fixtures" / "vuln"
IDRAC = json.loads((FIX / "cve5_idrac.json").read_text(encoding="utf-8"))
TRACKED = {"dell:idrac9_firmware"}


# ---------------- CVE 官方记录：解析 ----------------


def test_map_product_names() -> None:
    assert "dell:idrac9_firmware" in cve5.map_product("Dell", "Integrated Dell Remote Access Controller 9")
    assert "dell:idrac8_firmware" in cve5.map_product("Dell", "iDRAC8")
    assert "vmware:esxi" in cve5.map_product("VMware", "VMware ESXi")
    assert "synology:diskstation_manager" in cve5.map_product("Synology", "DiskStation Manager (DSM)")
    assert "mikrotik:routeros" in cve5.map_product("MikroTik", "RouterOS")
    assert "proxmox:virtual_environment" in cve5.map_product("Proxmox", "Proxmox Virtual Environment")
    assert cve5.map_product("Fortinet", "FortiOS") == {"fortinet:fortios"}  # 手动订阅的“厂商:产品”也能对上


def test_parse_record_with_branches_fixes_and_kev() -> None:
    rec = cve5.parse(IDRAC, products=TRACKED, keywords=[])
    assert rec is not None and rec.key == "CVE-2026-50020" and rec.source == "cve"
    assert rec.cvss == 9.8 and rec.severity == "critical" and rec.kev == {"dateAdded": "2026-10-20"}
    assert [a.product for a in rec.affects] == ["dell:idrac9_firmware"]  # 无关产品不保留
    ranges = rec.affects[0].ranges
    # 多个修复分支：7.00 分支修复于 7.00.00.172，7.10 分支修复于 7.10.50.00
    assert vulnmatch.cpe_hit("dell:idrac9_firmware", "7.00.00.200", ranges) is None
    assert vulnmatch.cpe_hit("dell:idrac9_firmware", "7.00.00.100", ranges).fixed == "7.00.00.172"
    assert vulnmatch.cpe_hit("dell:idrac9_firmware", "7.10.30.00", ranges).fixed == "7.10.50.00"
    assert vulnmatch.cpe_hit("dell:idrac9_firmware", "6.10.00.00", ranges).confidence == "affected"
    assert vulnmatch.cpe_hit("dell:idrac9_firmware", "7.10.50.00", ranges) is None
    assert [e["kind"] for e in rec.remediation] == ["solution", "workaround"]
    assert rec.references[0] == {"url": "https://vendor.example.invalid/dsa-2026-0001", "source": "cve", "tags": ["vendor"]}


def test_parse_filters_by_subscription() -> None:
    other = copy.deepcopy(IDRAC)
    other["containers"]["adp"] = []
    assert cve5.parse(other, products={"fortinet:fortios"}, keywords=[]) is None  # 与订阅无关
    assert cve5.parse(other, products=set(), keywords=["web interface"]) is not None  # 关键词命中
    assert cve5.parse(other, products={"dell:idrac8_firmware"}, keywords=[]) is not None  # 描述提到 iDRAC（只是提到，不生成版本区间）
    assert cve5.parse(other, products={"dell:idrac8_firmware"}, keywords=[]).affects == []
    rejected = copy.deepcopy(IDRAC)
    rejected["cveMetadata"]["state"] = "REJECTED"
    assert cve5.parse(rejected, products=TRACKED, keywords=[]) is None
    vm = copy.deepcopy(other)
    vm["containers"]["cna"]["affected"] = [{"vendor": "VMware", "product": "ESXi", "versions": [{"version": "8.0", "lessThan": "8.0 U3e", "status": "affected"}]}]
    vrec = cve5.parse(vm, products={"vmware:esxi"}, keywords=[])
    assert vrec.affects[0].ranges[0]["and_other"] is True  # VMware 按构建号修复：只标可能受影响


# ---------------- CVE 官方记录：增量与补齐 ----------------


class FakeCve:
    def __init__(self) -> None:
        self.delta = {"fetchTime": "2026-10-20T04:20:00.000Z", "new": [{"cveId": "CVE-2026-50020"}], "updated": []}
        self.log = [self.delta, {"fetchTime": "2026-10-20T04:13:00.000Z", "new": [], "updated": [{"cveId": "CVE-2026-50021"}]},
                    {"fetchTime": "2026-10-20T04:06:00.000Z", "new": [{"cveId": "CVE-2026-50022"}], "updated": []}]
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if url == cve5.DELTA_URL:
            return httpx.Response(200, json=self.delta)
        if url == cve5.DELTA_LOG_URL:
            return httpx.Response(200, json=self.log)
        cid = url.rsplit("/", 1)[1].removesuffix(".json")
        rec = copy.deepcopy(IDRAC)
        rec["cveMetadata"]["cveId"] = cid
        return httpx.Response(200, json=rec)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))


def test_fetch_delta_then_gap_reads_log() -> None:
    fake = FakeCve()
    http = fake.client()
    recs, cur = cve5.fetch(http, cursor={}, now=NOW, products=TRACKED, keywords=[])  # 首次：只读最近一轮，不回补历史
    assert [r.key for r in recs] == ["CVE-2026-50020"] and cur["fetchTime"] == "2026-10-20T04:20:00.000Z"
    assert fake.calls[-1].endswith("/cves/2026/50xxx/CVE-2026-50020.json")
    recs, cur = cve5.fetch(http, cursor=cur, now=NOW + timedelta(minutes=5), products=TRACKED, keywords=[])
    assert recs == [] and cve5.DELTA_LOG_URL not in fake.calls  # 没有新一轮
    # 停了 1 小时：读 deltaLog，只处理游标之后的各轮
    fake.log.insert(0, {"fetchTime": "2026-10-20T04:34:00.000Z", "new": [{"cveId": "CVE-2026-50023"}], "updated": []})
    fake.log.insert(0, {"fetchTime": "2026-10-20T04:27:00.000Z", "new": [], "updated": [{"cveId": "CVE-2026-50021"}]})
    fake.log.sort(key=lambda d: d["fetchTime"], reverse=True)
    recs, cur = cve5.fetch(http, cursor=cur, now=NOW + timedelta(hours=1), products=TRACKED, keywords=[])
    assert cve5.DELTA_LOG_URL in fake.calls
    assert sorted(r.key for r in recs) == ["CVE-2026-50021", "CVE-2026-50023"] and cur["fetchTime"] == "2026-10-20T04:34:00.000Z"


def test_fetch_caps_and_resumes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeCve()
    monkeypatch.setattr(cve5, "MAX_RECORDS", 1)
    cur = {"fetchTime": "2026-10-20T04:00:00.000Z", "polled_at": (NOW - timedelta(hours=1)).isoformat()}
    recs, cur = cve5.fetch(fake.client(), cursor=cur, now=NOW, products=TRACKED, keywords=[])
    assert [r.key for r in recs] == ["CVE-2026-50022"] and cur["fetchTime"] == "2026-10-20T04:06:00.000Z"  # 超出上限的轮次下一轮继续


def test_kev_conditional_request() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("If-None-Match", ""))
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json={"catalogVersion": "2026.10.20", "count": 0, "vulnerabilities": []}, headers={"ETag": '"v1"'})

    http = httpx.Client(transport=httpx.MockTransport(handle))
    _, cur = sources.fetch_kev(http, cursor={})
    assert cur["etag"] == '"v1"'
    recs, cur2 = sources.fetch_kev(http, cursor=cur)
    assert recs == [] and cur2 == cur and seen == ["", '"v1"']


# ---------------- 订阅命中预警 ----------------


def test_relevance_rules(session) -> None:  # type: ignore[no-untyped-def]
    from app.models import VulnSubscription

    subs = [VulnSubscription(kind="product", value="synology:diskstation_manager", label="群晖 DSM", min_severity="unknown", kev_only=False),
            VulnSubscription(kind="keyword", value="OpenSSH", label="OpenSSH", min_severity="critical", kev_only=False),
            VulnSubscription(kind="product", value="fortinet:fortios", label="FortiOS", min_severity="unknown", kev_only=True)]
    r = subscriptions.relevance(subs, title="Synology DSM 远程代码执行", summary="", products=set(), severity="critical", kev=False)
    assert r.labels == ["群晖 DSM"] and r.by_version_data is False
    assert subscriptions.relevance(subs, title="Fake DSMS issue", summary="", products=set(), severity="critical", kev=False).labels == []  # 整词匹配
    assert subscriptions.relevance(subs, title="OpenSSH 漏洞", summary="", products=set(), severity="high", kev=False).labels == []  # 低于订阅的最低级别
    assert subscriptions.relevance(subs, title="x", summary="", products={"fortinet:fortios"}, severity="critical", kev=False).labels == []  # 只看已被利用
    r = subscriptions.relevance(subs, title="x", summary="", products={"fortinet:fortios"}, severity="critical", kev=True)
    assert r.labels == ["FortiOS"] and r.by_version_data is True


def _deliveries(session) -> list[NotificationDelivery]:  # type: ignore[no-untyped-def]
    return list(session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.created_at, NotificationDelivery.title)))


def test_early_warning_then_confirmation(session) -> None:  # type: ignore[no-untyped-def]
    _a, cipher = _setup(session)
    subscriptions.sync_auto(session)  # 按软件清单自动订阅 iDRAC9
    upsert(session, [NormRecord(key="CVE-2026-50030", source="chaitin", native_id="CT-2026-50030", title="Dell iDRAC9 远程代码执行漏洞（测试数据）",
                                severity="critical"),
                     NormRecord(key="CVE-2026-50031", source="chaitin", native_id="CT-2026-50031", title="Fake 某 OA 系统漏洞（测试数据）", severity="critical"),
                     NormRecord(key="CVE-2026-50032", source="chaitin", native_id="CT-2026-50032", title="Dell iDRAC9 信息泄露（测试数据）", severity="medium")],
           NOW + timedelta(minutes=1))
    assert vulnnotify.instant(session, now=NOW + timedelta(minutes=3), cipher=cipher) == 0  # 汇总窗口内
    assert vulnnotify.instant(session, now=NOW + timedelta(minutes=7), cipher=cipher) == 2  # 本地命中 1 条 + 预警 1 条
    early = [d for d in _deliveries(session) if "待确认" in d.title]
    assert len(early) == 1 and early[0].title.startswith("【漏洞预警·待确认】CVE-2026-50030")
    assert "命中订阅：" in early[0].text and "（标题或描述提到）" in early[0].text and "本地影响：待确认" in early[0].text
    assert vulnnotify.instant(session, now=NOW + timedelta(minutes=9), cipher=cipher) == 0  # 每个漏洞只预警一次
    # 版本数据到了：比对命中本地后发“漏洞确认”
    upsert(session, [NormRecord(key="CVE-2026-50030", source="cve", cvss=9.8, severity="critical",
                                affects=[Affect("cpe", "dell:idrac9_firmware", [{"end_excl": "3.21.21.21", "and_other": False}])])], NOW + timedelta(minutes=20))
    vulnmatch.run(session, now=NOW + timedelta(minutes=20))
    assert vulnnotify.instant(session, now=NOW + timedelta(minutes=21), cipher=cipher) == 1
    assert _deliveries(session)[-1].title.startswith("【漏洞确认】CVE-2026-50030")


def test_early_warning_switch_and_age(session) -> None:  # type: ignore[no-untyped-def]
    _a, cipher = _setup(session)
    subscriptions.sync_auto(session)
    sync.settings_row(session).early_warning = False
    upsert(session, [NormRecord(key="CVE-2026-50040", source="chaitin", title="Dell iDRAC9 漏洞（测试数据）", severity="critical")], NOW)
    assert vulnnotify.instant(session, now=NOW + timedelta(minutes=7), cipher=cipher) == 1  # 只有本地命中那条
    sync.settings_row(session).early_warning = True
    session.get(VulnRecord, "CVE-2026-50040").first_seen_at = NOW - timedelta(days=3)  # 太旧的不补发
    assert vulnnotify.instant(session, now=NOW + timedelta(minutes=8), cipher=cipher) == 0


# ---------------- 立即触发 ----------------


def test_webhook_queues_match_and_notify(client, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "fake-admin", "admin")
    c = login(client, "fake-admin")
    tok = c.post("/api/vuln/webhook-token").json()
    anon = client.__class__(client.app)
    assert anon.post(tok["path"], json={"id": "FAKE-9", "title": "Fake 推送", "severity": "critical"}).json()["accepted"] is True
    with factory() as db:
        task = db.scalar(select(Task).where(Task.idempotency_key.like("vulnmatch:webhook:%")))
        assert task is not None and task.payload == {"notify": True}
    st = c.get("/api/vuln/settings").json()
    assert st["early_warning"] is True and st["aggregation_minutes"] == 5
    assert c.put("/api/vuln/settings", json={"online_enabled": False, "aggregation_minutes": 5, "early_warning": False, "revision": st["revision"]}).json()["early_warning"] is False


def test_sync_with_new_data_queues_notify(engine) -> None:  # type: ignore[no-untyped-def]
    from app.crypto import CredentialCipher, KeyRing
    from app.db import make_session_factory

    factory = make_session_factory(engine)
    with factory() as db:
        sync.settings_row(db).online_enabled = True
        db.commit()
    fake = FakeCve()

    class Ctx:
        session_factory = factory

        class lease:  # noqa: N801
            payload = {"source": "cve"}

    with factory() as db:
        _setup(db)
        db.commit()
    handler = sync.make_handler(lambda: CredentialCipher(KeyRing.generate()), http_factory=fake.client)
    assert handler(Ctx())["state"] == "ok"
    with factory() as db:
        assert db.get(VulnRecord, "CVE-2026-50020") is not None
        assert db.scalar(select(Task).where(Task.idempotency_key.like("vulnnotify:e:%"))) is not None
