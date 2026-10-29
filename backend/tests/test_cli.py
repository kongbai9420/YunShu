"""统一命令行 yunshu（部署兼容方案 D2）：配置文件 yunshu.env、日志落盘、子命令分派；只用临时目录和假数据。"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from app import __version__, cli

BACKEND = Path(__file__).resolve().parents[1]


def test_parse_env_file() -> None:
    text = "\n".join([
        "# 注释",
        "",
        "YUNSHU_PORT=9090",
        "export YUNSHU_BIND = 127.0.0.1",
        'YUNSHU_DATABASE_URL="postgresql+psycopg://fake:fake-pass@192.0.2.10:5432/yunshu"',
        "TZ='Asia/Tokyo'",
        "not a line",
        "BAD KEY=x",
        "YUNSHU_EMPTY=",
    ])
    assert cli.parse_env_file(text) == {
        "YUNSHU_PORT": "9090",
        "YUNSHU_BIND": "127.0.0.1",
        "YUNSHU_DATABASE_URL": "postgresql+psycopg://fake:fake-pass@192.0.2.10:5432/yunshu",
        "TZ": "Asia/Tokyo",
        "YUNSHU_EMPTY": "",
    }


def test_load_config_does_not_override_process_env(tmp_path: Path) -> None:
    conf = tmp_path / "yunshu.env"
    conf.write_text("YUNSHU_PORT=9090\nYUNSHU_BIND=127.0.0.1\n", encoding="utf-8")
    env = {"YUNSHU_CONFIG": str(conf), "YUNSHU_PORT": "8181"}
    assert cli.load_config(env) == conf
    assert env["YUNSHU_PORT"] == "8181"  # 进程环境变量优先
    assert env["YUNSHU_BIND"] == "127.0.0.1"
    assert cli.load_config({"YUNSHU_CONFIG": str(tmp_path / "missing.env")}) is None


def test_default_config_path() -> None:
    path = cli.default_config_path()
    if sys.platform == "win32":
        assert path.parts[-2:] == ("Yunshu", "yunshu.env")
    else:
        assert path == Path("/etc/yunshu/yunshu.env")


def test_log_dir_writes_rotating_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YUNSHU_LOG_DIR", str(tmp_path / "logs"))
    cli.setup_logging("api")
    logging.getLogger("fake.test").info("fake log line")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "fake log line" in (tmp_path / "logs" / "yunshu-api.log").read_text(encoding="utf-8")
    for h in list(logging.getLogger().handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            logging.getLogger().removeHandler(h)
            h.close()


def test_version_and_unknown_command(capsys, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("YUNSHU_CONFIG", str(tmp_path / "none.env"))
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__
    assert cli.main(["frobnicate"]) == 2
    assert "不认识的命令" in capsys.readouterr().err


def test_key_dir_equal_to_data_dir_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("YUNSHU_CONFIG", str(tmp_path / "none.env"))
    monkeypatch.setenv("YUNSHU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YUNSHU_MASTER_KEY_FILE", str(tmp_path / "data" / "master.key"))
    assert cli.main(["api"]) == 1
    assert "不能放在数据目录里" in capsys.readouterr().err


def test_tool_runs_like_python_m(tmp_path: Path) -> None:
    """yunshu keytool path 与 python -m app.keytool path 输出相同；配置文件里的主密钥路径生效。"""
    conf = tmp_path / "yunshu.env"
    key = tmp_path / "keys" / "master.key"
    conf.write_text(f"YUNSHU_MASTER_KEY_FILE={key}\nYUNSHU_DATA_DIR={tmp_path / 'data'}\n", encoding="utf-8")
    env = {"YUNSHU_CONFIG": str(conf), "PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    out = subprocess.run([sys.executable, "-m", "app", "keytool", "path"], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == str(key)
    init = subprocess.run([sys.executable, "-m", "app", "keytool", "init"], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)
    assert init.returncode == 0, init.stderr
    assert key.is_file()
