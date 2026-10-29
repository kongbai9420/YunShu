"""命令巡检的内置模板与命令安全检查（4.4 P21）。

内置模板依据网上常见的巡检清单整理（Linux 巡检脚本、Proxmox 社区与 check_pve2 的检查项、华为 / H3C 命令参考），
全部是只读命令，平台维护、页面上只读；都还没有经过实机验证。

检查项结构：{"key", "name", "command", "check": {...}, "severity", "timeout"}。check.type：
- record：只记录输出，不判定；
- empty：输出为空正常；
- regex：输出匹配 pattern 正常；
- not_regex：输出匹配 pattern 为异常；
- threshold：用 pattern 的第一个捕获组取出数值（可多个），按 op（gte 越大越差 / lte 越小越差）与 warning / critical 判定；
- exit0：退出码为 0 正常。
可选 ok_if：输出匹配时直接算正常（例如“未加入集群”）。异常的严重程度：threshold 按阈值，其余用检查项的 severity。

自定义模板保存时按危险命令清单拦截：删除、重启、关机、格式化、写文件、改服务、
改用户与权限、装卸软件、改虚拟机 / 存储池、进入网络设备配置模式、reset / delete / save 等，命中任何一条都拒绝保存；执行前再检查一次。
"""

from __future__ import annotations

import re
from typing import Any

PLATFORMS = {"linux": "Linux", "pve": "Proxmox VE", "huawei": "华为 VRP 交换机", "h3c": "H3C Comware 交换机", "cisco": "Cisco IOS 交换机"}
NETWORK_PLATFORMS = ("huawei", "h3c", "cisco")
# 网络设备进入会话后先关闭分页，只对本次会话生效
PAGING_OFF = {"huawei": "screen-length 0 temporary", "h3c": "screen-length disable", "cisco": "terminal length 0"}
# 网络设备允许的命令开头：华为 / H3C 只允许 display 类，Cisco 只允许 show 类（另含关闭分页与 ping / 路由跟踪）
_NETWORK_ALLOWED = {
    "cisco": r"(?i)\s*(show|sh|terminal\s+length|ping|traceroute)\b",
    "default": r"(?i)\s*(display|dis|screen-length|ping|tracert)\b",
}
CHECK_TYPES = ("record", "empty", "regex", "not_regex", "threshold", "exit0")
SEVERITIES = ("critical", "warning", "info")
MAX_ITEMS = 50
DEFAULT_TIMEOUT = 30


def _i(key: str, name: str, command: str, check: dict[str, Any] | None = None, severity: str = "warning") -> dict[str, Any]:
    return {"key": key, "name": name, "command": command, "check": check or {"type": "record"}, "severity": severity, "timeout": DEFAULT_TIMEOUT}


def _t(op: str, pattern: str, warning: float, critical: float | None = None) -> dict[str, Any]:
    return {"type": "threshold", "pattern": pattern, "op": op, "warning": warning, "critical": critical}


LINUX = [
    _i("load", "每核负载（1 分钟）", "awk -v n=\"$(nproc)\" '{printf \"%.2f\\n\", $1/n}' /proc/loadavg", _t("gte", r"([\d.]+)", 2, 4)),
    _i("memory", "可用内存比例 %", "awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{printf \"%.1f\\n\", a*100/t}' /proc/meminfo",
       _t("lte", r"([\d.]+)", 10, 5)),
    _i("disk", "磁盘使用率", "df -P -x tmpfs -x devtmpfs -x overlay -x squashfs | awk 'NR>1{print $5, $6}'", _t("gte", r"(\d+)%", 85, 95)),
    _i("inode", "inode 使用率", "df -iP -x tmpfs -x devtmpfs -x overlay -x squashfs | awk 'NR>1{print $5, $6}'", _t("gte", r"(\d+)%", 85, 95)),
    _i("zombie", "僵尸进程", "ps -eo stat=,pid=,comm= | awk '$1 ~ /^Z/'", {"type": "empty"}),
    _i("failed_units", "失败的服务", "systemctl --failed --no-legend --plain", {"type": "empty"}),
    _i("error_logs", "近 24 小时错误日志条数", "journalctl -p err -S -24h --no-pager -q | wc -l", _t("gte", r"(\d+)", 50), "info"),
    _i("oom", "近 24 小时内存溢出", "journalctl -k -S -24h --no-pager -q | grep -i 'out of memory' | tail -n 20", {"type": "empty"}),
    _i("ntp", "时间同步", "timedatectl show -p NTPSynchronized --value", {"type": "regex", "pattern": r"^yes\s*$"}),
    _i("readonly_mounts", "只读挂载", "awk '$4 ~ /^ro,/ && $3 !~ /^(squashfs|iso9660)$/' /proc/mounts", {"type": "empty"}, "critical"),
    _i("security_updates", "待安装的安全更新（Debian / Ubuntu）", "apt list --upgradable 2>/dev/null | grep -c -i security",
       _t("gte", r"(\d+)", 1), "info"),
]

