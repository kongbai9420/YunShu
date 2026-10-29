from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metrics, queue
from app.collect.base import AdapterError, CollectResult, EndpointContext, ProbeResult, infer_profile
from app.collect.reach import ReachAdapter
from app.collect.runner import collect_asset, enqueue_due, prune_tasks
from app.crypto import CredentialCipher, KeyRing
from app.metrics import Point
from app.models import Asset, CollectionStatus, ManagementEndpoint, Task
from app.services.credentials import create_credential
from tests.api_helpers import add_user, login

NOW = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
FAKE_SECRET = "fake-collect-secret-1"


class FakeAdapter:
    def __init__(self, protocol: str, *, fail: str | None = None, points: list[Point] | None = None, fingerprint: str = "") -> None:
        self.protocol = protocol
        self.fail = fail
        self.points = points if points is not None else [Point(f"{protocol}.value", 1.0, "x")]
        self.fingerprint = fingerprint
        self.contexts: list[EndpointContext] = []

    def capabilities(self) -> set[str]:
        return {"cpu"}

    def probe(self, ctx: EndpointContext) -> ProbeResult:
        return ProbeResult(ok=True)

    def discover(self, ctx: EndpointContext) -> dict[str, Any]:
        return {}

    def collect(self, ctx: EndpointContext) -> CollectResult:
        self.contexts.append(ctx)
        if self.fingerprint:
            ctx.trust.check_tls(self.fingerprint)
        if self.fail:
            raise AdapterError(self.fail, f"{self.protocol} 模拟失败")
        return CollectResult(points=[Point(p.key, p.value, p.unit, p.quality) for p in self.points], facts={"hostname": "fake"})


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(KeyRing.generate())


def make_asset(db: Session, *protocols: str, platform: str = "", role: str = "server", profile: str = "") -> Asset:
    a = Asset(name="甲", form="physical", role=role, platform=platform, collection_profile=profile)
    for i, p in enumerate(protocols):
        a.endpoints.append(ManagementEndpoint(address=f"192.0.2.{10 + i}", protocol=p, purpose="monitoring"))
    db.add(a)
    db.commit()
    return a


def statuses(db: Session, asset: Asset) -> dict[str, str]:
    return {s.protocol: s.state for s in db.scalars(select(CollectionStatus).where(CollectionStatus.asset_id == asset.id))}


def test_infer_profile() -> None:
    assert infer_profile("dell-idrac", "server", {"ipmi"}) == "dell-server"
    assert infer_profile("ubuntu", "server", {"ssh"}) == "linux"
    assert infer_profile("", "switch", {"snmp"}) == "snmp-switch"
    assert infer_profile("proxmox", "server", {"ssh"}) == "pve"
    assert infer_profile("", "server", set()) is None


def test_primary_success_skips_fallback(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "ssh", "snmp", platform="ubuntu")
    ssh, snmp = FakeAdapter("ssh"), FakeAdapter("snmp")
    run = collect_asset(session, cipher, a.id, {"ssh": ssh, "snmp": snmp}, now=NOW)
    session.commit()
    assert run.ok == ["snmp"] and run.skipped == ["ssh"] and not run.degraded
    assert ssh.contexts == []
    assert statuses(session, a) == {"snmp": "ok", "ssh": "skipped"}


def test_fallback_when_primary_fails_is_marked(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "snmp", "ssh", role="switch", profile="linux")  # SNMP 首选，SSH 备用
    snmp = FakeAdapter("snmp", fail="unreachable")
    ssh = FakeAdapter("ssh", points=[Point("cpu.load", 12.0, "%")])
    run = collect_asset(session, cipher, a.id, {"ssh": ssh, "snmp": snmp}, now=NOW)
    session.commit()
    assert run.degraded and run.failed == ["snmp"] and run.ok == ["ssh"]
    latest = {v.key: v for v in metrics.latest(session, a.id, now=NOW)}
    assert "降级：SNMP 不可用，改用 SSH" in latest["cpu.load"].message
    assert statuses(session, a) == {"snmp": "failed", "ssh": "ok"}


def test_failure_marks_known_metrics_failed_not_zero(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "ipmi", profile="generic-bmc")
    collect_asset(session, cipher, a.id, {"ipmi": FakeAdapter("ipmi", points=[Point("cpu.temp", 55.0, "°C")])}, now=NOW)
    collect_asset(session, cipher, a.id, {"ipmi": FakeAdapter("ipmi", fail="timeout")}, now=NOW + timedelta(minutes=1))
    session.commit()
    v = {x.key: x for x in metrics.latest(session, a.id, now=NOW + timedelta(minutes=1))}["cpu.temp"]
    assert v.quality == "failed" and v.value is None and v.last_valid_value == 55.0
    st = session.scalar(select(CollectionStatus).where(CollectionStatus.asset_id == a.id))
    assert st.consecutive_failures == 1 and st.error_kind == "timeout"


