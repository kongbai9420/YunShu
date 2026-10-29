"""首次启动向导（4.1 P12）。

全新安装且部署没有指定数据库（没有 YUNSHU_DATABASE_URL、没有保存过的选择、数据目录里也没有轻量版数据库）时，
API 进入设置模式：除健康检查和 /api/setup 外的接口都返回 503，页面显示向导。用户选择轻量版（SQLite），
或填写 PostgreSQL 连接并测试；保存后在当前进程里完成迁移和初始化，不需要手动重启。

- 选择保存在主密钥旁边的 database.json（与主密钥同一个单独的卷 / 目录，不放进数据目录），
  只有文件所有者可读；环境变量 YUNSHU_DATABASE_URL 始终优先。
- 从本机（127.0.0.1 / ::1）访问无需口令；从其他地址访问要填启动日志里打印的一次性设置口令，
  防止别人抢先把平台指向他们的数据库。
- PostgreSQL 只接受空库或本平台已有的库（重新连接），库里有其他表时拒绝。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

CONFIG_NAME = "database.json"
MIN_PG_VERSION = 160000


def config_path(master_key_file: Path) -> Path:
    return master_key_file.parent / CONFIG_NAME


def load_saved_url(master_key_file: Path) -> str | None:
    path = config_path(master_key_file)
    if not path.is_file():
        return None
    url = json.loads(path.read_text(encoding="utf-8")).get("url")
    return url if isinstance(url, str) and url else None


def save_url(master_key_file: Path, url: str) -> Path:
    path = config_path(master_key_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"url": url}, f)
    os.replace(tmp, path)
    return path


class DatabaseChoice(BaseModel):
    kind: Literal["sqlite", "postgresql"]
    host: str = Field("", max_length=255)
    port: int = Field(5432, ge=1, le=65535)
    database: str = Field("", max_length=63)
    username: str = Field("", max_length=63)
    password: SecretStr = SecretStr("")
    sslmode: Literal["disable", "prefer", "require", "verify-full"] = "prefer"
    token: str = Field("", max_length=128)


def build_url(choice: DatabaseChoice, data_dir: Path) -> str:
    if choice.kind == "sqlite":
        return f"sqlite:///{(data_dir / 'yunshu.db').as_posix()}"
    missing = [label for label, value in (("主机", choice.host), ("数据库名", choice.database), ("用户名", choice.username)) if not value.strip()]
    if missing:
        raise ValueError(f"请填写{'、'.join(missing)}")
    return URL.create(
        "postgresql+psycopg", username=choice.username.strip(), password=choice.password.get_secret_value() or None,
        host=choice.host.strip(), port=choice.port, database=choice.database.strip(),
        query={"sslmode": choice.sslmode, "connect_timeout": "5"},
    ).render_as_string(hide_password=False)


@dataclass
class CheckResult:
    ok: bool
    message: str
    version: str = ""
    state: Literal["empty", "yunshu", "foreign", "error"] = "error"

    def public(self) -> dict[str, object]:
        return {"ok": self.ok, "message": self.message, "version": self.version, "state": self.state}


def check_url(url: str) -> CheckResult:
    """连接并判断库是否可用。错误信息不带连接串，避免回显密码。"""
    if make_url(url).get_backend_name() == "sqlite":
        return CheckResult(True, "轻量版使用数据目录里的 SQLite 文件，无需额外服务。", "SQLite", "empty")
    try:
        engine = create_engine(url, pool_pre_ping=False)
    except ImportError:
        # 免安装桌面版、只装了基础依赖的源码环境没有 PostgreSQL 驱动
        return CheckResult(False, "这个安装包不含 PostgreSQL 驱动，只能用轻量版。需要 PostgreSQL 时请用 Docker 标准版、Linux 安装包或 Windows 服务版。")
    try:
        with engine.connect() as conn:
            num = int(conn.execute(text("SHOW server_version_num")).scalar_one())
            version = str(conn.execute(text("SHOW server_version")).scalar_one()).split()[0]
            tables = set(inspect(conn).get_table_names())
    except Exception as exc:  # noqa: BLE001 - 连接失败的原因各式各样，统一转成提示
        return CheckResult(False, f"无法连接：{_reason(exc)}")
    finally:
        engine.dispose()
    if num < MIN_PG_VERSION:
        return CheckResult(False, f"PostgreSQL {version} 版本过低，需要 16 或更高版本。", version)
    if not tables:
        return CheckResult(True, f"连接成功（PostgreSQL {version}），这是一个空库，保存后会创建数据表。", version, "empty")
    if "alembic_version" in tables and "users" in tables:
        return CheckResult(True, f"连接成功（PostgreSQL {version}），库里已有本平台的数据，保存后直接使用。", version, "yunshu")
    return CheckResult(False, f"连接成功（PostgreSQL {version}），但库里已有其他表。请换一个空库，或先清空。", version, "foreign")


def _reason(exc: Exception) -> str:
    raw = str(getattr(exc, "orig", None) or exc).strip()
    text_ = raw.splitlines()[0] if raw else type(exc).__name__
    if "password authentication failed" in text_:
        return "用户名或密码错误"
    if "does not exist" in text_ and "database" in text_:
        return "数据库不存在，请先创建"
    if "timeout" in text_.lower() or "timed out" in text_.lower():
        return "连接超时，请检查地址、端口和防火墙"
    if "Connection refused" in text_ or "connection refused" in text_:
        return "连接被拒绝，请检查地址、端口和数据库是否在运行"
    return text_[:200]


@dataclass
class SetupState:
    """设置模式下 API 进程持有的状态。"""

    token: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    lock: threading.Lock = field(default_factory=threading.Lock)

    def allowed(self, client_host: str | None, token: str) -> bool:
        if client_host in ("127.0.0.1", "::1", "localhost"):
            return True
        return bool(token) and secrets.compare_digest(token, self.token)
