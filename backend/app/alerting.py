"""P4 持久告警状态机；仅读取采集结果，不联网、不发送通知、不提交事务。

由单实例 Worker 每 15 秒调用。阈值判断只接受新鲜、非估算的有效样本；
同一观测只计数一次，调度次数和等待时间不能代替连续采集证据。
"""
from __future__ import annotations

import math
from fnmatch import fnmatchcase
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collect.runner import resolve_profile
from app.metrics import STALE_FACTOR
from app.server_events import SERVER_EVENT_METRIC, latest_critical, open_critical
from app.models import (
    Alert, AlertEvent, AlertRule, Asset, AssetRelation, CollectionStatus,
    LatestMetric, MaintenanceWindow, ManagementEndpoint, NetworkLink,
)

OPEN_STATES = frozenset({"pending", "active", "acknowledged", "silenced"})
ACTIVE_STATES = OPEN_STATES - {"pending"}
NETWORK_ROLES = frozenset({"switch", "router", "firewall", "ap"})
NETWORK_FAILURES = frozenset({"timeout", "unreachable"})


# 维护中超过预计结束时间：合成指标，维护中的资产也照常判断与通知，其余告警在维护中一律不产生
MAINTENANCE_METRIC = "asset.maintenance_overdue"


def _maintenance_overdue(asset: Asset, now: datetime) -> bool:
    return asset.lifecycle == "maintenance" and asset.maintenance_until is not None and _utc(asset.maintenance_until) <= now


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def matches_scope(asset: Asset, asset_ids: list[str], tags: list[str]) -> bool:
    """空选择表示全部；同时填写资产与标签时取交集，标签内部取并集。"""
    return (not asset_ids or str(asset.id) in asset_ids) and (not tags or bool(set(tags) & set(asset.tags)))


@dataclass(frozen=True)
class Evidence:
    key: str
    observed_at: datetime
    value: float | None
    unit: str
    valid: bool
    max_gap: int


def _metric_evidence(metric: LatestMetric, now: datetime) -> Evidence:
    gap = STALE_FACTOR * max(1, metric.interval_seconds)
    observed = _utc(metric.observed_at)
    valid = (metric.quality == "valid" and not metric.estimated and metric.value is not None
             and math.isfinite(metric.value) and 0 <= (now - observed).total_seconds() <= gap)
    return Evidence(metric.metric_key, observed, metric.value if valid else None, metric.unit, valid, gap)


def _availability(asset: Asset, statuses: list[CollectionStatus], endpoints: list[ManagementEndpoint], now: datetime) -> Evidence | None:
    """任一已成功端点可证明可达；只有所有采集端点的连接失败才能证明不可达。

    auth / unconfigured / protocol / untrusted 不代表断网。失败证据按采集的
    2/4/8 周期退避扩展有效期，但仍有界过期，Worker 停机不等于资产离线。
    """
    monitored = {e.id for e in endpoints if e.purpose != "control"}
    rows = [s for s in statuses if s.endpoint_id in monitored]
    if not monitored or not rows:
        return None
    profile = resolve_profile(asset)
    interval = profile.interval if profile else 60
    successes = [s for s in rows if s.state == "ok" and 0 <= (now - _utc(s.last_attempt_at)).total_seconds() <= STALE_FACTOR * interval]
    if successes:
        return Evidence("availability", max(_utc(s.last_attempt_at) for s in successes), 1, "", True, STALE_FACTOR * interval)
    observed = max(_utc(s.last_attempt_at) for s in rows)
    failures = [s for s in rows if s.state == "failed" and s.error_kind in NETWORK_FAILURES]
    gap = max((min(600, interval * 2 ** min(s.consecutive_failures, 3)) + STALE_FACTOR * interval for s in failures), default=STALE_FACTOR * interval)
    complete = {s.endpoint_id for s in failures} == monitored
    fresh = complete and all(0 <= (now - _utc(s.last_attempt_at)).total_seconds() <= gap for s in failures)
    return Evidence("availability", observed, 0 if fresh else None, "", fresh, gap)


def _metric_matches(pattern: str, key: str) -> bool:
    # 基础指标匹配实例指标（例如 disk.used_pct 匹配各挂载点）；显式实例只匹配自身。
    return fnmatchcase(key, pattern) or (":" not in pattern and key.startswith(pattern + ":"))


