"""SQLite → PostgreSQL 迁移工具（4.1 P10）。完整往返只在 PostgreSQL 上运行，全部为假数据。"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from app import metrics, migrate, queue, upgrade_to_pg
from app.crypto import CredentialCipher, load_or_create_keyring
from app.db import make_engine, make_session_factory
from app.models import (Asset, AssetRelation, Credential, Dashboard, LoginSession, ManagementEndpoint, Site, Task, User,
                        WorkerHeartbeat)
from app.models.control import ControlOperation
from app.services.credentials import create_credential, reveal_secret
from tests.api_helpers import add_user
from tests.conftest import PG_ADMIN_URL

pg_only = pytest.mark.skipif(not PG_ADMIN_URL, reason="只在 PostgreSQL 上运行")
SECRET = "fake-upgrade-secret"
DAY1 = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)


@pytest.fixture
def source(tmp_path: Path):  # type: ignore[no-untyped-def]
    """内容齐全的假 SQLite 源库：多级场所、资产 / 端点 / 凭据 / 关系、布局与资源排序、两天的指标与汇总、任务、会话。"""
    url = f"sqlite:///{(tmp_path / 'data' / 'yunshu.db').as_posix()}"
    key = tmp_path / "keys" / "master.key"
    engine = make_engine(url)
    migrate.upgrade(engine)
    factory = make_session_factory(engine)
    cipher = CredentialCipher(load_or_create_keyring(key))
    add_user(factory, "fake-upgrade-admin", "admin")
    with factory() as db:
        root = Site(name="假总部", kind="datacenter")
        db.add(root)
        db.flush()
        mid = Site(name="假机房", kind="room", parent_id=root.id)
        db.add(mid)
        db.flush()
        db.add(Site(name="假机柜区", kind="room", parent_id=mid.id))
        cred = create_credential(db, cipher, alias="假凭据", kind="password", secret=SECRET, username="root")
        host = Asset(name="假宿主", form="physical", role="server", site_id=mid.id, tags=["lab", "核心"])
        host.endpoints.append(ManagementEndpoint(address="192.0.2.31", protocol="ssh", credential_id=cred.id))
        vm = Asset(name="假虚拟机", form="vm", role="server")
        db.add_all([host, vm])
        db.flush()
        db.add(AssetRelation(source_asset_id=host.id, target_asset_id=vm.id, kind="hosts"))
        db.add(Dashboard(key="overview-resources", name="资源全景排序", data={"groups": ["server"], "order": [str(vm.id), str(host.id)], "children": {}}, revision=3, updated_by="fake"))
        for day in (DAY1, DAY1 + timedelta(days=1)):
            metrics.write_sample(db, host.id, "ssh", [metrics.Point("cpu.load", 12.5, "%"), metrics.Point("mem.used_pct", None, "%", "unsupported")], observed_at=day)
        metrics.rollup(db, now=DAY1 + timedelta(days=1, hours=2))
        done = queue.enqueue(db, "collect.asset", asset_id=host.id)
        done.status, done.finished_at = "succeeded", DAY1
        queue.enqueue(db, "collect.asset", asset_id=vm.id)
        user = db.scalar(select(User))
        db.add(LoginSession(token_hash="f" * 64, csrf_token="c" * 64, user_id=user.id, expires_at=DAY1 + timedelta(days=30)))
        db.commit()
        ids = {"host": host.id, "vm": vm.id, "cred": cred.id, "key": cipher.current_key_id}
    engine.dispose()
    return url, key, ids, tmp_path / "backups"


@pytest.fixture
def target():  # type: ignore[no-untyped-def]
    if not PG_ADMIN_URL:
        pytest.skip("只在 PostgreSQL 上运行")
    admin = create_engine(make_url(PG_ADMIN_URL), isolation_level="AUTOCOMMIT")
    name = f"yunshu_up_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    yield make_url(PG_ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _sha(url: str) -> str:
    return hashlib.sha256(Path(make_url(url).database).read_bytes()).hexdigest()


def test_precheck_rejects_non_postgres_target_and_missing_source(tmp_path: Path) -> None:
    rep = upgrade_to_pg.precheck(f"sqlite:///{(tmp_path / 'missing.db').as_posix()}", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    assert not rep.ok and any("目标必须是 PostgreSQL" in b for b in rep.blockers) and any("源库文件不存在" in b for b in rep.blockers)


@pg_only
def test_full_round_trip_keeps_everything_and_leaves_source_untouched(source, target) -> None:  # type: ignore[no-untyped-def]
    url, key, ids, backups = source
    before = _sha(url)
    rep = upgrade_to_pg.precheck(url, target, master_key_file=key)
    assert rep.ok, rep.blockers
    assert rep.counts["assets"] == 2 and rep.counts["sites"] == 3 and len(rep.raw_tables) == 2 and rep.key_ids == [ids["key"]]
    upgrade_to_pg.run(url, target, backups, master_key_file=key)
    assert _sha(url) == before  # 源库只读
    assert [p.name for p in backups.glob("*-pre-migrate.db")]  # 迁移前备份
    eng = make_engine(target)
    try:
        migrate.ensure_current(eng)
        with make_session_factory(eng)() as db:
            for table, n in rep.counts.items():
                if table not in upgrade_to_pg.SKIP:
                    assert db.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one() == n, table
            assert db.scalar(select(func.count()).select_from(LoginSession)) == 0  # 会话不迁移
            assert reveal_secret(db.get(Credential, ids["cred"]), CredentialCipher(load_or_create_keyring(key, create=False))) == SECRET
            host = db.get(Asset, ids["host"])
            assert host.tags == ["lab", "核心"] and host.endpoints[0].credential_id == ids["cred"]
            site = db.get(Site, host.site_id)
            assert site.name == "假机房" and db.get(Site, site.parent_id).name == "假总部"
            assert db.scalar(select(Dashboard.revision).where(Dashboard.key == "overview-resources")) == 3
            assert [(v.key, v.value, v.quality) for v in metrics.latest(db, ids["host"], now=DAY1 + timedelta(days=1))][0][1] == 12.5
            assert set(metrics.raw_tables(db)) == set(rep.raw_tables)
            assert db.scalar(select(Task.status).where(Task.asset_id == ids["vm"])) == "queued"  # 排队的读任务照常迁移
            # 序列已校正：迁移后继续写同一天的原始数据不会主键冲突
            metrics.write_sample(db, ids["host"], "ssh", [metrics.Point("cpu.load", 30, "%")], observed_at=DAY1 + timedelta(hours=1))
            db.commit()
            assert db.execute(text(f'SELECT count(*) FROM "{metrics.raw_table_name(DAY1.date())}"')).scalar_one() == 2
    finally:
        eng.dispose()
    # 重复执行：目标已非空，预检阻断，不会重复导入
    again = upgrade_to_pg.precheck(url, target, master_key_file=key)
    assert not again.ok and any("已有数据" in b for b in again.blockers)


@pg_only
def test_failure_rolls_back_to_empty_target_and_rerun_succeeds(source, target) -> None:  # type: ignore[no-untyped-def]
    url, key, _, backups = source
    with pytest.raises(upgrade_to_pg.UpgradeError, match="测试注入"):
        upgrade_to_pg.run(url, target, backups, master_key_file=key, fail_after="assets")
    eng = create_engine(target)
    with eng.connect() as conn:
        assert upgrade_to_pg._target_not_empty(conn) == []  # 结构最新、没有数据
    eng.dispose()
    assert upgrade_to_pg.precheck(url, target, master_key_file=key).ok
    upgrade_to_pg.run(url, target, backups, master_key_file=key)


@pg_only
def test_blockers_running_task_open_control_fresh_worker_unknown_table_and_old_source(source, target) -> None:  # type: ignore[no-untyped-def]
    url, key, ids, _ = source
    engine = make_engine(url)
    with make_session_factory(engine)() as db:
        t = queue.enqueue(db, "collect.asset")
        t.status = "running"
        db.add(ControlOperation(asset_id=ids["host"], kind="power", action="on", status="queued"))
        queue.beat_worker(db, "fake-host:1:x", role="all", scheduler=True, started_at=datetime.now(UTC))
        db.commit()
    tgt = create_engine(target)
    with tgt.begin() as conn:
        conn.execute(text("CREATE TABLE fake_other (id int)"))
    tgt.dispose()
    rep = upgrade_to_pg.precheck(url, target, master_key_file=key)
    text_all = "；".join(rep.blockers)
    for expected in ("执行中的任务", "未完成的控制操作", "Worker 心跳", "不认识的表 fake_other"):
        assert expected in text_all, expected
    with pytest.raises(upgrade_to_pg.UpgradeError):
        upgrade_to_pg.run(url, target, Path("unused"), master_key_file=key)
    migrate.downgrade(engine, "-1")
    engine.dispose()
    old = upgrade_to_pg.precheck(url, target, master_key_file=key)
    assert any("请先对源库运行" in b for b in old.blockers)
    assert WorkerHeartbeat  # 模型可用（心跳阻断依赖它）
