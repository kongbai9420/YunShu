"""收集构建环境依赖的许可文本；生成物仅写入免安装包输出目录。"""
from __future__ import annotations

import importlib.metadata
import json
import shutil
from pathlib import Path


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    output = repo / "artifacts" / "desktop" / "dist" / "Yunshu"
    notes = [
        "云枢运维平台本地候选包；未发布版本。",
        "本清单覆盖 Python/Node 构建环境依赖，部分仅在构建时使用。许可文本见 licenses/。",
        "elkjs 使用 EPL-2.0（双许可选项之一），原样分发，源码：https://github.com/kieler/elkjs",
        "PyInstaller 为 GPL-2.0-or-later with Bootloader-exception；应用代码不因此改用 GPL。",
        "Python 运行时许可证：https://docs.python.org/3/license.html",
    ]
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        notes.append(f"Python: {name} {dist.version}; {dist.metadata.get('License-Expression') or dist.metadata.get('License') or '见许可文本及项目链接'}; {dist.metadata.get('Home-page', '')}")
        for item in dist.files or []:
            if any(part.lower().startswith(("license", "licence", "copying", "notice")) for part in item.parts):
                source = Path(dist.locate_file(item))
                if source.is_file():
                    target = output / "licenses" / "python" / name / item.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    for manifest in (repo / "frontend" / "node_modules").rglob("package.json"):
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not meta.get("name") or not meta.get("version"):
            continue
        name = meta["name"].replace("/", "_")
        notes.append(f"Node: {meta['name']} {meta['version']}; {meta.get('license', '见许可文本')}; {meta.get('repository', '')}")
        for source in manifest.parent.iterdir():
            if source.is_file() and source.name.lower().startswith(("license", "licence", "copying", "notice")):
                target = output / "licenses" / "node" / name / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    python_license = Path(__import__("sys").base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copyfile(python_license, output / "Python-LICENSE.txt")
    (output / "THIRD_PARTY_NOTICES.txt").write_text("\n\n".join(notes), encoding="utf-8")


if __name__ == "__main__":
    main()
