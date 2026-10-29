"""数据库备份与恢复（落地方案 P1）。

轻量版为 SQLite 文件库（本文件），标准版为 PostgreSQL（pg_dump，见 app/backup_pg.py，4.1 P8）。
对外统一用 backup_database / verify_backup / restore_database，按连接串自动选择。

    python -m app.backup create                 # 立即备份
    python -m app.backup list                   # 列出备份
    python -m app.backup verify <备份文件>       # 校验完整性
    python -m app.backup restore <备份文件>      # 恢复（必须先停止服务）

- 用 SQLite 在线备份接口，服务运行中也能得到一致的快照；备份后做 integrity_check。
- 每份备份旁边有一个同名 .json，记录时间、原因、迁移版本、大小、SHA-256 和恢复凭据所需的主密钥编号。
- **备份不含主密钥**。恢复时如果当前密钥文件里缺少备份需要的密钥编号，会提示：
  资产可以恢复，但已保存的设备密码无法解密，需要重新录入或先恢复对应的密钥文件。
- 恢复前自动把当前数据库再备份一份（原因 pre-restore），恢复后升级到最新迁移。
- 保留策略：定时备份保留最近 N 份（默认 7），迁移前 / 恢复前 / 手动备份各保留最近 10 份。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import make_url

REASONS = ("scheduled", "manual", "pre-migrate", "pre-restore")
DATA_SUFFIXES = (".db", ".dump")  # SQLite 文件副本 / PostgreSQL 自定义格式备份
KEEP = {"scheduled": 7, "manual": 10, "pre-migrate": 10, "pre-restore": 10}


class BackupError(RuntimeError):
    pass


@dataclass
class BackupInfo:
    name: str
    created_at: str
    reason: str
    size: int
    sha256: str
    alembic_revision: str | None
    key_ids: list[str]
    asset_count: int


def sqlite_path(engine_or_url: Engine | str) -> Path:
    url = make_url(engine_or_url.url if isinstance(engine_or_url, Engine) else engine_or_url)
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        raise BackupError("只支持文件型 SQLite 数据库的备份")
    return Path(url.database)


def database_kind(database_url: str | Engine) -> str:
    """sqlite（本地文件库）或 postgresql（外部数据库）。"""
    url = make_url(database_url.url if isinstance(database_url, Engine) else database_url)
    return url.get_backend_name()


def _url_text(database_url: str | Engine) -> str:
    return database_url.url.render_as_string(hide_password=False) if isinstance(database_url, Engine) else database_url


def default_backup_dir(data_dir: Path) -> Path:
    return data_dir / "backups"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inspect_db(path: Path) -> tuple[str | None, list[str], int]:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError(f"备份文件损坏：{path.name}")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rev = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] if "alembic_version" in tables else None
        keys = [r[0] for r in conn.execute("SELECT DISTINCT key_id FROM credentials ORDER BY key_id")] if "credentials" in tables else []
        assets = conn.execute("SELECT count(*) FROM assets").fetchone()[0] if "assets" in tables else 0
        return rev, keys, assets
    except (sqlite3.DatabaseError, UnicodeDecodeError) as exc:  # 损坏的页可能让文本无法解码
        raise BackupError(f"不是有效的 SQLite 数据库：{path.name}") from exc
    finally:
        conn.close()


def create_backup(db_path: Path, backup_dir: Path, reason: str = "manual") -> BackupInfo:
    if reason not in REASONS:
        raise ValueError(reason)
    if not db_path.exists():
        raise BackupError("数据库文件不存在")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    target = backup_dir / f"yunshu-{stamp}-{reason}.db"
    tmp = target.with_suffix(".db.part")
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        rev, keys, assets = _inspect_db(tmp)
    except BackupError:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, target)
    info = BackupInfo(
        name=target.name,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        reason=reason,
        size=target.stat().st_size,
        sha256=_sha256(target),
        alembic_revision=rev,
        key_ids=keys,
        asset_count=assets,
    )
    target.with_suffix(".json").write_text(json.dumps(asdict(info), ensure_ascii=False, indent=2), encoding="utf-8")
    prune(backup_dir, reason)
    return info


def list_backups(backup_dir: Path) -> list[BackupInfo]:
    if not backup_dir.exists():
        return []
    out = []
    for meta in sorted(backup_dir.glob("yunshu-*.json"), reverse=True):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            if any(meta.with_suffix(s).exists() for s in DATA_SUFFIXES):
                out.append(BackupInfo(**data))
        except (ValueError, TypeError):
            continue
    return out


def prune(backup_dir: Path, reason: str) -> None:
    keep = KEEP.get(reason, 10)
    same = [b for b in list_backups(backup_dir) if b.reason == reason]
    for old in same[keep:]:
        (backup_dir / old.name).unlink(missing_ok=True)
        (backup_dir / old.name).with_suffix(".json").unlink(missing_ok=True)


def resolve_backup(backup_dir: Path, name_or_path: str) -> Path:
    path = Path(name_or_path)
    if not path.is_absolute() and not path.exists():
        path = backup_dir / name_or_path
    if not path.is_file():
        raise BackupError(f"找不到备份文件：{name_or_path}")
    return path


def verify_backup(path: Path) -> BackupInfo | None:
    """校验备份；有元数据时同时核对 SHA-256。返回元数据（没有则 None）。"""
    if path.suffix == ".dump":
        from app import backup_pg

        try:
            backup_pg.check_archive(path)
        except backup_pg.PgToolError as exc:
            raise BackupError(str(exc)) from exc
    else:
        _inspect_db(path)
    meta = path.with_suffix(".json")
    if meta.exists():
        info = BackupInfo(**json.loads(meta.read_text(encoding="utf-8")))
        if info.sha256 != _sha256(path):
            raise BackupError("备份文件与记录的 SHA-256 不一致，可能已被改动")
        return info
    return None


@dataclass
class RestoreResult:
    restored_from: str
    safety_backup: str | None
    missing_key_ids: list[str]


def restore_backup(backup: Path, db_path: Path, backup_dir: Path, available_key_ids: set[str]) -> RestoreResult:
    """把 backup 恢复到 db_path。调用方必须保证服务已停止（没有其他连接）。"""
    verify_backup(backup)
    _, keys, _ = _inspect_db(backup)
    missing = sorted(set(keys) - available_key_ids)
    safety = create_backup(db_path, backup_dir, "pre-restore").name if db_path.exists() else None
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    tmp = db_path.with_name(db_path.name + ".restoring")
    shutil.copyfile(backup, tmp)
    os.replace(tmp, db_path)
    return RestoreResult(restored_from=backup.name, safety_backup=safety, missing_key_ids=missing)


def _db_facts(engine: Engine) -> tuple[str | None, list[str], int]:
    """在线数据库的迁移版本、凭据密钥编号与资产数（PostgreSQL 备份的元数据）。"""
    tables = set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar() if "alembic_version" in tables else None
        keys = [r[0] for r in conn.execute(text("SELECT DISTINCT key_id FROM credentials ORDER BY key_id"))] if "credentials" in tables else []
        assets = conn.execute(text("SELECT count(*) FROM assets")).scalar_one() if "assets" in tables else 0
    return rev, keys, int(assets)


def _create_pg_backup(database_url: str, backup_dir: Path, reason: str) -> BackupInfo:
    from sqlalchemy import create_engine

    from app import backup_pg

    if reason not in REASONS:
        raise ValueError(reason)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    target = backup_dir / f"yunshu-{stamp}-{reason}.dump"
    tmp = target.with_suffix(".dump.part")
    engine = create_engine(database_url)
    try:
        backup_pg.dump(database_url, tmp)
        backup_pg.check_archive(tmp)
        rev, keys, assets = _db_facts(engine)
    except backup_pg.PgToolError as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(str(exc)) from exc
    finally:
        engine.dispose()
    os.replace(tmp, target)
    info = BackupInfo(
        name=target.name, created_at=datetime.now(UTC).isoformat(timespec="seconds"), reason=reason,
        size=target.stat().st_size, sha256=_sha256(target), alembic_revision=rev, key_ids=keys, asset_count=assets,
    )
    target.with_suffix(".json").write_text(json.dumps(asdict(info), ensure_ascii=False, indent=2), encoding="utf-8")
    prune(backup_dir, reason)
    return info


def backup_database(database_url: str | Engine, backup_dir: Path, reason: str = "manual") -> BackupInfo:
    """按数据库类型备份：SQLite 在线复制文件，PostgreSQL 调用 pg_dump。"""
    kind = database_kind(database_url)
    if kind == "sqlite":
        return create_backup(sqlite_path(database_url), backup_dir, reason)
    if kind == "postgresql":
        return _create_pg_backup(_url_text(database_url), backup_dir, reason)
    raise BackupError(f"不支持备份 {kind} 数据库")


def restore_database(backup: Path, database_url: str, backup_dir: Path, available_key_ids: set[str]) -> RestoreResult:
    """恢复到当前数据库。调用方必须保证服务已停止；备份类型必须与当前数据库一致。"""
    kind = database_kind(database_url)
    if (kind == "postgresql") != (backup.suffix == ".dump"):
        raise BackupError("备份类型与当前数据库不一致：SQLite 备份只能恢复到 SQLite，PostgreSQL 备份（.dump）只能恢复到 PostgreSQL")
    if kind == "sqlite":
        return restore_backup(backup, sqlite_path(database_url), backup_dir, available_key_ids)
    from sqlalchemy import create_engine

    from app import backup_pg

    verify_backup(backup)
    safety = backup_database(database_url, backup_dir, "pre-restore").name
    try:
        backup_pg.restore(database_url, backup)
    except backup_pg.PgToolError as exc:
        raise BackupError(f"{exc}（恢复前的数据库已另存为 {safety}）") from exc
    engine = create_engine(database_url)
    try:
        _, keys, _ = _db_facts(engine)
    finally:
        engine.dispose()
    return RestoreResult(restored_from=backup.name, safety_backup=safety, missing_key_ids=sorted(set(keys) - available_key_ids))


def backup_before_migration(engine: Engine, backup_dir: Path, head: str | None) -> BackupInfo | None:
    """库里已有数据且版本落后于 head 时先备份；新库或已是最新则跳过。"""
    if database_kind(engine) == "postgresql":
        rev, _, _ = _db_facts(engine)
        return None if rev is None or rev == head else backup_database(engine, backup_dir, "pre-migrate")
    try:
        path = sqlite_path(engine)
    except BackupError:
        return None
    if not path.exists():
        return None
    tables = set(inspect(engine).get_table_names())
    if "alembic_version" not in tables:
        return None
    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    if current is None or current == head:
        return None
    return create_backup(path, backup_dir, "pre-migrate")


def main(argv: list[str]) -> int:
    from app import migrate
    from app.crypto import KeyFileError, load_or_create_keyring
    from app.db import make_engine
    from app.settings import Settings

    parser = argparse.ArgumentParser(description="云枢 4.0 数据库备份与恢复")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create", help="立即备份")
    sub.add_parser("list", help="列出备份")
    v = sub.add_parser("verify", help="校验备份")
    v.add_argument("backup")
    r = sub.add_parser("restore", help="恢复（必须先停止服务）")
    r.add_argument("backup")
    r.add_argument("--yes", action="store_true", help="确认已停止服务并覆盖当前数据库")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    bdir = settings.backup_dir
    try:
        if args.cmd == "create":
            info = backup_database(settings.database_url, bdir, "manual")
            print(f"已备份：{bdir / info.name}（资产 {info.asset_count} 台）")
            print("提醒：备份不含主密钥，请另外用 `python -m app.keytool export` 备份密钥文件。")
        elif args.cmd == "list":
            for b in list_backups(bdir):
                print(f"{b.name}\t{b.reason}\t{b.created_at}\t资产 {b.asset_count}\t迁移 {b.alembic_revision}")
        elif args.cmd == "verify":
            info = verify_backup(resolve_backup(bdir, args.backup))
            print("校验通过" + (f"，密钥编号 {', '.join(info.key_ids) or '无'}" if info else "（无元数据）"))
        else:
            if not args.yes:
                print("恢复会覆盖当前数据库。请先停止服务，然后加 --yes 重新执行。")
                return 2
            try:
                ring = load_or_create_keyring(settings.master_key_file, data_dir=settings.data_dir, create=False)
                available = set(ring.keys)
            except KeyFileError:
                available = set()
            result = restore_database(resolve_backup(bdir, args.backup), settings.database_url, bdir, available)
            engine = make_engine(settings.database_url)
            try:
                migrate.upgrade(engine)
            finally:
                engine.dispose()
            print(f"已从 {result.restored_from} 恢复。")
            if result.safety_backup:
                print(f"恢复前的数据库已另存为 {result.safety_backup}。")
            if result.missing_key_ids:
                print(
                    "注意：当前密钥文件缺少备份所需的主密钥（编号 " + ", ".join(result.missing_key_ids) + "）。"
                    "资产已恢复，但已保存的设备密码无法解密；请恢复对应的密钥文件，或在凭据管理里重新录入密码。"
                )
    except BackupError as exc:
        print(f"错误：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
