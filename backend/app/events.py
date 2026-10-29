"""SSE 实时推送（P3）：GET /api/events 的事件流。

Worker 可能是独立进程，所以不依赖进程内消息，而是每隔约 2 秒查一次数据库，
找出客户端水位之后的变化：

- ``metrics``：latest_metrics.observed_at 晚于水位的指标，按资产分组，只带值、质量、单位、时间、采集间隔；
- ``collection``：collection_status.last_attempt_at 晚于水位的端点状态（不带 message / facts）；
  新连接（没有 Last-Event-ID）先发一次全部端点状态作为快照；
- ``tasks``：任务按状态计数，变化时才发。

事件 id 是水位（``m<微秒>.c<微秒>``），只写在每轮最后一个事件上；中途断开时浏览器用上一轮的 id
续传，最多重发一轮，不会漏。为防止 Worker 晚提交的旧时间戳被跳过，每轮都回看 lookback 秒，
已发过的同一时间戳不重发。每个事件的 data 不超过 max_event_bytes，超出时拆成多个事件。

查询都很小：latest_metrics 每资产每指标一行（100 台 × 30 指标约 3000 行），collection_status
每端点一行，任务计数走 ix_tasks_status 索引，所以不需要新增索引。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.metrics import NUMERIC_QUALITIES, STALE_FACTOR
from app.models import CollectionStatus, LatestMetric, Task


@dataclass(frozen=True)
class StreamConfig:
    poll_seconds: float = 2.0
    heartbeat_seconds: float = 15.0
    retry_ms: int = 3000
    # 连接最多保持 15 分钟，之后由服务端关闭，浏览器按 Last-Event-ID 自动重连（同时重新校验会话）
    max_seconds: float = 900.0
    # 每隔多久重新确认会话仍然有效（退出登录、被禁用后尽快断开）
    auth_check_seconds: float = 30.0
    max_event_bytes: int = 32_000
    lookback_seconds: float = 10.0
    max_rows: int = 5000


DEFAULT_CONFIG = StreamConfig()
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ID_RE = re.compile(r"^m(\d{1,20})\.c(\d{1,20})$")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _us(dt: datetime) -> int:
    return int((_aware(dt) - _EPOCH) / timedelta(microseconds=1))


def _from_us(us: int) -> datetime:
    return _EPOCH + timedelta(microseconds=us)


def _iso(dt: datetime | None) -> str | None:
    return _aware(dt).isoformat() if dt else None


@dataclass(frozen=True)
class Cursor:
    metrics_us: int
    collection_us: int

    def encode(self) -> str:
        return f"m{self.metrics_us}.c{self.collection_us}"

    @staticmethod
    def parse(value: str | None) -> Cursor | None:
        """解析 Last-Event-ID；格式不对或时间在未来一律当作新连接。"""
        m = _ID_RE.match((value or "").strip())
        if not m:
            return None
        cur = Cursor(int(m.group(1)), int(m.group(2)))
        limit = _us(datetime.now(UTC) + timedelta(minutes=5))
        return cur if cur.metrics_us <= limit and cur.collection_us <= limit else None


@dataclass
class _Track:
    """一类数据的水位与回看去重。floor 之前的不发（新连接不补发历史指标）。"""

    watermark: int
    floor: int
    sent: dict[tuple[str, str], int] = field(default_factory=dict)

    def since(self, lookback_us: int) -> datetime:
        return _from_us(max(self.floor, self.watermark - lookback_us, 0))

    def accept(self, key: tuple[str, str], ts: int) -> bool:
        if ts < self.floor or self.sent.get(key) == ts:
            return False
        self.sent[key] = ts
        if ts > self.watermark:
            self.watermark = ts
        return True

    def prune(self, lookback_us: int) -> None:
        edge = self.watermark - lookback_us
        if len(self.sent) > 0:
            self.sent = {k: v for k, v in self.sent.items() if v >= edge}


@dataclass
class StreamState:
    metrics: _Track
    collection: _Track
    snapshot: bool
    task_counts: dict[str, int] | None = None

    @classmethod
    def start(cls, cursor: Cursor | None, cfg: StreamConfig, *, now: datetime | None = None) -> StreamState:
        if cursor is None:
            now_us = _us(now or datetime.now(UTC))
            # 新连接：指标从现在开始推送；端点状态先发一次全量快照
            return cls(metrics=_Track(now_us, now_us), collection=_Track(0, 0), snapshot=True)
        lb = int(cfg.lookback_seconds * 1_000_000)
        return cls(
            metrics=_Track(cursor.metrics_us, max(0, cursor.metrics_us - lb)),
            collection=_Track(cursor.collection_us, max(0, cursor.collection_us - lb)),
            snapshot=False,
        )

    def cursor(self) -> Cursor:
        return Cursor(self.metrics.watermark, self.collection.watermark)


def _frame(event: str, data: Any, event_id: str | None = None) -> str:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {body}\n\n"


def _chunks(items: list[dict[str, Any]], limit: int, overhead: int) -> Iterable[list[dict[str, Any]]]:
    """按序列化后的字节数拆分，单个事件不超过 limit。超长的单项直接丢弃（不应出现：字段都有长度上限）。"""
    batch: list[dict[str, Any]] = []
    size = overhead
    for item in items:
        n = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()) + 1
        if n + overhead > limit:
            continue
        if batch and size + n > limit:
            yield batch
            batch, size = [], overhead
        batch.append(item)
        size += n
    if batch:
        yield batch


def poll(db: Session, state: StreamState, cfg: StreamConfig, *, now: datetime | None = None) -> list[tuple[str, dict[str, Any]]]:
    """查一轮变化，返回 [(事件名, 数据)]，并推进 state 的水位。"""
    now = now or datetime.now(UTC)
    lb = int(cfg.lookback_seconds * 1_000_000)
    out: list[tuple[str, dict[str, Any]]] = []

    # 指标
    rows = db.execute(
        select(
            LatestMetric.asset_id, LatestMetric.metric_key, LatestMetric.value, LatestMetric.unit,
            LatestMetric.quality, LatestMetric.observed_at, LatestMetric.interval_seconds,
        )
        .where(LatestMetric.observed_at >= state.metrics.since(lb))
        .order_by(LatestMetric.observed_at)
        .limit(cfg.max_rows)
    ).all()
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for aid, key, value, unit, quality, observed, interval in rows:
        ts = _us(observed)
        if not state.metrics.accept((str(aid), key), ts):
            continue
        observed = _aware(observed)
        if quality in NUMERIC_QUALITIES and (now - observed).total_seconds() > STALE_FACTOR * max(1, interval):
            quality, value = "stale", None  # 与 metrics.latest 同一规则：过期在读取时判断
        by_asset.setdefault(str(aid), []).append(
            {"key": key, "value": value, "quality": quality, "unit": unit, "observed_at": observed.isoformat(), "interval": interval}
        )
    for aid, items in by_asset.items():
        for part in _chunks(items, cfg.max_event_bytes, 64):
            out.append(("metrics", {"asset_id": aid, "items": part}))
    state.metrics.prune(lb)

    # 端点采集状态（不含 message、facts：可能较长，详情由页面按需读取）
    snapshot = state.snapshot
    stmt = select(
        CollectionStatus.endpoint_id, CollectionStatus.asset_id, CollectionStatus.protocol, CollectionStatus.state,
        CollectionStatus.error_kind, CollectionStatus.consecutive_failures, CollectionStatus.last_attempt_at,
        CollectionStatus.last_success_at,
    )
    if not snapshot:
        stmt = stmt.where(CollectionStatus.last_attempt_at >= state.collection.since(lb))
    changed: list[dict[str, Any]] = []
    for ep, aid, protocol, st, kind, failures, attempted, succeeded in db.execute(
        stmt.order_by(CollectionStatus.last_attempt_at).limit(cfg.max_rows)
    ):
        if not state.collection.accept((str(ep), ""), _us(attempted)):
            continue
        changed.append({
            "endpoint_id": str(ep), "asset_id": str(aid), "protocol": protocol, "state": st, "error_kind": kind,
            "consecutive_failures": failures, "last_attempt_at": _iso(attempted), "last_success_at": _iso(succeeded),
        })
    if snapshot:
        state.snapshot = False
        state.collection.floor = 0
        parts = list(_chunks(changed, cfg.max_event_bytes, 64)) or [[]]
        for i, part in enumerate(parts):
            out.append(("collection", {"snapshot": True, "final": i == len(parts) - 1, "items": part}))
    else:
        for part in _chunks(changed, cfg.max_event_bytes, 64):
            out.append(("collection", {"snapshot": False, "items": part}))
    state.collection.prune(lb)

    # 任务计数
    counts = {str(k): int(v) for k, v in db.execute(select(Task.status, func.count()).group_by(Task.status)).all()}
    if counts != state.task_counts:
        state.task_counts = counts
        out.append(("tasks", {"counts": counts}))
    return out


async def event_stream(
    session_factory: sessionmaker[Session],
    *,
    cursor: Cursor | None,
    cfg: StreamConfig = DEFAULT_CONFIG,
    still_authorized: Callable[[], bool] | None = None,
    should_stop: Callable[[], bool] | None = None,
    allow: Callable[[str, dict[str, Any]], dict[str, Any] | None] | None = None,
) -> AsyncIterator[str]:
    """生成 SSE 文本。客户端断开时 Starlette 会取消本协程；should_stop 供测试和关闭时使用。"""
    state = StreamState.start(cursor, cfg)
    started = last_write = last_auth = time.monotonic()

    def run_poll() -> list[tuple[str, dict[str, Any]]]:
        with session_factory() as db:
            return poll(db, state, cfg)

    yield f"retry: {cfg.retry_ms}\n: 云枢实时推送\n\n"
    while True:
        if should_stop and should_stop():
            return
        events = await asyncio.to_thread(run_poll)
        if allow is not None:
            # 按账号的资产范围过滤（4.1 P11）；水位照常推进，范围外事件直接丢弃
            events = [(n, d) for n, d0 in events if (d := allow(n, d0)) is not None]
        if events:
            last_id = state.cursor().encode()
            for i, (name, data) in enumerate(events):
                yield _frame(name, data, last_id if i == len(events) - 1 else None)
            last_write = time.monotonic()
        # 分小段睡，便于及时响应停止
        wake = time.monotonic() + cfg.poll_seconds
        while (left := wake - time.monotonic()) > 0:
            if should_stop and should_stop():
                return
            await asyncio.sleep(min(left, 0.5))
        now = time.monotonic()
        if now - started >= cfg.max_seconds:
            yield _frame("bye", {"reason": "reconnect"}, state.cursor().encode())
            return
        if still_authorized and now - last_auth >= cfg.auth_check_seconds:
            last_auth = now
            if not await asyncio.to_thread(still_authorized):
                yield _frame("bye", {"reason": "unauthorized"})
                return
        if now - last_write >= cfg.heartbeat_seconds:
            yield ": ping\n\n"
            last_write = now

