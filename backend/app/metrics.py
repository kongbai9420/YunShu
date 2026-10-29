"""指标存储。

- 原始数据：一次采集一行（同一资产同一次采集的多个指标打包成紧凑 JSON），按 UTC 日期分表
  `metric_raw_YYYYMMDD`，默认保留 48 小时，过期后整表删除，不逐行清理。
  每个指标只存 [值] 或 [值, 质量代码]，单位只在最新值表里存一份。
- 汇总：分钟（保留 30 天）、小时（保留 1 年），压缩存放（见 MetricRollup）；只汇总有效和估算的值。
- 最新值：latest_metrics；stale（过期）不落库，读取时按采集间隔判断。
- 失败、不支持、未配置的指标值为空，绝不写成 0。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, delete, func, inspect, select, text
from sqlalchemy.orm import Session

from app import dialect
from app.models import LatestMetric, MetricRollup, MetricState

QUALITIES = ("valid", "stale", "unsupported", "unconfigured", "failed", "estimated")
STORED_QUALITIES = ("valid", "unsupported", "unconfigured", "failed", "estimated")
NUMERIC_QUALITIES = ("valid", "estimated")
# 原始数据里的质量代码；valid 省略
Q_CODE = {"estimated": "e", "failed": "f", "unsupported": "u", "unconfigured": "c"}
Q_NAME = {v: k for k, v in Q_CODE.items()}
RAW_PREFIX = "metric_raw_"
MINUTE, HOUR, DAY = 60, 3600, 86400
STALE_FACTOR = 3  # 超过 3 个采集周期没有新值即为过期


@dataclass(frozen=True)
class Retention:
    raw_hours: int = 48
    minute_days: int = 30
    hour_days: int = 365


@dataclass
class Point:
    """一次采集里的一个指标。key 形如 cpu.temp、fan.rpm:FAN1、if.in_bps:eth0。"""

    key: str
    value: float | None
    unit: str = ""
    quality: str = "valid"
    estimated: bool = False
    message: str = ""

    def __post_init__(self) -> None:
        if self.quality not in STORED_QUALITIES:
            raise ValueError(f"不能写入的质量状态：{self.quality}")
        if self.quality in NUMERIC_QUALITIES and self.value is None:
            raise ValueError(f"{self.key}：有效值不能为空")
        if self.quality not in NUMERIC_QUALITIES:
            self.value = None  # 失败 / 不支持 / 未配置时不保留数值，避免显示成 0
        if self.quality == "estimated":
            self.estimated = True


def _epoch(dt: datetime) -> int:
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp())


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def raw_table_name(day: date) -> str:
    return f"{RAW_PREFIX}{day:%Y%m%d}"


def _day_of(ts: int) -> date:
    return datetime.fromtimestamp(ts, UTC).date()


_meta = MetaData()


def _raw_table(name: str) -> Table:
    if name in _meta.tables:
        return _meta.tables[name]
    return Table(
        name,
        _meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("asset_id", String(36), nullable=False),
        Column("source", String(64), nullable=False),
        Column("observed_at", Integer, nullable=False, index=True),
        Column("data", Text, nullable=False),
    )


def _ensure_raw(db: Session, day: date) -> Table:
    table = _raw_table(raw_table_name(day))
    table.create(db.connection(), checkfirst=True)
    return table


def raw_tables(db: Session) -> list[str]:
    return sorted(n for n in inspect(db.connection()).get_table_names() if re.fullmatch(rf"{RAW_PREFIX}\d{{8}}", n))


def _encode(p: Point) -> list[Any]:
    code = Q_CODE.get(p.quality)
    return [p.value] if code is None else [p.value, code]


def _decode(item: list[Any]) -> tuple[float | None, str]:
    return item[0], Q_NAME[item[1]] if len(item) > 1 else "valid"


# ---- 写入 ----


def write_sample(
    db: Session,
    asset_id: uuid.UUID,
    source: str,
    points: list[Point],
    *,
    observed_at: datetime,
    interval_seconds: int = 60,
) -> None:
    """写一次采集：原始一行 + 更新最新值。较旧的观测不会覆盖更新的最新值。"""
    if not points:
        return
    observed_at = _aware(observed_at)
    day = observed_at.astimezone(UTC).date()
    table = _raw_table(raw_table_name(day))
    row = {
        "asset_id": str(asset_id),
        "source": source,
        "observed_at": _epoch(observed_at),
        "data": json.dumps({p.key: _encode(p) for p in points}, ensure_ascii=False, separators=(",", ":")),
    }
    try:
        with db.begin_nested():
            db.execute(table.insert().values(**row))
    except dialect.MISSING_TABLE:
        # 当天的分表还不存在（每天第一次写入、恢复备份后）：建表后重试
        _ensure_raw(db, day)
        db.execute(table.insert().values(**row))

    rows = [
        {
            "asset_id": asset_id, "metric_key": p.key, "value": p.value, "unit": p.unit, "quality": p.quality,
            "estimated": p.estimated, "source": source, "message": p.message[:500], "observed_at": observed_at,
            "interval_seconds": interval_seconds,
            "last_valid_value": p.value if p.quality in NUMERIC_QUALITIES else None,
            "last_valid_at": observed_at if p.quality in NUMERIC_QUALITIES else None,
        }
        for p in points
    ]
    stmt = dialect.insert(db, LatestMetric)
    ex = stmt.excluded
    c = LatestMetric.__table__.c
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[c.asset_id, c.metric_key],
            set_={
                "value": ex.value, "unit": ex.unit, "quality": ex.quality, "estimated": ex.estimated,
                "source": ex.source, "message": ex.message, "observed_at": ex.observed_at,
                "interval_seconds": ex.interval_seconds,
                "last_valid_value": func.coalesce(ex.last_valid_value, c.last_valid_value),
                "last_valid_at": func.coalesce(ex.last_valid_at, c.last_valid_at),
            },
            # 较旧的观测（补传、乱序）不覆盖已有的较新值
            where=ex.observed_at >= c.observed_at,
        ),
        rows,
    )


# ---- 读取 ----


@dataclass
class LatestView:
    key: str
    value: float | None
    unit: str
    quality: str
    estimated: bool
    source: str
    message: str
    observed_at: datetime
    age_seconds: int
    interval_seconds: int
    last_valid_value: float | None
    last_valid_at: datetime | None


def latest(db: Session, asset_id: uuid.UUID, *, now: datetime | None = None) -> list[LatestView]:
    return latest_many(db, [asset_id], now=now)[asset_id]


def latest_many(
    db: Session, asset_ids: list[uuid.UUID], *, keys: tuple[str, ...] | None = None, now: datetime | None = None,
) -> dict[uuid.UUID, list[LatestView]]:
    """一批资产共用一次查询，沿用单资产接口的六种质量与过期判断。"""
    now = now or datetime.now(UTC)
    out: dict[uuid.UUID, list[LatestView]] = {aid: [] for aid in asset_ids}
    if not asset_ids:
        return out
    stmt = select(LatestMetric).where(LatestMetric.asset_id.in_(asset_ids)).order_by(LatestMetric.metric_key)
    if keys is not None:
        stmt = stmt.where(LatestMetric.metric_key.in_(keys))
    for r in db.scalars(stmt):
        observed = _aware(r.observed_at)
        age = int((now - observed).total_seconds())
        quality = r.quality
        if quality in NUMERIC_QUALITIES and age > STALE_FACTOR * max(1, r.interval_seconds):
            quality = "stale"
        out[r.asset_id].append(
            LatestView(
                key=r.metric_key, value=r.value if quality != "stale" else None, unit=r.unit, quality=quality,
                estimated=r.estimated, source=r.source, message=r.message, observed_at=observed, age_seconds=age,
                interval_seconds=r.interval_seconds,
                last_valid_value=r.last_valid_value, last_valid_at=_aware(r.last_valid_at) if r.last_valid_at else None,
            )
        )
    return out


def _unpack(row: MetricRollup, key: str, slot_seconds: int) -> list[dict[str, Any]]:
    m = json.loads(row.data).get(key)
    if not m:
        return []
    out = []
    for i, avg in enumerate(m["a"]):
        if avg is None:
            continue
        mn = m["mn"][i] if "mn" in m and m["mn"][i] is not None else avg
        mx = m["mx"][i] if "mx" in m and m["mx"][i] is not None else avg
        out.append({"t": row.bucket + i * slot_seconds, "min": mn, "max": mx, "avg": avg, "n": m["n"][i] if "n" in m else 1})
    return out


def series(
    db: Session, asset_id: uuid.UUID, key: str, start: datetime, end: datetime, *, resolution: str = "auto"
) -> dict[str, Any]:
    """时间序列。auto：6 小时内用原始数据，7 天内用分钟汇总，再长用小时汇总。"""
    start, end = _aware(start), _aware(end)
    s, e = _epoch(start), _epoch(end)
    span = e - s
    if resolution == "auto":
        resolution = "raw" if span <= 6 * HOUR else "minute" if span <= 7 * DAY else "hour"
    if resolution == "raw":
        points = []
        names = set(raw_tables(db))
        day = _day_of(s)
        while day <= _day_of(e):
            name = raw_table_name(day)
            if name in names:
                t = _raw_table(name)
                rows = db.execute(
                    select(t.c.observed_at, t.c.data)
                    .where(t.c.asset_id == str(asset_id), t.c.observed_at >= s, t.c.observed_at <= e)
                    .order_by(t.c.observed_at)
                ).all()
                for ts, data in rows:
                    item = json.loads(data).get(key)
                    if item is not None:
                        v, q = _decode(item)
                        points.append({"t": ts, "v": v, "q": q})
            day += timedelta(days=1)
        return {"resolution": "raw", "points": points}
    res, span_of_row = (MINUTE, HOUR) if resolution == "minute" else (HOUR, DAY)
    rows = db.scalars(
        select(MetricRollup)
        .where(
            MetricRollup.asset_id == asset_id, MetricRollup.resolution == res,
            MetricRollup.bucket >= s // span_of_row * span_of_row, MetricRollup.bucket <= e,
        )
        .order_by(MetricRollup.bucket)
    ).all()
    pts = [p for r in rows for p in _unpack(r, key, res) if s <= p["t"] <= e]
    return {"resolution": resolution, "points": pts}


# ---- 汇总与清理（由 Worker 定时计划调用） ----


def _state(db: Session, name: str) -> int | None:
    row = db.get(MetricState, name)
    return row.value if row else None


def _set_state(db: Session, name: str, value: int) -> None:
    row = db.get(MetricState, name)
    if row is None:
        db.add(MetricState(name=name, value=value))
    else:
        row.value = value


def _merge_slot(m: dict[str, list[Any]], slots: int, i: int, vmin: float, vmax: float, total: float, n: int) -> None:
    """把一个槽的统计并入压缩结构（同一槽重复写入会合并，不重复计数）。"""
    for k in ("a", "n"):
        m.setdefault(k, [None] * slots)
    old_n = m["n"][i] or 0
    if old_n:
        old_avg = m["a"][i]
        old_min = m["mn"][i] if "mn" in m and m["mn"][i] is not None else old_avg
        old_max = m["mx"][i] if "mx" in m and m["mx"][i] is not None else old_avg
        vmin, vmax = min(vmin, old_min), max(vmax, old_max)
        total += old_avg * old_n
        n += old_n
    m["a"][i] = round(total / n, 3)
    m["n"][i] = n
    if vmin != vmax or "mn" in m:
        for k, v in (("mn", vmin), ("mx", vmax)):
            m.setdefault(k, [None] * slots)
            m[k][i] = v


Stat = tuple[int, float, float, float, int]  # (槽号, 最小, 最大, 合计, 样本数)


def _upsert_packed(db: Session, resolution: int, bucket: int, slots: int, stats: dict[uuid.UUID, dict[str, list[Stat]]]) -> int:
    """每台资产只读写一次汇总行。stats: {asset_id: {key: [Stat, …]}}"""
    made = 0
    for aid, keys in stats.items():
        row = db.get(MetricRollup, (aid, resolution, bucket))
        data: dict[str, dict[str, list[Any]]] = json.loads(row.data) if row else {}
        for key, items in keys.items():
            for i, vmin, vmax, total, n in items:
                _merge_slot(data.setdefault(key, {}), slots, i, vmin, vmax, total, n)
                made += 1
        payload = json.dumps(data, separators=(",", ":"))
        if row is None:
            db.add(MetricRollup(asset_id=aid, resolution=resolution, bucket=bucket, data=payload))
        else:
            row.data = payload
    db.flush()
    return made


def rollup(db: Session, *, now: datetime | None = None, max_minutes: int = 24 * 60) -> dict[str, int]:
    """把已经结束的分钟汇总到分钟桶、已经结束的小时汇总到小时桶。可重复执行，不会重复计数。

    每次最多补 max_minutes 分钟，停机很久后分几轮追上，不会一次卡住 Worker。
    """
    now_ts = _epoch(now or datetime.now(UTC))
    upto = now_ts // MINUTE * MINUTE  # 当前这一分钟还没结束，不汇总
    names = raw_tables(db)
    since = _state(db, "rollup.minute")
    if since is None:
        if not names:
            return {"minute": 0, "hour": 0}
        t0 = _raw_table(names[0])
        first = db.scalar(select(func.min(t0.c.observed_at)))
        if first is None:
            return {"minute": 0, "hour": 0}
        since = first // MINUTE * MINUTE
    upto = min(upto, since + max_minutes * MINUTE)
    made_min = made_hour = 0
    name_set = set(names)
    m = since
    while m < upto:
        # 一次处理一个小时内的若干分钟（同一分钟汇总行）
        hour = m // HOUR * HOUR
        stop = min(upto, hour + HOUR)
        name = raw_table_name(_day_of(m))
        per: dict[uuid.UUID, dict[str, dict[int, list[float]]]] = {}
        if name in name_set:
            t = _raw_table(name)
            for aid, ts, data in db.execute(
                select(t.c.asset_id, t.c.observed_at, t.c.data).where(t.c.observed_at >= m, t.c.observed_at < stop)
            ):
                slot = (ts - hour) // MINUTE
                bucket = per.setdefault(uuid.UUID(aid), {})
                for key, item in json.loads(data).items():
                    v, q = _decode(item)
                    if v is not None and q in NUMERIC_QUALITIES:
                        bucket.setdefault(key, {}).setdefault(slot, []).append(v)
        if per:
            stats_min = {
                aid: {key: [(slot, min(v), max(v), sum(v), len(v)) for slot, v in slots.items()] for key, slots in keys.items()}
                for aid, keys in per.items()
            }
            made_min += _upsert_packed(db, MINUTE, hour, 60, stats_min)
        m = stop
    _set_state(db, "rollup.minute", upto)

    # 小时汇总：只处理已经完整结束、分钟数据也已汇总完的小时
    hour_upto = upto // HOUR * HOUR
    h_since = _state(db, "rollup.hour")
    if h_since is None:
        h_since = since // HOUR * HOUR
    h = h_since
    while h < hour_upto:
        day = h // DAY * DAY
        slot = (h - day) // HOUR
        stats: dict[uuid.UUID, dict[str, list[Stat]]] = {}
        for row in db.scalars(select(MetricRollup).where(MetricRollup.resolution == MINUTE, MetricRollup.bucket == h)):
            for key, mm in json.loads(row.data).items():
                vals = [(a, (mm.get("mn") or [None] * 60)[i], (mm.get("mx") or [None] * 60)[i], mm["n"][i]) for i, a in enumerate(mm["a"]) if a is not None]
                if not vals:
                    continue
                n = sum(v[3] for v in vals)
                stats.setdefault(row.asset_id, {})[key] = [(
                    slot,
                    min(v[1] if v[1] is not None else v[0] for v in vals),
                    max(v[2] if v[2] is not None else v[0] for v in vals),
                    sum(v[0] * v[3] for v in vals),
                    n,
                )]
        if stats:
            # 重新汇总同一小时时先清掉该槽，避免重复计数
            for aid, keys in stats.items():
                row = db.get(MetricRollup, (aid, HOUR, day))
                if row is not None:
                    data = json.loads(row.data)
                    for key in keys:
                        if key in data:
                            for k in data[key]:
                                data[key][k][slot] = None
                    row.data = json.dumps(data, separators=(",", ":"))
            made_hour += _upsert_packed(db, HOUR, day, 24, stats)
        h += HOUR
    _set_state(db, "rollup.hour", hour_upto)
    return {"minute": made_min, "hour": made_hour}


def enforce_retention(db: Session, retention: Retention = Retention(), *, now: datetime | None = None) -> dict[str, int]:
    """整表删除过期的原始日表；按时间删除过期的分钟、小时汇总行。"""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=retention.raw_hours)
    dropped = 0
    for name in raw_tables(db):
        day = datetime.strptime(name[len(RAW_PREFIX):], "%Y%m%d").replace(tzinfo=UTC)
        # 整张日表都早于保留期才删
        if day + timedelta(days=1) <= cutoff:
            db.execute(text(f"DROP TABLE IF EXISTS {name}"))
            if name in _meta.tables:
                _meta.remove(_meta.tables[name])
            dropped += 1
    m = db.execute(
        delete(MetricRollup).where(
            MetricRollup.resolution == MINUTE, MetricRollup.bucket + HOUR <= _epoch(now - timedelta(days=retention.minute_days))
        )
    ).rowcount
    h = db.execute(
        delete(MetricRollup).where(
            MetricRollup.resolution == HOUR, MetricRollup.bucket + DAY <= _epoch(now - timedelta(days=retention.hour_days))
        )
    ).rowcount
    return {"raw_tables": dropped, "minute": m or 0, "hour": h or 0}
