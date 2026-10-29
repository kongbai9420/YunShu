"""漏洞比对（4.3 P18d）：用软件清单对照漏洞的受影响版本区间，结论由程序给出，不交给 AI。

- 发行版软件包（Debian / Ubuntu，含 PVE）：按 dpkg 的版本比较规则，对照 OSV 的 introduced / fixed / last_affected；
  Debian 会把修复回移到旧版本，所以必须用发行版自己的数据，不能用 NVD 的上游版本号。
- 固件与设备系统：对照 NVD 配置里的版本区间（起止、含不含）或精确版本；
  配置里还要求同时满足其他条件（如特定硬件）、或只能按大版本判断（ESXi 没有构建号）时，结论为“可能受影响”。
"""
from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import inventory, metrics
from app.metrics import Point
from app.models import Asset, AssetPackage, CollectionStatus, VulnAffect, VulnMatch, VulnRecord

# ---------------- 版本比较 ----------------


def _order(c: str) -> int:
    if c == "~":
        return -1
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _verrevcmp(a: str, b: str) -> int:
    i = j = 0
    while i < len(a) or j < len(b):
        first_diff = 0
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _order(a[i]) if i < len(a) else 0
            bc = _order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


def _split_deb(v: str) -> tuple[int, str, str]:
    epoch = 0
    if ":" in v:
        e, v = v.split(":", 1)
        epoch = int(e) if e.isdigit() else 0
    upstream, _, revision = v.rpartition("-") if "-" in v else (v, "", "")
    return epoch, upstream, revision


def dpkg_compare(a: str, b: str) -> int:
    """与 dpkg --compare-versions 相同的规则；返回负数 / 0 / 正数。"""
    ea, ua, ra = _split_deb(a.strip())
    eb, ub, rb = _split_deb(b.strip())
    if ea != eb:
        return ea - eb
    return _verrevcmp(ua, ub) or _verrevcmp(ra, rb)


_TOKEN = re.compile(r"\d+|[A-Za-z]+")


def generic_compare(a: str, b: str) -> int:
    """固件版本：按数字段逐段比较（7.00.00.171、7.2-64570-3、2.19.1），字母段按字母序，缺的段当 0。"""
    ta, tb = _TOKEN.findall(a.lower()), _TOKEN.findall(b.lower())
    for i in range(max(len(ta), len(tb))):
        x = ta[i] if i < len(ta) else "0"
        y = tb[i] if i < len(tb) else "0"
        if x.isdigit() and y.isdigit():
            if int(x) != int(y):
                return 1 if int(x) > int(y) else -1
        elif x != y:
            if x.isdigit() != y.isdigit():
                return 1 if x.isdigit() else -1  # 1.0 > 1.0beta
            return 1 if x > y else -1
    return 0


# ---------------- 区间判断 ----------------


@dataclass
class Hit:
    confidence: str
    fixed: str = ""
    note: str = ""


def pkg_hit(version: str, ranges: list[dict[str, Any]]) -> Hit | None:
    """OSV 区间：introduced <= v < fixed，或 v <= last_affected；只有 introduced 表示尚未修复。"""
    for r in ranges:
        intro = r.get("introduced", "0")
        if intro not in ("", "0") and dpkg_compare(version, intro) < 0:
            continue
        if r.get("fixed"):
            if dpkg_compare(version, r["fixed"]) < 0:
                return Hit("affected", fixed=r["fixed"])
            continue
        if r.get("last_affected"):
            if dpkg_compare(version, r["last_affected"]) <= 0:
                return Hit("affected", note="发行版尚未发布修复版本")
            continue
        return Hit("affected", note="发行版尚未发布修复版本")
    return None


def _esxi_parts(version: str) -> tuple[str, int]:
    """8.0.2 → (8.0, 2)：ESXi 的第三段是 Update 编号。"""
    parts = version.split(".")
    return ".".join(parts[:2]), int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0


