"""4.3 P18c：国内漏洞源、GitHub 安全公告、厂商 RSS、外部推送（watchvuln / 通用 Webhook）、同一漏洞多来源归并。

国内接口的响应结构按 watchvuln（MIT）grab/ 目录里的结构体构造，数据为伪造，不访问外网。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from app.models import VulnRecord, VulnSighting, VulnSource
from app.vulndb import cn, sync
from app.vulndb.normalize import sources_of
from tests.api_helpers import add_user, login

NOW = datetime(2026, 10, 19, 2, 0, tzinfo=UTC)
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Fake Vendor PSIRT</title>
<item><title>DSA-2026-001: Security Update for Fake iDRAC (CVE-2026-31001)</title><link>https://psirt.example.invalid/dsa-2026-001</link>
<guid>DSA-2026-001</guid><pubDate>Mon, 12 Oct 2026 08:00:00 GMT</pubDate><description>&lt;p&gt;Critical remote code execution.&lt;/p&gt;</description></item>
<item><title>Multiple vulnerabilities in Fake BIOS</title><link>https://psirt.example.invalid/dsa-2026-002</link><guid>DSA-2026-002</guid>
<description>CVE-2026-31002 and CVE-2026-31003</description></item></channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Fake</title>
<entry><title>Fake RouterOS fix for CVE-2026-32001</title><link href="https://fake.example.invalid/a"/><id>tag:fake,1</id><updated>2026-10-10T00:00:00Z</updated>
<summary>high severity</summary></entry></feed>"""


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "stack.chaitin.com" in url:
        assert request.headers["referer"] == "https://stack.chaitin.com/vuldb/index"
        return httpx.Response(200, json={"code": 0, "data": {"count": 1, "list": [
            {"id": "fake-uuid-1", "title": "Fake 网关远程命令执行漏洞", "summary": "伪造摘要", "severity": "critical", "ct_id": "CT-2026-0001",
             "cve_id": "CVE-2026-31001", "references": "https://ref.example.invalid/1\n", "disclosure_date": "2026-10-12", "created_at": "2026-10-12T08:00:00+08:00"}]}})
    if url.endswith("/intelligence/list"):
        return httpx.Response(200, json={"success": True, "code": 200, "data": {"total": 1, "data": [{"mps": "MPS-2026-0001", "title": "Fake 组件反序列化", "level": "High"}]}})
    if url.endswith("/vdb/info"):
        assert json.loads(request.content) == {"vuln_no": "MPS-2026-0001"}
        return httpx.Response(200, json={"success": True, "code": 200, "data": [{"vuln_no": "MPS-2026-0001", "vuln_title": "Fake 组件反序列化漏洞", "cve_id": "",
                                                                               "level": "High", "description": "伪造", "publish_time": 1760227200000, "references": []}]})
    if "ti.qianxin.com" in url:
        item = {"id": 99, "vuln_name": "Fake 系统权限绕过", "qvd_code": "QVD-2026-0001", "cve_code": "CVE-2026-31001", "rating_level": "极危",
                "description": "伪造", "publish_time": "2026-10-12"}
        return httpx.Response(200, json={"status": 10000, "data": {"key_vuln_add": [item], "poc_exp_add": [item]}})
    if "threatbook" in url:
        return httpx.Response(200, json={"data": {"highrisk": [{"id": "XVE-2026-0001", "vuln_name_zh": "Fake 中间件漏洞", "vuln_publish_time": "2026-10-11"}]}})
    if "api.github.com" in url:
        return httpx.Response(200, json=[{"ghsa_id": "GHSA-fake-0001", "cve_id": None, "summary": "Prototype pollution in fake-lib", "description": "desc",
                                          "severity": "high", "html_url": "https://github.com/advisories/GHSA-fake-0001", "published_at": "2026-10-10T00:00:00Z",
                                          "updated_at": "2026-10-11T00:00:00Z", "cvss": {"score": 8.1},
                                          "vulnerabilities": [{"package": {"ecosystem": "npm", "name": "fake-lib"}}]}])
    if url == "https://psirt.example.invalid/rss":
        return httpx.Response(200, content=RSS.encode())
    if url == "https://fake.example.invalid/atom":
        return httpx.Response(200, content=ATOM.encode())
    return httpx.Response(404)


def client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_cn_sources_parse() -> None:
    c = cn.fetch_chaitin(client(), pages=1)[0]
    assert (c.key, c.source, c.native_id, c.severity, c.url) == ("CVE-2026-31001", "chaitin", "CT-2026-0001", "critical", "https://stack.chaitin.com/vuldb/detail/fake-uuid-1")
    o = cn.fetch_oscs(client())[0]
    assert (o.key, o.severity, o.title, o.published) == ("OSCS:MPS-2026-0001", "high", "Fake 组件反序列化漏洞", datetime(2025, 10, 12, tzinfo=UTC))
    t = cn.fetch_ti(client())
    assert len(t) == 1 and t[0].key == "CVE-2026-31001" and t[0].severity == "critical"  # 极危 → 严重；两组里重复的只算一次
    b = cn.fetch_threatbook(client())[0]
    assert b.key == "THREATBOOK:XVE-2026-0001" and b.url.endswith("XVE-2026-0001")
    g, cur = cn.fetch_ghsa(client(), {}, NOW)
    assert g[0].key == "GHSA:GHSA-fake-0001" and g[0].cvss == 8.1 and "fake-lib" in g[0].summary and cur == {"modified_since": NOW.isoformat()}


