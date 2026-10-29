"""任务类型与定时计划注册表。"""

from __future__ import annotations

from functools import lru_cache

from app import queue
from app.crypto import CredentialCipher, load_or_create_keyring
from app.settings import Settings
from app.worker import Handler, Schedule, TaskContext


def ping(ctx: TaskContext) -> dict:
    """自检任务：确认 Worker 在运行、能读写队列。"""
    return {"pong": True, "attempt": ctx.lease.attempt}


def build_handlers(settings: Settings, adapters=None, vuln_http_factory=None, discovery_probe=None, notification_sender=None, power_client_factory=None, power_verify=None, thermal_client_factory=None, thermal_catalog=None, guest_client_factory=None, snmp_config_factory=None, snmp_probe=None, inspection_session_factory=None) -> dict[str, Handler]:  # type: ignore[no-untyped-def]
    from app.collect import build_adapters
    from app.collect.runner import make_handler
    from app.discovery import make_handler as make_discovery_handler, probe_address
    from app.notification_queue import make_handler as make_notification_handler
    from app.control.bmc_snmp import BMC_SNMP_TASK
    from app.control.guest import GUEST_TASK, LINKED_POWER_TASK
    from app.control.service import (
        POWER_TASK, THERMAL_TASK, make_bmc_snmp_handler, make_guest_handler, make_power_handler, make_thermal_handler,
    )
    from app.control.thermal import PRODUCTION_CATALOG

    @lru_cache(maxsize=1)
    def cipher() -> CredentialCipher:
        return CredentialCipher(load_or_create_keyring(settings.master_key_file, data_dir=settings.data_dir))

    from app import inventory
    from app.vulndb import sync as vuln_sync
    from app import ai_notes, command_inspection, vulnnotify

    all_adapters = adapters if adapters is not None else build_adapters()
    return {"system.ping": ping, "collect.asset": make_handler(cipher, all_adapters),
            inventory.PACKAGE_TASK: inventory.make_handler(cipher, all_adapters.get("ssh")),
            vuln_sync.SYNC_TASK: vuln_sync.make_handler(cipher, vuln_http_factory),
            vuln_sync.MATCH_TASK: vuln_sync.make_match_handler(),
            vulnnotify.NOTIFY_TASK: vulnnotify.make_handler(cipher),
            ai_notes.SUMMARY_TASK: ai_notes.make_summary_handler(cipher),
            "discovery.batch": make_discovery_handler(cipher, discovery_probe or probe_address),
            "notification.send": make_notification_handler(cipher, notification_sender),
            POWER_TASK: make_power_handler(cipher, power_client_factory, **(power_verify or {})),
            # 宿主联动（先关其上虚拟机）用同一个电源处理器，只是任务种类不同、允许等待更久
            LINKED_POWER_TASK: make_power_handler(cipher, power_client_factory, **(power_verify or {})),
            GUEST_TASK: make_guest_handler(cipher, guest_client_factory),
            BMC_SNMP_TASK: make_bmc_snmp_handler(cipher, snmp_config_factory, snmp_probe),
            command_inspection.TASK: command_inspection.make_handler(cipher, inspection_session_factory),
            THERMAL_TASK: make_thermal_handler(cipher, thermal_client_factory,
                                               PRODUCTION_CATALOG if thermal_catalog is None else thermal_catalog)}


def build_schedules(settings: Settings, thermal_catalog=None) -> list[Schedule]:  # type: ignore[no-untyped-def]
    from app import metrics
    from app.collect.runner import enqueue_due, prune_tasks
    from app.alert_defaults import ensure_rules
    from app.alerting import evaluate
    from app.notification_queue import plan_notifications
    from app.control.service import reconcile
    from app.control.policy import governance_metrics, migrate_legacy, run_policies
    from app.control.thermal import PRODUCTION_CATALOG
    from app import bmc_gate, command_inspection, inventory, recycle, retention as history, server_events
    from app.collect import boost
    from app.vulndb import sync as vuln_sync
    from app import vulnnotify

    catalog = PRODUCTION_CATALOG if thermal_catalog is None else thermal_catalog

    def thermal(db):
        migrate_legacy(db)
        run_policies(db, catalog=catalog)
        governance_metrics(db, catalog=catalog)

    def alerts(db):
        ensure_rules(db)
        evaluate(db)
        plan_notifications(db)

    def daily_inspection(db):
        from app import inspection

        if inspection.daily_due(db, settings.inspection_hour):
            report = inspection.generate(db, kind="daily", by="定时巡检")
            # 4.3 P18f：开启“巡检报告摘要”时，由 Worker 另行调用 AI，不阻塞调度
            from app import ai_runtime, ai_notes

            if ai_runtime.feature_enabled(ai_runtime.settings_row(db), ai_notes.INSPECTION_FEATURE):
                queue.enqueue(db, ai_notes.SUMMARY_TASK, payload={"report_id": str(report.id)}, idempotency_key=f"aisummary:{report.id}", max_attempts=1,
                              priority=queue.BACKGROUND_PRIORITY)
        inspection.prune(db)

    retention = metrics.Retention(settings.raw_retention_hours, settings.minute_retention_days, settings.hour_retention_days)
    keep = history.HistoryRetention(settings.history_retention_days, settings.audit_retention_days)
    return [
        Schedule("collect.enqueue", 15, lambda db: enqueue_due(db)),
        Schedule("alerts.evaluate", 15, alerts),
        Schedule("control.reconcile", 15, lambda db: reconcile(db)),
        Schedule("thermal.policies", 15, thermal),
        Schedule("metrics.rollup", 60, lambda db: metrics.rollup(db)),
        Schedule("metrics.retention", 3600, lambda db: metrics.enforce_retention(db, retention)),
        Schedule("inspection.daily", 300, daily_inspection),
        Schedule("tasks.prune", 3600, lambda db: (prune_tasks(db), queue.prune_heartbeats(db))),
        Schedule("inventory.packages", 3600, lambda db: inventory.enqueue_due(db)),
        Schedule("server_events.prune", 3600, lambda db: server_events.prune(db)),
        Schedule("inspection.jobs", 30, lambda db: command_inspection.enqueue_due(db)),
        Schedule("inspection.prune", 3600, lambda db: command_inspection.prune(db)),
        Schedule("bmc.prune", 3600, lambda db: (bmc_gate.prune(db), boost.prune(db))),
        Schedule("history.prune", 3600, lambda db: history.prune(db, keep)),
        Schedule("assets.purge", 3600, lambda db: recycle.purge_due(db)),  # P22 回收站到期彻底删除
        Schedule("vuln.sync", 60, lambda db: vuln_sync.enqueue_due(db)),
        Schedule("vuln.match", 3600, lambda db: vuln_sync.enqueue_match(db)),
        Schedule("vuln.notify", 60, lambda db: vulnnotify.enqueue_due(db)),
    ]
