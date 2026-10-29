"""浏览器测试专用后端：真实数据库 / HTTP / Worker，设备传输全部注入伪造响应。

不提供生产开关，不接真实设备，不读取仓库 config.ini；每次启动使用新的临时目录。
"""
from __future__ import annotations

import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import httpx
import uvicorn
from app import metrics, tasks
from app.collect.base import AdapterError, CollectResult, EndpointContext, ProbeResult
from app.main import create_app
from app.metrics import Point
from app.control.base import ControlError
from app.control.thermal import Evidence, EvidenceSource, Identity, ThermalOperation, ThermalProfile
from app.models import Asset, ControlSetting, ManagementEndpoint
from app.settings import Settings
from tests.ai_fakes import FakeLLM  # 4.3 P18a：伪造的大模型接口，AI 设置页的自检与模型列表不访问外网


class FakeAdapter:
    def __init__(self, protocol): self.protocol = protocol
    def capabilities(self): return {"cpu", "memory", "system"}
    def probe(self, ctx): return ProbeResult(True)
    def discover(self, ctx): return {}
    def inventory(self, ctx):
        if ctx.address == "192.0.2.249":
            raise AdapterError("timeout", "模拟读取超时")
        if ctx.address == "192.0.2.248":
            time.sleep(1)
        if self.protocol == "ssh":
            ctx.trust.check_ssh("ssh-ed25519 AAAAfake-e2e-host")
        return ProbeResult(True, facts={"hostname": "fake-inventory-device", "os_id": "ubuntu", "vendor": "Fake Vendor", "model": "Fake Model", "serial": "FAKE-SERIAL"})
    def packages(self, ctx):
        # 4.3 P18b：伪造的 Debian 12 软件包清单
        ctx.trust.check_ssh("ssh-ed25519 AAAAfake-e2e-host")
        return {"manager": "dpkg", "os": "Debian GNU/Linux 12 (bookworm)", "os_id": "debian", "os_version": "12",
                "items": [["libssl3", "3.0.11-1~deb12u2", "openssl", "3.0.11-1~deb12u2"], ["bash", "5.2.15-2+b7", "bash", "5.2.15-2"]]}
    def collect(self, ctx):
        if self.protocol == "snmp" and ctx.address == "192.0.2.43":
            # 4.3 P18d：伪造的 iDRAC9，固件版本落在测试离线包里 CVE-2018-1243 的受影响区间
            return CollectResult(points=[Point("health.system", 1, "")], facts={"idrac": True, "idrac_name": "iDRAC9", "idrac_firmware": "3.00.00.00"})
        if self.protocol == "ssh" and ctx.address == "192.0.2.42":
            ctx.trust.check_ssh("ssh-ed25519 AAAAfake-e2e-host")
            return CollectResult(points=[Point("cpu.load", 5.0, "%")], facts={"hostname": "fake-debian", "os": "Debian GNU/Linux 12 (bookworm)", "os_id": "debian", "os_version": "12"})
        if self.protocol in ("pve-api", "esxi-api") and ctx.address in ("192.0.2.180", "192.0.2.181"):
            if ctx.secret == "fake-virt-failed":
                raise AdapterError("unreachable", "模拟虚拟化平台失联")
            host = "node-b" if ctx.secret == "fake-virt-moved" else "node-a"
            return CollectResult(facts={"virtualization": {
                "platform": "pve" if self.protocol == "pve-api" else "vmware", "instance_uuid": "fake-native-service", "complete": True,
                "hosts": [{"external_id": "node-a", "name": "fake-node-a"}, {"external_id": "node-b", "name": "fake-node-b"}],
                "guests": [
                    {"external_id": "100", "kind": "vm", "name": "fake-同名虚拟机-名称较长用于窄屏验证", "host_external_id": host, "power": "on"},
                    {"external_id": "101", "kind": "vm", "name": "fake-同名虚拟机-名称较长用于窄屏验证", "host_external_id": "node-a", "power": "off"},
                    {"external_id": "200", "kind": "container", "name": "fake-container", "host_external_id": "node-a", "power": "unknown"},
                ],
            }})
        if self.protocol == "snmp" and ctx.secret == "fake-snmp-off":
            raise AdapterError("unreachable", "模拟 SNMP 已关闭")
        if self.protocol == "ssh":
            ctx.trust.check_ssh("ssh-ed25519 AAAAfake-e2e-host")
        facts = {"hostname": "fake-device"}
        if self.protocol == "snmp" and ctx.address == "192.0.2.60":
            # 拓扑测试：模拟交换机的 LLDP 邻居表（伪造名称，只读观察）
            facts = {"sys_name": "fake-topo-sw", "lldp_neighbors": [
                {"local_port": "3", "remote_name": "fake-topo-srv", "remote_port": "eth0", "remote_chassis": "fake-chassis"}]}
        return CollectResult(points=[Point("cpu.load", 23.5, "%"), Point("mem.used_pct", None, "%", "unsupported"),
            Point("disk.used_pct", None, "%", "unconfigured"), Point("sensor.temp", None, "°C", "failed"),
            Point("power.watts", 72, "W", "estimated")], facts=facts)


class FakePower:
    """伪造 BMC 电源：按地址记状态；192.0.2.251 的控制命令超时（模拟结果不确定）。不连接任何设备。"""

    states: dict[str, str] = {}

    def __init__(self, ctx): self.address = ctx.address
    def power_state(self): return self.states.get(self.address, "on")
    def set_power(self, state):
        if self.address == "192.0.2.251":
            raise ControlError("timeout", "模拟 BMC 没有应答")
        self.states[self.address] = {"on": "on", "off": "off", "shutdown": "off", "reset": "on"}[state]
    def close(self): pass


