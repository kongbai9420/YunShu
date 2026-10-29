"""国际官方数据源：NVD、CISA KEV、OSV、EPSS。只读下载，所有网络请求经调用方传入的 httpx.Client（测试注入伪造接口）。

数据格式核对于 2026-10-19 的真实响应（NVD API 2.0、OSV v1、KEV JSON、FIRST EPSS API v1）。
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import re
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.vulndb.normalize import Affect, NormRecord, cvss3_score, fix_entry, parse_time, ref_tags, severity_from_score, severity_from_text

NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
OSV_API = "https://api.osv.dev/v1"
EPSS_API = "https://api.first.org/data/v1/epss"
UA = {"User-Agent": "YunShu-Ops vulnerability sync (+https://github.com/kongbai9420/YunShu)"}

# 应用类产品在 CPE 里是 a，其余（固件、操作系统）是 o
CPE_PART = {"vmware:vcenter_server": "a", "proxmox:virtual_environment": "a"}


class SourceError(Exception):
    pass


def _get_json(http: httpx.Client, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
    try:
        resp = http.get(url, params=params, headers={**UA, **(headers or {})})
    except httpx.HTTPError as exc:
        raise SourceError(f"连接失败：{type(exc).__name__}") from exc
    if resp.status_code in (403, 429):
        raise SourceError(f"被限流或拒绝（HTTP {resp.status_code}），稍后自动重试；NVD 建议配置免费 API 密钥")
    if resp.status_code >= 400:
        raise SourceError(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError("返回的不是 JSON") from exc


# ---------------- NVD ----------------


def cpe_match_string(product: str) -> str:
    """“厂商:产品”或“o:厂商:产品” → NVD virtualMatchString。"""
    parts = product.split(":")
    if len(parts) == 3:
        return f"cpe:2.3:{parts[0]}:{parts[1]}:{parts[2]}"
    return f"cpe:2.3:{CPE_PART.get(product, 'o')}:{product}"


def _vendor_product(criteria: str) -> tuple[str, str, str, str]:
    """cpe:2.3:o:dell:idrac9_firmware:7.00:*:... → (dell:idrac9_firmware, 版本, update, part)"""
    p = criteria.split(":")
    if len(p) < 6:
        return "", "", "", ""
    return f"{p[3]}:{p[4]}", p[5], p[6] if len(p) > 6 else "*", p[2]


def parse_nvd_cve(cve: dict[str, Any], tracked: set[str] | None) -> NormRecord:
    cid = cve["id"]
    summary = next((d["value"] for d in cve.get("descriptions") or [] if d.get("lang") == "en"), "")
    metrics = cve.get("metrics") or {}
    score, vector = None, ""
    for key in ("cvssMetricV31", "cvssMetricV40", "cvssMetricV30", "cvssMetricV2"):
        items = metrics.get(key) or []
        if items:
            chosen = next((m for m in items if m.get("type") == "Primary"), items[0])
            data = chosen.get("cvssData") or {}
            score, vector = data.get("baseScore"), data.get("vectorString", "")
            break
    affects: dict[str, Affect] = {}
    for cfg in cve.get("configurations") or []:
        and_other = cfg.get("operator") == "AND" or len(cfg.get("nodes") or []) > 1
        for node in cfg.get("nodes") or []:
            for m in node.get("cpeMatch") or []:
                if not m.get("vulnerable"):
                    continue
                product, version, update, _part = _vendor_product(m.get("criteria", ""))
                if not product or (tracked is not None and product not in tracked):
                    continue
                rng = {"exact": "" if version in ("*", "-") else version, "update": "" if update in ("*", "-") else update,
                       "start_incl": m.get("versionStartIncluding", ""), "start_excl": m.get("versionStartExcluding", ""),
                       "end_incl": m.get("versionEndIncluding", ""), "end_excl": m.get("versionEndExcluding", ""), "and_other": and_other}
                affects.setdefault(product, Affect(kind="cpe", product=product, ranges=[])).ranges.append(rng)
    title = summary.split(". ")[0][:200] if summary else cid
    return NormRecord(
        key=cid, source="nvd", native_id=cid, title=title, summary=summary, cvss=float(score) if score is not None else None,
        cvss_vector=vector, severity=severity_from_score(score), published=parse_time(cve.get("published")), modified=parse_time(cve.get("lastModified")),
        url=f"https://nvd.nist.gov/vuln/detail/{cid}",
        references=_nvd_refs(cve.get("references") or []),
        # NVD 收录了 KEV 的“要求措施”与期限（cisaRequiredAction / cisaActionDue），离线包里没有 KEV 时也能显示
        remediation=[e for e in [fix_entry("nvd", "action", cve.get("cisaRequiredAction"))] if e],
        kev_due=str(cve.get("cisaActionDue") or "")[:10],
        cwe=sorted({d["value"] for w in cve.get("weaknesses") or [] for d in w.get("description") or [] if str(d.get("value", "")).startswith("CWE-")}),
        affects=list(affects.values()) if tracked is not None else None,
    )


def _nvd_refs(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一链接在 NVD 里常按提交方重复出现，合并分类；补丁、公告排在前面。"""
    refs: dict[str, set[str]] = {}
    for r in raw:
        if r.get("url"):
            refs.setdefault(r["url"], set()).update(ref_tags(r.get("tags") or []))
    ordered = sorted(refs.items(), key=lambda kv: not ({"patch", "vendor", "mitigation"} & kv[1]))
    return [{"url": u, "source": "nvd"} | ({"tags": sorted(t)} if t else {}) for u, t in ordered[:30]]


