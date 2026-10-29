"""SSH 只读采集（Debian / Ubuntu、OpenWrt、Alpine / Rocky、PVE 备用）。

只执行下面固定的只读命令（读 /proc 与 /sys、df、hostname），模板和用户都不能传入任意脚本。
BusyBox（OpenWrt / Alpine）上同样可用。

主机密钥：先完成密钥交换、拿到对方主机密钥并与已固定的比对，一致（或首次）才发送密码，
不一致直接断开，密码不会发给可能的中间人。
"""

from __future__ import annotations

import io
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from app.collect.base import AdapterError, CollectResult, EndpointContext, ProbeResult
from app.metrics import Point

# 一次登录执行一条组合命令，用分隔行切开各段输出；每段都容忍缺失（命令不存在时为空）
SECTIONS: dict[str, str] = {
    "hostname": "cat /proc/sys/kernel/hostname",
    "os": "cat /etc/os-release 2>/dev/null; cat /etc/openwrt_release 2>/dev/null",
    "uptime": "cat /proc/uptime",
    "loadavg": "cat /proc/loadavg",
    "stat": "head -n 1 /proc/stat",
    "meminfo": "cat /proc/meminfo",
    "df": "df -P -k 2>/dev/null",
    "netdev": "cat /proc/net/dev",
    "thermal": "for z in /sys/class/thermal/thermal_zone*; do [ -r \"$z/temp\" ] && echo \"$(cat $z/type 2>/dev/null) $(cat $z/temp)\"; done",
    "nproc": "grep -c ^processor /proc/cpuinfo",
}
MARK = "@@yunshu@@"
COMMAND = "; ".join(f"echo '{MARK}{name}'; {cmd}" for name, cmd in SECTIONS.items())
INVENTORY_SECTIONS = {"hostname": SECTIONS["hostname"], "os": SECTIONS["os"],
                      "vendor": "cat /sys/class/dmi/id/sys_vendor 2>/dev/null",
                      "model": "cat /sys/class/dmi/id/product_name 2>/dev/null; cat /sys/firmware/devicetree/base/model 2>/dev/null",
                      "serial": "cat /sys/class/dmi/id/product_serial 2>/dev/null"}
INVENTORY_COMMAND = "; ".join(f"echo '{MARK}{name}'; {cmd}" for name, cmd in INVENTORY_SECTIONS.items())
# 4.3 P18b：已安装软件包清单（只读）。dpkg 同时给出源码包名与版本：发行版安全数据（OSV）按源码包记录。
PACKAGES_SECTIONS = {
    "os": SECTIONS["os"],
    "dpkg": "command -v dpkg-query >/dev/null 2>&1 && dpkg-query -W -f='${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\t${db:Status-Abbrev}\n'",
    "apk": "command -v dpkg-query >/dev/null 2>&1 || { command -v apk >/dev/null 2>&1 && apk info -v 2>/dev/null; }",
    "rpm": "command -v dpkg-query >/dev/null 2>&1 || command -v apk >/dev/null 2>&1 || { command -v rpm >/dev/null 2>&1 && rpm -qa --qf '%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{SOURCERPM}\n'; }",
}
PACKAGES_COMMAND = "; ".join(f"echo '{MARK}{name}'; {cmd}" for name, cmd in PACKAGES_SECTIONS.items())
MAX_PACKAGES = 20000
PSEUDO_FS = ("tmpfs", "devtmpfs", "overlay", "squashfs", "udev", "none", "shm", "efivarfs")


class SshRunner(Protocol):
    def run(self, command: str) -> str: ...

    def close(self) -> None: ...


# ---- paramiko 实现 ----


def host_key_text(key: Any) -> str:
    return f"{key.get_name()} {key.get_base64()}"


