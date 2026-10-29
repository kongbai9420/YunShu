"""按 MAC / IP 找端口（4.2 P14，参考 Netdisco 的做法）。

- 交换机 SNMP 采集每 10 分钟读一次 MAC 地址表（FDB）和 ARP 表，写入 mac_observations；
  同一交换机这次读到的条目刷新 last_seen，没再出现的条目保留 7 天后清理（设备离线时仍能查到最后位置）。
- 查询：IP 先经 ARP 表换成 MAC，再看哪些交换机端口学到了这个 MAC。一个端口上学到的 MAC 越少越可能是接入口；
  学到很多 MAC 或有 LLDP 邻居的端口标为“上联口”，排在后面。
- 只是发现证据：不据此合并资产、不认定物理链路。
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import Asset, CollectionStatus, MacObservation, ManagementEndpoint

KEEP = timedelta(days=7)
UPLINK_MACS = 8  # 一个端口学到超过这么多 MAC，按上联口处理


def normalize_mac(text: str) -> str | None:
    raw = re.sub(r"[^0-9a-fA-F]", "", text or "")
    return raw.lower() if len(raw) == 12 else None


def format_mac(mac: str) -> str:
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2))


def record(db: Session, switch_id: uuid.UUID, tables: dict[str, list[dict[str, Any]]], now: datetime) -> int:
    existing = {(o.source, o.mac, o.ip, o.port): o for o in db.scalars(select(MacObservation).where(MacObservation.switch_asset_id == switch_id))}
    seen = 0
    for source, rows in (("fdb", tables.get("fdb") or []), ("arp", tables.get("arp") or [])):
        for row in rows:
            mac = normalize_mac(str(row.get("mac", "")))
            if mac is None:
                continue
            key = (source, mac, str(row.get("ip") or "")[:64], str(row.get("port") or "")[:200])
            obs = existing.get(key)
            if obs is None:
                obs = MacObservation(switch_asset_id=switch_id, source=source, mac=mac, ip=key[2], port=key[3],
                                     vlan=row.get("vlan"), first_seen_at=now, last_seen_at=now)
                db.add(obs)
                existing[key] = obs
            else:
                obs.last_seen_at, obs.vlan = now, row.get("vlan", obs.vlan)
            seen += 1
    # synchronize_session="fetch"：SQLite 读回的时间不带时区，不能在 Python 里和带时区的时间比较
    db.execute(delete(MacObservation).where(MacObservation.switch_asset_id == switch_id, MacObservation.last_seen_at < now - KEEP)
               .execution_options(synchronize_session="fetch"))
    return seen


def search(db: Session, query: str, visible: Any | None) -> dict[str, Any]:
    """visible：可见资产 ID 的子查询（范围受限账号），None 表示全部可见。"""
    q = (query or "").strip()
    mac = normalize_mac(q)
    ip = ""
    if mac is None:
        try:
            ip = str(ipaddress.ip_address(q))
        except ValueError:
            return {"query": q, "kind": "invalid", "message": "请输入 MAC 地址（如 00:11:22:33:44:55）或 IP 地址", "macs": [], "locations": []}

    def scoped(stmt):  # type: ignore[no-untyped-def]
        return stmt if visible is None else stmt.where(MacObservation.switch_asset_id.in_(visible))

    macs = [mac] if mac else sorted(set(db.scalars(scoped(select(MacObservation.mac).where(MacObservation.source == "arp", MacObservation.ip == ip)))))
    arp = db.scalars(scoped(select(MacObservation).where(MacObservation.source == "arp", MacObservation.mac.in_(macs)))).all() if macs else []
    fdb = db.scalars(scoped(select(MacObservation).where(MacObservation.source == "fdb", MacObservation.mac.in_(macs)))).all() if macs else []
    names = {a.id: a.name for a in db.scalars(select(Asset).where(Asset.id.in_({o.switch_asset_id for o in [*arp, *fdb]})))}
    port_load = {(sw, port): n for sw, port, n in db.execute(
        select(MacObservation.switch_asset_id, MacObservation.port, func.count())
        .where(MacObservation.source == "fdb", MacObservation.switch_asset_id.in_(list(names)))
        .group_by(MacObservation.switch_asset_id, MacObservation.port))}
    lldp_ports = _lldp_ports(db, set(names))
    locations = []
    for o in fdb:
        count = port_load.get((o.switch_asset_id, o.port), 1)
        uplink = count > UPLINK_MACS or o.port in lldp_ports.get(o.switch_asset_id, set())
        locations.append({"switch_id": str(o.switch_asset_id), "switch_name": names.get(o.switch_asset_id, ""), "port": o.port,
                          "vlan": o.vlan, "mac": format_mac(o.mac), "macs_on_port": count, "uplink": uplink,
                          "first_seen_at": o.first_seen_at, "last_seen_at": o.last_seen_at})
    locations.sort(key=lambda x: (x["uplink"], x["macs_on_port"], x["switch_name"], x["port"]))
    ips = sorted({o.ip for o in arp if o.ip})
    # 管理端点地址相同的资产，只作提示，不自动认定
    addresses = [x for x in (ip, *ips) if x]
    owners = list(db.scalars(select(Asset).where(Asset.id.in_(
        select(ManagementEndpoint.asset_id).where(ManagementEndpoint.address.in_(addresses)))))) if addresses else []
    if visible is not None:
        allowed = set(db.scalars(visible))
        owners = [a for a in owners if a.id in allowed]
    return {"query": q, "kind": "mac" if mac else "ip", "macs": [format_mac(m) for m in macs], "ips": ips,
            "assets": [{"id": str(a.id), "name": a.name} for a in owners], "locations": locations,
            "message": "" if locations else ("没有交换机报告过这个地址" if macs else "ARP 表里没有这个 IP")}


def _lldp_ports(db: Session, switch_ids: set[uuid.UUID]) -> dict[uuid.UUID, set[str]]:
    out: dict[uuid.UUID, set[str]] = {}
    if not switch_ids:
        return out
    for st in db.scalars(select(CollectionStatus).where(CollectionStatus.asset_id.in_(switch_ids))):
        for nb in (st.facts or {}).get("lldp_neighbors") or []:
            if isinstance(nb, dict) and nb.get("local_port"):
                out.setdefault(st.asset_id, set()).add(str(nb["local_port"]))
    return out
