import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("supply_chain_evidence", Path(__file__).parents[2] / "scripts/supply_chain_evidence.py")
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)
SCANNER = {"name": "grype", "db": {"status": {"valid": True}}}


def test_findings_deduplicated_but_unfixed_high_still_blocks():
    sbom = {"spdxVersion": "SPDX-2.3", "packages": [{"name": "fake", "versionInfo": "1", "licenseDeclared": "MIT"}]}
    match = {"artifact": {"name": "fake", "version": "1"},
             "vulnerability": {"id": "FAKE-001", "severity": "High", "fix": {"state": "not-fixed", "versions": []}}}
    lower = {**match, "vulnerability": {**match["vulnerability"], "severity": "Low"}}
    result, _ = evidence.summarize(sbom, {"matches": [match, match, lower], "descriptor": SCANNER})
    assert result["vulnerability_counts"] == {"High": 1}
    assert result["evidence_gate"] == "blocked" and result["release_approval"] == "not_assessed"


def test_unknown_license_blocks_even_with_no_findings():
    sbom = {"bomFormat": "CycloneDX", "components": [
        {"name": "fake", "group": "@example", "version": "1", "licenses": [],
         "bom-ref": "fake-ref", "purl": "pkg:npm/%40example/fake@1"}]}
    result, inventory = evidence.summarize(sbom, {"matches": [], "descriptor": SCANNER})
    assert result["vulnerability_gate"] == "pass" and result["evidence_gate"] == "blocked"
    assert not inventory[0]["license_identified"] and result["license_review"] == "pending"
    assert inventory[0]["name"] == "@example/fake" and inventory[0]["id"] == "fake-ref"
    assert inventory[0]["purl"] == "pkg:npm/%40example/fake@1"


@pytest.mark.parametrize("sbom,scan", [
    ({"bomFormat": "CycloneDX", "components": []}, {"matches": [], "descriptor": {"name": "grype"}}),
    ({"bomFormat": "CycloneDX", "components": [{"name": "fake"}]}, {}),
])
def test_empty_sbom_and_missing_scan_never_pass(sbom, scan):
    with pytest.raises(ValueError):
        evidence.summarize(sbom, scan)


def test_missing_database_status_cannot_be_reported_as_clean():
    sbom = {"bomFormat": "CycloneDX", "components": [{"name": "fake", "licenses": [{"expression": "MIT"}]}]}
    with pytest.raises(ValueError, match="数据库有效"):
        evidence.summarize(sbom, {"matches": [], "descriptor": {"name": "grype"}})


def test_manual_license_review_requires_exact_version_and_never_overrides_tool():
    review = {"name": "fake", "version": "1", "license": "BSD-2-Clause", "evidence": "包内 LICENSE", "reviewed_at": "2026-10-18"}
    sbom = {"spdxVersion": "SPDX-2.3", "packages": [
        {"name": "fake", "versionInfo": "1", "licenseDeclared": "NOASSERTION"},
        {"name": "fake", "versionInfo": "2", "licenseDeclared": "NOASSERTION"},
        {"name": "known", "versionInfo": "1", "licenseDeclared": "MIT"}]}
    scan = {"matches": [], "descriptor": SCANNER}
    result, inventory = evidence.summarize(sbom, scan, reviews=[review, {**review, "name": "known", "license": "GPL-3.0"}])
    by_version = {(i["name"], i["version"]): i for i in inventory}
    assert by_version[("fake", "1")]["licenses"] == ["BSD-2-Clause"] and "manual-review" in by_version[("fake", "1")]["license_source"]
    assert not by_version[("fake", "2")]["license_identified"]  # 版本变化必须重新核对
    assert by_version[("known", "1")]["licenses"] == ["MIT"]  # 工具已识别的不被人工记录覆盖
    assert result["unidentified_licenses"] == 1 and result["evidence_gate"] == "blocked" and result["license_review"] == "pending"
    result, _ = evidence.summarize({**sbom, "packages": sbom["packages"][::2]}, scan, reviews=[review])
    assert result["evidence_gate"] == "pass"