def cpe_hit(product: str, version: str, ranges: list[dict[str, Any]]) -> Hit | None:
    for r in ranges:
        possible = bool(r.get("and_other"))
        note = "该漏洞还要求同时满足其他条件（如特定硬件型号），请对照厂商公告" if possible else ""
        if r.get("exact"):
            if product in ("vmware:esxi", "vmware:vcenter_server"):
                base, upd = _esxi_parts(version)
                if generic_compare(base, r["exact"]) != 0:
                    continue
                m = re.search(r"(\d+)", r.get("update") or "")
                want = int(m.group(1)) if m else 0 if (r.get("update") or "") in ("", "ga") else None
                if want is None or want == upd:
                    return Hit("possible", note="VMware 按补丁构建号区分是否修复，请对照 VMSA 公告核对构建号")
                continue
            if generic_compare(version, r["exact"]) == 0:
                return Hit("possible" if possible else "affected", note=note)
            continue
        if not any(r.get(k) for k in ("start_incl", "start_excl", "end_incl", "end_excl")):
            continue  # 版本写成 * 且没有区间：表示所有版本，太宽泛，不据此判断
        if r.get("start_incl") and generic_compare(version, r["start_incl"]) < 0:
            continue
        if r.get("start_excl") and generic_compare(version, r["start_excl"]) <= 0:
            continue
        if r.get("end_excl") and generic_compare(version, r["end_excl"]) >= 0:
            continue
        if r.get("end_incl") and generic_compare(version, r["end_incl"]) > 0:
            continue
        return Hit("possible" if possible else "affected", fixed=r.get("end_excl", ""), note=note)
    return None


# ---------------- 比对全部资产 ----------------


def run(db: Session, *, now: datetime | None = None, asset_ids: list[uuid.UUID] | None = None) -> dict[str, int]:
    """重新比对（默认全部在用资产），更新 vuln_matches；不再命中的标为已解决。返回统计。"""
    now = now or datetime.now(UTC)
    stmt = select(Asset).where(Asset.lifecycle == "active")
    if asset_ids is not None:
        stmt = stmt.where(Asset.id.in_(asset_ids))
    assets = list(db.scalars(stmt))
    ids = [a.id for a in assets]
    statuses: dict[uuid.UUID, list[CollectionStatus]] = defaultdict(list)
    for st in db.scalars(select(CollectionStatus).where(CollectionStatus.asset_id.in_(ids))):
        statuses[st.asset_id].append(st)

    found: dict[tuple[uuid.UUID, str, str], tuple[Hit, str, str]] = {}
    # 固件 / 设备系统
    items_by_cpe: dict[str, list[tuple[uuid.UUID, dict[str, Any]]]] = defaultdict(list)
    for a in assets:
        for item in inventory.software_for(a, statuses.get(a.id, [])):
            if item["cpe"]:
                items_by_cpe[item["cpe"]].append((a.id, item))
    if items_by_cpe:
        for aff in db.scalars(select(VulnAffect).where(VulnAffect.kind == "cpe", VulnAffect.product.in_(list(items_by_cpe)))):
            for asset_id, item in items_by_cpe[aff.product]:
                hit = cpe_hit(aff.product, item["version"], aff.ranges or [])
                if hit:
                    found[(asset_id, aff.record_id, aff.product)] = (hit, item["label"], item["version"])
    # 软件包
    pkgs: dict[tuple[str, str], list[AssetPackage]] = defaultdict(list)
    for p in db.scalars(select(AssetPackage).where(AssetPackage.asset_id.in_(ids), AssetPackage.ecosystem != "")):
        pkgs[(p.ecosystem, p.source)].append(p)
    by_eco: dict[str, list[str]] = defaultdict(list)
    for eco, src in pkgs:
        by_eco[eco].append(src)
    for eco, names in by_eco.items():
        for i in range(0, len(names), 500):
            q = select(VulnAffect).where(VulnAffect.kind == "pkg", VulnAffect.ecosystem == eco, VulnAffect.product.in_(names[i:i + 500]))
            for aff in db.scalars(q):
                seen_asset: set[uuid.UUID] = set()
                for p in pkgs[(eco, aff.product)]:
                    if p.asset_id in seen_asset:
                        continue
                    hit = pkg_hit(p.source_version, aff.ranges or [])
                    if hit:
                        seen_asset.add(p.asset_id)
                        if aff.note and aff.note not in ("not yet assigned",):
                            hit.note = "；".join(x for x in (hit.note, f"发行版紧急程度：{aff.note}") if x)
                        found[(p.asset_id, aff.record_id, f"{eco}/{aff.product}")] = (hit, f"{aff.product}（{eco}）", p.source_version)

    existing = {(m.asset_id, m.record_id, m.product): m for m in db.scalars(select(VulnMatch).where(VulnMatch.asset_id.in_(ids)))}
    new = reopened = resolved = 0
    for key, (hit, label, version) in found.items():
        m = existing.get(key)
        if m is None:
            m = VulnMatch(asset_id=key[0], record_id=key[1], product=key[2][:260], first_seen_at=now, status="open", reason="", status_by="", active=True)
            db.add(m)
            new += 1
        elif not m.active:
            m.active, m.resolved_at = True, None
            reopened += 1
        m.label, m.installed_version, m.fixed_version = label[:200], version[:200], hit.fixed[:200]
        m.confidence, m.note, m.last_seen_at = hit.confidence, hit.note[:500], now
    for key, m in existing.items():
        if key not in found and m.active:
            m.active, m.resolved_at = False, now
            resolved += 1
    # 到期复查：接受风险 / 已缓解的处置到了复查日期，回到待处理
    today = now.date()
    expired = db.execute(update(VulnMatch).where(VulnMatch.asset_id.in_(ids), VulnMatch.status.in_(("accepted", "mitigated")),
                                                 VulnMatch.review_at.is_not(None), VulnMatch.review_at <= today)
                         .values(status="open", reason=VulnMatch.reason + "\n（复查日期已到，回到待处理）", review_at=None)).rowcount or 0
    db.flush()
    write_metrics(db, ids, now)
    return {"assets": len(ids), "matches": len(found), "new": new, "reopened": reopened, "resolved": resolved, "review_expired": expired}


