"""CVE 官方记录（CVE Program 的 cvelistV5 仓库，CVE JSON 5 格式）。

仓库约每 7 分钟更新一次，cves/delta.json 列出最近一轮新增与更新的 CVE，cves/deltaLog.json 保留约 30 天的历史（约 28 MB）。
平时每 5 分钟读一次 delta.json；距上次读取超过 8 分钟（中间可能漏了一轮）时改读 deltaLog.json 补齐。
记录里有厂商（CNA）填写的受影响产品与版本、修复建议、临时缓解，以及 CISA 补充的 KEV 标记，通常比 NVD 补录 CPE 早得多。

只保留与订阅相关的记录：受影响产品对得上订阅的产品，或标题 / 描述提到订阅的产品名或关键词，或已被利用。
格式核对于 2026-10-20 的真实数据（CVE-2024-3400、CVE-2024-25943）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.vulndb import subscriptions
from app.vulndb.normalize import Affect, NormRecord, fix_entry, parse_time, ref_tags, severity_from_score, severity_from_text
from app.vulndb.sources import SourceError, _get_json, _vendor_product

BASE = "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves"
DELTA_URL = f"{BASE}/delta.json"
DELTA_LOG_URL = f"{BASE}/deltaLog.json"
GAP = timedelta(minutes=8)
MAX_RECORDS = 1000  # 一次最多取这么多条记录，补齐时剩下的下一轮继续
UNBOUNDED = {"", "0", "*", "-", "n/a", "na", "all", "unspecified", "unknown"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def map_product(vendor: str, product: str) -> set[str]:
    """厂商填写的“厂商 / 产品”名称 → 平台的产品标识（与软件清单、订阅一致的“厂商:产品”）。"""
    v, p = vendor.lower(), product.lower()
    out = {f"{_slug(vendor)}:{_slug(product)}"} if vendor and product else set()
    if "dell" in v:
        m = re.search(r"(?:idrac|remote access controller)\s*(\d+)", p)
        if m:
            out.add(f"dell:idrac{m.group(1)}_firmware")
    if v in ("vmware", "broadcom") or "vmware" in v:
        if "esxi" in p:
            out.add("vmware:esxi")
        if "vcenter" in p:
            out.add("vmware:vcenter_server")
    if "synology" in v and ("diskstation manager" in p or re.match(r"dsm\b", p)):
        out.add("synology:diskstation_manager")
    if "mikrotik" in v and "routeros" in p:
        out.add("mikrotik:routeros")
    if "proxmox" in v and ("virtual environment" in p or "proxmox ve" in p or p == "pve-manager"):
        out.add("proxmox:virtual_environment")
    return out


def _branch(version: str) -> str:
    parts = re.split(r"[.\-]", version)
    return ".".join(parts[:2]) if len(parts) >= 2 else ""


def ranges_for(versions: list[dict[str, Any]], *, vague: bool = False) -> list[dict[str, Any]]:
    """CVE 5 的 versions → 平台的区间格式（与 NVD 相同）。

    厂商常把多个版本分支写成多条“起始未知、小于修复版本”（如 iDRAC 7.00 分支修复于 7.00.00.172、7.10 分支修复于 7.10.50.00），
    直接取并集会把 7.00 分支的已修复版本误判为受影响：除最小的一条外，其余按修复版本的前两段作为分支起点。
    """
    out: list[dict[str, Any]] = []
    open_start: list[tuple[str, str]] = []
    for ver in versions:
        if ver.get("status") != "affected" or ver.get("versionType") == "git":
            continue
        start = str(ver.get("version") or "")
        lt, le = str(ver.get("lessThan") or ""), str(ver.get("lessThanOrEqual") or "")
        lt, le = ("" if lt == "*" else lt), ("" if le == "*" else le)
        base = {"exact": "", "update": "", "start_incl": "", "start_excl": "", "end_incl": "", "end_excl": "", "and_other": vague}
        if lt or le:
            if start.lower() in UNBOUNDED:
                open_start.append((lt, le))
                continue
            out.append(base | {"start_incl": start, "end_excl": lt, "end_incl": le})
        elif start.lower() not in UNBOUNDED:
            out.append(base | {"exact": start})
    if open_start:
        from app.vulnmatch import generic_compare

        open_start.sort(key=lambda r: _cmp_key(r[0] or r[1], generic_compare))
        for i, (lt, le) in enumerate(open_start):
            base = {"exact": "", "update": "", "start_excl": "", "end_incl": le, "end_excl": lt, "and_other": vague}
            out.append(base | {"start_incl": "" if i == 0 else _branch(lt or le)})
    return out


def _cmp_key(version: str, compare):  # type: ignore[no-untyped-def]
    import functools

    return functools.cmp_to_key(compare)(version)


def _metric(containers: list[dict[str, Any]]) -> tuple[float | None, str, str]:
    for key in ("cvssV3_1", "cvssV3_0", "cvssV4_0"):
        for c in containers:
            for m in c.get("metrics") or []:
                d = m.get(key)
                if d and d.get("baseScore") is not None:
                    return float(d["baseScore"]), str(d.get("vectorString", "")), str(d.get("baseSeverity", ""))
    return None, "", ""


def _kev(adps: list[dict[str, Any]]) -> dict[str, Any] | None:
    for a in adps:
        for m in a.get("metrics") or []:
            other = m.get("other") or {}
            if other.get("type") == "kev":
                return {"dateAdded": str((other.get("content") or {}).get("dateAdded", ""))[:10]}
    return None


def _texts(items: list[dict[str, Any]] | None) -> list[str]:
    items = items or []
    en = [i for i in items if str(i.get("lang", "")).lower().startswith("en")] or items
    return [str(i.get("value", "")) for i in en if i.get("value")]


def parse(data: dict[str, Any], *, products: set[str], keywords: list[str]) -> NormRecord | None:
    """解析一条 CVE 5 记录；与订阅无关或已撤销的返回 None。"""
    meta = data.get("cveMetadata") or {}
    cid = str(meta.get("cveId", "")).upper()
    if not cid.startswith("CVE-") or meta.get("state") != "PUBLISHED":
        return None
    containers = data.get("containers") or {}
    cna = containers.get("cna") or {}
    adps = containers.get("adp") or []
    summary = (_texts(cna.get("descriptions")) or [""])[0]
    title = str(cna.get("title") or "") or (summary.split(". ")[0][:200] if summary else cid)
    score, vector, sev_text = _metric([cna, *adps])
    kev = _kev(adps)
    affects: dict[str, Affect] = {}
    for a in [*(cna.get("affected") or []), *(x for adp in adps for x in adp.get("affected") or [])]:
        keys = map_product(str(a.get("vendor", "")), str(a.get("product", "")))
        keys |= {_vendor_product(c)[0] for c in a.get("cpes") or [] if isinstance(c, str)} - {""}
        tracked = keys & products
        if not tracked:
            continue
        # VMware 按补丁构建号区分修复，厂商写的版本号不能直接比较：标为可能受影响
        for product in tracked:
            rngs = ranges_for(a.get("versions") or [], vague=product.startswith("vmware:"))
            if rngs:
                affects.setdefault(product, Affect(kind="cpe", product=product, ranges=[])).ranges.extend(rngs)
    text = f"{title}\n{summary}"
    mentioned = any(subscriptions.mentions(text, t) for p in products for t in subscriptions.product_terms(p))
    if not affects and not kev and not mentioned and not any(k.lower() in text.lower() for k in keywords):
        return None
    refs = [{"url": r["url"], "source": "cve"} | ({"tags": ref_tags(r.get("tags") or [])} if ref_tags(r.get("tags") or []) else {})
            for r in (cna.get("references") or [])[:30] if r.get("url")]
    fixes = [fix_entry("cve", "solution", t) for t in _texts(cna.get("solutions"))] + [fix_entry("cve", "workaround", t) for t in _texts(cna.get("workarounds"))]
    return NormRecord(
        key=cid, source="cve", native_id=cid, title=title, summary=summary, cvss=score, cvss_vector=vector,
        severity=severity_from_score(score) if score is not None else severity_from_text(sev_text),
        published=parse_time(meta.get("datePublished")), modified=parse_time(meta.get("dateUpdated")),
        url=f"https://www.cve.org/CVERecord?id={cid}", references=refs, kev=kev,
        remediation=[f for f in fixes if f][:6], affects=list(affects.values()),
    )


def _record_url(item: dict[str, Any]) -> str:
    link = str(item.get("githubLink") or "")
    if link.startswith(f"{BASE}/"):
        return link
    m = re.fullmatch(r"CVE-(\d{4})-(\d+)", str(item.get("cveId", "")).upper())
    if not m:
        return ""
    return f"{BASE}/{m.group(1)}/{int(m.group(2)) // 1000}xxx/CVE-{m.group(1)}-{m.group(2)}.json"


def fetch(http: httpx.Client, *, cursor: dict[str, Any], now: datetime, products: set[str], keywords: list[str]) -> tuple[list[NormRecord], dict[str, Any]]:
    last_fetch = str(cursor.get("fetchTime") or "")
    polled = parse_time(cursor.get("polled_at"))
    if last_fetch and (polled is None or now - polled > GAP):
        log = _get_json(http, DELTA_LOG_URL)
        deltas = sorted((d for d in log if isinstance(d, dict) and str(d.get("fetchTime", "")) > last_fetch), key=lambda d: d["fetchTime"])
    else:
        delta = _get_json(http, DELTA_URL)
        deltas = [delta] if str(delta.get("fetchTime", "")) > last_fetch else []
    items: dict[str, dict[str, Any]] = {}
    done_until = last_fetch
    for d in deltas:
        batch = [i for i in (d.get("new") or []) + (d.get("updated") or []) if isinstance(i, dict) and i.get("cveId")]
        if items and len(items) + len(batch) > MAX_RECORDS:
            break  # 剩下的下一轮继续（游标只推进到已完整处理的一轮）
        for i in batch:
            items[str(i["cveId"]).upper()] = i
        done_until = str(d.get("fetchTime", done_until))
    records, failed = [], 0
    for cid, item in items.items():
        url = _record_url(item)
        if not url:
            continue
        try:
            rec = parse(_get_json(http, url), products=products, keywords=keywords)
        except SourceError:
            failed += 1
            continue
        if rec:
            records.append(rec)
    if items and failed == len(items):
        raise SourceError(f"{failed} 条记录都没有取到")
    # 平时轮询之间如果超过 GAP 下一轮会读 deltaLog 补齐，所以只在成功时更新 polled_at
    return records, {"fetchTime": done_until, "polled_at": now.isoformat(), "failed": failed}
