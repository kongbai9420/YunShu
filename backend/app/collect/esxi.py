"""VMware ESXi / vCenter 只读采集（vSphere API，pyVmomi，Apache-2.0）。只读取属性，不调用任何任务方法。

- 单机 ESXi 没有 REST 指标接口，用 vSphere SOAP（pyVmomi）；连 vCenter 时同样适用（汇总所有主机）。
- TLS：ESXi 默认自签证书，先比对证书指纹（首次信任并固定），再登录；登录后在 finally 里登出。
- 权限：建议用只读角色的账号。
- 指标：主机 CPU（MHz 换算为使用率）、内存、运行时间、电源状态与连接状态；数据存储使用率；虚拟机总数、运行数与各自电源状态。

注意：本适配器没有可用的模拟器做协议级测试，只做了数据映射的单元测试，需要实机验证。
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from typing import Any, ClassVar, Protocol

from app.collect.base import AdapterError, CollectResult, EndpointContext, ProbeResult
from app.collect.redfish import cert_fingerprint
from app.metrics import Point

MAX_VMS = 300


class VsphereClient(Protocol):
    def about(self) -> dict[str, Any]: ...

    def hosts(self) -> list[dict[str, Any]]:
        """[{name, cpu_used_mhz, cpu_total_mhz, mem_used_bytes, mem_total_bytes, uptime, power, connection}]"""
        ...

    def datastores(self) -> list[dict[str, Any]]:
        """[{name, capacity, free, accessible}]"""
        ...

    def vms(self) -> list[dict[str, Any]]:
        """[{name, power}]"""
        ...

    def close(self) -> None: ...


class PyvmomiClient:
    def __init__(self, ctx: EndpointContext) -> None:
        from pyVim.connect import SmartConnect
        from pyVmomi import vim

        if not ctx.username or not ctx.secret:
            raise AdapterError("unconfigured", "ESXi 端点没有关联用户名和密码的凭据")
        self.vim = vim
        sslctx = ssl.create_default_context()
        sslctx.check_hostname = False
        sslctx.verify_mode = ssl.CERT_NONE  # 证书由指纹固定校验，见 EsxiAdapter._connect
        try:
            self.si = SmartConnect(host=ctx.address, port=ctx.port or 443, user=ctx.username, pwd=ctx.secret, sslContext=sslctx, connectionPoolTimeout=int(ctx.timeout))
        except vim.fault.InvalidLogin as exc:
            raise AdapterError("auth", "ESXi 用户名或密码不正确") from exc
        except TimeoutError as exc:
            raise AdapterError("timeout", "连接 ESXi 超时") from exc
        except OSError as exc:
            raise AdapterError("unreachable", f"无法连接 ESXi：{exc}") from exc
        self.content = self.si.RetrieveContent()

    def _view(self, kind):  # type: ignore[no-untyped-def]
        v = self.content.viewManager.CreateContainerView(self.content.rootFolder, [kind], True)
        try:
            return list(v.view)
        finally:
            v.Destroy()

    def about(self) -> dict[str, Any]:
        a = self.content.about
        return {"product": a.fullName, "api_type": a.apiType, "version": a.version, "build": getattr(a, "build", None) or "", "instance_uuid": getattr(a, "instanceUuid", None) or ""}

    def hosts(self) -> list[dict[str, Any]]:
        out = []
        for h in self._view(self.vim.HostSystem):
            s = h.summary
            hw, qs = s.hardware, s.quickStats
            out.append({
                "name": h.name,
                "external_id": h._moId,
                "cpu_used_mhz": qs.overallCpuUsage, "cpu_total_mhz": (hw.cpuMhz or 0) * (hw.numCpuCores or 0),
                "mem_used_bytes": (qs.overallMemoryUsage or 0) * 1024 * 1024, "mem_total_bytes": hw.memorySize,
                "uptime": qs.uptime, "power": str(s.runtime.powerState), "connection": str(s.runtime.connectionState),
                "model": hw.model, "vendor": hw.vendor,
            })
        return out

    def datastores(self) -> list[dict[str, Any]]:
        return [{"name": d.name, "capacity": d.summary.capacity, "free": d.summary.freeSpace, "accessible": d.summary.accessible} for d in self._view(self.vim.Datastore)]

    def vms(self) -> list[dict[str, Any]]:
        out = []
        references = self._view(self.vim.VirtualMachine)
        self.inventory_complete = len(references) <= MAX_VMS
        for v in references[:MAX_VMS]:
            runtime, config = v.runtime, v.config
            instance_uuid = getattr(config, "instanceUuid", None) if config else None
            out.append({
                "name": v.name, "power": str(runtime.powerState), "connection": str(runtime.connectionState),
                "external_id": f"uuid:{instance_uuid}" if instance_uuid else f"moref:{v._moId}",
                "host_external_id": runtime.host._moId if runtime.host else "",
            })
        return out

    def close(self) -> None:
        from pyVim.connect import Disconnect

        try:
            Disconnect(self.si)
        except Exception:  # noqa: BLE001
            pass


class EsxiAdapter:
    protocol: ClassVar[str] = "esxi-api"

    def __init__(self, client_factory: Callable[[EndpointContext], VsphereClient] | None = None, fingerprint: Callable[[str, int, float], str] = cert_fingerprint) -> None:
        self._factory = client_factory or PyvmomiClient
        self._fingerprint = fingerprint

    def capabilities(self) -> set[str]:
        return {"system", "cpu", "memory", "disks", "vms"}

    def _connect(self, ctx: EndpointContext) -> VsphereClient:
        if ctx.tls_verify:
            ctx.trust.check_tls(self._fingerprint(ctx.address, ctx.port or 443, ctx.timeout))
        return self._factory(ctx)

    def probe(self, ctx: EndpointContext) -> ProbeResult:
        c = self._connect(ctx)
        try:
            return ProbeResult(ok=True, capabilities=self.capabilities(), facts=c.about())
        finally:
            c.close()

    def discover(self, ctx: EndpointContext) -> dict[str, Any]:
        c = self._connect(ctx)
        try:
            return {"hosts": [h["name"] for h in c.hosts()], "vms": [v["name"] for v in c.vms()]}
        finally:
            c.close()

    def inventory(self, ctx: EndpointContext) -> ProbeResult:
        c = self._connect(ctx)
        try:
            about = c.about()
            hosts = c.hosts() if about.get("api_type") != "VirtualCenter" else []
        finally:
            c.close()
        if about.get("api_type") == "VirtualCenter":
            return ProbeResult(ok=True, facts={"platform": "vcenter"})
        facts = {"platform": "esxi"}
        if len(hosts) == 1:
            facts.update({"hostname": hosts[0].get("name"), "vendor": hosts[0].get("vendor"), "model": hosts[0].get("model")})
        return ProbeResult(ok=True, facts=facts)

    def collect(self, ctx: EndpointContext) -> CollectResult:
        c = self._connect(ctx)
        try:
            about, hosts, stores, vms = c.about(), c.hosts(), c.datastores(), c.vms()
            inventory_complete = getattr(c, "inventory_complete", True) and len(vms) <= MAX_VMS
        finally:
            c.close()
        pts: list[Point] = []
        single = len(hosts) == 1
        for h in hosts:
            sfx = "" if single else f":{h['name']}"
            connected = h.get("connection") == "connected"
            pts.append(Point(f"host.up{sfx}", 1 if connected and h.get("power") == "poweredOn" else 0, ""))
            if not connected:
                continue
            if h.get("cpu_total_mhz"):
                pts.append(Point(f"cpu.load{sfx}", round((h.get("cpu_used_mhz") or 0) / h["cpu_total_mhz"] * 100, 1), "%"))
            if h.get("mem_total_bytes"):
                pts.append(Point(f"mem.used_pct{sfx}", round(h["mem_used_bytes"] / h["mem_total_bytes"] * 100, 1), "%"))
                pts.append(Point(f"mem.total_bytes{sfx}", int(h["mem_total_bytes"]), "B"))
            if h.get("uptime") is not None:
                pts.append(Point(f"system.uptime{sfx}", int(h["uptime"]), "s"))
        for d in stores[:64]:
            if d.get("accessible") and d.get("capacity"):
                pts.append(Point(f"storage.used_pct:{d['name']}", round((1 - d["free"] / d["capacity"]) * 100, 1), "%"))
            else:
                pts.append(Point(f"storage.used_pct:{d['name']}", None, "%", quality="failed", message="数据存储不可访问"))
        running = [v for v in vms if v.get("power") == "poweredOn"]
        pts.append(Point("vms.total", len(vms), ""))
        pts.append(Point("vms.running", len(running), ""))
        for v in vms[:MAX_VMS]:
            pts.append(Point(f"guest.up:{v['name']}", 1 if v.get("power") == "poweredOn" else 0, ""))
        facts = {**about}
        facts["virtualization"] = {
            "platform": "vmware", "instance_uuid": about.get("instance_uuid") or "",
            "complete": inventory_complete,
            "hosts": [{"external_id": h.get("external_id") or "", "name": h.get("name") or "", "connection": h.get("connection") or "unknown"} for h in hosts],
            "guests": [{
                "external_id": v.get("external_id") or "", "kind": "vm", "name": v.get("name") or "",
                "host_external_id": v.get("host_external_id") or "",
                "power": "unknown" if v.get("connection") in ("disconnected", "inaccessible", "orphaned", "invalid") else {"poweredOn": "on", "poweredOff": "off"}.get(v.get("power"), "unknown"),
            } for v in vms[:MAX_VMS]],
        }
        if single:
            facts.update({"model": hosts[0].get("model"), "vendor": hosts[0].get("vendor")})
        return CollectResult(points=pts, facts={k: v for k, v in facts.items() if v})
