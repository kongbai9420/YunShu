"""采集适配器。新增协议时在 build_adapters 里登记。"""

from __future__ import annotations

from app.collect.base import Adapter
from app.collect.esxi import EsxiAdapter
from app.collect.ipmi import IpmiAdapter
from app.collect.pve import PveAdapter
from app.collect.reach import ReachAdapter
from app.collect.redfish import RedfishAdapter
from app.collect.snmp import SnmpAdapter
from app.collect.ssh import SshAdapter


def build_adapters() -> dict[str, Adapter]:
    return {
        "reach": ReachAdapter(), "snmp": SnmpAdapter(), "ssh": SshAdapter(), "ipmi": IpmiAdapter(), "redfish": RedfishAdapter(),
        "pve-api": PveAdapter(), "esxi-api": EsxiAdapter(),
    }