PVE = [
    _i("version", "PVE 版本", "pveversion"),
    _i("quorum", "集群法定票数", "pvecm status", {"type": "regex", "pattern": r"Quorate:\s+Yes",
                                                     "ok_if": r"(?i)corosync config .* does not exist|not part of a cluster"}, "critical"),
    _i("storage", "存储状态", "pvesm status | awk 'NR>1 && $3 != \"active\"'", {"type": "empty"}),
    _i("zfs", "ZFS 存储池", "zpool status -x 2>&1", {"type": "regex", "pattern": r"all pools are healthy|no pools available"}, "critical"),
    _i("services", "PVE 服务", "systemctl is-active pve-cluster pvedaemon pveproxy pvestatd | grep -vx active", {"type": "empty"}, "critical"),
    _i("ceph", "Ceph 健康（有 Ceph 时）", "if command -v ceph >/dev/null 2>&1; then ceph health; else echo NO_CEPH; fi",
       {"type": "regex", "pattern": r"HEALTH_OK|NO_CEPH"}),
    _i("vms", "虚拟机列表", "qm list"),
    _i("containers", "容器列表", "pct list"),
] + [i for i in LINUX if i["key"] in ("load", "memory", "disk", "failed_units", "readonly_mounts", "ntp")]

_HW_BAD = r"(?i)\b(abnormal|fault|failed|failure|alarm)\b"
HUAWEI = [
    _i("version", "设备版本", "display version"),
    _i("cpu", "CPU 使用率", "display cpu-usage", _t("gte", r"(?i)(?:five minutes|5 minutes)\s*:?\s*(\d+)%", 80, 95)),
    _i("memory", "内存使用率", "display memory", _t("gte", r"(?i)(?:Using Percentage(?: Is)?|utilization)\s*:?\s*(\d+)%", 85, 95)),
    _i("device", "单板状态", "display device", {"type": "not_regex", "pattern": _HW_BAD}),
    _i("fan", "风扇", "display fan", {"type": "not_regex", "pattern": _HW_BAD}),
    _i("power", "电源", "display power", {"type": "not_regex", "pattern": r"(?i)\b(abnormal|fault|failed|no supply)\b"}),
    _i("temperature", "温度", "display temperature all", {"type": "not_regex", "pattern": _HW_BAD}),
    _i("interfaces", "接口简要状态", "display interface brief"),
    _i("alarm", "当前告警", "display alarm active", {"type": "not_regex", "pattern": r"(?i)\b(critical|major)\b"}),
    _i("logs", "日志缓冲（最近 50 条）", "display logbuffer size 50"),
]

H3C = [
    _i("version", "设备版本", "display version"),
    _i("cpu", "CPU 使用率", "display cpu-usage", _t("gte", r"(?i)(\d+)%\s+in last 5 minutes", 80, 95)),
    _i("memory", "内存空闲比例 %", "display memory", _t("lte", r"(?im)^\s*Mem:.*?([\d.]+)%\s*$", 15, 5)),
    _i("device", "单板状态", "display device", {"type": "not_regex", "pattern": _HW_BAD}),
    _i("fan", "风扇", "display fan", {"type": "not_regex", "pattern": _HW_BAD}),
    _i("power", "电源", "display power", {"type": "not_regex", "pattern": _HW_BAD}),
    _i("environment", "温度", "display environment", {"type": "not_regex", "pattern": _HW_BAD}),
    _i("interfaces", "接口简要状态", "display interface brief"),
    _i("alarm", "当前告警", "display alarm", {"type": "not_regex", "pattern": r"(?i)\b(critical|major)\b"}),
    _i("logs", "日志缓冲（最近的在前）", "display logbuffer reverse"),
]

CISCO = [
    _i("version", "设备版本", "show version"),
    _i("cpu", "CPU 使用率（5 分钟）", "show processes cpu | include CPU utilization", _t("gte", r"(?i)five minutes:\s*(\d+)%", 80, 95)),
    _i("memory", "内存", "show processes memory | include Processor Pool"),
    _i("environment", "风扇、电源与温度", "show environment all",
       {"type": "not_regex", "pattern": r"(?i)\b(fail(ed|ure)?|faulty|critical|alarm|shutdown)\b"}),
    _i("errdisabled", "被错误关闭的端口", "show interfaces status err-disabled", {"type": "not_regex", "pattern": r"(?i)err-disabled"}),
    _i("interfaces", "接口状态", "show interfaces status"),
    _i("logs", "日志缓冲", "show logging"),
]

