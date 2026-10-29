"""4.4 P20b 每台 BMC 排队与访问频率上限：只用伪造的适配器、伪造的 BMC 与内存 / 临时数据库，不连接任何设备。"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import bmc_gate
from app.collect.runner import collect_asset
from app.crypto import CredentialCipher, KeyRing
from app.db import make_engine
from app.metrics import Point
from app.migrate import prepare
from app.models import Asset, CollectionStatus, ControlOperation, ManagementEndpoint
from tests.test_collect import FakeAdapter
from tests.test_control_power import FakeBmc, make_asset, power, run_worker, set_owner, setup  # noqa: F401  （setup 是 fixture）

NOW = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)


def bmc_asset(db: Session, *protocols: str, address: str = "192.0.2.70", profile: str = "dell-server") -> Asset:
    a = Asset(name=f"fake-{uuid.uuid4().hex[:6]}", form="physical", role="server", platform="iDRAC9", collection_profile=profile, lifecycle="active")
    for p in protocols:
        a.endpoints.append(ManagementEndpoint(address=address, protocol=p, purpose="monitoring"))
    db.add(a)
    db.commit()
    return a


# ---- 排队本身 ----


@pytest.mark.parametrize("db_backend", [False, True])
def test_queue_control_waits_for_collection_and_collection_defers_to_control(tmp_path: Path, db_backend: bool) -> None:
    engine = make_engine(f"sqlite:///{(tmp_path / 'fake.db').as_posix()}")
    prepare(engine, auto=True, backup_dir=tmp_path)
    if db_backend:
        # PostgreSQL 多 Worker 用的数据库租约：逻辑在 SQLite 上同样可以验证
        bmc_gate._backends[engine] = bmc_gate.DbBackend(engine)
    addr = ["192.0.2.71"]
    released = threading.Event()

    def collect_for(seconds: float) -> None:
        with bmc_gate.hold(engine, addr, "collect", wait=1):
            time.sleep(seconds)
        released.set()

    t = threading.Thread(target=collect_for, args=(0.6,))
    t.start()
    time.sleep(0.2)
    start = time.monotonic()
    with bmc_gate.hold(engine, ["192.0.2.71:623"], "control", wait=5):  # 端口不同也是同一台 BMC
        assert released.is_set() and time.monotonic() - start >= 0.2
        # 控制进行中：采集不等待，直接顺延
        with pytest.raises(bmc_gate.BmcBusy) as exc:
            with bmc_gate.hold(engine, addr, "collect", wait=5):
                pass
        assert exc.value.holder_kind == "control" and "控制操作" in str(exc.value)
    t.join()
    # 另一次采集占用：等待；等不到报“正在执行采集”
    with bmc_gate.hold(engine, addr, "collect", wait=1):
        with pytest.raises(bmc_gate.BmcBusy) as exc:
            with bmc_gate.hold(engine, addr, "control", wait=0.3):
                pass
        assert exc.value.holder_kind == "collect"
    with bmc_gate.hold(engine, addr, "control", wait=0.1):
        pass  # 都已释放
    with bmc_gate.hold(engine, ["fake-ssh-only"], "collect", wait=0):
        with bmc_gate.hold(engine, addr, "collect", wait=0):
            pass  # 不同地址互不影响
    engine.dispose()


@pytest.mark.parametrize("db_backend", [False, True])
def test_rate_limit_per_bmc_and_protocol(tmp_path: Path, monkeypatch, db_backend: bool) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine(f"sqlite:///{(tmp_path / 'fake.db').as_posix()}")
    prepare(engine, auto=True, backup_dir=tmp_path)
    if db_backend:
        bmc_gate._backends[engine] = bmc_gate.DbBackend(engine)
    a = "192.0.2.72"
    assert bmc_gate.allow(engine, a, "redfish", "collect", NOW) == ""
    assert bmc_gate.allow(engine, a, "redfish", "control", NOW + timedelta(seconds=5)) == ""  # 控制计入次数
    assert "上限（2 次）" in bmc_gate.allow(engine, a, "redfish", "collect", NOW + timedelta(seconds=10))
    assert bmc_gate.allow(engine, a, "redfish", "control", NOW + timedelta(seconds=11)) == ""  # 控制不受上限
    assert bmc_gate.allow(engine, a, "ipmi", "collect", NOW + timedelta(seconds=10)) == ""
    assert bmc_gate.allow(engine, a, "ipmi", "collect", NOW + timedelta(seconds=40))
    assert bmc_gate.allow(engine, a, "ipmi", "collect", NOW + timedelta(seconds=70)) == ""  # 55 秒以前的不算
    assert bmc_gate.allow(engine, "192.0.2.73", "ipmi", "collect", NOW + timedelta(seconds=40)) == ""  # 另一台 BMC
    assert all(bmc_gate.allow(engine, a, "snmp", "collect", NOW) == "" for _ in range(10))  # SNMP 不限
    monkeypatch.setenv("YUNSHU_BMC_RATE", "redfish=0,ipmi=3")
    assert bmc_gate.rate_limits() == {"redfish": 0, "ipmi": 3}
    assert all(bmc_gate.allow(engine, a, "redfish", "collect", NOW + timedelta(seconds=12)) == "" for _ in range(5))
    engine.dispose()


# ---- 采集 ----


def test_collection_is_deferred_while_control_holds_the_bmc(session: Session) -> None:
    cipher = CredentialCipher(KeyRing.generate())
    a = bmc_asset(session, "snmp", "redfish")
    adapters = {"snmp": FakeAdapter("snmp"), "redfish": FakeAdapter("redfish")}
    with bmc_gate.hold(session.get_bind(), ["192.0.2.70"], "control", wait=0):
        run = collect_asset(session, cipher, a.id, adapters, now=NOW)
    assert "控制操作" in run.deferred_reason and run.ok == [] and run.failed == []
    assert adapters["snmp"].contexts == [] and adapters["redfish"].contexts == []
    assert session.scalars(select(CollectionStatus).where(CollectionStatus.asset_id == a.id)).all() == []  # 状态不变
    run = collect_asset(session, cipher, a.id, adapters, now=NOW + timedelta(seconds=60))
    assert run.deferred_reason == "" and run.ok


def test_rate_limited_primary_postpones_instead_of_falling_back(session: Session) -> None:
    cipher = CredentialCipher(KeyRing.generate())
    a = bmc_asset(session, "redfish", "ipmi", profile="generic-bmc")  # 模板：Redfish 首选、IPMI 兜底
    engine = session.get_bind()
    for i in range(2):
        bmc_gate.allow(engine, "192.0.2.70", "redfish", "control", NOW - timedelta(seconds=10 + i))
    adapters = {"redfish": FakeAdapter("redfish"), "ipmi": FakeAdapter("ipmi")}
    # 计划模式会先探测（不受上限）；这里只看模板路径，关掉计划
    from app.collect import plan as plans

    original = plans.applies
    plans.applies = lambda *_a, **_k: False  # type: ignore[assignment]
    try:
        run = collect_asset(session, cipher, a.id, adapters, now=NOW)
    finally:
        plans.applies = original  # type: ignore[assignment]
    assert run.deferred == ["redfish"] and run.degraded is False and run.failed == []
    assert adapters["redfish"].contexts == [] and adapters["ipmi"].contexts == []  # 不改用 IPMI
    # 页面上点“立即采集”：不受上限（走查发现：刚定时采集过再点立即采集，结果什么也没做）
    plans.applies = lambda *_a, **_k: False  # type: ignore[assignment]
    try:
        run = collect_asset(session, cipher, a.id, adapters, now=NOW + timedelta(seconds=1), force=True)
    finally:
        plans.applies = original  # type: ignore[assignment]
    assert run.deferred == [] and run.ok == ["redfish"]


def test_planned_owner_over_limit_skips_its_items_without_degrading(session: Session) -> None:
    cipher = CredentialCipher(KeyRing.generate())
    a = bmc_asset(session, "snmp", "redfish", "ipmi")
    adapters = {"snmp": FakeAdapter("snmp", points=[Point("cpu.temp", 40.0, "°C"), Point("fan.speed:1", 5000.0, "rpm")]),
                "redfish": FakeAdapter("redfish", points=[Point("cpu.temp", 41.0, "°C"), Point("raid.health:VD0", 1.0)]),
                "ipmi": FakeAdapter("ipmi", points=[Point("cpu.temp", 42.0, "°C")])}
    first = collect_asset(session, cipher, a.id, adapters, now=NOW)  # 探测：三个协议都读一次，不受上限
    assert first.probed and sorted(first.ok) == ["ipmi", "redfish", "snmp"]
    session.commit()
    engine = session.get_bind()
    for i in range(2):
        bmc_gate.allow(engine, "192.0.2.70", "redfish", "control", NOW + timedelta(seconds=590 + i))
    later = NOW + timedelta(seconds=600)  # 低频周期：硬盘 / 阵列由 Redfish 负责
    before = len(adapters["ipmi"].contexts)
    run = collect_asset(session, cipher, a.id, adapters, now=later)
    assert run.deferred == ["redfish"] and run.degraded is False and "snmp" in run.ok
    assert len(adapters["ipmi"].contexts) == before  # 没有把 Redfish 的数据交给 IPMI


# ---- 控制 ----


def test_control_waits_for_collection_then_gives_up_without_sending(app_factory, factory, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher, address="192.0.2.74").id
    engine = app_factory().app.state.engine
    bmc = FakeBmc("off")
    monkeypatch.setattr(bmc_gate, "CONTROL_WAIT", 0.3)
    with bmc_gate.hold(engine, ["192.0.2.74"], "collect", wait=0):
        assert power(op, [aid], "on").status_code == 202
        assert run_worker(app_factory, factory, bmc).run_once() == "succeeded"
    assert bmc.calls == []
    with factory() as db:
        o = db.scalar(select(ControlOperation).where(ControlOperation.asset_id == aid))
        assert o.status == "rejected" and "正在执行采集" in o.message and "没有发送任何命令" in o.message
    # 采集结束后再发：正常执行
    assert power(op, [aid], "on").status_code == 202
    assert run_worker(app_factory, factory, bmc).run_once() == "succeeded" and "set:on" in bmc.calls


def test_information_preview_returns_409_while_bmc_busy(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    from tests.api_helpers import add_user, login

    add_user(factory, "ada", "admin")
    admin = login(app_factory(), "ada")
    with factory() as db:
        aid = make_asset(db, app_factory.cipher, address="192.0.2.75").id
        ep = db.scalar(select(ManagementEndpoint).where(ManagementEndpoint.asset_id == aid))
    engine = app_factory().app.state.engine
    original = bmc_gate.PROBE_WAIT
    bmc_gate.PROBE_WAIT = 0.2
    try:
        with bmc_gate.hold(engine, ["192.0.2.75"], "control", wait=0):
            r = admin.post("/api/asset-information/preview", json={"endpoint_id": str(ep.id)})
    finally:
        bmc_gate.PROBE_WAIT = original
    assert r.status_code == 409 and "控制操作" in r.json()["detail"]
