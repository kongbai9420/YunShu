"""国内漏洞源、GitHub 安全公告、厂商 RSS、外部推送（watchvuln / 通用 Webhook）。

国内来源的接口地址与字段参考 watchvuln（https://github.com/zema1/watchvuln，MIT License，Copyright (c) 2021 koalr）
的 grab/ 目录，用 Python 重写，只保留使用公开 JSON 接口的来源：长亭、OSCS、奇安信威胁情报、微步。
阿里云漏洞库、Seebug 设有防爬验证，平台不做任何绕过；启明星辰、Struts2 需要解析网页结构，容易失效。
这四个来源可以由用户自行部署 watchvuln，再把推送地址指向本平台接入（见 parse_webhook）。

这些网站没有正式开放接口，页面或接口变化时会失效；失败只记在该来源的状态里，不影响其他来源。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.vulndb.normalize import CVE_RE, NormRecord, fix_entry, key_for, parse_time, severity_from_text
from app.vulndb.sources import UA, SourceError

BROWSER_UA = {"User-Agent": "Mozilla/5.0 (compatible; YunShu-Ops vulnerability watch)", "Accept": "application/json, text/plain, */*"}


def _json(resp: httpx.Response) -> Any:
    if resp.status_code >= 400:
        raise SourceError(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError("返回的不是 JSON（接口可能已变化）") from exc


def _req(http: httpx.Client, method: str, url: str, **kw: Any) -> httpx.Response:
    try:
        return http.request(method, url, **kw)
    except httpx.HTTPError as exc:
        raise SourceError(f"连接失败：{type(exc).__name__}") from exc


def _record(source: str, native: str, title: str, *, cve: str = "", summary: str = "", severity: str = "unknown", url: str = "",
            published: Any = None, refs: list[str] | None = None, fixes: list[tuple[str, Any]] | None = None) -> NormRecord:
    aliases = [cve] if cve and CVE_RE.fullmatch(cve.strip().upper()) else []
    return NormRecord(key=key_for(native, source, aliases=aliases), source=source, native_id=native, title=title.strip(), summary=(summary or "").strip(),
                      severity=severity, url=url, published=parse_time(published), aliases=[native] + aliases,
                      references=[{"url": r, "source": source} for r in (refs or []) if r.startswith("http")][:10],
                      remediation=[e for e in (fix_entry(source, kind, text) for kind, text in fixes or []) if e] if fixes is not None else None)


# ---- 长亭漏洞库 ----


def fetch_chaitin(http: httpx.Client, pages: int = 2) -> list[NormRecord]:
    out = []
    headers = {**BROWSER_UA, "Referer": "https://stack.chaitin.com/vuldb/index", "Origin": "https://stack.chaitin.com"}
    for i in range(pages):
        data = _json(_req(http, "GET", f"https://stack.chaitin.com/api/v2/vuln/list/?limit=15&offset={i * 15}&search=CT-", headers=headers))
        for d in (data.get("data") or {}).get("list") or []:
            refs = (d.get("references") or "").split("\n")
            out.append(_record("chaitin", d.get("ct_id", ""), d.get("title", ""), cve=d.get("cve_id") or "", summary=d.get("summary", ""),
                               severity=severity_from_text(d.get("severity")), url=f"https://stack.chaitin.com/vuldb/detail/{d.get('id', '')}",
                               published=d.get("disclosure_date") or d.get("created_at"), refs=refs))
    return out


# ---- OSCS ----


def fetch_oscs(http: httpx.Client, pages: int = 1) -> list[NormRecord]:
    headers = {**BROWSER_UA, "Referer": "https://www.oscs1024.com/cm", "Origin": "https://www.oscs1024.com"}
    out = []
    for page in range(1, pages + 1):
        data = _json(_req(http, "POST", "https://www.oscs1024.com/oscs/v1/intelligence/list", json={"page": page, "per_page": 10}, headers=headers))
        for item in (data.get("data") or {}).get("data") or []:
            mps = item.get("mps")
            if not mps:
                continue
            detail = _json(_req(http, "POST", "https://www.oscs1024.com/oscs/v1/vdb/info", json={"vuln_no": mps}, headers=headers))
            rows = detail.get("data") or []
            if not rows:
                continue
            d = rows[0]
            published = datetime.fromtimestamp(d["publish_time"] / 1000, UTC) if d.get("publish_time") else item.get("public_time")
            out.append(_record("oscs", d.get("vuln_no") or mps, d.get("vuln_title") or item.get("title", ""), cve=d.get("cve_id") or "",
                               summary=d.get("description", ""), severity=severity_from_text(d.get("level") or item.get("level")),
                               url=f"https://www.oscs1024.com/hd/{d.get('vuln_no') or mps}", published=published,
                               refs=[r.get("url", "") for r in d.get("references") or []], fixes=_oscs_fixes(d)))
    return out


def _oscs_fixes(d: dict[str, Any]) -> list[tuple[str, Any]]:
    """OSCS 详情：soulution_data（接口原样拼写）为修复方案，effect[].min_fixed_version 为各组件的最低修复版本。"""
    fixes: list[tuple[str, Any]] = []
    versions = [f"{e.get('name', '')} 升级到 {e['min_fixed_version']} 或更高版本" for e in d.get("effect") or [] if e.get("min_fixed_version")]
    if versions:
        fixes.append(("solution", "\n".join(dict.fromkeys(versions))))
    for text in d.get("soulution_data") or d.get("solution_data") or []:
        fixes.append(("solution", text))
    if d.get("vuln_suggest"):
        fixes.append(("solution", d["vuln_suggest"]))
    return fixes


# ---- 奇安信威胁情报中心 ----


def fetch_ti(http: httpx.Client) -> list[NormRecord]:
    headers = {**BROWSER_UA, "Referer": "https://ti.qianxin.com/", "Origin": "https://ti.qianxin.com"}
    data = _json(_req(http, "POST", "https://ti.qianxin.com/alpha-api/v2/vuln/one-day", headers=headers))
    body = data.get("data") or {}
    seen, out = set(), []
    for d in (body.get("key_vuln_add") or []) + (body.get("poc_exp_add") or []):
        code = d.get("qvd_code")
        if not code or code in seen:
            continue
        seen.add(code)
        level = {"极危": "critical"}.get(d.get("rating_level", ""), severity_from_text(d.get("rating_level")))
        out.append(_record("ti", code, d.get("vuln_name", ""), cve=d.get("cve_code") or "", summary=d.get("description", ""), severity=level,
                           url=f"https://ti.qianxin.com/vulnerability/detail/{d.get('id', '')}", published=d.get("publish_time")))
    return out


# ---- 微步 ----


def fetch_threatbook(http: httpx.Client) -> list[NormRecord]:
    headers = {**BROWSER_UA, "Referer": "https://x.threatbook.com/v5/vulIntelligence", "Accept-Language": "zh-CN,zh;q=0.9"}
    data = _json(_req(http, "GET", "https://x.threatbook.com/v5/node/vul_module/homePage", headers=headers))
    out = []
    for v in (data.get("data") or {}).get("highrisk") or (data.get("data") or {}).get("highRisk") or []:
        vid = v.get("id", "")
        if not vid:
            continue
        out.append(_record("threatbook", vid, v.get("vuln_name_zh") or v.get("vulnNameZh", ""), severity="critical",
                           url=f"https://x.threatbook.com/v5/vul/{vid}", published=v.get("vuln_publish_time") or v.get("vulnPublishTime")))
    return out


# ---- GitHub 安全公告（已审核）----


def fetch_ghsa(http: httpx.Client, cursor: dict[str, Any], now: datetime) -> tuple[list[NormRecord], dict[str, Any]]:
    since = parse_time(cursor.get("modified_since")) or now - timedelta(days=7)
    params = {"type": "reviewed", "per_page": 100, "sort": "updated", "direction": "desc", "modified": f">={since.date().isoformat()}"}
    resp = _req(http, "GET", "https://api.github.com/advisories", params=params, headers={**UA, "Accept": "application/vnd.github+json"})
    if resp.status_code in (403, 429):
        raise SourceError("GitHub 接口限流（未登录每小时 60 次），稍后自动重试")
    out = []
    for a in _json(resp):
        score = (a.get("cvss") or {}).get("score") or ((a.get("cvss_severities") or {}).get("cvss_v3") or {}).get("score")
        pkgs = sorted({(v.get("package") or {}).get("name", "") for v in a.get("vulnerabilities") or []} - {""})
        patched = [f"{(v.get('package') or {}).get('name', '')}（{(v.get('package') or {}).get('ecosystem', '')}）升级到 {v['first_patched_version']}"
                   for v in a.get("vulnerabilities") or [] if v.get("first_patched_version")]
        rec = _record("ghsa", a.get("ghsa_id", ""), a.get("summary", ""), cve=a.get("cve_id") or "", summary=a.get("description", "")[:5000],
                      severity=severity_from_text(a.get("severity")), url=a.get("html_url", ""), published=a.get("published_at"),
                      refs=[str(r) for r in a.get("references") or []], fixes=[("solution", "\n".join(patched))] if patched else [])
        if score:
            rec.cvss = float(score)
        if pkgs:
            rec.summary = f"涉及组件：{', '.join(pkgs[:10])}\n\n{rec.summary}"
        rec.modified = parse_time(a.get("updated_at"))
        out.append(rec)
    return out, {"modified_since": now.isoformat()}


# ---- 厂商公告 RSS / Atom ----


def fetch_rss(http: httpx.Client, feeds: list[dict[str, str]]) -> list[NormRecord]:
    """用户在数据源设置里填写的 RSS / Atom 地址（厂商安全公告），条目里能找到唯一 CVE 时按 CVE 归并。"""
    out = []
    for feed in feeds:
        url, name = feed.get("url", ""), feed.get("name", "") or urlsplit(feed.get("url", "")).hostname or "厂商公告"
        resp = _req(http, "GET", url, headers=UA, follow_redirects=True)
        if resp.status_code >= 400:
            raise SourceError(f"{name}：HTTP {resp.status_code}")
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise SourceError(f"{name}：不是有效的 RSS / Atom") from exc
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//item") or root.findall(".//a:entry", ns)
        for e in entries[:50]:
            title = (e.findtext("title") or e.findtext("a:title", namespaces=ns) or "").strip()
            link = e.findtext("link") or ""
            if not link:
                le = e.find("a:link", ns)
                link = le.get("href", "") if le is not None else ""
            desc = re.sub(r"<[^>]+>", " ", e.findtext("description") or e.findtext("a:summary", namespaces=ns) or "")
            when = e.findtext("pubDate") or e.findtext("a:updated", namespaces=ns) or ""
            native = (e.findtext("guid") or e.findtext("a:id", namespaces=ns) or link or title)[:100]
            rec = _record("rss", native, f"[{name}] {title}", summary=desc.strip()[:3000], url=link.strip(), severity=severity_from_text(desc[:200]))
            rec.key = key_for(native, "rss", text=f"{title} {desc}")
            try:
                from email.utils import parsedate_to_datetime

                rec.published = parsedate_to_datetime(when) if when and "," in when else parse_time(when)
            except (TypeError, ValueError):
                rec.published = None
            out.append(rec)
    return out


# ---- 外部推送 ----

WATCHVULN_HOSTS = {"avd.aliyun.com": "avd", "www.seebug.org": "seebug", "www.venustech.com.cn": "venustech", "cwiki.apache.org": "struts2",
                   "stack.chaitin.com": "chaitin", "www.oscs1024.com": "oscs", "ti.qianxin.com": "ti", "x.threatbook.com": "threatbook",
                   "www.cisa.gov": "kev"}


def parse_webhook(body: dict[str, Any]) -> NormRecord | None:
    """watchvuln 的 Webhook（type = watchvuln-vulninfo）或通用格式（source / id / cve / title / severity / url / description / published，
    可选 references、solution（修复建议）、workaround（临时缓解））。

    watchvuln 的启动消息、文本消息返回 None（忽略）。
    """
    if str(body.get("type", "")).startswith("watchvuln-"):
        if body["type"] != "watchvuln-vulninfo":
            return None
        c = body.get("content") or {}
        host = urlsplit(str(c.get("from", ""))).hostname or ""
        source = WATCHVULN_HOSTS.get(host, "webhook")
        return _record(source, str(c.get("unique_key") or c.get("cve") or c.get("title", ""))[:100], str(c.get("title", "")),
                       cve=str(c.get("cve") or ""), summary=str(c.get("description") or ""), severity=severity_from_text(c.get("severity")),
                       url=str(c.get("from") or ""), published=c.get("disclosure"), refs=[str(r) for r in c.get("references") or []],
                       fixes=[("solution", c.get("solutions"))])
    native = str(body.get("id") or body.get("cve") or "").strip()
    title = str(body.get("title") or "").strip()
    if not native or not title:
        raise ValueError("缺少 id（或 cve）与 title")
    return _record("webhook", native[:100], title, cve=str(body.get("cve") or ""), summary=str(body.get("description") or ""),
                   severity=severity_from_text(body.get("severity")), url=str(body.get("url") or ""), published=body.get("published"),
                   refs=[str(r) for r in body.get("references") or []],
                   fixes=[("solution", body.get("solution")), ("workaround", body.get("workaround"))])
