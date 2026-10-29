"""4.3 P18c：漏洞情报数据源（NVD、KEV、OSV、EPSS）、多来源归并、订阅、同步计划、离线包、接口。

样本取自 2026-10-19 各接口的真实公开数据（已裁剪），网络请求全部由伪造接口应答，不访问外网。
"""
from __future__ import annotations

import gzip
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.models import Asset, AssetPackage, CollectionStatus, ManagementEndpoint, Task, VulnAffect, VulnRecord, VulnSighting, VulnSource, VulnSubscription
from app.vulndb import bundle, sources, subscriptions, sync
from app.vulndb.normalize import Affect, NormRecord, cvss3_score, key_for, parse_time, sources_of, upsert
from tests.api_helpers import add_user, login

FIX = Path(__file__).parent / "fixtures" / "vuln"
NOW = datetime(2026, 10, 19, 2, 0, tzinfo=UTC)


def fx(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeFeeds:
    """按 URL 应答的伪造数据源。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.nvd_cve = fx("nvd_cve.json")
        self.osv = {"DEBIAN-CVE-2023-2975": fx("osv_debian.json"), "UBUNTU-CVE-2023-2975": fx("osv_ubuntu.json")}
        self.kev = fx("kev.json")
        self.fail: str = ""

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if self.fail and self.fail in url:
            return httpx.Response(503)
        if url.startswith(sources.NVD_API):
            return httpx.Response(200, json={"resultsPerPage": 1, "startIndex": 0, "totalResults": 1, "vulnerabilities": [{"cve": self.nvd_cve}]})
        if url == sources.KEV_URL:
            return httpx.Response(200, json=self.kev)
        if url.endswith("/querybatch"):
            body = json.loads(request.content)
            results = []
            for q in body["queries"]:
                hit = q["package"]["name"] == "openssl"
                results.append({"vulns": [{"id": "DEBIAN-CVE-2023-2975", "modified": "2026-09-15T09:01:32Z"}, {"id": "DSA-5500-1", "modified": "x"}]} if hit else {})
            return httpx.Response(200, json={"results": results})
        if "/v1/vulns/" in url:
            return httpx.Response(200, json=self.osv[url.rsplit("/", 1)[1]])
        if url.startswith(sources.EPSS_API):
            cves = request.url.params["cve"].split(",")
            return httpx.Response(200, json={"data": [{"cve": c, "epss": "0.5", "percentile": "0.9"} for c in cves]})
        if "osv-vulnerabilities" in url:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("DEBIAN-CVE-2023-2975.json", json.dumps(self.osv["DEBIAN-CVE-2023-2975"]))
                z.writestr("DSA-5500-1.json", json.dumps({"id": "DSA-5500-1", "affected": []}))
            return httpx.Response(200, content=buf.getvalue())
        if url == bundle.EPSS_CSV:
            csv = "#model_version:v2026,score_date:2026-10-18\ncve,epss,percentile\nCVE-2023-2975,0.00622,0.48752\nCVE-2099-0001,0.1,0.2\n"
            return httpx.Response(200, content=gzip.compress(csv.encode()))
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))


# ---------------- 归一化 ----------------


@pytest.mark.parametrize(("vector", "score"), [
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8), ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:L/A:N", 5.3),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0), ("CVSS:3.0/AV:N/AC:H/PR:L/UI:N/S:U/C:H/I:H/A:H", 7.5),
    ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:N/I:N/A:N", 0.0), ("CVSS:4.0/AV:N", None), ("garbage", None),
])
def test_cvss3_score(vector: str, score: float | None) -> None:
    assert cvss3_score(vector) == score


def test_key_for() -> None:
    assert key_for("AVD-2026-1", "avd", aliases=["cve-2026-12345"]) == "CVE-2026-12345"
    assert key_for("QVD-2026-9", "ti", text="涉及 CVE-2026-1111 的远程代码执行") == "CVE-2026-1111"
    assert key_for("QVD-2026-9", "ti", text="CVE-2026-1111 与 CVE-2026-2222") == "TI:QVD-2026-9"  # 提到多个 CVE 时不猜


def test_parse_nvd_keeps_only_tracked_products() -> None:
    rec = sources.parse_nvd_cve(fx("nvd_cve.json"), {"dell:idrac9_firmware"})
    assert rec.key == "CVE-2018-1243" and rec.cvss == 7.5 and rec.severity == "high" and rec.url.endswith("CVE-2018-1243")
    assert [a.product for a in rec.affects] == ["dell:idrac9_firmware"]
    assert rec.affects[0].ranges == [{"exact": "", "update": "", "start_incl": "", "start_excl": "", "end_incl": "", "end_excl": "3.21.21.21", "and_other": False}]
    assert sources.parse_nvd_cve(fx("nvd_cve.json"), None).affects is None


def test_parse_osv() -> None:
    rec = sources.parse_osv(fx("osv_debian.json"), {"Debian:12"})
    assert rec.key == "CVE-2023-2975" and rec.cvss == 5.3 and rec.severity == "medium"
    assert [(a.ecosystem, a.product, a.ranges) for a in rec.affects] == [("Debian:12", "openssl", [{"introduced": "0", "fixed": "3.0.10-1~deb12u1"}])]
    assert rec.url == "https://security-tracker.debian.org/tracker/CVE-2023-2975" and rec.affects[0].note == "not yet assigned"
    ub = sources.parse_osv(fx("osv_ubuntu.json"))
    assert ub.key == "CVE-2023-2975" and ub.affects[0].ecosystem == "Ubuntu:22.04:LTS" and ub.url.startswith("https://ubuntu.com/security/")
    assert sources.parse_osv({"id": "DSA-5500-1"}) is None


def test_upsert_merges_sources(session) -> None:  # type: ignore[no-untyped-def]
    nvd = sources.parse_nvd_cve(fx("nvd_cve.json"), {"dell:idrac9_firmware"})
    assert upsert(session, [nvd], NOW) == ["CVE-2018-1243"]
    cn = NormRecord(key="CVE-2018-1243", source="avd", native_id="AVD-2018-1243", title="Dell iDRAC 远程代码执行漏洞", severity="critical",
                    url="https://avd.aliyun.com/detail?id=AVD-2018-1243")
    kev = NormRecord(key="CVE-2018-1243", source="kev", title="Dell iDRAC Vulnerability", kev={"dateAdded": "2026-10-01", "knownRansomwareCampaignUse": "Known"})
    assert upsert(session, [cn, kev], NOW + timedelta(hours=1)) == []
    rec = session.get(VulnRecord, "CVE-2018-1243")
    assert rec.title == "Dell iDRAC 远程代码执行漏洞"  # 中文标题优先
    assert rec.cvss == 7.5 and rec.cvss_source == "nvd" and rec.severity == "high"  # 有 NVD 评分时以 NVD 为准
    assert rec.kev and rec.kev_added == "2026-10-01" and rec.kev_ransomware
    assert "AVD-2018-1243" in rec.aliases and parse_time(rec.first_seen_at) == NOW  # 首次发现时间不随后续来源改变
    assert sources_of(session, "CVE-2018-1243") == ["nvd", "avd", "kev"]
    # 只替换本来源的受影响范围，KEV 不提供范围时保留 NVD 的
    assert session.scalar(select(VulnAffect.product).where(VulnAffect.record_id == "CVE-2018-1243")) == "dell:idrac9_firmware"
    upsert(session, [NormRecord(key="CVE-2018-1243", source="nvd", affects=[Affect("cpe", "dell:idrac8_firmware", [{"end_excl": "2.70"}])])], NOW)
    assert [a for (a,) in session.execute(select(VulnAffect.product))] == ["dell:idrac8_firmware"]


# ---------------- 数据源 ----------------


def test_fetch_nvd_full_then_incremental() -> None:
    fake, sleeps = FakeFeeds(), []
    recs, cursor = sources.fetch_nvd(fake.client(), products=["dell:idrac9_firmware", "vmware:vcenter_server"], keywords=["openssh"], cursor={},
                                     now=NOW, sleep=sleeps.append)
    assert [r.key for r in recs] == ["CVE-2018-1243"]
    params = [dict(r.url.params) for r in fake.requests]
    assert params[0]["virtualMatchString"] == "cpe:2.3:o:dell:idrac9_firmware" and "lastModStartDate" not in params[0]
    assert params[1]["virtualMatchString"] == "cpe:2.3:a:vmware:vcenter_server"
    assert params[2]["keywordSearch"] == "openssh" and "pubStartDate" in params[2]
    assert sleeps == [6, 6]  # 无密钥：请求间隔 6 秒
    fake.requests.clear()
    sources.fetch_nvd(fake.client(), products=["dell:idrac9_firmware"], keywords=[], cursor=cursor, now=NOW + timedelta(hours=6),
                      api_key="fake-nvd-key", sleep=sleeps.append)
    req = fake.requests[0]
    assert "lastModStartDate" in req.url.params and req.headers["apikey"] == "fake-nvd-key"


def test_fetch_osv_queries_installed_packages_only_and_skips_unchanged() -> None:
    fake = FakeFeeds()
    pkgs = [("Debian:12", "openssl", "3.0.9-1"), ("Debian:12", "bash", "5.2.15-2")]
    recs, cursor = sources.fetch_osv(fake.client(), packages=pkgs, cursor={})
    assert [r.key for r in recs] == ["CVE-2023-2975"]
    assert not any("DSA-5500-1" in str(r.url) for r in fake.requests)  # DSA 不取详情
    fake.requests.clear()
    recs, _ = sources.fetch_osv(fake.client(), packages=pkgs, cursor=cursor)
    assert recs == [] and len(fake.requests) == 1  # 没变化的记录不重复取


def test_run_sources_and_failures(session) -> None:  # type: ignore[no-untyped-def]
    fake = FakeFeeds()
    a = Asset(name="fake-r740", form="physical", role="server", vendor="Dell", model="PowerEdge R740", lifecycle="active")
    ep = ManagementEndpoint(address="192.0.2.30", port=161, protocol="snmp")
    a.endpoints = [ep]
    session.add(a)
    session.flush()
    session.add(CollectionStatus(endpoint_id=ep.id, asset_id=a.id, protocol="snmp", state="ok", last_attempt_at=NOW, last_success_at=NOW,
                                 facts={"idrac": True, "idrac_name": "iDRAC9", "idrac_firmware": "3.00.00.00"}))
    session.add(AssetPackage(asset_id=a.id, endpoint_id=ep.id, ecosystem="Debian:12", name="libssl3", version="3.0.9-1", source="openssl", source_version="3.0.9-1"))
    session.flush()
    new = sync.run_source(session, "nvd", fake.client(), now=NOW, sleep=lambda _s: None)
    assert new == ["CVE-2018-1243"]
    assert session.scalar(select(VulnSubscription.value).where(VulnSubscription.auto.is_(True), VulnSubscription.kind == "product")) == "dell:idrac9_firmware"
    assert sync.run_source(session, "kev", fake.client(), now=NOW) == ["CVE-2026-102764", "CVE-2025-61624"]
    assert sync.run_source(session, "kev", fake.client(), now=NOW) == [] and session.get(VulnSource, "kev").items == 0  # 目录版本没变，不重复处理
    assert sync.run_source(session, "osv", fake.client(), now=NOW) == ["CVE-2023-2975"]
    session.get(VulnRecord, "CVE-2023-2975").severity = "high"
    sync.run_source(session, "epss", fake.client(), now=NOW)
    assert session.get(VulnRecord, "CVE-2023-2975").epss == 0.5
    fake.fail = "cisa.gov"
    session.get(VulnSource, "kev").cursor = {}
    sync.run_source(session, "kev", fake.client(), now=NOW)
    row = session.get(VulnSource, "kev")
    assert row.state == "failed" and "503" in row.message and row.last_ok_at is not None


def test_schedule_respects_online_switch(session) -> None:  # type: ignore[no-untyped-def]
    assert sync.enqueue_due(session, now=NOW) == 0  # 外网同步默认关闭
    sync.settings_row(session).online_enabled = True
    assert sync.enqueue_due(session, now=NOW) == 5  # CVE 官方记录 / NVD / KEV / OSV / EPSS 默认勾选
    assert sync.enqueue_due(session, now=NOW) == 0
    sync.source_row(session, "kev").last_run_at = NOW
    sync.source_row(session, "nvd").enabled = False
    assert sync.enqueue_due(session, now=NOW + timedelta(minutes=10)) == 3  # cve、osv、epss 尚未运行过，kev 还没到间隔
    assert {t.payload["source"] for t in session.scalars(select(Task))} == {"cve", "nvd", "kev", "osv", "epss"}


def test_bundle_build_and_import(session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    fake = FakeFeeds()
    out = tmp_path / "vulndb.zip"
    manifest = bundle.build(out, http=fake.client(), ecosystems=["Debian:12"], products=["dell:idrac9_firmware"], log=lambda _m: None, sleep=lambda _s: None)
    assert manifest["counts"] == {"kev": 2, "epss": "ok", "osv:Debian": 1, "nvd:dell:idrac9_firmware": 1}
    with zipfile.ZipFile(out) as z:
        assert sorted(z.namelist()) == ["epss.csv.gz", "kev.json", "manifest.json", "nvd/dell__idrac9_firmware.json", "osv/Debian.jsonl.gz"]
    with out.open("rb") as f:
        result = bundle.import_bundle(session, f, now=NOW)
    assert result["new"] == 4 and result["counts"]["epss"] == 1
    rec = session.get(VulnRecord, "CVE-2023-2975")
    assert rec.epss == pytest.approx(0.00622) and {a.ecosystem for a in session.scalars(select(VulnAffect).where(VulnAffect.record_id == rec.id))} == {"Debian:12"}
    with pytest.raises(ValueError):
        bundle.import_bundle(session, io.BytesIO(b"not a zip"))


# ---------------- 接口 ----------------


def test_api(app_factory, client, factory, tmp_path) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "fake-admin", "admin")
    add_user(factory, "fake-viewer", "viewer")
    adm = login(client, "fake-admin")
    s = adm.get("/api/vuln/settings").json()
    assert s["online_enabled"] is False and s["has_nvd_key"] is False
    srcs = adm.get("/api/vuln/sources").json()
    assert [(x["name"], x["enabled"], x["offline"]) for x in srcs[:5]] == [("cve", True, False), ("nvd", True, True), ("kev", True, True), ("osv", True, True), ("epss", True, True)]
    assert adm.post("/api/vuln/sources/kev/sync").status_code == 422  # 外网同步关闭时不能手动同步
    r = adm.put("/api/vuln/settings", json={"online_enabled": True, "aggregation_minutes": 30, "nvd_api_key": "fake-nvd-key", "revision": s["revision"]}).json()
    assert r["online_enabled"] and r["has_nvd_key"] and "fake-nvd-key" not in json.dumps(r)
    assert adm.post("/api/vuln/sources/kev/sync").json() == {"queued": 1}
    assert adm.patch("/api/vuln/sources/epss", json={"enabled": False, "interval_minutes": 60}).json()["enabled"] is False
    bad = adm.post("/api/vuln/subscriptions", json={"kind": "product", "value": "openssh"})
    assert bad.status_code == 422
    sub = adm.post("/api/vuln/subscriptions", json={"kind": "product", "value": "cpe:2.3:a:openbsd:openssh", "min_severity": "high"}).json()
    assert sub["value"] == "openbsd:openssh" and sub["auto"] is False
    assert adm.post("/api/vuln/subscriptions", json={"kind": "product", "value": "openbsd:openssh"}).status_code == 409
    assert adm.post("/api/vuln/subscriptions", json={"kind": "keyword", "value": "Struts2"}).status_code == 201
    view = login(app_factory(), "fake-viewer")
    assert view.get("/api/vuln/subscriptions").status_code == 200
    assert view.post("/api/vuln/subscriptions", json={"kind": "keyword", "value": "x1"}).status_code == 403
    out = tmp_path / "b.zip"
    bundle.build(out, http=FakeFeeds().client(), ecosystems=["Debian:12"], products=["dell:idrac9_firmware"], log=lambda _m: None, sleep=lambda _s: None)
    res = adm.post("/api/vuln/bundle", files={"file": ("b.zip", out.read_bytes(), "application/zip")}).json()
    assert res["new"] == 4
    assert adm.get("/api/vuln/settings").json()["last_bundle_at"]
    lst = view.get("/api/vuln/records?kev=true").json()
    assert lst["total"] == 2 and lst["items"][0]["sources"] == [{"name": "kev", "label": "CISA KEV"}]
    detail = view.get("/api/vuln/records/CVE-2023-2975").json()
    assert detail["cvss"] == 5.3 and detail["sources"][0]["url"].startswith("https://security-tracker.debian.org/")
    assert adm.post("/api/vuln/bundle", files={"file": ("x.zip", b"nope", "application/zip")}).status_code == 422
