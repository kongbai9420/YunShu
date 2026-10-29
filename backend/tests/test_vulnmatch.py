"""4.3 P18d：版本比较、区间判断、全量比对、处置与复查、资产范围。漏洞数据取自 tests/fixtures/vuln（真实公开数据裁剪），设备为伪造。"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import metrics, vulnmatch
from app.models import Asset, AssetPackage, CollectionStatus, ManagementEndpoint, UserScopeAsset, User, VulnMatch, VulnRecord
from app.vulndb import sources
from app.vulndb.normalize import upsert
from app.vulnmatch import cpe_hit, dpkg_compare, generic_compare, pkg_hit
from tests.api_helpers import add_user, login

FIX = Path(__file__).parent / "fixtures" / "vuln"
NOW = datetime(2026, 10, 19, 2, 0, tzinfo=UTC)


@pytest.mark.parametrize(("a", "b", "sign"), [
    ("1.0", "1.0-0", 0), ("0:1.0", "1.0", 0), ("1.0~rc1", "1.0", -1), ("1:0.9", "2.0", 1), ("1.0+b1", "1.0", 1),
    ("3.0.10-1~deb12u1", "3.0.9-1", 1), ("3.0.11-1~deb12u2", "3.0.10-1~deb12u1", 1), ("2.36-9+deb12u7", "2.36-9+deb12u10", -1),
    ("1.2.3a", "1.2.3", 1), ("1.0-1ubuntu1", "1.0-1", 1), ("3.0.2-0ubuntu1.10", "3.0.2-0ubuntu1.12", -1), ("1.0~~", "1.0~", -1),
])
def test_dpkg_compare(a: str, b: str, sign: int) -> None:
    r = dpkg_compare(a, b)
    assert (r > 0) - (r < 0) == sign
    r2 = dpkg_compare(b, a)
    assert (r2 > 0) - (r2 < 0) == -sign


@pytest.mark.parametrize(("a", "b", "sign"), [
    ("7.00.00.171", "3.21.21.21", 1), ("2.83.83.83", "2.60.60.60", 1), ("7.2-64570-3", "7.2-64570", 1), ("7.12.1", "7.12", 1),
    ("1.0", "1.0beta", 1), ("8.2.4", "8.2.4", 0), ("2.19.1", "2.9.0", 1),
])
def test_generic_compare(a: str, b: str, sign: int) -> None:
    assert generic_compare(a, b) == sign and generic_compare(b, a) == -sign


def test_pkg_hit() -> None:
    fixed = [{"introduced": "0", "fixed": "3.0.10-1~deb12u1"}]
    assert pkg_hit("3.0.9-1", fixed).fixed == "3.0.10-1~deb12u1"
    assert pkg_hit("3.0.10-1~deb12u1", fixed) is None and pkg_hit("3.0.11-1~deb12u2", fixed) is None
    assert pkg_hit("1.0-1", [{"introduced": "0"}]).note == "发行版尚未发布修复版本"
    assert pkg_hit("1.5-1", [{"introduced": "2.0-1", "fixed": "2.1-1"}]) is None
    assert pkg_hit("1.5-1", [{"introduced": "1.0-1", "last_affected": "1.5-1"}]) is not None


def test_cpe_hit() -> None:
    rng = [{"exact": "", "update": "", "start_incl": "", "start_excl": "", "end_incl": "", "end_excl": "3.21.21.21", "and_other": False}]
    assert cpe_hit("dell:idrac9_firmware", "3.00.00.00", rng) == vulnmatch.Hit("affected", fixed="3.21.21.21")
    assert cpe_hit("dell:idrac9_firmware", "7.00.00.171", rng) is None
    assert cpe_hit("dell:idrac9_firmware", "3.00.00.00", [{**rng[0], "and_other": True}]).confidence == "possible"
    assert cpe_hit("synology:diskstation_manager", "7.2-64570-3", [{"exact": "7.2-64570-3"}]).confidence == "affected"
    assert cpe_hit("x:y", "1.0", [{"exact": ""}]) is None  # 所有版本（*）且无区间：不据此判断
    esxi = [{"exact": "8.0", "update": "update_2"}]
    assert cpe_hit("vmware:esxi", "8.0.2", esxi).confidence == "possible"  # VMware 要看构建号
    assert cpe_hit("vmware:esxi", "8.0.3", esxi) is None and cpe_hit("vmware:esxi", "8.0.0", [{"exact": "8.0", "update": ""}]) is not None


def _load_vulns(session) -> None:  # type: ignore[no-untyped-def]
    nvd = sources.parse_nvd_cve(json.loads((FIX / "nvd_cve.json").read_text(encoding="utf-8")), {"dell:idrac9_firmware"})
    osv = sources.parse_osv(json.loads((FIX / "osv_debian.json").read_text(encoding="utf-8")), {"Debian:12"})
    upsert(session, [nvd, osv], NOW)


def _server(session, name: str = "fake-r740", *, fw: str = "3.00.00.00", openssl: str = "3.0.9-1") -> Asset:  # type: ignore[no-untyped-def]
    a = Asset(name=name, form="physical", role="server", vendor="Dell", model="PowerEdge R740", lifecycle="active")
    snmp = ManagementEndpoint(address="192.0.2.30", port=161, protocol="snmp")
    ssh = ManagementEndpoint(address="192.0.2.31", port=22, protocol="ssh", collect_packages=True)
    a.endpoints = [snmp, ssh]
    session.add(a)
    session.flush()
    session.add(CollectionStatus(endpoint_id=snmp.id, asset_id=a.id, protocol="snmp", state="ok", last_attempt_at=NOW, last_success_at=NOW,
                                 facts={"idrac": True, "idrac_name": "iDRAC9", "idrac_firmware": fw}))
    session.add(AssetPackage(asset_id=a.id, endpoint_id=ssh.id, ecosystem="Debian:12", name="libssl3", version=openssl, source="openssl", source_version=openssl))
    session.add(AssetPackage(asset_id=a.id, endpoint_id=ssh.id, ecosystem="Debian:12", name="openssl", version=openssl, source="openssl", source_version=openssl))
    session.flush()
    return a


def test_run_matches_resolves_and_writes_metrics(session) -> None:  # type: ignore[no-untyped-def]
    _load_vulns(session)
    a = _server(session)
    patched = _server(session, "fake-patched", fw="7.00.00.171", openssl="3.0.11-1~deb12u2")
    r = vulnmatch.run(session, now=NOW)
    assert (r["matches"], r["new"]) == (2, 2)
    rows = {m.record_id: m for m in session.scalars(select(VulnMatch).where(VulnMatch.asset_id == a.id))}
    assert rows["CVE-2018-1243"].fixed_version == "3.21.21.21" and rows["CVE-2018-1243"].label == "iDRAC9 固件"
    assert rows["CVE-2023-2975"].product == "Debian:12/openssl" and rows["CVE-2023-2975"].fixed_version == "3.0.10-1~deb12u1"
    assert session.scalar(select(VulnMatch).where(VulnMatch.asset_id == patched.id)) is None  # 两个二进制包同源，只记一次
    latest = {v.key: v.value for v in metrics.latest_many(session, [a.id], now=NOW)[a.id]}
    assert latest["vuln.open"] == 2 and latest["vuln.alert_count"] == 0  # 7.5 与 5.3 分，都没被利用：不告警
    session.get(VulnRecord, "CVE-2023-2975").kev = True
    vulnmatch.run(session, now=NOW + timedelta(minutes=5))
    assert {v.key: v.value for v in metrics.latest_many(session, [a.id], now=NOW)[a.id]}["vuln.alert_count"] == 1
    # 升级后不再命中：标为已解决，处置记录保留
    for p in session.scalars(select(AssetPackage).where(AssetPackage.asset_id == a.id)):
        p.source_version = "3.0.11-1~deb12u2"
    r = vulnmatch.run(session, now=NOW + timedelta(hours=1))
    assert r["resolved"] == 1 and not rows["CVE-2023-2975"].active and rows["CVE-2023-2975"].resolved_at is not None


def test_review_date_expiry(session) -> None:  # type: ignore[no-untyped-def]
    _load_vulns(session)
    a = _server(session)
    vulnmatch.run(session, now=NOW)
    m = session.scalar(select(VulnMatch).where(VulnMatch.asset_id == a.id, VulnMatch.record_id == "CVE-2018-1243"))
    m.status, m.reason, m.review_at = "accepted", "fake 隔离网段", date(2026, 10, 25)
    vulnmatch.run(session, now=NOW + timedelta(days=1))
    assert m.status == "accepted"
    vulnmatch.run(session, now=datetime(2026, 10, 25, 1, tzinfo=UTC))
    session.refresh(m)
    assert m.status == "open" and "复查日期已到" in m.reason and m.review_at is None


def test_api_matches_triage_and_scope(app_factory, client, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        _load_vulns(db)
        a = _server(db, "fake-mine")
        b = _server(db, "fake-other")
        vulnmatch.run(db, now=NOW)
        db.commit()
        a_id, b_id = a.id, b.id
    add_user(factory, "fake-admin", "admin")
    add_user(factory, "fake-scoped", "viewer")
    with factory() as db:
        u = db.scalar(select(User).where(User.username == "fake-scoped"))
        u.scope_all = False
        db.add(UserScopeAsset(user_id=u.id, asset_id=a_id))
        db.commit()
    adm = login(client, "fake-admin")
    lst = adm.get("/api/vuln/records?local=true").json()
    assert lst["total"] == 2 and {i["local_assets"] for i in lst["items"]} == {2}
    detail = adm.get("/api/vuln/records/CVE-2018-1243").json()
    assert detail["local_assets"] == 2 and {m["asset_name"] for m in detail["matches"]} == {"fake-mine", "fake-other"}
    scoped = login(app_factory(), "fake-scoped")
    assert {m["asset_name"] for m in scoped.get("/api/vuln/records/CVE-2018-1243").json()["matches"]} == {"fake-mine"}
    assert scoped.get(f"/api/vuln/matches?asset_id={b_id}").status_code == 404  # 范围外资产当作不存在
    mine = scoped.get(f"/api/vuln/matches?asset_id={a_id}").json()
    assert len(mine) == 2 and mine[0]["status_text"] == "待处理"
    mid = mine[0]["id"]
    assert scoped.patch(f"/api/vuln/matches/{mid}", json={"status": "affected"}).status_code == 403
    assert adm.patch(f"/api/vuln/matches/{mid}", json={"status": "not_affected"}).status_code == 422  # 必须写理由
    assert adm.patch(f"/api/vuln/matches/{mid}", json={"status": "accepted", "reason": "fake"}).status_code == 422  # 必须设复查日期
    assert adm.patch(f"/api/vuln/matches/{mid}", json={"status": "accepted", "reason": "fake", "review_at": "2000-01-01"}).status_code == 422
    r = adm.patch(f"/api/vuln/matches/{mid}", json={"status": "not_affected", "reason": "fake：该功能未启用"}).json()
    assert r["status"] == "not_affected" and r["status_by"] == "fake-admin"
    assert adm.get(f"/api/vuln/records/{r['record_id']}").json()["local_assets"] == 1
    assert adm.post("/api/vuln/match-now").json() == {"queued": 1}
