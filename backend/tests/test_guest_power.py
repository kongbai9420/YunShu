"""4.2 P17 虚拟机开关机与宿主联动：只用伪造的 PVE 接口、伪造的虚拟化客户端和伪造 BMC，不连接任何设备。"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from app.collect.base import EndpointContext
from app.control.base import ControlError
from app.control.guest import GUEST_ACTIONS, GUEST_TASK, LINKED_POWER_TASK, GuestRef, PveGuestClient, execute_guest
from app.control.service import POWER_TASK
from app.models import Asset, AssetRelation, ControlOperation, ManagementEndpoint, Task, VirtualizationObservation, VirtualizationSource
from app.services.credentials import create_credential
from app.worker import LONG_TASKS
from tests.test_control_power import NO_WAIT, FakeBmc, make_asset, power, set_owner, setup  # noqa: F401  （setup 是 fixture）

FAKE_TOKEN = "fake-pve-token-0000"
REF = GuestRef(platform="pve", kind="vm", external_id="101", node="pve1")


# ---- PVE 客户端 ----


class FakePve:
    def __init__(self, *, status: str = "running", exit_status: str = "OK", deny: bool = False, hang: bool = False) -> None:
        self.status, self.exit_status, self.deny, self.hang = status, exit_status, deny, hang
        self.calls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api2/json")
        self.calls.append(f"{request.method} {path}")
        self.headers.append(dict(request.headers))
        if self.deny:
            return httpx.Response(403, json={"data": None})
        if path == "/access/ticket":
            return httpx.Response(200, json={"data": {"ticket": "PVE:fake-ticket", "CSRFPreventionToken": "fake-csrf"}})
        if path.endswith("/status/current"):
            return httpx.Response(200, json={"data": {"status": self.status}})
        if request.method == "POST" and "/status/" in path:
            if self.hang:
                raise httpx.ReadTimeout("fake timeout", request=request)
            self.body = parse_qs(request.content.decode())
            self.status = "running" if path.endswith("/start") or path.endswith("/reboot") else "stopped"
            return httpx.Response(200, json={"data": "UPID:pve1:0001:fake:qmshutdown:101:root@pam:"})
        if "/tasks/" in path:
            return httpx.Response(200, json={"data": {"status": "stopped", "exitstatus": self.exit_status}})
        return httpx.Response(404)


def pve_ctx(kind: str = "token") -> EndpointContext:
    return EndpointContext(address="192.0.2.80", port=8006, protocol="pve-api", username="ops@pve!power" if kind == "token" else "root@pam",
                           secret=FAKE_TOKEN, credential_kind=kind, tls_verify=False)


def pve_client(fake: FakePve, kind: str = "token") -> PveGuestClient:
    return PveGuestClient(pve_ctx(kind), transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)


def test_pve_shutdown_posts_command_waits_for_task_and_verifies() -> None:
    fake = FakePve()
    out = execute_guest(pve_ctx(), REF, GUEST_ACTIONS["shutdown"], timeout=120, factory=lambda c, p: pve_client(fake))
    assert out.status == "verified" and out.observed == {"before": "on", "after": "off"}
    assert "POST /nodes/pve1/qemu/101/status/shutdown" in fake.calls
    assert fake.body == {"timeout": ["120"]}
    assert any(c.startswith("GET /nodes/pve1/tasks/UPID") for c in fake.calls)
    assert fake.headers[0]["authorization"] == f"PVEAPIToken=ops@pve!power={FAKE_TOKEN}"


def test_pve_ticket_login_sends_csrf_token_on_commands() -> None:
    fake = FakePve(status="stopped")
    c = pve_client(fake, "password")
    c.act(GuestRef("pve", "container", "200", "pve1"), "on", 60)
    post = next(h for call, h in zip(fake.calls, fake.headers) if call.startswith("POST /nodes"))
    assert post["csrfpreventiontoken"] == "fake-csrf" and "PVEAuthCookie" in post["cookie"]
    assert "POST /nodes/pve1/lxc/200/status/start" in fake.calls


def test_already_in_target_state_sends_nothing() -> None:
    fake = FakePve(status="stopped")
    out = execute_guest(pve_ctx(), REF, GUEST_ACTIONS["off"], factory=lambda c, p: pve_client(fake))
    assert out.status == "verified" and not any(c.startswith("POST") for c in fake.calls)


@pytest.mark.parametrize("fake,kind", [(FakePve(deny=True), "auth"), (FakePve(exit_status="VM quit/powerdown failed"), "protocol"), (FakePve(hang=True), "timeout")])
def test_pve_errors_map_to_control_error_kinds(fake: FakePve, kind: str) -> None:
    with pytest.raises(ControlError) as exc:
        execute_guest(pve_ctx(), REF, GUEST_ACTIONS["shutdown"], factory=lambda c, p: pve_client(fake))
    assert exc.value.kind == kind


def test_reboot_cannot_be_confirmed_from_power_state() -> None:
    out = execute_guest(pve_ctx(), REF, GUEST_ACTIONS["reboot"], factory=lambda c, p: pve_client(FakePve()))
    assert out.status == "unconfirmed"


def test_guest_tasks_get_longer_time_limits() -> None:
    assert LONG_TASKS[GUEST_TASK] > 1800 and LONG_TASKS[LINKED_POWER_TASK] > LONG_TASKS[GUEST_TASK]


# ---- 门禁、执行与宿主联动 ----


class FakeGuests:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.power: dict[str, str] = {}
        self.fail = fail or set()
        self.calls: list[str] = []

    def factory(self, ctx, platform):  # type: ignore[no-untyped-def]
        assert platform == "pve" and ctx.secret == FAKE_TOKEN
        return _GuestClient(self)


class _GuestClient:
    def __init__(self, g: FakeGuests) -> None:
        self.g = g

    def state(self, ref: GuestRef) -> str:
        return self.g.power.get(ref.external_id, "on")

    def act(self, ref: GuestRef, action: str, timeout: float) -> None:
        self.g.calls.append(f"{action}:{ref.node}/{ref.external_id}")
        if ref.external_id in self.g.fail:
            raise ControlError("unsupported", "模拟：虚拟机拒绝正常关机")
        self.g.power[ref.external_id] = "on" if action in ("on", "reboot") else "off"

    def close(self) -> None:
        pass


def build(db, cipher, *, control_endpoint=True, lifecycle="active", guests=("101",)):  # type: ignore[no-untyped-def]
    """宿主（IPMI 端点）+ PVE 监控端点（可选再加同地址的控制端点）+ 若干已关联的虚拟机。"""
    host = make_asset(db, cipher, name="fake-pve1", address="192.0.2.31")
    monitor = ManagementEndpoint(asset_id=host.id, address="192.0.2.80", port=8006, protocol="pve-api", purpose="monitoring")
    db.add(monitor)
    if control_endpoint:
        cred = create_credential(db, cipher, alias=f"pve-{uuid.uuid4()}", kind="token", username="ops@pve!power", secret=FAKE_TOKEN)
        db.add(ManagementEndpoint(asset_id=host.id, address="192.0.2.80", port=8006, protocol="pve-api", purpose="control", credential_id=cred.id))
    db.flush()
    source = VirtualizationSource(endpoint_id=monitor.id, platform="pve", target="192.0.2.80:8006", complete=True)
    db.add(source)
    db.flush()
    ids = []
    for vmid in guests:
        g = Asset(name=f"fake-vm-{vmid}", form="vm", role="server", lifecycle=lifecycle)
        db.add(g)
        db.flush()
        db.add(VirtualizationObservation(source_id=source.id, kind="vm", external_id=vmid, name=g.name, host_external_id="pve1", power="on", asset_id=g.id))
        db.add(AssetRelation(source_asset_id=host.id, target_asset_id=g.id, kind="hosts", origin="manual"))
        ids.append(g.id)
    db.commit()
    return host.id, ids


def worker(app_factory, factory, guests: FakeGuests, bmc: FakeBmc):  # type: ignore[no-untyped-def]
    from app.control.service import make_guest_handler, make_power_handler
    from app.worker import Worker

    cipher = lambda: app_factory.cipher  # noqa: E731
    return Worker(factory, {GUEST_TASK: make_guest_handler(cipher, guests.factory),
                            POWER_TASK: make_power_handler(cipher, bmc.factory, **NO_WAIT),
                            LINKED_POWER_TASK: make_power_handler(cipher, bmc.factory, **NO_WAIT)})


def guest_power(client, ids, action, confirm="", key=None):  # type: ignore[no-untyped-def]
    return client.post("/api/control/guest-power", json={"asset_ids": [str(i) for i in ids], "action": action, "confirm": confirm,
                                                         "reason": "验收测试", "request_key": key or uuid.uuid4().hex, "timeout": 60})


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YUNSHU_GUEST_POWER", "1")
    monkeypatch.setattr("app.control.service.GUEST_POLL_SECONDS", 0.01)


def test_disabled_by_default_and_monitor_only_endpoint_is_refused(app_factory, factory, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        _, (vm,) = build(db, app_factory.cipher, control_endpoint=False)
    info = op.get(f"/api/control/assets/{vm}/guest-power").json()
    assert info["enabled"] is False and all(a["code"] == "disabled" for a in info["actions"])
    monkeypatch.setenv("YUNSHU_GUEST_POWER", "1")
    info = op.get(f"/api/control/assets/{vm}/guest-power").json()
    assert all(a["code"] == "no_endpoint" for a in info["actions"]) and "仅监控" in info["actions"][0]["reason"]
    assert info["source"]["platform"] == "pve"


def test_guest_shutdown_runs_through_platform_endpoint(app_factory, factory, setup, enabled) -> None:  # type: ignore[no-untyped-def]
    op, viewer = setup
    with factory() as db:
        set_owner(db, "new")
        _, (vm,) = build(db, app_factory.cipher)
    info = op.get(f"/api/control/assets/{vm}/guest-power").json()
    assert [a["key"] for a in info["actions"] if a["allowed"]] == ["on", "shutdown", "off", "reboot"]
    assert guest_power(viewer, [vm], "shutdown").status_code == 403
    assert guest_power(op, [vm], "shutdown", confirm="wrong").status_code == 422  # 高风险要输入确认语
    r = guest_power(op, [vm], "shutdown", confirm="fake-vm-101")
    assert r.status_code == 202 and r.json()["operations"][0]["status"] == "queued"
    guests, bmc = FakeGuests(), FakeBmc()
    assert worker(app_factory, factory, guests, bmc).run_once() == "succeeded"
    assert guests.calls == ["shutdown:pve1/101"] and bmc.calls == []  # 没有借用宿主 IPMI
    with factory() as db:
        o = db.scalar(select(ControlOperation).where(ControlOperation.asset_id == vm))
        assert o.status == "verified" and o.kind == "guest_power" and o.target_key.startswith("guest:")


def test_planned_guest_and_physical_assets_are_refused(app_factory, factory, setup, enabled) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        host, (vm,) = build(db, app_factory.cipher, lifecycle="planned")
    actions = op.get(f"/api/control/assets/{vm}/guest-power").json()["actions"]
    assert all(a["code"] == "lifecycle" for a in actions)
    assert all(a["code"] == "not_guest" for a in op.get(f"/api/control/assets/{host}/guest-power").json()["actions"])


def test_host_shutdown_waits_for_guests_then_powers_off(app_factory, factory, setup, enabled) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        host, vms = build(db, app_factory.cipher, guests=("101", "102"))
    hosted = op.get(f"/api/control/assets/{host}/hosted-guests").json()
    assert hosted["enabled"] and [g["name"] for g in hosted["guests"]] == ["fake-vm-101", "fake-vm-102"]
    r = op.post("/api/control/power", json={"asset_ids": [str(host)], "action": "soft", "confirm": "fake-pve1", "reason": "演练",
                                           "request_key": uuid.uuid4().hex, "guests_first": True, "guest_timeout": 60})
    assert r.status_code == 202 and r.json()["guest_batch_id"] and len(r.json()["guests"]) == 2
    guests, bmc = FakeGuests(), FakeBmc("on")
    w = worker(app_factory, factory, guests, bmc)
    while w.run_once():
        pass
    assert sorted(guests.calls) == ["shutdown:pve1/101", "shutdown:pve1/102"]
    assert bmc.calls[:2] == ["get", "set:shutdown"]  # 虚拟机都确认关闭后才关宿主
    with factory() as db:
        host_op = db.scalar(select(ControlOperation).where(ControlOperation.asset_id == host))
        assert host_op.status == "verified"
        assert db.scalar(select(Task.kind).where(Task.id == host_op.task_id)) == LINKED_POWER_TASK


def test_host_action_is_cancelled_when_a_guest_does_not_shut_down(app_factory, factory, setup, enabled) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        host, _ = build(db, app_factory.cipher, guests=("101", "102"))
    op.post("/api/control/power", json={"asset_ids": [str(host)], "action": "off", "confirm": "fake-pve1", "reason": "演练",
                                       "request_key": uuid.uuid4().hex, "guests_first": True, "guest_timeout": 60})
    guests, bmc = FakeGuests(fail={"102"}), FakeBmc("on")
    w = worker(app_factory, factory, guests, bmc)
    while w.run_once():
        pass
    assert bmc.calls == []  # 宿主一个命令都没发
    with factory() as db:
        host_op = db.scalar(select(ControlOperation).where(ControlOperation.asset_id == host))
        assert host_op.status == "rejected" and "fake-vm-102" in host_op.message and "没有发送任何命令" in host_op.message


def test_wrong_host_confirmation_shuts_down_nothing(app_factory, factory, setup, enabled) -> None:  # type: ignore[no-untyped-def]
    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        host, _ = build(db, app_factory.cipher)
    r = op.post("/api/control/power", json={"asset_ids": [str(host)], "action": "soft", "confirm": "wrong", "reason": "演练",
                                           "request_key": uuid.uuid4().hex, "guests_first": True})
    assert r.status_code == 422
    with factory() as db:
        assert db.scalar(select(ControlOperation.id)) is None  # 连虚拟机关机都没有排队


@pytest.mark.parametrize("problem", ["maintenance", "no_endpoint", "indeterminate"])
def test_host_that_cannot_be_controlled_shuts_down_no_guest(app_factory, factory, setup, enabled, problem) -> None:  # type: ignore[no-untyped-def]
    from app.control.base import target_key
    from app.models import ControlTarget

    op, _ = setup
    with factory() as db:
        set_owner(db, "new")
        host, _ = build(db, app_factory.cipher, guests=("101", "102"))
        a = db.get(Asset, host)
        ipmi = next(e for e in a.endpoints if e.protocol == "ipmi")
        if problem == "maintenance":
            a.lifecycle = "maintenance"
        elif problem == "no_endpoint":
            db.delete(ipmi)
        else:
            db.add(ControlTarget(key=target_key(ipmi.address), state="indeterminate"))
        db.commit()
    r = op.post("/api/control/power", json={"asset_ids": [str(host)], "action": "soft", "confirm": "fake-pve1", "reason": "演练",
                                           "request_key": uuid.uuid4().hex, "guests_first": True, "guest_timeout": 60})
    assert r.status_code == 422
    message = r.json()["detail"]["message"]
    assert "fake-pve1" in message and "没有关闭任何虚拟机" in message
    guests, bmc = FakeGuests(), FakeBmc("on")
    w = worker(app_factory, factory, guests, bmc)
    while w.run_once():
        pass
    assert guests.calls == [] and bmc.calls == []  # 修复前：宿主被拒绝，虚拟机仍被关闭
    with factory() as db:
        assert db.scalar(select(ControlOperation.id)) is None


# ---- ESXi / vCenter 客户端（伪造 pyVmomi 对象）----


class _Fault(Exception):
    msg = "fake"


class _Vim:
    class fault:  # noqa: N801 - 模仿 pyVmomi 的命名
        class ToolsUnavailable(_Fault): ...
        class InvalidPowerState(_Fault): ...
        class NoPermission(_Fault): ...

    VirtualMachine = None


class _Task:
    def __init__(self, ok: bool = True) -> None:
        self.info = type("Info", (), {"state": "success" if ok else "error", "error": type("E", (), {"msg": "fake task error"})()})()


class _Vm:
    def __init__(self, power: str = "poweredOn", tools: bool = True, lag: int = 0) -> None:
        self.runtime = type("R", (), {"powerState": power})()
        self.tools, self.lag, self.calls = tools, lag, []

    def PowerOnVM_Task(self):  # noqa: N802
        self.calls.append("on")
        self.runtime.powerState = "poweredOn"
        return _Task()

    def PowerOffVM_Task(self):  # noqa: N802
        self.calls.append("off")
        self.runtime.powerState = "poweredOff"
        return _Task()

    def ShutdownGuest(self):  # noqa: N802
        self.calls.append("shutdown")
        if not self.tools:
            raise _Vim.fault.ToolsUnavailable()
        if self.lag == 0:
            self.runtime.powerState = "poweredOff"

    def RebootGuest(self):  # noqa: N802
        self.calls.append("reboot")


def vsphere(vm: _Vm):  # type: ignore[no-untyped-def]
    from app.control.guest import VsphereGuestClient

    client = object.__new__(VsphereGuestClient)
    index = type("Idx", (), {"FindByUuid": staticmethod(lambda *a: vm)})()
    client.c = type("C", (), {"vim": _Vim, "content": type("Content", (), {"searchIndex": index})(), "close": lambda self: None})()

    def sleep(_s: float) -> None:
        vm.lag -= 1
        if vm.lag <= 0:
            vm.runtime.powerState = "poweredOff"

    client.sleep = sleep
    return client


VREF = GuestRef(platform="vmware", kind="vm", external_id="uuid:5012-fake")


def test_vsphere_shutdown_waits_until_off() -> None:
    vm = _Vm(lag=2)
    out = execute_guest(pve_ctx(), VREF, GUEST_ACTIONS["shutdown"], factory=lambda c, p: vsphere(vm))
    assert out.status == "verified" and vm.calls == ["shutdown"]


def test_vsphere_without_tools_is_refused_not_forced_off() -> None:
    vm = _Vm(tools=False)
    with pytest.raises(ControlError) as exc:
        execute_guest(pve_ctx(), VREF, GUEST_ACTIONS["shutdown"], factory=lambda c, p: vsphere(vm))
    assert exc.value.kind == "unsupported" and "VMware Tools" in str(exc.value)
    assert vm.calls == ["shutdown"]  # 没有自动改成强制断电


def test_vsphere_power_on_and_off_use_tasks() -> None:
    vm = _Vm(power="poweredOff")
    assert execute_guest(pve_ctx(), VREF, GUEST_ACTIONS["on"], factory=lambda c, p: vsphere(vm)).status == "verified"
    assert execute_guest(pve_ctx(), VREF, GUEST_ACTIONS["off"], factory=lambda c, p: vsphere(vm)).status == "verified"
    assert vm.calls == ["on", "off"]
