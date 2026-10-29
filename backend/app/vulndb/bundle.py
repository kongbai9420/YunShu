"""离线包：在能上网的机器上生成，拿到不能上网的平台导入（外网同步默认关闭时用）。

    python -m app.vulndb.bundle --out yunshu-vulndb.zip                          # 默认：KEV、EPSS、Debian 11/12/13、常见设备的 NVD 数据
    python -m app.vulndb.bundle --out x.zip --ubuntu                            # 另加 Ubuntu LTS（需下载约 770 MB 的 OSV 数据）
    python -m app.vulndb.bundle --out x.zip --product dell:poweredge_r740_firmware   # 另加某个产品

包内容（zip）：manifest.json、kev.json、epss.csv.gz、osv/<生态>.jsonl.gz（已按生态与记录类型过滤）、nvd/<产品>.json。
导入时与在线同步用同一套解析，来源记为各自的名称（nvd / kev / osv），并在设置里记下包的生成时间。
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import shutil
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import httpx
from sqlalchemy.orm import Session

from app.vulndb import sources
from app.vulndb.normalize import upsert

FORMAT = 1
DEFAULT_ECOSYSTEMS = ("Debian:11", "Debian:12", "Debian:13")
UBUNTU_ECOSYSTEMS = ("Ubuntu:20.04:LTS", "Ubuntu:22.04:LTS", "Ubuntu:24.04:LTS")
DEFAULT_PRODUCTS = ("dell:idrac8_firmware", "dell:idrac9_firmware", "dell:idrac10_firmware", "vmware:esxi", "vmware:vcenter_server",
                    "proxmox:virtual_environment", "synology:diskstation_manager", "mikrotik:routeros")
OSV_ZIP = "https://storage.googleapis.com/osv-vulnerabilities/{base}/all.zip"
EPSS_CSV = "https://epss.empiricalsecurity.com/epss_scores-current.csv.gz"
MAX_ENTRY = 2 * 1024 ** 3  # 单个文件解压后上限，防止压缩炸弹


def build(out: Path, *, http: httpx.Client, ecosystems: Iterable[str] = DEFAULT_ECOSYSTEMS, products: Iterable[str] = DEFAULT_PRODUCTS,
          nvd_key: str = "", log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    ecosystems, products = sorted(set(ecosystems)), sorted(set(products))
    manifest: dict[str, Any] = {"format": FORMAT, "created_at": datetime.now(UTC).isoformat(), "ecosystems": ecosystems, "products": products, "counts": {}}
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        log("下载 CISA KEV …")
        kev = sources._get_json(http, sources.KEV_URL)
        z.writestr("kev.json", json.dumps(kev, ensure_ascii=False))
        manifest["counts"]["kev"] = len(kev.get("vulnerabilities") or [])
        log("下载 EPSS …")
        resp = http.get(EPSS_CSV, headers=sources.UA, follow_redirects=True)
        if resp.status_code == 200:
            z.writestr("epss.csv.gz", resp.content)
            manifest["counts"]["epss"] = "ok"
        for base in sorted({e.split(":")[0] for e in ecosystems}):
            wanted = {e for e in ecosystems if e.split(":")[0] == base}
            log(f"下载 OSV {base}（文件较大，请耐心等待）…")
            n = _osv_ecosystem(http, base, wanted, z)
            manifest["counts"][f"osv:{base}"] = n
        for i, product in enumerate(products):
            if i:
                sleep(0.6 if nvd_key else 6)
            log(f"下载 NVD {product} …")
            cves = list(sources.nvd_pages(http, {"virtualMatchString": sources.cpe_match_string(product)}, api_key=nvd_key, sleep=sleep))
            z.writestr(f"nvd/{product.replace(':', '__')}.json", json.dumps({"product": product, "vulnerabilities": [{"cve": c} for c in cves]}, ensure_ascii=False))
            manifest["counts"][f"nvd:{product}"] = len(cves)
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return manifest


def _osv_ecosystem(http: httpx.Client, base: str, wanted: set[str], z: zipfile.ZipFile) -> int:
    with tempfile.TemporaryFile() as tmp:
        with http.stream("GET", OSV_ZIP.format(base=base), headers=sources.UA, follow_redirects=True) as resp:
            if resp.status_code != 200:
                raise sources.SourceError(f"下载 OSV {base} 失败：HTTP {resp.status_code}")
            for chunk in resp.iter_bytes(1 << 20):
                tmp.write(chunk)
        tmp.seek(0)
        buf = io.BytesIO()
        n = 0
        with zipfile.ZipFile(tmp) as src, gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            for info in src.infolist():
                if not sources.osv_wanted(Path(info.filename).stem) or info.file_size > 50 * 1024 ** 2:
                    continue
                rec = json.loads(src.read(info))
                rec["affected"] = [a for a in rec.get("affected") or [] if (a.get("package") or {}).get("ecosystem") in wanted]
                if rec["affected"]:
                    gz.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
                    n += 1
        z.writestr(f"osv/{base}.jsonl.gz", buf.getvalue())
        return n


def import_bundle(db: Session, fileobj: IO[bytes], *, now: datetime | None = None, batch: int = 500) -> dict[str, Any]:
    """导入离线包，返回各部分条数与新出现的漏洞数。"""
    now = now or datetime.now(UTC)
    new_total, counts = 0, {}
    try:
        z = zipfile.ZipFile(fileobj)
    except zipfile.BadZipFile as exc:
        raise ValueError("不是有效的离线包（zip）") from exc
    with z:
        names = set(z.namelist())
        if "manifest.json" not in names:
            raise ValueError("离线包缺少 manifest.json")
        manifest = json.loads(_read(z, "manifest.json"))
        if manifest.get("format") != FORMAT:
            raise ValueError(f"离线包格式版本 {manifest.get('format')} 不受支持")
        ecosystems = set(manifest.get("ecosystems") or [])
        products = set(manifest.get("products") or [])
        if "kev.json" in names:
            recs = sources.parse_kev(json.loads(_read(z, "kev.json")))
            new_total += len(upsert(db, recs, now))
            counts["kev"] = len(recs)
        for name in sorted(n for n in names if n.startswith("osv/") and n.endswith(".jsonl.gz")):
            n, chunk = 0, []
            with gzip.GzipFile(fileobj=io.BytesIO(_read(z, name))) as gz:
                for line in gz:
                    rec = sources.parse_osv(json.loads(line), ecosystems or None)
                    if rec:
                        chunk.append(rec)
                    if len(chunk) >= batch:
                        new_total += len(upsert(db, chunk, now))
                        n, chunk = n + len(chunk), []
            new_total += len(upsert(db, chunk, now))
            counts[name] = n + len(chunk)
        for name in sorted(n for n in names if n.startswith("nvd/") and n.endswith(".json")):
            data = json.loads(_read(z, name))
            tracked = {data.get("product")} | products
            recs = [sources.parse_nvd_cve(v["cve"], tracked) for v in data.get("vulnerabilities") or [] if v.get("cve", {}).get("id")]
            for i in range(0, len(recs), batch):
                new_total += len(upsert(db, recs[i:i + batch], now))
            counts[name] = len(recs)
        if "epss.csv.gz" in names:
            from app.models import VulnRecord
            from sqlalchemy import select

            wanted = set(db.scalars(select(VulnRecord.id).where(VulnRecord.id.like("CVE-%"))))
            scores = sources.parse_epss_csv(_read(z, "epss.csv.gz"), wanted)
            from app.vulndb.sync import apply_epss

            counts["epss"] = apply_epss(db, scores)
    return {"created_at": manifest.get("created_at"), "counts": counts, "new": new_total}


def _read(z: zipfile.ZipFile, name: str) -> bytes:
    info = z.getinfo(name)
    if info.file_size > MAX_ENTRY:
        raise ValueError(f"{name} 过大")
    return z.read(name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成云枢漏洞情报离线包（在能上网的机器上运行）")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--ubuntu", action="store_true", help="另加 Ubuntu 20.04 / 22.04 / 24.04 LTS（需下载约 770 MB）")
    ap.add_argument("--ecosystem", action="append", default=[], help="另加发行版生态，如 Debian:12")
    ap.add_argument("--product", action="append", default=[], help="另加 NVD 产品，格式“厂商:产品”，如 dell:poweredge_r740_firmware")
    ap.add_argument("--nvd-key", default="", help="NVD API 密钥（可选，能加快下载）")
    ap.add_argument("--proxy", default="", help="代理地址（可选）")
    args = ap.parse_args(argv)
    ecosystems = [*DEFAULT_ECOSYSTEMS, *(UBUNTU_ECOSYSTEMS if args.ubuntu else ()), *args.ecosystem]
    with httpx.Client(timeout=120, proxy=args.proxy or None) as http:
        tmp = args.out.with_suffix(".part")
        manifest = build(tmp, http=http, ecosystems=ecosystems, products=[*DEFAULT_PRODUCTS, *args.product], nvd_key=args.nvd_key)
        shutil.move(tmp, args.out)
    print(json.dumps(manifest["counts"], ensure_ascii=False, indent=1))
    print(f"已生成 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
