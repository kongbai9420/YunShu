"""旧五条阈值规则的单次迁入；保存后的人工设置不被启动或重导入覆盖。"""
from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import AlertRule, LegacyMapping, NotificationChannel
from app.server_events import SERVER_EVENT_METRIC

DEFAULTS = [
    ("node_cpu_temp", "硬件 CPU 高温", "cpu.temp", 80, "critical"),
    ("server_cpu", "系统 CPU 使用率", "cpu.load", 90, "warning"),
    ("server_mem", "系统内存使用率", "mem.used_pct", 90, "critical"),
    ("server_swap", "Swap 使用率", "swap.used_pct", 80, "warning"),
    ("server_disk", "根分区使用率", "disk.used_pct:/", 92, "critical"),
]


def _bool(value, default=True):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).lower() in {"true", "1", "yes", "on"}


def ensure_rules(db: Session) -> None:
    old = db.scalar(select(LegacyMapping).where(LegacyMapping.legacy_kind == "alert_settings", LegacyMapping.legacy_id == "global"))
    cfg = old.payload if old and old.target_type != "alert_rules" else None
    mappings = list(db.scalars(select(LegacyMapping).where(LegacyMapping.legacy_kind.in_(["hardware_node", "system_server"])))) if cfg else []
    for key, name, metric, threshold, severity in DEFAULTS:
        rule = db.scalar(select(AlertRule).where(AlertRule.legacy_id == key))
        if rule is None:
            rule = AlertRule(name=name, metric_key=metric, threshold=threshold, recovery_threshold=threshold - 5, severity=severity, legacy_id=key)
            db.add(rule)
        elif rule.revision != 1:
            continue
        if cfg is not None:
            try:
                value = float(cfg.get(f"{key}_threshold", threshold))
                cooldown = int(cfg.get("cooldown_sec", 60))
                if not math.isfinite(value) or not -100000 <= value <= 1000000000 or not 0 <= cooldown <= 86400:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                rule.enabled = False
                audit.record(db, None, "alert.legacy.pending", summary=f"旧告警规则 {name} 的阈值或冷却无效，已停用，需人工确认")
                continue
            rule.threshold = value
            rule.recovery_threshold = value
            rule.cooldown_seconds = cooldown
            rule.enabled = _bool(cfg.get("enabled")) and _bool(cfg.get(f"{key}_enabled"))
            kind = "hardware_node" if key == "node_cpu_temp" else "system_server"
            ids = [str(m.target_id) for m in mappings if m.legacy_kind == kind and m.target_id]
            if ids:
                rule.asset_ids = ids
    if db.scalar(select(AlertRule).where(AlertRule.legacy_id == "server_event_critical")) is None:
        # 4.4 P20c：“服务器告警”里有待处理的严重事件就告警，新的严重事件到达时再提醒，全部标记已处理后恢复。
        # 取代 4.2 P13 按 24 小时条数判断的“BMC 日志出现严重事件”（同一件事不重复通知），沿用它的作用范围与通知渠道。
        sel_rule = db.scalar(select(AlertRule).where(AlertRule.legacy_id == "bmc_sel_critical"))
        rule = AlertRule(name="服务器出现严重硬件事件", metric_key=SERVER_EVENT_METRIC, threshold=1, recovery_threshold=0,
                         severity="critical", legacy_id="server_event_critical")
        if sel_rule is None:
            db.add(AlertRule(name="BMC 日志出现严重事件", metric_key="sel.critical_24h", threshold=1, recovery_threshold=0,
                             severity="critical", legacy_id="bmc_sel_critical", enabled=False))
        else:
            rule.enabled = sel_rule.enabled and not sel_rule.deleted
            rule.asset_ids, rule.tags, rule.channel_ids = list(sel_rule.asset_ids), list(sel_rule.tags), list(sel_rule.channel_ids)
            if rule.enabled:
                sel_rule.enabled = False
                audit.record(db, None, "alert.rule.replaced", target_type="alert_rule", target_id=sel_rule.id,
                             summary="“BMC 日志出现严重事件”已由“服务器出现严重硬件事件”取代并停用，作用范围与通知渠道已沿用")
        db.add(rule)
    if db.scalar(select(AlertRule).where(AlertRule.legacy_id == "maintenance_overdue")) is None:
        # 维护中超过预计结束时间还没改回在用，提醒一次；改回在用或延长预计结束后恢复
        db.add(AlertRule(name="维护超过预计结束时间", metric_key="asset.maintenance_overdue", threshold=1, recovery_threshold=0,
                         severity="warning", legacy_id="maintenance_overdue"))
    if db.scalar(select(AlertRule).where(AlertRule.legacy_id == "inspection_critical")) is None:
        # 4.4 P21：命令巡检判定为“严重”的异常才告警（警告 / 提示只在巡检结果里），下一次巡检正常后恢复
        db.add(AlertRule(name="巡检发现严重异常", metric_key="inspection.critical", threshold=1, recovery_threshold=0,
                         severity="critical", legacy_id="inspection_critical"))
    # 4.2 P15：备份失败、UPS 转电池供电
    for legacy_id, name, metric, comparison, threshold, recovery, severity, samples in (
        ("pve_backup_failed", "虚拟化备份失败", "backup.failed_24h", "gte", 1, 0, "warning", 1),
        ("ups_on_battery", "UPS 转为电池供电", "ups.on_mains", "lte", 0, 1, "critical", 2),
    ):
        if db.scalar(select(AlertRule).where(AlertRule.legacy_id == legacy_id)) is None:
            db.add(AlertRule(name=name, metric_key=metric, comparison=comparison, threshold=threshold, recovery_threshold=recovery,
                             severity=severity, consecutive_samples=samples, legacy_id=legacy_id))
    if db.scalar(select(AlertRule).where(AlertRule.legacy_id == "availability")) is None:
        db.add(AlertRule(name="管理端点持续不可达", kind="availability", metric_key="availability", threshold=1, recovery_threshold=0,
                         consecutive_samples=3, severity="critical", legacy_id="availability"))
    # P5：新平台负责控制后，设备可能停在手动风扇转速却无人接管，或温度达到熔断温度
    for key, name, metric in (("thermal_ungoverned", "温控脱管（可能停在手动风扇转速）", "control.thermal_ungoverned"),
                              ("thermal_fuse", "温控熔断（已请求恢复原厂自动温控）", "control.thermal_fuse")):
        if db.scalar(select(AlertRule).where(AlertRule.legacy_id == key)) is None:
            db.add(AlertRule(name=name, metric_key=metric, threshold=1, recovery_threshold=0, severity="critical", legacy_id=key))
    if cfg is not None:
        channel_kind = str(cfg.get("webhook_channel", "custom_webhook"))
        if channel_kind in {"custom_webhook", "serverchan", "dingtalk", "feishu", "wechat_work", "bark", "pushplus"}:
            from app.alerts_schemas import ChannelConfig
            replacements = {"level_text": "severity", "source": "asset_name", "rule_title": "title", "current_val": "value", "threshold_val": "threshold", "detail_msg": "message", "alert_time": "time"}
            title = str(cfg.get("custom_alert_title_template") or "{title}")
            text = str(cfg.get("custom_alert_body_template") or "{message}")
            for old_key, new_key in replacements.items():
                title = title.replace("{" + old_key + "}", "{" + new_key + "}")
                text = text.replace("{" + old_key + "}", "{" + new_key + "}")
            try:
                config = ChannelConfig(subject_template=title, text_template=text).model_dump()
            except ValueError:
                config = ChannelConfig().model_dump()
                audit.record(db, None, "alert.legacy.pending", summary="旧通知模板含不支持的变量，保留旧原始记录供人工核对，通知渠道使用默认模板")
            db.add(NotificationChannel(name="旧版 Webhook（需重新配置）", kind=channel_kind, enabled=False, config=config))
        old.target_type = "alert_rules"
        audit.record(db, None, "alert.legacy.migrate", summary="已迁入旧五条告警规则；webhook 地址与密钥需重新填写，未自动启用外发")
    db.flush()
