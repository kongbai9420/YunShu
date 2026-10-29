"""从带哈希的锁文件里取出指定的几个包（含全部哈希），供 Linux 打包只装 PyInstaller 本身。

backend/requirements-package.lock 是 Windows 桌面打包锁（含 pywebview、pythonnet 等），Linux 打包只需要
其中的 PyInstaller 及其依赖；直接装整个锁会带进不需要的包。本脚本不改动锁里的版本与哈希，只做筛选：

    python deploy/linux/lock_subset.py backend/requirements-package.lock pyinstaller altgraph ... > subset.lock

缺少任何一个包时退出码为 1，避免悄悄少装。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 打包工具本身（PyInstaller 6.x 在 Linux 上的依赖）；运行时依赖来自 requirements-standard.lock
PYINSTALLER_PACKAGES = ("pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "packaging", "setuptools")

_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str) -> dict[str, list[str]]:
    """包名（规范化）→ 该条目的行（需求行 + 哈希续行，不含注释）。"""
    entries: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        match = _REQ.match(line)
        if match:
            current = [line]
            entries[normalize(match.group(1))] = current
            continue
        stripped = line.strip()
        if current is not None and stripped.startswith("--hash="):
            current.append(line)
            continue
        current = None
    return entries


def subset(text: str, names: list[str] | tuple[str, ...]) -> str:
    entries = parse_lock(text)
    missing = [n for n in names if normalize(n) not in entries]
    if missing:
        raise KeyError(", ".join(missing))
    out: list[str] = []
    for name in names:
        lines = list(entries[normalize(name)])
        # 最后一行不应再以续行符结尾
        lines[-1] = lines[-1].rstrip(" \\")
        out.extend(lines)
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    lock, names = Path(argv[0]), argv[1:] or list(PYINSTALLER_PACKAGES)
    try:
        sys.stdout.write(subset(lock.read_text(encoding="utf-8"), names))
    except KeyError as exc:
        print(f"锁文件 {lock} 里没有：{exc.args[0]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
