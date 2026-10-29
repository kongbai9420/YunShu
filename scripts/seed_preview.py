"""通过现有 API 填充本地预览；不清库、不改账号、不覆盖已有演示资料。

backend/.venv/Scripts/python.exe scripts/seed_preview.py --url http://127.0.0.1:5195
登录密码交互输入，或由 YUNSHU_PREVIEW_PASSWORD 提供。先在页面完成首次改密。
"""
from __future__ import annotations

import argparse
import getpass
import os
from urllib.parse import urlsplit

import httpx

NOTE = "演示数据：仅供预览分类、端点、关联与拓扑；不是实际设备或采集结果。未配置凭据，保持规划中，不自动探测。"
PORTS = {"ipmi": 623, "redfish": 443, "snmp": 161, "ssh": 22, "https": 443, "http": 80,
         "pve-api": 8006, "esxi-api": 443, "winrm": 5986, "other": 80}

# 编号、名称、形态、角色、平台、模板、协议、场景、画布位置。
DEVICES = [
    ("home-router", "家庭主路由 OpenWrt", "physical", "router", "openwrt", "openwrt", ["ssh", "snmp"], "home", (450, 0)),
    ("home-switch", "家庭千兆交换机", "physical", "switch", "standard-mib", "snmp-switch", ["snmp"], "home", (450, 180)),
    ("home-ap", "客厅无线 AP", "physical", "ap", "openwrt", "openwrt", ["ssh", "snmp"], "home", (0, 360)),
    ("home-nas", "家庭 NAS · Alpine", "physical", "nas", "alpine", "linux-b", ["ssh", "https"], "home", (450, 360)),
    ("home-lxc", "家庭下载 LXC", "container", "server", "alpine", "linux-b", ["ssh"], "home", (450, 540)),
    ("home-wifi", "随身 Wi-Fi · 未开放监控", "physical", "other", "stock-wifi", "reachability", ["other"], "home", (0, 540)),
    ("home-printer", "家庭网络打印机", "physical", "other", "printer", "reachability", ["http"], "home", (900, 360)),
    ("home-service", "家庭 Web 服务容器", "container", "other", "nginx", "reachability", ["http"], "home", (900, 540)),
    ("dc-edge", "机房出口路由器", "physical", "router", "routeros", "snmp-switch", ["snmp"], "dc", (650, 0)),
    ("dc-firewall", "机房防火墙", "physical", "firewall", "opnsense", "reachability", ["https"], "dc", (650, 160)),
    ("dc-core", "机房万兆核心交换机", "physical", "switch", "standard-mib", "snmp-switch", ["snmp"], "dc", (650, 320)),
    ("dc-dell", "Dell R740 · iDRAC9", "physical", "server", "dell-idrac", "dell-server", ["ipmi", "redfish", "snmp"], "dc", (0, 500)),
    ("dc-pve", "PVE 宿主平台", "physical", "server", "proxmox", "pve", ["pve-api", "ssh"], "dc", (0, 680)),
    ("dc-ubuntu", "Ubuntu 应用虚拟机", "vm", "server", "ubuntu", "linux", ["ssh"], "dc", (-250, 860)),
    ("dc-lxc", "Debian 数据库 LXC", "container", "server", "debian", "linux", ["ssh"], "dc", (100, 860)),
    ("dc-esxi", "ESXi 宿主服务器", "physical", "server", "esxi", "esxi", ["esxi-api", "ssh"], "dc", (650, 500)),
    ("dc-rocky", "Rocky Linux 业务虚拟机", "vm", "server", "rocky", "linux-b", ["ssh"], "dc", (450, 680)),
    ("dc-vcenter", "vCenter 管理虚拟机", "vm", "server", "vcenter", "esxi", ["esxi-api"], "dc", (800, 680)),
    ("dc-windows", "Windows 虚拟机 · 仅端口监测", "vm", "server", "windows", "reachability", ["winrm"], "dc", (450, 860)),
    ("dc-backup", "Ubuntu 备份虚拟机", "vm", "server", "ubuntu", "linux", ["ssh"], "dc", (800, 860)),
    ("dc-nas", "机房共享 NAS", "physical", "nas", "truenas", "reachability", ["https", "ssh"], "dc", (1250, 500)),
    ("dc-storage", "iSCSI 存储阵列", "physical", "storage", "iscsi", "snmp-switch", ["snmp"], "dc", (1250, 680)),
    ("dc-ups", "机柜 UPS", "physical", "ups", "ups", "reachability", ["https", "snmp"], "dc", (1600, 500)),
    ("dc-debian", "Debian 独立物理服务器", "physical", "server", "debian", "linux", ["ssh"], "dc", (1600, 680)),
    ("dc-bmc", "通用 IPMI 服务器 · 维护示例", "physical", "server", "generic-bmc", "generic-bmc", ["ipmi"], "dc", (1250, 860)),
    ("dc-alma", "AlmaLinux 物理服务器 · 退役示例", "physical", "server", "almalinux", "linux-b", ["ssh"], "dc", (1600, 860)),
]

