"""SNMP 只读采集（v2c / v3，v3 优先；只发 GET / GETBULK，从不 SET）。

覆盖标准 MIB：
- SNMPv2-MIB：系统名称、描述、运行时间
- IF-MIB：接口名称、运行状态、64 位收发字节计数（换算成 bit/s）、错误计数
- HOST-RESOURCES-MIB：CPU 负载、内存与磁盘使用率
- UCD-SNMP-MIB（net-snmp，Linux / OpenWrt）：1 分钟平均负载、可用内存

设备不支持某张表时对应指标标为 unsupported，不报错、不写 0。

凭据：
- snmp_community：密钥就是团体名（v2c）
- snmpv3：用户名是 USM 用户；密钥是 JSON：{"auth_key": "...", "priv_key": "...",
  "auth_protocol": "SHA|SHA256|SHA512|MD5", "priv_protocol": "AES|AES256|DES"}，后两项可省略（默认 SHA / AES）
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from app.collect.base import AdapterError, CollectResult, EndpointContext, ProbeResult
from app.collect import snmp_vendors as vendors
from app.metrics import Point

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_OPER = "1.3.6.1.2.1.2.2.1.8"
IF_IN_ERR = "1.3.6.1.2.1.2.2.1.14"
IF_OUT_ERR = "1.3.6.1.2.1.2.2.1.20"
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_HC_IN = "1.3.6.1.2.1.31.1.1.1.6"
IF_HC_OUT = "1.3.6.1.2.1.31.1.1.1.10"
IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"
HR_PROC_LOAD = "1.3.6.1.2.1.25.3.3.1.2"
HR_STORAGE_TYPE = "1.3.6.1.2.1.25.2.3.1.2"
HR_STORAGE_DESCR = "1.3.6.1.2.1.25.2.3.1.3"
HR_STORAGE_UNITS = "1.3.6.1.2.1.25.2.3.1.4"
HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"
HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"
HR_STORAGE_RAM = "1.3.6.1.2.1.25.2.1.2"
HR_STORAGE_FIXED = "1.3.6.1.2.1.25.2.1.4"
UCD_LOAD1 = "1.3.6.1.4.1.2021.10.1.3.1"
UCD_MEM_TOTAL = "1.3.6.1.4.1.2021.4.5.0"
UCD_MEM_AVAIL = "1.3.6.1.4.1.2021.4.6.0"

# Dell iDRAC（IDRAC-MIB-SMIv2，企业号 674.10892.5；4.2 P13）：SNMP 无登录会话，不会在 iDRAC 留下登录日志。
# 依据 Dell 公开的 MIB 定义编写，未实机验证；读不到的表按“不支持”记录，不猜测。
IDRAC = "1.3.6.1.4.1.674.10892.5"
IDRAC_GLOBAL_STATUS = IDRAC + ".2.1.0"
IDRAC_POWER_STATE = IDRAC + ".2.4.0"
# 4.3 P18：固件版本与 iDRAC 代次（racShortName，如 “iDRAC9”）、服务器型号
IDRAC_SHORT_NAME = IDRAC + ".1.1.2.0"
IDRAC_FIRMWARE = IDRAC + ".1.1.8.0"
IDRAC_SYSTEM_MODEL = IDRAC + ".1.3.12.0"
IDRAC_TEMP = IDRAC + ".4.700.20.1"      # temperatureProbeTable：.5 状态 / .6 读数（0.1 ℃）/ .8 位置
IDRAC_FAN = IDRAC + ".4.700.12.1"       # coolingDeviceTable：.5 状态 / .6 读数（RPM）/ .8 位置
IDRAC_PSU = IDRAC + ".4.600.12.1"       # powerSupplyTable：.5 状态 / .8 位置
IDRAC_AMP = IDRAC + ".4.600.30.1"       # amperageProbeTable：.6 读数 / .7 类型（26 = 整机功率，单位 W）/ .8 位置
IDRAC_MEM = IDRAC + ".4.1100.50.1"      # memoryDeviceTable：.5 状态 / .8 位置
IDRAC_PDISK = IDRAC + ".5.1.20.130.4.1"  # physicalDiskTable：.2 名称 / .24 组件状态
IDRAC_VDISK = IDRAC + ".5.1.20.140.1.1"  # virtualDiskTable：.2 名称 / .20 组件状态
# ObjectStatusEnum：3 正常、4 非严重、5 严重、6 不可恢复；1 / 2（其他 / 未知）不换算
IDRAC_STATUS = {3: (1, "OK"), 4: (0.5, "Warning"), 5: (0, "Critical"), 6: (0, "NonRecoverable")}
LLDP_REMOTE_CHASSIS = "1.0.8802.1.1.2.1.4.1.1.5"
LLDP_REMOTE_PORT = "1.0.8802.1.1.2.1.4.1.1.7"
LLDP_REMOTE_NAME = "1.0.8802.1.1.2.1.4.1.1.9"

# 交换机 MAC 地址表与 ARP 表（4.2 P14，按 MAC / IP 找端口）：BRIDGE-MIB / Q-BRIDGE-MIB / IP-MIB，只读
BRIDGE_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"
FDB_STATUS = "1.3.6.1.2.1.17.4.3.1.3"  # 3 learned / 4 self（交换机自己的 MAC，不记录）
QFDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"
ARP_MAC = "1.3.6.1.2.1.4.22.1.2"
MAX_FDB = 8192
FDB_EVERY_SECONDS = 600  # MAC / ARP 表变化慢，每台设备 10 分钟读一次
MAX_INTERFACES = 128  # 大交换机只取前 128 个接口，避免一次采集过大


class SnmpClient(Protocol):
    def get(self, oids: list[str]) -> dict[str, Any]: ...

    def walk(self, oid: str) -> dict[str, Any]: ...


# ---- pysnmp 实现 ----


class PysnmpClient:
    def __init__(self, ctx: EndpointContext) -> None:
        from pysnmp.hlapi.v3arch import asyncio as h

        self.h = h
        self.ctx = ctx
        self.auth = self._auth(ctx)
        # pysnmp 的引擎和传输绑定在创建它们的事件循环上：一次采集的所有请求共用一个循环，结束后关闭
        self.loop = asyncio.new_event_loop()
        self.engine = h.SnmpEngine()
        self._target_obj = None

    def _context(self):  # type: ignore[no-untyped-def]
        # v3 上下文名：大多数设备为空；端点选项 context 可指定
        return self.h.ContextData(contextName=self.ctx.options.get("context", ""))

    def close(self) -> None:
        try:
            self.engine.close_dispatcher()
        except Exception:  # noqa: BLE001
            pass
        self.loop.close()

    def _run(self, coro):  # type: ignore[no-untyped-def]
        return self.loop.run_until_complete(coro)

    def _auth(self, ctx: EndpointContext):  # type: ignore[no-untyped-def]
        h = self.h
        if ctx.credential_kind == "snmpv3":
            try:
                cfg = json.loads(ctx.secret)
            except ValueError as exc:
                raise AdapterError("unconfigured", "SNMPv3 凭据格式不正确：密钥应为 JSON（auth_key、priv_key 等）") from exc
            auth = {"SHA": h.usmHMACSHAAuthProtocol, "SHA256": h.usmHMAC192SHA256AuthProtocol, "SHA512": h.usmHMAC384SHA512AuthProtocol, "MD5": h.usmHMACMD5AuthProtocol}
            priv = {"AES": h.usmAesCfb128Protocol, "AES256": h.usmAesCfb256Protocol, "DES": h.usmDESPrivProtocol}
            auth_key, priv_key = cfg.get("auth_key"), cfg.get("priv_key")
            return h.UsmUserData(
                ctx.username,
                authKey=auth_key,
                privKey=priv_key,
                authProtocol=auth[cfg.get("auth_protocol", "SHA")] if auth_key else h.usmNoAuthProtocol,
                privProtocol=priv[cfg.get("priv_protocol", "AES")] if priv_key else h.usmNoPrivProtocol,
            )
        if ctx.credential_kind == "snmp_community" and ctx.secret:
            return h.CommunityData(ctx.secret, mpModel=1)
        raise AdapterError("unconfigured", "SNMP 端点没有关联凭据：请关联团体名（v2c）或 SNMPv3 凭据")

    async def _target(self):  # type: ignore[no-untyped-def]
        if self._target_obj is None:
            self._target_obj = await self.h.UdpTransportTarget.create(
                (self.ctx.address, self.ctx.port or 161), timeout=min(self.ctx.timeout, 5), retries=1
            )
        return self._target_obj

    @staticmethod
    def _value(v: Any) -> Any:
        cls = type(v).__name__
        if cls in ("NoSuchObject", "NoSuchInstance", "EndOfMibView"):
            return None
        if cls in ("OctetString",):
            raw = bytes(v)
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.hex()
        try:
            return int(v)
        except (TypeError, ValueError):
            return str(v)

    def _check(self, err_ind, err_status, err_index) -> None:  # type: ignore[no-untyped-def]
        if err_ind:
            text = str(err_ind)
            if "timeout" in text.lower() or "No SNMP response" in text:
                raise AdapterError("timeout", "SNMP 无响应（地址、端口、团体名或 v3 参数可能不正确）")
            if "authorization" in text.lower() or "unknownUserName" in text or "wrongDigest" in text or "decryption" in text.lower():
                raise AdapterError("auth", f"SNMP 认证失败：{text}")
            raise AdapterError("protocol", f"SNMP 错误：{text}")
        if err_status:
            raise AdapterError("protocol", f"SNMP 错误：{err_status.prettyPrint()}")

    def get(self, oids: list[str]) -> dict[str, Any]:
        h = self.h

        async def go():  # type: ignore[no-untyped-def]
            target = await self._target()
            ei, es, ix, binds = await h.get_cmd(self.engine, self.auth, target, self._context(), *[h.ObjectType(h.ObjectIdentity(o)) for o in oids])
            self._check(ei, es, ix)
            return {str(name): self._value(val) for name, val in binds}

        return self._run(go())

    def walk(self, oid: str) -> dict[str, Any]:
        h = self.h

        async def go():  # type: ignore[no-untyped-def]
            target = await self._target()
            out: dict[str, Any] = {}
            async for ei, es, ix, binds in h.bulk_walk_cmd(
                self.engine, self.auth, target, self._context(), 0, 25, h.ObjectType(h.ObjectIdentity(oid)), lexicographicMode=False
            ):
                self._check(ei, es, ix)
                for name, val in binds:
                    out[str(name)] = self._value(val)
                if len(out) > 4 * MAX_INTERFACES:
                    break
            return out

        return self._run(go())


# ---- 适配器 ----


def _index(table: dict[str, Any], base: str) -> dict[str, Any]:
    """把 walk 结果 {base.idx: v} 转成 {idx: v}。"""
    prefix = base + "."
    return {k[len(prefix):]: v for k, v in table.items() if k.startswith(prefix) and v is not None}


@dataclass
class _Counter:
    value: int
    at: float


class SnmpAdapter:
    protocol: ClassVar[str] = "snmp"

    def __init__(self, client_factory: Callable[[EndpointContext], SnmpClient] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._factory = client_factory or PysnmpClient
        self._clock = clock
        self._counters: dict[tuple[str, int, str], _Counter] = {}
        self._lock = threading.Lock()

    def capabilities(self) -> set[str]:
        return {"system", "interfaces", "cpu", "memory", "disks"}

    def probe(self, ctx: EndpointContext) -> ProbeResult:
        client = self._factory(ctx)
        try:
            sys = client.get([SYS_NAME, SYS_DESCR])
        finally:
            getattr(client, "close", lambda: None)()
        return ProbeResult(ok=True, capabilities=self.capabilities(), facts={"sys_name": sys.get(SYS_NAME), "sys_descr": sys.get(SYS_DESCR)})

    def discover(self, ctx: EndpointContext) -> dict[str, Any]:
        client = self._factory(ctx)
        try:
            names = _index(client.walk(IF_NAME), IF_NAME) or _index(client.walk(IF_DESCR), IF_DESCR)
            return {"interfaces": [{"index": i, "name": n} for i, n in list(names.items())[:MAX_INTERFACES]],
                    "lldp_neighbors": self._lldp(client)}
        finally:
            getattr(client, "close", lambda: None)()

    def inventory(self, ctx: EndpointContext) -> ProbeResult:
        client = self._factory(ctx)
        try:
            sys = client.get([SYS_NAME, SYS_DESCR])
            facts = {"sys_name": sys.get(SYS_NAME)}
            # ENTITY-MIB：只使用明确标为 chassis(3) 的实体，避免误用线卡 / 电源的序列号。
            base = "1.3.6.1.2.1.47.1.1.1.1"
            try:
                chassis = [i for i, value in _index(client.walk(base + ".5"), base + ".5").items() if str(value) == "3"]
                if len(chassis) == 1:
                    suffix = "." + chassis[0]
                    values = client.get([base + f".{column}" + suffix for column in (11, 12, 13)])
                    facts.update({field: values.get(base + f".{column}" + suffix) for field, column in (("serial", 11), ("vendor", 12), ("model", 13))})
            except AdapterError as exc:
                if exc.kind != "unsupported":
                    raise
            return ProbeResult(ok=True, facts=facts)
        finally:
            getattr(client, "close", lambda: None)()

    @staticmethod
    def _lldp(client: SnmpClient) -> list[dict[str, str]]:
        # 只保存带本地端口和远端证据的观察，不据此写入已确认物理链路。
        names = _index(client.walk(LLDP_REMOTE_NAME), LLDP_REMOTE_NAME)
        ports = _index(client.walk(LLDP_REMOTE_PORT), LLDP_REMOTE_PORT)
        chassis = _index(client.walk(LLDP_REMOTE_CHASSIS), LLDP_REMOTE_CHASSIS)
        neighbors = []
        for idx in sorted(set(names) | set(ports) | set(chassis))[:MAX_INTERFACES]:
            parts = str(idx).split(".")
            if len(parts) != 3:
                continue
            neighbors.append({"local_port": parts[1], "remote_name": str(names.get(idx, ""))[:200],
                              "remote_port": str(ports.get(idx, ""))[:200], "remote_chassis": str(chassis.get(idx, ""))[:200]})
        return neighbors

    @staticmethod
    def _mac(value: Any) -> str | None:
        """MAC 统一为 12 位小写十六进制。pysnmp 把能按 UTF-8 解码的 6 字节也解成了字符串，这里还原。"""
        if isinstance(value, str):
            text = value.replace(":", "").replace("-", "").replace(" ", "").lower()
            if len(text) == 12 and all(c in "0123456789abcdef" for c in text):
                return text
            raw = value.encode("utf-8", errors="ignore")
            return raw.hex() if len(raw) == 6 else None
        if isinstance(value, bytes | bytearray) and len(value) == 6:
            return bytes(value).hex()
        return None

    def _fdb_arp(self, ctx: EndpointContext, client: SnmpClient, names: dict[str, Any]) -> dict[str, list[dict[str, Any]]] | None:
        """读 MAC 地址表与 ARP 表；同一设备 10 分钟内只读一次，其余周期返回 None。"""
        now = self._clock()
        key = (ctx.address, ctx.port or 161, "fdb")
        with self._lock:
            last = self._counters.get(key)
            if last is not None and now - last.at < FDB_EVERY_SECONDS:
                return None
            self._counters[key] = _Counter(0, now)
        bridge = {k: str(v) for k, v in _index(client.walk(BRIDGE_PORT_IFINDEX), BRIDGE_PORT_IFINDEX).items()}

        def port_name(bridge_port: Any) -> str:
            ifindex = bridge.get(str(bridge_port), str(bridge_port))
            return str(names.get(ifindex) or f"if{ifindex}")[:200]

        fdb: list[dict[str, Any]] = []
        for idx, port in _index(client.walk(QFDB_PORT), QFDB_PORT).items():
            parts = idx.split(".")
            if len(parts) == 7 and int(port or 0) > 0:
                fdb.append({"mac": "".join(f"{int(p):02x}" for p in parts[1:]), "port": port_name(port), "vlan": int(parts[0])})
        if not fdb:
            status = _index(client.walk(FDB_STATUS), FDB_STATUS)
            for idx, port in _index(client.walk(FDB_PORT), FDB_PORT).items():
                parts = idx.split(".")
                if len(parts) == 6 and int(port or 0) > 0 and str(status.get(idx, 3)) == "3":
                    fdb.append({"mac": "".join(f"{int(p):02x}" for p in parts), "port": port_name(port), "vlan": None})
        arp: list[dict[str, Any]] = []
        for idx, value in _index(client.walk(ARP_MAC), ARP_MAC).items():
            parts = idx.split(".")
            mac = self._mac(value)
            if len(parts) == 5 and mac and mac != "000000000000":
                arp.append({"mac": mac, "ip": ".".join(parts[1:]), "port": str(names.get(parts[0]) or f"if{parts[0]}")[:200]})
        return {"fdb": fdb[:MAX_FDB], "arp": arp[:MAX_FDB]}

    def _rate(self, ctx: EndpointContext, key: str, value: int) -> float | None:
        """64 位计数器换算成每秒速率；首次采集或计数器回绕 / 重置时返回 None。"""
        now = self._clock()
        ck = (ctx.address, ctx.port or 161, key)
        with self._lock:
            prev = self._counters.get(ck)
            self._counters[ck] = _Counter(value, now)
        if prev is None or now <= prev.at or value < prev.value:
            return None
        return (value - prev.value) / (now - prev.at)

    def collect(self, ctx: EndpointContext) -> CollectResult:
        client = self._factory(ctx)
        try:
            return self._collect(ctx, client)
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    @staticmethod
    def _status_point(key: str, raw: Any) -> Point:
        try:
            value, label = IDRAC_STATUS[int(raw)]
        except (KeyError, TypeError, ValueError):
            return Point(key, None, "", quality="unsupported", message="状态未知")
        return Point(key, value, "", message=label)

    def _idrac(self, client: SnmpClient, head: dict[str, Any]) -> list[Point] | None:
        """iDRAC 机型：读整机状态、温度、风扇、电源模块、功率、内存与磁盘 / RAID；不是 iDRAC 时返回 None。"""
        if head.get(IDRAC_GLOBAL_STATUS) is None:
            return None
        pts = [self._status_point("health.system", head[IDRAC_GLOBAL_STATUS])]
        if head.get(IDRAC_POWER_STATE) in (3, 4):
            pts.append(Point("power.on", 1 if head[IDRAC_POWER_STATE] == 4 else 0, ""))

        def table(base: str, *cols: int) -> list[tuple[str, dict[int, Any]]]:
            data = {c: _index(client.walk(f"{base}.{c}"), f"{base}.{c}") for c in cols}
            return [(idx, {c: data[c].get(idx) for c in cols}) for idx in sorted(set().union(*data.values()))]

        def label(row: dict[int, Any], col: int, idx: str, default: str) -> str:
            return str(row.get(col) or f"{default}{idx}").strip().replace(" ", "_")[:120]

        cpu = []
        for idx, row in table(IDRAC_TEMP, 6, 8):
            name = label(row, 8, idx, "temp")
            if row[6] is None:
                pts.append(Point(f"temp:{name}", None, "°C", quality="unsupported"))
                continue
            value = int(row[6]) / 10
            pts.append(Point(f"temp:{name}", value, "°C"))
            if "cpu" in name.lower():
                cpu.append(value)
        if cpu:
            pts.append(Point("cpu.temp", max(cpu), "°C"))
        for idx, row in table(IDRAC_FAN, 6, 8):
            name = label(row, 8, idx, "fan")
            pts.append(Point(f"fan.rpm:{name}", float(row[6]), "RPM") if row[6] is not None else Point(f"fan.rpm:{name}", None, "RPM", quality="unsupported"))
        for idx, row in table(IDRAC_PSU, 5, 8):
            pts.append(self._status_point(f"psu.health:{label(row, 8, idx, 'psu')}", row[5]))
        for idx, row in table(IDRAC_AMP, 6, 7):
            if str(row[7]) == "26" and row[6] is not None and not any(p.key == "power.watts" for p in pts):
                pts.append(Point("power.watts", float(row[6]), "W"))
        for idx, row in table(IDRAC_MEM, 5, 8):
            pts.append(self._status_point(f"mem.health:{label(row, 8, idx, 'dimm')}", row[5]))
        for idx, row in table(IDRAC_PDISK, 2, 24)[:64]:
            pts.append(self._status_point(f"disk.health:{label(row, 2, idx, 'disk')}", row[24]))
        for idx, row in table(IDRAC_VDISK, 2, 20)[:32]:
            pts.append(self._status_point(f"raid.health:{label(row, 2, idx, 'volume')}", row[20]))
        return pts

    def _collect(self, ctx: EndpointContext, client: SnmpClient) -> CollectResult:
        points: list[Point] = []
        messages: list[str] = []
        sys = client.get([SYS_NAME, SYS_DESCR, SYS_UPTIME, vendors.SYS_OBJECT_ID])
        facts = {"sys_name": sys.get(SYS_NAME), "sys_descr": sys.get(SYS_DESCR)}
        # 一次 GET 识别 iDRAC / UPS / 群晖（4.2 P13、P15）
        try:
            probe = client.get([IDRAC_GLOBAL_STATUS, IDRAC_POWER_STATE, *vendors.PROBE_OIDS])
        except AdapterError as exc:
            # SNMPv1 设备对未知 OID 报 noSuchName（协议错误）：说明不是这些设备，继续按通用设备采集
            if exc.kind not in ("unsupported", "protocol"):
                raise
            probe = {}
        idrac = self._idrac(client, probe)
        ups = vendors.ups(client, probe) if idrac is None else None
        if ups is not None:
            facts["ups"] = True
            return CollectResult(points=ups, facts=facts, messages=messages)
        if idrac is not None:
            # BMC 的接口与 HOST-RESOURCES 指标没有意义，只采集硬件部分
            facts["idrac"] = True
            try:
                info = client.get([IDRAC_SHORT_NAME, IDRAC_FIRMWARE, IDRAC_SYSTEM_MODEL])
                facts.update({k: str(info[oid]).strip() for k, oid in (("idrac_name", IDRAC_SHORT_NAME), ("idrac_firmware", IDRAC_FIRMWARE),
                                                                    ("system_model", IDRAC_SYSTEM_MODEL)) if info.get(oid)})
            except AdapterError as exc:
                if exc.kind not in ("unsupported", "protocol"):
                    raise
            if sys.get(SYS_UPTIME) is not None:
                idrac.append(Point("bmc.uptime", round(sys[SYS_UPTIME] / 100), "s"))
            return CollectResult(points=idrac, facts=facts, messages=messages)
        try:
            facts["lldp_neighbors"] = self._lldp(client)
        except AdapterError as exc:
            facts["lldp_error"] = exc.kind
            messages.append("LLDP 邻居表暂不可读，系统与接口指标继续采集")
        if sys.get(SYS_UPTIME) is not None:
            points.append(Point("system.uptime", round(sys[SYS_UPTIME] / 100), "s"))

        # 接口
        names = _index(client.walk(IF_NAME), IF_NAME) or _index(client.walk(IF_DESCR), IF_DESCR)
        if names:
            oper = _index(client.walk(IF_OPER), IF_OPER)
            hc_in = _index(client.walk(IF_HC_IN), IF_HC_IN)
            hc_out = _index(client.walk(IF_HC_OUT), IF_HC_OUT)
            speed = _index(client.walk(IF_HIGH_SPEED), IF_HIGH_SPEED)
            errs_in = _index(client.walk(IF_IN_ERR), IF_IN_ERR)
            errs_out = _index(client.walk(IF_OUT_ERR), IF_OUT_ERR)
            for idx, name in list(names.items())[:MAX_INTERFACES]:
                label = str(name).strip() or f"if{idx}"
                if idx in oper:
                    points.append(Point(f"if.up:{label}", 1 if oper[idx] == 1 else 0, ""))
                for metric, table in (("if.in_bps", hc_in), ("if.out_bps", hc_out)):
                    if idx not in table:
                        points.append(Point(f"{metric}:{label}", None, "bit/s", quality="unsupported", message="设备不提供 64 位计数器"))
                        continue
                    rate = self._rate(ctx, f"{metric}:{idx}", int(table[idx]))
                    if rate is not None:
                        points.append(Point(f"{metric}:{label}", round(rate * 8, 1), "bit/s"))
                if idx in speed and speed[idx]:
                    points.append(Point(f"if.speed_mbps:{label}", int(speed[idx]), "Mbit/s"))
                if idx in errs_in or idx in errs_out:
                    points.append(Point(f"if.errors:{label}", int(errs_in.get(idx, 0)) + int(errs_out.get(idx, 0)), ""))
            if len(names) > MAX_INTERFACES:
                messages.append(f"接口共 {len(names)} 个，只采集前 {MAX_INTERFACES} 个")
        try:
            tables = self._fdb_arp(ctx, client, names)
            if tables is not None:
                facts["mac_tables"] = tables
        except AdapterError as exc:
            if exc.kind not in ("unsupported", "protocol"):
                raise

        # CPU
        loads = [int(v) for v in _index(client.walk(HR_PROC_LOAD), HR_PROC_LOAD).values()]
        if loads:
            points.append(Point("cpu.load", round(sum(loads) / len(loads), 1), "%"))
        else:
            points.append(Point("cpu.load", None, "%", quality="unsupported", message="设备不提供 HOST-RESOURCES-MIB 处理器负载"))

        # 内存与磁盘
        types = _index(client.walk(HR_STORAGE_TYPE), HR_STORAGE_TYPE)
        mem_done = False
        if types:
            descr = _index(client.walk(HR_STORAGE_DESCR), HR_STORAGE_DESCR)
            units = _index(client.walk(HR_STORAGE_UNITS), HR_STORAGE_UNITS)
            size = _index(client.walk(HR_STORAGE_SIZE), HR_STORAGE_SIZE)
            used = _index(client.walk(HR_STORAGE_USED), HR_STORAGE_USED)
            for idx, t in types.items():
                total = int(size.get(idx, 0))
                if total <= 0:
                    continue
                pct = round(int(used.get(idx, 0)) / total * 100, 1)
                kind = str(t).lstrip(".")
                if kind == HR_STORAGE_RAM:
                    points.append(Point("mem.used_pct", pct, "%"))
                    points.append(Point("mem.total_bytes", total * int(units.get(idx, 1)), "B"))
                    mem_done = True
                elif kind == HR_STORAGE_FIXED:
                    points.append(Point(f"disk.used_pct:{descr.get(idx, idx)}", pct, "%"))
        if not mem_done:
            ucd = client.get([UCD_MEM_TOTAL, UCD_MEM_AVAIL, UCD_LOAD1])
            total, avail = ucd.get(UCD_MEM_TOTAL), ucd.get(UCD_MEM_AVAIL)
            if total:
                points.append(Point("mem.used_pct", round((total - (avail or 0)) / total * 100, 1), "%"))
                points.append(Point("mem.total_bytes", int(total) * 1024, "B"))
            else:
                points.append(Point("mem.used_pct", None, "%", quality="unsupported", message="设备不提供内存信息"))
            if ucd.get(UCD_LOAD1) is not None:
                try:
                    points.append(Point("load.1m", float(ucd[UCD_LOAD1]), ""))
                except (TypeError, ValueError):
                    pass
        try:
            facts.update(vendors.versions(client, sys.get(vendors.SYS_OBJECT_ID), probe))
        except AdapterError as exc:
            if exc.kind not in ("unsupported", "protocol"):
                raise
        for reader, arg in ((vendors.mikrotik, sys.get(vendors.SYS_OBJECT_ID)), (vendors.synology, probe)):
            try:
                points.extend(reader(client, arg))
            except AdapterError as exc:
                if exc.kind not in ("unsupported", "protocol"):
                    raise
                messages.append("厂商扩展指标暂不可读，通用指标继续采集")
        return CollectResult(points=points, facts=facts, messages=messages)