ALERT_STATUSES = ("open", "affected")


def is_alertable(rec: VulnRecord) -> bool:
    """告警只针对已被利用或 CVSS ≥ 9.0（默认值）。"""
    return bool(rec.kev) or (rec.cvss is not None and rec.cvss >= 9.0)


def write_metrics(db: Session, asset_ids: list[uuid.UUID], now: datetime) -> None:
    """每台资产写入漏洞计数指标，供告警规则、仪表盘和 /metrics 使用（来源记为 vuln）。"""
    counts: dict[uuid.UUID, dict[str, float]] = {a: {"vuln.open": 0, "vuln.alert_count": 0, "vuln.kev": 0} for a in asset_ids}
    rows = db.execute(select(VulnMatch.asset_id, VulnRecord).join(VulnRecord, VulnRecord.id == VulnMatch.record_id)
                      .where(VulnMatch.asset_id.in_(asset_ids), VulnMatch.active.is_(True), VulnMatch.status.in_(ALERT_STATUSES)))
    seen: set[tuple[uuid.UUID, str]] = set()
    for asset_id, rec in rows:
        if (asset_id, rec.id) in seen:
            continue
        seen.add((asset_id, rec.id))
        c = counts[asset_id]
        c["vuln.open"] += 1
        c["vuln.kev"] += 1 if rec.kev else 0
        c["vuln.alert_count"] += 1 if is_alertable(rec) else 0
    for asset_id, c in counts.items():
        metrics.write_sample(db, asset_id, "vuln", [Point(k, v, "") for k, v in c.items()], observed_at=now, interval_seconds=7200)


def summary_for_assets(db: Session, asset_ids: set[uuid.UUID] | None) -> dict[str, int]:
    """每个漏洞影响的本地资产数（只算 active 且未处置为不受影响 / 误报的）。"""
    stmt = select(VulnMatch.record_id, VulnMatch.asset_id).where(VulnMatch.active.is_(True), VulnMatch.status.not_in(("not_affected", "false_positive")))
    out: dict[str, set[uuid.UUID]] = defaultdict(set)
    for rid, aid in db.execute(stmt):
        if asset_ids is None or aid in asset_ids:
            out[rid].add(aid)
    return {k: len(v) for k, v in out.items()}


def review_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
