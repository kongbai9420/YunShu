"""数据源注册、按计划同步、手动同步。外网同步总开关默认关闭；每个来源再单独开关。"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import queue
from app.crypto import CredentialCipher
from app.models import Credential, Task, VulnRecord, VulnSetting, VulnSource
from app.services.credentials import reveal_secret
from app.vulndb import cn, cve5, sources, subscriptions
from app.vulndb.normalize import NormRecord, upsert

SYNC_TASK = "vuln.sync"


@dataclass(frozen=True)
class SourceDef:
    name: str
    label: str
    group: str
    description: str
    default_enabled: bool      # 打开外网同步总开关后是否默认勾选
    interval: int              # 分钟
    offline: bool              # 能否放进离线包


SOURCES: dict[str, SourceDef] = {d.name: d for d in (
    SourceDef("cve", "CVE 官方记录", "国际官方", "CVE 项目的官方记录（约每 7 分钟更新），带厂商填写的受影响版本与修复建议，通常比 NVD 早；只保留与订阅相关的", True, 5, False),
    SourceDef("nvd", "NVD", "国际官方", "全部 CVE 的评分与受影响产品版本；按订阅的产品与关键词增量拉取", True, 60, True),
    SourceDef("kev", "CISA KEV", "国际官方", "已被真实利用的漏洞清单；没有变化时不重新下载", True, 60, True),
    SourceDef("osv", "OSV", "国际官方", "Debian、Ubuntu（含 PVE）软件包漏洞；只查本地实际安装的包", True, 1440, True),
    SourceDef("epss", "EPSS", "国际官方", "未来 30 天被利用的概率，只用于排序", True, 1440, True),
    SourceDef("ghsa", "GitHub 安全公告", "开源组件", "GitHub 审核过的开源组件漏洞（近 7 天更新的部分）；未登录每小时限 60 次请求", False, 360, False),
    SourceDef("chaitin", "长亭漏洞库", "国内", "长亭漏洞库的新漏洞；非正式开放接口，网站改版时可能失效", False, 10, False),
    SourceDef("oscs", "OSCS", "国内", "OSCS 开源安全情报预警；非正式开放接口，可能失效", False, 10, False),
    SourceDef("ti", "奇安信威胁情报", "国内", "奇安信威胁情报中心每日重点漏洞；非正式开放接口，可能失效", False, 10, False),
    SourceDef("threatbook", "微步", "国内", "微步漏洞通告中的高风险漏洞；非正式开放接口，可能失效", False, 10, False),
    SourceDef("rss", "厂商公告（RSS）", "厂商公告", "在这里填写厂商安全公告的 RSS / Atom 地址，如 Dell、Broadcom、群晖、Proxmox", False, 360, False),
)}
# 阿里云漏洞库、Seebug 有防爬验证，启明星辰、Struts2 需要解析网页：平台不内置，可部署 watchvuln 后用 Webhook 推送进来
VIA_WEBHOOK = ("阿里云漏洞库", "Seebug", "启明星辰", "Struts2 公告")


def settings_row(db: Session) -> VulnSetting:
    row = db.get(VulnSetting, "global")
    if row is None:
        row = VulnSetting(key="global", online_enabled=False, aggregation_minutes=5, webhook_token_hash="", last_bundle_info={}, revision=1)
        db.add(row)
        db.flush()
    return row


def source_row(db: Session, name: str) -> VulnSource:
    row = db.get(VulnSource, name)
    if row is None:
        d = SOURCES[name]
        row = VulnSource(name=name, enabled=d.default_enabled, interval_minutes=d.interval, state="never", message="", items=0,
                         new_items=0, cursor={}, config={})
        db.add(row)
        db.flush()
    return row


def all_rows(db: Session) -> list[VulnSource]:
    return [source_row(db, n) for n in SOURCES]


def _nvd_key(db: Session, cipher: CredentialCipher | None, settings: VulnSetting) -> str:
    if cipher is None or settings.nvd_credential_id is None:
        return ""
    cred = db.get(Credential, settings.nvd_credential_id)
    return reveal_secret(cred, cipher) if cred else ""


def run_source(db: Session, name: str, http: httpx.Client, *, cipher: CredentialCipher | None = None, now: datetime | None = None,
               sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """同步一个来源；失败写进来源状态，不抛给调用方，返回新出现的漏洞键。"""
    now = now or datetime.now(UTC)
    row = source_row(db, name)
    settings = settings_row(db)
    row.last_run_at, row.state = now, "running"
    try:
        records: list[NormRecord] = []
        cursor = dict(row.cursor or {})
        if name == "cve":
            subscriptions.sync_auto(db)
            records, cursor = cve5.fetch(http, cursor=cursor, now=now, products=set(subscriptions.products(db)),
                                         keywords=subscriptions.keywords(db))
        elif name == "nvd":
            subscriptions.sync_auto(db)
            records, cursor = sources.fetch_nvd(http, products=subscriptions.products(db), keywords=subscriptions.keywords(db),
                                                cursor=cursor, now=now, api_key=_nvd_key(db, cipher, settings), sleep=sleep)
        elif name == "kev":
            records, cursor = sources.fetch_kev(http, cursor=cursor)
        elif name == "osv":
            records, cursor = sources.fetch_osv(http, packages=subscriptions.installed_packages(db), cursor=cursor)
        elif name == "epss":
            apply_epss(db, sources.fetch_epss(http, epss_targets(db, now)))
        elif name == "ghsa":
            records, cursor = cn.fetch_ghsa(http, cursor, now)
        elif name == "chaitin":
            records = cn.fetch_chaitin(http)
        elif name == "oscs":
            records = cn.fetch_oscs(http)
        elif name == "ti":
            records = cn.fetch_ti(http)
        elif name == "threatbook":
            records = cn.fetch_threatbook(http)
        elif name == "rss":
            feeds = [f for f in (row.config or {}).get("feeds", []) if f.get("url")]
            if not feeds:
                raise sources.SourceError("还没有填写 RSS 地址")
            records = cn.fetch_rss(http, feeds)
        else:
            raise sources.SourceError(f"未实现的来源：{name}")
        new = upsert(db, records, now)
        row.cursor, row.items, row.new_items = cursor, len(records), len(new)
        row.state, row.message, row.last_ok_at = "ok", "", now
        return new
    except sources.SourceError as exc:
        row.state, row.message = "failed", str(exc)[:1000]
        return []


def epss_targets(db: Session, now: datetime, limit: int = 2000) -> list[str]:
    """只给值得排序的 CVE 补 EPSS：严重 / 高危、已被利用，或还没有 EPSS 的新记录。"""
    stmt = (select(VulnRecord.id).where(VulnRecord.id.like("CVE-%"))
            .where(or_(VulnRecord.epss.is_(None), VulnRecord.updated_at >= now - timedelta(days=2)))
            .where(or_(VulnRecord.kev.is_(True), VulnRecord.severity.in_(("critical", "high"))))
            .order_by(VulnRecord.kev.desc(), VulnRecord.first_seen_at.desc()).limit(limit))
    return list(db.scalars(stmt))


def apply_epss(db: Session, scores: dict[str, tuple[float, float]]) -> int:
    n = 0
    for cid, (epss, pct) in scores.items():
        rec = db.get(VulnRecord, cid)
        if rec is not None:
            rec.epss, rec.epss_percentile = epss, pct
            n += 1
    return n


def due(row: VulnSource, now: datetime) -> bool:
    if not row.enabled:
        return False
    last = row.last_run_at
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return now - last >= timedelta(minutes=max(row.interval_minutes, 5))


def enqueue_due(db: Session, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    if not settings_row(db).online_enabled:
        return 0
    n = 0
    for row in all_rows(db):
        if due(row, now):
            n += enqueue(db, row.name, now=now)
    return n


def enqueue(db: Session, name: str, *, now: datetime | None = None, manual: bool = False) -> int:
    now = now or datetime.now(UTC)
    slot = int(now.timestamp()) // 60
    key = f"vulnsync:{name}:{'m' if manual else 's'}:{slot}"
    if db.scalar(select(Task.id).where(Task.idempotency_key == key)) is not None:
        return 0
    queue.enqueue(db, SYNC_TASK, payload={"source": name}, idempotency_key=key, max_attempts=1, priority=queue.BACKGROUND_PRIORITY)
    return 1


def make_handler(cipher_factory: Callable[[], CredentialCipher], http_factory: Callable[[], httpx.Client] | None = None):  # type: ignore[no-untyped-def]
    def handle(ctx) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        name = ctx.lease.payload["source"]
        with ctx.session_factory() as db:
            if not settings_row(db).online_enabled or name not in SOURCES:
                return {"skipped": True}
            http = http_factory() if http_factory else httpx.Client(timeout=60, follow_redirects=True)
            try:
                new = run_source(db, name, http, cipher=cipher_factory())
            finally:
                if http_factory is None:
                    http.close()
            db.commit()
            row = db.get(VulnSource, name)
            if row is not None and row.state == "ok" and (row.items or new):
                from app import vulnmatch, vulnnotify

                vulnmatch.run(db)
                vulnnotify.enqueue_now(db)  # 拉到新数据后立即检查通知，不等下一次定时
                db.commit()
            return {"source": name, "state": row.state if row else "", "new": len(new)}

    return handle



MATCH_TASK = "vuln.match"


def enqueue_match(db: Session, *, now: datetime | None = None, reason: str = "s", notify: bool = False) -> int:
    now = now or datetime.now(UTC)
    if reason == "s":
        key = f"vulnmatch:s:{int(now.timestamp()) // 3600}"
        if db.scalar(select(Task.id).where(Task.idempotency_key == key)) is not None:
            return 0
    else:
        # 手动或导入离线包后触发：不去重，否则同一分钟内的第二次导入不会重新比对
        import uuid

        key = f"vulnmatch:{reason}:{uuid.uuid4()}"
    queue.enqueue(db, MATCH_TASK, payload={"notify": True} if notify else {}, idempotency_key=key, max_attempts=1,
                  priority=queue.BACKGROUND_PRIORITY)
    return 1


def make_match_handler():  # type: ignore[no-untyped-def]
    def handle(ctx) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        from app import vulnmatch

        with ctx.session_factory() as db:
            result = vulnmatch.run(db)
            if ctx.lease.payload.get("notify"):
                from app import vulnnotify

                vulnnotify.enqueue_now(db)
            db.commit()
            return result

    return handle