def nvd_pages(http: httpx.Client, params: dict[str, Any], *, api_key: str = "", sleep: Callable[[float], None] = time.sleep,
              max_pages: int = 20) -> Iterator[dict[str, Any]]:
    """按页取 NVD；无密钥时每次请求间隔 6 秒（官方建议），有密钥 0.6 秒。"""
    headers = {"apiKey": api_key} if api_key else {}
    start = 0
    for page in range(max_pages):
        if page:
            sleep(0.6 if api_key else 6)
        data = _get_json(http, NVD_API, params={**params, "resultsPerPage": 2000, "startIndex": start}, headers=headers)
        for v in data.get("vulnerabilities") or []:
            if v.get("cve", {}).get("id"):
                yield v["cve"]
        start += int(data.get("resultsPerPage") or 0)
        if not data.get("resultsPerPage") or start >= int(data.get("totalResults") or 0):
            return


def _nvd_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fetch_nvd(http: httpx.Client, *, products: Iterable[str], keywords: Iterable[str], cursor: dict[str, Any], now: datetime,
              api_key: str = "", sleep: Callable[[float], None] = time.sleep) -> tuple[list[NormRecord], dict[str, Any]]:
    """按订阅的产品（CPE）与关键词拉取。首次全量（产品）/ 近 30 天（关键词），之后按 lastModified 增量（NVD 限制区间最长 120 天）。"""
    products, keywords = sorted(set(products)), sorted(set(keywords))
    tracked = set(products)
    out: dict[str, NormRecord] = {}
    new_cursor = dict(cursor)
    first = True
    for kind, values in (("cpe", products), ("kw", keywords)):
        for value in values:
            ck = f"{kind}:{value}"
            since = parse_time(cursor.get(ck))
            params: dict[str, Any] = {"virtualMatchString": cpe_match_string(value)} if kind == "cpe" else {"keywordSearch": value}
            if since and now - since < timedelta(days=119):
                params |= {"lastModStartDate": _nvd_time(since - timedelta(minutes=5)), "lastModEndDate": _nvd_time(now)}
            elif kind == "kw":
                params |= {"pubStartDate": _nvd_time(now - timedelta(days=30)), "pubEndDate": _nvd_time(now)}
            if not first:
                sleep(0.6 if api_key else 6)
            first = False
            for cve in nvd_pages(http, params, api_key=api_key, sleep=sleep):
                rec = parse_nvd_cve(cve, tracked if kind == "cpe" else tracked)
                prev = out.get(rec.key)
                if prev and prev.affects and rec.affects is not None:
                    names = {a.product for a in rec.affects}
                    rec.affects += [a for a in prev.affects if a.product not in names]
                out[rec.key] = rec
            new_cursor[ck] = now.isoformat()
    return list(out.values()), new_cursor


