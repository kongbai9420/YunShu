"""IPMI 与 Redfish 采集测试。全部用伪造数据；Redfish 的 JSON 按 iDRAC9 的结构手写，不是真实设备导出。"""

from __future__ import annotations

import datetime as dt
import http.server
import json
import ssl
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.collect.base import AdapterError, EndpointContext, Trust
from app.collect.ipmi import IpmiAdapter, IpmitoolClient, PyghmiClient, parse_sdr_elist
from app.collect.redfish import RedfishAdapter, cert_fingerprint

FAKE_USER, FAKE_PW = "fakeuser", "fake-bmc-pw-1"


def ctx(protocol: str, port: int, **kw: Any) -> EndpointContext:
    return EndpointContext("192.0.2.50", port, protocol, username=FAKE_USER, secret=FAKE_PW, **kw)


def pts(r) -> dict:  # type: ignore[no-untyped-def]
    return {p.key: p for p in r.points}


# ---- IPMI ----


class FakeCommand:
    def __init__(self, readings: list[dict[str, Any]], power: str = "on", fail: Exception | None = None) -> None:
        self.readings, self.power, self.fail = readings, power, fail
        self.logged_out = False
        self.ipmi_session = SimpleNamespace(logout=lambda: setattr(self, "logged_out", True))

    def get_sensor_data(self):  # type: ignore[no-untyped-def]
        if self.fail:
            raise self.fail
        for r in self.readings:
            yield SimpleNamespace(**r)

    def get_power(self) -> dict[str, str]:
        return {"powerstate": self.power}


READINGS = [
    {"name": "CPU1 Temp", "type": "Temperature", "value": 48.0, "units": "°C"},
    {"name": "CPU2 Temp", "type": "Temperature", "value": 51.0, "units": "°C"},
    {"name": "Inlet Temp", "type": "Temperature", "value": 23.0, "units": "°C"},
    {"name": "Fan1A", "type": "Fan", "value": 6840.0, "units": "RPM"},
    {"name": "Fan2A", "type": "Fan", "value": None, "units": "RPM", "unavailable": 1},
    {"name": "Pwr Consumption", "type": "Current", "value": 238.0, "units": "W"},
    {"name": "Intrusion", "type": "Physical Security", "value": None, "units": ""},
]


def test_ipmi_sensors_via_pyghmi() -> None:
    cmd = FakeCommand(READINGS)
    a = IpmiAdapter(client_factory=lambda c: PyghmiClient(c, command=cmd), reuse_sessions=False)
    p = pts(a.collect(ctx("ipmi", 623)))
    assert p["cpu.temp"].value == 51.0 and p["temp:Inlet_Temp"].value == 23.0
    assert p["fan.rpm:Fan1A"].value == 6840.0 and p["fan.rpm:Fan1A"].unit == "RPM"
    assert p["fan.rpm:Fan2A"].quality == "unsupported" and p["fan.rpm:Fan2A"].value is None
    assert p["power.watts"].value == 238.0 and p["power.on"].value == 1
    assert not any(k.startswith("fan.pct") for k in p)  # 不知道额定转速，不换算百分比
    assert cmd.logged_out  # 默认每次采集后登出


def test_ipmi_session_reuse() -> None:
    made: list[FakeCommand] = []

    def factory(c):  # type: ignore[no-untyped-def]
        cmd = FakeCommand(READINGS)
        made.append(cmd)
        return PyghmiClient(c, command=cmd)

    a = IpmiAdapter(client_factory=factory, reuse_sessions=True)
    a.collect(ctx("ipmi", 623))
    a.collect(ctx("ipmi", 623))
    assert len(made) == 1 and not made[0].logged_out


def test_ipmi_falls_back_to_ipmitool() -> None:
    out = "CPU1 Temp | 0Eh | ok | 3.1 | 47 degrees C\nFan1A | 30h | ok | 7.1 | 6960 RPM\nPS1 Status | 26h | ns | 10.1 | No Reading\n"

    def runner(cmd, **kw):  # type: ignore[no-untyped-def]
        assert FAKE_PW not in " ".join(cmd)  # 密码不出现在命令行
        assert kw["env"]["IPMI_PASSWORD"] == FAKE_PW
        text = out if "sdr" in cmd else "Chassis Power is on"
        return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")

    def broken(c):  # type: ignore[no-untyped-def]
        return PyghmiClient(c, command=FakeCommand([], fail=RuntimeError("timeout waiting for response")))

    def ipmitool(c):  # type: ignore[no-untyped-def]
        client = IpmitoolClient.__new__(IpmitoolClient)
        client.ctx, client.runner, client.exe = c, runner, "ipmitool"
        return client

    a = IpmiAdapter(client_factory=broken, fallback_factory=ipmitool, reuse_sessions=False)
    r = a.collect(ctx("ipmi", 623))
    p = pts(r)
    assert p["cpu.temp"].value == 47.0 and p["fan.rpm:Fan1A"].value == 6960.0
    assert p["power.on"].value == 1
    assert "已回退 ipmitool" in r.messages[0]