UNVERIFIED = "内置模板，只读命令；判定规则依据常见输出格式编写，还没有经过实机验证。"
BUILTIN = [
    {"key": "builtin-linux", "name": "Linux 通用巡检", "platform": "linux", "items": LINUX,
     "description": f"负载、内存、磁盘与 inode、僵尸进程、失败的服务、错误日志、内存溢出、时间同步、只读挂载、安全更新。{UNVERIFIED}"},
    {"key": "builtin-pve", "name": "Proxmox VE 巡检", "platform": "pve", "items": PVE,
     "description": f"集群票数、存储、ZFS、PVE 服务、Ceph，以及负载、内存、磁盘等主机项。{UNVERIFIED}"},
    {"key": "builtin-huawei", "name": "华为交换机巡检", "platform": "huawei", "items": HUAWEI,
     "description": f"版本、CPU、内存、单板、风扇、电源、温度、接口、当前告警、日志。会话开始时执行 screen-length 0 temporary 关闭分页（只对本次会话生效）。{UNVERIFIED}"},
    {"key": "builtin-h3c", "name": "H3C 交换机巡检", "platform": "h3c", "items": H3C,
     "description": f"版本、CPU、内存、单板、风扇、电源、温度、接口、当前告警、日志。会话开始时执行 screen-length disable 关闭分页（只对本次会话生效）。{UNVERIFIED}"},
    {"key": "builtin-cisco", "name": "Cisco 交换机巡检", "platform": "cisco", "items": CISCO,
     "description": f"版本、CPU、内存、风扇电源温度、被错误关闭的端口、接口、日志。会话开始时执行 terminal length 0 关闭分页（只对本次会话生效）。{UNVERIFIED}"},
]


# ---- 危险命令拦截 ----

_SHELL_DANGER = [
    (r"\b(rm|rmdir|unlink|shred|mv|cp|ln|install)\b", "删除、移动或复制文件"),
    (r"\b(reboot|shutdown|poweroff|halt)\b|\binit\s+[0-6]\b|\btelinit\b", "重启或关机"),
    (r"\b(mkfs(\.\w+)?|mkswap|dd|fdisk|sfdisk|gdisk|parted|wipefs|blkdiscard|lvremove|vgremove|pvremove|lvcreate|lvextend|lvreduce)\b",
     "磁盘分区或格式化"),
    (r"\bsystemctl\s+(?:-\S+\s+)*(start|stop|restart|reload|enable|disable|mask|unmask|kill|isolate|reboot|poweroff|halt|daemon-reload|edit|set-\S+)\b",
     "启停或修改服务"),
    (r"\bservice\s+\S+\s+(start|stop|restart|reload)\b", "启停服务"),
    (r"\b(kill|killall|pkill|xkill)\b", "结束进程"),
    (r"\b(chmod|chown|chgrp|chattr|setfacl)\b", "修改文件权限"),
    (r"\b(useradd|userdel|usermod|groupadd|groupdel|passwd|chpasswd|visudo)\b", "修改用户或密码"),
    (r"\b(tee|truncate|touch|mkdir)\b|\bsed\s+(?:-\S+\s+)*-i|\bsed\s+-\w*i", "写文件"),
    (r"\bcrontab\s+(-\w*[er])", "修改定时任务"),
    (r"\b(iptables|ip6tables|nft|ufw|firewall-cmd)\b(?!.*\s(-L|-S|-nL|-nvL|--list|list|status)\b)", "修改防火墙"),
    (r"\b(apt|apt-get|yum|dnf|zypper|apk|dpkg|rpm|pip|snap)\s+(?:-\S+\s+)*(install|remove|purge|upgrade|dist-upgrade|full-upgrade|update|erase|autoremove|-i|-e|-U|del|add)\b",
     "安装、卸载或升级软件"),
    (r"\b(qm|pct)\s+(start|stop|shutdown|reboot|reset|destroy|set|create|restore|migrate|resize|rollback|snapshot|delsnapshot|unlock|move[-_]disk|clone|template)\b",
     "修改虚拟机或容器"),
    (r"\bzpool\s+(create|destroy|export|import|clear|offline|online|remove|replace|attach|detach|add|scrub|trim|upgrade|set|split|labelclear)\b|\bzfs\s+(create|destroy|rollback|set|rename|snapshot|promote|send|receive|recv)\b",
     "修改 ZFS"),
    (r"\bceph\s+.*\b(set|unset|rm|purge|out|in|down|reweight|create|delete|pause|unpause|mark)\b", "修改 Ceph"),
    (r"\b(pvecm|pvesm|pveceph|pvesh)\s+(add|delnode|expected|remove|set|create|delete|alloc|free|init|purge|destroy)\b", "修改 PVE 集群或存储"),
    (r"\b(mount|umount|swapoff|swapon|sysctl\s+-w|modprobe|rmmod|insmod|hostnamectl\s+set|timedatectl\s+set|ip\s+(link|addr|route)\s+(set|add|del|flush))\b",
     "修改系统状态"),
    (r"\b(curl|wget)\b.*\s(-o|-O|--output)\b|\|\s*(sh|bash|zsh|python\d?|perl)\b|\b(eval|exec|source)\b", "下载或执行脚本"),
]
_NETWORK_DANGER = [
    (r"(?i)\b(system-view|sys|configure|conf|config)\b", "进入配置模式"),
    (r"(?i)\b(reset|delete|format|reboot|reload|undo|save|write|copy|erase|clear|startup|rename|move|mkdir|rmdir|fixdisk|upgrade|boot|install|patch|quit|return)\b",
     "修改设备或配置"),
]


