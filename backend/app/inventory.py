"""软件清单（4.3 P18b）：把各协议采集到的版本整理成统一的列表，并读取 Linux 已安装软件包。

系统、固件、虚拟化版本从每个端点最近一次采集的 facts 推导（不另存表）；
软件包由每天一次的 inventory.packages 任务通过 SSH 只读命令读取（端点上 collect_packages 打开时才读）。

每一项给出 cpe（厂商:产品，用于按 NVD 比对）或 ecosystem（发行版，用于按 OSV 比对软件包）。
认不出的设备不编造，留空即“未覆盖”。
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import queue
from app.collect.base import AdapterError
from app.crypto import CredentialCipher
from app.models import Asset, AssetPackage, CollectionStatus, ManagementEndpoint, PackageScan, Task

PACKAGE_TASK = "inventory.packages"
SCAN_INTERVAL = timedelta(hours=24)


def ecosystem(os_id: str, version: str) -> str:
    """发行版 → OSV 生态名。Ubuntu 的 LTS 版本（偶数年 .04）带 :LTS 后缀。PVE 的 os-release 是 Debian。"""
    os_id, version = (os_id or "").lower(), (version or "").strip()
    if not version:
        return ""
    if os_id == "debian":
        return f"Debian:{version.split('.')[0]}"
    if os_id == "ubuntu":
        m = re.match(r"^(\d{2})\.(\d{2})", version)
        if not m:
            return ""
        lts = int(m.group(1)) % 2 == 0 and m.group(2) == "04"
        return f"Ubuntu:{m.group(1)}.{m.group(2)}" + (":LTS" if lts else "")
    return ""


def idrac_generation(*, name: str = "", bmc_model: str = "", server_model: str = "", firmware: str = "") -> int | None:
    """iDRAC 代次：优先看 racShortName（iDRAC9），其次 Redfish Manager.Model（14G Monolithic），再看服务器型号，最后看固件主版本。"""
    m = re.search(r"idrac\s*(\d+)", name or "", re.I)
    if m:
        return int(m.group(1))
    by_server_gen = {12: 7, 13: 8, 14: 9, 15: 9, 16: 9, 17: 10}
    m = re.search(r"\b(1[2-7])G\b", bmc_model or "")
    if m:
        return by_server_gen[int(m.group(1))]
    m = re.search(r"\b[RTMCX][Ss]?\d(\d)\d", server_model or "")  # PowerEdge R740：中间一位 4 表示第 14 代
    if m:
        return by_server_gen.get(10 + int(m.group(1)))
    m = re.match(r"^(\d+)\.", firmware or "")
    if m and 3 <= int(m.group(1)) <= 7:
        return 9  # iDRAC9 固件是 3.x～7.x；1.x / 2.x 在 iDRAC7、8、10 之间无法区分，不猜
    return None


def dell_model_key(model: str) -> str:
    """PowerEdge R740xd → r740xd，用于 NVD 的 dell:poweredge_r740xd_firmware。"""
    m = re.search(r"\b([RTMCX][Ss]?\d{3,4}[a-z]*)\b", model or "", re.I)
    return m.group(1).lower() if m else ""


def _item(kind: str, label: str, vendor: str, product: str, version: str, *, cpe: str = "", ecosystem_name: str = "",
          source: str, detail: str = "", observed: datetime | None = None) -> dict[str, Any]:
    return {"kind": kind, "label": label, "vendor": vendor, "product": product, "version": version, "detail": detail,
            "cpe": cpe, "ecosystem": ecosystem_name, "source": source, "observed_at": observed.isoformat() if observed else None}


def software_for(asset: Asset, statuses: list[CollectionStatus]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(it: dict[str, Any]) -> None:
        key = (it["cpe"] or it["label"], it["version"])
        if it["version"] and key not in seen:
            seen.add(key)
            items.append(it)

    is_dell = "dell" in (asset.vendor or "").lower() or (asset.model or "").lower().startswith("poweredge")
    for st in statuses:
        f, when, src = st.facts or {}, st.last_success_at, st.protocol
        if f.get("os_id") and f.get("os_version"):
            eco = ecosystem(f["os_id"], f["os_version"])
            add(_item("os", f.get("os") or f["os_id"], f["os_id"], f["os_id"], f["os_version"], ecosystem_name=eco, source=src, observed=when,
                      detail="" if eco else "这个系统暂不支持软件包漏洞比对"))
        if f.get("pve_version"):
            ver = str(f["pve_version"]).split("/")[-1] if "/" in str(f["pve_version"]) else str(f["pve_version"])
            add(_item("hypervisor", "Proxmox VE", "proxmox", "virtual_environment", ver, cpe="proxmox:virtual_environment", source=src, observed=when))
        if f.get("kernel"):
            add(_item("os", "Linux 内核", "linux", "linux_kernel", str(f["kernel"]), source=src, observed=when, detail="内核按发行版软件包比对"))
        if f.get("api_type") in ("HostAgent", "VirtualCenter") and f.get("version"):
            esxi = f["api_type"] == "HostAgent"
            add(_item("hypervisor", "VMware ESXi" if esxi else "VMware vCenter Server", "vmware", "esxi" if esxi else "vcenter_server",
                      str(f["version"]), cpe="vmware:esxi" if esxi else "vmware:vcenter_server", source=src, observed=when,
                      detail=f"构建号 {f['build']}" if f.get("build") else "没有构建号，只能按大版本判断"))
        bmc_fw = f.get("idrac_firmware") or (f.get("bmc_firmware") if is_dell else None)
        if bmc_fw:
            gen = idrac_generation(name=f.get("idrac_name", ""), bmc_model=f.get("bmc_model", ""),
                                   server_model=f.get("system_model") or asset.model, firmware=str(bmc_fw))
            add(_item("firmware", f"iDRAC{gen or ''} 固件", "dell", f"idrac{gen}_firmware" if gen else "idrac_firmware", str(bmc_fw),
                      cpe=f"dell:idrac{gen}_firmware" if gen else "", source=src, observed=when,
                      detail="" if gen else "无法判断 iDRAC 代次，暂不比对"))
        if f.get("bios") and is_dell:
            key = dell_model_key(f.get("model") or asset.model)
            add(_item("bios", "BIOS", "dell", f"poweredge_{key}_firmware" if key else "bios", str(f["bios"]),
                      cpe=f"dell:poweredge_{key}_firmware" if key else "", source=src, observed=when,
                      detail="" if key else "无法识别服务器型号，暂不比对"))
        if f.get("dsm_version"):
            m = re.search(r"(\d+\.\d+(?:\.\d+)?-\d+)(?:\s+Update\s+(\d+))?", str(f["dsm_version"]))
            ver = (m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")) if m else str(f["dsm_version"])
            add(_item("appliance", "群晖 DSM", "synology", "diskstation_manager", ver, cpe="synology:diskstation_manager", source=src,
                      observed=when, detail=f"{f.get('dsm_model', '')} {f['dsm_version']}".strip()))
        if f.get("routeros_version"):
            ver = str(f["routeros_version"]).split()[0]
            add(_item("appliance", "MikroTik RouterOS", "mikrotik", "routeros", ver, cpe="mikrotik:routeros", source=src, observed=when))
    return items


def asset_software(db: Session, asset: Asset) -> dict[str, Any]:
    statuses = list(db.scalars(select(CollectionStatus).where(CollectionStatus.asset_id == asset.id)))
    scans = list(db.scalars(select(PackageScan).where(PackageScan.asset_id == asset.id)))
    count = {s.endpoint_id: s.count for s in scans}
    return {
        "items": software_for(asset, statuses),
        "package_scans": [{"endpoint_id": str(s.endpoint_id), "state": s.state, "message": s.message, "manager": s.manager,
                           "ecosystem": s.ecosystem, "count": count.get(s.endpoint_id, 0),
                           "last_attempt_at": s.last_attempt_at.isoformat() if s.last_attempt_at else None,
                           "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None} for s in scans],
        "packages_enabled": [str(e.id) for e in asset.endpoints if e.collect_packages and e.protocol == "ssh"],
    }


def list_packages(db: Session, asset_id: uuid.UUID, q: str = "", limit: int = 200) -> list[dict[str, Any]]:
    stmt = select(AssetPackage).where(AssetPackage.asset_id == asset_id).order_by(AssetPackage.name).limit(limit)
    if q:
        stmt = stmt.where(AssetPackage.name.contains(q) | AssetPackage.source.contains(q))
    return [{"name": p.name, "version": p.version, "source": p.source, "source_version": p.source_version, "ecosystem": p.ecosystem}
            for p in db.scalars(stmt)]


# ---- 软件包读取 ----


def scan_endpoint(db: Session, cipher: CredentialCipher, ep: ManagementEndpoint, adapter: Any, now: datetime | None = None) -> PackageScan:
    from app.collect.runner import _context

    now = now or datetime.now(UTC)
    scan = db.get(PackageScan, ep.id) or PackageScan(endpoint_id=ep.id, asset_id=ep.asset_id, count=0)
    db.add(scan)
    scan.last_attempt_at = now
    try:
        ctx = _context(db, cipher, ep, "ssh")
        result = adapter.packages(ctx)
    except AdapterError as exc:
        scan.state, scan.message = "failed", str(exc)[:1000]
        return scan
    if ctx.trust.observed_ssh and not ep.ssh_host_key:
        ep.ssh_host_key, ep.trust_pinned_at = ctx.trust.observed_ssh, now
    eco = ecosystem(result.get("os_id", ""), result.get("os_version", ""))
    db.execute(delete(AssetPackage).where(AssetPackage.endpoint_id == ep.id))
    names: set[str] = set()
    for name, version, source, source_version in result.get("items", []):
        if name in names:
            continue
        names.add(name)
        db.add(AssetPackage(asset_id=ep.asset_id, endpoint_id=ep.id, ecosystem=eco, name=name[:200], version=version[:200],
                            source=source[:200], source_version=source_version[:200], observed_at=now))
    scan.manager, scan.ecosystem, scan.count = result.get("manager", ""), eco, len(names)
    scan.last_success_at = now
    if not result.get("manager"):
        scan.state, scan.message = "unsupported", "没有找到 dpkg、apk 或 rpm，无法读取软件包"
    else:
        scan.state = "ok"
        notes = []
        if not eco:
            notes.append("这个系统暂不支持软件包漏洞比对（目前支持 Debian、Ubuntu，含 PVE）")
        if result.get("truncated"):
            notes.append("软件包超过 2 万个，只保存了前 2 万个")
        scan.message = "；".join(notes)
    return scan


def enqueue_due(db: Session, *, now: datetime | None = None) -> int:
    """每天为打开了软件包清单的 SSH 端点入队一次读取任务。"""
    now = now or datetime.now(UTC)
    count = 0
    eps = db.scalars(select(ManagementEndpoint).join(Asset, Asset.id == ManagementEndpoint.asset_id)
                     .where(ManagementEndpoint.collect_packages.is_(True), ManagementEndpoint.protocol == "ssh",
                            ManagementEndpoint.purpose != "control", Asset.lifecycle == "active"))
    for ep in eps:
        scan = db.get(PackageScan, ep.id)
        last = scan.last_attempt_at if scan else None
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if last is not None and now - last < SCAN_INTERVAL:
            continue
        key = f"pkgscan:{ep.id}:{now.date().isoformat()}"
        if db.scalar(select(Task.id).where(Task.idempotency_key == key)) is not None:
            continue
        queue.enqueue(db, PACKAGE_TASK, asset_id=ep.asset_id, payload={"endpoint_id": str(ep.id)}, idempotency_key=key, max_attempts=1,
                      priority=queue.BACKGROUND_PRIORITY)
        count += 1
    return count


def request_scan(db: Session, asset: Asset, now: datetime | None = None) -> int:
    """页面上点“立即读取”：为这台资产所有打开开关的 SSH 端点各入队一次。"""
    now = now or datetime.now(UTC)
    n = 0
    for ep in asset.endpoints:
        if ep.collect_packages and ep.protocol == "ssh" and ep.purpose != "control":
            queue.enqueue(db, PACKAGE_TASK, asset_id=asset.id, payload={"endpoint_id": str(ep.id)},
                          idempotency_key=f"pkgscan:{ep.id}:manual:{int(now.timestamp()) // 60}", max_attempts=1,
                          priority=queue.BACKGROUND_PRIORITY)
            n += 1
    return n


def make_handler(cipher_factory: Callable[[], CredentialCipher], adapter: Any):  # type: ignore[no-untyped-def]
    def handle(ctx) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        with ctx.session_factory() as db:
            ep = db.get(ManagementEndpoint, uuid.UUID(ctx.lease.payload["endpoint_id"]))
            if ep is None or not ep.collect_packages:
                return {"skipped": True}
            scan = scan_endpoint(db, cipher_factory(), ep, adapter)
            db.commit()
            if scan.state == "ok":
                from app import vulnmatch

                vulnmatch.run(db, asset_ids=[ep.asset_id])
                db.commit()
            return {"state": scan.state, "count": scan.count, "ecosystem": scan.ecosystem}

    return handle
