"""按采集模板执行一台资产的采集，并写入指标与采集状态。

- primary 依次尝试，有一个成功就不再试 fallback；全部失败才用 fallback，并在指标与状态里标出“降级”。
- supplement 总是执行，或按 every 每隔几个周期执行一次（如 Dell 的 Redfish 每 10 个周期补充一次并读取 BMC 日志）。
- 失败时，把该来源以前报过的指标写成 failed（值为空），界面显示“采集失败”而不是旧值或 0。
- 首次连接时固定 TLS 指纹 / SSH 主机密钥；之后不一致适配器会拒绝，状态记为 untrusted。
- 服务器 BMC 有两个以上协议时按采集计划执行（4.4 P20a，见 app/collect/plan.py），不按模板步骤。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import bmc_gate, metrics, queue, server_events
from app.collect import plan as plans
from app.collect.base import PROFILES, Adapter, AdapterError, EndpointContext, Profile, Trust, infer_profile
from app.crypto import CredentialCipher, DecryptionError
from app.metrics import Point
from app.models import Asset, CollectionPlan, CollectionStatus, Credential, LatestMetric, ManagementEndpoint, Task
from app.services.credentials import reveal_secret

PROTOCOL_LABEL = {"snmp": "SNMP", "ssh": "SSH", "ipmi": "IPMI", "redfish": "Redfish", "pve-api": "PVE API", "esxi-api": "ESXi API", "reach": "可达性"}


@dataclass
class AssetRun:
    asset_id: uuid.UUID
    profile: str | None
    ok: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    degraded: bool = False
    cooling_down: list[str] = field(default_factory=list)
    unconfigured: list[str] = field(default_factory=list)  # 缺凭据，没有连设备；不算协议失败
    probed: str = ""  # 本次重新探测了采集计划时的原因（4.4 P20a）
    # 4.4 P20b：BMC 排队或频率上限导致本轮顺延的协议，以及整台顺延的原因（不算失败，状态不变）
    deferred: list[str] = field(default_factory=list)
    deferred_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"profile": self.profile, "ok": self.ok, "failed": self.failed, "skipped": self.skipped, "degraded": self.degraded,
                "cooling_down": self.cooling_down, "unconfigured": self.unconfigured, "probed": self.probed,
                "deferred": self.deferred, "deferred_reason": self.deferred_reason}


def retry_after(st: CollectionStatus, interval: int) -> datetime | None:
    """首选失败后按 2/4/8 个周期冷却，最多 10 分钟；不会每轮同时登录所有协议。"""
    if st.state not in ("failed", "untrusted") or st.consecutive_failures < 1:
        return None
    attempted = st.last_attempt_at if st.last_attempt_at.tzinfo else st.last_attempt_at.replace(tzinfo=UTC)
    return attempted + timedelta(seconds=min(600, interval * 2 ** min(st.consecutive_failures, 3)))


def resolve_profile(asset: Asset) -> Profile | None:
    name = asset.collection_profile or infer_profile(asset.platform, asset.role, {e.protocol for e in asset.endpoints})
    return PROFILES.get(name) if name else None


def _endpoints_for(asset: Asset, protocol: str) -> list[ManagementEndpoint]:
    if protocol == "reach":
        return [e for e in asset.endpoints if e.purpose != "control"][:1]
    return [e for e in asset.endpoints if e.protocol == protocol and e.purpose != "control"]


def _context(db: Session, cipher: CredentialCipher, ep: ManagementEndpoint, protocol: str) -> EndpointContext:
    username = secret = kind = ""
    if ep.credential_id and protocol != "reach":
        cred = db.get(Credential, ep.credential_id)
        if cred is not None:
            try:
                secret = reveal_secret(cred, cipher)
            except DecryptionError as exc:
                raise AdapterError("unconfigured", f"凭据“{cred.alias}”无法解密：{exc}") from exc
            username, kind = cred.username, cred.kind
            cred.last_used_at = datetime.now(UTC)
    return EndpointContext(
        address=ep.address, port=ep.port, protocol=protocol, username=username, secret=secret, credential_kind=kind,
        tls_verify=ep.tls_verify, trust=Trust(tls_fingerprint=ep.tls_fingerprint, ssh_host_key=ep.ssh_host_key),
        options={"endpoint_protocol": ep.protocol},
    )


def _status(db: Session, ep: ManagementEndpoint, protocol: str) -> CollectionStatus:
    st = db.get(CollectionStatus, ep.id)
    if st is None:
        st = CollectionStatus(
            endpoint_id=ep.id, asset_id=ep.asset_id, protocol=protocol, state="skipped", last_attempt_at=datetime.now(UTC),
            consecutive_failures=0, duration_ms=0, message="", error_kind="", capabilities=[], facts={},
        )
        db.add(st)
    return st


def _mark_failed_points(db: Session, asset_id: uuid.UUID, protocol: str, message: str, now: datetime, interval: int,
                        quality: str = "failed") -> None:
    keys = db.execute(select(LatestMetric.metric_key, LatestMetric.unit).where(LatestMetric.asset_id == asset_id, LatestMetric.source == protocol)).all()
    if keys:
        metrics.write_sample(
            db, asset_id, protocol,
            [Point(k, None, unit, quality=quality, message=message) for k, unit in keys],
            observed_at=now, interval_seconds=interval,
        )


def _run_endpoint(
    db: Session, cipher: CredentialCipher, adapter: Adapter, asset: Asset, ep: ManagementEndpoint, protocol: str,
    *, interval: int, note: str, now: datetime, capture: list[Point] | None = None, options: dict[str, Any] | None = None,
    sel_capture: list[dict[str, Any]] | None = None, limited: bool = True,
) -> bool | None:
    """capture 不为 None 时只收集指标、不写入（采集计划按数据项筛选后再写）；状态照常更新。

    options 传给适配器（如让 IPMI 同时读 SEL）；BMC 日志条目在 sel_capture 不为 None 时交给调用方，否则直接存入服务器告警。
    返回 None 表示超过这台 BMC 的每分钟访问上限、本轮顺延（4.4 P20b）：没有连接设备，状态不变。limited=False（探测）不受上限限制。
    """
    if bmc_gate.allow(db.get_bind(), ep.address, protocol, "collect" if limited else "probe", now):
        return None
    st = _status(db, ep, protocol)
    st.last_attempt_at = now
    t0 = time.perf_counter()
    try:
        ctx = _context(db, cipher, ep, protocol)
        if options:
            ctx.options.update(options)
        result = adapter.collect(ctx)
    except AdapterError as exc:
        # 缺少凭据等配置问题单独标为 unconfigured：没有连设备，界面提示“待配置凭据”，不当作采集失败
        st.state = {"untrusted": "untrusted", "unconfigured": "unconfigured"}.get(exc.kind, "failed")
        st.error_kind, st.message = exc.kind, str(exc)[:1000]
        st.consecutive_failures += 1
        st.duration_ms = int((time.perf_counter() - t0) * 1000)
        unconfigured = exc.kind == "unconfigured"
        _mark_failed_points(db, asset.id, protocol, f"{PROTOCOL_LABEL.get(protocol, protocol)} {'待配置凭据' if unconfigured else '采集失败'}：{exc}",
                            now, interval, quality="unconfigured" if unconfigured else "failed")
        return False
    except Exception as exc:  # noqa: BLE001 - 适配器内部错误按采集失败处理，不影响其他端点
        st.state, st.error_kind, st.message = "failed", "protocol", f"{type(exc).__name__}: {exc}"[:1000]
        st.consecutive_failures += 1
        st.duration_ms = int((time.perf_counter() - t0) * 1000)
        _mark_failed_points(db, asset.id, protocol, f"{PROTOCOL_LABEL.get(protocol, protocol)} 采集出错", now, interval)
        return False
    # 首次信任：没有固定值时记下这次看到的指纹
    if ctx.trust.observed_tls and not ep.tls_fingerprint:
        ep.tls_fingerprint, ep.trust_pinned_at = ctx.trust.observed_tls, now
    if ctx.trust.observed_ssh and not ep.ssh_host_key:
        ep.ssh_host_key, ep.trust_pinned_at = ctx.trust.observed_ssh, now
    if note:
        for p in result.points:
            p.message = (note + "；" + p.message) if p.message else note
    if capture is not None:
        capture.extend(result.points)
    else:
        metrics.write_sample(db, asset.id, protocol, result.points, observed_at=now, interval_seconds=interval)
    st.state, st.error_kind = "ok", ""
    st.message = "；".join([note, *result.messages] if note else result.messages)[:1000]
    st.capabilities = sorted(adapter.capabilities())
    facts = dict(result.facts)
    sel = facts.pop("sel_entries", None)
    if isinstance(sel, list):
        # 4.4 P20c：BMC 日志逐条进服务器告警，不存进采集状态
        if sel_capture is not None:
            sel_capture.extend(sel)
        else:
            server_events.record(db, asset.id, protocol, sel, now)
        facts["sel_read_at"] = now.isoformat()
    tables = facts.pop("mac_tables", None)
    if isinstance(tables, dict):
        # MAC / ARP 表单独存表（4.2 P14），不放进采集状态，避免每次都写一大段 JSON
        from app.services.locate import record

        record(db, asset.id, tables, now)
        facts["mac_tables_at"] = now.isoformat()
    elif st.facts and st.facts.get("mac_tables_at"):
        facts["mac_tables_at"] = st.facts["mac_tables_at"]
    st.facts = facts
    if isinstance(result.facts.get("virtualization"), dict):
        from app.virtualization import ingest
        ingest(db, ep, result.facts["virtualization"], now)
    st.last_success_at, st.consecutive_failures = now, 0
    st.duration_ms = int((time.perf_counter() - t0) * 1000)
    return True


def collect_asset(
    db: Session, cipher: CredentialCipher, asset_id: uuid.UUID, adapters: dict[str, Adapter], *, now: datetime | None = None,
    force: bool = False,
) -> AssetRun:
    now = now or datetime.now(UTC)
    asset = db.get(Asset, asset_id)
    if asset is None or asset.deleted_at is not None:  # 排队后被移入回收站的不再采集
        return AssetRun(asset_id, None)
    profile = resolve_profile(asset)
    run = AssetRun(asset_id, profile.name if profile else None)
    if profile is None:
        return run
    primaries = [s for s in profile.steps if s.mode == "primary"]
    fallbacks = [s for s in profile.steps if s.mode == "fallback"]
    supplements = [s for s in profile.steps if s.mode == "supplement"]

    # 模板方式下，只有 IPMI、没有 Redfish 的服务器每 10 个周期由 IPMI 读一次 BMC 日志（4.4 P20c）
    legacy_sel = int(now.timestamp()) // profile.interval % plans.SLOW_EVERY == 0 and not _endpoints_for(asset, "redfish")

    def attempt(protocol: str, note: str = "", *, cooldown: bool = False, every: int = 1, capture: list[Point] | None = None,
                options: dict[str, Any] | None = None, sel_capture: list[dict[str, Any]] | None = None, limited: bool = True) -> bool | None:
        adapter = adapters.get(protocol)
        if options is None and protocol == "ipmi" and legacy_sel:
            options = {"sel": True}
        eps = _endpoints_for(asset, protocol)
        if adapter is None or not eps:
            return None  # 没有该协议的端点或适配器：不算失败
        ok_any = False
        ran = deferred_here = False
        for ep in eps:
            previous = db.get(CollectionStatus, ep.id)
            next_try = retry_after(previous, profile.interval) if previous and cooldown and not force else None
            if next_try is not None and now < next_try:
                run.cooling_down.append(protocol)
                continue
            # 降频的补充步骤按实际间隔记录指标周期，避免两次执行之间被判为过期
            ok = _run_endpoint(db, cipher, adapter, asset, ep, protocol, interval=profile.interval * every, note=note, now=now,
                               capture=capture, options=options, sel_capture=sel_capture,
                               # 页面上点“立即采集”（force）与“获取资料”一样：计入次数，但不受每分钟上限
                               limited=limited and not force)
            if ok is None:
                run.deferred.append(protocol)
                deferred_here = True
                continue
            ran = True
            if ok:
                run.ok.append(protocol)
            elif _status(db, ep, protocol).state == "unconfigured":
                run.unconfigured.append(protocol)
            else:
                run.failed.append(protocol)
            ok_any = ok_any or ok
        return None if deferred_here and not ran else ok_any

    # 4.4 P20b：同一台 BMC 同一时刻只让一个操作访问；控制或“获取资料”正在进行时整台本轮顺延
    bmc_addresses = [e.address for e in asset.endpoints if e.protocol in bmc_gate.BMC_PROTOCOLS and e.purpose != "control"]
    try:
        with bmc_gate.hold(db.get_bind(), bmc_addresses, "collect", wait=bmc_gate.COLLECT_WAIT):
            _collect_steps(db, asset, profile, adapters, attempt, run, primaries, fallbacks, supplements, now=now)
    except bmc_gate.BmcBusy as exc:
        run.deferred_reason = f"{exc}，本轮采集顺延"
    return run


def _collect_steps(db: Session, asset: Asset, profile: Profile, adapters: dict[str, Adapter], attempt: Callable[..., bool | None],
                   run: AssetRun, primaries: list, fallbacks: list, supplements: list, *, now: datetime) -> None:
    # 计划为空（探测时所有协议都不可用）时按模板执行：保留冷却、降级和失败显示，每小时再探测
    if plans.applies(asset, profile.name) and _collect_planned(db, asset, profile, adapters, attempt, run, now=now):
        return

    primary_ok: bool | None = None  # None：根本没有首选协议的端点
    tried: list[str] = []
    postponed = False
    for step in primaries:
        r = attempt(step.protocol, cooldown=True)
        if r is None:
            if step.protocol in run.deferred:
                postponed = True  # 首选超过访问上限：本轮顺延，不改用备用协议
                break
            continue
        tried.append(step.protocol)
        primary_ok = r
        if r:
            break
    for step in fallbacks:
        if postponed:
            break
        if primary_ok:
            for ep in _endpoints_for(asset, step.protocol):
                st = _status(db, ep, step.protocol)
                st.state, st.message, st.last_attempt_at = "skipped", "首选协议可用，未使用备用协议", now
                run.skipped.append(step.protocol)
            continue
        if primary_ok is None:
            r = attempt(step.protocol)  # 只有备用协议的端点：它就是唯一来源，不算降级
        else:
            labels = "、".join(PROTOCOL_LABEL.get(x, x) for x in tried)
            r = attempt(step.protocol, note=f"降级：{labels} 不可用，改用 {PROTOCOL_LABEL.get(step.protocol, step.protocol)}")
            run.degraded = bool(r)
        if r:
            break
    slot = int(now.timestamp()) // profile.interval
    for step in supplements:
        # 降频只在本周期已有其他来源时生效；它是唯一来源时每个周期照常采集
        reduced = step.every > 1 and bool(run.ok)
        if reduced and slot % step.every:
            continue
        attempt(step.protocol, every=step.every if reduced else 1)


def _collect_planned(db: Session, asset: Asset, profile: Profile, adapters: dict[str, Adapter], attempt: Callable[..., bool | None],
                     run: AssetRun, *, now: datetime) -> bool:
    """按采集计划执行（4.4 P20a）：需要时先探测，再让每项到期数据只由它的主来源（失败时由兜底）提供。

    返回 False 表示计划里没有任何可用数据项，本周期改按模板执行。
    """
    protocols = [p for p in plans.BMC_PROTOCOLS if p in adapters and _endpoints_for(asset, p)]
    row = db.get(CollectionPlan, asset.id)
    reason = plans.needs_probe(row, asset, now)
    if not reason and row is not None and not row.items:
        return False
    captured: dict[str, list[Point]] = {}
    sels: dict[str, list[dict[str, Any]]] = {}
    if reason:
        probed: dict[str, list[str] | None] = {}
        for p in protocols:
            cap: list[Point] = []
            sels[p] = []
            # 探测每小时最多一次，必须读到各协议：不受每分钟访问上限限制（仍计入次数）
            ok = attempt(p, capture=cap, options={"sel": True} if p == "ipmi" else {}, sel_capture=sels[p], limited=False)
            captured[p] = cap
            # 失败或缺凭据记为 None：之后每小时重新探测一次，恢复后再纳入计划
            probed[p] = plans.items_seen(cap) if ok else None
        if row is None:
            row = CollectionPlan(asset_id=asset.id, items={}, probed={}, signature="", probed_at=now, reprobe=False, check={})
            db.add(row)
        # 4.4 P20e：双来源核对固定的项在自动探测中保留；页面要求重新探测时清除，重新核对
        pins = {} if row.reprobe else dict((row.check or {}).get("pinned") or {})
        if row.reprobe:
            row.check = {}
        row.items = plans.apply_pins(plans.build({k: v or [] for k, v in probed.items()}), pins)
        row.probed, row.signature = probed, plans.signature(asset)
        row.probed_at, row.reprobe = now, False
        plans.cross_check(row, {p: cap for p, cap in captured.items() if probed.get(p)}, now)
        run.probed = reason
        owner = {item: order[0] for item, order in row.items.items()}
        notes: dict[tuple[str, str], str] = {}
        slow_due = True
    else:
        slot = int(now.timestamp()) // profile.interval
        due = plans.due_items(row.items, slot)
        slow_due = slot % plans.SLOW_EVERY == 0
        owner = {item: row.items[item][0] for item in due}
        notes = {}
        failed: set[str] = set()
        while True:
            wanted = {p for p in owner.values() if p not in captured and p not in failed}
            if not wanted:
                break
            p = next(x for x in plans.BMC_PROTOCOLS if x in wanted)
            cap = []
            sels[p] = []
            # IPMI 只在它负责 BMC 日志、且到了低频周期时才读 SEL
            opts = {"sel": True} if p == "ipmi" and owner.get("sel") == "ipmi" else {}
            r = attempt(p, cooldown=True, capture=cap, options=opts, sel_capture=sels[p])
            if r:
                captured[p] = cap
                continue
            if r is None and p in run.deferred:
                # 超过访问上限：它负责的数据本轮不采，不交给兜底，也不算降级
                for item, src in list(owner.items()):
                    if src == p:
                        del owner[item]
                continue
            failed.add(p)
            # 主来源失败：它负责的数据交给计划里的下一个可用协议
            for item, src in list(owner.items()):
                if src != p:
                    continue
                nxt = next((q for q in row.items[item] if q not in failed and q != p), None)
                if nxt is None:
                    del owner[item]
                    continue
                owner[item] = nxt
                notes[(nxt, item)] = f"降级：{PROTOCOL_LABEL.get(p, p)} 不可用，{plans.ITEMS[item].label}改用 {PROTOCOL_LABEL.get(nxt, nxt)}"
                run.degraded = True
        # 4.4 P20e：每天一次，用温度 / 功耗的另一个协议各读一次比对（只比对，不写入它的指标）
        if plans.check_due(row, now):
            extra: dict[str, list[Point]] = {}
            for item in plans.CHECK_ITEMS:
                order = row.items.get(item) or []
                if item not in owner or len(order) < 2 or order[0] not in captured:
                    continue
                other = next((q for q in order[1:] if q not in failed), None)
                if other is None or other in captured or other in extra:
                    continue
                cap = []
                if attempt(other, cooldown=True, capture=cap):
                    extra[other] = cap
            if extra:
                plans.cross_check(row, {**captured, **extra}, now)
        # 探测成功、但计划里没有由它负责数据的协议：不连接，状态里说明原因（探测时失败的保留失败状态，等每小时重试）
        idle = [p for p in protocols if isinstance(row.probed.get(p), list) and not any(p in order for order in row.items.values())]
        for p in idle:
            for ep in _endpoints_for(asset, p):
                st = _status(db, ep, p)
                st.state, st.last_attempt_at = "skipped", now
                st.message = "探测时它读不到服务器温度、风扇、健康等数据，采集计划不使用它"
            run.skipped.append(p)
    # BMC 日志只存负责它的协议读到的条目，避免同一事件从两个协议各存一份
    sel_owner = owner.get("sel") or (row.items.get("sel") or [None])[0]
    if sel_owner in sels and sel_owner in captured:
        server_events.record(db, asset.id, sel_owner, sels[sel_owner], now)
    for p, pts in captured.items():
        fast: list[Point] = []
        slow: list[Point] = []
        for pt in pts:
            item = plans.item_of(pt.key)
            if item is not None and owner.get(item) != p:
                continue
            note = notes.get((p, item), "") if item else ""
            if note:
                pt.message = f"{note}；{pt.message}" if pt.message else note
            (slow if item and plans.ITEMS[item].slow else fast).append(pt)
        if fast:
            metrics.write_sample(db, asset.id, p, fast, observed_at=now, interval_seconds=profile.interval)
        if slow and slow_due:
            metrics.write_sample(db, asset.id, p, slow, observed_at=now, interval_seconds=profile.interval * plans.SLOW_EVERY)
    return True


# ---- 计划与任务处理 ----


def enqueue_due(db: Session, *, now: datetime | None = None) -> int:
    """为在用的、有采集模板的资产按采集周期入队；同一周期用幂等键去重。"""
    from app.collect import boost

    now = now or datetime.now(UTC)
    count = 0
    boosted = boost.active(db, now)
    for asset in db.scalars(select(Asset).where(Asset.lifecycle == "active")):
        profile = resolve_profile(asset)
        if profile is None:
            continue
        if asset.id in boosted and profile.interval > boost.BOOST_INTERVAL:
            # 4.4 P20d：临时提频，每 15 秒一次（代替按模板周期）
            key = f"collect:{asset.id}:boost:{int(now.timestamp()) // boost.BOOST_INTERVAL}"
        else:
            key = f"collect:{asset.id}:{int(now.timestamp()) // profile.interval}"
        if db.scalar(select(Task.id).where(Task.idempotency_key == key)) is not None:
            continue
        queue.enqueue(db, "collect.asset", asset_id=asset.id, payload={"asset_id": str(asset.id)}, idempotency_key=key, max_attempts=1)
        count += 1
    return count


def prune_tasks(db: Session, *, now: datetime | None = None) -> int:
    """采集任务每分钟一条，成功的保留 1 天、失败的保留 7 天，避免任务表无限增长。"""
    now = now or datetime.now(UTC)
    a = db.execute(delete(Task).where(Task.kind == "collect.asset", Task.status == "succeeded", Task.finished_at < now - timedelta(days=1))).rowcount
    b = db.execute(delete(Task).where(Task.kind == "collect.asset", Task.status.in_(("failed", "cancelled")), Task.finished_at < now - timedelta(days=7))).rowcount
    return (a or 0) + (b or 0)


def make_handler(cipher_factory: Callable[[], CredentialCipher], adapters: dict[str, Adapter]):  # type: ignore[no-untyped-def]
    from app.worker import TaskContext

    def handle(ctx: TaskContext) -> dict[str, Any]:
        cipher = cipher_factory()
        with ctx.session_factory() as db:
            run = collect_asset(db, cipher, uuid.UUID(ctx.lease.payload["asset_id"]), adapters,
                                force=bool(ctx.lease.payload.get("force", False)))
            db.commit()
        return run.as_dict()

    return handle