# 离线测试专用的风扇命令：只在这个测试后端里注入，替换内置命令库
FIXTURE_PROFILE = ThermalProfile(
    profile_id="e2e-fixture", vendor="fake vendor", model_patterns=(r"Fake Model.*",), firmware_min="1.0", firmware_max="9.9",
    driver_id="dell_idrac_raw_v1",
    operations=frozenset({ThermalOperation.RESTORE_AUTO, ThermalOperation.SET_SPEED_ALL, ThermalOperation.SET_SPEED_PER_FAN}),
    safe_min_pwm=30, safe_max_pwm=100, evidence=Evidence(EvidenceSource.STANDALONE_FIXTURE, note="浏览器测试"),
)


class FakeThermal:
    """伪造风扇控制：192.0.2.252 报告未登记的厂商（门禁拒绝）。不连接任何设备。"""

    def __init__(self, ctx): self.address = ctx.address
    def identity(self):
        if self.address == "192.0.2.252":
            return Identity("Other Vendor", "X1", "1.0")
        return Identity("Fake Vendor", "Fake Model 1", "2.0")
    def raw(self, netfn, command, data): pass
    def close(self): pass


def fake_probe(address, protocol, ports, timeout, credential):
    time.sleep(0.05)
    return {"address": address, "ports": ports, "facts": {"sys_name": "模拟发现设备"}}


def main():
    with tempfile.TemporaryDirectory(prefix="yunshu-p2-e2e-", ignore_cleanup_errors=True) as temporary:
        base = Path(temporary)
        cfg = Settings(data_dir=base / "data", database_url=f"sqlite:///{(base / 'data' / 'test.db').as_posix()}",
                       master_key_file=base / "keys" / "master.key", backup_interval_hours=0, embedded_worker=True,
                       control_cutover_enabled=False)  # 浏览器用例覆盖部署显式关闭开关（=0）时的界面；默认打开由后端测试覆盖
        original_handlers = tasks.build_handlers
        original_schedules = tasks.build_schedules
        tasks.build_schedules = lambda settings: original_schedules(settings, thermal_catalog=(FIXTURE_PROFILE,))
        tasks.build_handlers = lambda settings: original_handlers(settings, adapters={p: FakeAdapter(p) for p in ("snmp", "ssh", "reach", "ipmi", "redfish", "pve-api", "esxi-api")}, discovery_probe=fake_probe,
            notification_sender=lambda kind, config, secrets, message: {"accepted": True, "kind": kind},
            power_client_factory=FakePower, power_verify={"verify_attempts": 1, "verify_interval": 0},
            thermal_client_factory=FakeThermal, thermal_catalog=(FIXTURE_PROFILE,))
        app = create_app(cfg)
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application):
            async with original_lifespan(application):
                application.state.thermal_catalog = (FIXTURE_PROFILE,)
                application.state.information_adapters = {p: FakeAdapter(p) for p in ("snmp", "ssh", "ipmi", "redfish", "pve-api", "esxi-api")}
                application.state.ai_http_client = httpx.Client(transport=FakeLLM(json_schema=False, models=["fake-deepseek-chat", "fake-deepseek-reasoner"], answer={
                    "conclusion": "ASSET_1 的 iDRAC 固件在受影响范围内（伪造的 AI 回答）", "impact": "伪造的影响说明", "exploit_conditions": ["能访问 iDRAC 网页"],
                    "priority": "尽快处理", "actions": ["升级 iDRAC 固件"], "evidence": ["伪造依据"]}, answers=[
                    ("请解读下面这条告警", {"summary": "伪造的告警解读", "likely_causes": [{"cause": "伪造原因", "confidence": "中", "evidence": "伪造依据"}],
                                         "checks": ["伪造检查步骤"], "urgency": "持续观察", "note": ""}),
                    ("巡检报告写成", {"overall": "需关注", "headline": "伪造的巡检摘要", "top_issues": [{"title": "伪造问题", "why": "伪造原因", "action": "伪造建议"}],
                                     "changes": ""})],
                    tool_plan=[("top_metric", {"metric": "cpu.load"})], final_text="伪造回答：CPU 使用率最高的是 ASSET_1，详见 #/monitoring").transport())
                with application.state.session_factory() as db:
                    asset = Asset(name="质量样本设备", asset_no="FAKE-QUALITY", form="physical", role="server", platform="ubuntu", lifecycle="planned", collection_profile="linux")
                    asset.endpoints = [ManagementEndpoint(address="192.0.2.10", port=22, protocol="ssh")]
                    db.add(asset); db.flush()
                    result = FakeAdapter("snmp").collect(EndpointContext("192.0.2.10", 161, "snmp"))
                    metrics.write_sample(db, asset.id, "snmp", result.points, observed_at=datetime.now(UTC))
                    metrics.write_sample(db, asset.id, "snmp", [Point("fixture.stale", 8, "%")], observed_at=datetime.now(UTC) - timedelta(hours=1))
                    # 浏览器测试直接以“新平台负责控制”启动，设备是上面的伪造 BMC
                    db.add(ControlSetting(key="default", owner="new", revision=1, changed_by="e2e", note="浏览器测试"))
                    db.commit()
                yield
        app.router.lifespan_context = lifespan
        uvicorn.run(app, host="127.0.0.1", port=8015, log_level="warning")


if __name__ == "__main__":
    main()
