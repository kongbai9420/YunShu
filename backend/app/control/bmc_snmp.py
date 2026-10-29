"""一键开启 / 关闭 BMC 的 SNMP（4.4 P19）。

通过 BMC 的 Redfish 修改设置，不用 IPMI（IPMI 没有开启 SNMP 代理的标准命令）：
- Dell iDRAC：改 Manager 的 Attributes（SNMP.1.AgentEnable、SNMP.1.AgentCommunity；SNMPv3 使用 iDRAC 用户，
  打开该用户的 Users.N.SNMPv3Enable，认证 / 加密密钥就是该用户的密码）。
- 标准写法（HPE iLO、华为 / 超聚变 openUBMC 等）：PATCH Manager 的 NetworkProtocol 里的 SNMP 对象。
  团体名数组只追加，原有条目用 {} 占位表示不改；iLO 修改后需要重启管理芯片才生效，平台不会自动重启。
- 读不到可用 SNMP 设置的（联想 XCC 专用扩展、新华三、浪潮及部分 OpenBMC 机型）：明确拒绝，提示到 BMC 网页手动开启。

开启后用所选的 SNMP 凭据读一次 sysName，读到才算“已查询确认”。规则与电源控制相同：
修改命令发出前失败记为“失败”；发出后超时记为“结果不确定”，不重试。默认关闭（YUNSHU_BMC_SNMP=1 打开）。

4.4 P20c 起同一入口还能让 BMC 主动推送告警（与开启 SNMP 共用门禁）：
- 配置 / 移除 Trap 目标：Dell iDRAC 用 SNMPAlert.N.DestAddr / State（并打开全局告警开关 IPMILan.1.AlertEnable）；
  按 Redfish 标准支持 SNMP 订阅的 BMC 在 EventService 里建 SubscriptionType=SNMPTrap 的订阅。只支持 v2c 团体名。
- 订阅 / 取消 Redfish 事件：在 EventService/Subscriptions 建一条推送到平台的订阅，地址带随机令牌。
  同一平台地址的旧订阅先删掉再建，避免重复推送。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.collect.base import AdapterError, EndpointContext
from app.collect.redfish import cert_fingerprint
from app.control.base import ControlError

BMC_SNMP_TASK = "control.bmc_snmp"
SNMP_CREDENTIAL_KINDS = ("snmp_community", "snmpv3")


def bmc_snmp_enabled() -> bool:
    return os.environ.get("YUNSHU_BMC_SNMP", "0") == "1"


@dataclass(frozen=True)
class SnmpAction:
    key: str
    label: str
    high_risk: bool


SNMP_ACTIONS: dict[str, SnmpAction] = {
    a.key: a for a in (
        SnmpAction("enable", "开启 BMC 的 SNMP", False),
        # 关闭会让依赖 SNMP 的其他监控系统一起读不到数据
        SnmpAction("disable", "关闭 BMC 的 SNMP", True),
        # 4.4 P20c：让 BMC 主动推送告警；只增删平台自己的推送目标，不改其他设置
        SnmpAction("trap_on", "在 BMC 上配置 Trap 目标", False),
        SnmpAction("trap_off", "移除平台的 Trap 目标", False),
        SnmpAction("events_on", "订阅 BMC 的 Redfish 事件", False),
        SnmpAction("events_off", "取消 Redfish 事件订阅", False),
    )
}
PUSH_ACTIONS = ("trap_on", "trap_off", "events_on", "events_off")
EVENTS_PATH = "/api/bmc-events/"
TRAP_PORT = 162


def events_base_url() -> str:
    """BMC 推送 Redfish 事件时访问平台的 HTTPS 地址（例如 https://yunshu.example.test），未设置时不能订阅。"""
    url = os.environ.get("YUNSHU_BMC_EVENTS_URL", "").strip().rstrip("/")
    return url if url.startswith("https://") else ""


@dataclass(frozen=True)
class SnmpCredential:
    version: str  # v2c / v3
    community: str = ""
    user: str = ""
    auth_protocol: str = "SHA"
    priv_protocol: str = "AES"


def snmp_credential(kind: str, username: str, secret: str) -> SnmpCredential:
    """与 SNMP 采集同样的凭据约定：团体名凭据的密钥就是团体名；v3 的密钥是 JSON。"""
    if kind == "snmp_community":
        return SnmpCredential("v2c", community=secret)
    try:
        data = json.loads(secret)
    except ValueError:
        data = {}
    return SnmpCredential("v3", user=username, auth_protocol=str(data.get("auth_protocol") or "SHA").upper(),
                          priv_protocol=str(data.get("priv_protocol") or "AES").upper())