# 起点 → 终点，方向与“承载 / 安装于 / 属于 / 依赖”的资产语义一致。
RELATIONS = [
    ("dc-pve", "dc-dell", "installed_in"),
    ("dc-pve", "dc-ubuntu", "hosts"), ("dc-pve", "dc-lxc", "hosts"),
    ("dc-esxi", "dc-rocky", "hosts"), ("dc-esxi", "dc-vcenter", "hosts"),
    ("dc-esxi", "dc-windows", "hosts"), ("dc-esxi", "dc-backup", "hosts"),
    ("dc-vcenter", "dc-esxi", "belongs_to"),
    ("dc-ubuntu", "dc-lxc", "depends_on"), ("dc-backup", "dc-nas", "depends_on"),
    ("dc-pve", "dc-storage", "depends_on"), ("dc-esxi", "dc-storage", "depends_on"),
    ("dc-dell", "dc-ups", "depends_on"), ("dc-esxi", "dc-ups", "depends_on"),
    ("home-nas", "home-lxc", "hosts"), ("home-nas", "home-service", "hosts"),
    ("home-lxc", "home-nas", "belongs_to"), ("home-service", "home-router", "depends_on"),
]
LINKS = [
    ("home-router", "home-switch", "lan1", "1", "ethernet"),
    ("home-switch", "home-ap", "2", "eth0", "ethernet"),
    ("home-switch", "home-nas", "3", "eth0", "ethernet"),
    ("home-switch", "home-printer", "4", "lan", "ethernet"),
    ("home-ap", "home-wifi", "wlan0", "wlan0", "wifi"),
    ("home-nas", "home-lxc", "br0", "eth0", "virtual"),
    ("home-nas", "home-service", "br0", "eth0", "virtual"),
    ("dc-edge", "dc-firewall", "sfp1", "wan", "fiber"),
    ("dc-firewall", "dc-core", "lan", "sfp1", "fiber"),
    ("dc-core", "dc-dell", "sfp2", "NIC1", "fiber"),
    ("dc-core", "dc-pve", "sfp3", "vmbr0", "fiber"),
    ("dc-core", "dc-esxi", "sfp4", "vmnic0", "fiber"),
    ("dc-core", "dc-nas", "sfp5", "ix0", "fiber"),
    ("dc-core", "dc-storage", "sfp6", "iscsi1", "fiber"),
    ("dc-core", "dc-debian", "10", "eth0", "ethernet"),
    ("dc-core", "dc-ups", "11", "management", "ethernet"),
    ("dc-core", "dc-bmc", "12", "BMC", "ethernet"),
    ("dc-core", "dc-alma", "13", "eth0", "ethernet"),
    ("dc-pve", "dc-ubuntu", "vmbr0", "ens18", "virtual"),
    ("dc-pve", "dc-lxc", "vmbr0", "eth0", "virtual"),
    ("dc-esxi", "dc-rocky", "vSwitch0", "ens192", "virtual"),
    ("dc-esxi", "dc-vcenter", "vSwitch0", "eth0", "virtual"),
    ("dc-esxi", "dc-windows", "vSwitch0", "Ethernet", "virtual"),
    ("dc-esxi", "dc-backup", "vSwitch0", "ens192", "virtual"),
    ("dc-ups", "dc-dell", "USB", "USB", "other"),
]


def checked(response: httpx.Response):
    if not response.is_success:
        raise RuntimeError(f"{response.request.method} {response.request.url.path} 失败（HTTP {response.status_code}），请检查预览账号和现有演示编号。")
    return response.json() if response.content else None


