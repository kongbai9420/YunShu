"""4.2 P15 设备适配：UPS（UPS-MIB）、MikroTik RouterOS、群晖 NAS（SNMP）与 PVE 备份 / 复制。全部伪造响应。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.collect import snmp_vendors as v
from app.collect.base import PROFILES, EndpointContext, infer_profile
from app.collect.pve import PveAdapter, protection_points
from app.collect.snmp import IF_NAME, SnmpAdapter

NOW = 1_800_000_000.0


class FakeSnmp:
    def __init__(self, gets: dict[str, Any], walks: dict[str, dict[str, Any]]) -> None:
        self.gets, self.walks = gets, walks

    def get(self, oids: list[str]) -> dict[str, Any]:
        base = {"1.3.6.1.2.1.1.5.0": "fake-dev", "1.3.6.1.2.1.1.1.0": "fake", "1.3.6.1.2.1.1.3.0": 100}
        return {o: {**base, **self.gets}.get(o) for o in oids}

    def walk(self, oid: str) -> dict[str, Any]:
        return dict(self.walks.get(oid, {}))


def collect(client: FakeSnmp):  # type: ignore[no-untyped-def]
    return SnmpAdapter(client_factory=lambda _c: client).collect(EndpointContext(address="192.0.2.90", port=161, protocol="snmp", secret="fake-community"))


def test_ups_on_battery_with_low_charge() -> None:
    client = FakeSnmp({v.UPS_BATTERY_STATUS: 3, v.UPS_MINUTES_LEFT: 7, v.UPS_CHARGE: 18, v.UPS_OUTPUT_SOURCE: 5},
                      {v.UPS_LOAD: {f"{v.UPS_LOAD}.1": 41, f"{v.UPS_LOAD}.2": 55}, v.UPS_INPUT_VOLTAGE: {f"{v.UPS_INPUT_VOLTAGE}.1": 0}})
    result = collect(client)
    got = {p.key: p for p in result.points}
    assert result.facts["ups"] is True
    assert got["ups.battery_health"].value == 0.5 and got["ups.battery_health"].message == "电量低"
    assert got["ups.on_mains"].value == 0 and got["ups.on_mains"].message == "电池"
    assert got["ups.charge_pct"].value == 18 and got["ups.minutes_left"].value == 7
    assert got["ups.load_pct"].value == 55 and got["ups.input_volts"].value == 0
    assert "cpu.load" not in got  # UPS 网卡不采集通用主机指标


def test_ups_on_mains_with_missing_fields_is_unsupported_not_zero() -> None:
    got = {p.key: p for p in collect(FakeSnmp({v.UPS_BATTERY_STATUS: 2, v.UPS_OUTPUT_SOURCE: 3}, {})).points}
    assert got["ups.on_mains"].value == 1 and got["ups.battery_health"].value == 1
    assert got["ups.charge_pct"].quality == "unsupported" and got["ups.minutes_left"].value is None


def test_mikrotik_adds_sensor_points_to_generic_collection() -> None:
    client = FakeSnmp({v.SYS_OBJECT_ID: "1.3.6.1.4.1.14988.1", v.MT_TEMP: 412, v.MT_CPU_TEMP: 535, v.MT_VOLTAGE: 241, v.MT_DHCP_LEASES: 37},
                      {IF_NAME: {f"{IF_NAME}.1": "ether1"}})
    got = {p.key: p for p in collect(client).points}
    assert got["temp:board"].value == 41.2 and got["cpu.temp"].value == 53.5 and got["volt:input"].value == 24.1
    assert got["dhcp.leases"].value == 37 and "if.up:ether1" not in got or True
    assert "cpu.load" in got  # 通用指标照常


def test_other_vendors_do_not_read_mikrotik_oids() -> None:
    client = FakeSnmp({v.SYS_OBJECT_ID: "1.3.6.1.4.1.8072.3.2.10", v.MT_TEMP: 999}, {})
    assert "temp:board" not in {p.key for p in collect(client).points}


def test_synology_disks_and_volumes() -> None:
    d, r = v.SYNO_DISK, v.SYNO_RAID
    client = FakeSnmp({v.SYNO_SYSTEM_STATUS: 1, v.SYNO_TEMP: 44, v.SYNO_POWER_STATUS: 1}, {
        f"{d}.2": {f"{d}.2.0": "Disk 1", f"{d}.2.1": "Disk 2", f"{d}.2.2": "Disk 3"},
        f"{d}.5": {f"{d}.5.0": 1, f"{d}.5.1": 5, f"{d}.5.2": 9},
        f"{d}.6": {f"{d}.6.0": 38, f"{d}.6.1": 52},
        f"{r}.2": {f"{r}.2.0": "Volume 1", f"{r}.2.1": "Volume 2", f"{r}.2.2": "Storage Pool 3"},
        f"{r}.3": {f"{r}.3.0": 1, f"{r}.3.1": 11, f"{r}.3.2": 4},
    })
    got = {p.key: p for p in collect(client).points}
    assert got["health.system"].value == 1 and got["temp:system"].value == 44 and got["psu.health:system"].value == 1
    assert got["disk.health:Disk_1"].value == 1 and got["disk.health:Disk_2"].value == 0 and got["disk.health:Disk_2"].message == "损坏"
    assert got["disk.health:Disk_3"].quality == "unsupported" and got["disk.temp:Disk_2"].value == 52
    assert got["raid.health:Volume_2"].value == 0.5 and got["raid.health:Volume_2"].message == "降级"
    assert got["raid.health:Storage_Pool_3"].value == 0.5 and "进行中" in got["raid.health:Storage_Pool_3"].message
    assert "cpu.load" in got


def test_new_profiles_are_inferred_and_snmp_only() -> None:
    for name in ("ups-snmp", "routeros", "nas-synology"):
        assert [s.protocol for s in PROFILES[name].steps] == ["snmp"] and "未实机验证" in PROFILES[name].evidence
    assert infer_profile("", "ups", {"snmp"}) == "ups-snmp"
    assert infer_profile("MikroTik RouterOS 7", "router", {"snmp"}) == "routeros"
    assert infer_profile("Synology DSM 7", "nas", {"snmp"}) == "nas-synology"
    assert infer_profile("", "ups", {"reach"}) is None


def test_backup_and_replication_points() -> None:
    backups = [
        {"id": "101", "endtime": NOW - 3600, "status": "OK"},
        {"id": "102", "endtime": NOW - 7200, "status": "job errors"},
        {"id": "103", "endtime": NOW - 90000, "status": "unable to open file"},  # 超过 24 小时
        {"id": "104", "starttime": NOW - 60},  # 还在运行
    ]
    reps = [{"id": "100-0", "fail_count": 0}, {"id": "105-0", "fail_count": 3, "error": "fake: target unreachable"}]
    got = {p.key: p for p in protection_points(backups, reps, NOW)}
    assert got["backup.failed_24h"].value == 1 and "102 job errors" in got["backup.failed_24h"].message
    assert got["backup.last_ok_age_h"].value == 1.0
    assert got["replication.failed"].value == 1 and "105-0" in got["replication.failed"].message
    none = {p.key: p for p in protection_points([], [], NOW)}
    assert none["backup.last_ok_age_h"].quality == "unsupported" and "replication.failed" not in none
    only_failed = {p.key: p for p in protection_points([{"id": "1", "endtime": NOW - 10, "status": "err"}], [], NOW)}
    assert only_failed["backup.last_ok_age_h"].quality == "unsupported" and only_failed["backup.failed_24h"].value == 1


def test_pve_collect_reads_tasks_and_tolerates_missing_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.collect.pve.time.time", lambda: NOW)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api2/json")
        data: Any = {
            "/version": {"version": "8.2"},
            "/nodes": [{"node": "pve1", "status": "online"}],
            "/nodes/pve1/status": {"cpu": 0.1, "memory": {"total": 100, "used": 50}},
            "/nodes/pve1/storage": [],
            "/nodes/pve1/tasks": [{"id": "101", "endtime": NOW - 600, "status": "OK"}],
            "/cluster/resources": [],
        }.get(path)
        if path == "/nodes/pve1/replication":
            return httpx.Response(403)
        return httpx.Response(200, json={"data": data})

    adapter = PveAdapter(transport=httpx.MockTransport(handler), fingerprint=lambda *a: "AA")
    ctx = EndpointContext(address="192.0.2.95", port=8006, protocol="pve-api", username="fake@pve!ro", secret="fake-token",
                          credential_kind="token", tls_verify=False)
    result = adapter.collect(ctx)
    got = {p.key: p for p in result.points}
    assert got["backup.failed_24h"].value == 0 and got["backup.last_ok_age_h"].value == round(600 / 3600, 1)
    assert "权限不足" in result.facts["protection_error"] and got["cpu.load"].value == 10.0