def _bad(rule: AlertRule, evidence: Evidence) -> bool:
    if not evidence.valid or evidence.value is None:
        return False
    if rule.kind == "availability":
        return evidence.value == 0
    return evidence.value <= rule.threshold if rule.comparison == "lte" else evidence.value >= rule.threshold


def _recovered(rule: AlertRule, evidence: Evidence) -> bool:
    if not evidence.valid or evidence.value is None:
        return False
    if rule.kind == "availability":
        return evidence.value == 1
    return evidence.value >= rule.recovery_threshold if rule.comparison == "lte" else evidence.value <= rule.recovery_threshold


def _event(db: Session, alert: Alert, kind: str, now: datetime, message: str) -> AlertEvent:
    db.flush()  # UUID 默认值由 INSERT 分配；事件必须引用已存在的告警。
    event = AlertEvent(alert_id=alert.id, kind=kind, title=alert.title, message=message[:2000],
                       value=alert.value, threshold=alert.threshold, unit=alert.unit, metric_key=alert.metric_key,
                       asset_name=alert.asset_name, severity=alert.severity, episode=alert.episode,
                       created_at=now, notify=False, routed=False)
    db.add(event)
    return event


def _close(db: Session, alert: Alert, now: datetime, reason: str) -> None:
    if alert.state not in OPEN_STATES:
        return
    alert.state, alert.resolved_at, alert.sample_count = "resolved", now, 0
    alert.suppression_reason = reason
    _event(db, alert, "closed", now, reason + "；关闭此规则实例，不代表设备或指标已经恢复")


def _upstreams(db: Session, assets: dict[uuid.UUID, Asset]) -> dict[uuid.UUID, set[uuid.UUID]]:
    graph: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for relation in db.scalars(select(AssetRelation).where(AssetRelation.kind == "depends_on")):
        graph[relation.source_asset_id].add(relation.target_asset_id)
    # 已确认物理链路是无向的，只有网络设备连接非网络设备时可以明确推断基础上游。
    for link in db.scalars(select(NetworkLink).where(NetworkLink.state == "confirmed", NetworkLink.origin.in_(("manual", "confirmed")))):
        source, target = assets.get(link.source_asset_id), assets.get(link.target_asset_id)
        if source is None or target is None:
            continue
        if source.role in NETWORK_ROLES and target.role not in NETWORK_ROLES:
            graph[target.id].add(source.id)
        elif target.role in NETWORK_ROLES and source.role not in NETWORK_ROLES:
            graph[source.id].add(target.id)
    return graph


def _dependency_reason(asset_id: uuid.UUID, graph: dict[uuid.UUID, set[uuid.UUID]], offline: set[uuid.UUID], assets: dict[uuid.UUID, Asset]) -> str:
    def leads_back(parent: uuid.UUID) -> bool:
        visited: set[uuid.UUID] = set()
        pending = list(graph.get(parent, ()))
        while pending:
            item = pending.pop()
            if item == asset_id:
                return True
            if item not in visited:
                visited.add(item)
                pending.extend(graph.get(item, ()))
        return False

    seen = {asset_id}
    queue = list(graph.get(asset_id, ()))
    while queue:
        parent = queue.pop()
        if parent in seen:
            continue
        seen.add(parent)
        # 人工依赖可能误配成环；环内成员不能互相静默到没有任何根因通知。
        if parent in offline and not leads_back(parent):
            return f"上游不可达：{assets[parent].name}"
        queue.extend(graph.get(parent, ()))
    return ""


