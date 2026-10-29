"""修复与缓解：把版本比对得出的修复版本、各来源的修复建议原文、补丁与公告链接整理成一份。

只整理来源原文与本地比对结果，不生成内容；同一段建议多个来源都给出时合并，标注全部来源。
详情页、即时通知、AI 分析共用这一份。
"""
from __future__ import annotations

import re
from typing import Any

from app.models import VulnRecord
from app.vulndb.normalize import SOURCE_LABEL

KIND_TEXT = {"solution": "修复建议", "workaround": "临时缓解", "action": "处置要求"}
KIND_ORDER = {"solution": 0, "workaround": 1, "action": 2}
TAG_TEXT = {"patch": "补丁", "vendor": "厂商公告", "mitigation": "缓解措施", "advisory": "安全公告"}
TAG_ORDER = ("patch", "vendor", "mitigation", "advisory")
QUIET = ("not_affected", "false_positive")


def _norm(text: str) -> str:
    return re.sub(r"[\s，。,.;；:：]+", "", text).lower()


def build(rec: VulnRecord, matches: list[dict[str, Any]]) -> dict[str, Any]:
    """matches 为已按用户可见范围过滤的命中（routes_vuln._match_out 的输出）。"""
    fixed: dict[tuple[str, str], dict[str, Any]] = {}
    for m in matches:
        if not m.get("active") or m.get("status") in QUIET:
            continue
        key = (m["label"], m.get("fixed_version") or "")
        item = fixed.setdefault(key, {"product": key[0], "fixed_version": key[1], "assets": [], "note": ""})
        if m["asset_name"] not in item["assets"]:
            item["assets"].append(m["asset_name"])
        if not key[1] and m.get("note"):
            item["note"] = m["note"]
    texts: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for e in rec.remediation or []:
        label = SOURCE_LABEL.get(e.get("source", ""), e.get("source", ""))
        k = _norm(e.get("text", ""))
        if not k:
            continue
        if k in seen:
            if label not in seen[k]["sources"]:
                seen[k]["sources"].append(label)
            continue
        item = {"kind": e.get("kind", "solution"), "kind_text": KIND_TEXT.get(e.get("kind", ""), "修复建议"), "text": e["text"], "sources": [label]}
        seen[k] = item
        texts.append(item)
    texts.sort(key=lambda t: KIND_ORDER.get(t["kind"], 9))
    links = []
    for ref in rec.references or []:
        tags = [t for t in TAG_ORDER if t in (ref.get("tags") or [])]
        if tags:
            links.append({"url": ref["url"], "source": SOURCE_LABEL.get(ref.get("source", ""), ref.get("source", "")), "tags": tags,
                          "tags_text": [TAG_TEXT[t] for t in tags]})
    links.sort(key=lambda r: TAG_ORDER.index(r["tags"][0]))
    return {"fixed": sorted(fixed.values(), key=lambda f: (not f["fixed_version"], f["product"])), "texts": texts, "links": links[:12],
            "kev_due": rec.kev_due or ""}


def headline(rec: VulnRecord) -> str:
    """即时通知里的一行：来源给出的第一条修复建议 / 临时缓解 / 处置要求（截断，注明来源）。修复版本由通知另列。"""
    for kind in ("workaround", "solution", "action"):
        for e in rec.remediation or []:
            if e.get("kind") == kind and e.get("text"):
                text = e["text"].replace("\n", " ")
                label = SOURCE_LABEL.get(e.get("source", ""), e.get("source", ""))
                return f"{KIND_TEXT[kind]}（{label}）：{text[:150]}{'…' if len(text) > 150 else ''}"
    return ""
