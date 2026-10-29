"""厂商预设：数据在 presets.json，随版本更新；用户保存时可以改地址、模型和 quirks。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.ai.protocols import PROTOCOLS

QUIRKS = frozenset({"json_object_only", "no_max_tokens_with_json", "mistral_tool_ids", "no_temperature"})


@lru_cache(maxsize=1)
def presets() -> list[dict[str, Any]]:
    data = json.loads((Path(__file__).with_name("presets.json")).read_text(encoding="utf-8"))
    out = []
    for p in data["providers"]:
        assert p["protocol"] in PROTOCOLS, p["id"]
        assert set(p.get("quirks", [])) <= QUIRKS, p["id"]
        out.append(p)
    return out


def preset(preset_id: str) -> dict[str, Any] | None:
    return next((p for p in presets() if p["id"] == preset_id), None)