# ---------------- CISA KEV ----------------


def parse_kev(data: dict[str, Any]) -> list[NormRecord]:
    out = []
    for v in data.get("vulnerabilities") or []:
        cid = str(v.get("cveID", "")).upper()
        if not cid.startswith("CVE-"):
            continue
        out.append(NormRecord(key=cid, source="kev", native_id=cid, title=v.get("vulnerabilityName", ""), summary=v.get("shortDescription", ""),
                              url="https://www.cisa.gov/known-exploited-vulnerabilities-catalog", kev=v, kev_due=str(v.get("dueDate") or "")[:10],
                              references=_kev_refs(v.get("notes") or ""),
                              remediation=[e for e in [fix_entry("kev", "action", v.get("requiredAction"))] if e],
                              cwe=[c for c in v.get("cwes") or [] if str(c).startswith("CWE-")]))
    return out


def _kev_refs(notes: str) -> list[dict[str, Any]]:
    """KEV 的 notes 里第一类链接是厂商公告；CISA 自己的指令说明与 NVD 页面不算。"""
    out = []
    for url in re.findall(r"https?://[^\s;]+", notes):
        host = url.split("/")[2].lower()
        vendor = not (host.endswith("cisa.gov") or host == "nvd.nist.gov")
        out.append({"url": url, "source": "kev"} | ({"tags": ["vendor"]} if vendor else {}))
    return out[:10]