class ParamikoRunner:
    def __init__(self, ctx: EndpointContext) -> None:
        import paramiko

        self.ctx = ctx
        # 没有凭据就不去连设备：直接提示待配置，而不是等到连接超时再报采集失败
        if not ctx.username or not ctx.secret:
            raise AdapterError("unconfigured", "SSH 端点没有关联用户名和密码 / 私钥的凭据")
        try:
            sock = socket.create_connection((ctx.address, ctx.port or 22), timeout=ctx.timeout)
        except TimeoutError as exc:
            raise AdapterError("timeout", "SSH 连接超时") from exc
        except OSError as exc:
            raise AdapterError("unreachable", f"无法连接 SSH 端口：{exc.strerror or exc}") from exc
        self.transport = paramiko.Transport(sock)
        try:
            self.transport.start_client(timeout=ctx.timeout)
            ctx.trust.check_ssh(host_key_text(self.transport.get_remote_server_key()))
            self._auth(paramiko)
        except AdapterError:
            self.transport.close()
            raise
        except paramiko.SSHException as exc:
            self.transport.close()
            raise AdapterError("protocol", f"SSH 握手失败：{exc}") from exc

    def _auth(self, paramiko) -> None:  # type: ignore[no-untyped-def]
        ctx = self.ctx
        try:
            if ctx.credential_kind == "ssh_key":
                key = None
                for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
                    try:
                        key = cls.from_private_key(io.StringIO(ctx.secret))
                        break
                    except paramiko.SSHException:
                        continue
                if key is None:
                    raise AdapterError("unconfigured", "无法识别 SSH 私钥格式（支持 Ed25519 / ECDSA / RSA，不支持带口令的私钥）")
                self.transport.auth_publickey(ctx.username, key)
            else:
                self.transport.auth_password(ctx.username, ctx.secret)
        except paramiko.AuthenticationException as exc:
            raise AdapterError("auth", "SSH 用户名或密码 / 私钥不正确") from exc

    def run(self, command: str) -> str:
        chan = self.transport.open_session(timeout=self.ctx.timeout)
        chan.settimeout(self.ctx.timeout)
        try:
            chan.exec_command(command)
            out = bytearray()
            while True:
                try:
                    data = chan.recv(65536)
                except TimeoutError as exc:
                    raise AdapterError("timeout", "SSH 命令执行超时") from exc
                if not data:
                    break
                out += data
                if len(out) > 4 * 1024 * 1024:
                    raise AdapterError("protocol", "命令输出过大")
            return out.decode("utf-8", "replace")
        finally:
            chan.close()

    def close(self) -> None:
        self.transport.close()


# ---- 解析 ----


def split_sections(output: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    current = None
    for line in output.splitlines():
        if line.startswith(MARK):
            current = line[len(MARK):].strip()
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def parse_os(text: str) -> dict[str, str]:
    kv = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip().strip("'\"")
    name = kv.get("PRETTY_NAME") or kv.get("DISTRIB_DESCRIPTION") or kv.get("NAME", "")
    os_id = kv.get("ID") or ("openwrt" if "DISTRIB_ID" in kv else "")
    out = {"os": name, "os_id": os_id.lower()}
    # 4.3 P18：版本号用于选择发行版漏洞数据（Debian:12、Ubuntu:22.04:LTS 等）
    if kv.get("VERSION_ID") or kv.get("DISTRIB_RELEASE"):
        out["os_version"] = kv.get("VERSION_ID") or kv.get("DISTRIB_RELEASE", "")
    if kv.get("VERSION"):
        out["os_version_text"] = kv["VERSION"]
    return out


def parse_packages(sections: dict[str, str]) -> dict[str, Any]:
    """返回 {"manager": dpkg / apk / rpm / "", "items": [[包名, 版本, 源码包, 源码版本], ...]}；只保留已安装的包。"""
    items: list[list[str]] = []
    manager = ""
    for line in sections.get("dpkg", "").splitlines():
        parts = line.split("	")
        if len(parts) < 2 or not parts[0]:
            continue
        if len(parts) >= 5 and parts[4] and not parts[4].startswith("ii"):
            continue  # 已删除只留配置（rc）等不算已安装
        name = parts[0].split(":", 1)[0]
        version = parts[1]
        source = (parts[2] if len(parts) > 2 and parts[2] else name).split(" ", 1)[0]
        source_version = parts[3] if len(parts) > 3 and parts[3] else version
        items.append([name, version, source, source_version])
        manager = "dpkg"
    if not manager:
        for line in sections.get("apk", "").splitlines():
            m = re.match(r"^(.+?)-(\d[^-]*-r\d+)$", line.strip())
            if m:
                items.append([m.group(1), m.group(2), m.group(1), m.group(2)])
                manager = "apk"
    if not manager:
        for line in sections.get("rpm", "").splitlines():
            parts = line.split("	")
            if len(parts) >= 2 and parts[0]:
                version = parts[1].removeprefix("0:")
                src = re.sub(r"-[^-]+-[^-]+\.src\.rpm$", "", parts[2]) if len(parts) > 2 else parts[0]
                items.append([parts[0], version, src or parts[0], version])
                manager = "rpm"
    return {"manager": manager, "items": items[:MAX_PACKAGES], "truncated": len(items) > MAX_PACKAGES}


def parse_meminfo(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1]) * 1024
    return out