def test_rss_and_atom() -> None:
    recs = cn.fetch_rss(client(), [{"name": "Fake PSIRT", "url": "https://psirt.example.invalid/rss"}, {"url": "https://fake.example.invalid/atom"}])
    assert [r.key for r in recs] == ["CVE-2026-31001", "RSS:DSA-2026-002", "CVE-2026-32001"]  # 正文提到多个 CVE 时不猜
    assert recs[0].title.startswith("[Fake PSIRT]") and recs[0].published == datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    assert "<p>" not in recs[0].summary and recs[2].severity == "high"


def test_same_cve_from_many_sources_is_one_record(session) -> None:  # type: ignore[no-untyped-def]
    for name in ("chaitin", "ti", "rss"):
        row = sync.source_row(session, name)
        row.config = {"feeds": [{"name": "Fake PSIRT", "url": "https://psirt.example.invalid/rss"}]}
    assert sync.run_source(session, "chaitin", client(), now=NOW) == ["CVE-2026-31001"]
    assert sync.run_source(session, "ti", client(), now=NOW.replace(minute=1)) == []
    sync.run_source(session, "rss", client(), now=NOW.replace(minute=2))
    assert sources_of(session, "CVE-2026-31001") == ["chaitin", "ti", "rss"]
    rec = session.get(VulnRecord, "CVE-2026-31001")
    assert rec.title == "Fake 网关远程命令执行漏洞" and rec.severity == "critical" and {"CT-2026-0001", "QVD-2026-0001"} <= set(rec.aliases)
    sync.source_row(session, "rss").config = {}
    sync.run_source(session, "rss", client(), now=NOW)
    assert session.get(VulnSource, "rss").message == "还没有填写 RSS 地址"


def test_parse_webhook() -> None:
    wv = {"type": "watchvuln-vulninfo", "content": {"unique_key": "AVD-2026-31001", "title": "Fake 漏洞（AVD）", "description": "伪造", "severity": "严重",
                                                    "cve": "CVE-2026-31001", "disclosure": "2026-10-12", "references": ["https://ref.example.invalid/2"],
                                                    "from": "https://avd.aliyun.com/detail?id=AVD-2026-31001"}}
    r = cn.parse_webhook(wv)
    assert (r.key, r.source, r.severity) == ("CVE-2026-31001", "avd", "critical")
    assert cn.parse_webhook({"type": "watchvuln-initial", "content": {}}) is None
    g = cn.parse_webhook({"id": "FAKE-1", "title": "自定义来源漏洞", "severity": "high", "url": "https://x.example.invalid"})
    assert (g.key, g.source) == ("WEBHOOK:FAKE-1", "webhook")
    with pytest.raises(ValueError):
        cn.parse_webhook({"title": "缺少编号"})


def test_webhook_endpoint(client, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "fake-admin", "admin")
    c = login(client, "fake-admin")
    assert client.post("/api/vuln/webhook/anything", json={}).status_code == 404  # 未开启
    tok = c.post("/api/vuln/webhook-token").json()
    assert tok["path"] == f"/api/vuln/webhook/{tok['token']}" and tok["token"].startswith("ysw_")
    anon = client.__class__(client.app)  # 不带登录 Cookie、不带 CSRF 头
    body = {"type": "watchvuln-vulninfo", "content": {"unique_key": "SSVID-1", "title": "Fake Seebug 漏洞", "severity": "高危", "cve": "",
                                                      "from": "https://www.seebug.org/vuldb/ssvid-1"}}
    r = anon.post(tok["path"], json=body).json()
    assert r == {"accepted": True, "id": "SEEBUG:SSVID-1", "new": True}
    assert anon.post(tok["path"], json={"type": "watchvuln-text", "content": {"message": "hi"}}).json()["ignored"] is True
    assert anon.post(tok["path"], content=b"not json").status_code == 422
    assert anon.post("/api/vuln/webhook/ysw_wrong", json=body).status_code == 404
    c.delete("/api/vuln/webhook-token")
    assert anon.post(tok["path"], json=body).status_code == 404
    with factory() as db:
        assert db.scalar(select(VulnSighting.source).where(VulnSighting.record_id == "SEEBUG:SSVID-1")) == "seebug"
    src = c.get("/api/vuln/sources").json()
    assert {s["name"] for s in src} >= {"chaitin", "oscs", "ti", "threatbook", "ghsa", "rss"}
    assert all(not s["enabled"] for s in src if s["group"] in ("国内", "开源组件", "厂商公告"))  # 由用户决定是否启用
    assert c.patch("/api/vuln/sources/kev", json={"feeds": [{"url": "https://x.example.invalid/rss"}]}).status_code == 422
    rss = c.patch("/api/vuln/sources/rss", json={"enabled": True, "feeds": [{"name": "Fake", "url": "https://psirt.example.invalid/rss"}]}).json()
    assert rss["feeds"] == [{"name": "Fake", "url": "https://psirt.example.invalid/rss"}]
    assert c.get("/api/vuln/sources-info").json()["via_webhook"] == ["阿里云漏洞库", "Seebug", "启明星辰", "Struts2 公告"]
