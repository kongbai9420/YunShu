"""4.2 P16：巡检报告、只读令牌与 /metrics、OIDC 单点登录。全部伪造数据与伪造身份提供方。"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import inspection, metrics
from app.metrics import Point
from app.models import Alert, AlertRule, Asset, CollectionStatus, InspectionReport, ManagementEndpoint, User, UserScopeAsset
from app.oidc import OidcConfig, OidcError, Provider
from tests.api_helpers import FAKE_PASSWORD, add_user, login

NOW = datetime(2026, 10, 19, 2, 0, tzinfo=UTC)  # 北京时间 10:00


def _asset(db: Session, name: str, points: list[Point], *, at: datetime = NOW, interval: int = 60) -> Asset:
    a = Asset(name=name, form="physical", role="server")
    db.add(a)
    db.flush()
    metrics.write_sample(db, a.id, "fake", points, observed_at=at, interval_seconds=interval)
    return a


def test_inspection_finds_problems_and_skips_stale_assets(session: Session) -> None:
    hot = _asset(session, "fake-hot", [Point("cpu.temp", 83.0, "°C"), Point("disk.used_pct:/", 85.0, "%"), Point("fan.rpm:FAN1", 0.0, "RPM")])
    hw = _asset(session, "fake-hw", [Point("psu.health:PS2", 0.0, "", message="Critical"), Point("raid.health:VD0", 0.5, ""),
                                     Point("sel.critical_24h", 2.0, "", message="fake voltage event"), Point("cpu.temp", 55.0, "°C")])
    prot = _asset(session, "fake-pve", [Point("backup.failed_24h", 1.0, "", message="101 job errors"), Point("backup.last_ok_age_h", 60.0, "h"),
                                        Point("replication.failed", 1.0, ""), Point("storage.used_pct:local-zfs", 93.0, "%")])
    ups = _asset(session, "fake-ups", [Point("ups.on_mains", 0.0, ""), Point("ups.battery_health", 1.0, "")])
    _asset(session, "fake-old", [Point("cpu.temp", 99.0, "°C")], at=NOW - timedelta(hours=2))  # 过期：不按 99 ℃ 报
    ok = _asset(session, "fake-ok", [Point("cpu.temp", 40.0, "°C")])
    ep = ManagementEndpoint(asset_id=ok.id, address="192.0.2.40", protocol="snmp", purpose="monitoring")
    session.add(ep)
    session.flush()
    session.add(CollectionStatus(endpoint_id=ep.id, asset_id=ok.id, protocol="snmp", state="failed", message="fake timeout", last_attempt_at=NOW))
    rule = AlertRule(name="fake", metric_key="cpu.temp", threshold=80, recovery_threshold=75, severity="critical")
    session.add(rule)
    session.flush()
    # BMC 日志：读“服务器告警”里待处理的严重事件，已处理的不再列出
    from app import server_events
    server_events.record(session, hw.id, "ipmi_sel", [{"id": "1", "created": NOW, "severity": "Critical", "message": "fake voltage event"}], NOW)
    server_events.record(session, prot.id, "ipmi_sel", [{"id": "2", "created": NOW, "severity": "Critical", "message": "fake handled"}], NOW)
    for e in session.scalars(select(server_events.ServerEvent).where(server_events.ServerEvent.asset_id == prot.id)):
        e.state = "handled"
    session.add(Alert(rule_id=rule.id, asset_id=hot.id, asset_name="fake-hot", metric_key="cpu.temp", state="active", severity="critical", title="CPU 高温"))
    session.commit()

    items, count = inspection.inspect(session, NOW)
    got = {(i["asset_name"], i["title"]): i for i in items}
    assert count == 6
    assert got[("fake-hot", "CPU 温度 83 ℃")]["severity"] == "critical"
    assert got[("fake-hot", "磁盘“/”已用 85%")]["severity"] == "warning"
    assert ("fake-hot", "风扇“FAN1”转速为 0") in got and ("fake-hot", "未处理告警：CPU 高温") in got
    assert got[("fake-hw", "电源模块“PS2”故障")]["severity"] == "critical"
    assert got[("fake-hw", "RAID / 存储池“VD0”告警")]["severity"] == "warning"
    assert "fake voltage event" in got[("fake-hw", "1 条严重硬件事件待处理（服务器告警）")]["detail"]
    assert {t for n, t in got if n == "fake-pve"} == {"24 小时内有 1 次备份失败", "已有 60 小时没有成功备份", "1 个复制任务出错", "存储池“local-zfs”已用 93%"}
    assert got[("fake-ups", "UPS 正由电池供电")]["severity"] == "critical"
    assert got[("fake-old", "全部指标已过期")]["severity"] == "warning" and not any(n == "fake-old" and "温度" in t for n, t in got)
    assert ("fake-ok", "SNMP 采集失败") in got and not any(n == "fake-hw" and "CPU 温度" in t for n, t in got)
    assert items[0]["severity"] == "critical" and items[-1]["severity"] == "warning"  # 严重的排在前面

    report = inspection.generate(session, kind="manual", by="fake", now=NOW)
    only = inspection.view(report, {ups.id})
    assert [i["asset_name"] for i in only["items"]] == ["fake-ups"] and only["asset_count"] is None and only["by_severity"]["critical"] == 1
    text = inspection.to_csv(inspection.view(report, None))
    assert text.startswith("﻿级别,类别,资产") and "fake-ups" in text


def test_inspection_temperature_and_capacity_follow_alert_rules(session: Session) -> None:
    """有告警规则覆盖时按规则判断（超过阈值用规则级别，10% 以内记警告）；没有覆盖的仍按固定值。"""
    a = _asset(session, "fake-rack1", [Point("cpu.temp", 83.0, "°C"), Point("disk.used_pct:/", 85.0, "%"), Point("disk.used_pct:/data", 85.0, "%")])
    a.tags = ["fake-rack"]
    b = _asset(session, "fake-rack2", [Point("cpu.temp", 83.0, "°C")])
    session.add_all([
        AlertRule(name="fake-temp", metric_key="cpu.temp", threshold=90, recovery_threshold=85, severity="critical", tags=["fake-rack"]),
        AlertRule(name="fake-root", metric_key="disk.used_pct:/", threshold=80, recovery_threshold=75, severity="critical"),
        AlertRule(name="fake-off", metric_key="cpu.temp", threshold=50, recovery_threshold=45, severity="critical", enabled=False),
    ])
    session.commit()
    got = {(i["asset_name"], i["title"]): i["severity"] for i in inspection.inspect(session, NOW)[0]}
    assert got[("fake-rack1", "CPU 温度 83 ℃")] == "warning"  # 规则阈值 90，83 在 10% 以内
    assert got[("fake-rack1", "磁盘“/”已用 85%")] == "critical"  # 规则阈值 80（固定值只算警告）
    assert got[("fake-rack1", "磁盘“/data”已用 85%")] == "warning"  # 没有规则覆盖：固定值 80 / 90
    assert got[("fake-rack2", "CPU 温度 83 ℃")] == "critical"  # 规则只限 fake-rack 标签，这台按固定值；停用的规则不算
    assert b.name == "fake-rack2"


def test_csv_neutralizes_formulas() -> None:
    data = {"items": [{"severity": "warning", "category": "硬件", "asset_name": "=cmd|' /C calc'!A0", "site": "@x", "title": "+1",
                       "detail": "-2", "metric_key": "k", "value": 1}]}
    row = inspection.to_csv(data).splitlines()[1]
    assert "'=cmd" in row and ",'@x," in row and ",'+1,'-2," in row


def test_daily_report_is_due_once_per_local_day(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    assert not inspection.daily_due(session, 11, NOW)  # 本地 10 点，还没到 11 点
    assert inspection.daily_due(session, 8, NOW)
    inspection.generate(session, kind="daily", now=NOW)
    session.commit()
    assert not inspection.daily_due(session, 8, NOW + timedelta(hours=3))
    assert inspection.daily_due(session, 8, NOW + timedelta(days=1))
    assert not inspection.daily_due(session, -1, NOW + timedelta(days=1))
    session.add(InspectionReport(kind="manual", created_at=NOW - timedelta(days=100), items=[]))
    session.commit()
    assert inspection.prune(session, now=NOW) == 1


def _scoped_user(factory, name: str, asset_id: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, name, "viewer")
    with factory() as db:
        u = db.scalar(select(User).where(User.username == name))
        u.scope_all = False
        db.add(UserScopeAsset(user_id=u.id, asset_id=asset_id))
        db.commit()


def test_reports_api_and_metrics_export_follow_scope(client, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        mine = _asset(db, "fake-mine", [Point("cpu.temp", 85.0, "°C"), Point("disk.used_pct:/var", 50.0, "%")], at=datetime.now(UTC))
        other = _asset(db, "fake-secret", [Point("cpu.temp", 90.0, "°C")], at=datetime.now(UTC))
        db.commit()
        mine_id, other_id = mine.id, other.id
    add_user(factory, "fake-ops", "operator")
    _scoped_user(factory, "fake-scoped", mine_id)

    login(client, "fake-ops")
    created = client.post("/api/inspections")
    assert created.status_code == 201 and created.json()["by_severity"]["critical"] == 2
    rid = created.json()["id"]
    assert client.get("/api/inspections").json()[0]["affected_assets"] == 2
    csv_resp = client.get(f"/api/inspections/{rid}/export.csv")
    assert csv_resp.headers["content-disposition"].startswith('attachment; filename="inspection-') and "fake-secret" in csv_resp.text

    login(client, "fake-scoped")
    assert client.post("/api/inspections").status_code == 403  # 只读角色不能手动生成
    view = client.get(f"/api/inspections/{rid}").json()
    assert [i["asset_name"] for i in view["items"]] == ["fake-mine"] and "fake-secret" not in client.get(f"/api/inspections/{rid}/export.csv").text

    tok = client.post("/api/tokens", json={"name": "fake-prometheus"}).json()
    assert tok["token"].startswith("ys_") and "token" not in client.get("/api/tokens").json()[0]
    body = client.get("/metrics", headers={"Authorization": f"Bearer {tok['token']}"}).text
    assert f'yunshu_cpu_temp{{asset="fake-mine",asset_id="{mine_id}",item="",unit="°C"}} 85 ' in body
    assert 'yunshu_disk_used_pct{asset="fake-mine"' in body and 'item="/var"' in body
    assert str(other_id) not in body and "# TYPE yunshu_cpu_temp gauge" in body
    assert client.get("/metrics", headers={"Authorization": "Bearer ys_wrong"}).status_code == 401
    assert client.get("/metrics").status_code == 401
    client.delete(f"/api/tokens/{tok['id']}")
    assert client.get("/metrics", headers={"Authorization": f"Bearer {tok['token']}"}).status_code == 401


# ---- OIDC：伪造身份提供方 ----

ISSUER = "https://sso.example.invalid/realms/ops"
CLIENT = "yunshu-fake"


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FakeIdp:
    def __init__(self) -> None:
        self.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ec = ec.generate_private_key(ec.SECP256R1())
        n = self.rsa.public_key().public_numbers()
        e = self.ec.public_key().public_numbers()
        self.jwks = {"keys": [
            {"kty": "RSA", "kid": "r1", "n": _b64e(n.n.to_bytes(256, "big")), "e": _b64e(n.e.to_bytes(3, "big"))},
            {"kty": "EC", "kid": "e1", "crv": "P-256", "x": _b64e(e.x.to_bytes(32, "big")), "y": _b64e(e.y.to_bytes(32, "big"))},
        ]}
        self.next_claims: dict[str, Any] = {}
        self.posts: list[dict[str, list[str]]] = []

    def sign(self, claims: dict[str, Any], alg: str = "RS256", kid: str | None = None) -> str:
        head = _b64e(json.dumps({"alg": alg, "kid": kid or ("r1" if alg == "RS256" else "e1")}).encode())
        body = _b64e(json.dumps(claims).encode())
        data = f"{head}.{body}".encode()
        if alg == "RS256":
            sig = self.rsa.sign(data, padding.PKCS1v15(), hashes.SHA256())
        else:
            r, s = decode_dss_signature(self.ec.sign(data, ec.ECDSA(hashes.SHA256())))
            sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{head}.{body}.{_b64e(sig)}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth",
                                             "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/certs"})
        if request.url.path.endswith("/certs"):
            return httpx.Response(200, json=self.jwks)
        if request.url.path.endswith("/token"):
            self.posts.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"id_token": self.sign(self.next_claims)})
        return httpx.Response(404)


def claims(nonce: str, **extra: Any) -> dict[str, Any]:
    return {"iss": ISSUER, "aud": CLIENT, "sub": "fake-sub-1", "exp": time.time() + 300, "nonce": nonce,
            "preferred_username": "fake.sso", "name": "伪造 单点", "groups": ["ops"], **extra}


def config(**kw: Any) -> OidcConfig:
    return OidcConfig(issuer=ISSUER, client_id=CLIENT, client_secret="fake-client-secret", redirect_url="http://testserver/api/auth/oidc/callback",
                      admin_groups=frozenset({"admins"}), operator_groups=frozenset({"ops"}), **kw)


def test_config_from_env_requires_issuer_client_and_redirect() -> None:
    assert OidcConfig.from_env({}) is None
    cfg = OidcConfig.from_env({"YUNSHU_OIDC_ISSUER": ISSUER + "/", "YUNSHU_OIDC_CLIENT_ID": CLIENT, "YUNSHU_OIDC_REDIRECT_URL": "http://x/cb",
                               "YUNSHU_OIDC_ADMIN_GROUPS": "admins, root", "YUNSHU_OIDC_OPERATOR_GROUPS": "ops"})
    assert cfg is not None and cfg.issuer == ISSUER and cfg.role_for(["root"]) == "admin" and cfg.role_for(["ops"]) == "operator"
    assert cfg.role_for([]) == "viewer" and cfg.scope_all is False


@pytest.mark.parametrize("change,message", [
    ({"aud": "someone-else"}, "不是签发给本平台"), ({"iss": "https://evil.invalid"}, "签发方"),
    ({"exp": time.time() - 3600}, "过期"), ({"nonce": "other"}, "nonce"),
])
def test_id_token_claims_are_checked(change: dict[str, Any], message: str) -> None:
    idp = FakeIdp()
    p = Provider(config(), transport=httpx.MockTransport(idp.handler))
    p.discovery()
    assert p.verify(idp.sign(claims("n1")), "n1")["sub"] == "fake-sub-1"
    assert p.verify(idp.sign(claims("n1"), alg="ES256"), "n1")["sub"] == "fake-sub-1"
    with pytest.raises(OidcError, match=message):
        p.verify(idp.sign({**claims("n1"), **change}), "n1")


def test_id_token_signature_and_algorithm_are_enforced() -> None:
    idp = FakeIdp()
    p = Provider(config(), transport=httpx.MockTransport(idp.handler))
    p.discovery()
    good = idp.sign(claims("n1"))
    head, body, sig = good.split(".")
    forged = _b64e(json.dumps({**claims("n1"), "sub": "admin"}).encode())
    with pytest.raises(OidcError, match="签名无效"):
        p.verify(f"{head}.{forged}.{sig}", "n1")
    none_alg = _b64e(json.dumps({"alg": "none", "kid": "r1"}).encode())
    with pytest.raises(OidcError, match="不支持的签名算法"):
        p.verify(f"{none_alg}.{body}.", "n1")
    with pytest.raises(OidcError, match="找不到"):
        p.verify(idp.sign(claims("n1"), kid="unknown"), "n1")


def _sso(client, idp: FakeIdp, **claim_extra: Any) -> httpx.Response:  # type: ignore[no-untyped-def]
    start = client.get("/api/auth/oidc/start", follow_redirects=False)
    assert start.status_code == 303
    q = parse_qs(urlparse(start.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == [CLIENT]
    flow = json.loads(base64.urlsafe_b64decode(client.cookies.get("yunshu_oidc")))
    idp.next_claims = claims(flow["nonce"], **claim_extra)
    return client.get("/api/auth/oidc/callback", params={"code": "fake-code", "state": q["state"][0]}, follow_redirects=False)


def test_sso_flow_creates_user_syncs_role_and_blocks_password_login(client, factory) -> None:  # type: ignore[no-untyped-def]
    idp = FakeIdp()
    client.app.state.oidc_provider = Provider(config(), transport=httpx.MockTransport(idp.handler))
    assert client.get("/api/auth/oidc/status").json() == {"enabled": True, "label": "单点登录"}

    done = _sso(client, idp)
    assert done.status_code == 303 and done.headers["location"] == "/#/"
    assert idp.posts[0]["code_verifier"] and idp.posts[0]["client_secret"] == ["fake-client-secret"]
    me = client.get("/api/auth/me").json()["user"]
    assert me["username"] == "fake.sso" and me["role"] == "operator" and me["auth_source"] == "oidc" and me["scope_all"] is False

    client.cookies.clear()
    _sso(client, idp, groups=["admins"])  # 组变了：角色跟着同步，账号不重复创建
    with factory() as db:
        users = db.scalars(select(User).where(User.auth_source == "oidc")).all()
        assert len(users) == 1 and users[0].role == "admin"
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "fake.sso", "password": "!"}).status_code == 401

    # 回调的 state 与 Cookie 不一致：拒绝
    client.cookies.clear()
    client.get("/api/auth/oidc/start", follow_redirects=False)
    bad = client.get("/api/auth/oidc/callback", params={"code": "x", "state": "forged"}, follow_redirects=False)
    assert "sso_error" in bad.headers["location"] and client.get("/api/auth/me").status_code == 401


def test_sso_refuses_to_take_over_local_account(client, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "fake.sso", "admin")
    idp = FakeIdp()
    client.app.state.oidc_provider = Provider(config(), transport=httpx.MockTransport(idp.handler))
    done = _sso(client, idp)
    assert "sso_error" in done.headers["location"] and client.get("/api/auth/me").status_code == 401
    login(client, "fake.sso", FAKE_PASSWORD)  # 本地账号照常可用


def test_sso_disabled_by_default(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/api/auth/oidc/status").json() == {"enabled": False, "label": ""}
    assert "sso_error" in client.get("/api/auth/oidc/start", follow_redirects=False).headers["location"]