def test_missing_credential_is_unconfigured_not_failed(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "ipmi", profile="generic-bmc")
    adapter = FakeAdapter("ipmi", points=[Point("cpu.temp", 55.0, "°C")])
    collect_asset(session, cipher, a.id, {"ipmi": adapter}, now=NOW)
    adapter.fail = "unconfigured"
    run = collect_asset(session, cipher, a.id, {"ipmi": adapter}, now=NOW + timedelta(minutes=1))
    assert run.unconfigured == ["ipmi"] and run.failed == []
    assert statuses(session, a) == {"ipmi": "unconfigured"}
    v = {x.key: x for x in metrics.latest(session, a.id, now=NOW + timedelta(minutes=1))}["cpu.temp"]
    assert v.quality == "unconfigured" and v.value is None and "待配置凭据" in v.message
    # 配好凭据后下一轮立即重试，不进入失败冷却
    adapter.fail = None
    run = collect_asset(session, cipher, a.id, {"ipmi": adapter}, now=NOW + timedelta(minutes=2))
    assert run.ok == ["ipmi"] and not run.cooling_down


def test_failed_primary_cools_down_then_recovers(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "snmp", "ssh", platform="ubuntu")
    snmp, ssh = FakeAdapter("snmp", fail="unreachable"), FakeAdapter("ssh")
    collect_asset(session, cipher, a.id, {"snmp": snmp, "ssh": ssh}, now=NOW)
    run = collect_asset(session, cipher, a.id, {"snmp": snmp, "ssh": ssh}, now=NOW + timedelta(seconds=60))
    assert run.cooling_down == ["snmp"] and run.degraded and len(snmp.contexts) == 1
    snmp.fail = None
    run = collect_asset(session, cipher, a.id, {"snmp": snmp, "ssh": ssh}, now=NOW + timedelta(seconds=121))
    assert not run.degraded and run.ok == ["snmp"] and len(snmp.contexts) == 2


def test_reach_does_not_decrypt_credentials_and_uses_endpoint_protocol(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "ssh", profile="reachability")
    cred = create_credential(session, cipher, alias="fake-reach-credential", kind="password", secret=FAKE_SECRET)
    session.flush()
    a.endpoints[0].credential_id = cred.id
    reach = FakeAdapter("reach")
    collect_asset(session, CredentialCipher(KeyRing.generate()), a.id, {"reach": reach}, now=NOW)
    assert reach.contexts[0].options["endpoint_protocol"] == "ssh"
    assert reach.contexts[0].secret == ""
    a.endpoints[0].purpose = "control"
    session.commit()
    assert collect_asset(session, cipher, a.id, {"reach": reach}, now=NOW).ok == []


def test_explicit_collection_bypasses_primary_cooldown(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "snmp", "ssh", platform="ubuntu")
    snmp, ssh = FakeAdapter("snmp", fail="unreachable"), FakeAdapter("ssh")
    collect_asset(session, cipher, a.id, {"snmp": snmp, "ssh": ssh}, now=NOW)
    snmp.fail = None
    run = collect_asset(session, cipher, a.id, {"snmp": snmp, "ssh": ssh}, now=NOW + timedelta(seconds=10), force=True)
    assert run.ok == ["snmp"] and not run.degraded and len(snmp.contexts) == 2


def test_supplement_always_runs(session: Session, cipher: CredentialCipher) -> None:
    # 4.4 P20a 起服务器 BMC 有两个以上协议时按采集计划执行：首次采集先把每个协议都探测一遍
    a = make_asset(session, "ipmi", "redfish", platform="dell-idrac")
    ipmi, redfish = FakeAdapter("ipmi"), FakeAdapter("redfish", points=[Point("disk.health:0", 1.0)])
    run = collect_asset(session, cipher, a.id, {"ipmi": ipmi, "redfish": redfish}, now=NOW)
    assert sorted(run.ok) == ["ipmi", "redfish"] and run.probed == "首次采集"


def test_credentials_passed_in_memory_only(session: Session, cipher: CredentialCipher) -> None:
    cred = create_credential(session, cipher, alias="c", kind="password", secret=FAKE_SECRET, username="u")
    session.flush()
    a = make_asset(session, "ssh", platform="ubuntu")
    a.endpoints[0].credential_id = cred.id
    session.commit()
    ssh = FakeAdapter("ssh")
    collect_asset(session, cipher, a.id, {"ssh": ssh}, now=NOW)
    session.commit()
    ctx = ssh.contexts[0]
    assert ctx.secret == FAKE_SECRET and ctx.username == "u"
    assert FAKE_SECRET not in repr(ctx)
    st = session.scalar(select(CollectionStatus))
    assert FAKE_SECRET not in str(st.message) + str(st.facts)


