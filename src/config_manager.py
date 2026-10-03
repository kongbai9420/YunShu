import os
import sys
import json
import uuid
import configparser

DEFAULT_NODES = [
    {"temp": 45, "speed": 15, "name": "静音基准"},
    {"temp": 55, "speed": 22, "name": "日常轻载"},
    {"temp": 65, "speed": 32, "name": "中载巡航"},
    {"temp": 72, "speed": 45, "name": "温升加速"},
    {"temp": 78, "speed": 70, "name": "重载强冷"},
    {"temp": 82, "speed": 100, "is_safety": True, "name": "BMC熔断保护"}
]

# 1. 硬件节点 (IPMI / BMC 物理机，支持多品牌与序列号资产)
DEFAULT_HARDWARE_NODES = [
    {
        "id": "srv_primary",
        "name": "Dell 核心计算节点",
        "ip": "192.168.1.10",
        "user": "root",
        "password": "",
        "brand": "dell",
        "model": "PowerEdge R730xd",
        "serial": "7X89B22",
        "timeout": 30,
        "retry": 2,
        "mode": "dynamic",
        "manual_speed": 25,
        "preset_key": "silent",
        "enabled": True
    },
    {
        "id": "srv_inspur",
        "name": "浪潮 分布式存储节点",
        "ip": "192.168.1.20",
        "user": "admin",
        "password": "",
        "brand": "inspur",
        "model": "NF5280M5",
        "serial": "2198038123",
        "timeout": 30,
        "retry": 2,
        "mode": "preset",
        "manual_speed": 20,
        "preset_key": "silent",
        "enabled": True
    },
    {
        "id": "srv_huawei",
        "name": "华为 异构推理服务器",
        "ip": "192.168.1.30",
        "user": "Administrator",
        "password": "",
        "brand": "huawei",
        "model": "FusionServer 2288H V5",
        "serial": "2102311WUT10J3000",
        "timeout": 30,
        "retry": 2,
        "mode": "auto",
        "manual_speed": 30,
        "preset_key": "two_node_eco",
        "enabled": True
    }
]

# 2. 系统服务器 (SSH 探针服务器，可绑定节点，也可独立存在未绑定节点)
DEFAULT_SYSTEM_SERVERS = [
    {
        "id": "srv_pve_bound",
        "name": "PVE 虚拟化母机",
        "host": "192.168.1.100",
        "port": 22,
        "username": "root",
        "password": "",
        "node_id": "srv_primary",  # 明确绑定到 srv_primary (Node #1)
        "os_name": "Proxmox VE 8.1",
        "enabled": True
    },
    {
        "id": "srv_standalone_ubuntu",
        "name": "独立应用服务器",
        "host": "192.168.1.150",
        "port": 22,
        "username": "ubuntu",
        "password": "",
        "node_id": "",           # 独立无绑定
        "os_name": "Ubuntu 22.04 LTS",
        "enabled": True
    }
]

