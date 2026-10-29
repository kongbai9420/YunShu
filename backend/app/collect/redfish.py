"""iDRAC9 等 Redfish 只读采集（补充 IPMI 读不到的硬盘与 RAID 状态）。只发 GET，
登录会话除外（POST SessionService/Sessions 拿令牌，结束时 DELETE 释放）。

- 会话复用：同一端点缓存 X-Auth-Token，令牌失效（401）时重新登录一次。
- TLS：BMC 多为自签证书，不能靠 CA 校验；先取对方证书的 SHA-256 指纹，与已固定的比对，
  首次则记下，比对通过后才发送用户名密码。端点“校验证书”关闭时跳过比对。
- 覆盖：系统健康与电源状态、CPU / 内存概况、温度、风扇、整机功耗、硬盘健康、RAID 卷健康，
  以及 BMC 系统事件日志（SEL，4.2 P13）：只读最近一页，统计 24 小时内的严重 / 警告条数，不清空日志。
"""

from __future__ import annotations

import hashlib
import socket
import ssl
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx

from app.collect.base import AdapterError, CollectResult, EndpointContext, ProbeResult
from app.metrics import Point

HEALTH_VALUE = {"OK": 1, "Warning": 0.5, "Critical": 0}


def cert_fingerprint(address: str, port: int, timeout: float) -> str:
    """取对方证书的 SHA-256 指纹（不做 CA 校验，只为比对固定值）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((address, port), timeout=timeout) as sock, ctx.wrap_socket(sock, server_hostname=address) as tls:
            der = tls.getpeercert(binary_form=True)
    except TimeoutError as exc:
        raise AdapterError("timeout", "连接 Redfish（HTTPS）超时") from exc
    except OSError as exc:
        raise AdapterError("unreachable", f"无法连接 Redfish（HTTPS）端口：{exc.strerror or exc}") from exc
    digest = hashlib.sha256(der or b"").hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


class RedfishAdapter:
    protocol: ClassVar[str] = "redfish"

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        fingerprint: Callable[[str, int, float], str] = cert_fingerprint,
    ) -> None:
        self._transport = transport
        self._fingerprint = fingerprint
        self._tokens: dict[tuple[str, int, str], tuple[str, str]] = {}  # key -> (token, session uri)
        self._lock = threading.Lock()

    def capabilities(self) -> set[str]:
        return {"system", "sensors", "fans", "power", "disks", "raid"}

    # -- HTTP --

    def _client(self, ctx: EndpointContext) -> httpx.Client:
        # 没有凭据就不去连设备（包括取证书指纹），直接提示待配置
        if not ctx.username or not ctx.secret:
            raise AdapterError("unconfigured", "Redfish 端点没有关联用户名和密码的凭据")
        port = ctx.port or 443
        if ctx.tls_verify:
            ctx.trust.check_tls(self._fingerprint(ctx.address, port, ctx.timeout))
        return httpx.Client(
            base_url=f"https://{ctx.address}:{port}", verify=False, timeout=ctx.timeout, transport=self._transport,
            headers={"Accept": "application/json", "OData-Version": "4.0"},
        )

    def _login(self, http: httpx.Client, ctx: EndpointContext) -> str:
        if not ctx.username or not ctx.secret:
            raise AdapterError("unconfigured", "Redfish 端点没有关联用户名和密码的凭据")
        try:
            r = http.post("/redfish/v1/SessionService/Sessions", json={"UserName": ctx.username, "Password": ctx.secret})
        except httpx.TimeoutException as exc:
            raise AdapterError("timeout", "连接 Redfish 超时（BMC 没有应答 HTTPS）") from exc
        except httpx.TransportError as exc:
            raise AdapterError("unreachable", f"无法连接 Redfish：{str(exc)[:200]}") from exc
        if r.status_code in (401, 403):
            raise AdapterError("auth", "Redfish 用户名或密码不正确")
        if r.status_code >= 400 or "X-Auth-Token" not in r.headers:
            raise AdapterError("protocol", f"Redfish 登录失败（HTTP {r.status_code}）")
        key = (ctx.address, ctx.port or 443, ctx.username)
        with self._lock:
            self._tokens[key] = (r.headers["X-Auth-Token"], r.headers.get("Location", ""))
        return r.headers["X-Auth-Token"]

    def _get(self, http: httpx.Client, ctx: EndpointContext, path: str) -> dict[str, Any]:
        key = (ctx.address, ctx.port or 443, ctx.username)
        with self._lock:
            token = self._tokens.get(key, ("", ""))[0]
        if not token:
            token = self._login(http, ctx)
        try:
            r = http.get(path, headers={"X-Auth-Token": token})
            if r.status_code == 401:  # 会话过期：重新登录一次
                r = http.get(path, headers={"X-Auth-Token": self._login(http, ctx)})
        except httpx.TimeoutException as exc:
            raise AdapterError("timeout", "Redfish 请求超时") from exc
        except httpx.TransportError as exc:
            raise AdapterError("unreachable", f"Redfish 连接失败：{exc}") from exc
        if r.status_code == 404:
            return {}
        if r.status_code >= 400:
            raise AdapterError("protocol", f"Redfish {path} 返回 HTTP {r.status_code}")
        return r.json()

    def logout(self, ctx: EndpointContext) -> None:
        key = (ctx.address, ctx.port or 443, ctx.username)
        with self._lock:
            token, uri = self._tokens.pop(key, ("", ""))
        if token and uri:
            try:
                with self._client(ctx) as http:
                    http.delete(uri, headers={"X-Auth-Token": token})
            except (AdapterError, httpx.HTTPError):
                pass

    def _members(self, http: httpx.Client, ctx: EndpointContext, path: str) -> list[str]:
        return [m["@odata.id"] for m in self._get(http, ctx, path).get("Members", []) if "@odata.id" in m]

    # -- 接口 --

    def probe(self, ctx: EndpointContext) -> ProbeResult:
        with self._client(ctx) as http:
            root = self._get(http, ctx, "/redfish/v1/")
        return ProbeResult(ok=True, capabilities=self.capabilities(), facts={"redfish_version": root.get("RedfishVersion"), "vendor": root.get("Vendor")})

    def discover(self, ctx: EndpointContext) -> dict[str, Any]:
        with self._client(ctx) as http:
            return {"systems": self._members(http, ctx, "/redfish/v1/Systems"), "chassis": self._members(http, ctx, "/redfish/v1/Chassis")}

    def inventory(self, ctx: EndpointContext) -> ProbeResult:
        with self._client(ctx) as http:
            systems = self._members(http, ctx, "/redfish/v1/Systems")
            if len(systems) != 1:
                return ProbeResult(ok=True)  # 多系统不挑一台冒充当前资产
            s = self._get(http, ctx, systems[0])
        return ProbeResult(ok=True, facts={"hostname": s.get("HostName"), "vendor": s.get("Manufacturer"),
                                          "model": s.get("Model"), "serial": s.get("SerialNumber")})

    def collect(self, ctx: EndpointContext) -> CollectResult:
        pts: list[Point] = []
        facts: dict[str, Any] = {}
        with self._client(ctx) as http:
            for sys_uri in self._members(http, ctx, "/redfish/v1/Systems")[:2]:
                s = self._get(http, ctx, sys_uri)
                facts.update({"model": s.get("Model"), "serial": s.get("SerialNumber"), "bios": s.get("BiosVersion"), "hostname": s.get("HostName")})
                health = (s.get("Status") or {}).get("HealthRollup") or (s.get("Status") or {}).get("Health")
                if health in HEALTH_VALUE:
                    pts.append(Point("health.system", HEALTH_VALUE[health], "", message=health))
                if s.get("PowerState"):
                    pts.append(Point("power.on", 1 if s["PowerState"] == "On" else 0, ""))
                mem = (s.get("MemorySummary") or {}).get("TotalSystemMemoryGiB")
                if mem is not None:
                    pts.append(Point("mem.total_bytes", int(float(mem) * 1024**3), "B"))
                pts.extend(self._storage(http, ctx, (s.get("Storage") or {}).get("@odata.id")))
            # 4.3 P18：BMC 固件版本（iDRAC 的 Manager 资源），漏洞比对用
            for mgr_uri in self._members(http, ctx, "/redfish/v1/Managers")[:1]:
                mgr = self._get(http, ctx, mgr_uri)
                facts.update({"bmc_firmware": mgr.get("FirmwareVersion"), "bmc_model": mgr.get("Model")})
            for ch_uri in self._members(http, ctx, "/redfish/v1/Chassis")[:2]:
                th = self._get(http, ctx, f"{ch_uri}/Thermal")
                for t in th.get("Temperatures", []):
                    name = (t.get("Name") or t.get("MemberId") or "temp").replace(" ", "_")
                    v = t.get("ReadingCelsius")
                    pts.append(Point(f"temp:{name}", float(v), "°C") if v is not None else Point(f"temp:{name}", None, "°C", quality="unsupported"))
                for f in th.get("Fans", []):
                    name = (f.get("Name") or f.get("FanName") or f.get("MemberId") or "fan").replace(" ", "_")
                    v, unit = f.get("Reading"), f.get("ReadingUnits", "RPM")
                    if v is None:
                        pts.append(Point(f"fan.rpm:{name}", None, "RPM", quality="unsupported"))
                    elif unit == "RPM":
                        pts.append(Point(f"fan.rpm:{name}", float(v), "RPM"))
                    else:
                        # BMC 自己报告的百分比，不是我们估算的；单独存放，避免与 RPM 混淆
                        pts.append(Point(f"fan.pct:{name}", float(v), "%"))
                pw = self._get(http, ctx, f"{ch_uri}/Power")
                for pc in pw.get("PowerControl", [])[:1]:
                    if pc.get("PowerConsumedWatts") is not None:
                        pts.append(Point("power.watts", float(pc["PowerConsumedWatts"]), "W"))
            entries: list[dict[str, Any]] = []
            pts.extend(self._sel(http, ctx, entries=entries))
        out = {k: v for k, v in facts.items() if v}
        if entries:
            # 4.4 P20c：逐条交给“服务器告警”入库（采集任务取走，不存进采集状态）
            out["sel_entries"] = entries
        return CollectResult(points=pts, facts=out)

    def _sel(self, http: httpx.Client, ctx: EndpointContext, now: datetime | None = None,
             entries: list[dict[str, Any]] | None = None) -> list[Point]:
        """BMC 系统事件日志：找到 SEL 日志服务，读最近一页条目，统计 24 小时内的严重 / 警告条数。
        entries 不为 None 时同时收集每一条（记录号、时间、级别、文字），供服务器告警入库。"""
        now = now or datetime.now(UTC)
        for mgr_uri in self._members(http, ctx, "/redfish/v1/Managers")[:2]:
            mgr = self._get(http, ctx, mgr_uri)
            services = (mgr.get("LogServices") or {}).get("@odata.id")
            for svc_uri in self._members(http, ctx, services)[:8] if services else []:
                svc = self._get(http, ctx, svc_uri)
                if str(svc.get("Id", "")).upper() != "SEL" and svc.get("LogEntryType") != "SEL":
                    continue
                entries_uri = (svc.get("Entries") or {}).get("@odata.id")
                if not entries_uri:
                    continue
                members = self._get(http, ctx, f"{entries_uri}?$top=100").get("Members", [])[:100]
                counts = {"Critical": 0, "Warning": 0}
                latest: dict[str, tuple[datetime, str]] = {}
                if entries is not None:
                    entries.extend({"id": e.get("Id") or e.get("@odata.id"), "created": e.get("Created"), "severity": e.get("Severity"),
                                    "message": e.get("Message") or ""} for e in members if isinstance(e, dict))
                for e in members:
                    sev = e.get("Severity")
                    try:
                        created = datetime.fromisoformat(str(e.get("Created")).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if sev not in counts or created.tzinfo is None or now - created > timedelta(hours=24):
                        continue
                    counts[sev] += 1
                    if sev not in latest or created > latest[sev][0]:
                        latest[sev] = (created, str(e.get("Message") or "")[:200])
                return [Point(f"sel.{sev.lower()}_24h", float(n), "", message=latest.get(sev, (None, ""))[1])
                        for sev, n in counts.items()]
        return [Point("sel.critical_24h", None, "", quality="unsupported", message="BMC 不提供 SEL 日志服务")]

    def _storage(self, http: httpx.Client, ctx: EndpointContext, uri: str | None) -> list[Point]:
        if not uri:
            return []
        out: list[Point] = []
        for ctrl_uri in self._members(http, ctx, uri)[:8]:
            ctrl = self._get(http, ctx, ctrl_uri)
            for d in ctrl.get("Drives", [])[:64]:
                drive = self._get(http, ctx, d["@odata.id"])
                name = (drive.get("Name") or drive.get("Id") or "disk").replace(" ", "_")
                h = (drive.get("Status") or {}).get("Health")
                out.append(Point(f"disk.health:{name}", HEALTH_VALUE[h], "", message=h) if h in HEALTH_VALUE else Point(f"disk.health:{name}", None, "", quality="unsupported"))
            vols = (ctrl.get("Volumes") or {}).get("@odata.id")
            for v_uri in self._members(http, ctx, vols)[:32] if vols else []:
                vol = self._get(http, ctx, v_uri)
                name = (vol.get("Name") or vol.get("Id") or "volume").replace(" ", "_")
                h = (vol.get("Status") or {}).get("Health")
                raid = vol.get("RAIDType") or vol.get("VolumeType") or ""
                out.append(Point(f"raid.health:{name}", HEALTH_VALUE[h], "", message=f"{raid} {h}".strip()) if h in HEALTH_VALUE else Point(f"raid.health:{name}", None, "", quality="unsupported"))
        return out
