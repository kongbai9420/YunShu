"""各数据源统一成 NormRecord 后写库：同一 CVE 多来源归并，每个来源记一条发布记录（VulnSighting）。"""
from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import VulnAffect, VulnRecord, VulnSighting

SEVERITIES = ("critical", "high", "medium", "low", "unknown")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}
SEVERITY_TEXT = {"critical": "严重", "high": "高危", "medium": "中危", "low": "低危", "unknown": "未评级"}
CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.I)
_CJK = re.compile(r"[一-鿿]")

# 来源的显示名称（通知里“哪些平台发布了这个漏洞”）
SOURCE_LABEL = {
    "cve": "CVE 官方记录", "nvd": "NVD", "kev": "CISA KEV", "osv": "OSV", "epss": "EPSS", "ghsa": "GitHub 安全公告",
    "avd": "阿里云漏洞库", "chaitin": "长亭漏洞库", "oscs": "OSCS", "ti": "奇安信威胁情报", "threatbook": "微步",
    "seebug": "Seebug", "venustech": "启明星辰", "struts2": "Struts2 公告", "rss": "厂商公告", "webhook": "外部推送",
    "bundle": "离线包",
}


# 参考链接的分类：NVD 的 tags、OSV 的 type 统一成这几种
REF_TAGS = {
    "Patch": "patch", "FIX": "patch", "Vendor Advisory": "vendor", "Third Party Advisory": "advisory", "ADVISORY": "advisory",
    "Mitigation": "mitigation", "Exploit": "exploit", "Release Notes": "release",
    # CVE JSON 5 的 references[].tags
    "vendor-advisory": "vendor", "patch": "patch", "mitigation": "mitigation", "exploit": "exploit", "third-party-advisory": "advisory",
    "release-notes": "release",
}
REMEDIATION_KINDS = ("action", "solution", "workaround")


def ref_tags(raw: Any) -> list[str]:
    return sorted({REF_TAGS[t] for t in (raw if isinstance(raw, list) else [raw]) if isinstance(t, str) and t in REF_TAGS})


def clean_text(text: Any, limit: int = 2000) -> str:
    """来源给的修复建议常带 HTML 或多余空行：去标签、反转义、压缩空白。"""
    t = re.sub(r"<br\s*/?>|</p>|</li>", "\n", str(text or ""), flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))  # 先去标签再反转义，正文里的 &lt;admin&gt; 保留为 <admin>
    lines = [re.sub(r"[ \t　]+", " ", ln).strip() for ln in t.splitlines()]
    return "\n".join(ln for ln in lines if ln)[:limit]


def fix_entry(source: str, kind: str, text: Any) -> dict[str, str] | None:
    t = clean_text(text)
    return {"source": source, "kind": kind, "text": t} if len(t) >= 4 and kind in REMEDIATION_KINDS else None


def severity_from_score(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0:
        return "low"
    return "unknown"


def severity_from_text(text: str | None) -> str:
    t = (text or "").strip().lower()
    for key, words in (("critical", ("critical", "严重", "超危")), ("high", ("high", "高危", "高")), ("medium", ("medium", "moderate", "中危", "中")),
                       ("low", ("low", "negligible", "低危", "低"))):
        if any(t == w or t.startswith(w) for w in words):
            return key
    return "unknown"


def worse(a: str, b: str) -> str:
    return a if SEVERITY_RANK.get(a, 4) <= SEVERITY_RANK.get(b, 4) else b


# ---- CVSS 3.x 基础分（OSV 只给向量；按 FIRST 规范计算）----

_W = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}, "AC": {"L": 0.77, "H": 0.44}, "UI": {"N": 0.85, "R": 0.62},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0}, "I": {"H": 0.56, "L": 0.22, "N": 0.0}, "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}


def _roundup(x: float) -> float:
    i = round(x * 100000)
    return i / 100000.0 if i % 10000 == 0 else (math.floor(i / 10000) + 1) / 10.0