DEFAULT_CONFIG = {
    "ipmi": {
        "active_server_id": "srv_primary",
        "dashboard_view_mode": "overview",  # 'overview' (总览), 'nodes' (节点), 'servers' (服务器)
        "probe_layout_style": "compact_grid", # 'compact_grid' or 'compact_row'
        "ui_refresh_sec": "1",               # 界面 UI 刷新周期 (秒, 默认 1s, 独立于底层采样)
        "ipmi_poll_mode": "auto_1s",         # 'auto_1s' (自动轮询：上轮结束1秒后再次获取) or 'custom' (自定义固定秒数)
        "auto_refresh_sec": "3",             # 精准核心采样周期 (秒，默认 3s，极速模式可 1s)
        "global_sensor_poll_sec": "60",      # 全量百项传感器全量采集周期 (秒，默认 60s 或按需展开)
        "node_ping_interval_sec": "10",      # 硬件物理节点在线测活周期 (秒，默认 10s)
        "node_ping_retry_count": "2",        # 硬件物理节点测活判定重试次数 (默认 2次)
        "offline_reconnect_enabled": "1",    # 离线节点主动重连探针开关 (1: 开启, 0: 关闭)
        "offline_reconnect_interval_sec": "15", # 离线节点重连尝试间隔 (秒，默认 15s，上线后自动退出重连恢复常规测活)
        "server_ping_interval_sec": "5",     # 系统服务器(SSH)在线测活周期 (秒，默认 5s)
        "server_ping_retry_count": "2",      # 系统服务器(SSH)测活判定重试次数 (默认 2次)
        "subsystem_poll_sec": "2",
        "default_timeout_sec": "30",
        "default_retry_count": "2",
        "autostart": "0",
        "autostart_mode": "auto",
        "mode": "auto",
        "manual_mode_type": "global",
        "manual_speed": "25",
        "preset_key": "silent",
        "curve_nodes_json": json.dumps(DEFAULT_NODES, ensure_ascii=False),
        "ip": "192.168.1.1",
        "user": "root",
        "password": "",
        # ==========================================
        # IPMI 极速优化引擎开关 (默认全部开启，互不冲突)
        # ==========================================
        "opt_dcmi_temp_enabled": "1",        # 优化项1: DCMI 毫秒直读 (单帧 UDP 报文直读 CPU 与机箱温度)
        "opt_sdr_cache_enabled": "1",        # 优化项2: SDR 本地静态缓存 (-S sdr_cache，体积缩减 70%)
        "opt_cipher_suite_enabled": "1",     # 优化项3: 固定 Cipher Suite 3 (-C 3，跳过双向套件协商)
        "opt_fast_retransmit_enabled": "1",  # 优化项4: 局域网极速重传 (-N 1，丢包 1 秒即重传)
        "opt_type_filter_enabled": "1",      # 优化项5: 传感器定向分流提取 (按 Temperature/Fan 过滤)
        "opt_keepalive_session_enabled": "1" # 优化项6: 会话管道保持与批处理 (Session Keep-Alive / Exec 管道加速)
    },
    "cluster": {
        "servers_json": json.dumps(DEFAULT_HARDWARE_NODES, ensure_ascii=False),
        "system_servers_json": json.dumps(DEFAULT_SYSTEM_SERVERS, ensure_ascii=False)
    },
    "alert": {
        "enabled": "1",
        "cooldown_sec": "60",
        "sound_enabled": "1",
        "sound_type": "default",
        "custom_sound_path": "",
        "tts_enabled": "1",
        "tts_speech_rate": "0",
        # 板块 A: 硬件节点告警规则
        "node_cpu_temp_enabled": "1",
        "node_cpu_temp_threshold": "80",
        # 板块 B: 系统服务器告警规则
        "server_cpu_enabled": "1",
        "server_cpu_threshold": "90",
        "server_mem_enabled": "1",
        "server_mem_threshold": "90",
        "server_swap_enabled": "1",
        "server_swap_threshold": "80",
        "server_disk_enabled": "1",
        "server_disk_threshold": "92",
        # Section C: 多渠道外部消息通知 (移植借鉴自 GitHub 开源项目 Apprise 与 ANotify)
        "webhook_enabled": "0",
        "webhook_channel": "custom_webhook",
        "webhook_url": "",
        "webhook_secret": "",
        # Section D: 自定义告警模版内容
        "custom_alert_title_template": "【{level_text}】{source} 触发告警",
        "custom_alert_body_template": "告警项目: {rule_title}\n告警源: {source}\n当前数值: {current_val}\n设定阈值: {threshold_val}\n详情说明: {detail_msg}\n发生时间: {alert_time}"
    },
    "logging": {
        "log_debug_mode": "0",         # 0: 仅展示 Warning/Error/Critical 报错日志; 1: 调试模式展示全量日志
        "log_retention_days": "7"       # 日志保留期限 (天，默认 7 天，超期自动删除)
    }
}

