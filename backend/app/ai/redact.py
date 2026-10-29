"""发给 AI 前的脱敏与回来后的还原（借鉴 k8sgpt --anonymize）。

已知的名称（资产名、主机名、序列号）由调用方登记；文本里出现的 IPv4 / IPv6 / MAC 自动识别。
代号用纯 ASCII（ASSET_1、IP_2……），模型一般会原样保留，还原时再换回原名。
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any

_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")
_MAC = re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")
_TOKEN = re.compile(r"\b(ASSET|HOST|SN|IP|MAC|NAME)_(\d+)\b")
KINDS = {"asset": "ASSET", "host": "HOST", "serial": "SN", "name": "NAME"}
# 这些键的值是版本号、编号或程序算出的结论，不做地址识别
KEEP_KEYS = frozenset({"version", "versions", "fixed", "fixed_version", "affected", "cve", "id", "cpe", "vector", "build"})


class Redactor:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._forward: dict[str, str] = {}
        self._back: dict[str, str] = {}
        self._counts: dict[str, int] = {}
        self._names: dict[str, str] = {}

    def _token(self, prefix: str, original: str) -> str:
        if original in self._forward:
            return self._forward[original]
        self._counts[prefix] = self._counts.get(prefix, 0) + 1
        token = f"{prefix}_{self._counts[prefix]}"
        self._forward[original], self._back[token] = token, original
        return token

    def register(self, value: str | None, kind: str = "name") -> str:
        """登记一个已知敏感值并返回它的代号；未开启脱敏时原样返回。"""
        if not value or not self.enabled:
            return value or ""
        token = self._token(KINDS.get(kind, "NAME"), value)
        self._names[value] = token
        return token

    def text(self, value: str) -> str:
        if not self.enabled or not value:
            return value
        # 先替换登记过的长名称，避免短名称是长名称的一部分时被拆坏
        for original in sorted(self._names, key=len, reverse=True):
            value = value.replace(original, self._names[original])
        value = _MAC.sub(lambda m: self._token("MAC", m.group(0).lower()), value)
        value = _IPV4.sub(self._ip, value)
        value = _IPV6.sub(self._ip6, value)
        return value

    def _ip(self, m: re.Match[str]) -> str:
        raw = m.group(0)
        try:
            ipaddress.ip_network(raw, strict=False)
        except ValueError:
            return raw  # 版本号之类（如 1.2.3.400）不是地址，保留
        return self._token("IP", raw)

    def _ip6(self, m: re.Match[str]) -> str:
        raw = m.group(0)
        try:
            ipaddress.ip_address(raw)
        except ValueError:
            return raw
        return self._token("IP", raw)

    def data(self, value: Any, keep: frozenset[str] = KEEP_KEYS) -> Any:
        """递归处理字典 / 列表里的字符串。keep 里的键原样保留：版本号如 2.60.60.60 看起来像 IP，不能被替换。"""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: (v if k in keep else self.data(v, keep)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.data(v, keep) for v in value]
        return value

    def restore(self, value: Any) -> Any:
        if isinstance(value, str):
            return _TOKEN.sub(lambda m: self._back.get(m.group(0), m.group(0)), value)
        if isinstance(value, dict):
            return {k: self.restore(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.restore(v) for v in value]
        return value

    def mapping(self) -> dict[str, str]:
        """代号 → 原值，只在本地页面“预览发送内容”时展示。"""
        return dict(self._back)
