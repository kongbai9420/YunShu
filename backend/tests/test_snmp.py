"""SNMP 采集测试。

- 单元测试：用录制的 OID 数据（tests/fixtures/snmp/*.snmprec）回放，不连网络。
- 模拟器测试：本机 127.0.0.1 上启动 snmpsim（开发依赖），用真实的 pysnmp 客户端走一遍 v2c 和 v3。
  这只证明协议实现正确，不代表某款真实设备已验证。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from app.collect.base import AdapterError, EndpointContext
from app.collect.snmp import PysnmpClient, SnmpAdapter

FIX = Path(__file__).parent / "fixtures" / "snmp"


def load_snmprec(name: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for line in (FIX / f"{name}.snmprec").read_text(encoding="utf-8").splitlines():
        oid, tag, value = line.split("|", 2)
        out[oid] = value if tag in ("4", "6") else int(value)
    return out


class Recorded:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def get(self, oids: list[str]) -> dict[str, Any]:
        return {o: self.data.get(o) for o in oids}

    def walk(self, oid: str) -> dict[str, Any]:
        return {k: v for k, v in self.data.items() if k.startswith(oid + ".")}


def ctx(**kw: Any) -> EndpointContext:
    return EndpointContext(address="192.0.2.30", port=161, protocol="snmp", credential_kind="snmp_community", secret="fake-community", **kw)


def points_of(result) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {p.key: p for p in result.points}


def test_linux_host_metrics() -> None:
    data = load_snmprec("linux")
    t = [100.0]
    adapter = SnmpAdapter(client_factory=lambda c: Recorded(data), clock=lambda: t[0])
    first = points_of(adapter.collect(ctx()))
    assert first["cpu.load"].value == 20.0
    assert first["mem.used_pct"].value == 25.0
    assert first["mem.total_bytes"].value == 8000000 * 1024
    assert first["disk.used_pct:/"].value == 25.0
    assert first["if.up:eth0"].value == 1
    assert first["if.speed_mbps:eth0"].value == 1000
    assert first["if.errors:eth0"].value == 4
    assert first["system.uptime"].value == 86400
    assert "if.in_bps:eth0" not in first  # 首次采集只有计数器，没有速率

    # 60 秒后计数器增加 750 MB → 100 Mbit/s
    data["1.3.6.1.2.1.31.1.1.1.6.2"] += 750_000_000
    t[0] += 60
    second = points_of(adapter.collect(ctx()))
    assert second["if.in_bps:eth0"].value == 100_000_000.0
    assert second["if.out_bps:eth0"].value == 0.0

    # 计数器变小（设备重启）：不出负数，本轮不出速率
    data["1.3.6.1.2.1.31.1.1.1.6.2"] = 10
    t[0] += 60
    assert "if.in_bps:eth0" not in points_of(adapter.collect(ctx()))


def test_lldp_observations_keep_local_port_and_do_not_claim_confirmed_links():
    from app.collect.snmp import LLDP_REMOTE_CHASSIS, LLDP_REMOTE_NAME, LLDP_REMOTE_PORT
    data = load_snmprec("switch")
    data.update({f"{LLDP_REMOTE_NAME}.0.5.1": "fake-neighbor", f"{LLDP_REMOTE_PORT}.0.5.1": "eth0",
                 f"{LLDP_REMOTE_CHASSIS}.0.5.1": "fake-chassis"})
    closed = []
    class Client(Recorded):
        def close(self): closed.append(True)
    adapter = SnmpAdapter(client_factory=lambda c: Client(data))
    observed = adapter.discover(ctx())
    assert observed["lldp_neighbors"] == [{"local_port": "5", "remote_name": "fake-neighbor", "remote_port": "eth0", "remote_chassis": "fake-chassis"}]
    assert adapter.collect(ctx()).facts["lldp_neighbors"] == observed["lldp_neighbors"]
    assert len(closed) == 2


def test_minimal_switch_marks_unsupported_not_zero() -> None:
    adapter = SnmpAdapter(client_factory=lambda c: Recorded(load_snmprec("switch")))
    p = points_of(adapter.collect(ctx()))
    assert p["if.up:GigabitEthernet1/0/1"].value == 1 and p["if.up:GigabitEthernet1/0/2"].value == 0
    assert p["if.in_bps:GigabitEthernet1/0/1"].quality == "unsupported" and p["if.in_bps:GigabitEthernet1/0/1"].value is None
    assert p["cpu.load"].quality == "unsupported"
    assert p["mem.used_pct"].quality == "unsupported"


def test_unavailable_optional_lldp_does_not_discard_system_metrics():
    class NoLldp(Recorded):
        def walk(self, oid):
            if oid.startswith("1.0.8802"):
                raise AdapterError("unsupported", "fake unsupported MIB")
            return super().walk(oid)
    result = SnmpAdapter(client_factory=lambda c: NoLldp(load_snmprec("linux"))).collect(ctx())
    assert points_of(result)["cpu.load"].value == 20
    assert result.facts["lldp_error"] == "unsupported" and result.messages


def test_no_credential_is_unconfigured() -> None:
    with pytest.raises(AdapterError) as e:
        PysnmpClient(EndpointContext("192.0.2.30", 161, "snmp"))
    assert e.value.kind == "unconfigured"
    with pytest.raises(AdapterError) as e:
        PysnmpClient(EndpointContext("192.0.2.30", 161, "snmp", username="u", secret="not-json", credential_kind="snmpv3"))
    assert e.value.kind == "unconfigured"


# ---- snmpsim 模拟器 ----


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _skip_or_fail(reason: str) -> None:
    # CI 上必须真正跑模拟器测试，不能因为跳过而掩盖问题
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def simulator(tmp_path_factory):  # type: ignore[no-untyped-def]
    exe = shutil.which("snmpsim-command-responder", path=str(Path(sys.executable).parent))
    if exe is None:
        _skip_or_fail("没有安装 snmpsim（开发依赖）")
    data = tmp_path_factory.mktemp("snmpdata")
    shutil.copy(FIX / "linux.snmprec", data / "public.snmprec")
    port = _free_udp_port()
    proc = subprocess.Popen(
        [exe, f"--data-dir={data}", f"--agent-udpv4-endpoint=127.0.0.1:{port}",
         "--v3-user=fakeuser", "--v3-auth-key=fake-auth-key-123", "--v3-auth-proto=SHA",
         "--v3-priv-key=fake-priv-key-123", "--v3-priv-proto=AES"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        # pysnmp 按系统编码读自带的 MIB 文件，中文 Windows（GBK）下会启动失败
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    try:
        deadline = time.time() + 20
        c = EndpointContext("127.0.0.1", port, "snmp", credential_kind="snmp_community", secret="public", timeout=1)
        while time.time() < deadline:
            client = PysnmpClient(c)
            try:
                if client.get(["1.3.6.1.2.1.1.5.0"]).get("1.3.6.1.2.1.1.5.0"):
                    break
            except AdapterError:
                time.sleep(0.5)
            finally:
                client.close()
        else:
            _skip_or_fail("snmpsim 没有按时启动")
        yield port
    finally:
        proc.terminate()
        proc.wait(5)


def test_pysnmp_v2c_against_simulator(simulator: int) -> None:
    c = EndpointContext("127.0.0.1", simulator, "snmp", credential_kind="snmp_community", secret="public", timeout=2)
    p = points_of(SnmpAdapter().collect(c))
    assert p["cpu.load"].value == 20.0 and p["if.up:eth0"].value == 1 and p["mem.used_pct"].value == 25.0


def test_pysnmp_v3_against_simulator(simulator: int) -> None:
    secret = json.dumps({"auth_key": "fake-auth-key-123", "priv_key": "fake-priv-key-123", "auth_protocol": "SHA", "priv_protocol": "AES"})
    c = EndpointContext("127.0.0.1", simulator, "snmp", username="fakeuser", credential_kind="snmpv3", secret=secret, timeout=2,
                        options={"context": "public"})
    client = PysnmpClient(c)
    try:
        sys_name = client.get(["1.3.6.1.2.1.1.5.0"])
    finally:
        client.close()
    assert sys_name["1.3.6.1.2.1.1.5.0"] == "fake-host"


def test_wrong_community_times_out(simulator: int) -> None:
    c = EndpointContext("127.0.0.1", simulator, "snmp", credential_kind="snmp_community", secret="wrong", timeout=1)
    with pytest.raises(AdapterError) as e:
        SnmpAdapter().collect(c)
    assert e.value.kind in ("timeout", "auth")