def _strip_quotes(cmd: str) -> str:
    """去掉引号内的文字再检查：awk / grep 的表达式里的 > 与关键词不算。"""
    return re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", " ", cmd)


def dangerous(command: str, platform: str) -> str:
    """返回命中的危险类别（空串表示没有命中）。"""
    bare = _strip_quotes(command)
    if platform in NETWORK_PLATFORMS:
        for pattern, label in _NETWORK_DANGER:
            if re.search(pattern, command):
                return label
        if not re.match(_NETWORK_ALLOWED.get(platform, _NETWORK_ALLOWED["default"]), command):
            return "Cisco 只允许 show 一类查看命令" if platform == "cisco" else "网络设备只允许 display 一类查看命令"
        return ""
    for pattern, label in _SHELL_DANGER:
        if re.search(pattern, bare):
            return label
    # 重定向写文件；2>/dev/null、2>&1、>/dev/null 这类丢弃输出的除外
    if re.search(r"(?<![0-9&<-])>>?(?!\s*(/dev/null|&\d))|[0-9&]>>?(?!\s*(/dev/null|&\d))", bare):
        return "重定向写文件"
    return ""


def validate_items(items: list[dict[str, Any]], platform: str) -> tuple[list[dict[str, Any]], list[str]]:
    """整理检查项并检查；返回 (整理后的检查项, 问题列表)。"""
    problems: list[str] = []
    out: list[dict[str, Any]] = []
    if platform not in PLATFORMS:
        return [], [f"不支持的平台：{platform}"]
    if not items:
        problems.append("至少要有一条检查项")
    if len(items) > MAX_ITEMS:
        problems.append(f"检查项最多 {MAX_ITEMS} 条")
    keys: set[str] = set()
    for n, raw in enumerate(items[:MAX_ITEMS], 1):
        name = str(raw.get("name") or "").strip()[:200]
        command = str(raw.get("command") or "").strip()
        label = f"第 {n} 条“{name or '未命名'}”"
        if not name or not command:
            problems.append(f"{label}：名称和命令都要填")
            continue
        if len(command) > 1000 or "\n" in command or "\r" in command:
            problems.append(f"{label}：命令只能是一行，最多 1000 个字符")
            continue
        if why := dangerous(command, platform):
            problems.append(f"{label}：命令含危险操作（{why}），不能保存")
        key = re.sub(r"[^a-z0-9_]+", "_", str(raw.get("key") or f"item{n}").lower()).strip("_")[:64] or f"item{n}"
        while key in keys:
            key = f"{key}_{n}"[:64]
        keys.add(key)
        check = dict(raw.get("check") or {"type": "record"})
        ctype = check.get("type", "record")
        if ctype not in CHECK_TYPES:
            problems.append(f"{label}：不支持的判定方式 {ctype}")
            continue
        clean: dict[str, Any] = {"type": ctype}
        for field in ("pattern", "ok_if"):
            if check.get(field):
                try:
                    re.compile(str(check[field]))
                except re.error as exc:
                    problems.append(f"{label}：正则表达式有误（{exc}）")
                clean[field] = str(check[field])[:500]
        if ctype in ("regex", "not_regex", "threshold") and not clean.get("pattern"):
            problems.append(f"{label}：这种判定方式需要填写正则表达式")
        if ctype == "threshold":
            clean["op"] = "lte" if check.get("op") == "lte" else "gte"
            try:
                clean["warning"] = float(check["warning"])
                clean["critical"] = float(check["critical"]) if check.get("critical") not in (None, "") else None
            except (KeyError, TypeError, ValueError):
                problems.append(f"{label}：数值阈值要填警告值（严重值可选）")
        severity = raw.get("severity") if raw.get("severity") in SEVERITIES else "warning"
        try:
            timeout = max(5, min(300, int(raw.get("timeout") or DEFAULT_TIMEOUT)))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        out.append({"key": key, "name": name, "command": command, "check": clean, "severity": severity, "timeout": timeout})
    return out, problems
