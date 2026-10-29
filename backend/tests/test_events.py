"""SSE 实时推送：鉴权、指标与采集事件、Last-Event-ID 续传、心跳、断开。全部用伪造数据。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from app import metrics
from app.events import Cursor, StreamConfig, StreamState, event_stream, poll
from app.metrics import Point
from app.models import Asset, CollectionStatus, LoginSession, ManagementEndpoint, Task
from tests.api_helpers import add_user, login

FAST = StreamConfig(poll_seconds=0.05, heartbeat_seconds=0.12, max_seconds=5.0, auth_check_seconds=0.1)


def parse(text: str) -> list[dict]:
    """把 SSE 文本拆成事件：{"event", "data", "id"}、{"comment"} 或 {"retry"}。"""
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        ev: dict = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                ev.setdefault("comment", line[1:].strip())
            else:
                k, _, v = line.partition(": ")
                ev[k] = json.loads(v) if k == "data" else v
        out.append(ev)
    return out


def us(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000)


def add_asset(factory: sessionmaker[Session], name: str = "伪造服务器") -> tuple[Asset, ManagementEndpoint]:
    with factory() as db:
        a = Asset(name=name, form="physical", role="server")
        ep = ManagementEndpoint(address="192.0.2.31", port=161, protocol="snmp")
        a.endpoints = [ep]
        db.add(a)
        db.commit()
        db.refresh(a)
        db.refresh(ep)
        db.expunge_all()
        return a, ep


def set_status(factory: sessionmaker[Session], a: Asset, ep: ManagementEndpoint, state: str, at: datetime) -> None:
    with factory() as db:
        row = db.get(CollectionStatus, ep.id)
        if row is None:
            row = CollectionStatus(
                endpoint_id=ep.id, asset_id=a.id, protocol="snmp", state=state, last_attempt_at=at,
                message="伪造消息，不应出现在推送里", facts={"hostname": "fake-host"},
            )
            db.add(row)
        row.state, row.last_attempt_at = state, at
        db.commit()


def write(factory: sessionmaker[Session], a: Asset, points: list[Point], at: datetime) -> None:
    with factory() as db:
        metrics.write_sample(db, a.id, "snmp", points, observed_at=at)
        db.commit()


def named(frames: list[dict], name: str) -> list[dict]:
    return [e for e in frames if e.get("event") == name]


# ---- 通过 HTTP ----


def test_requires_login(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/api/events").status_code == 401


def test_requires_password_change_done(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "newbie", "viewer", must_change=True)
    assert login(app_factory(), "newbie").get("/api/events").status_code == 403


def test_viewer_stream_over_http(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "victor", "viewer")
    c = login(app_factory(), "victor")
    c.app.state.sse_config = StreamConfig(poll_seconds=0.05, heartbeat_seconds=0.1, max_seconds=0.4)
    a, ep = add_asset(factory)
    set_status(factory, a, ep, "ok", datetime.now(UTC))
    with c.stream("GET", "/api/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"].startswith("no-cache")
        text = "".join(resp.iter_text())  # 到 max_seconds 后服务端主动关闭
    evs = parse(text)
    assert evs[0]["retry"] == "3000"
    snap = named(evs, "collection")[0]["data"]
    assert snap["snapshot"] is True and snap["items"][0]["state"] == "ok" and snap["items"][0]["asset_id"] == str(a.id)
    assert any(e.get("comment") == "ping" for e in evs)  # 心跳
    assert evs[-1]["event"] == "bye" and Cursor.parse(evs[-1]["id"]) is not None
    assert "伪造消息" not in text and "fake-host" not in text and "192.0.2.31" not in text  # 不带 message / facts / 地址


def test_resume_over_http_with_header(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    c = login(app_factory(), "olivia")
    c.app.state.sse_config = StreamConfig(poll_seconds=0.05, heartbeat_seconds=1, max_seconds=0.2)
    a, ep = add_asset(factory)
    t0 = datetime.now(UTC) - timedelta(seconds=40)
    set_status(factory, a, ep, "failed", t0 + timedelta(seconds=20))
    with c.stream("GET", "/api/events", headers={"Last-Event-ID": Cursor(us(t0), us(t0)).encode()}) as resp:
        evs = parse("".join(resp.iter_text()))
    coll = named(evs, "collection")
    assert coll and coll[0]["data"]["snapshot"] is False and coll[0]["data"]["items"][0]["state"] == "failed"


def test_stream_closes_when_session_ends(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "olivia", "operator")
    c = login(app_factory(), "olivia")
    # TestClient 会先跑完整个响应，所以流中途的会话检查直接驱动生成器测试
    checks: list[bool] = []

    def still_authorized() -> bool:
        checks.append(True)
        return len(checks) < 2  # 第二次检查时会话已失效

    async def run() -> list[dict]:
        return parse("".join([c async for c in event_stream(factory, cursor=None, cfg=FAST, still_authorized=still_authorized)]))

    frames = asyncio.run(run())
    assert frames[-1]["event"] == "bye" and frames[-1]["data"] == {"reason": "unauthorized"} and "id" not in frames[-1]
    # 路由里的检查：会话删除（退出登录 / 过期）后不再通过
    from fastapi import HTTPException

    from app.api.routes_events import _check

    class Req:
        app = c.app

    token = c.cookies.get("yunshu_session")
    _check(Req(), token, commit=False)  # type: ignore[arg-type]
    with factory() as db:
        db.execute(delete(LoginSession))
        db.commit()
    with pytest.raises(HTTPException) as err:
        _check(Req(), token, commit=False)  # type: ignore[arg-type]
    assert err.value.status_code == 401
    assert c.get("/api/events").status_code == 401


# ---- 直接驱动生成器（短轮询间隔） ----


async def run_stream(
    factory: sessionmaker[Session],
    *,
    until: Callable[[list[dict]], bool],
    cursor: Cursor | None = None,
    during: Callable[[], None] | None = None,
    cfg: StreamConfig = FAST,
) -> list[dict]:
    """运行事件流直到 until(已收事件) 为真；during 在收到第一轮事件后调用一次（模拟 Worker 写入）。"""
    frames: list[dict] = []
    stop = False
    called = False
    async for chunk in event_stream(factory, cursor=cursor, cfg=cfg, should_stop=lambda: stop):
        frames.extend(parse(chunk))
        if not called and any("event" in f for f in frames):
            called = True
            if during:
                await asyncio.to_thread(during)
        if until(frames):
            stop = True
    return frames


def test_emits_metrics_and_collection_after_writes(factory) -> None:  # type: ignore[no-untyped-def]
    a, ep = add_asset(factory)
    write(factory, a, [Point("cpu.temp", 40.0, "°C")], datetime.now(UTC) - timedelta(minutes=1))  # 连接前的旧指标不补发

    def worker() -> None:
        now = datetime.now(UTC)
        write(factory, a, [Point("cpu.temp", 61.5, "°C"), Point("power.watts", None, "W", quality="failed")], now)
        set_status(factory, a, ep, "failed", now)

    frames = asyncio.run(run_stream(
        factory, during=worker, until=lambda f: bool(named(f, "metrics")) and len(named(f, "collection")) >= 2,
    ))
    m = named(frames, "metrics")
    assert len(m) == 1 and m[0]["data"]["asset_id"] == str(a.id)
    items = {i["key"]: i for i in m[0]["data"]["items"]}
    assert items["cpu.temp"]["value"] == 61.5 and items["cpu.temp"]["quality"] == "valid" and items["cpu.temp"]["unit"] == "°C"
    assert items["power.watts"]["value"] is None and items["power.watts"]["quality"] == "failed"  # 失败不写成 0
    coll = named(frames, "collection")
    assert coll[0]["data"] == {"snapshot": True, "final": True, "items": []}
    assert coll[-1]["data"]["items"][0]["state"] == "failed"
    assert frames[0]["retry"] == "3000"
    assert named(frames, "tasks")[0]["data"] == {"counts": {}}


def test_no_duplicates_between_polls(factory) -> None:  # type: ignore[no-untyped-def]
    a, _ = add_asset(factory)
    state = StreamState.start(None, FAST, now=datetime.now(UTC) - timedelta(seconds=5))
    write(factory, a, [Point("cpu.load", 3.0, "%")], datetime.now(UTC))
    with factory() as db:
        first = poll(db, state, FAST)
        second = poll(db, state, FAST)
    assert [n for n, _ in first].count("metrics") == 1
    assert second == []  # 回看窗口内已发过的不重发；任务计数没变也不发


def test_late_commit_inside_lookback_is_not_lost(factory) -> None:  # type: ignore[no-untyped-def]
    a, _ = add_asset(factory)
    now = datetime.now(UTC)
    state = StreamState.start(None, FAST, now=now - timedelta(seconds=30))
    write(factory, a, [Point("cpu.load", 5.0, "%")], now)
    with factory() as db:
        poll(db, state, FAST)
    # Worker 晚提交了一条时间戳更早的观测（另一台资产）
    b, _ = add_asset(factory, "伪造交换机")
    write(factory, b, [Point("if.in_bps:eth0", 1000.0, "bit/s")], now - timedelta(seconds=3))
    with factory() as db:
        got = poll(db, state, FAST)
    assert [d["asset_id"] for n, d in got if n == "metrics"] == [str(b.id)]


def test_last_event_id_resume(factory) -> None:  # type: ignore[no-untyped-def]
    a, ep = add_asset(factory)
    t0 = datetime.now(UTC) - timedelta(seconds=60)
    write(factory, a, [Point("cpu.load", 1.0, "%")], t0)
    set_status(factory, a, ep, "ok", t0)
    cursor = Cursor.parse(Cursor(us(t0), us(t0)).encode())
    assert cursor is not None
    # 断线期间发生的变化
    t1 = t0 + timedelta(seconds=30)
    write(factory, a, [Point("cpu.load", 2.0, "%")], t1)
    set_status(factory, a, ep, "failed", t1)
    frames = asyncio.run(run_stream(factory, cursor=cursor, until=lambda f: bool(named(f, "tasks"))))
    assert [i["value"] for e in named(frames, "metrics") for i in e["data"]["items"]] == [2.0]  # 不重发 t0
    coll = named(frames, "collection")
    assert coll and coll[0]["data"]["snapshot"] is False and coll[0]["data"]["items"][0]["state"] == "failed"
    ids = [e["id"] for e in frames if "id" in e]
    assert len(ids) == 1 and "id" in frames[-1]  # 只在一轮的最后一个事件上带 id
    resumed = Cursor.parse(ids[0])
    assert resumed and resumed.metrics_us == us(t1) and resumed.collection_us == us(t1)


@pytest.mark.parametrize("value", ["", "garbage", "m1.c", "m1.c2;drop", Cursor(us(datetime.now(UTC) + timedelta(days=1)), 0).encode()])
def test_bad_last_event_id_means_fresh_connection(value: str) -> None:
    assert Cursor.parse(value) is None


def test_stale_on_resume(factory) -> None:  # type: ignore[no-untyped-def]
    a, _ = add_asset(factory)
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    write(factory, a, [Point("cpu.load", 9.0, "%")], t0)
    state = StreamState.start(Cursor(us(t0 - timedelta(seconds=1)), 0), FAST)
    with factory() as db:
        item = next(d for n, d in poll(db, state, FAST) if n == "metrics")["items"][0]
    assert item["quality"] == "stale" and item["value"] is None  # 过期不显示旧值，也不显示 0


def test_heartbeat_when_idle(factory) -> None:  # type: ignore[no-untyped-def]
    frames = asyncio.run(run_stream(factory, until=lambda f: sum(e.get("comment") == "ping" for e in f) >= 2))
    assert sum(e.get("comment") == "ping" for e in frames) >= 2


def test_task_counts_on_change(factory) -> None:  # type: ignore[no-untyped-def]
    def worker() -> None:
        with factory() as db:
            db.add(Task(kind="collect.asset", status="queued"))
            db.commit()

    frames = asyncio.run(run_stream(factory, during=worker, until=lambda f: len(named(f, "tasks")) >= 2))
    assert [e["data"]["counts"] for e in named(frames, "tasks")] == [{}, {"queued": 1}]


def test_payload_is_capped(factory) -> None:  # type: ignore[no-untyped-def]
    a, _ = add_asset(factory)
    cfg = StreamConfig(max_event_bytes=2000)
    state = StreamState.start(None, cfg, now=datetime.now(UTC) - timedelta(seconds=5))
    write(factory, a, [Point(f"fan.rpm:FAN{i:03d}", float(i), "rpm") for i in range(200)], datetime.now(UTC))
    with factory() as db:
        events = [d for n, d in poll(db, state, cfg) if n == "metrics"]
    assert len(events) > 1
    assert all(len(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode()) <= 2000 for d in events)
    assert sum(len(d["items"]) for d in events) == 200


def test_stops_on_disconnect(factory) -> None:  # type: ignore[no-untyped-def]
    async def run() -> None:
        gen = event_stream(factory, cursor=None, cfg=StreamConfig(poll_seconds=0.05, heartbeat_seconds=60))
        assert (await gen.__anext__()).startswith("retry:")
        while "event: tasks" not in await gen.__anext__():  # 第一轮的最后一个事件
            pass
        pending = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.2)
        pending.cancel()  # 客户端断开时 Starlette 取消正在等待的流
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(run())


def test_should_stop_ends_stream(factory) -> None:  # type: ignore[no-untyped-def]
    async def run() -> list[str]:
        return [c async for c in event_stream(factory, cursor=None, cfg=FAST, should_stop=lambda: True)]

    assert asyncio.run(run()) == ["retry: 3000\n: 云枢实时推送\n\n"]
