"""时区数据（部署兼容方案 D1）：Windows 没有系统时区库，zoneinfo 要靠 tzdata 包。

- 全部锁文件都必须无条件带 tzdata（原来只有标准版锁里有，而且只在 win32 上），否则免安装包和轻量版
  按北京时间计算时（每日报告、巡检计划、维护超期提醒、漏洞汇总）会抛 ZoneInfoNotFoundError；
- 万一还是缺时区数据，_local_tz 退回固定 UTC+8，不报错。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
LOCKS = ("requirements.lock", "requirements-dev.lock", "requirements-desktop.lock",
         "requirements-standard.lock", "requirements-standard-dev.lock")

# 在子进程里模拟没有任何时区数据的环境：挡住 tzdata 包，清空系统时区目录（Windows 本来就没有）
NO_TZ_DATA = """
import sys
sys.modules["tzdata"] = None
import zoneinfo
zoneinfo.reset_tzpath([])
try:
    zoneinfo.ZoneInfo("Asia/Shanghai")
    print("has-data")
except zoneinfo.ZoneInfoNotFoundError:
    print("no-data")
from datetime import UTC, datetime
import app.inspection
app.inspection._host_tz = lambda: None  # 不跟随主机，结果与运行测试的机器所在时区无关
from app.inspection import _local_tz
from app.command_inspection import next_run
print(datetime(2026, 10, 28, 16, 30, tzinfo=UTC).astimezone(_local_tz()).isoformat())
print(next_run({"kind": "daily", "time": "08:00"}, datetime(2026, 10, 28, 1, 0, tzinfo=UTC)).isoformat())
"""


@pytest.mark.parametrize("lock", LOCKS)
def test_every_lock_has_tzdata(lock: str) -> None:
    text = (BACKEND / lock).read_text(encoding="utf-8")
    # 不能带平台条件（例如 ; sys_platform == 'win32'），PyInstaller 打的 Linux 包同样没有系统时区库可依赖
    assert re.search(r"^tzdata==[\d.]+ \\$", text, re.M), f"{lock} 缺少无条件的 tzdata"


def test_local_tz_without_any_tz_data() -> None:
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    env.pop("TZ", None)
    out = subprocess.run([sys.executable, "-c", NO_TZ_DATA], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.split()
    assert lines[0] == "no-data"  # 确实模拟出了缺时区数据的环境
    assert lines[1] == "2026-10-29T00:30:00+08:00"
    # 每天 08:00（北京时间）= 00:00 UTC：10-28 01:00 UTC 之后的下一次是 10-29 00:00 UTC
    assert lines[2] == "2026-10-29T00:00:00+00:00"


def test_local_tz_bad_name_falls_back_to_beijing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import inspection
    from app.inspection import _local_tz

    monkeypatch.setattr(inspection, "_host_tz", lambda: None)
    monkeypatch.setenv("TZ", "Not/AZone")
    assert datetime(2026, 1, 1, tzinfo=UTC).astimezone(_local_tz()).utcoffset().total_seconds() == 8 * 3600
    monkeypatch.setenv("TZ", "UTC")
    assert datetime(2026, 1, 1, tzinfo=UTC).astimezone(_local_tz()).utcoffset().total_seconds() == 0


def test_follows_host_when_tz_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from zoneinfo import ZoneInfo

    from app import inspection

    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(inspection, "_host_tz", lambda: ZoneInfo("Europe/Berlin"))
    assert inspection._local_tz() == ZoneInfo("Europe/Berlin")
    # TZ 设了就以 TZ 为准；名字无效时跟随主机
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert inspection._local_tz() == ZoneInfo("Asia/Tokyo")
    monkeypatch.setenv("TZ", "Not/AZone")
    assert inspection._local_tz() == ZoneInfo("Europe/Berlin")


def test_linux_host_zone_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app import inspection

    tz_file = tmp_path / "timezone"
    tz_file.write_text("Europe/Paris" + chr(10), encoding="utf-8")
    monkeypatch.setattr(inspection, "Path", lambda _p: tz_file)
    assert inspection._host_zone_key() == "Europe/Paris"
    # 没有 /etc/timezone 时看 /etc/localtime 指向哪个 zoneinfo 文件
    monkeypatch.setattr(inspection, "Path", lambda _p: tmp_path / "missing")
    monkeypatch.setattr(inspection.os.path, "realpath", lambda _p: "/usr/share/zoneinfo/America/New_York")
    assert inspection._host_zone_key() == "America/New_York"
    monkeypatch.setattr(inspection.os.path, "realpath", lambda _p: "/etc/localtime")
    assert inspection._host_zone_key() is None
