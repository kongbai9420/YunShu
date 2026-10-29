"""认证：密码哈希、登录会话、登录限速。

语义沿用旧版 src/auth_manager.py：PBKDF2-HMAC-SHA256 十万次、格式 pbkdf2_sha256$salt$hex，
旧 users.json 的哈希可直接导入；同一来源 IP 5 分钟内失败 5 次锁定 5 分钟；会话 24 小时，
每次访问顺延。与旧版不同：会话存数据库（只存令牌哈希），重启后不掉线；不支持 URL 携带令牌。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import LoginSession, User

PBKDF2_ITERATIONS = 100_000
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
SESSION_TTL = timedelta(hours=24)
# 滑动过期最多每 5 分钟顺延一次：否则每个只读请求都变成写事务，SQLite 轻量版并发时写锁排队（4.1 P12 容量测量发现）
SESSION_RENEW_AFTER = timedelta(minutes=5)
USER_ROLES = ("admin", "operator", "viewer")
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${salt}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 3 or parts[0] != "pbkdf2_sha256":
        return False
    expected = hash_password(password, parts[1])
    return hmac.compare_digest(expected, stored)


def is_valid_password_hash(stored: str) -> bool:
    parts = stored.split("$")
    return len(parts) == 3 and parts[0] == "pbkdf2_sha256" and bool(parts[1]) and len(parts[2]) == 64


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _aware(dt: datetime) -> datetime:
    # SQLite 读回的时间不带时区，统一按 UTC 处理
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# 同一来源对所有账号合计的失败上限：防止换着用户名试；放宽到 30 次，反向代理后多人共用一个来源地址时不会互相锁住
MAX_FAILED_PER_SOURCE = 30


class LoginThrottle:
    """登录失败计数（内存中，单实例部署足够；重启后清零，与旧版一致）。

    同一来源 + 同一账号连续失败 5 次锁这一组 5 分钟，同一来源合计失败 30 次锁整个来源 5 分钟。
    只按来源计数时，反向代理后所有人共用一个来源地址，一个人输错就锁住所有人。
    """

    def __init__(self, clock=time.monotonic) -> None:  # type: ignore[no-untyped-def]
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    @staticmethod
    def _keys(ip: str, username: str) -> tuple[str, str]:
        return f"ip:{ip}", f"user:{ip}:{username.strip().lower()}"

    def remaining_lock(self, ip: str, username: str) -> int:
        with self._lock:
            now = self._clock()
            left = 0
            for key in self._keys(ip, username):
                until = self._locked_until.get(key, 0.0)
                if now < until:
                    left = max(left, int(until - now) + 1)
                else:
                    self._locked_until.pop(key, None)
            return left

    def record_failure(self, ip: str, username: str) -> None:
        with self._lock:
            now = self._clock()
            for key, limit in zip(self._keys(ip, username), (MAX_FAILED_PER_SOURCE, MAX_FAILED_ATTEMPTS)):
                recent = [t for t in self._failures.get(key, []) if now - t < LOCKOUT_SECONDS]
                recent.append(now)
                self._failures[key] = recent
                if len(recent) >= limit:
                    self._locked_until[key] = now + LOCKOUT_SECONDS
                    self._failures.pop(key, None)

    def clear(self, ip: str, username: str) -> None:
        """登录成功只清这个账号的计数；来源合计不清，避免用一个能登录的账号反复重置。"""
        with self._lock:
            key = self._keys(ip, username)[1]
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_token: str
    expires_at: datetime


def issue_session(db: Session, user: User, client_ip: str) -> IssuedSession:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = datetime.now(UTC)
    expires = now + SESSION_TTL
    db.add(
        LoginSession(
            token_hash=_token_hash(token), csrf_token=csrf, user_id=user.id, expires_at=expires, client_ip=client_ip
        )
    )
    user.last_login_at = now
    db.execute(delete(LoginSession).where(LoginSession.expires_at < now))
    return IssuedSession(token=token, csrf_token=csrf, expires_at=expires)


def resolve_session(db: Session, token: str | None) -> tuple[LoginSession, User] | None:
    if not token:
        return None
    row = db.scalar(select(LoginSession).where(LoginSession.token_hash == _token_hash(token)))
    if row is None:
        return None
    now = datetime.now(UTC)
    if _aware(row.expires_at) < now:
        db.delete(row)
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    if _aware(row.expires_at) < now + SESSION_TTL - SESSION_RENEW_AFTER:
        row.expires_at = now + SESSION_TTL
    return row, user


def revoke_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(LoginSession).where(LoginSession.token_hash == _token_hash(token)))


def revoke_user_sessions(db: Session, user: User, *, keep: LoginSession | None = None) -> None:
    stmt = delete(LoginSession).where(LoginSession.user_id == user.id)
    if keep is not None:
        stmt = stmt.where(LoginSession.id != keep.id)
    db.execute(stmt)