def get_app_directory():
    # 优先使用程序执行时所在的当前工作目录或可执行文件所在目录，确保不同版本 exe 共享同级 ./config.ini
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    
    # 源码开发运行环境下：检查当前运行根目录是否有 config.ini
    cwd = os.getcwd()
    if os.path.exists(os.path.join(cwd, "config.ini")) or os.path.exists(os.path.join(cwd, "src")):
        return cwd
        
    return os.path.dirname(os.path.abspath(__file__))

class ConfigManager:
    def __init__(self, filename="config.ini"):
        app_dir = get_app_directory()
        self.filename = os.path.join(app_dir, filename) if not os.path.isabs(filename) else filename
        self.config = configparser.ConfigParser()
        self.load()

    def load(self):
        # 1. 优先读取已存在的用户配置文件
        user_config_exists = os.path.exists(self.filename) and os.path.isfile(self.filename)
        if user_config_exists:
            try:
                self.config.read(self.filename, encoding="utf-8")
            except UnicodeDecodeError:
                try:
                    self.config.read(self.filename, encoding="gbk")
                except Exception:
                    pass
            except Exception:
                pass

        # 2. 对于已存在配置中缺失的 section 或 option，安全补充出厂默认值，绝不覆盖用户已修改的持久化值！
        for sec, kvs in DEFAULT_CONFIG.items():
            if not self.config.has_section(sec):
                self.config.add_section(sec)
            for k, v in kvs.items():
                if not self.config.has_option(sec, k):
                    self.config.set(sec, k, str(v))

        if not user_config_exists:
            try:
                self.save()
            except Exception:
                pass

        # Initialize defaults if not present at all
        if not self.config.has_option("cluster", "servers_json"):
            self.set_servers(DEFAULT_HARDWARE_NODES)
        if not self.config.has_option("cluster", "system_servers_json"):
            self.set_system_servers(DEFAULT_SYSTEM_SERVERS)

        return self.filename

    # ==========================================
    # Hardware Nodes (IPMI / 带外)
    # ==========================================
    def get_servers(self):
        if not self.config.has_section("cluster"):
            self.config.add_section("cluster")
        raw = self.config.get("cluster", "servers_json", fallback="")
        if raw:
            try:
                srvs = json.loads(raw)
                if isinstance(srvs, list):
                    return srvs
            except Exception:
                pass
        return []

    def set_servers(self, servers):
        if not self.config.has_section("cluster"):
            self.config.add_section("cluster")
        self.config.set("cluster", "servers_json", json.dumps(servers, ensure_ascii=False))
        self.save()

    def set_server_mode(self, srv_id, mode, manual_speed=None, preset_key=None):
        servers = self.get_servers()
        updated = False
        for s in servers:
            if s.get("id") == srv_id:
                s["mode"] = mode
                if manual_speed is not None:
                    s["manual_speed"] = int(manual_speed)
                if preset_key is not None:
                    s["preset_key"] = preset_key
                updated = True
                break
        if updated:
            self.set_servers(servers)
            if self.get("active_server_id") == srv_id:
                self.set("mode", mode)
                if manual_speed is not None:
                    self.set("manual_speed", str(manual_speed))
                self.save()
        return updated

    def set_probe_layout_style(self, style):
        self.set("probe_layout_style", style)
        self.save()
        return True

    def get_active_server(self):
        servers = self.get_servers()
        active_id = self.get("active_server_id", "node_r730")
        for s in servers:
            if s.get("id") == active_id:
                return s
        if servers:
            return servers[0]
        return DEFAULT_HARDWARE_NODES[0]

    def get_active_server_id(self):
        srv = self.get_active_server()
        return srv.get("id") if srv else self.get("active_server_id", "node_r730")

    def set_active_server_id(self, srv_id):
        self.set("active_server_id", srv_id)
        servers = self.get_servers()
        for s in servers:
            if s.get("id") == srv_id:
                self.set("ip", s.get("ip", "192.168.1.1"))
                self.set("user", s.get("user", "root"))
                self.set("password", s.get("password", ""))
                break
        self.save()

    def add_server(self, name, ip, user, password, model="Dell PowerEdge", brand="dell", serial="", timeout=30, retry=2):
        servers = self.get_servers()
        new_id = f"node_{uuid.uuid4().hex[:6]}"
        brand_val = (brand or "dell").strip().lower()
        if not brand_val:
            brand_val = "dell"
        new_srv = {
            "id": new_id,
            "name": name.strip() or f"{brand_val.upper()} 硬件节点 ({ip})",
            "ip": ip.strip() or "192.168.1.1",
            "user": user.strip() if user else "",
            "password": password or "",
            "model": model.strip() or ("Dell PowerEdge" if brand_val == "dell" else f"{brand_val.upper()} Server"),
            "brand": brand_val,
            "serial": serial.strip(),
            "timeout": max(1, min(120, int(timeout))),
            "retry": max(0, min(10, int(retry))),
            "mode": "auto",
            "manual_speed": 25,
            "preset_key": "silent",
            "enabled": True
        }
        servers.append(new_srv)
        self.set_servers(servers)
        return new_srv

    def delete_server(self, srv_id):
        servers = self.get_servers()
        if len(servers) <= 1:
            return False, "集群中至少需要保留一个硬件节点配置"
        new_list = [s for s in servers if s.get("id") != srv_id]
        self.set_servers(new_list)
        if self.get("active_server_id") == srv_id and new_list:
            self.set_active_server_id(new_list[0]["id"])
        # Unbind any system servers pointing to this deleted node
        sys_srvs = self.get_system_servers()
        for ss in sys_srvs:
            if ss.get("node_id") == srv_id:
                ss["node_id"] = ""
        self.set_system_servers(sys_srvs)
        return True, "硬件节点已成功移除"

    def update_server(self, srv_id, updates):
        servers = self.get_servers()
        updated = False
        for s in servers:
            if s.get("id") == srv_id:
                s.update(updates)
                updated = True
                break
        if updated:
            self.set_servers(servers)
            if self.get("active_server_id") == srv_id:
                self.set_active_server_id(srv_id)
        return updated

    # ==========================================
    # System Servers (SSH 独立或绑定服务器)
    # ==========================================
    def get_system_servers(self):
        if not self.config.has_section("cluster"):
            self.config.add_section("cluster")
        raw = self.config.get("cluster", "system_servers_json", fallback="")
        if raw:
            try:
                srvs = json.loads(raw)
                if isinstance(srvs, list):
                    return srvs
            except Exception:
                pass
        return []

    def set_system_servers(self, servers):
        if not self.config.has_section("cluster"):
            self.config.add_section("cluster")
        self.config.set("cluster", "system_servers_json", json.dumps(servers, ensure_ascii=False))
        self.save()

    def add_system_server(self, name, host, port, username, password, node_id="", os_name=""):
        srvs = self.get_system_servers()
        new_id = f"srv_{uuid.uuid4().hex[:6]}"
        new_srv = {
            "id": new_id,
            "name": name.strip() or f"系统服务器 ({host})",
            "host": host.strip() or "127.0.0.1",
            "port": int(port or 22),
            "username": username.strip() if username else "",
            "password": password or "",
            "node_id": (node_id or "").strip(),
            "os_name": (os_name or "").strip(),
            "enabled": True
        }
        srvs.append(new_srv)
        self.set_system_servers(srvs)
        return new_srv

    def delete_system_server(self, srv_id):
        srvs = self.get_system_servers()
        new_list = [s for s in srvs if s.get("id") != srv_id]
        self.set_system_servers(new_list)
        return True, "系统服务器已成功移除"

    def update_system_server(self, srv_id, updates):
        srvs = self.get_system_servers()
        updated = False
        for s in srvs:
            if s.get("id") == srv_id:
                s.update(updates)
                updated = True
                break
        if updated:
            self.set_system_servers(srvs)
        return updated

    # ==========================================
    # Curve Nodes
    # ==========================================
    def get_curve_nodes(self):
        raw = self.get("curve_nodes_json")
        if raw:
            try:
                nodes = json.loads(raw)
                if isinstance(nodes, list) and len(nodes) >= 2:
                    return sorted(nodes, key=lambda x: x.get("temp", 0))
            except Exception:
                pass
        return DEFAULT_NODES

    def set_curve_nodes(self, nodes):
        if not isinstance(nodes, list) or len(nodes) < 2:
            return False
        sorted_nodes = sorted(nodes, key=lambda x: int(x.get("temp", 0)))
        self.set("curve_nodes_json", json.dumps(sorted_nodes, ensure_ascii=False))
        self.save()
        return True

    # ==========================================
    # Logging Config
    # ==========================================
    def get_log_param(self, key, fallback=None):
        if not self.config.has_section("logging"):
            return fallback
        if not self.config.has_option("logging", key):
            return fallback
        val = self.config.get("logging", key)
        if key == "log_debug_mode":
            return val in ("1", "true", "True")
        if key == "log_retention_days":
            try:
                return int(val)
            except Exception:
                return 7
        return val

    def set_log_param(self, key, value):
        if not self.config.has_section("logging"):
            self.config.add_section("logging")
        if isinstance(value, bool):
            val_str = "1" if value else "0"
        else:
            val_str = str(value)
        self.config.set("logging", key, val_str)
        self.save()

    # ==========================================
    # Alerts Config
    # ==========================================
    def get_alert_param(self, key):
        if not self.config.has_section("alert"):
            return None
        if not self.config.has_option("alert", key):
            return None
        raw = self.config.get("alert", key)
        if key.endswith("_enabled") or key in ("enabled", "sound_enabled", "tts_enabled", "webhook_enabled"):
            return raw == "1" or raw.lower() == "true"
        if key in ("cooldown_sec", "tts_speech_rate"):
            try:
                return int(raw)
            except Exception:
                return 0
        if "threshold" in key:
            try:
                return float(raw)
            except Exception:
                return 80.0
        # 兼容模版字符串包含换行与格式符
        return raw.replace("\\n", "\n")

    def set_alert_param(self, key, value):
        if not self.config.has_section("alert"):
            self.config.add_section("alert")
        if isinstance(value, bool):
            val_str = "1" if value else "0"
        else:
            val_str = str(value).replace("\r\n", "\\n").replace("\n", "\\n")
        self.config.set("alert", key, val_str)
        self.save()

    def get_all(self):
        data = {}
        for sec in self.config.sections():
            data[sec] = dict(self.config.items(sec))
        data["curve_nodes"] = self.get_curve_nodes()
        data["servers"] = self.get_servers()
        data["system_servers"] = self.get_system_servers()
        data["active_server"] = self.get_active_server()
        return data

    def get(self, key, default=None):
        if self.config.has_option("ipmi", key):
            return self.config.get("ipmi", key)
        return default

    def get_int(self, key, default=0):
        try:
            return self.config.getint("ipmi", key)
        except Exception:
            return default

    def get_bool(self, key, default=False):
        try:
            val = self.get(key, None)
            if val is None:
                return default
            val_str = str(val).strip().lower()
            return val_str in ("1", "true", "yes", "on")
        except Exception:
            return default

    def set(self, key, value):
        if not self.config.has_section("ipmi"):
            self.config.add_section("ipmi")
        self.config.set("ipmi", key, str(value))

    def save(self):
        try:
            target_dir = os.path.dirname(os.path.abspath(self.filename))
            if target_dir and not os.path.exists(target_dir):
                os.makedirs(target_dir, exist_ok=True)
            with open(self.filename, "w", encoding="utf-8") as f:
                self.config.write(f)
            return True
        except Exception as e:
            return False