def _boost(db: Session, alerts: dict, rules: dict, assets: dict, evidence_for: dict, windows: list, now: datetime) -> None:
    """4.4 P20d：未结束的告警与接近阈值的指标让资产临时提高采集频率；维护中、维护窗口内不提频。"""
    from app.collect import boost

    reasons: dict[uuid.UUID, str] = {}
    for alert in alerts.values():
        rule, asset = rules.get(alert.rule_id), assets.get(alert.asset_id)
        if alert.state in OPEN_STATES and rule is not None and asset is not None and rule.kind == "threshold" and not rule.metric_key.startswith(("server_event.", "inspection.")):
            reasons.setdefault(asset.id, f"告警：{rule.name}")
    for (rule_id, asset_id, _key), sample in evidence_for.items():
        rule = rules.get(rule_id)
        if asset_id in reasons or rule is None or rule.kind != "threshold" or not sample.valid or sample.value is None:
            continue
        if rule.metric_key.startswith(("server_event.", "inspection.")):
            continue
        if boost.near_threshold(rule.comparison, rule.threshold, sample.value):
            reasons[asset_id] = f"接近阈值：{rule.name}（{sample.value:g}{sample.unit}，阈值 {rule.threshold:g}）"
    for asset_id, reason in reasons.items():
        asset = assets.get(asset_id)
        if asset is None or asset.lifecycle != "active" or any(matches_scope(asset, w.asset_ids, w.tags) for w in windows):
            continue
        boost.mark(db, asset_id, reason, now)


def _owes_recovery(db: Session, alert: Alert) -> bool:
    """本周期的触发 / 重复通知真的发出去过，最后结束于“恢复”且那次恢复没有通知出去。被关闭（规则停用、移出范围）的不算。

    “发出去过”以投递记录为准：有投递且没被取消（发送失败也算，平台确实尝试过）。触发通知还在合并等待时
    维护窗口就开始了，投递会被取消、用户从没收到，这种不补发恢复，免得只收到一条没头没尾的“已恢复”。
    """
    from app.models import NotificationDelivery, NotificationIntent

    rows = db.execute(select(AlertEvent.kind, AlertEvent.notify).where(
        AlertEvent.alert_id == alert.id, AlertEvent.episode == alert.episode,
        AlertEvent.kind.in_(("trigger", "repeat", "recover", "closed"))).order_by(AlertEvent.created_at)).all()
    ends = [(kind, notify) for kind, notify in rows if kind in ("recover", "closed")]
    if not ends or ends[-1] != ("recover", False):
        return False
    # 一封投递会合并多条事件；被抑制的事件的意图也挂在这封投递上，但不在它的 event_ids 里，所以还要核对内容
    candidates = db.execute(select(NotificationIntent.event_id, NotificationDelivery.event_ids)
                            .join(NotificationDelivery, NotificationDelivery.id == NotificationIntent.delivery_id)
                            .join(AlertEvent, AlertEvent.id == NotificationIntent.event_id)
                            .where(AlertEvent.alert_id == alert.id, AlertEvent.episode == alert.episode,
                                   AlertEvent.kind.in_(("trigger", "repeat")), NotificationDelivery.status != "cancelled")).all()
    return any(str(event_id) in (event_ids or []) for event_id, event_ids in candidates)


