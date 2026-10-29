"""IPMI 电源控制：开机、正常软关机、硬件重启、强制断电；状态查询只读。

只用 IPMI 标准的 chassis power 命令，不发 OEM raw 命令。登录失败（还没发控制命令）记为失败；
控制命令发出后超时记为“结果不确定”，不重试、不换协议。
发送成功只代表 BMC 接受了指令；之后再读一次电源状态，读到预期状态才算“已确认”。
硬件重启无法从电源状态看出是否真的重启过，只能标为“未确认”。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.collect.base import EndpointContext
from app.control.base import ControlError


@dataclass(frozen=True)
class PowerAction:
    key: str
    label: str
    high_risk: bool
    # 执行后预期的电源状态；None 表示无法从电源状态确认（如硬件重启）
    expect: str | None
    pyghmi_state: str


ACTIONS: dict[str, PowerAction] = {
    a.key: a
    for a in (
        PowerAction("on", "开机", False, "on", "on"),
        PowerAction("soft", "正常软关机", True, "off", "shutdown"),
        PowerAction("reset", "硬件重启", True, None, "reset"),
        PowerAction("off", "强制断电", True, "off", "off"),
    )
}


class PowerClient(Protocol):
    def power_state(self) -> str:
        """返回 "on" / "off"。"""
        ...

    def set_power(self, state: str) -> None: ...

    def close(self) -> None: ...


PowerClientFactory = Callable[[EndpointContext], PowerClient]


def _is_timeout(exc: Exception) -> bool:
    low = str(exc).lower()
    return "timeout" in low or "timed out" in low or "no response" in low


class PyghmiPowerClient:
    def __init__(self, ctx: EndpointContext) -> None:
        if not ctx.username or not ctx.secret:
            raise ControlError("unconfigured", "IPMI 端点没有关联用户名和密码的凭据")
        try:
            from pyghmi.ipmi import command as cmd

            self.cmd = cmd.Command(bmc=ctx.address, userid=ctx.username, password=ctx.secret, port=ctx.port or 623, keepalive=False)
        except ImportError as exc:
            raise ControlError("unsupported", "没有安装 pyghmi，无法执行 IPMI 电源控制") from exc
        except Exception as exc:  # noqa: BLE001 - 登录阶段失败：控制命令还没发出
            low = str(exc).lower()
            if "unauthorized" in low or "password" in low or "auth" in low or "rakp" in low:
                raise ControlError("auth", "BMC 用户名或密码不正确") from exc
            raise ControlError("unreachable", f"无法登录 BMC：{str(exc)[:200]}") from exc

    def power_state(self) -> str:
        try:
            state = self.cmd.get_power().get("powerstate")
        except Exception as exc:  # noqa: BLE001
            raise ControlError("timeout" if _is_timeout(exc) else "protocol", f"读取电源状态失败：{str(exc)[:200]}") from exc
        if state not in ("on", "off"):
            raise ControlError("protocol", f"BMC 返回了无法识别的电源状态：{state}")
        return state

    def set_power(self, state: str) -> None:
        try:
            self.cmd.set_power(state, wait=False)
        except Exception as exc:  # noqa: BLE001
            if _is_timeout(exc):
                raise ControlError("timeout", "电源指令已发出但 BMC 没有应答，设备当前状态未知") from exc
            raise ControlError("protocol", f"BMC 拒绝了电源指令：{str(exc)[:200]}") from exc

    def close(self) -> None:
        try:
            self.cmd.ipmi_session.logout()
        except Exception:  # noqa: BLE001
            pass


@dataclass
class PowerOutcome:
    # accepted（已接受未确认）/ verified（已查询确认）/ unconfirmed（无法确认）
    status: str
    message: str
    observed: dict[str, Any] = field(default_factory=dict)


def execute_power(
    ctx: EndpointContext,
    action: PowerAction,
    *,
    factory: PowerClientFactory = PyghmiPowerClient,
    verify_attempts: int = 3,
    verify_interval: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> PowerOutcome:
    """执行一次电源操作。抛出 ControlError 表示失败或结果不确定（kind=timeout）。"""
    client = factory(ctx)
    try:
        before = client.power_state()
        client.set_power(action.pyghmi_state)
        # 指令已被接受；之后的读取失败不改变“已接受”这一事实
        observed: dict[str, Any] = {"before": before}
        if action.expect is None:
            return PowerOutcome("unconfirmed", f"{action.label}指令已被 BMC 接受；无法从电源状态确认设备已重启，请稍后查看设备", observed)
        for i in range(max(1, verify_attempts)):
            if i:
                sleep(verify_interval)
            try:
                now = client.power_state()
            except ControlError as exc:
                observed["verify_error"] = str(exc)
                continue
            observed["after"] = now
            if now == action.expect:
                return PowerOutcome("verified", f"{action.label}指令已被接受，电源状态已确认为“{'开' if now == 'on' else '关'}”", observed)
        return PowerOutcome(
            "accepted",
            f"{action.label}指令已被 BMC 接受，但电源状态还没有变成预期的“{'开' if action.expect == 'on' else '关'}”；"
            "设备可能仍在执行（软关机可能需要几分钟），请稍后查看",
            observed,
        )
    finally:
        client.close()
