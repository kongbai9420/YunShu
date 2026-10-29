"""4.3 P18 补充：修复与缓解。各来源的修复建议原文、参考链接分类、KEV 处置期限；多来源合并；详情、通知、AI 输入。

全部使用本地样例数据，不访问外网。
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from app import vuln_ai, vulnnotify
from app.models import NotificationDelivery, VulnRecord
from app.vulndb import cn, remediation, sources
from app.vulndb.normalize import NormRecord, clean_text, fix_entry, upsert
from tests.api_helpers import add_user, login
from tests.test_vuln_notify_ai import NOW, _setup

FIX = Path(__file__).parent / "fixtures" / "vuln"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_nvd_reference_tags_and_cisa_action() -> None:
    cve = {
        "id": "CVE-2026-50002", "descriptions": [{"lang": "en", "value": "Fake flaw."}], "metrics": {}, "configurations": [],
        "cisaActionDue": "2026-11-01", "cisaRequiredAction": "Apply mitigations per vendor instructions or discontinue use of the product.",
        "references": [
            {"url": "https://example.invalid/blog", "tags": ["Third Party Advisory"]},
            {"url": "https://vendor.example.invalid/advisory", "tags": ["Vendor Advisory"]},
            {"url": "https://vendor.example.invalid/advisory", "tags": ["Patch"]},
            {"url": "https://example.invalid/poc", "tags": ["Exploit"]},
            {"url": "https://example.invalid/plain"},
        ],
    }
    rec = sources.parse_nvd_cve(cve, None)
    assert rec.references[0] == {"url": "https://vendor.example.invalid/advisory", "source": "nvd", "tags": ["patch", "vendor"]}  # 同链接合并、补丁排前
    assert {"url": "https://example.invalid/plain", "source": "nvd"} in rec.references
    assert rec.kev_due == "2026-11-01"
    assert rec.remediation == [{"source": "nvd", "kind": "action", "text": cve["cisaRequiredAction"]}]
    # NVD 真实样例：Vendor Advisory 标签保留
    real = sources.parse_nvd_cve(_load("nvd_cve.json"), None)
    assert any("vendor" in (r.get("tags") or []) for r in real.references)


def test_kev_action_due_and_vendor_link() -> None:
    rec = sources.parse_kev(_load("kev.json"))[0]
    assert rec.kev_due == "2026-10-19"
    assert rec.remediation and rec.remediation[0]["kind"] == "action" and rec.remediation[0]["text"].startswith("Apply mitigations")
    vendor = [r for r in rec.references if r.get("tags") == ["vendor"]]
    assert vendor == [{"url": "https://www.beyondtrust.com/trust-center/security-advisories/bt26-05", "source": "kev", "tags": ["vendor"]}]
    assert all("tags" not in r for r in rec.references if "cisa.gov" in r["url"] or "nvd.nist.gov" in r["url"])


def test_osv_reference_types() -> None:
    rec = sources.parse_osv(_load("osv_debian.json"))
    assert rec is not None and rec.references[0]["tags"] == ["advisory"]


def test_oscs_ghsa_and_webhook_fix_text() -> None:
    fixes = cn._oscs_fixes({
        "effect": [{"name": "fake-lib", "min_fixed_version": "2.4.1"}, {"name": "fake-lib", "min_fixed_version": "2.4.1"}],
        "soulution_data": ["<p>升级到最新版本。</p><p>临时关闭 &lt;admin&gt; 接口</p>"], "vuln_suggest": "",
    })
    entries = [fix_entry("oscs", k, t) for k, t in fixes]
    assert entries[0]["text"] == "fake-lib 升级到 2.4.1 或更高版本"
    assert entries[1]["text"] == "升级到最新版本。\n临时关闭 <admin> 接口"
    wv = cn.parse_webhook({"type": "watchvuln-vulninfo", "content": {
        "unique_key": "AVD-2026-1", "title": "Fake 漏洞", "cve": "CVE-2026-50003", "from": "https://avd.aliyun.com/detail?id=AVD-2026-1",
        "solutions": "1. 升级到 1.2.3\n2. 临时在防火墙上限制 8443 端口", "references": []}})
    assert wv is not None and wv.source == "avd" and wv.remediation == [{"source": "avd", "kind": "solution", "text": "1. 升级到 1.2.3\n2. 临时在防火墙上限制 8443 端口"}]
    generic = cn.parse_webhook({"id": "X-1", "title": "Fake", "workaround": "关闭远程管理", "solution": ""})
    assert generic is not None and generic.remediation == [{"source": "webhook", "kind": "workaround", "text": "关闭远程管理"}]
    # 不提供修复字段的来源（如长亭）不清空已有建议
    assert cn._record("chaitin", "CT-1", "t").remediation is None
    assert clean_text("a<br/>b &amp; c\n\n  d  ") == "a\nb & c\nd"


def test_merge_keeps_sources_and_build_dedupes(session) -> None:  # type: ignore[no-untyped-def]
    key = "CVE-2026-50004"
    upsert(session, [
        NormRecord(key=key, source="kev", kev={"dateAdded": "2026-10-18", "dueDate": "2026-11-08"},
                   remediation=[fix_entry("kev", "action", "Apply updates per vendor instructions.")],
                   references=[{"url": "https://vendor.example.invalid/a", "source": "kev", "tags": ["vendor"]}]),
        NormRecord(key=key, source="nvd", remediation=[fix_entry("nvd", "action", "Apply updates per vendor instructions")],
                   references=[{"url": "https://vendor.example.invalid/a", "source": "nvd", "tags": ["patch"]}]),
        NormRecord(key=key, source="oscs", remediation=[fix_entry("oscs", "workaround", "临时禁用 WebUI")]),
        NormRecord(key=key, source="chaitin", title="Fake"),  # 不带建议：不影响已有
    ], NOW)
    rec = session.get(VulnRecord, key)
    assert rec.kev_due == "2026-11-08" and len(rec.remediation) == 3
    assert rec.references[0]["tags"] == ["patch", "vendor"]
    upsert(session, [NormRecord(key=key, source="oscs", remediation=[fix_entry("oscs", "solution", "升级到 9.9")])], NOW)
    assert [e["text"] for e in rec.remediation if e["source"] == "oscs"] == ["升级到 9.9"]  # 同一来源替换，其他来源保留
    built = remediation.build(rec, [
        {"active": True, "status": "open", "label": "Fake WebUI", "fixed_version": "9.9", "asset_name": "fake-a", "note": ""},
        {"active": True, "status": "open", "label": "Fake WebUI", "fixed_version": "9.9", "asset_name": "fake-b", "note": ""},
        {"active": True, "status": "false_positive", "label": "Fake WebUI", "fixed_version": "9.9", "asset_name": "fake-c", "note": ""},
        {"active": True, "status": "open", "label": "Fake pkg", "fixed_version": "", "asset_name": "fake-d", "note": "发行版尚未发布修复版本"},
    ])
    assert built["fixed"] == [{"product": "Fake WebUI", "fixed_version": "9.9", "assets": ["fake-a", "fake-b"], "note": ""},
                              {"product": "Fake pkg", "fixed_version": "", "assets": ["fake-d"], "note": "发行版尚未发布修复版本"}]
    action = [t for t in built["texts"] if t["kind"] == "action"]
    assert len(action) == 1 and action[0]["sources"] == ["CISA KEV", "NVD"]  # 只差标点的同一段话合并
    assert built["texts"][0]["kind"] == "solution" and built["kev_due"] == "2026-11-08"
    assert built["links"][0]["tags_text"] == ["补丁", "厂商公告"]
    assert remediation.headline(rec) == "修复建议（OSCS）：升级到 9.9"


def test_instant_notice_and_ai_context_carry_advice(session) -> None:  # type: ignore[no-untyped-def]
    _a, cipher = _setup(session, cvss=9.8, kev=True)
    upsert(session, [NormRecord(key="CVE-2026-50001", source="kev", kev={"dateAdded": "2026-10-18", "dueDate": "2026-11-08"},
                                remediation=[fix_entry("kev", "action", "Apply mitigations per vendor instructions.")]),
                     NormRecord(key="CVE-2026-50001", source="chaitin", remediation=[fix_entry("chaitin", "workaround", "临时限制 iDRAC 网页只允许管理网段访问")])], NOW)
    assert vulnnotify.instant(session, now=NOW + timedelta(minutes=31), cipher=cipher) == 1
    text = session.scalar(select(NotificationDelivery)).text
    assert "修复版本：3.21.21.21" in text and "临时缓解（长亭漏洞库）：临时限制 iDRAC 网页只允许管理网段访问" in text
    assert "CISA 要求处置期限：2026-11-08" in text
    rec = session.get(VulnRecord, "CVE-2026-50001")
    ctx = vuln_ai._context(session, rec, _Plain())
    assert {e["source"] for e in ctx["remediation"]} == {"CISA KEV", "长亭漏洞库"} and ctx["kev_due"] == "2026-11-08"


class _Plain:
    def register(self, value: str, kind: str) -> str:
        return value


def test_record_api_has_remediation(client, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        _setup(db, kev=True)
        upsert(db, [NormRecord(key="CVE-2026-50001", source="kev", kev={"dateAdded": "2026-10-18", "dueDate": "2026-11-08"},
                               remediation=[fix_entry("kev", "action", "Apply mitigations per vendor instructions.")])], NOW)
        db.commit()
    add_user(factory, "fake-viewer", "viewer")
    d = login(client, "fake-viewer").get("/api/vuln/records/CVE-2026-50001").json()
    assert d["kev_due"] == "2026-11-08"
    r = d["remediation"]
    assert r["fixed"][0]["fixed_version"] == "3.21.21.21" and r["fixed"][0]["assets"] == ["fake-idrac-01"]
    assert r["texts"][0]["sources"] == ["CISA KEV"] and r["kev_due"] == "2026-11-08"