def cvss3_score(vector: str) -> float | None:
    if not vector or not vector.startswith("CVSS:3"):
        return None
    try:
        m = dict(part.split(":", 1) for part in vector.split("/")[1:])
        scope_changed = m["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if scope_changed else 0.62, "H": 0.5 if scope_changed else 0.27}[m["PR"]]
        iss = 1 - (1 - _W["C"][m["C"]]) * (1 - _W["I"][m["I"]]) * (1 - _W["A"][m["A"]])
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if scope_changed else 6.42 * iss
        exploit = 8.22 * _W["AV"][m["AV"]] * _W["AC"][m["AC"]] * pr * _W["UI"][m["UI"]]
    except (KeyError, ValueError):
        return None
    if impact <= 0:
        return 0.0
    return _roundup(min(1.08 * (impact + exploit), 10)) if scope_changed else _roundup(min(impact + exploit, 10))


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    s = str(value).strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s += "T00:00:00+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class Affect:
    kind: str
    product: str
    ranges: list[dict[str, Any]]
    ecosystem: str = ""
    note: str = ""


@dataclass
class NormRecord:
    """一个来源对一个漏洞的描述。key 为 CVE（有的话）或“来源:自家编号”。"""

    key: str
    source: str
    native_id: str = ""
    title: str = ""
    summary: str = ""
    severity: str = "unknown"
    cvss: float | None = None
    cvss_vector: str = ""
    published: datetime | None = None
    modified: datetime | None = None
    url: str = ""
    aliases: list[str] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    cwe: list[str] = field(default_factory=list)
    affects: list[Affect] | None = None  # None：这个来源不提供受影响范围，保留已有的；列表：替换该来源的范围
    kev: dict[str, Any] | None = None
    kev_due: str = ""
    # None：这个来源不提供修复建议，保留已有的；列表：替换该来源的条目（fix_entry 生成）
    remediation: list[dict[str, str]] | None = None


def key_for(native_id: str, source: str, text: str = "", aliases: list[str] | None = None) -> str:
    """有 CVE 编号就用 CVE，否则用“来源:编号”。"""
    for candidate in [native_id, *(aliases or [])]:
        m = CVE_RE.search(candidate or "")
        if m:
            return m.group(0).upper()
    if text:
        found = {c.upper() for c in CVE_RE.findall(text)}
        if len(found) == 1:  # 正文只提到一个 CVE 才认定是它；提到多个时不猜
            return found.pop()
    return f"{source.upper()}:{native_id}"[:80]


def upsert(db: Session, records: list[NormRecord], now: datetime | None = None) -> list[str]:
    """写入并返回本次新出现的漏洞键（之前从未见过）。"""
    now = now or datetime.now(UTC)
    new_keys: list[str] = []
    for r in records:
        rec = db.get(VulnRecord, r.key)
        if rec is None:
            rec = VulnRecord(id=r.key, first_seen_at=now, aliases=[], references=[], cwe=[], severity="unknown")
            db.add(rec)
            new_keys.append(r.key)
        _merge(rec, r, now)
        sighting = db.get(VulnSighting, (r.key, r.source))
        if sighting is None:
            sighting = VulnSighting(record_id=r.key, source=r.source, seen_at=now, title="", url="", severity="unknown")
            db.add(sighting)
        sighting.native_id = (r.native_id or r.key)[:120]
        sighting.title = (r.title or sighting.title)[:500]
        sighting.url = (r.url or sighting.url)[:1000]
        sighting.severity = r.severity if r.severity != "unknown" else sighting.severity or "unknown"
        sighting.published = r.published or sighting.published
        if r.affects is not None:
            db.flush()
            db.execute(delete(VulnAffect).where(VulnAffect.record_id == r.key, VulnAffect.source == r.source))
            for a in r.affects:
                db.add(VulnAffect(record_id=r.key, kind=a.kind, ecosystem=a.ecosystem, product=a.product[:200], ranges=a.ranges,
                                  source=r.source, note=a.note[:200]))
    db.flush()
    return new_keys


def _merge(rec: VulnRecord, r: NormRecord, now: datetime) -> None:
    # 标题：优先中文（用户是中文环境），其次已有，再其次本来源
    if r.title and (not rec.title or (_CJK.search(r.title) and not _CJK.search(rec.title))):
        rec.title = r.title[:500]
    if r.summary and (not rec.summary or (_CJK.search(r.summary) and not _CJK.search(rec.summary)) or r.source == "nvd" and not _CJK.search(rec.summary)):
        rec.summary = r.summary[:20000]
    # 评分：NVD 优先；没有 NVD 时取各来源中最高的
    if r.cvss is not None and (rec.cvss is None or r.source == "nvd" or (rec.cvss_source != "nvd" and r.cvss > rec.cvss)):
        rec.cvss, rec.cvss_vector, rec.cvss_source = r.cvss, r.cvss_vector[:200], r.source
    sev = severity_from_score(rec.cvss) if rec.cvss is not None else "unknown"
    rec.severity = sev if rec.cvss_source == "nvd" and sev != "unknown" else worse(worse(rec.severity or "unknown", sev), r.severity)
    # SQLite 读回的时间不带时区，统一成 UTC 再比较
    rec.published = min([d for d in (parse_time(rec.published), parse_time(r.published)) if d], default=None)
    rec.modified = max([d for d in (parse_time(rec.modified), parse_time(r.modified)) if d], default=None)
    aliases = set(rec.aliases or []) | {a for a in r.aliases if a and a != rec.id} | ({r.native_id} if r.native_id and r.native_id != rec.id else set())
    rec.aliases = sorted(aliases)[:50]
    refs = [dict(ref) for ref in rec.references or []]
    by_url = {ref.get("url"): ref for ref in refs}
    for ref in ([{"url": r.url, "source": r.source}] if r.url else []) + r.references:
        url = ref.get("url")
        if not url:
            continue
        tags = list(ref.get("tags") or [])
        if url in by_url:
            if tags:
                by_url[url]["tags"] = sorted(set(by_url[url].get("tags") or []) | set(tags))
        elif len(refs) < 40 or tags:  # 带分类的（补丁、公告）即使超出上限也保留
            item = {"url": url[:1000], "source": ref.get("source", r.source)} | ({"tags": tags} if tags else {})
            by_url[url] = item
            refs.append(item)
    rec.references = refs[:60]
    if r.remediation is not None:
        keep = [e for e in rec.remediation or [] if e.get("source") != r.source]
        rec.remediation = keep + [e for e in r.remediation if e][:6]
    if r.kev_due:
        rec.kev_due = r.kev_due[:10]
    rec.cwe = sorted(set(rec.cwe or []) | set(r.cwe))[:20]
    if r.kev is not None:
        rec.kev = True
        rec.kev_added = str(r.kev.get("dateAdded", ""))[:10]
        rec.kev_ransomware = str(r.kev.get("knownRansomwareCampaignUse", "")).lower() == "known"
        rec.kev_due = str(r.kev.get("dueDate", "") or rec.kev_due or "")[:10]
    rec.updated_at = now


def sources_of(db: Session, key: str) -> list[str]:
    return [s for (s,) in db.execute(select(VulnSighting.source).where(VulnSighting.record_id == key).order_by(VulnSighting.seen_at))]