def test_license_review_file_is_complete_and_wildcard_only_for_own_project(tmp_path):
    assert {r["name"] for r in evidence.load_reviews()} >= {"pysnmp", "python", "yunshu-ops"}
    bad = tmp_path / "reviews.json"
    bad.write_text('{"reviews": [{"name": "other", "version": "*", "license": "MIT", "evidence": "x", "reviewed_at": "2026-10-18"}]}', encoding="utf-8")
    with pytest.raises(ValueError):
        evidence.load_reviews(bad)
    bad.write_text('{"reviews": [{"name": "other", "version": "1", "license": "MIT"}]}', encoding="utf-8")
    with pytest.raises(ValueError):
        evidence.load_reviews(bad)


def test_vulnerability_exception_only_for_registered_unfixed_and_unexpired():
    sbom = {"spdxVersion": "SPDX-2.3", "packages": [{"name": "libfake", "versionInfo": "1", "licenseDeclared": "MIT"}]}
    exc = {"reviewed_at": "2026-10-18", "expires": "2027-01-16",
           "exceptions": [{"id": "CVE-FAKE-1", "package": "libfake", "version": "1"}]}

    def run(fix, version="1", today="2026-10-18", vid="CVE-FAKE-1"):
        match = {"artifact": {"name": "libfake", "version": version},
                 "vulnerability": {"id": vid, "severity": "Critical", "fix": fix}}
        return evidence.summarize(sbom, {"matches": [match], "descriptor": SCANNER}, reviews=[], exceptions=exc, today=today)[0]

    ok = run({"state": "wont-fix", "versions": []})
    assert ok["vulnerability_gate"] == "pass" and ok["accepted_exceptions"] == 1 and ok["vulnerability_counts"] == {"Critical": 1}
    assert ok["findings"][0]["accepted_exception"] is True
    assert run({"state": "fixed", "versions": ["2"]})["vulnerability_gate"] == "blocked"  # 出现修复版本必须升级
    assert run({"state": "not-fixed", "versions": []}, version="1.1")["vulnerability_gate"] == "blocked"  # 包版本变化须重新登记
    assert run({"state": "wont-fix", "versions": []}, today="2027-01-17")["vulnerability_gate"] == "blocked"  # 到期复查
    assert run({"state": "wont-fix", "versions": []}, vid="CVE-FAKE-2")["vulnerability_gate"] == "blocked"  # 未登记


def test_exception_file_entries_are_exact():
    doc = evidence.load_exceptions()
    assert doc["expires"] and all(e["version"] and e["id"] and e["package"] for e in doc["exceptions"])
    assert not any(e["package"].startswith(("freeipmi", "libfreeipmi")) for e in doc["exceptions"])


@pytest.mark.parametrize("severity, gate, code", [("Low", "pass", 0), ("High", "blocked", 1)])
def test_linux_package_scope_summarizes_and_gates(tmp_path, monkeypatch, severity, gate, code):
    import json
    import sys

    sbom = tmp_path / "sbom.spdx.json"
    sbom.write_text(json.dumps({"spdxVersion": "SPDX-2.3", "packages": [
        {"name": "fake-lib", "versionInfo": "1", "licenseDeclared": "MIT"}]}), encoding="utf-8")
    scan = tmp_path / "scan.grype.json"
    scan.write_text(json.dumps({"descriptor": SCANNER, "matches": [
        {"artifact": {"name": "fake-lib", "version": "1"},
         "vulnerability": {"id": "CVE-FAKE-9", "severity": severity, "fix": {"state": "fixed", "versions": ["2"]}}}]}),
        encoding="utf-8")
    subject = tmp_path / "yunshu-0.0.0-linux-amd64.tar.gz"
    subject.write_bytes(b"fake tarball")
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["supply_chain_evidence.py", "--sbom", str(sbom), "--scan", str(scan),
                                      "--output", str(out), "--scope", "linux-package",
                                      "--subject-file", str(subject), "--enforce"])
    assert evidence.main() == code
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["scope"] == "linux-package" and summary["evidence_gate"] == gate
    assert summary["subject"] == {"type": "file", "name": subject.name, "sha256": evidence.digest(subject)}
    # 原有输入照常绑定，另外绑定 Linux 打包脚本
    assert set(evidence.INPUTS) | set(evidence.SCOPE_INPUTS["linux-package"]) == set(summary["inputs"])
    assert (out / "SHA256SUMS").is_file()


def test_existing_scopes_unchanged():
    assert evidence.SCOPES[:3] == ("image", "frontend-lock", "desktop-build-environment")
    assert set(evidence.SCOPE_INPUTS) == {"linux-package"}