def seed(client: httpx.Client) -> dict:
    """仅增加缺失记录；重复执行不改用户编辑过的资产、关系或画布。"""
    sites = {item["name"]: item for item in checked(client.get("/api/sites"))}
    scenes = {}
    for key, name, kind in (("home", "[演示] 家庭实验室", "home"), ("dc", "[演示] 小型机房", "datacenter")):
        if name not in sites:
            sites[name] = checked(client.post("/api/sites", json={"name": name, "kind": kind, "notes": NOTE}))
        scenes[key] = sites[name]
    rack = next((item for item in checked(client.get("/api/racks")) if item["site_id"] == scenes["dc"]["id"] and item["name"] == "[演示] A01 机柜"), None)
    if rack is None:
        rack = checked(client.post("/api/racks", json={"site_id": scenes["dc"]["id"], "name": "[演示] A01 机柜", "height_u": 42}))
    existing = {}
    offset = 0
    while True:
        page = checked(client.get("/api/assets", params={"limit": 500, "offset": offset}))
        existing.update({item["asset_no"]: item for item in page["items"]})
        offset += len(page["items"])
        if offset >= page["total"]: break
    assets = {}
    created = 0
    rack_unit = 1
    for index, (key, name, form, role, platform, profile, protocols, scene, _) in enumerate(DEVICES, 1):
        number = "DEMO-" + key.upper()
        if number not in existing:
            endpoints = [{"address": f"192.0.2.{index + 30}", "protocol": protocol, "port": PORTS[protocol], "purpose": "monitoring", "notes": NOTE} for protocol in protocols]
            body = {"asset_no": number, "name": "[演示] " + name, "form": form, "role": role, "platform": platform,
                    "vendor": "Dell（型号演示，未实机验证）" if key == "dc-dell" else "演示厂商",
                    "model": "PowerEdge R740（演示）" if key == "dc-dell" else "演示型号",
                    "serial_no": f"DEMO-SN-{index:03}", "collection_profile": profile,
                    "lifecycle": "maintenance" if key == "dc-bmc" else "retired" if key == "dc-alma" else "planned",
                    "site_id": scenes[scene]["id"], "tags": ["演示数据", "家庭实验室" if scene == "home" else "小型机房"],
                    "notes": NOTE + ("该端点示例仅体现建档及端口检测，不代表已实现完整指标采集。" if profile == "reachability" else ""), "endpoints": endpoints}
            if scene == "dc" and form == "physical":
                body.update({"rack_id": rack["id"], "rack_unit": rack_unit})
                rack_unit += 3
            existing[number] = checked(client.post("/api/assets", json=body))
            created += 1
        assets[key] = existing[number]
    graph = checked(client.get("/api/topology/graph"))
    relations = {(item["source_asset_id"], item["target_asset_id"], item["kind"]) for item in graph["relations"]}
    for source, target, kind in RELATIONS:
        triple = (assets[source]["id"], assets[target]["id"], kind)
        if triple not in relations:
            checked(client.post("/api/relations", json={"source_asset_id": triple[0], "target_asset_id": triple[1], "kind": kind}))
    links = {(item["source_asset_id"], item["target_asset_id"], item["kind"]) for item in graph["links"]}
    for source, target, source_port, target_port, kind in LINKS:
        triple = (assets[source]["id"], assets[target]["id"], kind)
        if triple not in links:
            checked(client.post("/api/topology/links", json={"source_asset_id": triple[0], "target_asset_id": triple[1],
                    "source_port": source_port, "target_port": target_port, "kind": kind, "notes": "演示人工连线，并非扫描发现的真实链路。"}))
    views = {item["name"]: item for item in checked(client.get("/api/topology/views"))}
    for scene, kind, title in (("home", "home", "[演示] 家庭网络与容器"), ("dc", "datacenter", "[演示] 机房与虚拟化")):
        if title in views: continue
        view = checked(client.post("/api/topology/views", json={"name": title, "kind": kind, "site_id": scenes[scene]["id"]}))
        layout = {assets[item[0]]["id"]: {"x": item[8][0], "y": item[8][1], "locked": False} for item in DEVICES if item[7] == scene}
        checked(client.patch(f"/api/topology/views/{view['id']}", json={"revision": view["revision"], "layout": layout}))
    return {"assets": len(assets), "created": created, "endpoints": sum(len(a["endpoints"]) for a in assets.values()),
            "relations": len(RELATIONS), "links": len(LINKS), "views": 2}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="本机预览 URL，例如 http://127.0.0.1:5195")
    parser.add_argument("--username", default="admin")
    args = parser.parse_args()
    url = urlsplit(args.url)
    if url.scheme not in {"http", "https"} or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
        parser.error("仅支持本机预览的根地址")
    password = os.environ.get("YUNSHU_PREVIEW_PASSWORD") or getpass.getpass("预览登录密码：")
    try:
        with httpx.Client(base_url=args.url.rstrip("/"), timeout=30, trust_env=False) as client:
            account = checked(client.post("/api/auth/login", json={"username": args.username, "password": password}))
            if account["user"]["require_password_change"]:
                raise RuntimeError("请先在预览页面完成首次改密，再运行填充命令。")
            client.headers["X-CSRF-Token"] = account["csrf_token"]
            result = seed(client)
            print(f"演示预览已填充：{result['assets']} 台设备（本次新增 {result['created']}），{result['endpoints']} 个端点，{result['relations']} 条资产关系，{result['links']} 条人工连线，{result['views']} 个拓扑视图。")
    except (RuntimeError, httpx.HTTPError) as exc:
        # 不输出请求正文、环境变量或登录密码。
        parser.exit(1, f"{str(exc) if isinstance(exc, RuntimeError) else '无法连接本机预览，请确认预览已启动。'}\n")


if __name__ == "__main__": main()
