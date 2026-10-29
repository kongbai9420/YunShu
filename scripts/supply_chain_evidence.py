"""整理工具实际生成的 SBOM / Grype 报告与许可证清单；不代替完整发布批准。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ("backend/requirements.lock", "backend/requirements-standard.lock", "backend/requirements-desktop.lock",
          "backend/requirements-package.lock", "frontend/package-lock.json", "docker/Dockerfile.ops",
          "scripts/license-reviews.json", "scripts/vulnerability-exceptions.json")
SCOPES = ("image", "frontend-lock", "desktop-build-environment", "linux-package")
# 某个范围额外绑定的输入（在 INPUTS 之外）；linux-package 是解压后的 Linux 压缩包实际内容
SCOPE_INPUTS = {"linux-package": ("scripts/build-linux.sh", "scripts/yunshu-linux.spec", "deploy/linux/install.sh",
                                  "deploy/linux/uninstall.sh", "deploy/linux/lock_subset.py")}
UNKNOWN_LICENSES = {"", "NONE", "NOASSERTION", "UNKNOWN"}
REVIEWS = ROOT / "scripts" / "license-reviews.json"
EXCEPTIONS = ROOT / "scripts" / "vulnerability-exceptions.json"
UNFIXED_STATES = {"wont-fix", "not-fixed"}


def load_exceptions(path: Path = EXCEPTIONS) -> dict:
    if not path.exists():
        return {"expires": "", "exceptions": []}
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    if not doc.get("expires") or not doc.get("reviewed_at"):
        raise ValueError("漏洞例外登记缺少复查日期或到期日")
    for item in doc["exceptions"]:
        if not all(item.get(key) for key in ("id", "package", "version")):
            raise ValueError(f"漏洞例外必须写明编号、包和精确版本：{item}")
    return doc


def accepted(finding: dict, exceptions: dict, today: str) -> bool:
    """只接受登记过、未到期、且扫描时仍无修复版本的命中；一旦出现修复版本或包版本变化就恢复阻断。"""
    fix = finding["fix"] or {}
    if exceptions.get("expires", "") < today or fix.get("state") not in UNFIXED_STATES or fix.get("versions"):
        return False
    return any(e["id"] == finding["id"] and e["package"] == finding["package"] and e["version"] == finding["version"]
               for e in exceptions["exceptions"])


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


# 本项目自身的组件名：本地构建的镜像标签，以及发布到镜像仓库的名称（发布时的 SBOM 根组件）
OWN_NAMES = {"yunshu-ops", "ghcr.io/kongbai9420/yunshu"}


def load_reviews(path: Path = REVIEWS) -> list[dict]:
    if not path.exists():
        return []
    reviews = json.loads(path.read_text(encoding="utf-8-sig"))["reviews"]
    for review in reviews:
        if not all(review.get(key) for key in ("name", "version", "license", "evidence", "reviewed_at")):
            raise ValueError(f"许可证人工核对记录不完整：{review}")
        if review["version"] == "*" and review["name"] not in OWN_NAMES:
            raise ValueError("只有本项目自身可以使用通配版本，其他组件必须按精确版本核对")
    return reviews


def apply_reviews(inventory: list[dict], reviews: list[dict]) -> None:
    """只补充工具没有识别的许可证；工具已识别的结果不被覆盖。"""
    for item in inventory:
        if item["license_identified"]:
            continue
        review = next((r for r in reviews if r["name"] == item["name"] and r["version"] in ("*", item["version"])), None)
        if review:
            item.update(licenses=[review["license"]], license_identified=True,
                        license_source=f"manual-review {review['reviewed_at']}: {review['evidence']}")


def components(sbom: dict) -> list[dict]:
    if str(sbom.get("spdxVersion", "")).startswith("SPDX-"):
        result = []
        for package in sbom["packages"]:
            licenses = [package.get("licenseDeclared", "")]
            if licenses[0].upper() in UNKNOWN_LICENSES:
                licenses = [package.get("licenseConcluded", "")]
            purl = next((ref.get("referenceLocator", "") for ref in package.get("externalRefs", [])
                         if ref.get("referenceType") == "purl"), "")
            result.append({"id": package.get("SPDXID", ""), "purl": purl,
                           "name": package["name"], "version": package.get("versionInfo", ""), "licenses": licenses})
    elif sbom.get("bomFormat") == "CycloneDX":
        result = []
        for package in sbom["components"]:
            licenses = [item.get("expression") or item.get("license", {}).get("id")
                        or item.get("license", {}).get("name", "") for item in package.get("licenses", [])]
            name = f"{package['group']}/{package['name']}" if package.get("group") else package["name"]
            result.append({"id": package.get("bom-ref", ""), "purl": package.get("purl", ""),
                           "name": name, "version": package.get("version", ""), "licenses": licenses})
    else:
        raise ValueError("只接受工具生成的 SPDX JSON 或 CycloneDX JSON")
    if not result:
        raise ValueError("SBOM 为空，不能当作无依赖")
    for item in result:
        item["license_identified"] = bool(item["licenses"]) and all(
            name.upper() not in UNKNOWN_LICENSES for name in item["licenses"])
    return sorted(result, key=lambda item: (item["name"], item["version"]))


def summarize(sbom: dict, scan: dict, reviews: list[dict] | None = None, exceptions: dict | None = None,
              today: str | None = None) -> tuple[dict, list[dict]]:
    inventory = components(sbom)
    apply_reviews(inventory, load_reviews() if reviews is None else reviews)
    if not isinstance(scan.get("matches"), list) or scan.get("descriptor", {}).get("name") != "grype":
        raise ValueError("缺少有效的 Grype 扫描报告，不能当作零漏洞")
    if scan["descriptor"].get("db", {}).get("status", {}).get("valid") is not True:
        raise ValueError("扫描报告未确认漏洞数据库有效，不能当作已完成检查")
    findings = {}
    severity_rank = {"negligible": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    for match in scan["matches"]:
        vulnerability = match["vulnerability"]
        artifact = match["artifact"]
        item = {"id": vulnerability["id"], "severity": vulnerability.get("severity", "Unknown"),
                "package": artifact["name"], "version": artifact.get("version", ""),
                "fix": vulnerability.get("fix", {})}
        key = (item["id"], item["package"], item["version"])
        previous = findings.get(key)
        # 多数据源对同一命中分级不同时保留最高级别，避免后出现的 Low 覆盖 High。
        if previous is None or severity_rank.get(item["severity"].lower(), 5) > severity_rank.get(previous["severity"].lower(), 5):
            findings[key] = item
    counts = Counter(item["severity"] for item in findings.values())
    exceptions = load_exceptions() if exceptions is None else exceptions
    today = today or datetime.now(UTC).date().isoformat()
    blocked = accepted_count = 0
    for item in findings.values():
        if item["severity"].lower() in {"negligible", "low", "medium"}:
            continue
        # 计数照常显示全部命中；例外只决定是否阻断，并在报告里逐项标明
        if accepted(item, exceptions, today):
            item["accepted_exception"] = True
            accepted_count += 1
        else:
            blocked += 1
    unknown = sum(not item["license_identified"] for item in inventory)
    return {"components": len(inventory), "unidentified_licenses": unknown,
            "license_review": "pending", "vulnerability_counts": dict(sorted(counts.items())),
            "blocking_vulnerabilities": blocked, "accepted_exceptions": accepted_count,
            "exceptions_expire": exceptions.get("expires") or None,
            "vulnerability_gate": "blocked" if blocked else "pass",
            "evidence_gate": "blocked" if blocked or unknown else "pass",
            "release_approval": "not_assessed",
            "scanner": {key: scan["descriptor"].get(key) for key in ("name", "version", "db")},
            "findings": sorted(findings.values(), key=lambda item: (item["severity"], item["package"], item["id"]))}, inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--scan", type=Path, required=True)
    parser.add_argument("--scan-input", type=Path, help="Grype 实际读取的 SBOM，默认与 --sbom 相同")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", choices=SCOPES, required=True)
    subject = parser.add_mutually_exclusive_group(required=True)
    subject.add_argument("--subject-file", type=Path)
    subject.add_argument("--image-id")
    parser.add_argument("--enforce", action="store_true", help="有 High/Critical/Unknown 漏洞或未知许可证时返回失败")
    args = parser.parse_args()
    sbom = json.loads(args.sbom.read_text(encoding="utf-8-sig"))
    scan = json.loads(args.scan.read_text(encoding="utf-8-sig"))
    summary, inventory = summarize(sbom, scan)
    scan_input = args.scan_input or args.sbom
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT).returncode != 0
    if args.image_id:
        if not args.image_id.startswith("sha256:") or len(args.image_id) != 71:
            raise ValueError("image-id 必须是实际 docker image inspect 返回的配置摘要")
        target = {"type": "docker-image-config", "digest": args.image_id}
    else:
        target = {"type": "file", "name": args.subject_file.name, "sha256": digest(args.subject_file)}
    summary.update({"scope": args.scope, "subject": target, "source_revision": revision,
                    "source_dirty": dirty, "generated_at": datetime.now(UTC).isoformat(),
                    "workflow_run": os.getenv("GITHUB_RUN_ID"),
                    "inputs": {name: digest(ROOT / name) for name in INPUTS + SCOPE_INPUTS.get(args.scope, ())},
                    "reports": {"sbom": {"name": args.sbom.name, "sha256": digest(args.sbom)},
                                "scan_input": {"name": scan_input.name, "sha256": digest(scan_input)},
                                "scan": {"name": args.scan.name, "sha256": digest(args.scan)}}})
    args.output.mkdir(parents=True, exist_ok=True)
    for name, data in (("summary.json", summary), ("licenses.json", inventory)):
        (args.output / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 清单绑定报告和输入；它是完整性校验记录，不是数字签名或 SLSA provenance。
    paths = list(dict.fromkeys([args.sbom, scan_input, args.scan, args.output / "summary.json", args.output / "licenses.json"]))
    (args.output / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in paths), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("scope", "components", "unidentified_licenses",
                     "vulnerability_counts", "blocking_vulnerabilities", "accepted_exceptions",
                     "evidence_gate", "release_approval")}, ensure_ascii=False))
    return 1 if args.enforce and summary["evidence_gate"] != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())
