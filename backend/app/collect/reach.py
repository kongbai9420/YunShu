"""可达性检查：对管理端口发起 TCP 连接，记录是否可达与连接耗时（随身 WiFi 等只做可达性，落地方案 B 批）。

不发 ICMP（需要管理员权限），也不发送任何应用层数据。
"""

from __future__ import annotations

import socket
import time
from typing import Any, ClassVar

from app.collect.base import AdapterError, CollectResult, EndpointContext, ProbeResult
from app.metrics import Point

DEFAULT_PORTS = {"ssh": 22, "https": 443, "http": 80, "snmp": 161, "ipmi": 623, "redfish": 443}


class ReachAdapter:
    protocol: ClassVar[str] = "reach"

    def __init__(self, connect=socket.create_connection) -> None:  # type: ignore[no-untyped-def]
        self._connect = connect

    def capabilities(self) -> set[str]:
        return {"system"}

    def _measure(self, ctx: EndpointContext) -> float:
        port = ctx.port or DEFAULT_PORTS.get(ctx.options.get("endpoint_protocol", ""), 80)
        t0 = time.perf_counter()
        try:
            sock = self._connect((ctx.address, port), ctx.timeout)
        except TimeoutError as exc:
            raise AdapterError("timeout", f"连接 {port} 端口超时") from exc
        except OSError as exc:
            raise AdapterError("unreachable", f"无法连接 {port} 端口：{exc.strerror or exc}") from exc
        sock.close()
        return (time.perf_counter() - t0) * 1000

    def probe(self, ctx: EndpointContext) -> ProbeResult:
        ms = self._measure(ctx)
        return ProbeResult(ok=True, capabilities=self.capabilities(), message=f"可达，{ms:.0f} ms")

    def discover(self, ctx: EndpointContext) -> dict[str, Any]:
        return {}

    def collect(self, ctx: EndpointContext) -> CollectResult:
        ms = self._measure(ctx)
        return CollectResult(points=[Point("reach.up", 1, ""), Point("reach.rtt_ms", round(ms, 1), "ms")])
