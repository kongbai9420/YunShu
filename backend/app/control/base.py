"""控制适配器公共部分：错误类型、管理目标规范化。"""

from __future__ import annotations

import ipaddress
import re


class ControlError(Exception):
    """kind：

    - rejected：门禁拒绝（没有向设备发送任何东西）
    - unconfigured：缺少凭据或配置（没有发送）
    - unreachable / auth：登录前失败（没有发送控制命令）
    - timeout：控制命令发出后没有应答 → 结果不确定，不重试
    - protocol：设备明确拒绝或返回错误
    - unsupported：设备或库不支持该操作
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def target_key(address: str) -> str | None:
    """把管理地址规范化为互斥键 bmc:<地址>。

    去掉端口；IPv6 压缩并加方括号；主机名转小写、去掉末尾的点。不做 DNS 解析，
    所以指向同一台 BMC 的不同主机名会被当成不同目标——建议控制端点直接填 IP。
    """
    raw = (address or "").strip()
    if not raw:
        return None
    host = raw
    m = re.fullmatch(r"\[([^\]]+)\](?::\d+)?", raw)
    if m:
        host = m.group(1)
    else:
        try:
            return f"bmc:[{ipaddress.IPv6Address(raw).compressed}]"
        except ValueError:
            pass
        if raw.count(":") == 1:
            host = raw.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        name = host.lower().rstrip(".")
        return f"bmc:{name}" if re.fullmatch(r"[a-z0-9.-]+", name) else None
    return f"bmc:[{ip.compressed}]" if ip.version == 6 else f"bmc:{ip.compressed}"
