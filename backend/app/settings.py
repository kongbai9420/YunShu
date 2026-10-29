"""运行配置：只从环境变量读取，读取发生在调用时而不是 import 时。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.crypto import default_key_path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    master_key_file: Path
    # 经 HTTPS 访问时设为 1，会话 Cookie 只走 HTTPS
    secure_cookies: bool = False
    # 备份目录，默认在数据目录下的 backups/
    backup_path: Path | None = None
    # 定时备份间隔（小时），0 表示关闭
    backup_interval_hours: float = 24
    # 1 表示由 API 进程在后台线程里运行 Worker（桌面版、本地预览）；Docker 里用独立的 Worker 入口
    embedded_worker: bool = False
    # 启动时是否自动升级数据库结构：None 表示按库类型（SQLite 自动，PostgreSQL 只检查），见 app/migrate.py
    auto_migrate: bool | None = None
    # 构建好的前端目录（frontend/dist）；设置后由 API 一并提供页面，Docker 镜像里用这种方式
    frontend_dir: Path | None = None
    # 指标保留期：原始 48 小时、分钟汇总 30 天、小时汇总 1 年
    raw_retention_hours: int = 48
    minute_retention_days: int = 30
    hour_retention_days: int = 365
    # 告警、通知、任务、AI 用量；审计与控制操作记录（0 为不清理）
    history_retention_days: int = 180
    audit_retention_days: int = 365
    # P5 控制切换开关（4.0.0-beta.1 起默认打开）：允许管理员在页面上确认后把控制归属切到新平台。
    # 部署设置 YUNSHU_CONTROL_CUTOVER=0 可关闭，关闭时页面只能查看切换前检查，不能切换；切回旧控制器不受影响。
    control_cutover_enabled: bool = True
    # 旧版服务地址（如 http://旧版主机:8080）：设置后切换前检查会确认它已停止响应
    legacy_url: str = ""
    # 每日巡检报告的生成时间（本地时间几点之后）；-1 关闭（4.2 P16）
    inspection_hour: int = 8
    # 全新安装且部署没有指定数据库时为 True：API 进入首次启动向导（4.1 P12，见 app/setup_wizard.py）
    setup_pending: bool = False

    @property
    def backup_dir(self) -> Path:
        return self.backup_path or self.data_dir / "backups"

    @classmethod
    def from_env(cls) -> Settings:
        from app.setup_wizard import load_saved_url

        data_dir = Path(os.environ.get("YUNSHU_DATA_DIR", "data")).resolve()
        key_file = default_key_path()
        sqlite_file = data_dir / "yunshu.db"
        # 优先级：环境变量 > 首次启动向导保存的选择 > 轻量版默认；三者都没有且是全新数据目录时进入向导
        database_url = os.environ.get("YUNSHU_DATABASE_URL") or load_saved_url(key_file)
        setup_pending = (
            not database_url and not sqlite_file.exists()
            and os.environ.get("YUNSHU_SETUP_WIZARD", "1") != "0"
        )
        return cls(
            data_dir=data_dir,
            database_url=database_url or f"sqlite:///{sqlite_file.as_posix()}",
            master_key_file=key_file,
            setup_pending=setup_pending,
            inspection_hour=int(os.environ.get("YUNSHU_INSPECTION_HOUR", "8")),
            secure_cookies=os.environ.get("YUNSHU_SECURE_COOKIES", "0") == "1",
            backup_path=Path(os.environ["YUNSHU_BACKUP_DIR"]).resolve() if os.environ.get("YUNSHU_BACKUP_DIR") else None,
            backup_interval_hours=float(os.environ.get("YUNSHU_BACKUP_INTERVAL_HOURS", "24")),
            embedded_worker=os.environ.get("YUNSHU_EMBEDDED_WORKER", "0") == "1",
            auto_migrate={"1": True, "0": False}.get(os.environ.get("YUNSHU_AUTO_MIGRATE", "").strip()),
            frontend_dir=Path(os.environ["YUNSHU_FRONTEND_DIR"]) if os.environ.get("YUNSHU_FRONTEND_DIR") else None,
            raw_retention_hours=int(os.environ.get("YUNSHU_RAW_RETENTION_HOURS", "48")),
            minute_retention_days=int(os.environ.get("YUNSHU_MINUTE_RETENTION_DAYS", "30")),
            hour_retention_days=int(os.environ.get("YUNSHU_HOUR_RETENTION_DAYS", "365")),
            history_retention_days=int(os.environ.get("YUNSHU_HISTORY_RETENTION_DAYS", "180")),
            audit_retention_days=int(os.environ.get("YUNSHU_AUDIT_RETENTION_DAYS", "365")),
            control_cutover_enabled=os.environ.get("YUNSHU_CONTROL_CUTOVER", "1") != "0",
            legacy_url=os.environ.get("YUNSHU_LEGACY_URL", "").strip(),
        )
