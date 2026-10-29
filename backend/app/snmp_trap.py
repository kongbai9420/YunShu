"""SNMP Trap 接收（4.4 P20c，默认关闭）：BMC 主动推送的告警直接写进“服务器告警”。

- 开关：YUNSHU_SNMP_TRAP=1。监听 YUNSHU_SNMP_TRAP_BIND（默认 0.0.0.0）的 UDP YUNSHU_SNMP_TRAP_PORT（默认 1162）；
  BMC 只会发往 162，部署时把主机的 UDP 162 映射到这个端口，平台进程不需要特权端口。
- 由 Worker 进程运行（独立线程）；API 进程不监听。
- 只接受 SNMP v1 / v2c（Trap 与 Inform）。来源地址必须是某台资产管理端点的 IP，团体名必须与该资产
  SNMP 端点关联的团体名凭据一致，或是平台配置 Trap 目标时用的团体名（只存哈希），否则丢弃；SNMPv3 Trap 暂不支持（丢弃并计数）。
- 每个来源每分钟最多收 60 条，超出的丢弃，避免告警风暴拖垮数据库。
- Dell iDRAC 的 Trap 带告警文字与状态（IDRAC-MIB alertMessage / alertCurrentStatus），直接使用；
  其他厂商取 Trap 里的文字变量，级别按关键词判断，判断不出时记为“警告”。
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import socket
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

DEFAULT_PORT = 1162
REFRESH_SECONDS = 60
RATE_PER_MINUTE = 60
SOURCE = "snmp_trap"

SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
TRAP_OID = "1.3.6.1.6.3.1.1.4.1.0"
SKIP_PREFIXES = (SYS_UPTIME, TRAP_OID, "1.3.6.1.6.3.18.1.3.0", "1.3.6.1.6.3.18.1.4.0", "1.3.6.1.6.3.1.1.4.3.0")
DELL_ALERT = "1.3.6.1.4.1.674.10892.5.3.1."
DELL_MESSAGE_ID, DELL_MESSAGE, DELL_STATUS, DELL_FQDD = DELL_ALERT + "1", DELL_ALERT + "2", DELL_ALERT + "3", DELL_ALERT + "6"
# IDRAC-MIB ObjectStatusEnum：1 other、2 unknown、3 ok、4 nonCritical、5 critical、6 nonRecoverable
DELL_SEVERITY = {"4": "warning", "5": "critical", "6": "critical"}
GENERIC = {
    "1.3.6.1.6.3.1.1.5.1": "设备冷启动（coldStart）", "1.3.6.1.6.3.1.1.5.2": "设备热启动（warmStart）",
    "1.3.6.1.6.3.1.1.5.3": "网口断开（linkDown）", "1.3.6.1.6.3.1.1.5.4": "网口恢复（linkUp）",
    "1.3.6.1.6.3.1.1.5.5": "SNMP 认证失败（authenticationFailure）",
}
_RECOVERED = re.compile(r"returned to normal|return(ed)? to (a )?normal|recovered|restored|cleared|\bis ok\b|恢复")
_WARNING = re.compile(r"non-?critical|warn|degrad|predictive|redundancy (is )?(lost|reduced)|告警|警告|降级")
_CRITICAL = re.compile(r"critical|fail|fault|non-?recoverable|error|lost|严重|故障|失败")


def trap_enabled() -> bool:
    return os.environ.get("YUNSHU_SNMP_TRAP", "0") == "1"


def trap_port() -> int:
    try:
        return int(os.environ.get("YUNSHU_SNMP_TRAP_PORT", str(DEFAULT_PORT)))
    except ValueError:
        return DEFAULT_PORT


def trap_target() -> str:
    """BMC 应发往的平台地址（BMC 能访问到的本机 IP），配置 Trap 目标时使用。"""
    return os.environ.get("YUNSHU_SNMP_TRAP_TARGET", "").strip()


@dataclass
class Trap:
    version: str  # v1 / v2c
    community: str
    trap_oid: str
    uptime: str
    varbinds: list[tuple[str, str]]
    response: bytes | None = None  # Inform 需要回应


def _text(value: Any) -> str:
    try:
        raw = value.asOctets() if hasattr(value, "asOctets") else None
    except Exception:  # noqa: BLE001
        raw = None
    if raw is not None:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.hex()
    return value.prettyPrint() if hasattr(value, "prettyPrint") else str(value)


def decode(data: bytes) -> Trap:
    """解析 v1 / v2c 的 Trap 或 Inform；不是这些（含 SNMPv3）时抛 ValueError。"""
    from pyasn1.codec.ber import decoder, encoder
    from pyasn1.error import PyAsn1Error
    from pysnmp.proto import api

    try:
        ver = int(api.decodeMessageVersion(data))
    except (PyAsn1Error, ValueError, TypeError) as exc:
        raise ValueError("不是 SNMP 报文") from exc
    if ver not in api.PROTOCOL_MODULES:
        raise ValueError("v3")
    mod = api.PROTOCOL_MODULES[ver]
    try:
        msg, _ = decoder.decode(data, asn1Spec=mod.Message())
    except PyAsn1Error as exc:
        raise ValueError("报文格式不正确") from exc
    community = _text(mod.apiMessage.get_community(msg))
    pdu = mod.apiMessage.get_pdu(msg)
    if ver == api.SNMP_VERSION_1:
        if not pdu.isSameTypeWith(mod.TrapPDU()):
            raise ValueError("不是 Trap")
        generic, specific = int(mod.apiTrapPDU.get_generic_trap(pdu)), int(mod.apiTrapPDU.get_specific_trap(pdu))
        enterprise = str(mod.apiTrapPDU.get_enterprise(pdu))
        # RFC 3584：v1 Trap 转成 v2 的通知 OID
        oid = f"1.3.6.1.6.3.1.1.5.{generic + 1}" if generic != 6 else f"{enterprise}.0.{specific}"
        binds = [(str(o), _text(v)) for o, v in mod.apiTrapPDU.get_varbinds(pdu)]
        return Trap("v1", community, oid, str(mod.apiTrapPDU.get_timestamp(pdu)), binds)
    inform = pdu.isSameTypeWith(mod.InformRequestPDU())
    if not (inform or pdu.isSameTypeWith(mod.SNMPv2TrapPDU())):
        raise ValueError("不是 Trap")
    binds = [(str(o), _text(v)) for o, v in mod.apiPDU.get_varbinds(pdu)]
    values = dict(binds)
    response = None
    if inform:
        rsp = mod.apiMessage.get_response(msg)
        mod.apiPDU.set_varbinds(mod.apiMessage.get_pdu(rsp), mod.apiPDU.get_varbinds(pdu))
        response = encoder.encode(rsp)
    return Trap("v2c", community, values.get(TRAP_OID, ""), values.get(SYS_UPTIME, ""), binds, response)


def _severity_from_text(text: str) -> str:
    low = text.lower()
    if _RECOVERED.search(low):
        return "info"
    if _WARNING.search(low):
        return "warning"
    if _CRITICAL.search(low):
        return "critical"
    return "warning"


def to_entry(trap: Trap, now: datetime) -> dict[str, Any]:
    """转成 server_events.record 的条目格式。"""
    values = dict(trap.varbinds)
    message = values.get(DELL_MESSAGE, "").strip()
    if message:
        severity = DELL_SEVERITY.get(values.get(DELL_STATUS, "").strip(), "info")
        extra = " ".join(x for x in (values.get(DELL_MESSAGE_ID, "").strip(), values.get(DELL_FQDD, "").strip()) if x)
        if extra:
            message = f"{message}（{extra}）"
    elif trap.trap_oid in GENERIC:
        message = GENERIC[trap.trap_oid]
        severity = "info" if trap.trap_oid in ("1.3.6.1.6.3.1.1.5.2", "1.3.6.1.6.3.1.1.5.4") else "warning"
    else:
        texts = [v.strip() for o, v in trap.varbinds if not o.startswith(SKIP_PREFIXES) and v.strip() and not re.fullmatch(r"[\d.:-]+", v.strip())]
        message = "；".join(texts)[:800] or f"收到 Trap {trap.trap_oid or '（未知类型）'}"
        severity = _severity_from_text(message)
    return {"id": f"{trap.trap_oid}#{trap.uptime}"[:100], "created": now, "severity": severity, "message": message}


# ---- 来源识别 ----


@dataclass
class Directory:
    """来源 IP → [(资产, 可接受的团体名)]，每分钟从库里刷新。"""

    by_ip: dict[str, list[tuple[uuid.UUID, set[str]]]] = field(default_factory=dict)
    loaded_at: float = 0.0

    @staticmethod
    def build(db: Session, reveal: Callable[[Any], str]) -> Directory:
        from app.models import Asset, BmcEventSubscription, Credential, ManagementEndpoint

        communities: dict[uuid.UUID, set[str]] = defaultdict(set)
        addresses: dict[str, set[uuid.UUID]] = defaultdict(set)
        rows = db.execute(select(ManagementEndpoint, Credential).outerjoin(Credential, Credential.id == ManagementEndpoint.credential_id)
                          .join(Asset, Asset.id == ManagementEndpoint.asset_id).where(Asset.lifecycle != "retired")).all()
        for ep, cred in rows:
            if ep.protocol in ("snmp", "redfish", "ipmi"):
                try:
                    addresses[str(ipaddress.ip_address(ep.address.strip()))].add(ep.asset_id)
                except ValueError:
                    pass  # 主机名不参与识别：Trap 只认 IP
            if cred is not None and cred.kind == "snmp_community":
                try:
                    communities[ep.asset_id].add(reveal(cred))
                except Exception:  # noqa: BLE001 - 解不开的凭据当作没有
                    log.warning("团体名凭据 %s 无法解密，Trap 接收忽略它", cred.id)
        for sub in db.scalars(select(BmcEventSubscription).where(BmcEventSubscription.kind == SOURCE, BmcEventSubscription.token_hash != "")):
            communities[sub.asset_id].add("sha256:" + sub.token_hash)
        out = {ip: [(a, communities.get(a, set())) for a in sorted(ids)] for ip, ids in addresses.items()}
        return Directory(out, time.monotonic())

    def match(self, ip: str, community: str) -> uuid.UUID | str:
        """返回资产编号，或丢弃原因。"""
        candidates = self.by_ip.get(ip)
        if not candidates:
            return "unknown_source"
        hashed = "sha256:" + hashlib.sha256(community.encode()).hexdigest()
        for asset_id, allowed in candidates:
            if community and (community in allowed or hashed in allowed):
                return asset_id
        return "bad_community"


class TrapReceiver:
    def __init__(self, session_factory: Callable[[], Session], reveal: Callable[[Any], str], *, port: int | None = None,
                 bind: str | None = None) -> None:
        self.session_factory, self.reveal = session_factory, reveal
        self.port = trap_port() if port is None else port
        self.bind = bind if bind is not None else os.environ.get("YUNSHU_SNMP_TRAP_BIND", "0.0.0.0")
        self.directory = Directory()
        self.stats: dict[str, int] = defaultdict(int)
        self._recent: dict[str, deque[float]] = defaultdict(deque)
        self._stop = threading.Event()
        self.ready = threading.Event()

    def _directory(self) -> Directory:
        if time.monotonic() - self.directory.loaded_at > REFRESH_SECONDS:
            with self.session_factory() as db:
                self.directory = Directory.build(db, self.reveal)
        return self.directory

    def _rate_ok(self, ip: str) -> bool:
        q, now = self._recent[ip], time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_PER_MINUTE:
            return False
        q.append(now)
        return True

    def handle(self, data: bytes, ip: str, *, now: datetime | None = None) -> tuple[str, bytes | None]:
        """处理一个报文，返回 (结果, 需要回给发送方的报文)。结果：accepted / duplicate 或丢弃原因。"""
        from app import server_events
        from app.models import BmcEventSubscription

        self.stats["received"] += 1
        try:
            trap = decode(data)
        except ValueError as exc:
            reason = "v3" if str(exc) == "v3" else "malformed"
            self.stats[reason] += 1
            return reason, None
        found = self._directory().match(ip, trap.community)
        if isinstance(found, str):
            self.stats[found] += 1
            return found, None
        if not self._rate_ok(ip):
            self.stats["rate_limited"] += 1
            return "rate_limited", trap.response
        now = now or datetime.now(UTC)
        with self.session_factory() as db:
            entry = to_entry(trap, now)
            added = server_events.record(db, found, SOURCE, [entry], now)
            if added and entry["severity"] in ("critical", "warning"):
                from app.collect import boost

                boost.on_event(db, found, SOURCE, now)  # 4.4 P20d：立即补采并临时提频
            sub = db.scalar(select(BmcEventSubscription).where(BmcEventSubscription.asset_id == found, BmcEventSubscription.kind == SOURCE))
            if sub is not None:
                sub.last_event_at, sub.events_received = now, sub.events_received + 1
            db.commit()
        self.stats["accepted" if added else "duplicate"] += 1
        return ("accepted" if added else "duplicate"), trap.response

    def serve(self) -> None:
        sock = socket.socket(socket.AF_INET6 if ":" in self.bind else socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.bind, self.port))
        sock.settimeout(1.0)
        self.port = sock.getsockname()[1]
        self.ready.set()
        log.info("SNMP Trap 接收已开启：UDP %s:%s", self.bind, self.port)
        try:
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(65535)
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                try:
                    result, reply = self.handle(data, str(ipaddress.ip_address(addr[0].split("%")[0])).removeprefix("::ffff:"))
                    if reply:
                        sock.sendto(reply, addr)
                    if result not in ("accepted", "duplicate"):
                        log.debug("丢弃来自 %s 的 Trap：%s", addr[0], result)
                except Exception:  # noqa: BLE001 - 单条出错不影响后续接收
                    log.exception("处理来自 %s 的 Trap 出错", addr[0])
        finally:
            sock.close()

    def stop(self) -> None:
        self._stop.set()


def start_if_enabled(session_factory: Callable[[], Session], cipher_factory: Callable[[], Any]) -> TrapReceiver | None:
    if not trap_enabled():
        return None
    from app.services.credentials import reveal_secret

    receiver = TrapReceiver(session_factory, lambda cred: reveal_secret(cred, cipher_factory()))

    def run() -> None:
        try:
            receiver.serve()
        except OSError:
            log.exception("SNMP Trap 接收无法监听 UDP %s:%s（端口被占用或没有权限），Trap 不会被接收", receiver.bind, receiver.port)

    threading.Thread(target=run, name="yunshu-snmp-trap", daemon=True).start()
    return receiver
