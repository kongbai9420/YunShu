"""从 SQLite 轻量版升级到 PostgreSQL 标准版（4.1 P10）。

    python -m app.upgrade_to_pg precheck --target <PostgreSQL 连接串>     # 只读预检
    python -m app.upgrade_to_pg run --target <PostgreSQL 连接串> --yes    # 执行（服务必须已停止）

- 源库只以只读方式打开；执行前先做 pre-migrate 备份。主密钥文件不复制、不改动。
- 目标只允许空库：任一表有数据或存在不认识的表即阻断，不合并、不覆盖。
- 复制与核对在目标的一个事务里完成：失败整体回滚，目标回到结构最新的空库，可直接重跑。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, DateTime, Engine, create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import metrics, migrate
from app.models import Base

SKIP = ("login_sessions", "scheduler_locks", "worker_heartbeats")  # 运行期状态，不迁移
RAW = re.compile(rf"{metrics.RAW_PREFIX}\d{{8}}")
BATCH = 1000
SAMPLE = 50
OPEN_CONTROL = ("queued", "running")
# 结构迁移会预置两个默认拓扑视图（0010）：目标里只有这些且未改动（修订 1）仍算空库；导入前在同一事务里清掉，以源库为准
SEEDED = {"topology_views": "SELECT count(*) FROM topology_views WHERE NOT (name IN ('家庭网络', '机房') AND revision = 1)"}


class UpgradeError(RuntimeError):
    pass


@dataclass
class Report:
    source: str
    target: str
    head: str | None
    source_revision: str | None = None
    target_revision: str | None = None
    source_bytes: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    raw_tables: dict[str, int] = field(default_factory=dict)
    key_ids: list[str] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers


def _safe(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)


def readonly_source(source_url: str) -> Engine:
    """只读打开 SQLite 文件：迁移过程中不可能改动源库。"""
    url = make_url(source_url)
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        raise UpgradeError("源库必须是 SQLite 文件库")
    path = Path(url.database)
    if not path.is_file():
        raise UpgradeError(f"源库文件不存在：{path}")
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False))


def _tables() -> list[Any]:
    return [t for t in Base.metadata.sorted_tables if t.name not in SKIP]


def _revision(conn: Connection) -> str | None:
    if "alembic_version" not in inspect(conn).get_table_names():
        return None
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _available_keys(master_key_file: Path | None) -> set[str] | None:
    if master_key_file is None:
        return None
    from app.crypto import KeyFileError, load_or_create_keyring

    try:
        return set(load_or_create_keyring(master_key_file, create=False).keys)
    except KeyFileError:
        return set()


def precheck(source_url: str, target_url: str, *, master_key_file: Path | None = None, now: datetime | None = None) -> Report:
    """只读：源与目标都不写。"""
    from app.models import ControlOperation, Task, WorkerHeartbeat

    now = now or datetime.now(UTC)
    rep = Report(source=_safe(source_url), target=_safe(target_url), head=migrate.head_revision())
    if make_url(target_url).get_backend_name() != "postgresql":
        rep.blockers.append("目标必须是 PostgreSQL")
    try:
        src = readonly_source(source_url)
    except UpgradeError as exc:
        rep.blockers.append(str(exc))
        return rep
    rep.source_bytes = Path(make_url(source_url).database).stat().st_size
    try:
        with src.connect() as conn:
            rep.source_revision = _revision(conn)
            if rep.source_revision != rep.head:
                rep.blockers.append(f"源库结构版本为 {rep.source_revision or '未初始化'}，程序为 {rep.head}：请先对源库运行 python -m app.migrate（会先自动备份）")
                return rep
            names = set(inspect(conn).get_table_names())
            for t in _tables():
                rep.counts[t.name] = conn.execute(select(func.count()).select_from(t)).scalar_one()
            for name in sorted(n for n in names if RAW.fullmatch(n)):
                rep.raw_tables[name] = conn.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one()
            running = conn.execute(select(func.count()).select_from(Task).where(Task.status == "running")).scalar_one()
            if running:
                rep.blockers.append(f"有 {running} 个执行中的任务：请先停止服务，等租约过期或任务结束后再迁移")
            open_ops = conn.execute(select(func.count()).select_from(ControlOperation).where(ControlOperation.status.in_(OPEN_CONTROL))).scalar_one()
            if open_ops:
                rep.blockers.append(f"有 {open_ops} 个未完成的控制操作：请在旧环境中处理完再迁移（迁移绝不重放控制命令）")
            if "worker_heartbeats" in names:
                fresh = [s for s in conn.execute(select(WorkerHeartbeat.last_seen)).scalars()
                         if (now - (s if s.tzinfo else s.replace(tzinfo=UTC))) < timedelta(seconds=60)]
                if fresh:
                    rep.blockers.append("源库 60 秒内有 Worker 心跳：服务仍在运行，请先停止 API 与 Worker")
            rep.key_ids = [k for k in conn.execute(text("SELECT DISTINCT key_id FROM credentials ORDER BY key_id")).scalars()]
    finally:
        src.dispose()
    available = _available_keys(master_key_file)
    if available is not None:
        rep.missing_keys = sorted(set(rep.key_ids) - available)
        if rep.missing_keys:
            rep.warnings.append(f"当前密钥文件缺少编号 {', '.join(rep.missing_keys)}：迁移后这些凭据仍无法解密（与迁移前相同）")
    if make_url(target_url).get_backend_name() == "postgresql":
        try:
            tgt = create_engine(target_url)
            with tgt.connect() as conn:
                rep.target_revision = _revision(conn)
                rep.blockers += _target_not_empty(conn)
            tgt.dispose()
        except Exception as exc:  # noqa: BLE001 - 连不上目标就是阻断原因
            rep.blockers.append(f"连接目标失败：{type(exc).__name__}: {str(exc)[:300]}")
    return rep


def _target_not_empty(conn: Connection) -> list[str]:
    known = {t.name for t in Base.metadata.sorted_tables} | {"alembic_version"}
    out = []
    for name in inspect(conn).get_table_names():
        if name not in known and not RAW.fullmatch(name):
            out.append(f"目标库里有本程序不认识的表 {name}：只允许迁移到空库")
        elif name in SEEDED:
            if conn.execute(text(SEEDED[name])).scalar():
                out.append(f"目标库的表 {name} 已有数据：只允许迁移到空库，不合并、不覆盖")
        elif name != "alembic_version" and conn.execute(text(f'SELECT EXISTS (SELECT 1 FROM "{name}")')).scalar():
            out.append(f"目标库的表 {name} 已有数据：只允许迁移到空库，不合并、不覆盖")
    return out


def _utc(table: Any, row: dict[str, Any]) -> dict[str, Any]:
    for c in table.columns:
        v = row.get(c.name)
        if isinstance(c.type, DateTime) and isinstance(v, datetime) and v.tzinfo is None:
            row[c.name] = v.replace(tzinfo=UTC)
    return row


def _rows(src: Connection, table: Any) -> list[dict[str, Any]]:
    rows = [dict(r._mapping) for r in src.execute(select(table))]
    if table.name == "sites":  # 自引用：上级先、下级后
        done: set[Any] = set()
        ordered: list[dict[str, Any]] = []
        while rows:
            ready = [r for r in rows if r["parent_id"] is None or r["parent_id"] in done]
            if not ready:
                raise UpgradeError("场所的上级关系有环，无法迁移")
            ordered += ready
            done |= {r["id"] for r in ready}
            rows = [r for r in rows if r["id"] not in done]
        rows = ordered
    return [_utc(table, r) for r in rows]


def _copy(src: Connection, dst: Connection, table: Any) -> int:
    rows = _rows(src, table)
    for i in range(0, len(rows), BATCH):
        dst.execute(table.insert(), rows[i:i + BATCH])
    return len(rows)


def _sample_equal(src: Connection, dst: Connection, table: Any) -> bool:
    pk = list(table.primary_key.columns)
    a = [_utc(table, dict(r._mapping)) for r in src.execute(select(table).order_by(*pk).limit(SAMPLE))]
    b = [_utc(table, dict(r._mapping)) for r in dst.execute(select(table).order_by(*[dst_c for dst_c in pk]).limit(SAMPLE))]
    # 两库对 UUID 的排序可能不同：按主键对齐后比较
    key = lambda r: tuple(str(r[c.name]) for c in pk)  # noqa: E731
    return {key(r): r for r in a} == {key(r): r for r in b} if len(a) == len(b) else False


def run(source_url: str, target_url: str, backup_dir: Path, *, master_key_file: Path | None = None, fail_after: str | None = None) -> Report:
    """执行迁移。fail_after 仅供测试：复制完该表后故意失败，验证整体回滚。"""
    from app.backup import create_backup, sqlite_path

    rep = precheck(source_url, target_url, master_key_file=master_key_file)
    if not rep.ok:
        raise UpgradeError("预检未通过：" + "；".join(rep.blockers))
    create_backup(sqlite_path(source_url), backup_dir, "pre-migrate")
    tgt = create_engine(target_url)
    migrate.upgrade(tgt)
    src = readonly_source(source_url)
    try:
        with src.connect() as s, tgt.begin() as d:
            if _target_not_empty(d):
                raise UpgradeError("目标库在预检之后被写入了数据，已停止")
            for name in SEEDED:
                d.execute(text(f'DELETE FROM "{name}"'))
            for table in _tables():
                if _copy(s, d, table) != rep.counts[table.name]:
                    raise UpgradeError(f"表 {table.name} 复制后行数不一致")
                if fail_after == table.name:
                    raise UpgradeError(f"测试注入的失败（{table.name} 之后）")
            for name, count in rep.raw_tables.items():
                raw = metrics._raw_table(name)
                raw.create(d, checkfirst=True)
                if _copy(s, d, raw) != count:
                    raise UpgradeError(f"按日原始指标表 {name} 复制后行数不一致")
                d.execute(text(f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), (SELECT COALESCE(max(id), 1) FROM \"{name}\"), (SELECT count(*) > 0 FROM \"{name}\"))"))
            # 核对：在提交前完成，任何不一致都整体回滚
            for table in _tables():
                n = d.execute(select(func.count()).select_from(table)).scalar_one()
                if n != rep.counts[table.name] or not _sample_equal(s, d, table):
                    raise UpgradeError(f"核对失败：表 {table.name}")
            _check_credentials(d, master_key_file, rep)
    finally:
        src.dispose()
        tgt.dispose()
    return rep


def _check_credentials(conn: Connection, master_key_file: Path | None, rep: Report) -> None:
    if master_key_file is None or not rep.counts.get("credentials"):
        return
    from app.crypto import CredentialCipher, DecryptionError, KeyFileError, load_or_create_keyring
    from app.models import Credential
    from app.services.credentials import reveal_secret

    try:
        cipher = CredentialCipher(load_or_create_keyring(master_key_file, create=False))
    except KeyFileError:
        rep.warnings.append("没有找到密钥文件，未核对凭据解密")
        return
    bad = 0
    with Session(bind=conn) as db:
        for cred in db.scalars(select(Credential).where(Credential.key_id.not_in(rep.missing_keys or [""]))):
            try:
                reveal_secret(cred, cipher)
            except DecryptionError:
                bad += 1
    if bad:
        raise UpgradeError(f"有 {bad} 条凭据在目标库无法用当前密钥解密")


def _print(rep: Report) -> None:
    print(f"源：{rep.source}（{rep.source_bytes} 字节，结构版本 {rep.source_revision}）")
    print(f"目标：{rep.target}（结构版本 {rep.target_revision or '空'}；程序 {rep.head}）")
    print("各表行数：" + "，".join(f"{k} {v}" for k, v in rep.counts.items() if v))
    print(f"按日原始指标表：{len(rep.raw_tables)} 张，共 {sum(rep.raw_tables.values())} 行")
    print(f"凭据所需主密钥编号：{', '.join(rep.key_ids) or '无'}")
    for w in rep.warnings:
        print(f"注意：{w}")
    for b in rep.blockers:
        print(f"阻断：{b}")


def main(argv: list[str]) -> int:
    from app.settings import Settings

    parser = argparse.ArgumentParser(description="从 SQLite 轻量版升级到 PostgreSQL 标准版")
    parser.add_argument("cmd", choices=("precheck", "run"))
    parser.add_argument("--target", required=True, help="PostgreSQL 连接串，例如 postgresql+psycopg://用户@主机:5432/库名")
    parser.add_argument("--source", help="SQLite 连接串，默认取当前配置")
    parser.add_argument("--yes", action="store_true", help="确认服务已停止并执行迁移")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    source = args.source or settings.database_url
    try:
        if args.cmd == "precheck":
            rep = precheck(source, args.target, master_key_file=settings.master_key_file)
            _print(rep)
            print("预检通过，可以执行迁移。" if rep.ok else "预检未通过。")
            return 0 if rep.ok else 2
        if not args.yes:
            print("迁移前请先停止 API 与 Worker，然后加 --yes 重新执行。源库只读、不会改动。")
            return 2
        rep = run(source, args.target, settings.backup_dir, master_key_file=settings.master_key_file)
        _print(rep)
        print("迁移完成并已核对。下一步：把 YUNSHU_DATABASE_URL 改为目标库，安装标准版依赖后启动 API 与 Worker。")
        print("源 SQLite 与 pre-migrate 备份都已保留；切换后在 PostgreSQL 上新产生的数据不会自动带回源库。")
        return 0
    except UpgradeError as exc:
        print(f"错误：{exc}")
        print("目标库已回滚为空库（结构最新、没有数据），修正问题后可以直接重跑；源库未改动。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
