"""IPMI 只读采集：传感器（温度、风扇、电压、功耗）与电源状态。从不发送控制或 raw 命令。

- 首选 pyghmi（Apache-2.0）；pyghmi 不可用或失败时回退 ipmitool（`sdr elist full`，只读）。
- 会话：默认每次采集登录、读取、登出。YUNSHU_IPMI_SESSION_REUSE=1 时复用会话，
  减少 BMC 登录日志；它还没在真实 BMC 上验证过，所以默认关闭，与旧版开关的默认值一致。
- 风扇：只记录实测转速（RPM）；不知道额定转速时不换算成百分比。
- 读不到值的传感器标为 unsupported，不写 0。

注意：旧版在 P5 之前仍在轮询同一批 BMC。部分 BMC 同时会话数有限（如 iDRAC 约 5 个），
新旧两边都采集时请留意 BMC 会话是否够用。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from collections.abc import Callable
from typing import Any, ClassVar, Protocol

from app.collect.base import AdapterError, CollectResult, EndpointContext, ProbeResult
from app.metrics import Point

SESSION_REUSE_ENV = "YUNSHU_IPMI_SESSION_REUSE"
UNIT_MAP = {"°C": "°C", "C": "°C", "degrees C": "°C", "RPM": "RPM", "V": "V", "Volts": "V", "W": "W", "Watts": "W", "A": "A", "Amps": "A", "%": "%", "percent": "%"}


class IpmiClient(Protocol):
    def inventory(self) -> dict[str, Any]: ...

    def sensors(self) -> list[dict[str, Any]]:
        """[{name, type, value, units, unavailable}]"""
        ...

    def power_on(self) -> bool | None: ...

    def close(self) -> None: ...


def _slug(name: str) -> str:
    return re.sub(r"\s+", "_", name.strip())


# ---- pyghmi ----


class PyghmiClient:
    def __init__(self, ctx: EndpointContext, *, command: Any = None) -> None:
        if not ctx.username or not ctx.secret:
            raise AdapterError("unconfigured", "IPMI 端点没有关联用户名和密码的凭据")
        try:
            if command is None:
                from pyghmi.ipmi import command as cmd

                command = cmd.Command(bmc=ctx.address, userid=ctx.username, password=ctx.secret, port=ctx.port or 623, keepalive=False)
        except Exception as exc:  # noqa: BLE001 - pyghmi 把超时、认证失败都抛成 IpmiException
            raise _map_error(exc) from exc
        self.cmd = command

    def sensors(self) -> list[dict[str, Any]]:
        try:
            return [
                {"name": r.name, "type": r.type, "value": r.value, "units": r.units, "unavailable": getattr(r, "unavailable", 0)}
                for r in self.cmd.get_sensor_data()
            ]
        except Exception as exc:  # noqa: BLE001
            raise _map_error(exc) from exc

    def power_on(self) -> bool | None:
        try:
            return self.cmd.get_power().get("powerstate") == "on"
        except Exception:  # noqa: BLE001 - 电源状态读不到不影响传感器
            return None

    def close(self) -> None:
        try:
            self.cmd.ipmi_session.logout()
        except Exception:  # noqa: BLE001
            pass

    def sel(self) -> list[dict[str, Any]]:
        """BMC 系统事件日志（4.4 P20c，Redfish 不可用时兜底）：只读最近 200 条，从不清空。"""
        try:
            events = list(self.cmd.get_event_log())[-200:]
        except Exception as exc:  # noqa: BLE001
            raise _map_error(exc) from exc
        out = []
        for i, ev in enumerate(events):
            health = ev.get("severity")
            # pyghmi Health：0 正常、1 警告、2 严重、4 故障
            sev = "Critical" if isinstance(health, int) and health >= 2 else "Warning" if health == 1 else "OK"
            text = " ".join(str(ev.get(k) or "") for k in ("component", "event", "event_data")).strip()
            if ev.get("deassertion"):
                text += "（已恢复）"
            out.append({"id": ev.get("event_id", ev.get("record_id", i)), "created": ev.get("timestamp"), "severity": sev, "message": text})
        return out

    def inventory(self) -> dict[str, Any]:
        try:
            # System FRU，避免把主板 / 电源模块的资料当成整机。
            info = self.cmd.get_inventory_of_component("System") or {}
            return {"vendor": info.get("Manufacturer"), "model": info.get("Product name"), "serial": info.get("Serial Number")}
        except Exception as exc:
            raise _map_error(exc) from exc


def _map_error(exc: Exception) -> AdapterError:
    text = str(exc)
    low = text.lower()
    if "timeout" in low or "no response" in low or "timed out" in low:
        return AdapterError("timeout", "BMC 无响应（地址、端口或网络问题）")
    if "unauthorized" in low or "password" in low or "auth" in low or "rakp" in low:
        return AdapterError("auth", "BMC 用户名或密码不正确")
    return AdapterError("protocol", f"IPMI 错误：{text[:200]}")


# ---- ipmitool 回退 ----


def parse_sdr_elist(text: str) -> list[dict[str, Any]]:
    """解析 `ipmitool sdr elist full`：名称 | ID | 状态 | 实体 | 读数 单位"""
    out = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        name, status, reading = parts[0], parts[2], parts[4]
        m = re.match(r"^(-?\d+(?:\.\d+)?)\s*(.*)$", reading)
        if m and status not in ("ns", "na"):
            unit = m.group(2).strip()
            kind = "Temperature" if "degrees" in unit else "Fan" if unit == "RPM" else "Voltage" if unit == "Volts" else "Power" if unit == "Watts" else "Other"
            out.append({"name": name, "type": kind, "value": float(m.group(1)), "units": unit, "unavailable": 0})
        else:
            out.append({"name": name, "type": "Other", "value": None, "units": "", "unavailable": 1})
    return out


class IpmitoolClient:
    def __init__(self, ctx: EndpointContext, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
        if not ctx.username or not ctx.secret:
            raise AdapterError("unconfigured", "IPMI 端点没有关联用户名和密码的凭据")
        self.ctx = ctx
        self.runner = runner
        self.exe = shutil.which("ipmitool")
        if self.exe is None:
            raise AdapterError("unsupported", "pyghmi 不可用且没有安装 ipmitool")

    def _run(self, *args: str) -> str:
        c = self.ctx
        # 密码通过环境变量传（-E），不出现在命令行和进程列表里
        cmd = [self.exe, "-I", "lanplus", "-H", c.address, "-p", str(c.port or 623), "-U", c.username, "-E", *args]
        try:
            res = self.runner(cmd, capture_output=True, text=True, timeout=c.timeout, env={**os.environ, "IPMI_PASSWORD": c.secret})
        except subprocess.TimeoutExpired as exc:
            raise AdapterError("timeout", "ipmitool 执行超时") from exc
        if res.returncode != 0:
            err = (res.stderr or "").lower()
            if "unauthorized" in err or "password" in err:
                raise AdapterError("auth", "BMC 用户名或密码不正确")
            raise AdapterError("timeout" if "timeout" in err or "unable to establish" in err else "protocol", f"ipmitool 失败：{res.stderr.strip()[:200]}")
        return res.stdout

    def sensors(self) -> list[dict[str, Any]]:
        return parse_sdr_elist(self._run("sdr", "elist", "full"))

    def inventory(self) -> dict[str, Any]:
        text = self._run("fru", "print", "0")
        values = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
        values = {key.strip(): value.strip() for key, value in values.items()}
        return {"vendor": values.get("Product Manufacturer"), "model": values.get("Product Name"), "serial": values.get("Product Serial")}

    def power_on(self) -> bool | None:
        try:
            return "on" in self._run("chassis", "power", "status").lower().split()[-1]
        except AdapterError:
            return None

    def close(self) -> None:
        pass


# ---- 适配器 ----


class IpmiAdapter:
    protocol: ClassVar[str] = "ipmi"

    def __init__(
        self,
        client_factory: Callable[[EndpointContext], IpmiClient] | None = None,
        fallback_factory: Callable[[EndpointContext], IpmiClient] | None = None,
        reuse_sessions: bool | None = None,
    ) -> None:
        self._factory = client_factory or PyghmiClient
        self._fallback = fallback_factory or IpmitoolClient
        self.reuse = os.environ.get(SESSION_REUSE_ENV, "0") == "1" if reuse_sessions is None else reuse_sessions
        self._sessions: dict[tuple[str, int, str], tuple[str, IpmiClient, float]] = {}
        self._lock = threading.Lock()

    def capabilities(self) -> set[str]:
        return {"sensors", "fans", "power"}

    def _client(self, ctx: EndpointContext) -> tuple[IpmiClient, bool]:
        """返回 (客户端, 用完是否要关闭)。复用模式下按 (地址, 端口, 用户) 缓存。"""
        if not self.reuse:
            return self._factory(ctx), True
        key = (ctx.address, ctx.port or 623, ctx.username)
        with self._lock:
            cached = self._sessions.get(key)
            if cached and cached[0] == ctx.secret and time.monotonic() - cached[2] < 600:
                return cached[1], False
        client = self._factory(ctx)
        with self._lock:
            self._sessions[key] = (ctx.secret, client, time.monotonic())
        return client, False

    def _drop(self, ctx: EndpointContext) -> None:
        with self._lock:
            cached = self._sessions.pop((ctx.address, ctx.port or 623, ctx.username), None)
        if cached:
            cached[1].close()

    def _read(self, ctx: EndpointContext) -> tuple[list[dict[str, Any]], bool | None, str]:
        try:
            client, close = self._client(ctx)
            try:
                sensors, power = client.sensors(), client.power_on()
                # 采集计划要求时（Redfish 不可用、由 IPMI 兜底读日志）同一会话里再读 SEL；读不了不影响传感器
                if ctx.options.get("sel") and hasattr(client, "sel"):
                    try:
                        ctx.options["sel_entries"] = client.sel()
                    except AdapterError:
                        ctx.options["sel_entries"] = None
                return sensors, power, ""
            finally:
                if close:
                    client.close()
        except AdapterError as exc:
            if exc.kind in ("unconfigured", "auth"):
                raise
            self._drop(ctx)
            first = exc
        try:
            fb = self._fallback(ctx)
        except AdapterError:
            raise first from None
        try:
            return fb.sensors(), fb.power_on(), f"pyghmi 读取失败（{first}），已回退 ipmitool"
        finally:
            fb.close()

    def probe(self, ctx: EndpointContext) -> ProbeResult:
        sensors, power, msg = self._read(ctx)
        return ProbeResult(ok=True, capabilities=self.capabilities(), facts={"sensor_count": len(sensors), "power_on": power}, message=msg)

    def inventory(self, ctx: EndpointContext) -> ProbeResult:
        for factory in (self._factory, self._fallback):
            client = None
            try:
                client = factory(ctx)
                return ProbeResult(ok=True, facts=client.inventory())
            except AdapterError as exc:
                if exc.kind in {"auth", "unconfigured"} or factory == self._fallback:
                    raise
            finally:
                if client is not None:
                    client.close()
        raise AdapterError("unsupported", "无法读取系统 FRU")

    def discover(self, ctx: EndpointContext) -> dict[str, Any]:
        sensors, _, _ = self._read(ctx)
        return {"sensors": [{"name": s["name"], "type": s["type"], "units": s["units"]} for s in sensors]}

    def collect(self, ctx: EndpointContext) -> CollectResult:
        sensors, power, msg = self._read(ctx)
        pts: list[Point] = []
        cpu_temps: list[float] = []
        for s in sensors:
            name, kind, value, units = s["name"], str(s["type"]), s["value"], UNIT_MAP.get(s["units"], s["units"])
            # 按单位判断功耗：不少 BMC 把整机功耗标成 Current 类型、单位 W
            prefix = "power.watts" if units == "W" else {"Temperature": "temp", "Fan": "fan.rpm", "Voltage": "volt", "Current": "amp"}.get(kind)
            if prefix is None:
                continue
            key = f"{prefix}:{_slug(name)}"
            if value is None or s.get("unavailable"):
                pts.append(Point(key, None, units, quality="unsupported", message="传感器没有读数"))
                continue
            pts.append(Point(key, float(value), units))
            if kind == "Temperature" and "cpu" in name.lower():
                cpu_temps.append(float(value))
            if prefix == "power.watts" and not any(p.key == "power.watts" for p in pts):
                pts.append(Point("power.watts", float(value), "W"))
        if cpu_temps:
            pts.append(Point("cpu.temp", max(cpu_temps), "°C"))
        if power is not None:
            pts.append(Point("power.on", 1 if power else 0, ""))
        facts: dict[str, Any] = {"sensor_count": len(sensors), "power_on": power}
        entries = ctx.options.pop("sel_entries", None)
        if entries is not None:
            from app.server_events import counts_24h

            for sev, (n, latest) in counts_24h(entries, datetime.now(UTC)).items():
                pts.append(Point(f"sel.{sev}_24h", float(n), "", message=latest))
            if entries:
                facts["sel_entries"] = entries
        return CollectResult(points=pts, facts=facts, messages=[msg] if msg else [])
