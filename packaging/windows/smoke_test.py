"""对打包好的 yunshu.exe 做隔离自检（scripts/build-windows.ps1 调用；只用临时目录、127.0.0.1 和伪造数据）。

    python packaging/windows/smoke_test.py <yunshu.exe> [报告 .json]

检查：version；`api` 与 `service --foreground` 两种方式各启动一次 → /api/ready 200 → admin / admin123
首次登录要求改密 → `worker --check` → Ctrl+Break 停止 → 单实例锁已释放；以及 backup create / keytool fingerprint。
不读取本机的 yunshu.env（用临时配置文件 + YUNSHU_CONFIG），也不碰 %ProgramData%。
"""

from __future__ import annotations

import json
from contextlib import closing
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(config: Path) -> dict[str, str]:
    # 去掉构建机上可能有的 YUNSHU_* 变量，只认临时配置文件
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("YUNSHU_")}
    env["YUNSHU_CONFIG"] = str(config)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


def _run(exe: Path, env: dict[str, str], *args: str, timeout: float = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(exe), *args], env=env, capture_output=True, timeout=timeout, check=False)


def _get(url: str) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _login(base: str) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(base + "/api/auth/login", method="POST",
                                 data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                                 headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=10) as resp:
        return json.loads(resp.read())


def _locks(db: Path) -> int:
    with closing(sqlite3.connect(db)) as conn:  # sqlite3 的 with 不关闭连接
        return conn.execute("select count(*) from scheduler_locks").fetchone()[0]


def _tail(log: Path, lines: int = 40) -> str:
    try:
        return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _serve_once(exe: Path, env: dict[str, str], port: int, db: Path, args: list[str], log: Path) -> dict[str, object]:
    base = f"http://127.0.0.1:{port}"
    with log.open("wb") as out:
        proc = subprocess.Popen([str(exe), *args], env=env, stdout=out, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            deadline = time.monotonic() + 120
            while True:
                if proc.poll() is not None:
                    raise AssertionError(f"{args} 提前退出（{proc.returncode}）：\n{_tail(log)}")
                try:
                    if _get(base + "/api/ready") == 200:
                        break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise AssertionError(f"{args} 120 秒内没有就绪：\n{_tail(log)}")
                time.sleep(0.5)
            assert _login(base)["user"]["require_password_change"] is True, "首次登录应要求改密"
            check = _run(exe, env, "worker", "--check")
            assert check.returncode == 0, f"worker --check 失败：{check.stdout!r}"
            assert _locks(db) >= 1, "内嵌 Worker 没有拿到单实例锁"
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
                try:
                    proc.wait(90)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    raise AssertionError(f"{args} 90 秒内没有停止：\n{_tail(log)}") from None
    assert _locks(db) == 0, f"{args} 停止后单实例锁没有释放：\n{_tail(log)}"
    return {"args": args, "exit_code": proc.returncode}


def smoke(exe: Path) -> dict[str, object]:
    report: dict[str, object] = {}
    version = subprocess.run([str(exe), "version"], capture_output=True, text=True, timeout=60, check=True)
    report["version"] = version.stdout.strip()
    with tempfile.TemporaryDirectory(prefix="yunshu-smoke-") as folder:
        root = Path(folder)
        port = _free_port()
        config = root / "yunshu.env"
        config.write_text("\n".join([
            f"YUNSHU_DATA_DIR={root / 'data'}",
            f"YUNSHU_MASTER_KEY_FILE={root / 'keys' / 'master.key'}",
            f"YUNSHU_LOG_DIR={root / 'logs'}",
            "YUNSHU_EMBEDDED_WORKER=1",
            "YUNSHU_SETUP_WIZARD=0",
            "YUNSHU_BACKUP_INTERVAL_HOURS=0",
            "YUNSHU_CONTROL_CUTOVER=0",
            "YUNSHU_BIND=127.0.0.1",
            f"YUNSHU_PORT={port}",
            "",
        ]), encoding="utf-8")
        env = _env(config)
        db = root / "data" / "yunshu.db"
        report["api"] = _serve_once(exe, env, port, db, ["api"], root / "api.out.log")
        report["service_foreground"] = _serve_once(exe, env, port, db, ["service", "--foreground"],
                                                   root / "service.out.log")
        assert (root / "logs" / "yunshu-service.log").is_file(), "服务方式没有写日志文件"
        backup = _run(exe, env, "backup", "create")
        assert backup.returncode == 0, f"backup create 失败：{backup.stdout!r} {backup.stderr!r}"
        listed = _run(exe, env, "backup", "list")
        assert listed.returncode == 0 and listed.stdout.strip(), "backup list 没有输出"
        finger = _run(exe, env, "keytool", "fingerprint")
        assert finger.returncode == 0, f"keytool fingerprint 失败：{finger.stdout!r} {finger.stderr!r}"
        report["maintenance_tools"] = True
        outside = _run(exe, env, "service")
        assert outside.returncode == 1, "在服务管理器之外运行 service 应返回 1"
    report["status"] = "ok"
    return report


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    report = smoke(Path(argv[0]).resolve())
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if len(argv) > 1:
        Path(argv[1]).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
