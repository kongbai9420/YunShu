"""Proxmox VE 只读采集（REST API，端口 8006）。只发 GET（密码登录换票据除外）。

凭据二选一：
- API 令牌（推荐，类型 token）：用户名填 `user@realm!tokenid`，密钥填令牌值；
- 用户名 + 密码（类型 password）：用户名填 `root@pam` 这类带 realm 的名字，登录换票据。
建议给令牌只分配 PVEAuditor 角色（只读）。

TLS：PVE 默认自签证书，先比对证书指纹（首次信任并固定），再发送凭据。
指标：节点 CPU、内存、负载、运行时间、根分区；存储使用率；虚拟机与容器的运行数、各自运行状态与 CPU / 内存。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, ClassVar

import httpx

from app.collect.base import AdapterError, CollectResult, EndpointContext, ProbeResult
from app.collect.redfish import cert_fingerprint
from app.metrics import Point

MAX_GUESTS = 200


def protection_points(backups: list[dict[str, Any]], replication: list[dict[str, Any]], now: float) -> list[Point]:
    """备份与复制（4.2 P15）：24 小时内失败的备份数、距上次成功备份的小时数、出错的复制任务数。"""
    done = [b for b in backups if b.get("endtime") and b.get("status")]
    pts: list[Point] = []
    if not done:
        pts.append(Point("backup.last_ok_age_h", None, "h", quality="unsupported", message="最近 50 个任务里没有备份任务"))
    else:
        failed = [b for b in done if b["status"] != "OK" and now - float(b["endtime"]) <= 86400]
        last = max((float(b["endtime"]) for b in done if b["status"] == "OK"), default=None)
        latest_failure = max(failed, key=lambda b: float(b["endtime"]), default=None)
        pts.append(Point("backup.failed_24h", float(len(failed)), "",
                         message=f"{latest_failure.get('id') or ''} {latest_failure['status']}".strip()[:200] if latest_failure else ""))
        pts.append(Point("backup.last_ok_age_h", round((now - last) / 3600, 1), "h") if last is not None
                   else Point("backup.last_ok_age_h", None, "h", quality="unsupported", message="最近的备份都没有成功"))
    if replication:
        bad = [r for r in replication if int(r.get("fail_count") or 0) > 0 or r.get("error")]
        pts.append(Point("replication.failed", float(len(bad)), "",
                         message="；".join(f"{r.get('id')}: {str(r.get('error') or '')[:80]}" for r in bad[:3])))
    return pts


class PveAdapter:
    protocol: ClassVar[str] = "pve-api"

    def __init__(self, transport: httpx.BaseTransport | None = None, fingerprint: Callable[[str, int, float], str] = cert_fingerprint) -> None:
        self._transport = transport
        self._fingerprint = fingerprint

    def capabilities(self) -> set[str]:
        return {"system", "cpu", "memory", "disks", "vms"}

    def _client(self, ctx: EndpointContext) -> httpx.Client:
        port = ctx.port or 8006
        if ctx.tls_verify:
            ctx.trust.check_tls(self._fingerprint(ctx.address, port, ctx.timeout))
        if not ctx.username or not ctx.secret:
            raise AdapterError("unconfigured", "PVE 端点没有关联 API 令牌或用户名密码")
        http = httpx.Client(base_url=f"https://{ctx.address}:{port}/api2/json", verify=False, timeout=ctx.timeout, transport=self._transport)
        if ctx.credential_kind == "token":
            http.headers["Authorization"] = f"PVEAPIToken={ctx.username}={ctx.secret}"
        else:
            try:
                r = http.post("/access/ticket", data={"username": ctx.username, "password": ctx.secret})
            except httpx.TimeoutException as exc:
                http.close()
                raise AdapterError("timeout", "PVE 登录超时") from exc
            except httpx.TransportError as exc:
                http.close()
                raise AdapterError("unreachable", f"无法连接 PVE：{exc}") from exc
            if r.status_code == 401:
                http.close()
                raise AdapterError("auth", "PVE 用户名或密码不正确（用户名需带 realm，如 root@pam）")
            ticket = (r.json().get("data") or {}).get("ticket")
            if r.status_code >= 400 or not ticket:
                http.close()
                raise AdapterError("protocol", f"PVE 登录失败（HTTP {r.status_code}）")
            http.cookies.set("PVEAuthCookie", ticket)
        return http

    @staticmethod
    def _get(http: httpx.Client, path: str) -> Any:
        try:
            r = http.get(path)
        except httpx.TimeoutException as exc:
            raise AdapterError("timeout", "PVE 请求超时") from exc
        except httpx.TransportError as exc:
            raise AdapterError("unreachable", f"无法连接 PVE：{exc}") from exc
        if r.status_code == 401:
            raise AdapterError("auth", "PVE 令牌或密码无效")
        if r.status_code == 403:
            raise AdapterError("auth", "PVE 账号权限不足（需要至少 PVEAuditor）")
        if r.status_code >= 400:
            raise AdapterError("protocol", f"PVE {path} 返回 HTTP {r.status_code}")
        return r.json().get("data")

    def probe(self, ctx: EndpointContext) -> ProbeResult:
        http = self._client(ctx)
        try:
            ver = self._get(http, "/version") or {}
        finally:
            http.close()
        return ProbeResult(ok=True, capabilities=self.capabilities(), facts={"pve_version": ver.get("version")})

    def discover(self, ctx: EndpointContext) -> dict[str, Any]:
        http = self._client(ctx)
        try:
            guests = self._get(http, "/cluster/resources?type=vm") or []
        finally:
            http.close()
        return {"guests": [{"vmid": g.get("vmid"), "name": g.get("name"), "type": g.get("type"), "node": g.get("node")} for g in guests]}

    def inventory(self, ctx: EndpointContext) -> ProbeResult:
        http = self._client(ctx)
        try:
            self._get(http, "/version")  # 确认确为 PVE 服务
            nodes = self._get(http, "/nodes") or []
        finally:
            http.close()
        return ProbeResult(ok=True, facts={"platform": "proxmox", "hostname": nodes[0].get("node") if len(nodes) == 1 else None})

    def collect(self, ctx: EndpointContext) -> CollectResult:
        pts: list[Point] = []
        facts: dict[str, Any] = {}
        http = self._client(ctx)
        try:
            ver = self._get(http, "/version") or {}
            facts["pve_version"] = ver.get("version")
            raw_nodes = self._get(http, "/nodes")
            nodes = raw_nodes if isinstance(raw_nodes, list) else []
            backups: list[dict[str, Any]] = []
            replication: list[dict[str, Any]] = []
            protection_errors: list[str] = []
            for n in nodes[:8]:
                name = n.get("node", "node")
                if n.get("status") != "online":
                    pts.append(Point(f"pve.node_up:{name}", 0, ""))
                    continue
                pts.append(Point(f"pve.node_up:{name}", 1, ""))
                st = self._get(http, f"/nodes/{name}/status") or {}
                single = len(nodes) == 1
                sfx = "" if single else f":{name}"
                if "cpu" in st:
                    pts.append(Point(f"cpu.load{sfx}", round(float(st["cpu"]) * 100, 1), "%"))
                mem = st.get("memory") or {}
                if mem.get("total"):
                    pts.append(Point(f"mem.used_pct{sfx}", round(mem["used"] / mem["total"] * 100, 1), "%"))
                    pts.append(Point(f"mem.total_bytes{sfx}", int(mem["total"]), "B"))
                if st.get("loadavg"):
                    pts.append(Point(f"load.1m{sfx}", float(st["loadavg"][0]), ""))
                if st.get("uptime") is not None:
                    pts.append(Point(f"system.uptime{sfx}", int(st["uptime"]), "s"))
                root = st.get("rootfs") or {}
                if root.get("total"):
                    pts.append(Point(f"disk.used_pct:/{sfx}", round(root["used"] / root["total"] * 100, 1), "%"))
                if single:
                    facts.update({"cpu_model": (st.get("cpuinfo") or {}).get("model"), "kernel": st.get("kversion")})
                for s in (self._get(http, f"/nodes/{name}/storage") or [])[:32]:
                    if s.get("total") and s.get("active"):
                        pts.append(Point(f"storage.used_pct:{s.get('storage')}{sfx}", round(s["used"] / s["total"] * 100, 1), "%"))
                # 4.2 P15：备份（vzdump 任务）与复制任务状态。权限不足时只记原因，不影响其余指标
                for path, bucket in ((f"/nodes/{name}/tasks?typefilter=vzdump&limit=50", backups), (f"/nodes/{name}/replication", replication)):
                    try:
                        bucket.extend(x for x in (self._get(http, path) or []) if isinstance(x, dict))
                    except AdapterError as exc:
                        if exc.kind not in ("auth", "protocol"):
                            raise
                        protection_errors.append(str(exc))
            raw_guests = self._get(http, "/cluster/resources?type=vm")
            guests = raw_guests if isinstance(raw_guests, list) else []
        finally:
            http.close()
        pts.extend(protection_points(backups, replication, time.time()))
        if protection_errors:
            facts["protection_error"] = protection_errors[0]
        running = [g for g in guests if g.get("status") == "running"]
        pts.append(Point("vms.total", len(guests), ""))
        pts.append(Point("vms.running", len(running), ""))
        for g in guests[:MAX_GUESTS]:
            label = f"{g.get('vmid')}-{g.get('name') or g.get('type')}"
            pts.append(Point(f"guest.up:{label}", 1 if g.get("status") == "running" else 0, ""))
            if g.get("status") == "running":
                if g.get("cpu") is not None:
                    pts.append(Point(f"guest.cpu:{label}", round(float(g["cpu"]) * 100, 1), "%"))
                if g.get("maxmem"):
                    pts.append(Point(f"guest.mem_pct:{label}", round(g.get("mem", 0) / g["maxmem"] * 100, 1), "%"))
        facts["guests"] = [{"vmid": g.get("vmid"), "name": g.get("name"), "type": g.get("type"), "status": g.get("status")} for g in guests[:MAX_GUESTS]]
        facts["virtualization"] = {
            "platform": "pve", "instance_uuid": "",
            "complete": isinstance(raw_nodes, list) and isinstance(raw_guests, list) and len(guests) <= MAX_GUESTS and len(nodes) <= 8,
            "hosts": [{"external_id": str(n.get("node") or ""), "name": n.get("node") or "", "connection": n.get("status") or "unknown"} for n in nodes[:8]],
            "guests": [{
                "external_id": str(g["vmid"]) if isinstance(g.get("vmid"), int) else "",
                "kind": {"qemu": "vm", "lxc": "container"}.get(g.get("type"), "unknown"),
                "name": g.get("name") or "", "host_external_id": str(g.get("node") or ""),
                "power": {"running": "on", "stopped": "off"}.get(g.get("status"), "unknown"),
            } for g in guests[:MAX_GUESTS]],
        }
        return CollectResult(points=pts, facts={k: v for k, v in facts.items() if v})
