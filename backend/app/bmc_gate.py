"""每台 BMC 排队与访问频率上限（4.4 P20b）。

BMC 是最弱的一环：同一台 BMC 同一时刻只让一个平台操作访问它，采集、控制与页面上的“获取资料”共用同一个队列，
按管理地址（与控制互斥相同的键 bmc:<地址>）排队。只管 BMC 类协议（IPMI、Redfish、SNMP）。

- 控制优先：控制操作等正在进行的采集结束（最多 90 秒）再发命令；控制进行中，这台 BMC 的采集本轮顺延，不算失败。
- 采集遇到另一次采集占用时最多等 20 秒，等不到本轮顺延。
- 频率上限：每台 BMC 每分钟 Redfish 2 次、IPMI 1 次（YUNSHU_BMC_RATE="redfish=2,ipmi=1" 可改，0 表示不限）。
  只限制采集，超出的本轮顺延；控制与“获取资料”不受限制，但计入次数。为吸收定时误差，“一分钟”按最近 55 秒计。
- SQLite 部署只有一个 Worker（API 内嵌或单独一个），在进程内排队；PostgreSQL 多 Worker 时用数据库里的租约
  （bmc_leases，带过期时间，进程崩溃不会永久占用）与访问记录（bmc_accesses，保留 1 小时）跨进程排队。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
import weakref
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.control.base import target_key

BMC_PROTOCOLS = ("ipmi", "redfish", "snmp")
DEFAULT_RATE = {"redfish": 2, "ipmi": 1}
WINDOW = 55.0
CONTROL_WAIT = 90.0
COLLECT_WAIT = 20.0
PROBE_WAIT = 30.0
LEASE_TTL = {"collect": timedelta(minutes=10), "probe": timedelta(minutes=5), "control": timedelta(minutes=30)}
KIND_LABEL = {"collect": "采集", "control": "控制操作", "probe": "获取资料"}


def rate_limits() -> dict[str, int]:
    raw = os.environ.get("YUNSHU_BMC_RATE", "").strip()
    if not raw:
        return dict(DEFAULT_RATE)
    out = dict(DEFAULT_RATE)
    for part in raw.split(","):
        name, _, value = part.partition("=")
        if name.strip() in DEFAULT_RATE and value.strip().isdigit():
            out[name.strip()] = int(value)
    return out


class BmcBusy(Exception):
    """排不上队：reason = busy（被占用）/ rate（超过频率上限）；holder_kind 是占用者的种类。"""

    def __init__(self, reason: str, message: str, holder_kind: str = "") -> None:
        super().__init__(message)
        self.reason, self.holder_kind = reason, holder_kind


def keys_for(addresses: list[str]) -> list[str]:
    return sorted({k for a in addresses if (k := target_key(a))})


# ---- 进程内（SQLite）----


@dataclass
class _Holder:
    token: str
    kind: str


class MemoryBackend:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._held: dict[str, _Holder] = {}
        self._access: dict[tuple[str, str], deque[datetime]] = defaultdict(deque)

    def acquire(self, keys: list[str], kind: str, token: str, wait: float, give_up_on: tuple[str, ...]) -> None:
        deadline = time.monotonic() + wait
        with self._cond:
            while True:
                busy = [self._held[k] for k in keys if k in self._held]
                if not busy:
                    for k in keys:
                        self._held[k] = _Holder(token, kind)
                    return
                left = deadline - time.monotonic()
                if left <= 0 or busy[0].kind in give_up_on:
                    raise BmcBusy("busy", f"这台 BMC 正在执行{KIND_LABEL.get(busy[0].kind, busy[0].kind)}", busy[0].kind)
                self._cond.wait(min(left, 1.0))

    def release(self, keys: list[str], token: str) -> None:
        with self._cond:
            for k in keys:
                if k in self._held and self._held[k].token == token:
                    del self._held[k]
            self._cond.notify_all()

    def count(self, key: str, protocol: str, now: datetime) -> int:
        q = self._access[(key, protocol)]
        while q and (now - q[0]).total_seconds() >= WINDOW:
            q.popleft()
        return sum(1 for t in q if t <= now)

    def record(self, key: str, protocol: str, kind: str, now: datetime) -> None:
        self._access[(key, protocol)].append(now)

    def holders(self) -> dict[str, str]:
        with self._cond:
            return {k: h.kind for k, h in self._held.items()}


# ---- 数据库（PostgreSQL 多 Worker）----


class DbBackend:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _session(self) -> Session:
        return Session(bind=self.engine, expire_on_commit=False)

    def _try(self, keys: list[str], kind: str, token: str) -> str | None:
        """全部拿到返回 None，否则返回占用者种类（已拿到的会退回）。"""
        from app.models import BmcLease

        now = datetime.now(UTC)
        got: list[str] = []
        with self._session() as db:
            for k in keys:
                if db.get(BmcLease, k) is None:
                    try:
                        with db.begin_nested():
                            db.add(BmcLease(key=k, holder="", kind="", expires_at=now))
                    except IntegrityError:
                        pass
                res = db.execute(update(BmcLease).where(BmcLease.key == k, (BmcLease.holder == "") | (BmcLease.expires_at < now))
                                 .values(holder=token, kind=kind, expires_at=now + LEASE_TTL.get(kind, timedelta(minutes=10))))
                if res.rowcount != 1:
                    other = db.scalar(select(BmcLease.kind).where(BmcLease.key == k)) or "collect"
                    if got:
                        db.execute(update(BmcLease).where(BmcLease.key.in_(got), BmcLease.holder == token).values(holder=""))
                    db.commit()
                    return other
                got.append(k)
            db.commit()
        return None

    def acquire(self, keys: list[str], kind: str, token: str, wait: float, give_up_on: tuple[str, ...]) -> None:
        deadline = time.monotonic() + wait
        while True:
            other = self._try(keys, kind, token)
            if other is None:
                return
            if time.monotonic() >= deadline or other in give_up_on:
                raise BmcBusy("busy", f"这台 BMC 正在执行{KIND_LABEL.get(other, other)}", other)
            time.sleep(0.5)

    def release(self, keys: list[str], token: str) -> None:
        from app.models import BmcLease

        with self._session() as db:
            db.execute(update(BmcLease).where(BmcLease.key.in_(keys), BmcLease.holder == token).values(holder=""))
            db.commit()

    def count(self, key: str, protocol: str, now: datetime) -> int:
        from app.models import BmcAccess

        since = now - timedelta(seconds=WINDOW)
        with self._session() as db:
            return db.scalar(select(func.count()).select_from(BmcAccess).where(BmcAccess.key == key, BmcAccess.protocol == protocol,
                                                                              BmcAccess.at > since, BmcAccess.at <= now)) or 0

    def record(self, key: str, protocol: str, kind: str, now: datetime) -> None:
        from app.models import BmcAccess

        with self._session() as db:
            db.add(BmcAccess(key=key, protocol=protocol, kind=kind, at=now))
            db.commit()

    def holders(self) -> dict[str, str]:
        from app.models import BmcLease

        now = datetime.now(UTC)
        with self._session() as db:
            return dict(db.execute(select(BmcLease.key, BmcLease.kind).where(BmcLease.holder != "", BmcLease.expires_at >= now)).all())


_backends: weakref.WeakKeyDictionary[Engine, MemoryBackend | DbBackend] = weakref.WeakKeyDictionary()
_backends_guard = threading.Lock()


def backend_for(engine: Engine) -> MemoryBackend | DbBackend:
    with _backends_guard:
        b = _backends.get(engine)
        if b is None:
            b = DbBackend(engine) if engine.dialect.name == "postgresql" else MemoryBackend()
            _backends[engine] = b
        return b


# ---- 对外 ----


def engine_of(session_factory) -> Engine:  # type: ignore[no-untyped-def]
    with session_factory() as db:
        return db.get_bind()


@contextmanager
def hold(engine: Engine, addresses: list[str], kind: str, *, wait: float) -> Iterator[list[str]]:
    """占用这些地址对应的 BMC；排不上抛 BmcBusy。采集遇到控制或获取资料占用时不等待。"""
    keys = keys_for(addresses)
    if not keys:
        yield []
        return
    b = backend_for(engine)
    token = uuid.uuid4().hex
    give_up = ("control", "probe") if kind == "collect" else ()
    b.acquire(keys, kind, token, wait, give_up)
    try:
        yield keys
    finally:
        b.release(keys, token)


def allow(engine: Engine, address: str, protocol: str, kind: str, now: datetime | None = None) -> str:
    """一次 BMC 访问前调用：采集超过频率上限时返回顺延原因（不记次数）；否则记一次并返回空串。

    now 用采集周期的时间（测试里可以模拟时间）；不传用当前时间。
    """
    key = target_key(address)
    if key is None or protocol not in DEFAULT_RATE:
        return ""
    now = now or datetime.now(UTC)
    b = backend_for(engine)
    limit = rate_limits().get(protocol, 0)
    if kind == "collect" and limit > 0 and b.count(key, protocol, now) >= limit:
        return f"这台 BMC 一分钟内的 {'Redfish' if protocol == 'redfish' else 'IPMI'} 访问已达上限（{limit} 次），本轮顺延"
    b.record(key, protocol, kind, now)
    return ""


def prune(db: Session, *, now: datetime | None = None) -> int:
    from app.models import BmcAccess

    now = now or datetime.now(UTC)
    res = db.execute(delete(BmcAccess).where(BmcAccess.at < now - timedelta(hours=1)), execution_options={"synchronize_session": False})
    return res.rowcount or 0
