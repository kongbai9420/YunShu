"""PostgreSQL 备份与恢复（4.1 P8，由平台调用 pg_dump）。

- 备份：`pg_dump --format=custom`，写完用 `pg_restore --list` 校验可读，再记录 SHA-256、迁移版本、
  资产数与恢复凭据所需的主密钥编号（与 SQLite 备份同一份元数据格式，文件扩展名 .dump）。
- 恢复：只能在停服后用命令行执行；先把当前库另存一份（pre-restore），再清空 public schema、`pg_restore`。
- 工具：按数据库服务器大版本查找 pg_dump / pg_restore（客户端比服务器新时恢复还要 psql），客户端大版本必须不低于服务器；找不到或版本不够时
  明确报错，不假装成功。可用 YUNSHU_PG_BIN_DIR 指定目录。
- 口令只通过环境变量 PGPASSWORD 传给子进程，不出现在命令行参数、日志或元数据里。备份不含主密钥。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

SUFFIX = ".dump"
TIMEOUT = 3600


class PgToolError(RuntimeError):
    pass


def _url(database_url: str | URL) -> URL:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise PgToolError("不是 PostgreSQL 连接")
    return url


def _libpq(url: URL) -> tuple[list[str], dict[str, str]]:
    """转成 libpq 参数；口令放进环境变量。"""
    args = ["--dbname", url.database or "postgres"]
    if url.host:
        args += ["--host", url.host]
    if url.port:
        args += ["--port", str(url.port)]
    if url.username:
        args += ["--username", url.username]
    env = {**os.environ, "PGCONNECT_TIMEOUT": "10"}
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    return args, env


def server_major(database_url: str | URL) -> int:
    engine = create_engine(_url(database_url))
    try:
        with engine.connect() as conn:
            return int(conn.execute(text("SHOW server_version_num")).scalar_one()) // 10000
    finally:
        engine.dispose()


def _tool_major(path: str) -> int | None:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)(?:\.\d+)?", out)
    return int(m.group(1)) if m else None


def find_tool(name: str, server: int) -> str:
    """客户端大版本 ≥ 服务器大版本。依次找 YUNSHU_PG_BIN_DIR、/usr/lib/postgresql/<版本>/bin（新到旧）、PATH。"""
    candidates: list[str] = []
    if os.environ.get("YUNSHU_PG_BIN_DIR"):
        candidates.append(str(Path(os.environ["YUNSHU_PG_BIN_DIR"]) / name))
    root = Path("/usr/lib/postgresql")
    if root.is_dir():
        versions = sorted((int(p.name) for p in root.iterdir() if p.name.isdigit()), reverse=True)
        candidates += [str(root / str(v) / "bin" / name) for v in versions]
    found = shutil.which(name)
    if found:
        candidates.append(found)
    seen: list[str] = []
    for path in candidates:
        if not (Path(path).is_file() or Path(path + ".exe").is_file()):
            continue
        major = _tool_major(path)
        seen.append(f"{path}（{major or '未知'}）")
        if major is not None and major >= server:
            return path
    raise PgToolError(
        f"找不到可用的 {name}：数据库服务器为 PostgreSQL {server}，需要 {server} 或更新版本的客户端工具"
        + (f"；已找到 {'、'.join(seen)}" if seen else "") + "。可设置 YUNSHU_PG_BIN_DIR 指定目录。"
    )


def _run(cmd: list[str], env: dict[str, str]) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise PgToolError(f"{Path(cmd[0]).name} 超时") from exc
    if res.returncode != 0:
        # 只返回工具自己的错误输出（不含口令：口令只在环境变量里）
        raise PgToolError(f"{Path(cmd[0]).name} 失败：{res.stderr.strip()[-800:]}")
    return res.stdout


def dump(database_url: str | URL, target: Path) -> None:
    url = _url(database_url)
    args, env = _libpq(url)
    pg_dump = find_tool("pg_dump", server_major(url))
    _run([pg_dump, "--format=custom", "--no-owner", "--no-privileges", "--file", str(target), *args], env)


def check_archive(path: Path, server: int | None = None) -> None:
    """用 pg_restore --list 读一遍目录，确认是完整可读的自定义格式备份。"""
    tool = find_tool("pg_restore", server or 0)
    _run([tool, "--list", str(path)], dict(os.environ))


def restore(database_url: str | URL, archive: Path) -> None:
    """清空 public schema 后恢复。调用方必须保证服务已停止。"""
    url = _url(database_url)
    args, env = _libpq(url)
    pg_restore = find_tool("pg_restore", server_major(url))
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()
    server = server_major(url)
    client = _tool_major(pg_restore)
    if client is not None and client > server:
        _restore_via_sql(pg_restore, find_tool("psql", server), args, env, archive)
        return
    _run([pg_restore, "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction", *args, str(archive)], env)


# 新版客户端写出的、旧版服务器不认识的会话设置。17 版 pg_restore 恢复到 16 版服务器时会带上
# SET transaction_timeout，服务器报 unrecognized configuration parameter，整个恢复失败
_NEWER_ONLY_SETTINGS = (b"SET transaction_timeout",)


def _restore_via_sql(pg_restore: str, psql: str, args: list[str], env: dict[str, str], archive: Path) -> None:
    """客户端比服务器新时：pg_restore 只把备份转成 SQL，去掉旧服务器不认识的设置，再由 psql 在一个事务里执行。
    逐行转发，不把整份 SQL 读进内存。"""
    reader = subprocess.Popen([pg_restore, "--no-owner", "--no-privileges", "--file", "-", str(archive)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    writer = subprocess.Popen([psql, "--no-psqlrc", "--quiet", "--single-transaction", "--set", "ON_ERROR_STOP=1", *args],
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
    assert reader.stdout is not None and writer.stdin is not None
    try:
        for line in reader.stdout:
            if not line.lstrip().startswith(_NEWER_ONLY_SETTINGS):
                writer.stdin.write(line)
        writer.stdin.close()
        reader_err = reader.stderr.read().decode(errors="replace") if reader.stderr else ""
        writer_err = writer.stderr.read().decode(errors="replace") if writer.stderr else ""
        reader.wait(timeout=TIMEOUT)
        writer.wait(timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        reader.kill()
        writer.kill()
        raise PgToolError(f"恢复中断：{type(exc).__name__}") from exc
    if reader.returncode != 0:
        raise PgToolError(f"{Path(pg_restore).name} 失败：{reader_err.strip()[-800:]}")
    if writer.returncode != 0:
        raise PgToolError(f"{Path(psql).name} 失败：{writer_err.strip()[-800:]}")
