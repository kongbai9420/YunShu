"""采集适配器统一接口。

适配器只读：probe（测试连接与识别）、discover（发现接口、传感器或关联对象）、collect（采集指标）、
capabilities（声明能力）。控制操作是另一套接口（validate / execute / verify），P5 才有，
任何采集适配器都不得发出写命令。

凭据明文只在 EndpointContext 里、只在内存中传给适配器，不写日志、不进结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, ClassVar, Protocol

from app.metrics import Point

CAPABILITIES = ("sensors", "fans", "power", "cpu", "memory", "disks", "raid", "interfaces", "vms", "system")


class AdapterError(Exception):
    """kind: unreachable / timeout / auth / unsupported / protocol / untrusted / unconfigured"""

    RETRYABLE = frozenset({"unreachable", "timeout"})

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def retryable(self) -> bool:
        return self.kind in self.RETRYABLE


@dataclass
class Trust:
    """首次信任并固定：没有固定值时记下首次看到的指纹；之后不一致就拒绝连接。"""

    tls_fingerprint: str = ""
    ssh_host_key: str = ""
    observed_tls: str = ""
    observed_ssh: str = ""

    def check_tls(self, fingerprint: str) -> None:
        self.observed_tls = fingerprint
        if self.tls_fingerprint and fingerprint != self.tls_fingerprint:
            raise AdapterError("untrusted", "TLS 证书指纹与已固定的不一致，可能是证书更换或中间人；请在端点设置里确认后重新信任")

    def check_ssh(self, host_key: str) -> None:
        self.observed_ssh = host_key
        if self.ssh_host_key and host_key != self.ssh_host_key:
            raise AdapterError("untrusted", "SSH 主机密钥与已固定的不一致，可能是重装系统或中间人；请在端点设置里确认后重新信任")


@dataclass
class EndpointContext:
    address: str
    port: int | None
    protocol: str
    username: str = ""
    secret: str = ""
    credential_kind: str = ""
    timeout: float = 10.0
    tls_verify: bool = True
    trust: Trust = field(default_factory=Trust)
    options: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:  # 不把密码打进日志或异常
        return f"EndpointContext({self.protocol}://{self.address}:{self.port})"


@dataclass
class ProbeResult:
    ok: bool
    capabilities: set[str] = field(default_factory=set)
    facts: dict[str, Any] = field(default_factory=dict)
    message: str = ""


@dataclass
class CollectResult:
    points: list[Point] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)


class Adapter(Protocol):
    protocol: ClassVar[str]

    def capabilities(self) -> set[str]: ...

    def probe(self, ctx: EndpointContext) -> ProbeResult: ...

    def discover(self, ctx: EndpointContext) -> dict[str, Any]: ...

    def collect(self, ctx: EndpointContext) -> CollectResult: ...


# ---- 采集模板（A / B 批，落地方案 P2） ----


@dataclass(frozen=True)
class Step:
    protocol: str
    # primary：首选；fallback：前面的都失败时才用（降级并标出）；supplement：总是额外采集（补充数据源）
    mode: str = "primary"
    # 补充步骤每隔几个采集周期执行一次（4.2 P13：Redfish 补充降频，减少 BMC 负担）
    every: int = 1


@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    steps: tuple[Step, ...]
    interval: int = 60
    batch: str = "A"
    verified: bool = False  # 只有在真实设备或脱敏样本上测过才标“已验证”
    platforms: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    prerequisites: str = ""
    limitations: str = ""
    evidence: str = ""


def _load_profiles() -> dict[str, Profile]:
    """内置声明式模板，仅允许已注册的只读协议；不加载脚本或任意命令。"""
    items = json.loads(Path(__file__).with_name("profiles.json").read_text(encoding="utf-8"))
    profiles = {}
    for item in items:
        steps = tuple(Step(**s) for s in item.pop("steps"))
        if any(s.protocol not in {"snmp", "ssh", "ipmi", "redfish", "pve-api", "esxi-api", "reach"}
               or s.mode not in {"primary", "fallback", "supplement"} or s.every < 1
               or (s.every > 1 and s.mode != "supplement") for s in steps):
            raise ValueError("采集模板包含未支持的协议或步骤")
        p = Profile(**item, steps=steps)
        if p.name in profiles or not steps or p.interval < 10:
            raise ValueError("采集模板名称重复、缺少步骤或周期过短")
        profiles[p.name] = p
    return profiles


PROFILES = _load_profiles()


def infer_profile(platform: str, role: str, protocols: set[str]) -> str | None:
    """资产没有指定采集模板时按平台和已有端点推断。"""
    p = platform.lower()
    if "idrac" in p or ("dell" in p and protocols.intersection({"ipmi", "snmp", "redfish"})):
        return "dell-server"
    # 4.2 P15：UPS、RouterOS、群晖（都走 SNMP）
    if "snmp" in protocols and (role == "ups" or "ups" in p.split()):
        return "ups-snmp"
    if "snmp" in protocols and ("routeros" in p or "mikrotik" in p):
        return "routeros"
    if "snmp" in protocols and ("synology" in p or "dsm" in p.split() or "群晖" in p):
        return "nas-synology"
    if any(x in p for x in ("openwrt", "immortalwrt", "istoreos")):
        return "openwrt"
    if "proxmox" in p or p == "pve" or p.startswith("pve ") or "pve-api" in protocols:
        return "pve"
    if "esxi" in p or "vmware" in p or "esxi-api" in protocols:
        return "esxi"
    if any(x in p for x in ("alpine", "rocky", "almalinux", "centos")):
        return "linux-b"
    if any(x in p for x in ("debian", "ubuntu", "linux")) and protocols.intersection({"snmp", "ssh"}):
        return "linux"
    if role in ("switch", "router", "ap", "firewall") and "snmp" in protocols:
        return "snmp-switch"
    if protocols.intersection({"ipmi", "redfish"}):
        return "generic-bmc"
    if "ssh" in protocols:
        return "linux"
    if "snmp" in protocols:
        return "snmp-switch"
    return None
