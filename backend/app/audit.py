"""操作审计。只追加；写入前去掉敏感字段，避免密码、令牌进入审计详情。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent, User

SENSITIVE_KEYS = ("password", "secret", "token", "community", "private_key", "passphrase")


def _is_sensitive(key: object) -> bool:
    k = str(key).lower()
    return k == "key" or any(s in k for s in SENSITIVE_KEYS)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***" if _is_sensitive(k) else redact(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def record(
    db: Session,
    actor: User | None,
    action: str,
    *,
    target_type: str = "",
    target_id: object = "",
    summary: str = "",
    detail: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_id=actor.id if actor else None,
        actor_name=actor.username if actor else "",
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        summary=summary,
        detail=redact(detail or {}),
    )
    db.add(event)
    return event