def parse_cpu(line: str) -> tuple[int, int] | None:
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    nums = [int(x) for x in parts[1:] if x.isdigit()]
    if len(nums) < 4:
        return None
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return sum(nums), idle


def parse_df(text: str) -> list[tuple[str, int, int]]:
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6 or parts[0] in PSEUDO_FS or parts[0].startswith(("tmpfs", "devtmpfs", "overlay")):
            continue
        try:
            total, used = int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if total > 0:
            rows.append((parts[5], total * 1024, used * 1024))
    return rows


def parse_netdev(text: str) -> dict[str, tuple[int, int, int]]:
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        nums = rest.split()
        if len(nums) >= 16:
            out[name.strip()] = (int(nums[0]), int(nums[8]), int(nums[2]) + int(nums[10]))
    return out


@dataclass
class _Prev:
    value: float
    at: float


class SshAdapter:
    protocol: ClassVar[str] = "ssh"

    def __init__(self, runner_factory: Callable[[EndpointContext], SshRunner] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._factory = runner_factory or ParamikoRunner
        self._clock = clock
        self._prev: dict[tuple[str, int, str], _Prev] = {}
        self._lock = threading.Lock()

    def capabilities(self) -> set[str]:
        return {"system", "cpu", "memory", "disks", "interfaces", "sensors"}

    def _fetch(self, ctx: EndpointContext) -> dict[str, str]:
        runner = self._factory(ctx)
        try:
            return split_sections(runner.run(COMMAND))
        finally:
            runner.close()

    def probe(self, ctx: EndpointContext) -> ProbeResult:
        s = self._fetch(ctx)
        return ProbeResult(ok=True, capabilities=self.capabilities(), facts={"hostname": s.get("hostname", ""), **parse_os(s.get("os", ""))})

    def inventory(self, ctx: EndpointContext) -> ProbeResult:
        runner = self._factory(ctx)
        try:
            s = split_sections(runner.run(INVENTORY_COMMAND))
        finally:
            runner.close()
        facts = {k: s.get(k, "") for k in ("hostname", "vendor", "model", "serial")}
        return ProbeResult(ok=True, facts={**facts, **parse_os(s.get("os", ""))})

    def packages(self, ctx: EndpointContext) -> dict[str, Any]:
        """4.3 P18b：读取已安装软件包清单（只读命令，不修改设备）。"""
        runner = self._factory(ctx)
        try:
            s = split_sections(runner.run(PACKAGES_COMMAND))
        finally:
            runner.close()
        return {**parse_packages(s), **parse_os(s.get("os", ""))}

    def discover(self, ctx: EndpointContext) -> dict[str, Any]:
        s = self._fetch(ctx)
        return {"interfaces": sorted(parse_netdev(s.get("netdev", ""))), "filesystems": [m for m, _, _ in parse_df(s.get("df", ""))]}

    def _delta(self, ctx: EndpointContext, key: str, value: float, now: float) -> tuple[float, float] | None:
        ck = (ctx.address, ctx.port or 22, key)
        with self._lock:
            prev = self._prev.get(ck)
            self._prev[ck] = _Prev(value, now)
        if prev is None or now <= prev.at or value < prev.value:
            return None
        return value - prev.value, now - prev.at

    def collect(self, ctx: EndpointContext) -> CollectResult:
        s = self._fetch(ctx)
        now = self._clock()
        pts: list[Point] = []
        facts = {"hostname": s.get("hostname", ""), **parse_os(s.get("os", ""))}

        if s.get("uptime"):
            pts.append(Point("system.uptime", int(float(s["uptime"].split()[0])), "s"))
        if s.get("loadavg"):
            la = s["loadavg"].split()
            pts.append(Point("load.1m", float(la[0]), ""))
            pts.append(Point("load.5m", float(la[1]), ""))

        cpu = parse_cpu(s.get("stat", ""))
        if cpu:
            total, idle = cpu
            dt = self._delta(ctx, "cpu.total", total, now)
            di = self._delta(ctx, "cpu.idle", idle, now)
            if dt and di and dt[0] > 0:
                pts.append(Point("cpu.load", round((1 - di[0] / dt[0]) * 100, 1), "%"))
        else:
            pts.append(Point("cpu.load", None, "%", quality="unsupported", message="读不到 /proc/stat"))
        if s.get("nproc", "").isdigit():
            facts["cpu_count"] = int(s["nproc"])

        mem = parse_meminfo(s.get("meminfo", ""))
        if mem.get("MemTotal"):
            avail = mem.get("MemAvailable", mem.get("MemFree", 0) + mem.get("Buffers", 0) + mem.get("Cached", 0))
            pts.append(Point("mem.used_pct", round((1 - avail / mem["MemTotal"]) * 100, 1), "%"))
            pts.append(Point("mem.total_bytes", mem["MemTotal"], "B"))
            if mem.get("SwapTotal"):
                pts.append(Point("swap.used_pct", round((1 - mem.get("SwapFree", 0) / mem["SwapTotal"]) * 100, 1), "%"))
        else:
            pts.append(Point("mem.used_pct", None, "%", quality="unsupported", message="读不到 /proc/meminfo"))

        for mount, total, used in parse_df(s.get("df", ""))[:32]:
            pts.append(Point(f"disk.used_pct:{mount}", round(used / total * 100, 1), "%"))

        for name, (rx, tx, errs) in list(parse_netdev(s.get("netdev", "")).items())[:64]:
            if name == "lo":
                continue
            for metric, value in (("if.in_bps", rx), ("if.out_bps", tx)):
                d = self._delta(ctx, f"{metric}:{name}", value, now)
                if d:
                    pts.append(Point(f"{metric}:{name}", round(d[0] * 8 / d[1], 1), "bit/s"))
            pts.append(Point(f"if.errors:{name}", errs, ""))

        temps = []
        for line in s.get("thermal", "").splitlines():
            parts = line.rsplit(None, 1)
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                temps.append((parts[0] or "zone", int(parts[1]) / 1000))
        for zone, t in temps[:16]:
            pts.append(Point(f"temp:{zone}", round(t, 1), "°C"))
        # 只有能认出是 CPU 的温度区才记为 cpu.temp（acpitz、无线网卡等不算）
        cpu_temps = [t for z, t in temps if any(k in z.lower() for k in ("x86_pkg", "cpu", "coretemp", "k10temp", "soc"))]
        if cpu_temps:
            pts.append(Point("cpu.temp", round(max(cpu_temps), 1), "°C"))
        return CollectResult(points=pts, facts=facts)