def evaluate(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """在调用者事务中求值，产生持久事件供统一通知规划器消费；不 commit。

    单 Worker 锁由调度器保证；任何待确认计数、冷却与静默在重启后仍从库恢复。
    """
    now = _utc(now or datetime.now(UTC))
    db.flush()
    # 回收站里的资产当作不存在：它的未结束告警按“已移出规则作用范围”关闭
    assets = {a.id: a for a in db.scalars(select(Asset).where(Asset.deleted_at.is_(None)))}
    rules = {r.id: r for r in db.scalars(select(AlertRule))}
    alerts = {(a.rule_id, a.asset_id, a.metric_key): a for a in db.scalars(select(Alert))}
    metrics: dict[uuid.UUID, list[LatestMetric]] = defaultdict(list)
    statuses: dict[uuid.UUID, list[CollectionStatus]] = defaultdict(list)
    endpoints: dict[uuid.UUID, list[ManagementEndpoint]] = defaultdict(list)
    for metric in db.scalars(select(LatestMetric)):
        metrics[metric.asset_id].append(metric)
    for status in db.scalars(select(CollectionStatus)):
        statuses[status.asset_id].append(status)
    for endpoint in db.scalars(select(ManagementEndpoint)):
        endpoints[endpoint.asset_id].append(endpoint)
    windows = [w for w in db.scalars(select(MaintenanceWindow)) if _utc(w.starts_at) <= now < _utc(w.ends_at)]
    graph = _upstreams(db, assets)
    # 只需要未结束告警当前周期的通知记录；不读全部历史事件，求值耗时不随运行时间增长
    notified = {(alert_id, episode) for alert_id, episode in db.execute(
        select(AlertEvent.alert_id, AlertEvent.episode).join(Alert, Alert.id == AlertEvent.alert_id).where(
            AlertEvent.notify.is_(True), AlertEvent.kind.in_(("trigger", "repeat")), AlertEvent.episode == Alert.episode,
            Alert.state.in_(OPEN_STATES)))}
    events: list[tuple[AlertEvent, Alert, AlertRule]] = []
    silenced_recoveries: set[tuple[uuid.UUID, int]] = set()
    evidence_for: dict[tuple[uuid.UUID, uuid.UUID, str], Evidence] = {}
    result = {"evaluated": 0, "triggered": 0, "repeated": 0, "recovered": 0, "notified": 0}
    # 4.4 P20c：服务器告警里待处理的严重事件（合成证据，每次求值都是新的观测）
    server_open = open_critical(db)
    # 4.4 P21：各设备最近一次命令巡检里的严重异常
    from app.command_inspection import INSPECTION_METRIC, critical_findings

    inspection_open = critical_findings(db)
    fresh_server_events: set[int] = set()  # 事件对象的 id()：主键在写库时才分配

    for alert in list(alerts.values()):
        if alert.state == "silenced" and (alert.silenced_until is None or _utc(alert.silenced_until) <= now):
            alert.state = "acknowledged" if alert.acknowledged_by else "active"
            alert.silenced_until = None
            _event(db, alert, "unsilenced", now, "静默已到期")
        rule, asset = rules.get(alert.rule_id), assets.get(alert.asset_id)
        if rule is None or not rule.enabled or rule.deleted:
            _close(db, alert, now, "规则已停用或删除")
        elif asset is None or not matches_scope(asset, rule.asset_ids, rule.tags) or not _metric_matches(rule.metric_key, alert.metric_key):
            _close(db, alert, now, "资产或指标已移出规则作用范围")

    # 首先求全部生命周期；通知标记统一在末尾完成，不依赖规则或资产遍历顺序。
    for rule in rules.values():
        if not rule.enabled or rule.deleted:
            continue
        for asset in assets.values():
            if not matches_scope(asset, rule.asset_ids, rule.tags):
                continue
            evidence = ([_availability(asset, statuses[asset.id], endpoints[asset.id], now)] if rule.kind == "availability"
                        else [_metric_evidence(m, now) for m in metrics[asset.id] if _metric_matches(rule.metric_key, m.metric_key)])
            if (rule.kind == "threshold" and _metric_matches(rule.metric_key, SERVER_EVENT_METRIC)
                    and (asset.id in server_open or (rule.id, asset.id, SERVER_EVENT_METRIC) in alerts)):
                evidence.append(Evidence(SERVER_EVENT_METRIC, now, float(server_open.get(asset.id, (0, now))[0]), "条", True, 600))
            if (rule.kind == "threshold" and _metric_matches(rule.metric_key, MAINTENANCE_METRIC)
                    and (_maintenance_overdue(asset, now) or (rule.id, asset.id, MAINTENANCE_METRIC) in alerts)):
                evidence.append(Evidence(MAINTENANCE_METRIC, now, 1.0 if _maintenance_overdue(asset, now) else 0.0, "", True, 600))
            if (rule.kind == "threshold" and _metric_matches(rule.metric_key, INSPECTION_METRIC)
                    and (asset.id in inspection_open or (rule.id, asset.id, INSPECTION_METRIC) in alerts)):
                found = inspection_open.get(asset.id)
                evidence.append(Evidence(INSPECTION_METRIC, now, float(found.count if found else 0), "项", True, 600))
            for sample in evidence:
                if sample is None:
                    continue
                result["evaluated"] += 1
                key = (rule.id, asset.id, sample.key)
                evidence_for[key] = sample
                alert = alerts.get(key)
                if alert is None:
                    # 维护中、规划中、已退役的资产不产生新告警（进入维护前已有的告警保留并抑制通知），改回“在用”后照常判断
                    if (asset.lifecycle != "active" and sample.key != MAINTENANCE_METRIC) or not _bad(rule, sample):
                        continue
                    alert = Alert(rule_id=rule.id, asset_id=asset.id, asset_name=asset.name, metric_key=sample.key,
                                  state="pending", severity=rule.severity, title=f"{asset.name} · {rule.name}",
                                  value=sample.value, unit=sample.unit, threshold=rule.threshold, episode=1,
                                  sample_count=0, first_seen_at=sample.observed_at, last_seen_at=now,
                                  acknowledged_by="", suppression_reason="")
                    db.add(alert)
                    alerts[key] = alert
                    _event(db, alert, "pending", now, "等待连续样本及持续超限条件满足")
                alert.asset_name, alert.title, alert.severity = asset.name, f"{asset.name} · {rule.name}", rule.severity
                alert.threshold, alert.unit = rule.threshold, sample.unit
                is_new = alert.last_sample_at is None or sample.observed_at > _utc(alert.last_sample_at)
                if not sample.valid:
                    if alert.state == "pending":
                        alert.sample_count = 0
                    if is_new and sample.observed_at <= now:
                        alert.last_sample_at = sample.observed_at
                    continue  # 未知不恢复、不重复提醒，也不能靠时间越过 duration。
                if not is_new:
                    continue
                previous_at = _utc(alert.last_sample_at) if alert.last_sample_at else None
                alert.last_sample_at, alert.last_seen_at, alert.value = sample.observed_at, now, sample.value
                if _recovered(rule, sample):
                    if alert.state in OPEN_STATES:
                        was_active = alert.state in ACTIVE_STATES
                        if alert.state == "silenced":
                            silenced_recoveries.add((alert.id, alert.episode))
                        alert.state, alert.resolved_at, alert.sample_count = "resolved", now, 0
                        alert.silenced_until = None
                        if was_active:
                            event = _event(db, alert, "recover", now, "新鲜有效样本已达到恢复阈值")
                            events.append((event, alert, rule))
                            result["recovered"] += 1
                        else:
                            _event(db, alert, "cancelled", now, "在确认触发前已回到正常范围")
                    continue
                if not _bad(rule, sample):
                    if alert.state == "pending":
                        alert.sample_count = 0
                    continue  # 恢复与触发阈值之间维持已激活状态。
                if alert.state == "resolved":
                    alert.state, alert.episode, alert.sample_count = "pending", alert.episode + 1, 0
                    alert.activated_at, alert.resolved_at, alert.silenced_until = None, None, None
                    alert.acknowledged_by, alert.suppression_reason = "", ""
                    _event(db, alert, "pending", now, "新的超限周期，等待触发条件")
                if alert.state == "pending":
                    if alert.sample_count == 0 or (previous_at is not None and (sample.observed_at - previous_at).total_seconds() > sample.max_gap):
                        alert.first_seen_at, alert.sample_count = sample.observed_at, 0
                    alert.sample_count += 1
                    duration = (sample.observed_at - _utc(alert.first_seen_at)).total_seconds()
                    if alert.sample_count >= max(1, rule.consecutive_samples) and duration >= max(0, rule.duration_seconds):
                        alert.state, alert.activated_at = "active", now
                        event = _event(db, alert, "trigger", now, "连续新鲜有效样本满足触发条件")
                        events.append((event, alert, rule))
                        result["triggered"] += 1

    offline = {a.asset_id for a in alerts.values() if a.state in ACTIVE_STATES and rules[a.rule_id].kind == "availability" and a.asset_id in assets}
    existing_trigger = {a.id for event, a, _ in events if event.kind == "trigger"}
    for key, alert in alerts.items():
        asset, rule = assets.get(alert.asset_id), rules.get(alert.rule_id)
        if asset is None or rule is None or not rule.enabled or rule.deleted or not matches_scope(asset, rule.asset_ids, rule.tags):
            continue
        reason = ""
        if asset.lifecycle in {"maintenance", "planned", "retired"} and alert.metric_key != MAINTENANCE_METRIC:
            reason = f"资产处于{'维护中' if asset.lifecycle == 'maintenance' else '规划中' if asset.lifecycle == 'planned' else '已退役'}状态"
        elif any(matches_scope(asset, window.asset_ids, window.tags) for window in windows):
            reason = "资产处于维护窗口"
        elif rule.suppress_by_dependency:
            reason = _dependency_reason(asset.id, graph, offline, assets)
        # API 静默与确认状态保留，抑制额外独立记录，恢复仍由有效样本决定。
        if alert.state in ACTIVE_STATES and alert.suppression_reason != reason:
            _event(db, alert, "suppressed" if reason else "unsuppressed", now, reason or "维护或上游依赖抑制已结束")
        recovered_now = any(e.kind == "recover" and a is alert for e, a, _ in events)  # 本轮刚恢复的，下面按正常流程通知
        if alert.state == "resolved" and alert.suppression_reason and not reason and not recovered_now and _owes_recovery(db, alert):
            # 维护窗口、维护中或上游依赖抑制期间恢复的告警：抑制结束后补发恢复通知
            event = _event(db, alert, "recover", now, f"{alert.suppression_reason}期间已恢复，抑制结束后补发恢复通知")
            events.append((event, alert, rule))
            notified.add((alert.id, alert.episode))
            result["recovered"] += 1
        alert.suppression_reason = reason
        sample = evidence_for.get(key)
        acknowledged_ok = alert.state == "acknowledged" and alert.metric_key == SERVER_EVENT_METRIC
        if (alert.state != "active" and not acknowledged_ok) or reason or sample is None or not _bad(rule, sample) or alert.id in existing_trigger:
            continue
        cooldown_ok = alert.last_notified_at is None or (now - _utc(alert.last_notified_at)).total_seconds() >= rule.cooldown_seconds
        if not cooldown_ok:
            continue
        if (alert.id, alert.episode) not in notified:
            event = _event(db, alert, "trigger", now, "触发条件仍成立，冷却或抑制已结束")
            events.append((event, alert, rule))
        elif alert.metric_key == SERVER_EVENT_METRIC and alert.last_notified_at is not None and asset.id in server_open \
                and server_open[asset.id][1] > _utc(alert.last_notified_at):
            # 已经通知过，又来了新的严重事件：再提醒一次（已确认的告警也提醒，静默的不提醒）
            new = latest_critical(db, asset.id, since=_utc(alert.last_notified_at))
            event = _event(db, alert, "repeat", now, "新的严重事件：" + "；".join(new))
            events.append((event, alert, rule))
            fresh_server_events.add(id(event))
            result["repeated"] += 1
        elif alert.state == "active" and rule.repeat_seconds > 0 and alert.last_notified_at is not None and (now - _utc(alert.last_notified_at)).total_seconds() >= rule.repeat_seconds:
            event = _event(db, alert, "repeat", now, "告警仍持续，按重复提醒间隔通知")
            events.append((event, alert, rule))
            result["repeated"] += 1

    _boost(db, alerts, rules, assets, evidence_for, windows, now)
    for event, alert, rule in events:
        if alert.suppression_reason:
            continue
        if event.kind == "recover":
            event.notify = (alert.id, alert.episode) in notified and (alert.id, alert.episode) not in silenced_recoveries
        elif id(event) in fresh_server_events:
            event.notify = alert.state in ("active", "acknowledged") and (now - _utc(alert.last_notified_at)).total_seconds() >= rule.cooldown_seconds
        else:
            if event.kind == "trigger" and alert.metric_key == SERVER_EVENT_METRIC and alert.asset_id is not None:
                event.message = "待处理的严重事件：" + "；".join(latest_critical(db, alert.asset_id))
            if event.kind == "trigger" and alert.metric_key == MAINTENANCE_METRIC and alert.asset_id in assets:
                from app.inspection import _local_tz

                until = assets[alert.asset_id].maintenance_until
                event.message = (f"维护中已超过预计结束时间（{_utc(until).astimezone(_local_tz()):%Y-%m-%d %H:%M}），"
                                 "资产仍未改回“在用”：期间不采集、不告警") if until else event.message
            if event.kind == "trigger" and alert.metric_key == INSPECTION_METRIC and alert.asset_id in inspection_open:
                event.message = "最近一次巡检的严重异常：" + "、".join(inspection_open[alert.asset_id].names[:10])
            event.notify = alert.state == "active" and (alert.last_notified_at is None or (now - _utc(alert.last_notified_at)).total_seconds() >= rule.cooldown_seconds)
        if event.notify:
            alert.last_notified_at = now
            notified.add((alert.id, alert.episode))
            result["notified"] += 1
    db.flush()
    return result
