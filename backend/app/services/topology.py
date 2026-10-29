"""拓扑草稿生成与增量合并（落地方案 P3：发现 → 草稿 → 手动修改 → 增量合并）。

输入只有数据库里已有的采集事实，不连接任何设备：
- SNMP 采集保存的 LLDP 邻居表（CollectionStatus.facts["lldp_neighbors"]）；
- 远端设备按名称匹配资产：采集到的 sys_name / hostname 或资产名称，不区分大小写，
  可去掉域名后缀再比。**不按 IP 匹配或合并资产**：远端名称本身是 IP 时不匹配；
  同名匹配到多台资产时不猜，列为“未匹配”。

合并规则（三层分存）：
- 只会新增、刷新或移除 origin=discovered、state=candidate、未锁定的链路；
- 人工链路、已确认链路、已忽略（rejected）链路和视图布局一律不动；
- 已忽略的同一指纹不再提出；两台资产之间已有人工或已确认链路时不再提候选；
- 端口级并行链路（4.2 P14）：指纹由“两台资产 + 两端端口”组成，同一对设备之间的多条线各是一条候选。
  人工 / 已确认链路填了端口时只覆盖同一对端口；没填端口时仍覆盖整对设备。4.2 之前按设备对生成的
  已忽略指纹继续压制整对设备的候选；旧的按设备对的候选在下次合并时换成按端口的候选；
- 某条候选的所有上报设备这次都成功读到了邻居表、却都没再报告它，才算“不再发现”并移除；
  有上报设备这次没读到（采集失败、设备不支持）的候选保留不动。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, CollectionStatus, NetworkLink

PROTECTED_ORIGINS = ("manual", "confirmed")


@dataclass
class Proposal:
    fingerprint: str
    source_asset_id: uuid.UUID
    target_asset_id: uuid.UUID
    source_port: str
    target_port: str
    observations: list[dict[str, str]]
    observed_at: str | None

    def evidence(self) -> dict[str, Any]:
        return {"source": "lldp", "observations": self.observations, "observed_at": self.observed_at}

    def stable(self) -> list[Any]:
        """比较“是否有变化”用的部分：端口与观察内容，不含时间。"""
        return [str(self.source_asset_id), str(self.target_asset_id), self.source_port, self.target_port, self.observations]


@dataclass
class Plan:
    added: list[Proposal] = field(default_factory=list)
    refreshed: list[tuple[NetworkLink, Proposal]] = field(default_factory=list)
    unchanged: list[tuple[NetworkLink, Proposal]] = field(default_factory=list)
    removed: list[NetworkLink] = field(default_factory=list)
    kept_unreadable: list[NetworkLink] = field(default_factory=list)
    skipped_rejected: list[Proposal] = field(default_factory=list)
    skipped_covered: list[Proposal] = field(default_factory=list)
    unmatched: list[dict[str, str]] = field(default_factory=list)
    protected: int = 0
    readable_reporters: int = 0

    @property
    def token(self) -> str:
        """预览与应用之间结果变了，应用时返回 409，让人重新预览，不静默合并。"""
        data = {
            "added": sorted([p.fingerprint, p.stable()] for p in self.added),
            "refreshed": sorted([str(link.id), p.stable()] for link, p in self.refreshed),
            "removed": sorted(str(link.id) for link in self.removed),
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:32]


def _norm(name: object) -> str:
    return str(name or "").strip().lower()


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text.split("%")[0])
    except ValueError:
        return False
    return True


def _keys(name: object) -> list[str]:
    """名称的匹配键：完整名称，以及去掉域名后缀的短名。IP 形式的名称不参与匹配。"""
    full = _norm(name)
    if not full or _is_ip(full):
        return []
    short = full.split(".")[0]
    return [full] if short == full or not short else [full, short]


def lldp_fingerprint(a: uuid.UUID, b: uuid.UUID, a_port: str = "", b_port: str = "") -> str:
    """不带端口时是 4.2 之前的设备对指纹；带端口时按资产 ID 排序后两端端口跟着换位。"""
    (lo, lo_port), (hi, hi_port) = sorted(((str(a), a_port), (str(b), b_port)))
    if not a_port and not b_port:
        return f"lldp:{lo}|{hi}"
    return f"lldp:{lo}|{hi}|{_port(lo_port)}|{_port(hi_port)}"


def _port(name: str) -> str:
    return " ".join(str(name or "").lower().split())


def _is_pair_fingerprint(fp: str) -> bool:
    return fp.startswith("lldp:") and fp.count("|") == 1


def pair_key(a: uuid.UUID, b: uuid.UUID) -> tuple[str, str]:
    lo, hi = sorted((str(a), str(b)))
    return lo, hi


def build_plan(db: Session) -> Plan:
    plan = Plan()
    # 回收站里的资产既不作为上报方，也不作为匹配对象
    assets = {a.id: a for a in db.scalars(select(Asset).where(Asset.deleted_at.is_(None)))}
    statuses = db.scalars(select(CollectionStatus)).all()

    # 名称索引：资产名称 + 采集到的系统名 / 主机名
    index: dict[str, set[uuid.UUID]] = defaultdict(set)
    for asset in assets.values():
        for key in _keys(asset.name):
            index[key].add(asset.id)
    for st in statuses:
        if st.asset_id not in assets:
            continue
        for fact in ("sys_name", "hostname"):
            for key in _keys((st.facts or {}).get(fact)):
                index[key].add(st.asset_id)

    def match(remote_name: str) -> tuple[uuid.UUID | None, str]:
        keys = _keys(remote_name)
        if not keys:
            if _is_ip(_norm(remote_name)):
                return None, "远端名称是 IP 地址，不按 IP 匹配资产"
            return None, "远端没有报告名称"
        for key in keys:
            found = index.get(key, set())
            if len(found) == 1:
                return next(iter(found)), ""
            if len(found) > 1:
                return None, f"名称“{remote_name}”对应多台资产，未自动选择"
        return None, f"没有名称为“{remote_name}”的资产"

    readable: set[uuid.UUID] = set()
    observed: dict[str, list[tuple[uuid.UUID, dict[str, str]]]] = defaultdict(list)
    observed_at: dict[str, str] = {}
    for st in sorted(statuses, key=lambda s: str(s.endpoint_id)):
        neighbors = (st.facts or {}).get("lldp_neighbors")
        if st.state != "ok" or not isinstance(neighbors, list) or st.asset_id not in assets:
            continue
        readable.add(st.asset_id)
        when = st.last_success_at.isoformat() if st.last_success_at else None
        for nb in neighbors:
            if not isinstance(nb, dict):
                continue
            obs = {k: str(nb.get(k, ""))[:200] for k in ("local_port", "remote_name", "remote_port", "remote_chassis")}
            target, reason = match(obs["remote_name"])
            if target == st.asset_id:
                continue  # 自己的回环或误报
            if target is None:
                plan.unmatched.append({"reporter_asset_id": str(st.asset_id), "reporter_name": assets[st.asset_id].name,
                                       "local_port": obs["local_port"], "remote_name": obs["remote_name"], "reason": reason})
                continue
            fp = lldp_fingerprint(st.asset_id, target, obs["local_port"] or "?", obs["remote_port"] or "?")
            item = (st.asset_id, {"reporter_asset_id": str(st.asset_id), "neighbor_asset_id": str(target), **obs})
            if item not in observed[fp]:
                observed[fp].append(item)
            if when and when > observed_at.get(fp, ""):
                observed_at[fp] = when
    plan.readable_reporters = len(readable)

    proposals: dict[str, Proposal] = {}
    for fp, items in observed.items():
        items.sort(key=lambda it: (it[1]["reporter_asset_id"], it[1]["local_port"], it[1]["remote_port"]))
        reporter = items[0][0]
        mine = [o for r, o in items if r == reporter]
        target = uuid.UUID(mine[0]["neighbor_asset_id"])
        proposals[fp] = Proposal(
            fingerprint=fp, source_asset_id=reporter, target_asset_id=target,
            source_port=",".join(dict.fromkeys(o["local_port"] for o in mine if o["local_port"]))[:200],
            target_port=",".join(dict.fromkeys(o["remote_port"] for o in mine if o["remote_port"]))[:200],
            observations=[o for _, o in items], observed_at=observed_at.get(fp),
        )

    links = db.scalars(select(NetworkLink)).all()
    by_fp: dict[str, NetworkLink] = {}
    covered_pairs: set[tuple[str, str]] = set()
    covered_ports: set[str] = set()
    rejected_pairs: set[tuple[str, str]] = set()
    for link in links:
        if link.fingerprint:
            by_fp[link.fingerprint] = link
            if link.state == "rejected" and _is_pair_fingerprint(link.fingerprint):
                rejected_pairs.add(pair_key(link.source_asset_id, link.target_asset_id))
        if link.origin in PROTECTED_ORIGINS or link.locked:
            plan.protected += 1
            if link.state != "rejected":
                if link.source_port or link.target_port:
                    covered_ports.add(lldp_fingerprint(link.source_asset_id, link.target_asset_id, link.source_port or "?", link.target_port or "?"))
                else:
                    covered_pairs.add(pair_key(link.source_asset_id, link.target_asset_id))

    for fp, prop in sorted(proposals.items()):
        existing = by_fp.get(fp)
        if (existing is not None and existing.state == "rejected") or pair_key(prop.source_asset_id, prop.target_asset_id) in rejected_pairs:
            plan.skipped_rejected.append(prop)
        elif existing is not None and (existing.origin in PROTECTED_ORIGINS or existing.locked):
            plan.skipped_covered.append(prop)
        elif pair_key(prop.source_asset_id, prop.target_asset_id) in covered_pairs or fp in covered_ports:
            plan.skipped_covered.append(prop)
        elif existing is None:
            plan.added.append(prop)
        elif _stable_of(existing) == prop.stable():
            plan.unchanged.append((existing, prop))
        else:
            plan.refreshed.append((existing, prop))

    for link in links:
        if link.origin != "discovered" or link.state != "candidate" or link.locked or link.fingerprint in proposals:
            continue
        reporters = {uuid.UUID(o["reporter_asset_id"]) for o in (link.evidence or {}).get("observations", []) if o.get("reporter_asset_id")}
        if reporters and reporters <= readable:
            plan.removed.append(link)
        else:
            plan.kept_unreadable.append(link)
    return plan


def _stable_of(link: NetworkLink) -> list[Any]:
    return [str(link.source_asset_id), str(link.target_asset_id), link.source_port, link.target_port,
            (link.evidence or {}).get("observations", [])]


def apply_plan(db: Session, plan: Plan, *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    for prop in plan.added:
        db.add(NetworkLink(
            source_asset_id=prop.source_asset_id, target_asset_id=prop.target_asset_id,
            source_port=prop.source_port, target_port=prop.target_port, kind="ethernet",
            origin="discovered", state="candidate", fingerprint=prop.fingerprint, evidence=prop.evidence(),
            locked=False, last_seen_at=now,
        ))
    for link, prop in plan.refreshed:
        link.source_asset_id, link.target_asset_id = prop.source_asset_id, prop.target_asset_id
        link.source_port, link.target_port = prop.source_port, prop.target_port
        link.evidence, link.last_seen_at = prop.evidence(), now
    for link, prop in plan.unchanged:
        link.evidence, link.last_seen_at = prop.evidence(), now
    for link in plan.removed:
        db.delete(link)
    db.flush()
    return {"added": len(plan.added), "refreshed": len(plan.refreshed), "unchanged": len(plan.unchanged),
            "removed": len(plan.removed), "protected": plan.protected}