def test_ipmi_auth_error_does_not_fall_back() -> None:
    calls = []
    a = IpmiAdapter(
        client_factory=lambda c: PyghmiClient(c, command=FakeCommand([], fail=RuntimeError("Unauthorized name"))),
        fallback_factory=lambda c: calls.append(1),  # type: ignore[arg-type,return-value]
        reuse_sessions=False,
    )
    with pytest.raises(AdapterError) as e:
        a.collect(ctx("ipmi", 623))
    assert e.value.kind == "auth" and calls == []


def test_parse_sdr_elist() -> None:
    rows = parse_sdr_elist("Inlet Temp | 04h | ok | 7.1 | 22 degrees C\nVoltage 1 | 6Ch | ok | 10.1 | 232 Volts\nCPU2 Temp | 0Fh | ns | 3.2 | Disabled\n")
    assert rows[0] == {"name": "Inlet Temp", "type": "Temperature", "value": 22.0, "units": "degrees C", "unavailable": 0}
    assert rows[1]["type"] == "Voltage" and rows[2]["unavailable"] == 1


def test_ipmi_needs_credentials() -> None:
    with pytest.raises(AdapterError) as e:
        PyghmiClient(EndpointContext("192.0.2.50", 623, "ipmi"))
    assert e.value.kind == "unconfigured"


# ---- Redfish ----


def idrac_fixture() -> dict[str, Any]:
    return {
        "/redfish/v1/": {"RedfishVersion": "1.17.0", "Vendor": "Dell"},
        "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/System.Embedded.1"}]},
        "/redfish/v1/Systems/System.Embedded.1": {
            "Model": "PowerEdge R740", "SerialNumber": "FAKESN1", "HostName": "fake-r740", "PowerState": "On",
            "Status": {"Health": "OK", "HealthRollup": "Warning"}, "MemorySummary": {"TotalSystemMemoryGiB": 128},
            "Storage": {"@odata.id": "/redfish/v1/Systems/System.Embedded.1/Storage"},
        },
        "/redfish/v1/Systems/System.Embedded.1/Storage": {"Members": [{"@odata.id": "/redfish/v1/Systems/System.Embedded.1/Storage/RAID.Integrated.1-1"}]},
        "/redfish/v1/Systems/System.Embedded.1/Storage/RAID.Integrated.1-1": {
            "Drives": [{"@odata.id": "/d/0"}, {"@odata.id": "/d/1"}],
            "Volumes": {"@odata.id": "/v"},
        },
        "/d/0": {"Name": "Physical Disk 0:1:0", "Status": {"Health": "OK"}},
        "/d/1": {"Name": "Physical Disk 0:1:1", "Status": {"Health": "Critical"}},
        "/v": {"Members": [{"@odata.id": "/v/0"}]},
        "/v/0": {"Name": "Virtual Disk 0", "RAIDType": "RAID1", "Status": {"Health": "Warning"}},
        "/redfish/v1/Chassis": {"Members": [{"@odata.id": "/redfish/v1/Chassis/System.Embedded.1"}]},
        "/redfish/v1/Chassis/System.Embedded.1/Thermal": {
            "Temperatures": [{"Name": "CPU1 Temp", "ReadingCelsius": 49}, {"Name": "Exhaust Temp", "ReadingCelsius": None}],
            "Fans": [{"Name": "Fan 1A", "Reading": 6600, "ReadingUnits": "RPM"}, {"Name": "Fan Zone", "Reading": 35, "ReadingUnits": "Percent"}],
        },
        "/redfish/v1/Chassis/System.Embedded.1/Power": {"PowerControl": [{"PowerConsumedWatts": 245}]},
    }


