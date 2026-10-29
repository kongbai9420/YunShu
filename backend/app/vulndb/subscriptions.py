"""订阅：按软件清单自动生成“我有的设备”，加上用户手动添加的产品 / 关键词。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import inventory
from app.models import Asset, AssetPackage, CollectionStatus, VulnSubscription
from app.vulndb.normalize import SEVERITY_RANK


def inventory_targets(db: Session) -> dict[tuple[str, str], str]:
    """在用资产上出现的 (kind, value) → 显示名。kind：product（CPE 厂商:产品）/ ecosystem（发行版）。"""
    out: dict[tuple[str, str], str] = {}
    statuses: dict = {}
    for st in db.scalars(select(CollectionStatus)):
        statuses.setdefault(st.asset_id, []).append(st)
    for asset in db.scalars(select(Asset).where(Asset.lifecycle == "active")):
        for item in inventory.software_for(asset, statuses.get(asset.id, [])):
            if item["cpe"]:
                out[("product", item["cpe"])] = item["label"]
            if item["ecosystem"]:
                out[("ecosystem", item["ecosystem"])] = item["ecosystem"]
    for (eco,) in db.execute(select(AssetPackage.ecosystem).where(AssetPackage.ecosystem != "").distinct()):
        out[("ecosystem", eco)] = eco
    return out


def sync_auto(db: Session) -> tuple[int, int]:
    """补上新出现的自动订阅；设备都下线后删除对应的自动订阅（用户手动停用过的保留，尊重其选择）。"""
    targets = inventory_targets(db)
    existing = {(s.kind, s.value): s for s in db.scalars(select(VulnSubscription).where(VulnSubscription.auto.is_(True)))}
    added = removed = 0
    for key, label in targets.items():
        if key not in existing:
            db.add(VulnSubscription(kind=key[0], value=key[1], label=label, auto=True, enabled=True, created_by="按软件清单自动"))
            added += 1
    for key, sub in existing.items():
        if key not in targets and sub.enabled:
            db.delete(sub)
            removed += 1
    db.flush()
    return added, removed


def products(db: Session) -> list[str]:
    return sorted({s.value for s in db.scalars(select(VulnSubscription).where(VulnSubscription.enabled.is_(True), VulnSubscription.kind == "product"))})


def keywords(db: Session) -> list[str]:
    return sorted({s.value for s in db.scalars(select(VulnSubscription).where(VulnSubscription.enabled.is_(True), VulnSubscription.kind == "keyword"))})


def installed_packages(db: Session) -> list[tuple[str, str, str]]:
    rows = db.execute(select(AssetPackage.ecosystem, AssetPackage.source, AssetPackage.source_version)
                      .join(Asset, Asset.id == AssetPackage.asset_id)
                      .where(AssetPackage.ecosystem != "", Asset.lifecycle == "active").distinct())
    return [(e, s, v) for e, s, v in rows]


# ---- 订阅相关性（订阅命中预警、CVE 官方记录的筛选）----

# 产品 → 标题 / 描述里常见的写法。没有列出的产品用“产品”部分（下划线换成空格）
PRODUCT_TERMS: list[tuple[str, list[str]]] = [
    ("dell:idrac", ["iDRAC", "Integrated Dell Remote Access Controller"]),
    ("dell:poweredge_", ["PowerEdge"]),
    ("vmware:esxi", ["ESXi"]),
    ("vmware:vcenter_server", ["vCenter"]),
    ("synology:diskstation_manager", ["DiskStation Manager", "Synology DSM", "群晖 DSM"]),
    ("mikrotik:routeros", ["RouterOS"]),
    ("proxmox:virtual_environment", ["Proxmox VE", "Proxmox Virtual Environment"]),
]


def product_terms(product: str) -> list[str]:
    for prefix, terms in PRODUCT_TERMS:
        if product.startswith(prefix):
            return terms
    name = product.split(":", 1)[-1].replace("_", " ").strip()
    return [name] if len(name) >= 4 else []


def mentions(text: str, term: str) -> bool:
    """整词匹配（不区分大小写），避免 “DSM” 命中 “DSMS” 这类误报；后面紧跟数字算命中（如 iDRAC9、DSM7）。"""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z])", text, re.I) is not None


@dataclass
class Relevance:
    labels: list[str]          # 命中的订阅（显示名）
    by_version_data: bool      # 是否由受影响产品数据命中（否则只是标题 / 描述提到）


def enabled_subscriptions(db: Session) -> list[VulnSubscription]:
    return list(db.scalars(select(VulnSubscription).where(VulnSubscription.enabled.is_(True), VulnSubscription.kind.in_(("product", "keyword")))))


def relevance(subs: list[VulnSubscription], *, title: str, summary: str, products: set[str], severity: str, kev: bool) -> Relevance:
    """一个漏洞命中了哪些订阅（产品订阅看受影响产品或标题 / 描述里的产品名，关键词订阅看标题 / 描述），并遵守订阅的严重程度与“只看已被利用”。"""
    text = f"{title}\n{summary[:4000]}"
    labels: list[str] = []
    by_data = False
    for s in subs:
        if s.kev_only and not kev:
            continue
        if s.min_severity != "unknown" and SEVERITY_RANK.get(severity, 9) > SEVERITY_RANK.get(s.min_severity, 9):
            continue
        hit = False
        if s.kind == "product":
            if s.value in products:
                hit = by_data = True
            else:
                hit = any(mentions(text, t) for t in product_terms(s.value))
        elif s.kind == "keyword":
            hit = s.value.lower() in text.lower()
        if hit:
            labels.append(s.label or s.value)
    return Relevance(sorted(set(labels)), by_data)
