"""P5 电源控制：只用伪造的 BMC 客户端，不连接任何真实设备。"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import queue
from app.control.base import ControlError, target_key
from app.control.service import POWER_TASK, reconcile
from app.models import Asset, AuditEvent, ControlOperation, ControlSetting, ControlTarget, ManagementEndpoint, Task
from app.services.credentials import create_credential
from tests.api_helpers import add_user, login

FAKE_SECRET = "fake-bmc-secret-1"


class FakeBmc:
    """记录每一次调用；可以让 set_power 超时或报错，模拟软关机缓慢。"""

    def __init__(self, state: str = "on", *, fail: str | None = None, lag: int = 0) -> None:
        self.state = state
        self.fail = fail
        self.lag = lag
        self.calls: list[str] = []
        self.pending: str | None = None
        self.gate: threading.Event | None = None

    def factory(self, ctx):  # type: ignore[no-untyped-def]
        assert ctx.username == "root" and ctx.secret == FAKE_SECRET
        return _Client(self)


class _Client:
    def __init__(self, bmc: FakeBmc) -> None:
        self.bmc = bmc

    def power_state(self) -> str:
        b = self.bmc
        b.calls.append("get")
        if b.pending and b.lag <= 0:
            b.state, b.pending = b.pending, None
        b.lag -= 1
        return b.state

    def set_power(self, state: str) -> None:
        b = self.bmc
        b.calls.append(f"set:{state}")
        if b.gate is not None:
            b.gate.wait(5)
        if b.fail:
            raise ControlError(b.fail, "模拟失败")
        target = {"on": "on", "off": "off", "shutdown": "off", "reset": "on"}[state]
        if b.lag:
            b.pending = target
        else:
            b.state = target

    def close(self) -> None:
        self.bmc.calls.append("close")


NO_WAIT = {"verify_attempts": 3, "verify_interval": 0, "sleep": lambda s: None}


def make_asset(db, cipher, name="甲", address="192.0.2.30", *, purpose="management", credential=True, lifecycle="active"):  # type: ignore[no-untyped-def]
    a = Asset(name=name, form="physical", role="server", platform="dell-idrac", lifecycle=lifecycle)
    ep = ManagementEndpoint(address=address, port=623, protocol="ipmi", purpose=purpose)
    if credential:
        ep.credential_id = create_credential(db, cipher, alias=f"bmc-{uuid.uuid4()}", kind="password", username="root", secret=FAKE_SECRET).id
    a.endpoints.append(ep)
    db.add(a)
    db.commit()
    return a


def set_owner(db, owner: str) -> None:  # type: ignore[no-untyped-def]
    row = db.get(ControlSetting, "default") or ControlSetting(key="default", owner="legacy", revision=1, changed_by="", note="")
    row.owner = owner
    db.merge(row)
    db.commit()


def run_worker(app_factory, factory, bmc: FakeBmc, **kw):  # type: ignore[no-untyped-def]
    from app.control.service import make_power_handler
    from app.worker import Worker

    return Worker(factory, {POWER_TASK: make_power_handler(lambda: app_factory.cipher, bmc.factory, **NO_WAIT)}, **kw)


@pytest.fixture
def setup(app_factory, factory):  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    add_user(factory, "victor", "viewer")
    return login(app_factory(), "olivia"), login(app_factory(), "victor")


def power(client, ids, action, confirm="", key=None):  # type: ignore[no-untyped-def]
    return client.post("/api/control/power", json={"asset_ids": [str(i) for i in ids], "action": action, "confirm": confirm,
                                                   "reason": "验收测试", "request_key": key or uuid.uuid4().hex})


def test_target_key_normalization() -> None:
    assert target_key("192.0.2.5") == target_key("192.0.2.5:624") == "bmc:192.0.2.5"
    assert target_key("2001:0DB8::1") == target_key("[2001:db8::1]:623") == "bmc:[2001:db8::1]"
    assert target_key("BMC-01.Example.test.") == "bmc:bmc-01.example.test"
    assert target_key("") is None and target_key("bad host") is None


def test_legacy_owner_rejects_everything_with_zero_device_calls(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        a = make_asset(db, app_factory.cipher)
        aid = a.id
    info = op.get(f"/api/control/assets/{aid}/power").json()
    assert all(not x["allowed"] and x["code"] == "legacy_owner" for x in info["actions"])
    assert op.get("/api/control/status").json()["owner"] == "legacy"
    r = power(op, [aid], "on").json()
    assert r["operations"][0]["status"] == "rejected" and "旧控制器" in r["operations"][0]["message"]
    bmc = FakeBmc()
    assert run_worker(app_factory, factory, bmc).run_once() is None  # 没有入队
    assert bmc.calls == []


def test_power_on_verified_and_audited(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    op, viewer = setup
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    bmc = FakeBmc("off")
    assert power(viewer, [aid], "on").status_code == 403
    r = power(op, [aid], "on")
    assert r.status_code == 202 and r.json()["operations"][0]["status"] == "queued"
    assert run_worker(app_factory, factory, bmc).run_once() == "succeeded"
    assert bmc.calls == ["get", "set:on", "get", "close"], [(o["status"], o["message"]) for o in op.get(f"/api/control/operations?asset_id={aid}").json()]
    ops = op.get(f"/api/control/operations?asset_id={aid}").json()
    assert ops[0]["status"] == "verified" and ops[0]["observed"] == {"before": "off", "after": "on"}
    with factory() as db:
        actions = [e.action for e in db.scalars(select(AuditEvent).order_by(AuditEvent.occurred_at))]
        assert "control.power.request" in actions and "control.power.result" in actions
        assert FAKE_SECRET not in str([e.detail for e in db.scalars(select(AuditEvent))])


def test_high_risk_requires_typed_confirmation(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        a, b = make_asset(db, app_factory.cipher, "甲"), make_asset(db, app_factory.cipher, "乙", "192.0.2.31")
        ids = [a.id, b.id]
    bad = power(op, ids[:1], "off", confirm="")
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "confirm"
    assert power(op, ids, "off", confirm="甲").status_code == 422
    assert power(op, ids, "off", confirm="2 台").status_code == 202
    with factory() as db:
        assert db.scalar(select(Task).where(Task.kind == POWER_TASK).limit(1)).risk == "control"


def test_double_submit_and_busy_target_do_not_duplicate(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        a = make_asset(db, app_factory.cipher, "甲")
        # 同一 BMC 地址（不同端口）挂在另一台资产上
        b = make_asset(db, app_factory.cipher, "乙", "192.0.2.30:624")
        aid, bid = a.id, b.id
    first = power(op, [aid], "reset", confirm="甲", key="dup-key-0001").json()
    again = power(op, [aid], "reset", confirm="甲", key="dup-key-0001").json()
    assert again["duplicate"] is True and again["operations"][0]["id"] == first["operations"][0]["id"]
    other = power(op, [bid], "on").json()["operations"][0]
    assert other["status"] == "rejected" and "未完成的控制操作" in other["message"]
    with factory() as db:
        assert db.scalar(select(Task.id).where(Task.kind == POWER_TASK).offset(1)) is None  # 只有一个任务
    bmc = FakeBmc()
    w = run_worker(app_factory, factory, bmc)
    assert w.run_once() == "succeeded" and w.run_once() is None
    assert bmc.calls.count("set:reset") == 1
    assert op.get(f"/api/control/operations?asset_id={aid}").json()[0]["status"] == "unconfirmed"  # 重启无法从电源状态确认


def test_slow_soft_shutdown_is_accepted_not_verified(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    power(op, [aid], "soft", confirm="甲")
    bmc = FakeBmc("on", lag=10)
    run_worker(app_factory, factory, bmc).run_once()
    o = op.get(f"/api/control/operations?asset_id={aid}").json()[0]
    assert o["status"] == "accepted" and "还没有变成预期" in o["message"]


def test_failure_is_failed_and_timeout_is_indeterminate_never_retried(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
        bid = make_asset(db, app_factory.cipher, "乙", "192.0.2.31").id
    power(op, [aid], "on")
    run_worker(app_factory, factory, FakeBmc("off", fail="protocol")).run_once()
    assert op.get(f"/api/control/operations?asset_id={aid}").json()[0]["status"] == "failed"
    # 失败不会把设备锁住
    assert power(op, [aid], "on").json()["operations"][0]["status"] == "queued"
    run_worker(app_factory, factory, FakeBmc("off")).run_once()

    power(op, [bid], "off", confirm="乙")
    bmc = FakeBmc("on", fail="timeout")
    w = run_worker(app_factory, factory, bmc)
    w.run_once()
    assert w.run_once() is None and bmc.calls.count("set:off") == 1  # 不重试
    o = op.get(f"/api/control/operations?asset_id={bid}").json()[0]
    assert o["status"] == "indeterminate" and "不会自动重试" in o["message"]
    # 设备进入“结果不确定”：新的控制被拒绝，直到确认解除
    blocked = power(op, [bid], "on").json()["operations"][0]
    assert blocked["status"] == "rejected" and "结果不确定" in blocked["message"]
    key = op.get("/api/control/status").json()["indeterminate_targets"][0]["key"]
    assert op.post("/api/control/targets/acknowledge", json={"key": key, "note": " "}).status_code == 422
    assert op.post("/api/control/targets/acknowledge", json={"key": key, "note": "已登录 BMC 确认电源已关闭"}).status_code == 200
    assert power(op, [bid], "on").json()["operations"][0]["status"] == "queued"


def test_worker_timeout_marks_indeterminate_and_blocks_target(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    power(op, [aid], "off", confirm="甲")
    bmc = FakeBmc("on")
    bmc.gate = threading.Event()  # set_power 卡住，模拟 BMC 不应答
    w = run_worker(app_factory, factory, bmc, timeout_seconds=0.3, lease_seconds=3)
    assert w.run_once() == "indeterminate"
    with factory() as db:
        assert reconcile(db) == 1
        db.commit()
    o = op.get(f"/api/control/operations?asset_id={aid}").json()[0]
    assert o["status"] == "indeterminate"
    bmc.gate.set()  # 卡住的线程结束后也不能覆盖“不确定”
    import time

    for _ in range(50):
        with factory() as db:
            t = db.get(ControlTarget, "bmc:192.0.2.30")
            if t.busy_operation_id is None:
                break
        time.sleep(0.05)
    assert op.get(f"/api/control/operations?asset_id={aid}").json()[0]["status"] == "indeterminate"
    assert power(op, [aid], "on").json()["operations"][0]["status"] == "rejected"


def test_gate_rechecked_at_execution(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    power(op, [aid], "on")
    with factory() as db:
        set_owner(db, "legacy")  # 排队期间切回旧控制器
    bmc = FakeBmc("off")
    run_worker(app_factory, factory, bmc).run_once()
    o = op.get(f"/api/control/operations?asset_id={aid}").json()[0]
    assert o["status"] == "rejected" and bmc.calls == []


def test_endpoint_and_lifecycle_rules(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        mon = make_asset(db, app_factory.cipher, "监控", "192.0.2.40", purpose="monitoring").id
        nocred = make_asset(db, app_factory.cipher, "无凭据", "192.0.2.41", credential=False).id
        retired = make_asset(db, app_factory.cipher, "退役", "192.0.2.42", lifecycle="retired").id
        maint = make_asset(db, app_factory.cipher, "维护", "192.0.2.43", lifecycle="maintenance").id  # 旧版停用的设备
    codes = {}
    for aid in (mon, nocred, retired, maint):
        info = op.get(f"/api/control/assets/{aid}/power").json()
        codes[aid] = info["actions"][0]["code"]
    assert codes == {mon: "no_endpoint", nocred: "unconfigured", retired: "lifecycle", maint: "lifecycle"}
    r = power(op, [mon, nocred, retired], "on").json()
    assert {o["status"] for o in r["operations"]} == {"rejected"}
    assert len(power(op, [uuid.uuid4()], "on").json()["detail"]["message"]) > 0


def test_batch_limit_and_unknown_action(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    ids = [uuid.uuid4() for _ in range(21)]
    assert power(op, ids, "on").status_code == 422
    with factory() as db:
        aid = make_asset(db, app_factory.cipher).id
    r = power(op, [aid], "cycle")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_action"


def test_reconcile_crashed_worker(app_factory, factory, setup) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        aid = make_asset(db, app_factory.cipher).id
    power(op, [aid], "on")
    with factory() as db:
        lease = queue.claim(db, "crashed-worker", kinds=(POWER_TASK,), lease_seconds=1)
        o = db.scalar(select(ControlOperation))
        o.status, o.started_at = "running", datetime.now(UTC)
        db.commit()
        queue.reap_expired(db, now=datetime.now(UTC) + timedelta(seconds=5))
        assert reconcile(db) == 1
        db.commit()
        assert lease is not None
    assert op.get(f"/api/control/operations?asset_id={aid}").json()[0]["status"] == "indeterminate"
