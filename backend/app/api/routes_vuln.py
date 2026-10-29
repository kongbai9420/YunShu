"""漏洞情报（4.3 P18c）：数据源、订阅、设置、离线包、漏洞列表。配置只有管理员可改，查看三种角色都可以。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import tempfile
import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import audit, vulnmatch
from app.api.deps import admin_only, editor, get_db, reader
from app.models.vulnmatch import TRIAGE
from app.scope import require_asset, scope_for
from app.models import Asset, Credential, User, VulnMatch, VulnRecord, VulnSighting, VulnSource, VulnSubscription
from app.services.credentials import create_credential, set_secret
from app.vulndb import bundle, cn, remediation, subscriptions, sync
from app.vulndb.normalize import SEVERITIES, SEVERITY_TEXT, SOURCE_LABEL, upsert

router = APIRouter(prefix="/api/vuln", tags=["vuln"])
MAX_BUNDLE = 1024 ** 3


def _dt(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


# ---- 设置 ----


class SettingsIn(BaseModel):
    online_enabled: bool
    aggregation_minutes: int = Field(ge=0, le=1440)
    notify_enabled: bool | None = None
    notify_channel_ids: list[uuid.UUID] | None = Field(None, max_length=50)
    digest_hour: int | None = Field(None, ge=0, le=23)
    early_warning: bool | None = None
    nvd_api_key: str | None = Field(None, max_length=200)
    revision: int


def _settings_out(db: Session) -> dict:
    s = sync.settings_row(db)
    return {"online_enabled": s.online_enabled, "aggregation_minutes": s.aggregation_minutes, "has_nvd_key": s.nvd_credential_id is not None,
            "webhook_enabled": bool(s.webhook_token_hash), "last_bundle_at": _dt(s.last_bundle_at), "last_bundle_info": s.last_bundle_info or {},
            "notify_enabled": s.notify_enabled, "notify_channel_ids": s.notify_channel_ids or [], "digest_hour": s.digest_hour,
            "early_warning": s.early_warning, "last_digest_at": _dt(s.last_digest_at), "revision": s.revision}


@router.get("/settings")
def get_settings(_: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    return _settings_out(db)


@router.put("/settings")
def put_settings(body: SettingsIn, request: Request, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    s = sync.settings_row(db)
    if body.revision != s.revision:
        raise HTTPException(409, "设置已被其他人修改，请刷新后再改")
    changed = []
    if s.online_enabled != body.online_enabled:
        changed.append("外网同步" + ("开启" if body.online_enabled else "关闭"))
    s.online_enabled, s.aggregation_minutes = body.online_enabled, body.aggregation_minutes
    if body.notify_enabled is not None:
        s.notify_enabled = body.notify_enabled
    if body.digest_hour is not None:
        s.digest_hour = body.digest_hour
    if body.early_warning is not None:
        s.early_warning = body.early_warning
    if body.notify_channel_ids is not None:
        from app.models import NotificationChannel

        for cid in body.notify_channel_ids:
            if db.get(NotificationChannel, cid) is None:
                raise HTTPException(422, "选择的通知渠道不存在")
        s.notify_channel_ids = [str(c) for c in body.notify_channel_ids]
    if body.nvd_api_key is not None:
        key = body.nvd_api_key.strip()
        cred = db.get(Credential, s.nvd_credential_id) if s.nvd_credential_id else None
        if not key and cred is not None:
            s.nvd_credential_id = None
            db.delete(cred)
            changed.append("清除 NVD 密钥")
        elif key and cred is None:
            cred = create_credential(db, request.app.state.cipher, alias=f"NVD API 密钥-{uuid.uuid4()}", kind="token", secret=key, notes="由漏洞情报设置管理")
            db.flush()
            s.nvd_credential_id = cred.id
            changed.append("设置 NVD 密钥")
        elif key and cred is not None:
            set_secret(cred, request.app.state.cipher, key)
            changed.append("更换 NVD 密钥")
    s.revision += 1
    audit.record(db, actor, "vuln.settings.update", target_type="vuln_settings", target_id="global", summary="；".join(changed) or "修改漏洞情报设置")
    return _settings_out(db)


# ---- 数据源 ----


def _source_out(row: VulnSource) -> dict:
    d = sync.SOURCES[row.name]
    return {"name": row.name, "label": d.label, "group": d.group, "description": d.description, "offline": d.offline,
            "enabled": row.enabled, "interval_minutes": row.interval_minutes, "state": row.state, "message": row.message,
            "items": row.items, "new_items": row.new_items, "last_run_at": _dt(row.last_run_at), "last_ok_at": _dt(row.last_ok_at),
            "feeds": (row.config or {}).get("feeds", []) if row.name == "rss" else None}


@router.get("/sources")
def list_sources(_: User = Depends(reader), db: Session = Depends(get_db)) -> list[dict]:
    return [_source_out(r) for r in sync.all_rows(db)]


@router.get("/sources-info")
def sources_info(_: User = Depends(reader)) -> dict:
    return {"via_webhook": list(sync.VIA_WEBHOOK)}


class Feed(BaseModel):
    name: str = Field("", max_length=60)
    url: str = Field(min_length=8, max_length=500)


class SourceIn(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = Field(None, ge=5, le=10080)
    feeds: list[Feed] | None = Field(None, max_length=30)


@router.patch("/sources/{name}")
def update_source(name: str, body: SourceIn, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    if name not in sync.SOURCES:
        raise HTTPException(404, "没有这个数据源")
    row = sync.source_row(db, name)
    if body.feeds is not None:
        if name != "rss":
            raise HTTPException(422, "只有“厂商公告（RSS）”可以填写地址")
        for f in body.feeds:
            if urlsplit(f.url).scheme not in ("http", "https"):
                raise HTTPException(422, f"地址格式不正确：{f.url}")
        row.config = {**(row.config or {}), "feeds": [f.model_dump() for f in body.feeds]}
    for k, v in body.model_dump(exclude_unset=True, exclude={"feeds"}).items():
        if v is not None:
            setattr(row, k, v)
    audit.record(db, actor, "vuln.source.update", target_type="vuln_source", target_id=name,
                 summary=f"数据源 {sync.SOURCES[name].label}：{'启用' if row.enabled else '停用'}，每 {row.interval_minutes} 分钟")
    return _source_out(row)


@router.post("/sources/{name}/sync", status_code=status.HTTP_202_ACCEPTED)
def sync_now(name: str, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    if name not in sync.SOURCES:
        raise HTTPException(404, "没有这个数据源")
    if not sync.settings_row(db).online_enabled:
        raise HTTPException(422, "外网同步未开启；不能上网的环境请导入离线包")
    queued = sync.enqueue(db, name, manual=True)
    audit.record(db, actor, "vuln.source.sync", target_type="vuln_source", target_id=name, summary=f"手动同步 {sync.SOURCES[name].label}")
    return {"queued": queued}


# ---- 订阅 ----


class SubscriptionIn(BaseModel):
    kind: Literal["product", "keyword"]
    value: str = Field(min_length=2, max_length=200)
    label: str = Field("", max_length=200)
    min_severity: Literal["critical", "high", "medium", "low", "unknown"] = "unknown"
    kev_only: bool = False


class SubscriptionUpdate(BaseModel):
    enabled: bool | None = None
    min_severity: Literal["critical", "high", "medium", "low", "unknown"] | None = None
    kev_only: bool | None = None


def _sub_out(s: VulnSubscription) -> dict:
    return {"id": str(s.id), "kind": s.kind, "value": s.value, "label": s.label, "min_severity": s.min_severity, "kev_only": s.kev_only,
            "auto": s.auto, "enabled": s.enabled, "created_by": s.created_by, "created_at": _dt(s.created_at)}


@router.get("/subscriptions")
def list_subscriptions(_: User = Depends(reader), db: Session = Depends(get_db)) -> list[dict]:
    subscriptions.sync_auto(db)
    rows = db.scalars(select(VulnSubscription).order_by(VulnSubscription.auto.desc(), VulnSubscription.kind, VulnSubscription.value))
    return [_sub_out(s) for s in rows]


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
def add_subscription(body: SubscriptionIn, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    value = body.value.strip().lower() if body.kind == "product" else body.value.strip()
    if body.kind == "product":
        value = value.removeprefix("cpe:2.3:")
        parts = value.split(":")
        if len(parts) == 3 and parts[0] in ("a", "o", "h"):
            value = ":".join(parts[1:])
        if value.count(":") != 1 or not all(parts and p for p in value.split(":")):
            raise HTTPException(422, "产品格式为“厂商:产品”，与 NVD 的 CPE 一致，例如 openbsd:openssh")
    if db.scalar(select(VulnSubscription.id).where(VulnSubscription.kind == body.kind, VulnSubscription.value == value)) is not None:
        raise HTTPException(409, "已经订阅过")
    s = VulnSubscription(kind=body.kind, value=value, label=body.label.strip() or value, min_severity=body.min_severity, kev_only=body.kev_only,
                         auto=False, enabled=True, created_by=actor.username)
    db.add(s)
    db.flush()
    audit.record(db, actor, "vuln.subscription.create", target_type="vuln_subscription", target_id=s.id, summary=f"订阅 {s.kind} {s.value}")
    return _sub_out(s)


@router.patch("/subscriptions/{sub_id}")
def update_subscription(sub_id: uuid.UUID, body: SubscriptionUpdate, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    s = db.get(VulnSubscription, sub_id)
    if s is None:
        raise HTTPException(404, "订阅不存在")
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(s, k, v)
    audit.record(db, actor, "vuln.subscription.update", target_type="vuln_subscription", target_id=s.id, summary=f"修改订阅 {s.value}")
    return _sub_out(s)


@router.delete("/subscriptions/{sub_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_subscription(sub_id: uuid.UUID, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> None:
    s = db.get(VulnSubscription, sub_id)
    if s is None:
        raise HTTPException(404, "订阅不存在")
    if s.auto:
        raise HTTPException(422, "自动订阅随软件清单生成，不能删除；不需要时可以停用")
    audit.record(db, actor, "vuln.subscription.delete", target_type="vuln_subscription", target_id=s.id, summary=f"取消订阅 {s.value}")
    db.delete(s)


# ---- 离线包 ----


@router.post("/bundle")
def import_bundle(file: UploadFile = File(...), actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    with tempfile.SpooledTemporaryFile(max_size=64 * 1024 ** 2) as tmp:
        size = 0
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > MAX_BUNDLE:
                raise HTTPException(413, "离线包超过 1 GB")
            tmp.write(chunk)
        tmp.seek(0)
        try:
            result = bundle.import_bundle(db, tmp)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    s = sync.settings_row(db)
    s.last_bundle_at, s.last_bundle_info = datetime.now(UTC), result
    sync.enqueue_match(db, reason="bundle")
    audit.record(db, actor, "vuln.bundle.import", target_type="vuln_settings", target_id="global",
                 summary=f"导入离线包（生成于 {result.get('created_at')}），新漏洞 {result['new']} 个")
    return result


# ---- 漏洞列表（P18e 的页面在此基础上加影响范围）----


def _record_out(r: VulnRecord, srcs: list[str], local: int = 0) -> dict:
    return {"id": r.id, "local_assets": local, "title": r.title, "severity": r.severity, "severity_text": SEVERITY_TEXT.get(r.severity, r.severity),
            "cvss": r.cvss, "kev": r.kev, "kev_added": r.kev_added, "epss": r.epss, "epss_percentile": r.epss_percentile,
            "published": _dt(r.published), "first_seen_at": _dt(r.first_seen_at),
            "sources": [{"name": s, "label": SOURCE_LABEL.get(s, s)} for s in srcs]}


@router.get("/records")
def list_records(q: str = Query("", max_length=100), severity: str = Query("", max_length=16), kev: bool = False,
                 source: str = Query("", max_length=32), local: bool = False, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                 user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    impact = vulnmatch.summary_for_assets(db, _visible(db, user))
    stmt = select(VulnRecord)
    if local:
        stmt = stmt.where(VulnRecord.id.in_(list(impact) or [""]))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(VulnRecord.id.ilike(like), VulnRecord.title.ilike(like)))
    if severity in SEVERITIES:
        stmt = stmt.where(VulnRecord.severity == severity)
    if kev:
        stmt = stmt.where(VulnRecord.kev.is_(True))
    if source:
        stmt = stmt.where(VulnRecord.id.in_(select(VulnSighting.record_id).where(VulnSighting.source == source)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = list(db.scalars(stmt.order_by(VulnRecord.first_seen_at.desc(), VulnRecord.id).limit(limit).offset(offset)))
    srcs: dict[str, list[str]] = {}
    for rid, s in db.execute(select(VulnSighting.record_id, VulnSighting.source).where(VulnSighting.record_id.in_([r.id for r in rows]))
                             .order_by(VulnSighting.seen_at, VulnSighting.source)):
        srcs.setdefault(rid, []).append(s)
    return {"total": total, "items": [_record_out(r, srcs.get(r.id, []), impact.get(r.id, 0)) for r in rows]}


@router.get("/records/{record_id}")
def get_record(record_id: str, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    r = db.get(VulnRecord, record_id)
    if r is None:
        raise HTTPException(404, "漏洞不存在")
    sightings = db.scalars(select(VulnSighting).where(VulnSighting.record_id == r.id).order_by(VulnSighting.seen_at))
    out = _record_out(r, [])
    out |= {"summary": r.summary, "cvss_vector": r.cvss_vector, "cvss_source": r.cvss_source, "aliases": r.aliases, "references": r.references,
            "cwe": r.cwe, "kev_ransomware": r.kev_ransomware, "kev_due": r.kev_due, "modified": _dt(r.modified),
            "sources": [{"name": s.source, "label": SOURCE_LABEL.get(s.source, s.source), "native_id": s.native_id, "title": s.title, "url": s.url,
                         "severity": s.severity, "published": _dt(s.published), "seen_at": _dt(s.seen_at)} for s in sightings]}
    out["matches"] = _matches(db, user, record_id=r.id)
    out["local_assets"] = len({m["asset_id"] for m in out["matches"] if m["active"] and m["status"] not in ("not_affected", "false_positive")})
    out["remediation"] = remediation.build(r, out["matches"])
    return out



# ---- 外部推送（watchvuln / 通用 Webhook）----


@router.post("/webhook-token")
def create_webhook_token(actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    """生成新的接收令牌（旧的立即作废）。令牌只在这里显示一次，库里只存哈希。"""
    token = "ysw_" + secrets.token_urlsafe(32)
    s = sync.settings_row(db)
    s.webhook_token_hash = hashlib.sha256(token.encode()).hexdigest()
    audit.record(db, actor, "vuln.webhook.token", target_type="vuln_settings", target_id="global", summary="生成漏洞推送接收令牌")
    return {"token": token, "path": f"/api/vuln/webhook/{token}"}


@router.delete("/webhook-token", status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook_token(actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> None:
    sync.settings_row(db).webhook_token_hash = ""
    audit.record(db, actor, "vuln.webhook.token", target_type="vuln_settings", target_id="global", summary="关闭漏洞推送接收")


@router.post("/webhook/{token}")
async def receive_webhook(token: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """不需要登录：凭路径里的令牌接收推送。只写漏洞情报，不触发任何设备操作。"""
    expected = sync.settings_row(db).webhook_token_hash
    if not expected or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected):
        raise HTTPException(404, "Not Found")
    raw = await request.body()
    if len(raw) > 256 * 1024:
        raise HTTPException(413, "内容过大")
    try:
        import json

        body: Any = json.loads(raw)
        rec = cn.parse_webhook(body) if isinstance(body, dict) else None
        if rec is None and not (isinstance(body, dict) and str(body.get("type", "")).startswith("watchvuln-")):
            raise ValueError("格式不正确")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if rec is None:
        return {"accepted": False, "ignored": True}
    new = upsert(db, [rec])
    sync.enqueue_match(db, reason="webhook", notify=True)  # 推送一到就比对并检查通知
    return {"accepted": True, "id": rec.key, "new": bool(new)}


# ---- 比对结果与处置（4.3 P18d）----

STATUS_TEXT = {"open": "待处理", "affected": "确认受影响", "not_affected": "不受影响", "mitigated": "已缓解", "accepted": "接受风险", "false_positive": "误报"}
assert set(STATUS_TEXT) == set(TRIAGE)


def _visible(db: Session, user: User) -> set[uuid.UUID] | None:
    # 全部可见时也返回集合：回收站里的资产不计入影响范围、不列出比对结果
    return set(db.scalars(scope_for(db, user).assets()))


def _match_out(m: VulnMatch, asset: Asset | None, rec: VulnRecord | None = None) -> dict:
    out = {"id": str(m.id), "asset_id": str(m.asset_id), "asset_name": asset.name if asset else "", "record_id": m.record_id, "product": m.product,
           "label": m.label, "installed_version": m.installed_version, "fixed_version": m.fixed_version, "confidence": m.confidence, "note": m.note,
           "active": m.active, "first_seen_at": _dt(m.first_seen_at), "resolved_at": _dt(m.resolved_at), "status": m.status,
           "status_text": STATUS_TEXT.get(m.status, m.status), "reason": m.reason, "review_at": m.review_at.isoformat() if m.review_at else None,
           "status_by": m.status_by, "status_at": _dt(m.status_at)}
    if rec is not None:
        out |= {"title": rec.title, "severity": rec.severity, "severity_text": SEVERITY_TEXT.get(rec.severity, rec.severity), "cvss": rec.cvss, "kev": rec.kev}
    return out


def _matches(db: Session, user: User, *, record_id: str | None = None, asset_id: uuid.UUID | None = None, active: bool | None = None) -> list[dict]:
    visible = _visible(db, user)
    stmt = select(VulnMatch, Asset, VulnRecord).join(Asset, Asset.id == VulnMatch.asset_id).join(VulnRecord, VulnRecord.id == VulnMatch.record_id)
    if record_id:
        stmt = stmt.where(VulnMatch.record_id == record_id)
    if asset_id:
        stmt = stmt.where(VulnMatch.asset_id == asset_id)
    if active is not None:
        stmt = stmt.where(VulnMatch.active.is_(active))
    rows = db.execute(stmt.order_by(VulnMatch.active.desc(), VulnRecord.kev.desc(), VulnRecord.cvss.desc().nulls_last(), Asset.name)).all()
    return [_match_out(m, a, r) for m, a, r in rows if visible is None or m.asset_id in visible]


@router.get("/matches")
def list_matches(asset_id: uuid.UUID | None = None, record_id: str | None = Query(None, max_length=80), active: bool | None = True,
                 user: User = Depends(reader), db: Session = Depends(get_db)) -> list[dict]:
    if asset_id is not None:
        require_asset(db, scope_for(db, user), asset_id)
    return _matches(db, user, record_id=record_id, asset_id=asset_id, active=active)


class TriageIn(BaseModel):
    status: Literal["open", "affected", "not_affected", "mitigated", "accepted", "false_positive"]
    reason: str = Field("", max_length=2000)
    review_at: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")


@router.patch("/matches/{match_id}")
def triage(match_id: uuid.UUID, body: TriageIn, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    m = db.get(VulnMatch, match_id)
    if m is None:
        raise HTTPException(404, "比对结果不存在")
    if body.status not in ("open", "affected") and not body.reason.strip():
        raise HTTPException(422, "标为不受影响、已缓解、接受风险或误报时必须写明理由")
    if body.status in ("accepted", "mitigated") and not body.review_at:
        raise HTTPException(422, "接受风险或已缓解需要设置复查日期")
    review = vulnmatch.review_date(body.review_at)
    if review is not None and review <= datetime.now(UTC).date():
        raise HTTPException(422, "复查日期需要晚于今天")
    before = m.status
    m.status, m.reason, m.review_at = body.status, body.reason.strip(), review
    m.status_by, m.status_at = actor.username, datetime.now(UTC)
    asset = db.get(Asset, m.asset_id)
    audit.record(db, actor, "vuln.match.triage", target_type="vuln_match", target_id=m.id,
                 summary=f"{m.record_id} @ {asset.name if asset else m.asset_id}：{STATUS_TEXT[before]} → {STATUS_TEXT[m.status]}",
                 detail={"reason": m.reason, "review_at": body.review_at})
    vulnmatch.write_metrics(db, [m.asset_id], datetime.now(UTC))
    return _match_out(m, asset, db.get(VulnRecord, m.record_id))


@router.post("/match-now", status_code=status.HTTP_202_ACCEPTED)
def match_now(actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    audit.record(db, actor, "vuln.match.run", target_type="vuln_settings", target_id="global", summary="手动重新比对")
    return {"queued": sync.enqueue_match(db, reason="manual")}



# ---- 通知与 AI 影响分析（4.3 P18e）----


@router.post("/digest-now")
def digest_now(actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    from app import vulnnotify

    audit.record(db, actor, "vuln.digest.send", target_type="vuln_settings", target_id="global", summary="手动发送漏洞每日汇总")
    return vulnnotify.digest(db, force=True)


def _require_all(db: Session, user: User) -> None:
    from app.scope import require_all

    require_all(scope_for(db, user), "AI 影响分析")  # 分析会涉及全部受影响资产


@router.get("/records/{record_id}/analysis")
def get_analysis(record_id: str, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    from app import ai_runtime, vuln_ai

    _require_all(db, user)
    enabled = ai_runtime.feature_enabled(ai_runtime.settings_row(db), vuln_ai.FEATURE)
    return {"enabled": enabled, "analysis": vuln_ai.out(vuln_ai.latest(db, record_id))}


@router.post("/records/{record_id}/analysis")
def run_analysis(record_id: str, request: Request, force: bool = False, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    from app import vuln_ai
    from app.ai.protocols import AiError

    _require_all(db, actor)
    try:
        row = vuln_ai.analyze(db, request.app.state.cipher, record_id, user_name=actor.username, force=force,
                              client=getattr(request.app.state, "ai_http_client", None))
    except AiError as exc:
        db.commit()  # 失败也保留用量记录
        raise HTTPException(status.HTTP_502_BAD_GATEWAY if exc.kind not in ("disabled", "quota", "not_found") else 422,
                            {"kind": exc.kind, "message": str(exc)}) from None
    audit.record(db, actor, "vuln.analysis.run", target_type="vuln_record", target_id=record_id, summary=f"AI 影响分析 {record_id}（{row.model}）")
    return {"enabled": True, "analysis": vuln_ai.out(row)}


@router.get("/records/{record_id}/analysis/preview")
def preview_analysis(record_id: str, user: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    from app import vuln_ai
    from app.ai.protocols import AiError

    _require_all(db, user)
    try:
        return vuln_ai.preview(db, record_id)
    except AiError as exc:
        raise HTTPException(404, str(exc)) from None
