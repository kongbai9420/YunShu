"""虚拟机 / 容器电源控制（4.2 P17）。

通过发现这台虚拟机的虚拟化平台端点下命令，不借用宿主 IPMI：
- PVE：POST /nodes/{节点}/qemu|lxc/{vmid}/status/start|shutdown|stop|reboot，返回任务编号（UPID），
  轮询 /nodes/{节点}/tasks/{UPID}/status 直到结束；API 令牌需要 VM.PowerMgmt 权限。
- ESXi / vCenter：pyVmomi 的 PowerOnVM_Task / ShutdownGuest / PowerOffVM_Task / RebootGuest；
  正常关机与重启需要虚拟机里装有 VMware Tools，平台不会自动改成强制断电。
规则与 P5 相同：命令发出前失败记为“失败”；发出后超时或连接中断记为“结果不确定”，不重试；
执行后再查一次电源状态，读到预期状态才算“已查询确认”。默认关闭（YUNSHU_GUEST_POWER=1 打开）。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.collect.base import AdapterError, EndpointContext
from app.collect.redfish import cert_fingerprint
from app.control.base import ControlError

GUEST_TASK = "control.guest_power"
LINKED_POWER_TASK = "control.power_linked"
DEFAULT_SHUTDOWN_TIMEOUT = 300


def guest_power_enabled() -> bool:
    return os.environ.get("YUNSHU_GUEST_POWER", "0") == "1"


@dataclass(frozen=True)
class GuestAction:
    key: str
    label: str
    high_risk: bool
    expect: str | None  # 执行后预期电源状态；None 表示无法从电源状态确认（重启）


GUEST_ACTIONS: dict[str, GuestAction] = {
    a.key: a for a in (
        GuestAction("on", "开机", False, "on"),
        GuestAction("shutdown", "正常关机", True, "off"),
        GuestAction("off", "强制断电", True, "off"),
        GuestAction("reboot", "重启", True, None),
    )
}


@dataclass(frozen=True)
class GuestRef:
    platform: str  # pve / vmware
    kind: str  # vm / container
    external_id: str
    node: str = ""


class GuestClient(Protocol):
    def state(self, ref: GuestRef) -> str:
        """返回 on / off / unknown。"""
        ...

    def act(self, ref: GuestRef, action: str, timeout: float) -> None:
        """执行并等平台任务结束；失败抛 ControlError。"""
        ...

    def close(self) -> None: ...


GuestClientFactory = Callable[[EndpointContext, str], GuestClient]


# ---- PVE ----

PVE_COMMAND = {"on": "start", "shutdown": "shutdown", "off": "stop", "reboot": "reboot"}


class PveGuestClient:
    def __init__(self, ctx: EndpointContext, *, transport: httpx.BaseTransport | None = None,
                 fingerprint: Callable[[str, int, float], str] = cert_fingerprint, sleep: Callable[[float], None] = time.sleep,
                 poll_interval: float = 2.0) -> None:
        port = ctx.port or 8006
        if ctx.tls_verify:
            ctx.trust.check_tls(fingerprint(ctx.address, port, ctx.timeout))
        if not ctx.username or not ctx.secret:
            raise ControlError("unconfigured", "PVE 控制端点没有关联 API 令牌或用户名密码")
        self.sleep, self.poll = sleep, poll_interval
        self.http = httpx.Client(base_url=f"https://{ctx.address}:{port}/api2/json", verify=False, timeout=ctx.timeout, transport=transport)
        if ctx.credential_kind == "token":
            self.http.headers["Authorization"] = f"PVEAPIToken={ctx.username}={ctx.secret}"
            return
        try:
            r = self.http.post("/access/ticket", data={"username": ctx.username, "password": ctx.secret})
        except httpx.HTTPError as exc:
            self.http.close()
            raise ControlError("unreachable", f"无法连接 PVE：{exc}") from exc
        data = (r.json().get("data") or {}) if r.status_code < 400 else {}
        if not data.get("ticket"):
            self.http.close()
            raise ControlError("auth", "PVE 用户名或密码不正确（用户名需带 realm，如 root@pam）")
        self.http.cookies.set("PVEAuthCookie", data["ticket"])
        # 票据登录时写操作必须带 CSRF 令牌
        self.http.headers["CSRFPreventionToken"] = data.get("CSRFPreventionToken", "")

    @staticmethod
    def _path(ref: GuestRef) -> str:
        if not ref.node or not ref.external_id.isdigit():
            raise ControlError("unsupported", "缺少 PVE 节点或虚拟机编号，无法定位这台虚拟机")
        return f"/nodes/{quote(ref.node)}/{'lxc' if ref.kind == 'container' else 'qemu'}/{ref.external_id}"

    def _check(self, r: httpx.Response, *, sent: bool) -> Any:
        if r.status_code in (401, 403):
            raise ControlError("auth", "PVE 账号没有该虚拟机的电源管理权限（需要 VM.PowerMgmt）")
        if r.status_code >= 400:
            msg = (r.json().get("errors") or r.text) if "json" in r.headers.get("content-type", "") else r.text
            raise ControlError("protocol", f"PVE 拒绝了请求（HTTP {r.status_code}）：{str(msg)[:200]}")
        return r.json().get("data")

    def state(self, ref: GuestRef) -> str:
        try:
            data = self._check(self.http.get(f"{self._path(ref)}/status/current"), sent=False) or {}
        except httpx.HTTPError as exc:
            raise ControlError("unreachable", f"查询 PVE 虚拟机状态失败：{exc}") from exc
        return {"running": "on", "stopped": "off"}.get(str(data.get("status")), "unknown")

    def act(self, ref: GuestRef, action: str, timeout: float) -> None:
        path = f"{self._path(ref)}/status/{PVE_COMMAND[action]}"
        body = {"timeout": int(timeout)} if action == "shutdown" else {}
        try:
            r = self.http.post(path, data=body)
        except httpx.TimeoutException as exc:
            raise ControlError("timeout", "PVE 没有应答电源命令") from exc
        except httpx.HTTPError as exc:
            raise ControlError("timeout", f"发送电源命令时连接中断：{exc}") from exc
        upid = self._check(r, sent=True)
        if not upid:
            return
        deadline = time.monotonic() + timeout + 30
        task = f"/nodes/{quote(ref.node)}/tasks/{quote(str(upid), safe='')}/status"
        while True:
            try:
                st = self._check(self.http.get(task), sent=True) or {}
            except httpx.HTTPError as exc:
                raise ControlError("timeout", f"查询 PVE 任务进度失败：{exc}") from exc
            if st.get("status") == "stopped":
                if st.get("exitstatus") != "OK":
                    raise ControlError("protocol", f"PVE 任务结束但未成功：{st.get('exitstatus')}")
                return
            if time.monotonic() > deadline:
                raise ControlError("timeout", f"PVE 任务在 {int(timeout)} 秒内没有结束")
            self.sleep(self.poll)

    def close(self) -> None:
        self.http.close()


# ---- ESXi / vCenter ----

VSPHERE_STATE = {"poweredOn": "on", "poweredOff": "off", "suspended": "off"}


class VsphereGuestClient:
    def __init__(self, ctx: EndpointContext, *, fingerprint: Callable[[str, int, float], str] = cert_fingerprint,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        from app.collect.esxi import PyvmomiClient

        if ctx.tls_verify:
            ctx.trust.check_tls(fingerprint(ctx.address, ctx.port or 443, ctx.timeout))
        try:
            self.c = PyvmomiClient(ctx)
        except AdapterError as exc:
            raise ControlError("unreachable" if exc.kind != "auth" else "auth", str(exc)) from exc
        self.sleep = sleep

    def _vm(self, ref: GuestRef):  # type: ignore[no-untyped-def]
        vim, content = self.c.vim, self.c.content
        if ref.external_id.startswith("uuid:"):
            vm = content.searchIndex.FindByUuid(None, ref.external_id[5:], True, True)
        elif ref.external_id.startswith("moref:"):
            vm = vim.VirtualMachine(ref.external_id[6:], self.c.si._stub)
        else:
            vm = None
        if vm is None:
            raise ControlError("protocol", "在 vSphere 里找不到这台虚拟机（可能已删除或迁移到其他 vCenter）")
        return vm

    def state(self, ref: GuestRef) -> str:
        return VSPHERE_STATE.get(str(self._vm(ref).runtime.powerState), "unknown")

    def _wait_task(self, task, timeout: float) -> None:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + timeout
        while str(task.info.state) in ("queued", "running"):
            if time.monotonic() > deadline:
                raise ControlError("timeout", f"vSphere 任务在 {int(timeout)} 秒内没有结束")
            self.sleep(2)
        if str(task.info.state) != "success":
            raise ControlError("protocol", f"vSphere 任务失败：{getattr(task.info.error, 'msg', task.info.error)}")

    def act(self, ref: GuestRef, action: str, timeout: float) -> None:
        vm = self._vm(ref)
        try:
            if action == "on":
                self._wait_task(vm.PowerOnVM_Task(), timeout)
            elif action == "off":
                self._wait_task(vm.PowerOffVM_Task(), timeout)
            elif action == "reboot":
                vm.RebootGuest()
            else:
                vm.ShutdownGuest()
                deadline = time.monotonic() + timeout
                while VSPHERE_STATE.get(str(vm.runtime.powerState)) != "off":
                    if time.monotonic() > deadline:
                        raise ControlError("timeout", f"虚拟机在 {int(timeout)} 秒内没有完成正常关机")
                    self.sleep(3)
        except self.c.vim.fault.ToolsUnavailable as exc:
            raise ControlError("unsupported", "虚拟机里没有运行 VMware Tools，无法正常关机或重启；需要时请改用强制断电") from exc
        except self.c.vim.fault.InvalidPowerState as exc:
            raise ControlError("protocol", f"当前电源状态不允许该操作：{getattr(exc, 'msg', exc)}") from exc
        except self.c.vim.fault.NoPermission as exc:
            raise ControlError("auth", "vSphere 账号没有该虚拟机的电源操作权限") from exc

    def close(self) -> None:
        self.c.close()


def default_factory(ctx: EndpointContext, platform: str) -> GuestClient:
    return PveGuestClient(ctx) if platform == "pve" else VsphereGuestClient(ctx)


# ---- 执行 ----


@dataclass
class GuestOutcome:
    status: str  # verified / accepted / unconfirmed
    message: str
    observed: dict[str, Any] = field(default_factory=dict)


def execute_guest(ctx: EndpointContext, ref: GuestRef, action: GuestAction, *, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
                  factory: GuestClientFactory = default_factory) -> GuestOutcome:
    client = factory(ctx, ref.platform)
    try:
        before = client.state(ref)
        observed: dict[str, Any] = {"before": before}
        if action.expect is not None and before == action.expect:
            return GuestOutcome("verified", f"虚拟机已经是“{'开' if before == 'on' else '关'}”状态，没有发送命令", observed)
        client.act(ref, action.key, timeout)
        if action.expect is None:
            return GuestOutcome("unconfirmed", f"{action.label}命令已被平台接受；无法从电源状态确认已重启，请稍后查看", observed)
        try:
            now = client.state(ref)
        except ControlError as exc:
            observed["verify_error"] = str(exc)
            now = "unknown"
        observed["after"] = now
        if now == action.expect:
            return GuestOutcome("verified", f"{action.label}已完成，电源状态已确认为“{'开' if now == 'on' else '关'}”", observed)
        return GuestOutcome("accepted", f"{action.label}命令已被平台接受，但电源状态还不是预期的“{'开' if action.expect == 'on' else '关'}”，请稍后查看", observed)
    finally:
        client.close()
