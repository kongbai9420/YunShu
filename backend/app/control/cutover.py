"""控制切换：把控制归属从旧控制器切到新平台，或切回。

不得让新旧系统同时向同一台 BMC 下发命令。所以：
- 切到新平台必须同时满足：部署没有关闭切换开关（YUNSHU_CONTROL_CUTOVER 默认打开，设为 0 关闭）、管理员逐项确认
  检查清单、输入确认文字、没有未完成的控制操作，以及（配置了旧版地址时）旧版服务已停止响应。
- 切回旧控制器随时可以：新平台立即停止下发（排队中的操作执行前会被门禁拒绝），再按手册重新启动旧版。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.control.service import OPEN_STATES, Rejected, control_setting, now_utc
from app.control.thermal import ThermalProfile
from app.models import ControlOperation, ControlTarget, ThermalPolicy, User
from app.settings import Settings

CONFIRM_NEW = "切换到新平台"
CONFIRM_LEGACY = "切回旧控制器"

CHECKLIST: list[tuple[str, str]] = [
    ("legacy_auto", "旧版里每台服务器都已切回“自动”温控，或已登录 BMC 确认风扇为原厂自动模式"),
    ("legacy_stopped", "旧版服务已停止（进程或容器已退出、开机自启已关闭），不会再向 BMC 发送任何命令"),
    ("backup", "已备份新平台数据库（含单独导出的密钥文件）和旧版配置快照，回滚需要它们"),
    ("catalog", "已知晓：能力目录中没有经实机验证的机型时，新平台不会下发任何风扇命令；可能停在手动转速的设备会触发“温控脱管”告警，需要人工处理"),
]


@dataclass
class Check:
    key: str
    label: str
    ok: bool | None  # None 表示无法自动确认，需人工确认
    detail: str
    blocking: bool = True


LegacyProbe = Callable[[str], bool]


def probe_legacy(url: str) -> bool:
    """旧版是否还在响应。只发一个 GET，不带凭据。"""
    try:
        with httpx.Client(timeout=3.0, follow_redirects=False) as c:
            c.get(url)
        return True
    except httpx.HTTPError:
        return False


def preflight(db: Session, settings: Settings, catalog: tuple[ThermalProfile, ...], *, legacy_probe: LegacyProbe = probe_legacy) -> list[Check]:
    checks: list[Check] = [
        Check("switch", "控制切换开关未关闭（YUNSHU_CONTROL_CUTOVER 未设为 0）", settings.control_cutover_enabled,
              "已打开" if settings.control_cutover_enabled else "已关闭：这套部署设置了 YUNSHU_CONTROL_CUTOVER=0，只能查看检查结果"),
    ]
    if settings.legacy_url:
        alive = legacy_probe(settings.legacy_url)
        checks.append(Check("legacy_down", "旧版服务已停止响应", not alive,
                            "旧版地址仍在响应，请先停止旧版" if alive else "旧版地址已无响应"))
    else:
        checks.append(Check("legacy_down", "旧版服务已停止响应", None, "未配置旧版地址（YUNSHU_LEGACY_URL），无法自动确认，请在检查清单中人工确认", blocking=False))
    open_ops = db.scalar(select(func.count()).select_from(ControlOperation).where(ControlOperation.status.in_(OPEN_STATES))) or 0
    checks.append(Check("no_open_ops", "没有排队或执行中的控制操作", open_ops == 0, f"还有 {open_ops} 个" if open_ops else "没有"))
    indeterminate = db.scalar(select(func.count()).select_from(ControlTarget).where(ControlTarget.state == "indeterminate")) or 0
    checks.append(Check("no_indeterminate", "没有结果不确定的设备", indeterminate == 0,
                        f"{indeterminate} 台待核对（切换后它们仍会暂停控制）" if indeterminate else "没有", blocking=False))
    manual = db.scalar(select(func.count()).select_from(ThermalPolicy).where(ThermalPolicy.legacy_mode.in_(("manual", "dynamic", "preset")))) or 0
    checks.append(Check("catalog", "能力目录中有经实机验证的机型", bool(catalog),
                        (f"有 {len(catalog)} 个" if catalog else "没有：切换后不会下发任何风扇命令")
                        + (f"；旧版有 {manual} 台不是自动模式，切换后会触发“温控脱管”告警" if manual and not catalog else ""), blocking=False))
    return checks


def describe(db: Session, settings: Settings, catalog: tuple[ThermalProfile, ...], **kw: Any) -> dict[str, Any]:
    s = control_setting(db)
    checks = preflight(db, settings, catalog, **kw)
    return {
        "owner": s.owner, "revision": s.revision, "changed_by": s.changed_by,
        "changed_at": s.changed_at.isoformat() if s.changed_at else None, "note": s.note,
        "cutover_enabled": settings.control_cutover_enabled,
        "checks": [c.__dict__ for c in checks],
        "ready": all(c.ok for c in checks if c.blocking),
        "checklist": [{"key": k, "label": v} for k, v in CHECKLIST],
        "confirm_new": CONFIRM_NEW, "confirm_legacy": CONFIRM_LEGACY,
    }


def change_owner(
    db: Session, actor: User, settings: Settings, catalog: tuple[ThermalProfile, ...], *, owner: str, revision: int, confirm: str,
    checklist: list[str], note: str, legacy_probe: LegacyProbe = probe_legacy,
) -> dict[str, Any]:
    s = control_setting(db)
    if revision != s.revision:
        raise Rejected("conflict", f"控制归属已被其他人修改（当前修订 {s.revision}），请刷新后再操作")
    if owner == s.owner:
        raise Rejected("same", "控制归属没有变化")
    if owner == "new":
        checks = preflight(db, settings, catalog, legacy_probe=legacy_probe)
        failed = [c.label for c in checks if c.blocking and not c.ok]
        if failed:
            raise Rejected("preflight", "切换前检查未通过：" + "；".join(failed))
        missing = [label for key, label in CHECKLIST if key not in checklist]
        if missing:
            raise Rejected("checklist", "请逐项确认检查清单：" + "；".join(missing))
        if confirm.strip() != CONFIRM_NEW:
            raise Rejected("confirm", f"请输入“{CONFIRM_NEW}”确认")
    elif owner == "legacy":
        if confirm.strip() != CONFIRM_LEGACY:
            raise Rejected("confirm", f"请输入“{CONFIRM_LEGACY}”确认")
    else:
        raise Rejected("owner", "控制归属只能是旧控制器或新平台")
    old = s.owner
    s.owner, s.revision, s.changed_by, s.changed_at, s.note = owner, s.revision + 1, actor.display_name or actor.username, now_utc(), note[:500]
    audit.record(
        db, actor, "control.owner.change", target_type="control", target_id="default",
        summary=("控制归属切到新平台" if owner == "new" else "控制归属切回旧控制器") + (f"：{note.strip()[:200]}" if note.strip() else ""),
        detail={"from": old, "to": owner, "checklist": sorted(checklist), "revision": s.revision},
    )
    db.flush()
    return describe(db, settings, catalog, legacy_probe=legacy_probe)