# ---- Redfish 写客户端 ----


class ConfigClient(Protocol):
    def get(self, path: str) -> tuple[dict[str, Any], str]:
        """返回 (资源, ETag)；404 返回空字典。"""
        ...

    def patch(self, path: str, body: dict[str, Any], etag: str) -> None: ...

    def post(self, path: str, body: dict[str, Any]) -> str:
        """返回新建资源的路径（Location），BMC 没给时返回空串。"""
        ...

    def delete(self, path: str) -> None: ...

    def close(self) -> None: ...


ConfigClientFactory = Callable[[EndpointContext], ConfigClient]


def _redfish_message(r: httpx.Response) -> str:
    try:
        info = (r.json().get("error") or {}).get("@Message.ExtendedInfo") or []
        return "；".join(str(i.get("Message", "")) for i in info if isinstance(i, dict))[:300]
    except ValueError:
        return r.text[:300]


class RedfishConfigClient:
    """登录会话（结束时删除）；BMC 自签证书按端点固定的指纹比对后才发送密码。"""

    def __init__(self, ctx: EndpointContext, *, transport: httpx.BaseTransport | None = None,
                 fingerprint: Callable[[str, int, float], str] = cert_fingerprint) -> None:
        if not ctx.username or not ctx.secret:
            raise ControlError("unconfigured", "Redfish 端点没有关联用户名和密码的凭据")
        port = ctx.port or 443
        if ctx.tls_verify:
            try:
                ctx.trust.check_tls(fingerprint(ctx.address, port, ctx.timeout))
            except AdapterError as exc:
                raise ControlError("auth" if exc.kind == "untrusted" else "unreachable", str(exc)) from exc
        self.http = httpx.Client(base_url=f"https://{ctx.address}:{port}", verify=False, timeout=ctx.timeout, transport=transport,
                                 headers={"Accept": "application/json", "OData-Version": "4.0"})
        self.session_uri = ""
        try:
            r = self.http.post("/redfish/v1/SessionService/Sessions", json={"UserName": ctx.username, "Password": ctx.secret})
        except httpx.TimeoutException as exc:
            self.http.close()
            raise ControlError("unreachable", "连接 Redfish 超时（BMC 没有应答 HTTPS），没有修改任何设置") from exc
        except httpx.HTTPError as exc:
            self.http.close()
            raise ControlError("unreachable", f"无法连接 Redfish，没有修改任何设置：{str(exc)[:200]}") from exc
        if r.status_code in (401, 403):
            self.http.close()
            raise ControlError("auth", "Redfish 用户名或密码不正确")
        if r.status_code >= 400 or "X-Auth-Token" not in r.headers:
            self.http.close()
            raise ControlError("protocol", f"Redfish 登录失败（HTTP {r.status_code}）")
        self.http.headers["X-Auth-Token"] = r.headers["X-Auth-Token"]
        self.session_uri = r.headers.get("Location", "")

    def get(self, path: str) -> tuple[dict[str, Any], str]:
        try:
            r = self.http.get(path)
        except httpx.HTTPError as exc:
            raise ControlError("unreachable", f"读取 {path} 失败：{exc}") from exc
        if r.status_code == 404:
            return {}, ""
        if r.status_code in (401, 403):
            raise ControlError("auth", f"Redfish 账号无权读取 {path}")
        if r.status_code >= 400:
            raise ControlError("protocol", f"Redfish {path} 返回 HTTP {r.status_code}")
        return r.json(), r.headers.get("ETag", "")

    def patch(self, path: str, body: dict[str, Any], etag: str) -> None:
        headers = {"If-Match": etag} if etag else {}
        try:
            r = self.http.patch(path, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ControlError("timeout", "修改已发出但 BMC 没有应答，SNMP 当前状态未知") from exc
        except httpx.HTTPError as exc:
            raise ControlError("timeout", f"发送修改时连接中断：{exc}") from exc
        if r.status_code in (401, 403):
            raise ControlError("auth", "Redfish 账号没有修改 BMC 网络设置的权限（需要“配置管理器”一类的权限）")
        if r.status_code >= 400:
            raise ControlError("protocol", f"BMC 拒绝了修改（HTTP {r.status_code}）：{_redfish_message(r)}")

    def post(self, path: str, body: dict[str, Any]) -> str:
        try:
            r = self.http.post(path, json=body)
        except httpx.TimeoutException as exc:
            raise ControlError("timeout", "订阅请求已发出但 BMC 没有应答，是否建成未知") from exc
        except httpx.HTTPError as exc:
            raise ControlError("timeout", f"发送订阅时连接中断：{exc}") from exc
        if r.status_code in (401, 403):
            raise ControlError("auth", "Redfish 账号没有管理事件订阅的权限（需要“配置管理器”一类的权限）")
        if r.status_code >= 400:
            raise ControlError("protocol", f"BMC 拒绝了订阅（HTTP {r.status_code}）：{_redfish_message(r)}")
        location = r.headers.get("Location", "")
        if not location:
            try:
                location = str(r.json().get("@odata.id") or "")
            except ValueError:
                location = ""
        return httpx.URL(location).path if location.startswith("http") else location

    def delete(self, path: str) -> None:
        try:
            r = self.http.delete(path)
        except httpx.TimeoutException as exc:
            raise ControlError("timeout", "删除请求已发出但 BMC 没有应答，是否删除未知") from exc
        except httpx.HTTPError as exc:
            raise ControlError("timeout", f"发送删除时连接中断：{exc}") from exc
        if r.status_code in (401, 403):
            raise ControlError("auth", "Redfish 账号没有管理事件订阅的权限")
        if r.status_code >= 400 and r.status_code != 404:
            raise ControlError("protocol", f"BMC 拒绝了删除（HTTP {r.status_code}）：{_redfish_message(r)}")

    def close(self) -> None:
        try:
            if self.session_uri:
                self.http.delete(self.session_uri)
        except httpx.HTTPError:
            pass
        self.http.close()


# ---- 识别 BMC 的 SNMP 设置 ----


@dataclass
class Plan:
    style: str  # dell / standard
    vendor: str
    uri: str
    etag: str
    enabled: bool
    data: dict[str, Any]
    needs_reset: bool = False


def _members(c: ConfigClient, path: str) -> list[str]:
    return [m["@odata.id"] for m in c.get(path)[0].get("Members", []) if isinstance(m, dict) and "@odata.id" in m]


MANUAL_HINT = "请登录 BMC 网页手动开启 SNMP（通常在“网络服务”或“远程服务 → SNMP”）"


def detect(c: ConfigClient) -> Plan:
    """只读：找到这台 BMC 放 SNMP 设置的位置和当前状态。不支持时抛 unsupported，没有任何写操作。"""
    root, _ = c.get("/redfish/v1/")
    oem = root.get("Oem") or {}
    managers = _members(c, "/redfish/v1/Managers")
    if not managers:
        raise ControlError("unsupported", f"BMC 的 Redfish 没有列出管理控制器，无法定位 SNMP 设置。{MANUAL_HINT}")
    mgr_uri = managers[0]
    mgr, _ = c.get(mgr_uri)
    if "Dell" in oem or str(mgr.get("Id", "")).startswith("iDRAC"):
        uri = f"{mgr_uri}/Attributes"
        res, etag = c.get(uri)
        attrs = res.get("Attributes") or {}
        if "SNMP.1.AgentEnable" in attrs:
            return Plan("dell", "Dell iDRAC", uri, etag, attrs["SNMP.1.AgentEnable"] == "Enabled", attrs)
    np_uri = (mgr.get("NetworkProtocol") or {}).get("@odata.id")
    if np_uri:
        np, etag = c.get(np_uri)
        snmp = np.get("SNMP")
        if isinstance(snmp, dict) and "ProtocolEnabled" in snmp:
            hpe = "Hpe" in oem or "Hp" in oem
            return Plan("standard", "HPE iLO" if hpe else str(root.get("Vendor") or mgr.get("Manufacturer") or "Redfish"),
                        np_uri, etag, bool(snmp["ProtocolEnabled"]), snmp, needs_reset=hpe)
    if "Lenovo" in oem:
        raise ControlError("unsupported", f"联想 XCC 的 SNMP 设置在联想专用扩展里，平台还没有适配。{MANUAL_HINT}")
    raise ControlError("unsupported", f"这台 BMC 的 Redfish 没有提供 SNMP 设置（常见于新华三、浪潮及部分 OpenBMC 机型）。{MANUAL_HINT}")


# ---- 生成修改内容 ----

DELL_AUTH = {"SHA": "SHA", "MD5": "MD5"}
DELL_PRIV = {"AES": "AES", "DES": "DES"}


def _dell_enable(plan: Plan, cred: SnmpCredential) -> tuple[dict[str, Any], list[str]]:
    a, want, notes = plan.data, {"SNMP.1.AgentEnable": "Enabled"}, []
    if cred.version == "v2c":
        current = str(a.get("SNMP.1.AgentCommunity") or "")
        # iDRAC 只有一个团体名：已经开着且在用别的团体名时不覆盖，避免打断正在用它的其他监控
        if plan.enabled and current and current != cred.community:
            raise ControlError("rejected", "iDRAC 只有一个团体名，现在已开启并在用另一个团体名；平台不覆盖它。"
                                           "请改选与 iDRAC 上相同团体名的凭据，或在 iDRAC 上手动修改")
        want["SNMP.1.AgentCommunity"] = cred.community
        if a.get("SNMP.1.SNMPProtocol") == "SNMPv3":
            want["SNMP.1.SNMPProtocol"] = "All"
            notes.append("iDRAC 原来只允许 SNMPv3，已改为同时允许 v2c")
    else:
        idx = next((m.group(1) for k, v in a.items() if (m := re.fullmatch(r"Users\.(\d+)\.UserName", k)) and v == cred.user), None)
        if idx is None:
            raise ControlError("rejected", f"iDRAC 上没有名为“{cred.user}”的用户。iDRAC 的 SNMPv3 使用 iDRAC 本地用户，"
                                           "请先在 iDRAC 建好该用户，SNMPv3 凭据的认证 / 加密密钥填该用户的密码")
        if cred.auth_protocol not in DELL_AUTH or cred.priv_protocol not in DELL_PRIV:
            raise ControlError("rejected", "iDRAC 的 SNMPv3 只支持 SHA / MD5 认证与 AES / DES 加密，请修改凭据里的协议")
        want[f"Users.{idx}.SNMPv3Enable"] = "Enabled"
        for key, value in ((f"Users.{idx}.SNMPv3AuthenticationType", DELL_AUTH[cred.auth_protocol]),
                           (f"Users.{idx}.SNMPv3PrivacyType", DELL_PRIV[cred.priv_protocol])):
            if key in a:
                want[key] = value
    return {k: v for k, v in want.items() if a.get(k) != v}, notes


def _standard_enable(plan: Plan, cred: SnmpCredential) -> tuple[dict[str, Any], list[str]]:
    s, want, notes = plan.data, {"ProtocolEnabled": True}, []
    if cred.version == "v2c":
        if "EnableSNMPv2c" in s:
            want["EnableSNMPv2c"] = True
        cs = s.get("CommunityStrings")
        if isinstance(cs, list):
            if not any(isinstance(x, dict) and x.get("CommunityString") == cred.community for x in cs):
                # Redfish PATCH 数组：{} 表示该条不改，只在末尾追加一条只读团体名
                want["CommunityStrings"] = [{} for _ in cs] + [{"CommunityString": cred.community, "AccessMode": "Limited"}]
        else:
            notes.append("这台 BMC 不支持通过 Redfish 设置团体名，需要 BMC 上已有同名的只读团体名")
    else:
        if "EnableSNMPv3" in s:
            want["EnableSNMPv3"] = True
        notes.append(f"SNMPv3 用户“{cred.user}”需要已在 BMC 上建好")
    return {k: v for k, v in want.items() if k == "CommunityStrings" or s.get(k) != v}, notes


@dataclass
class SnmpOutcome:
    status: str  # verified / accepted
    message: str
    observed: dict[str, Any] = field(default_factory=dict)


SnmpProbe = Callable[[EndpointContext], str]


def default_probe(ctx: EndpointContext) -> str:
    from app.collect.snmp import SnmpAdapter

    res = SnmpAdapter().probe(ctx)
    return str(res.facts.get("sys_name") or res.facts.get("sys_descr") or "")


def execute_bmc_snmp(
    ctx: EndpointContext, action: str, *, cred: SnmpCredential | None = None, snmp_ctx: EndpointContext | None = None,
    factory: ConfigClientFactory = RedfishConfigClient, probe: SnmpProbe = default_probe,
) -> SnmpOutcome:
    """开启或关闭一台 BMC 的 SNMP。抛出 ControlError 表示失败或结果不确定（kind=timeout）。"""
    enable = action == "enable"
    c = factory(ctx)
    try:
        plan = detect(c)
        observed: dict[str, Any] = {"style": plan.style, "vendor": plan.vendor, "before": plan.enabled}
        notes: list[str] = []
        if enable:
            if cred is None:
                raise ControlError("unconfigured", "没有选择 SNMP 凭据")
            changes, notes = _dell_enable(plan, cred) if plan.style == "dell" else _standard_enable(plan, cred)
        elif plan.style == "dell":
            changes = {"SNMP.1.AgentEnable": "Disabled"} if plan.enabled else {}
        else:
            changes = {"ProtocolEnabled": False} if plan.enabled else {}
        # 只记录改了哪些项，不记录团体名
        observed["changed"] = sorted(changes)
        if changes:
            c.patch(plan.uri, {"Attributes": changes} if plan.style == "dell" else {"SNMP": changes}, plan.etag)
        try:
            after = detect(c).enabled
        except ControlError as exc:
            observed["verify_error"] = str(exc)
            after = None
        observed["after"] = after
    finally:
        c.close()
    done = "没有需要修改的设置" if not changes else "修改已被 BMC 接受"
    tail = ("；" + "；".join(notes)) if notes else ""
    if after is not enable:
        reset = "iLO 需要重启管理芯片后才生效（不影响服务器运行），平台不会自动重启，请在 iLO 上操作后再查看" if plan.needs_reset else "请稍后在 BMC 上查看"
        return SnmpOutcome("accepted", f"{done}，但回读的 SNMP 状态还不是“{'开启' if enable else '关闭'}”；{reset}{tail}", observed)
    if not enable:
        return SnmpOutcome("verified", f"{done}，SNMP 已确认关闭。平台上这台资产的 SNMP 端点保留，之后采集会失败，不再需要时请删除{tail}", observed)
    if snmp_ctx is None:
        return SnmpOutcome("accepted", f"{done}，SNMP 已开启{tail}", observed)
    try:
        name = probe(snmp_ctx)
    except AdapterError as exc:
        observed["snmp_probe"] = exc.kind
        hint = "（SNMPv3 请核对 BMC 上的用户与密钥）" if cred and cred.version == "v3" else ""
        return SnmpOutcome("accepted", f"{done}，SNMP 已开启，但用所选凭据还读不到数据{hint}：{exc}{tail}", observed)
    observed["snmp_probe"] = "ok"
    return SnmpOutcome("verified", f"{done}，SNMP 已开启，并用所选凭据读到了设备名称“{name[:60]}”{tail}", observed)


# ---- 4.4 P20c：BMC 主动推送（Trap 目标、Redfish 事件订阅） ----

PUSH_HINT = "请登录 BMC 网页手动设置（通常在“告警”或“SNMP → Trap 目标”）"


def _event_service(c: ConfigClient) -> tuple[dict[str, Any], str]:
    svc, _ = c.get("/redfish/v1/EventService")
    if not svc:
        raise ControlError("unsupported", "这台 BMC 的 Redfish 没有事件服务（EventService）")
    if svc.get("ServiceEnabled") is False:
        raise ControlError("unsupported", "BMC 关闭了 Redfish 事件服务，请先在 BMC 上开启")
    return svc, (svc.get("Subscriptions") or {}).get("@odata.id") or "/redfish/v1/EventService/Subscriptions"


def _subscriptions(c: ConfigClient, uri: str, match: Callable[[str], bool]) -> list[str]:
    out = []
    for m in _members(c, uri):
        sub, _ = c.get(m)
        if match(str(sub.get("Destination") or "")):
            out.append(m)
    return out


def _dell_attributes(c: ConfigClient) -> tuple[str, str, dict[str, Any]] | None:
    root, _ = c.get("/redfish/v1/")
    managers = _members(c, "/redfish/v1/Managers")
    if not managers:
        return None
    mgr, _ = c.get(managers[0])
    if "Dell" not in (root.get("Oem") or {}) and not str(mgr.get("Id", "")).startswith("iDRAC"):
        return None
    uri = f"{managers[0]}/Attributes"
    res, etag = c.get(uri)
    attrs = res.get("Attributes") or {}
    return (uri, etag, attrs) if any(k.startswith("SNMPAlert.") for k in attrs) else None


def _dell_slots(attrs: dict[str, Any]) -> list[str]:
    return sorted({m.group(1) for k in attrs if (m := re.fullmatch(r"SNMPAlert\.(\d+)\.DestAddr", k))}, key=int)


def _trap_dest(target: str) -> str:
    return f"snmp://{target}:{TRAP_PORT}"


def _dell_trap(c: ConfigClient, found: tuple[str, str, dict[str, Any]], action: str, target: str,
               cred: SnmpCredential | None) -> SnmpOutcome:
    uri, etag, a = found
    slots = _dell_slots(a)
    mine = [n for n in slots if str(a.get(f"SNMPAlert.{n}.DestAddr") or "").strip() == target]
    observed: dict[str, Any] = {"style": "dell", "vendor": "Dell iDRAC", "target": target}
    notes: list[str] = []
    if action == "trap_on":
        assert cred is not None
        current = str(a.get("SNMP.1.AgentCommunity") or "")
        if current and current != cred.community:
            raise ControlError("rejected", "iDRAC 发送 Trap 用的是它的 SNMP 团体名，现在和所选凭据不同；"
                                           "请改选与 iDRAC 相同团体名的凭据，或先用同一凭据“开启 BMC 的 SNMP”")
        free = [n for n in slots if not str(a.get(f"SNMPAlert.{n}.DestAddr") or "").strip().strip("0.:")]
        slot = (mine or free or [None])[0]
        if slot is None:
            raise ControlError("rejected", f"iDRAC 的 {len(slots)} 个 Trap 目标都已被占用，平台不覆盖别人的设置。{PUSH_HINT}")
        want = {f"SNMPAlert.{slot}.DestAddr": target, f"SNMPAlert.{slot}.State": "Enabled"}
        if not current:
            want["SNMP.1.AgentCommunity"] = cred.community
        if "IPMILan.1.AlertEnable" in a and a["IPMILan.1.AlertEnable"] != "Enabled":
            want["IPMILan.1.AlertEnable"] = "Enabled"
            notes.append("已打开 iDRAC 的全局告警开关（会同时启用 iDRAC 上已配置的其他告警方式）")
        if a.get("SNMP.1.TrapFormat") == "SNMPv3":
            want["SNMP.1.TrapFormat"] = "SNMPv2"
            notes.append("Trap 格式由 SNMPv3 改为 SNMPv2（平台只接收 v1 / v2c）")
        observed["slot"] = slot
    else:
        want = {}
        for n in mine:
            want[f"SNMPAlert.{n}.State"] = "Disabled"
            want[f"SNMPAlert.{n}.DestAddr"] = ""
    changes = {k: v for k, v in want.items() if a.get(k) != v}
    observed["changed"] = sorted(changes)  # 只记录改了哪些项，不记录团体名
    if changes:
        c.patch(uri, {"Attributes": changes}, etag)
    after = _dell_attributes(c)
    a2 = after[2] if after else {}
    now_mine = [n for n in _dell_slots(a2) if str(a2.get(f"SNMPAlert.{n}.DestAddr") or "").strip() == target
                and a2.get(f"SNMPAlert.{n}.State") == "Enabled"]
    tail = ("；" + "；".join(notes)) if notes else ""
    done = "没有需要修改的设置" if not changes else "修改已被 BMC 接受"
    if action == "trap_on":
        if now_mine:
            return SnmpOutcome("verified", f"{done}，iDRAC 的 Trap 目标 {now_mine[0]} 已指向 {target}（UDP {TRAP_PORT}）。"
                                           f"告警种类沿用 iDRAC 的告警筛选设置{tail}", observed)
        return SnmpOutcome("accepted", f"{done}，但回读还没看到指向 {target} 的 Trap 目标，请稍后在 iDRAC 上查看{tail}", observed)
    if now_mine:
        return SnmpOutcome("accepted", f"{done}，但回读仍看到指向 {target} 的 Trap 目标，请在 iDRAC 上查看", observed)
    return SnmpOutcome("verified", f"{done}，iDRAC 上已没有指向 {target} 的 Trap 目标", observed)


def _standard_trap(c: ConfigClient, action: str, target: str, cred: SnmpCredential | None) -> SnmpOutcome:
    svc, subs_uri = _event_service(c)
    types = [str(t) for t in (svc.get("SubscriptionTypes") or []) if t]
    if "SNMP" not in svc and "SNMPTrap" not in types:
        raise ControlError("unsupported", f"这台 BMC 的 Redfish 不支持配置 SNMP Trap 目标（常见于 HPE iLO、联想、新华三、浪潮）。{PUSH_HINT}，"
                                          f"目标地址 {target}，UDP {TRAP_PORT}")
    dest = _trap_dest(target)
    ours = _subscriptions(c, subs_uri, lambda d: d.startswith(f"snmp://{target}"))
    observed: dict[str, Any] = {"style": "standard", "target": target, "removed": len(ours)}
    for m in ours:
        c.delete(m)
    if action == "trap_off":
        left = _subscriptions(c, subs_uri, lambda d: d.startswith(f"snmp://{target}"))
        if left:
            return SnmpOutcome("accepted", f"已请求删除 {len(ours)} 条，但回读仍有指向 {target} 的 Trap 订阅，请在 BMC 上查看", observed)
        return SnmpOutcome("verified", f"BMC 上已没有指向 {target} 的 Trap 订阅" + (f"（删除了 {len(ours)} 条）" if ours else ""), observed)
    assert cred is not None
    observed["remote"] = c.post(subs_uri, {"Destination": dest, "SubscriptionType": "SNMPTrap", "Protocol": "SNMPv2c",
                                           "Context": "yunshu", "SNMP": {"TrapCommunity": cred.community}})
    found = _subscriptions(c, subs_uri, lambda d: d == dest)
    if found:
        observed["remote"] = observed["remote"] or found[0]
        return SnmpOutcome("verified", f"BMC 已建好发往 {target}（UDP {TRAP_PORT}）的 Trap 订阅", observed)
    return SnmpOutcome("accepted", "BMC 接受了 Trap 订阅，但回读没有看到，请稍后在 BMC 上查看", observed)


def _events(c: ConfigClient, action: str, base: str, token: str) -> SnmpOutcome:
    svc, subs_uri = _event_service(c)
    prefix = base + EVENTS_PATH
    ours = _subscriptions(c, subs_uri, lambda d: d.startswith(prefix))
    observed: dict[str, Any] = {"destination": prefix, "removed": len(ours)}
    for m in ours:
        c.delete(m)
    if action == "events_off":
        left = _subscriptions(c, subs_uri, lambda d: d.startswith(prefix))
        if left:
            return SnmpOutcome("accepted", "已请求删除，但回读仍有推送到平台的订阅，请在 BMC 上查看", observed)
        return SnmpOutcome("verified", "BMC 上已没有推送到平台的事件订阅" + (f"（删除了 {len(ours)} 条）" if ours else ""), observed)
    body: dict[str, Any] = {"Destination": prefix + token, "Protocol": "Redfish", "Context": "yunshu"}
    if "Alert" in (svc.get("EventTypesForSubscription") or []):
        body["EventTypes"] = ["Alert"]
    observed["remote"] = c.post(subs_uri, body)
    found = _subscriptions(c, subs_uri, lambda d: d == prefix + token)
    replaced = f"，替换了之前的 {len(ours)} 条" if ours else ""
    if found:
        observed["remote"] = observed["remote"] or found[0]
        return SnmpOutcome("verified", f"BMC 已建好推送到平台的 Redfish 事件订阅{replaced}。收到第一条事件后，“服务器告警”里会显示最近推送时间", observed)
    return SnmpOutcome("accepted", f"BMC 接受了订阅{replaced}，但回读没有看到，请稍后在 BMC 上查看", observed)


def execute_bmc_push(
    ctx: EndpointContext, action: str, *, cred: SnmpCredential | None = None, target: str = "", events_url: str = "", token: str = "",
    factory: ConfigClientFactory = RedfishConfigClient,
) -> SnmpOutcome:
    """配置 / 移除 BMC 主动推送。抛出 ControlError 表示失败或结果不确定（kind=timeout）。"""
    if action == "trap_on" and (cred is None or cred.version != "v2c"):
        raise ControlError("unconfigured", "配置 Trap 目标需要选择团体名（v2c）凭据：平台只接收 v1 / v2c Trap")
    c = factory(ctx)
    try:
        if action in ("events_on", "events_off"):
            return _events(c, action, events_url, token)
        dell = _dell_attributes(c)
        return _dell_trap(c, dell, action, target, cred) if dell else _standard_trap(c, action, target, cred)
    finally:
        c.close()
