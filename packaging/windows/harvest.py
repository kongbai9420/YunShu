"""把 PyInstaller 输出目录（dist/yunshu）列成 WiX 片段 AppFiles.wxs（ComponentGroup AppFiles）。

    python packaging/windows/harvest.py <dist/yunshu> <输出 .wxs>

WiX 5 不再自带目录采集工具，这里自己生成：每个文件一个组件、文件作为 KeyPath，组件 GUID 由 WiX 按安装路径
自动生成（同一路径每次构建都相同，升级时能正确对应）。yunshu.exe 由 Yunshu.wxs 单独声明（服务挂在它上面）。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

EXCLUDE = {"yunshu.exe"}


def _id(prefix: str, rel: str) -> str:
    return prefix + hashlib.sha1(rel.lower().encode("utf-8")).hexdigest()[:32]


def _attr(value: str) -> str:
    # WiX 预处理器会展开 $(...)，路径里的 $ 要写成 $$
    return quoteattr(value.replace("$", "$$"))


def harvest(app_dir: Path) -> str:
    app_dir = app_dir.resolve()
    files = sorted(p for p in app_dir.rglob("*") if p.is_file() and p.relative_to(app_dir).as_posix() not in EXCLUDE)
    if not (app_dir / "yunshu.exe").is_file() or not files:
        raise SystemExit(f"{app_dir} 不是 PyInstaller 输出的 yunshu 目录")
    dirs: dict[str, list[str]] = {}
    for f in files:
        parent = f.parent.relative_to(app_dir).as_posix()
        parent = "" if parent == "." else parent
        while True:
            dirs.setdefault(parent, [])
            if not parent:
                break
            up = parent.rsplit("/", 1)[0] if "/" in parent else ""
            if parent not in dirs.setdefault(up, []):
                dirs[up].append(parent)
            parent = up

    def dir_id(rel: str) -> str:
        return "INSTALLFOLDER" if not rel else _id("d", rel)

    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             "<!-- 由 packaging/windows/harvest.py 生成，不要手改 -->",
             '<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">', "  <Fragment>"]

    def emit(rel: str, depth: int) -> None:
        for child in sorted(dirs.get(rel, [])):
            name = child.rsplit("/", 1)[-1]
            lines.append(f'{"  " * depth}<Directory Id="{dir_id(child)}" Name={_attr(name)}>')
            emit(child, depth + 1)
            lines.append(f'{"  " * depth}</Directory>')

    lines.append('    <DirectoryRef Id="INSTALLFOLDER">')
    emit("", 3)
    lines.append("    </DirectoryRef>")
    lines.append('    <ComponentGroup Id="AppFiles">')
    for f in files:
        rel = f.relative_to(app_dir).as_posix()
        parent = f.parent.relative_to(app_dir).as_posix()
        parent = "" if parent == "." else parent
        lines.append(f'      <Component Id="{_id("c", rel)}" Directory="{dir_id(parent)}">')
        lines.append(f'        <File Id="{_id("f", rel)}" Source={_attr(str(f))} KeyPath="yes" />')
        lines.append("      </Component>")
    lines.append("    </ComponentGroup>")
    lines += ["  </Fragment>", "</Wix>", ""]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    out = Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(harvest(Path(argv[0])), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