def test_trust_on_first_use_then_mismatch(session: Session, cipher: CredentialCipher) -> None:
    a = make_asset(session, "redfish", profile="dell-server")  # 只有 redfish（补充）也会执行
    collect_asset(session, cipher, a.id, {"redfish": FakeAdapter("redfish", fingerprint="AA:BB")}, now=NOW)
    session.commit()
    ep = session.get(ManagementEndpoint, a.endpoints[0].id)
    assert ep.tls_fingerprint == "AA:BB" and ep.trust_pinned_at is not None
    run = collect_asset(session, cipher, a.id, {"redfish": FakeAdapter("redfish", fingerprint="CC:DD")}, now=NOW + timedelta(minutes=1))
    session.commit()
    assert run.failed == ["redfish"]
    assert statuses(session, a) == {"redfish": "untrusted"}
    assert session.get(ManagementEndpoint, ep.id).tls_fingerprint == "AA:BB"  # 不会被新指纹覆盖


def test_adapter_crash_is_isolated(session: Session, cipher: CredentialCipher) -> None:
    class Boom(FakeAdapter):
        def collect(self, ctx):  # type: ignore[no-untyped-def]
            raise ValueError("解析失败")

    a = make_asset(session, "ipmi", "redfish", platform="dell-idrac")
    run = collect_asset(session, cipher, a.id, {"ipmi": Boom("ipmi"), "redfish": FakeAdapter("redfish")}, now=NOW)
    assert run.failed == ["ipmi"] and run.ok == ["redfish"]


def test_enqueue_due_is_idempotent_per_slot(session: Session) -> None:
    make_asset(session, "ssh", platform="ubuntu")
    retired = make_asset(session, "ssh", platform="ubuntu")
    retired.lifecycle = "retired"
    make_asset(session)  # 没有端点：不采集
    session.commit()
    assert enqueue_due(session, now=NOW) == 1
    assert enqueue_due(session, now=NOW + timedelta(seconds=20)) == 0  # 同一周期
    assert enqueue_due(session, now=NOW + timedelta(seconds=61)) == 1
    session.commit()
    assert session.query(Task).count() == 2


def test_prune_tasks(session: Session) -> None:
    for st, age in (("succeeded", 2), ("succeeded", 0), ("failed", 3), ("failed", 8)):
        session.add(Task(kind="collect.asset", status=st, finished_at=NOW - timedelta(days=age)))
    session.commit()
    assert prune_tasks(session, now=NOW) == 2


def test_reach_adapter() -> None:
    class Sock:
        def close(self) -> None:
            pass

    ok = ReachAdapter(connect=lambda addr, timeout: Sock()).collect(EndpointContext("192.0.2.1", 443, "reach"))
    assert {p.key for p in ok.points} == {"reach.up", "reach.rtt_ms"}

    def refuse(addr, timeout):  # type: ignore[no-untyped-def]
        raise ConnectionRefusedError(111, "Connection refused")

    with pytest.raises(AdapterError) as e:
        ReachAdapter(connect=refuse).collect(EndpointContext("192.0.2.1", 443, "reach"))
    assert e.value.kind == "unreachable"

    def slow(addr, timeout):  # type: ignore[no-untyped-def]
        raise socket.timeout("timed out")

    with pytest.raises(AdapterError) as e:
        ReachAdapter(connect=slow).collect(EndpointContext("192.0.2.1", 443, "reach"))
    assert e.value.kind == "timeout"


def test_collect_task_via_worker(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    from app.tasks import build_handlers
    from app.worker import Worker

    with factory() as db:
        a = make_asset(db, "ssh", platform="ubuntu")
        aid = a.id
        queue.enqueue(db, "collect.asset", asset_id=aid, payload={"asset_id": str(aid)})
        db.commit()
    handlers = build_handlers(app_factory.settings, adapters={"ssh": FakeAdapter("ssh")})
    assert Worker(factory, handlers).run_once() == "succeeded"
    with factory() as db:
        t = db.scalar(select(Task))
        assert t.result["ok"] == ["ssh"] and t.result["profile"] == "linux"


def test_collection_api(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    add_user(factory, "victor", "viewer")
    op, v = login(app_factory(), "olivia"), login(app_factory(), "victor")
    with factory() as db:
        a = make_asset(db, "ssh", platform="ubuntu")
        bare = make_asset(db)
        ep_id = a.endpoints[0].id
        a.endpoints[0].ssh_host_key = "ssh-ed25519 AAAAfake"
        db.commit()
        aid, bare_id = a.id, bare.id
    assert len(v.get("/api/collection/profiles").json()) >= 8
    info = v.get(f"/api/assets/{aid}/collection").json()
    assert info["profile"]["name"] == "linux" and info["profile"]["inferred"] is True
    assert v.post(f"/api/assets/{aid}/collect").status_code == 403
    assert op.post(f"/api/assets/{aid}/collect").status_code == 202
    assert op.post(f"/api/assets/{bare_id}/collect").status_code == 422
    assert op.post(f"/api/endpoints/{ep_id}/trust/reset").status_code == 204
    with factory() as db:
        assert db.get(ManagementEndpoint, ep_id).ssh_host_key == ""