class FakeIdrac:
    def __init__(self) -> None:
        self.data = idrac_fixture()
        self.logins = 0
        self.valid = {"tok-1"}
        self.deleted: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/redfish/v1/SessionService/Sessions":
            body = json.loads(request.content)
            if body != {"UserName": FAKE_USER, "Password": FAKE_PW}:
                return httpx.Response(401)
            self.logins += 1
            tok = f"tok-{self.logins}"
            self.valid.add(tok)
            return httpx.Response(201, headers={"X-Auth-Token": tok, "Location": "/redfish/v1/SessionService/Sessions/1"})
        if request.method == "DELETE":
            self.deleted.append(path)
            return httpx.Response(204)
        assert request.method == "GET"  # 采集只读
        if request.headers.get("X-Auth-Token") not in self.valid:
            return httpx.Response(401)
        return httpx.Response(200, json=self.data[path]) if path in self.data else httpx.Response(404)


def redfish(fake: FakeIdrac, fp: str = "AA:BB") -> RedfishAdapter:
    return RedfishAdapter(transport=httpx.MockTransport(fake), fingerprint=lambda a, p, t: fp)


def test_redfish_collect() -> None:
    fake = FakeIdrac()
    c = ctx("redfish", 443)
    r = redfish(fake).collect(c)
    p = pts(r)
    assert r.facts == {"model": "PowerEdge R740", "serial": "FAKESN1", "hostname": "fake-r740"}
    assert p["health.system"].value == 0.5 and p["power.on"].value == 1
    assert p["mem.total_bytes"].value == 128 * 1024**3
    assert p["disk.health:Physical_Disk_0:1:0"].value == 1 and p["disk.health:Physical_Disk_0:1:1"].value == 0
    assert p["raid.health:Virtual_Disk_0"].value == 0.5 and "RAID1" in p["raid.health:Virtual_Disk_0"].message
    assert p["temp:CPU1_Temp"].value == 49.0 and p["temp:Exhaust_Temp"].quality == "unsupported"
    assert p["fan.rpm:Fan_1A"].value == 6600.0 and p["fan.pct:Fan_Zone"].value == 35.0
    assert p["power.watts"].value == 245.0
    assert c.trust.observed_tls == "AA:BB"


def test_redfish_session_reused_and_renewed() -> None:
    fake = FakeIdrac()
    a = redfish(fake)
    a.collect(ctx("redfish", 443))
    a.collect(ctx("redfish", 443))
    assert fake.logins == 1  # 第二次复用令牌
    fake.valid.clear()  # 令牌失效
    a.collect(ctx("redfish", 443))
    assert fake.logins == 2
    a.logout(ctx("redfish", 443))
    assert fake.deleted == ["/redfish/v1/SessionService/Sessions/1"]


def test_redfish_pinned_fingerprint_mismatch_stops_before_login() -> None:
    fake = FakeIdrac()
    c = ctx("redfish", 443, trust=Trust(tls_fingerprint="AA:BB"))
    with pytest.raises(AdapterError) as e:
        redfish(fake, fp="CC:DD").collect(c)
    assert e.value.kind == "untrusted" and fake.logins == 0


def test_redfish_wrong_password() -> None:
    c = EndpointContext("192.0.2.50", 443, "redfish", username=FAKE_USER, secret="wrong")
    with pytest.raises(AdapterError) as e:
        redfish(FakeIdrac()).collect(c)
    assert e.value.kind == "auth"


# ---- 真实 TLS：本机自签证书，验证指纹读取 ----


def _self_signed(tmp: Path) -> tuple[Path, Path, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-idrac")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cp, kp = tmp / "cert.pem", tmp / "key.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    hexd = cert.fingerprint(hashes.SHA256()).hex().upper()
    return cp, kp, ":".join(hexd[i : i + 2] for i in range(0, len(hexd), 2))


def test_cert_fingerprint_from_real_tls_server(tmp_path: Path) -> None:
    cp, kp, expected = _self_signed(tmp_path)
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    sslctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sslctx.load_cert_chain(cp, kp)
    server.socket = sslctx.wrap_socket(server.socket, server_side=True)
    th = threading.Thread(target=server.handle_request, daemon=True)
    th.start()
    try:
        assert cert_fingerprint("127.0.0.1", server.server_address[1], 5) == expected
    finally:
        server.server_close()


def test_redfish_without_credentials_does_not_touch_device() -> None:
    touched: list[str] = []

    def fake(req: httpx.Request) -> httpx.Response:
        touched.append(str(req.url))
        return httpx.Response(500)

    a = RedfishAdapter(transport=httpx.MockTransport(fake), fingerprint=lambda *_: touched.append("tls") or "")
    with pytest.raises(AdapterError) as e:
        a.collect(EndpointContext("192.0.2.51", 443, "redfish"))
    assert e.value.kind == "unconfigured" and touched == []