def fetch_kev(http: httpx.Client, *, cursor: dict[str, Any]) -> tuple[list[NormRecord], dict[str, Any]]:
    # 条件请求：清单没变时服务器回 304，不重新下载约 1 MB 的 JSON
    etag = str(cursor.get("etag") or "")
    try:
        resp = http.get(KEV_URL, headers={**UA, **({"If-None-Match": etag} if etag else {})})
    except httpx.HTTPError as exc:
        raise SourceError(f"连接失败：{type(exc).__name__}") from exc
    if resp.status_code == 304:
        return [], cursor
    if resp.status_code >= 400:
        raise SourceError(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise SourceError("返回的不是 JSON") from exc
    version = str(data.get("catalogVersion", ""))
    new_cursor = {"catalogVersion": version, "count": data.get("count")} | ({"etag": resp.headers["etag"]} if resp.headers.get("etag") else {})
    if version and version == cursor.get("catalogVersion"):
        return [], new_cursor
    return parse_kev(data), new_cursor


# ---------------- OSV ----------------


def osv_wanted(osv_id: str) -> bool:
    """只用按 CVE 拆分的发行版记录（DEBIAN-CVE-*、UBUNTU-CVE-*）；DSA / DLA / USN 是同一批 CVE 的公告，跳过避免重复。"""
    return osv_id.startswith(("DEBIAN-CVE-", "UBUNTU-CVE-"))


def parse_osv(rec: dict[str, Any], ecosystems: set[str] | None = None) -> NormRecord | None:
    rid = rec.get("id", "")
    if not osv_wanted(rid):
        return None
    cves = [u for u in (rec.get("upstream") or []) + (rec.get("aliases") or []) if str(u).startswith("CVE-")]
    key = cves[0] if cves else rid.split("-", 1)[1] if rid.count("-") >= 3 else rid
    score, vector, sev_text = None, "", ""
    for s in rec.get("severity") or []:
        if s.get("type") == "CVSS_V3":
            vector, score = s.get("score", ""), cvss3_score(s.get("score", ""))
        elif s.get("type") == "Ubuntu":
            sev_text = s.get("score", "")
    affects = []
    for a in rec.get("affected") or []:
        pkg = a.get("package") or {}
        eco = pkg.get("ecosystem", "")
        if ecosystems is not None and eco not in ecosystems:
            continue
        ranges = []
        for r in a.get("ranges") or []:
            if r.get("type") not in ("ECOSYSTEM", None):
                continue
            current: dict[str, str] = {}
            for ev in r.get("events") or []:
                if "introduced" in ev:
                    if current:
                        ranges.append(current)
                    current = {"introduced": ev["introduced"]}
                elif "fixed" in ev or "last_affected" in ev:
                    current.update({k: ev[k] for k in ("fixed", "last_affected") if k in ev})
                    ranges.append(current)
                    current = {}
            if current:
                ranges.append(current)
        note = str((a.get("ecosystem_specific") or {}).get("urgency", ""))
        if ranges and pkg.get("name"):
            affects.append(Affect(kind="pkg", ecosystem=eco, product=pkg["name"], ranges=ranges, note=note))
    distro = "debian" if rid.startswith("DEBIAN") else "ubuntu"
    url = f"https://security-tracker.debian.org/tracker/{key}" if distro == "debian" else f"https://ubuntu.com/security/{key}"
    summary = rec.get("details") or rec.get("summary") or ""
    return NormRecord(key=key, source="osv", native_id=rid, title=(rec.get("summary") or summary.split("\n")[0])[:200], summary=summary,
                      cvss=score, cvss_vector=vector, severity=severity_from_score(score) if score is not None else severity_from_text(sev_text),
                      published=parse_time(rec.get("published")), modified=parse_time(rec.get("modified")), url=url,
                      aliases=[rid], affects=affects,
                      references=[{"url": r["url"], "source": "osv"} | ({"tags": ref_tags(r.get("type"))} if ref_tags(r.get("type")) else {})
                                  for r in (rec.get("references") or [])[:20] if r.get("url")])


def fetch_osv(http: httpx.Client, *, packages: Iterable[tuple[str, str, str]], cursor: dict[str, Any]) -> tuple[list[NormRecord], dict[str, Any]]:
    """在线：用 querybatch 只查本地实际安装的（生态, 源码包, 版本），再取有变化的记录详情。"""
    queries = sorted(set(packages))
    ecosystems = {q[0] for q in queries}
    seen: dict[str, str] = {}
    for i in range(0, len(queries), 1000):
        chunk = queries[i:i + 1000]
        body = {"queries": [{"package": {"ecosystem": e, "name": n}, "version": v} for e, n, v in chunk]}
        try:
            resp = http.post(f"{OSV_API}/querybatch", json=body, headers=UA)
        except httpx.HTTPError as exc:
            raise SourceError(f"连接失败：{type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code}")
        for result in resp.json().get("results") or []:
            for v in result.get("vulns") or []:
                if osv_wanted(v.get("id", "")):
                    seen[v["id"]] = v.get("modified", "")
    known: dict[str, str] = cursor.get("modified") or {}
    out = []
    for vid, modified in sorted(seen.items()):
        if known.get(vid) == modified and modified:
            continue
        rec = parse_osv(_get_json(http, f"{OSV_API}/vulns/{vid}"), ecosystems)
        if rec:
            out.append(rec)
    return out, {"modified": {**known, **seen}}


# ---------------- EPSS ----------------


def fetch_epss(http: httpx.Client, cves: list[str]) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for i in range(0, len(cves), 100):
        data = _get_json(http, EPSS_API, params={"cve": ",".join(cves[i:i + 100]), "limit": 100})
        for row in data.get("data") or []:
            try:
                out[row["cve"].upper()] = (float(row["epss"]), float(row["percentile"]))
            except (KeyError, ValueError):
                continue
    return out


def parse_epss_csv(raw: bytes, wanted: set[str] | None = None) -> dict[str, tuple[float, float]]:
    """FIRST 每日 CSV（可为 gzip）：第一行是 #model_version 注释，接着 cve,epss,percentile。"""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    out = {}
    lines = [ln for ln in io.StringIO(raw.decode("utf-8")) if not ln.startswith("#")]
    for row in csv.DictReader(lines):
        cid = str(row.get("cve", "")).upper()
        if wanted is not None and cid not in wanted:
            continue
        try:
            out[cid] = (float(row["epss"]), float(row["percentile"]))
        except (KeyError, ValueError):
            continue
    return out


def dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")
