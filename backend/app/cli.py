"""统一命令行 `yunshu`（Linux 压缩包、Windows 服务版与免安装包共用；镜像入口脚本做同样的事）。

    yunshu api                 页面与接口（默认监听 0.0.0.0:8080，YUNSHU_BIND / YUNSHU_PORT 可改）
    yunshu worker [--check]    后台 Worker；--check 为健康检查
    yunshu migrate|backup|keytool|legacy_import|upgrade_to_pg ...   维护工具，参数同 python -m app.<名字>
    yunshu version

启动前读取配置文件 yunshu.env（每行 KEY=VALUE，# 开头为注释）：YUNSHU_CONFIG 指定的文件，否则
Linux /etc/yunshu/yunshu.env、Windows %ProgramData%\\Yunshu\\yunshu.env。进程里已有的环境变量优先，文件不会覆盖。
设置 YUNSHU_LOG_DIR 时，日志除标准输出外还写到该目录（每个文件 10 MB，保留 5 个）。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import runpy
import sys
from pathlib import Path

TOOLS = ("migrate", "backup", "keytool", "legacy_import", "upgrade_to_pg")
USAGE = "用法：yunshu {api|worker|" + "|".join(TOOLS) + "|version} [参数]"


def default_config_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "Yunshu" / "yunshu.env"
    return Path("/etc/yunshu/yunshu.env")


def parse_env_file(text: str) -> dict[str, str]:
    """KEY=VALUE；忽略空行和 # 注释；值两端的一对引号去掉。不做变量展开，不执行任何东西。"""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key.replace("_", "").isalnum():
            out[key] = value
    return out


def load_config(environ: dict[str, str] | None = None) -> Path | None:
    """把配置文件里的变量补进环境变量（已有的不覆盖），返回读到的文件。"""
    env = os.environ if environ is None else environ
    path = Path(env["YUNSHU_CONFIG"]) if env.get("YUNSHU_CONFIG") else default_config_path()
    if not path.is_file():
        return None
    for key, value in parse_env_file(path.read_text(encoding="utf-8-sig")).items():
        env.setdefault(key, value)
    return path


def setup_logging(name: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_dir = os.environ.get("YUNSHU_LOG_DIR")
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            Path(log_dir) / f"yunshu-{name}.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", handlers=handlers, force=True)


def _frozen_defaults() -> None:
    """打包后的程序：页面文件在包里，没指定时由 API 一并提供。"""
    if getattr(sys, "frozen", False) and not os.environ.get("YUNSHU_FRONTEND_DIR"):
        bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "frontend"
        if (bundled / "index.html").is_file():
            os.environ["YUNSHU_FRONTEND_DIR"] = str(bundled)


def _check_key_location() -> str | None:
    """与镜像入口相同的检查：主密钥目录不能就是数据目录。"""
    from app.crypto import default_key_path

    data_dir = Path(os.environ.get("YUNSHU_DATA_DIR", "data")).resolve()
    key_dir = default_key_path().resolve().parent
    if key_dir == data_dir:
        return "主密钥文件不能放在数据目录里，请把 YUNSHU_MASTER_KEY_FILE 设到数据目录以外"
    return None


def run_api() -> int:
    import uvicorn

    host = os.environ.get("YUNSHU_BIND", "0.0.0.0")
    port = int(os.environ.get("YUNSHU_PORT", "8080"))
    # 与镜像一致：采信 FORWARDED_ALLOW_IPS 里代理转发的客户端地址（未设置时只信本机）
    uvicorn.run("app.main:create_app", factory=True, host=host, port=port, proxy_headers=True, log_config=None)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args.pop(0) if args else "api"
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if cmd == "version":
        from app import __version__

        print(__version__)
        return 0
    config = load_config()
    _frozen_defaults()
    if cmd not in ("api", "worker", *TOOLS):
        print(f"不认识的命令：{cmd}\n{USAGE}", file=sys.stderr)
        return 2
    if cmd in ("api", "worker") and "--check" not in args and (problem := _check_key_location()):
        print(problem, file=sys.stderr)
        return 1
    if cmd == "worker" and "--check" in args:
        from app.worker import check

        return check()
    setup_logging(cmd)
    if config is not None:
        logging.getLogger(__name__).info("已读取配置文件 %s", config)
    if cmd == "api":
        return run_api()
    if cmd == "worker":
        from app.worker import main as worker_main

        sys.argv = ["yunshu worker", *args]
        return worker_main()
    # 维护工具：与 python -m app.<名字> 完全相同
    sys.argv = [f"yunshu {cmd}", *args]
    try:
        runpy.run_module(f"app.{cmd}", run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
