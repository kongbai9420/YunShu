"""设备型号库导入（4.2 P14）：读取 netbox-community/devicetype-library（CC0-1.0）的 ZIP 包。

只取机柜需要的字段：厂商、型号、slug、部件号、U 高、是否全深，以及接口 / 电源口数量。
不新增 YAML 依赖：型号文件是固定格式的简单 YAML，这里只解析顶层“键: 值”和顶层列表的条目数，
其余结构（模块、前后面板图片等）忽略。用户自行下载 ZIP 后上传，平台不联网获取。
重复导入按（厂商, 型号）更新，结果相同；手工录入（source=manual）的型号不被覆盖。
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DeviceType

MAX_FILES = 20000
MAX_FILE_BYTES = 256 * 1024
MAX_ZIP_BYTES = 200 * 1024 * 1024
LIST_COUNTS = {"interfaces": "interfaces", "power-ports": "power_ports"}
_KEY = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")


class DeviceTypeError(ValueError):
    pass


def _scalar(raw: str) -> str:
    raw = raw.split(" #", 1)[0].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def parse(text: str) -> dict:
    """解析一个型号文件，返回所需字段；缺厂商或型号时抛 DeviceTypeError。"""
    data: dict[str, str] = {}
    counts = {v: 0 for v in LIST_COUNTS.values()}
    current: str | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or line.startswith("---"):
            continue
        if not line[0].isspace() and not line.startswith("-"):
            m = _KEY.match(line)
            current = None
            if m:
                key, value = m.group(1), m.group(2)
                if value.strip():
                    data[key] = _scalar(value)
                else:
                    current = key
            continue
        # 顶层列表的条目：“- name: …”（缩进 0 或 2 都有）
        if current in LIST_COUNTS and re.match(r"^\s{0,4}- ", line):
            counts[LIST_COUNTS[current]] += 1
    manufacturer, model = data.get("manufacturer", "").strip(), data.get("model", "").strip()
    if not manufacturer or not model:
        raise DeviceTypeError("缺少 manufacturer 或 model")
    try:
        u_height = float(data.get("u_height", "1") or 1)
    except ValueError as exc:
        raise DeviceTypeError(f"u_height 不是数字：{data.get('u_height')}") from exc
    if not 0 <= u_height <= 60:
        raise DeviceTypeError(f"u_height 超出范围：{u_height}")
    return {
        "manufacturer": manufacturer[:100], "model": model[:200], "slug": data.get("slug", "")[:200],
        "part_number": data.get("part_number", "")[:100], "u_height": u_height,
        "is_full_depth": data.get("is_full_depth", "true").lower() not in ("false", "no", "0"),
        **counts,
    }


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    kept_manual: int = 0
    errors: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {"created": self.created, "updated": self.updated, "unchanged": self.unchanged,
                "kept_manual": self.kept_manual, "errors": self.errors[:50], "error_count": len(self.errors)}


def import_zip(db: Session, blob: bytes, *, manufacturers: set[str] | None = None) -> ImportResult:
    """导入 ZIP 里 device-types/**/*.yaml|yml。manufacturers 给出时只导入这些厂商（不区分大小写）。"""
    if len(blob) > MAX_ZIP_BYTES:
        raise DeviceTypeError("文件过大")
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise DeviceTypeError("不是有效的 ZIP 文件") from exc
    names = [n for n in zf.namelist() if "device-types/" in n and n.lower().endswith((".yaml", ".yml"))]
    if not names:
        raise DeviceTypeError("ZIP 里没有 device-types 目录下的型号文件")
    if len(names) > MAX_FILES:
        raise DeviceTypeError(f"型号文件超过 {MAX_FILES} 个")
    wanted = {m.lower() for m in manufacturers} if manufacturers else None
    existing = {(t.manufacturer.lower(), t.model.lower()): t for t in db.scalars(select(DeviceType))}
    result = ImportResult()
    for name in sorted(names):
        info = zf.getinfo(name)
        if info.file_size > MAX_FILE_BYTES:
            result.errors.append(f"{name}：文件过大，已跳过")
            continue
        try:
            item = parse(zf.read(name).decode("utf-8", errors="replace"))
        except DeviceTypeError as exc:
            result.errors.append(f"{name}：{exc}")
            continue
        if wanted is not None and item["manufacturer"].lower() not in wanted:
            continue
        key = (item["manufacturer"].lower(), item["model"].lower())
        row = existing.get(key)
        if row is None:
            row = DeviceType(**item, source="devicetype-library")
            db.add(row)
            existing[key] = row
            result.created += 1
        elif row.source == "manual":
            result.kept_manual += 1
        elif all(getattr(row, k) == v for k, v in item.items()):
            result.unchanged += 1
        else:
            for k, v in item.items():
                setattr(row, k, v)
            result.updated += 1
    return result
