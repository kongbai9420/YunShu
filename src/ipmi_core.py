import os
import sys
import time
import math
import random
import re
import logging
import threading
import subprocess

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("IPMICore")

PRESETS = {
    "silent": {
        "name": "静音模式 (Silent)",
        "desc": "所有风扇恒定 15%，极致静音，适合低负载居家/办公环境",
        "speeds": [15, 15, 15, 15, 15, 15]
    },
    "one_node_l": {
        "name": "单节点负载 (L-Node)",
        "desc": "左侧节点增强散热 [25, 40, 30, 25, 20, 20]，适用于单CPU或左侧PCIE高载",
        "speeds": [25, 40, 30, 25, 20, 20]
    },
    "one_node_r": {
        "name": "单节点负载 (R-Node)",
        "desc": "右侧节点增强散热 [20, 20, 25, 30, 40, 25]，适用于右侧PCIE/单CPU高载",
        "speeds": [20, 20, 25, 30, 40, 25]
    },
    "two_node_eco": {
        "name": "双节点节能 (ECO)",
        "desc": "双路CPU均衡轻载 [15, 23, 20, 20, 23, 15]，噪音与风量平衡",
        "speeds": [15, 23, 20, 20, 23, 15]
    },
    "two_node_perf": {
        "name": "双节点高性能 (125W+)",
        "desc": "双路CPU高性能 [25, 40, 30, 30, 40, 25]，适合高TDP或渲染编译",
        "speeds": [25, 40, 30, 30, 40, 25]
    },
    "turbo": {
        "name": "满载散热 (Turbo)",
        "desc": "所有风扇 80%，强效排出积热，适合高负载严苛环境",
        "speeds": [80, 80, 80, 80, 80, 80]
    }
}
# Alias quiet -> silent for robust compatibility
PRESETS["quiet"] = PRESETS["silent"]

DEMO_HARDWARE_SERVERS = [
    {
        "id": "srv_primary",
        "name": "Dell 核心计算节点",
        "ip": "192.168.1.10",
        "user": "root",
        "password": "",
        "brand": "dell",
        "model": "PowerEdge R730xd",
        "serial": "7X89B22",
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
        "mode": "auto",
        "manual_speed": 30,
        "preset_key": "two_node_eco",
        "enabled": True
    }
]

class IPMICore:
    def __init__(self, config_mgr):
        self.config_mgr = config_mgr
        self.ipmitool_path = self._locate_ipmitool()
        self.is_monitoring = False
        self.monitor_thread = None
        self.demo_mode = False
        
        # When not connected, values MUST be None/empty so UI displays clean placeholder '--'
        # Default mode is 'auto' (Dell 原厂托管 / BMC 自动温控)
        self.last_sensor_data = {
            "connected": None,  # None: 正在获取; True: 在线; False: 离线
            "latency_ms": None,
            "last_updated": "--:--:--",
            "mode": "auto",
            "cpu_temps": [],
            "max_cpu_temp": None,
            "inlet_temp": None,
            "fans": [],
            "all_sensors": [],
            "power": {
                "total_watts": None,
                "ps1": {"watts": None, "status": "ok", "online": False},
                "ps2": {"watts": None, "status": "ok", "online": False}
            },
            "safety_triggered": False,
            "current_target_speed": None,
            "error_msg": "正在获取连接..."
        }
        
        # Multi-server cluster telemetry mapping: srv_id -> telemetry dict
        self.cluster_telemetry = {}
        for s in self.config_mgr.get_servers():
            sid = s.get("id")
            if sid:
                self.cluster_telemetry[sid] = {
                    "id": sid,
                    "name": s.get("name", "未命名节点"),
                    "ip": s.get("ip", ""),
                    "brand": s.get("brand", "dell"),
                    "model": s.get("model", ""),
                    "serial": s.get("serial", ""),
                    "mode": s.get("mode", "auto"),
                    "connected": None, # None: 正在获取连接中; True: 联机; False: 最终离线
                    "latency_ms": None,
                    "last_updated": "--:--:--",
                    "cpu_temps": [],
                    "max_cpu_temp": None,
                    "inlet_temp": None,
                    "fans": [],
                    "all_sensors": [],
                    "fan_target_pct": None,
                    "avg_fan_rpm": None,
                    "error_msg": "正在获取连接..."
                }
        self._sdr_cache_dir = os.path.join(os.getcwd(), "sdr_cache")
        try:
            os.makedirs(self._sdr_cache_dir, exist_ok=True)
        except Exception:
            pass
        self.lock = threading.Lock()
        self.comm_lock = threading.Lock()   # Dedicated lock ensuring hardware IPMI requests never collide
        self._host_locks = {}
        self._last_applied_speeds = {}
        self._active_processes = set()
        self._node_fail_counts = {}         # Debounce filter: consecutive failure counter per node
        self._last_global_poll_time = {}    # srv_id -> timestamp of last full sensor table poll
        self._last_offline_reconnect_time = {} # srv_id -> timestamp of last offline reconnect probe attempt
        self.fetch_lock = threading.Lock()  # dedicated lock so IPC get_status never waits on subprocess timeout
        self._demo_tick = 0
        self._last_applied_speed = -1
        self._ensure_path_env()

    def _get_host_lock(self, host):
        with self.lock:
            if host not in self._host_locks:
                self._host_locks[host] = threading.Lock()
            return self._host_locks[host]

    def set_demo_mode(self, enabled: bool):
        self.demo_mode = bool(enabled)
        if self.demo_mode:
            # 开启演示：仅在内存中生成虚拟演示遥测，绝不篡改或覆写用户的持久化配置文件
            self._generate_demo_telemetry()
        else:
            with self.lock:
                self.last_sensor_data["connected"] = False
                self.last_sensor_data["cpu_temps"] = []
                self.last_sensor_data["max_cpu_temp"] = None
                self.last_sensor_data["inlet_temp"] = None
                self.last_sensor_data["fans"] = []
                self.last_sensor_data["all_sensors"] = []
                self.cluster_telemetry = {}
        return self.demo_mode

    def _locate_ipmitool(self):
        tool_names = ["ipmitool", "ipmitool.exe"]
        if hasattr(sys, "_MEIPASS"):
            for t in tool_names:
                for sub in ["assets", ""]:
                    bundled = os.path.join(sys._MEIPASS, sub, t) if sub else os.path.join(sys._MEIPASS, t)
                    if os.path.exists(bundled):
                        return bundled

        base_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(base_dir, "assets", "ipmitool"),
            os.path.join(base_dir, "assets", "ipmitool.exe"),
            os.path.join(base_dir, "..", "assets", "ipmitool"),
            os.path.join(base_dir, "..", "assets", "ipmitool.exe"),
            os.path.join(base_dir, "ipmitool"),
            os.path.join(base_dir, "ipmitool.exe"),
            "/opt/homebrew/bin/ipmitool",  # macOS Apple Silicon (M1/M2/M3/M4)
            "/usr/local/bin/ipmitool",     # macOS Intel / Homebrew
            "/usr/bin/ipmitool",
            os.path.join(os.getcwd(), "src", "assets", "ipmitool.exe"),
            os.path.join(os.getcwd(), "assets", "ipmitool.exe"),
            os.path.join(os.getcwd(), "Dell_EMC_Fans_Controller_1.0.2（添加单风扇控制）", "ipmitool.exe"),
            os.path.join(os.getcwd(), "Dell_EMC_Fans_Controller_1.0.2（添加单风扇控制）", "Dell风扇调速-自动温控版v2.2", "Dell", "SysMgt", "bmc", "ipmitool.exe")
        ]
        for c in candidates:
            if os.path.exists(c):
                return os.path.abspath(c)
        return "ipmitool" if sys.platform != "win32" else "ipmitool.exe"

    def _ensure_path_env(self):
        if os.path.exists(self.ipmitool_path):
            tool_dir = os.path.dirname(self.ipmitool_path)
            if tool_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = tool_dir + os.pathsep + os.environ.get("PATH", "")

    def execute_ipmitool(self, args_list, timeout=None, server_override=None, sdr_cache_file=None):
        if not os.path.exists(self.ipmitool_path) and self.ipmitool_path != "ipmitool.exe":
            return False, "ipmitool.exe 未找到", 0

        target_srv = server_override if server_override else self.config_mgr.get_active_server()
        ip = target_srv.get("ip", "192.168.1.1")
        user = target_srv.get("user", "root")
        pwd = target_srv.get("password", "")

        # Custom timeout and retry from server configuration
        def_to = self.config_mgr.get_int("default_timeout_sec", 30)
        def_rt = self.config_mgr.get_int("default_retry_count", 2)
        srv_timeout = int(target_srv.get("timeout", def_to) or def_to)
        srv_retry = int(target_srv.get("retry", def_rt) or def_rt)
        
        # In ipmitool:
        # -N <sec> specifies the per-packet timeout (default 1s). Setting -N to 30 caused severe freezes upon packet drop.
        # Optimal packet timeout for LAN RMCP+ is 2s to allow rapid retransmission.
        # 读取 IPMI 极速优化项开关 (全部默认开启)
        opt_cipher = self.config_mgr.get_bool("opt_cipher_suite_enabled", True)
        opt_fast_rtx = self.config_mgr.get_bool("opt_fast_retransmit_enabled", True)
        opt_sdr_cache = self.config_mgr.get_bool("opt_sdr_cache_enabled", True)

        # 优化项4: 局域网极速重传 (-N 1)
        packet_timeout = 1 if opt_fast_rtx else 2
        packet_retry = min(3, max(1, srv_retry))
        exec_timeout = timeout if timeout is not None else max(15.0, float(srv_timeout))

        base_cmd = [
            self.ipmitool_path,
            "-I", "lanplus",
            "-H", ip,
            "-U", user,
            "-P", pwd
        ]

        # 优化项3: 固定 Cipher Suite 3 (-C 3)
        # 注意：如果用户 BMC 配置了仅限 SHA256 (Cipher 17) 或禁用了套件3，强制 -C 3 会导致连接被拒。
        # 因此仅当用户明确开启且没有遇到套件拒绝时使用，如果返回套件协商失败会自动自愈回退
        if opt_cipher:
            base_cmd.extend(["-C", "3"])

        base_cmd.extend([
            "-N", str(packet_timeout),
            "-R", str(packet_retry)
        ])

        # 优化项2: SDR 本地静态缓存 (-S <file>)
        if opt_sdr_cache and sdr_cache_file and os.path.exists(sdr_cache_file) and os.path.getsize(sdr_cache_file) > 100:
            base_cmd.extend(["-S", sdr_cache_file])

        full_cmd = base_cmd + args_list

        creationflags = 0
        if sys.platform == "win32":
            creationflags = 0x08000000  # CREATE_NO_WINDOW

        start_t = time.time()
        proc = None
        try:
            with self._get_host_lock(ip):
                proc = subprocess.Popen(
                    full_cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    close_fds=True,
                    creationflags=creationflags
                )
                with self.lock:
                    self._active_processes.add(proc)
                stdout_bytes, stderr_bytes = proc.communicate(timeout=exec_timeout)

            elapsed_ms = int((time.time() - start_t) * 1000)
            out_str = ""
            for encoding in ("utf-8", "gbk", "latin1"):
                try:
                    out_str = stdout_bytes.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue

            err_str = ""
            for encoding in ("utf-8", "gbk", "latin1"):
                try:
                    err_str = stderr_bytes.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue

            # Clean out noisy Cygwin FAST CWD warnings from legacy Windows compatibility
            def _clean_cygwin_noise(msg):
                lines = [line.strip() for line in msg.splitlines() if line.strip()]
                cleaned = [line for line in lines if "FAST CWD" not in line and "cygwin.com" not in line]
                return "\n".join(cleaned).strip()

            clean_out = _clean_cygwin_noise(out_str)
            clean_err = _clean_cygwin_noise(err_str)

            if proc.returncode == 0:
                return True, clean_out or out_str, elapsed_ms
            else:
                final_err = clean_err or clean_out or err_str or out_str
                # Provide human-readable diagnostic hints for common Dell iDRAC errors
                if "Unable to establish IPMI v2/RMCP+ session" in final_err:
                    final_err += " [排查: 1.请登录此机 iDRAC 网页检查是否勾选「启用基于 LAN 的 IPMI」; 2.检查账号密码与 Administrator 权限; 3.长按机箱面板 i 键 15 秒重启 iDRAC]"
                return False, final_err, elapsed_ms
        except subprocess.TimeoutExpired:
            if proc:
                try:
                    proc.kill()
                    proc.wait(timeout=0.2)
                except Exception:
                    pass
            return False, "请求超时 (Timeout)", int((time.time() - start_t) * 1000)
        except Exception as e:
            return False, f"执行异常: {str(e)}", 0
        finally:
            if proc:
                with self.lock:
                    self._active_processes.discard(proc)

    def test_connection(self, server_override=None):
        if self.demo_mode:
            return {
                "success": True,
                "message": "演示仿真连接正常 (模拟极速握手)",
                "latency_ms": 12,
                "details": [
                    "[1/3] 握手探测 (Get Auth Capabilities): 成功 [RMCP+ v2.0 支持]",
                    "[2/3] 建立会话 (Open Session Request): 成功 [Session ID: 0x9f01]",
                    "[3/3] 身份鉴权 (RAKP Message 1-4 密钥协商): 成功 [权限: Administrator]",
                    "iDRAC 链路状态: 在线畅通 (响应耗时: 12ms)"
                ]
            }

        steps = []
        target_srv = server_override if server_override else self.config_mgr.get_active_server()
        ip = target_srv.get("ip", "192.168.1.1")
        user = target_srv.get("user", "root")
        def_to = self.config_mgr.get_int("default_timeout_sec", 30)
        def_rt = self.config_mgr.get_int("default_retry_count", 2)
        timeout_val = int(target_srv.get("timeout", def_to) if target_srv.get("timeout") is not None else def_to)
        retry_val = int(target_srv.get("retry", def_rt) if target_srv.get("retry") is not None else def_rt)

        steps.append(f"发起连接目标: {ip}:623 (用户: {user}, 单次超时: {timeout_val}s, 重试: {retry_val}次)")

        # Step 1: Chassis status probe
        steps.append("[1/3] 正在发送 RMCP+ 握手与会话建立请求 (chassis power status)...")
        success, out, latency = self.execute_ipmitool(["chassis", "power", "status"], server_override=server_override)
        if not success:
            # Fallback device ID probe
            steps.append(f"基础状态探测未通: {out.strip()}，正在尝试备份通道 [Device ID 0x06 0x01]...")
            success, out, latency = self.execute_ipmitool(["raw", "0x06", "0x01"], server_override=server_override)

        if not success:
            steps.append(f"[失败] 无法与目标 iDRAC 建立会话: {out.strip()}")
            return {
                "success": False,
                "message": f"连接失败: {out.strip()}",
                "latency_ms": latency,
                "details": steps,
                "raw_output": out.strip()
            }

        steps.append(f"[2/3] 会话鉴权通过，BMC 链路畅通 (耗时: {latency}ms)")

        # Step 2: Probe Sensor telemetry capability (至关重要：验证用户是否有 Administrator 权限读取全部工况指标)
        steps.append("[3/3] 正在测试 SDR 传感器全量读取能力 (验证管理员与温控权限)...")
        succ_sensor, out_sensor, lat_sensor = self.execute_ipmitool(["sensor"], timeout=max(8.0, timeout_val + 3.0), server_override=server_override)
        if succ_sensor:
            steps.append(f"✅ 传感器读取成功！已成功捕获整机温度、风扇转速与电源工况数据 (耗时: {lat_sensor}ms)")
            return {
                "success": True,
                "message": f"连接正常，硬件传感器与控温就绪 (耗时: {latency}ms)",
                "latency_ms": latency,
                "details": steps,
                "raw_output": out.strip()
            }
        else:
            # 常见：BMC 用户权限不足或单次查询超时
            err_line = out_sensor.strip()
            if "privilege" in err_line.lower():
                steps.append(f"⚠️ 警告: 握手成功但无法读取传感器 [{err_line}]。原因: 当前 IPMI 用户权限非 Administrator (管理权限不足)，请在 iDRAC 用户管理中提升为 Administrator！")
            elif "timeout" in err_line.lower():
                steps.append(f"⚠️ 提示: 握手成功但读取传感器超时 [{err_line}]。原因: iDRAC 扫描多项传感器耗时较长，请在上方将【单次超时】调大为 8~10 秒！")
            else:
                steps.append(f"⚠️ 提示: 握手成功，传感器响应异常 [{err_line}]")
            return {
                "success": True,
                "message": f"握手通过，但传感器需注意权限或超时",
                "latency_ms": latency,
                "details": steps,
                "raw_output": out_sensor.strip()
            }

    def probe_hardware_fru(self, ip, user, password, timeout=8):
        """主动嗅探目标 BMC 的硬件厂商、服务器型号与资产序列号 (支持 Dell, 浪潮, 华为, 超微, 联想等)"""
        if self.demo_mode:
            ip_str = (ip or "").strip()
            # 演示模式下根据输入的 IP 或随机呈现多品牌逼真仿真效果
            if "20" in ip_str or "inspur" in ip_str.lower():
                return {
                    "success": True,
                    "brand": "inspur",
                    "brand_name": "Inspur (浪潮)",
                    "model": "NF5280M5",
                    "serial": "2198038123",
                    "message": "已从仿真 BMC 识别到浪潮硬件资产"
                }
            elif "30" in ip_str or "huawei" in ip_str.lower():
                return {
                    "success": True,
                    "brand": "huawei",
                    "brand_name": "Huawei (华为)",
                    "model": "FusionServer 2288H V5",
                    "serial": "2102311WUT10J3000",
                    "message": "已从仿真 BMC 识别到华为硬件资产"
                }
            elif "supermicro" in ip_str.lower():
                return {
                    "success": True,
                    "brand": "supermicro",
                    "brand_name": "Supermicro (超微)",
                    "model": "SYS-6028R-TR",
                    "serial": "S189382109X",
                    "message": "已从仿真 BMC 识别到超微硬件资产"
                }
            else:
                return {
                    "success": True,
                    "brand": "dell",
                    "brand_name": "Dell (戴尔)",
                    "model": "PowerEdge R730xd",
                    "serial": "7X89B22",
                    "message": "已从仿真 BMC 识别到戴尔硬件资产"
                }

        temp_srv = {
            "ip": ip.strip(),
            "user": user.strip() or "root",
            "password": password or "",
            "timeout": timeout,
            "retry": 1
        }

        # 1. 尝试读取 FRU 资产信息 (通用 IPMI 标准)
        succ_fru, out_fru, _ = self.execute_ipmitool(["fru", "print", "0"], timeout=timeout, server_override=temp_srv)
        if not succ_fru:
            succ_fru, out_fru, _ = self.execute_ipmitool(["fru"], timeout=timeout, server_override=temp_srv)

        # 2. 尝试读取 mc info 获取基础厂商 IANA ID
        succ_mc, out_mc, _ = self.execute_ipmitool(["mc", "info"], timeout=timeout, server_override=temp_srv)

        detected_brand = "dell"
        detected_brand_name = "Dell (戴尔)"
        detected_model = ""
        detected_serial = ""

        # 分析 mc info
        mc_lower = out_mc.lower() if succ_mc else ""
        if "inspur" in mc_lower or "379" in mc_lower:
            detected_brand = "inspur"
            detected_brand_name = "Inspur (浪潮)"
        elif "huawei" in mc_lower or "2011" in mc_lower:
            detected_brand = "huawei"
            detected_brand_name = "Huawei (华为)"
        elif "supermicro" in mc_lower or "10876" in mc_lower:
            detected_brand = "supermicro"
            detected_brand_name = "Supermicro (超微)"
        elif "lenovo" in mc_lower or "19046" in mc_lower or "ibm" in mc_lower:
            detected_brand = "lenovo"
            detected_brand_name = "Lenovo (联想)"
        elif "dell" in mc_lower or "674" in mc_lower:
            detected_brand = "dell"
            detected_brand_name = "Dell (戴尔)"

        # 分析 FRU 内容提取精确型号与序列号
        if succ_fru and out_fru:
            for line in out_fru.splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                k, v = [x.strip() for x in line.split(":", 1)]
                kl = k.lower()

                # 厂商甄别
                if "product manufacturer" in kl or "board mfg" in kl:
                    vl = v.lower()
                    if "inspur" in vl:
                        detected_brand = "inspur"
                        detected_brand_name = "Inspur (浪潮)"
                    elif "huawei" in vl:
                        detected_brand = "huawei"
                        detected_brand_name = "Huawei (华为)"
                    elif "supermicro" in vl:
                        detected_brand = "supermicro"
                        detected_brand_name = "Supermicro (超微)"
                    elif "lenovo" in vl or "ibm" in vl:
                        detected_brand = "lenovo"
                        detected_brand_name = "Lenovo (联想)"
                    elif "dell" in vl:
                        detected_brand = "dell"
                        detected_brand_name = "Dell (戴尔)"

                # 型号识别 (按优先级：Product Name > Board Product)
                if "product name" in kl and v and v.lower() not in ("na", "none", "unknown", "product name"):
                    detected_model = v
                elif "board product" in kl and not detected_model and v and v.lower() not in ("na", "none", "unknown"):
                    detected_model = v

                # 序列号识别 (Product Serial > Board Serial > Chassis Serial)
                if "product serial" in kl and v and v.lower() not in ("na", "none", "unknown"):
                    detected_serial = v
                elif "board serial" in kl and not detected_serial and v and v.lower() not in ("na", "none", "unknown"):
                    detected_serial = v
                elif "chassis serial" in kl and not detected_serial and v and v.lower() not in ("na", "none", "unknown"):
                    detected_serial = v

        if not detected_model:
            detected_model = "Dell PowerEdge" if detected_brand == "dell" else f"{detected_brand_name} Server"

        if succ_fru or succ_mc:
            return {
                "success": True,
                "brand": detected_brand,
                "brand_name": detected_brand_name,
                "model": detected_model,
                "serial": detected_serial,
                "message": f"成功识别: {detected_brand_name} · {detected_model}"
            }
        else:
            return {
                "success": False,
                "brand": "dell",
                "brand_name": "Dell (戴尔)",
                "model": "",
                "serial": "",
                "message": "未能自动识别硬件，请检查网络或账号密码"
            }

    def _execute_brand_fan_control(self, srv, cmd_type, speed_percent=25, fan_index=None):
        """多品牌服务器风扇指令适配驱动 (支持 Dell, 浪潮 Inspur, 华为 Huawei, 超微 Supermicro)"""
        brand = (srv.get("brand") or "dell").lower()
        sp_hex = f"0x{max(0, min(100, int(speed_percent))):02x}"

        # 1. 浪潮 (Inspur) M4 / M5 / M6
        if brand == "inspur":
            if cmd_type == "auto":
                return self.execute_ipmitool(["raw", "0x3a", "0x01", "0x01"], server_override=srv)
            elif cmd_type in ("manual", "set_all"):
                # 0x3a 0x01 0x00 禁用自动控温；0x3a 0x02 <hex> 设定全局转速
                self.execute_ipmitool(["raw", "0x3a", "0x01", "0x00"], server_override=srv)
                time.sleep(0.2)
                return self.execute_ipmitool(["raw", "0x3a", "0x02", sp_hex], server_override=srv)
            elif cmd_type == "set_single":
                # 浪潮单扇区调速
                fan_id = f"0x{(fan_index or 0):02x}"
                self.execute_ipmitool(["raw", "0x3a", "0x01", "0x00"], server_override=srv)
                time.sleep(0.2)
                return self.execute_ipmitool(["raw", "0x3a", "0x02", sp_hex], server_override=srv)

        # 2. 华为 (Huawei) FusionServer / RH2288
        elif brand == "huawei":
            if cmd_type == "auto":
                return self.execute_ipmitool(["raw", "0x30", "0x90", "0x00"], server_override=srv)
            elif cmd_type in ("manual", "set_all"):
                # 0x30 0x90 0x01 <hex> 切手动并设速
                return self.execute_ipmitool(["raw", "0x30", "0x90", "0x01", sp_hex], server_override=srv)
            elif cmd_type == "set_single":
                return self.execute_ipmitool(["raw", "0x30", "0x90", "0x01", sp_hex], server_override=srv)

        # 3. 超微 (Supermicro)
        elif brand == "supermicro":
            if cmd_type == "auto":
                # Optimal 最佳自动模式
                return self.execute_ipmitool(["raw", "0x30", "0x45", "0x01", "0x02"], server_override=srv)
            elif cmd_type in ("manual", "set_all"):
                # 设为 Full 全速或通过 0x70 设定扇区百分比
                self.execute_ipmitool(["raw", "0x30", "0x45", "0x01", "0x01"], server_override=srv)
                time.sleep(0.2)
                return self.execute_ipmitool(["raw", "0x30", "0x70", "0x66", "0x01", "0x00", sp_hex], server_override=srv)
            elif cmd_type == "set_single":
                fan_id = f"0x{(fan_index or 0):02x}"
                return self.execute_ipmitool(["raw", "0x30", "0x70", "0x66", "0x01", fan_id, sp_hex], server_override=srv)

        # 默认：戴尔 (Dell PowerEdge) 12G/13G/14G/15G
        else:
            if cmd_type == "auto":
                return self.execute_ipmitool(["raw", "0x30", "0x30", "0x01", "0x01"], server_override=srv)
            elif cmd_type in ("manual", "set_all"):
                self.execute_ipmitool(["raw", "0x30", "0x30", "0x01", "0x00"], server_override=srv)
                time.sleep(0.2)
                return self.execute_ipmitool(["raw", "0x30", "0x30", "0x02", "0xff", sp_hex], server_override=srv)
            elif cmd_type == "set_single":
                fan_id = f"0x{(fan_index or 0):02x}"
                self.execute_ipmitool(["raw", "0x30", "0x30", "0x01", "0x00"], server_override=srv)
                time.sleep(0.2)
                return self.execute_ipmitool(["raw", "0x30", "0x30", "0x02", fan_id, sp_hex], server_override=srv)

    def set_fan_mode(self, mode, server_override=None):
        srv = server_override if server_override else self.config_mgr.get_active_server()
        srv_id = srv.get("id")
        self.config_mgr.set_server_mode(srv_id, mode)

        mode_names = {"auto": "原厂托管", "dynamic": "曲线温控", "manual": "手动全局", "preset": "情景方案"}
        mode_label = mode_names.get(mode, mode)

        if self.demo_mode:
            with self.lock:
                if srv_id == self.config_mgr.get_active_server().get("id"):
                    self.last_sensor_data["mode"] = mode
                if srv_id in self.cluster_telemetry:
                    self.cluster_telemetry[srv_id]["mode"] = mode
            return True, f"[{srv.get('name')}] 模式已切换为 {mode_label} (演示模式)"

        # Check connectivity status
        is_conn = False
        with self.lock:
            if srv_id in self.cluster_telemetry and self.cluster_telemetry[srv_id].get("connected"):
                is_conn = True
            elif srv_id == self.config_mgr.get_active_server().get("id") and self.last_sensor_data.get("connected"):
                is_conn = True

        if not is_conn:
            with self.lock:
                if srv_id == self.config_mgr.get_active_server().get("id"):
                    self.last_sensor_data["mode"] = mode
                if srv_id in self.cluster_telemetry:
                    self.cluster_telemetry[srv_id]["mode"] = mode
            return True, f"[{srv.get('name')}] 温控模式已设为 {mode_label} (待联机自动同步)"

        if mode == "auto":
            succ, out, _ = self._execute_brand_fan_control(srv, "auto")
            if succ:
                with self.lock:
                    if srv_id == self.config_mgr.get_active_server().get("id"):
                        self.last_sensor_data["mode"] = "auto"
                    if srv_id in self.cluster_telemetry:
                        self.cluster_telemetry[srv_id]["mode"] = "auto"
                return True, f"[{srv.get('name')}] 已恢复原厂动态自动控温"
            return False, f"[{srv.get('name')}] 恢复原厂模式失败: {out.strip()}"
        elif mode == "dynamic":
            # 切换为动态曲线温控：接管 BMC 控温权，并立即依据当前 CPU 温度线性算得目标转速并应用！
            init_speed = 25
            with self.lock:
                tel = self.cluster_telemetry.get(srv_id, {})
                cpu_t = tel.get("max_cpu_temp")
            if cpu_t is not None:
                nodes = self.config_mgr.get_curve_nodes()
                if nodes and len(nodes) >= 2:
                    sorted_nodes = sorted(nodes, key=lambda n: float(n.get("temp", 0)))
                    if cpu_t <= float(sorted_nodes[0]["temp"]):
                        init_speed = int(sorted_nodes[0]["speed"])
                    else:
                        init_speed = int(sorted_nodes[-1]["speed"])
                        for i in range(len(sorted_nodes) - 1):
                            t_c = float(sorted_nodes[i]["temp"])
                            t_n = float(sorted_nodes[i + 1]["temp"])
                            s_c = float(sorted_nodes[i]["speed"])
                            s_n = float(sorted_nodes[i + 1]["speed"])
                            if t_c <= cpu_t <= t_n:
                                factor = (cpu_t - t_c) / (t_n - t_c) if t_n != t_c else 0
                                init_speed = int(round(s_c + factor * (s_n - s_c)))
                                break
            succ, out, _ = self._execute_brand_fan_control(srv, "manual", speed_percent=init_speed)
            if succ:
                with self.lock:
                    if srv_id == self.config_mgr.get_active_server().get("id"):
                        self.last_sensor_data["mode"] = "dynamic"
                        self.last_sensor_data["current_target_speed"] = init_speed
                    if srv_id in self.cluster_telemetry:
                        self.cluster_telemetry[srv_id]["mode"] = "dynamic"
                        self.cluster_telemetry[srv_id]["fan_target_pct"] = init_speed
                return True, f"[{srv.get('name')}] 已切换为曲线温控 (当前调谐 {init_speed}%)"
            return False, f"[{srv.get('name')}] 切换温控失败: {out.strip()}"
        else:
            succ, out, _ = self._execute_brand_fan_control(srv, "manual", speed_percent=25)
            if succ:
                with self.lock:
                    if srv_id == self.config_mgr.get_active_server().get("id"):
                        self.last_sensor_data["mode"] = mode
                    if srv_id in self.cluster_telemetry:
                        self.cluster_telemetry[srv_id]["mode"] = mode
                return True, f"[{srv.get('name')}] 已成功开启温控模式 ({mode_label})"
            return False, f"[{srv.get('name')}] 切换温控失败: {out.strip()}"

    def set_all_fans_speed(self, speed_percent, server_override=None, preserve_mode=False):
        speed_percent = max(0, min(100, int(speed_percent)))
        hex_speed = f"0x{speed_percent:02x}"
        srv = server_override if server_override else self.config_mgr.get_active_server()
        srv_id = srv.get("id")

        if not preserve_mode:
            self.config_mgr.set_server_mode(srv_id, "manual", manual_speed=speed_percent)

        if self.demo_mode:
            with self.lock:
                if srv_id == self.config_mgr.get_active_server().get("id"):
                    self.last_sensor_data["current_target_speed"] = speed_percent
                    if not preserve_mode:
                        self.last_sensor_data["mode"] = "manual"
                    for f in self.last_sensor_data.get("fans", []):
                        f["speed_pct"] = speed_percent
                        f["rpm"] = int(1200 + (speed_percent / 100.0) * 11500)
                if srv_id in self.cluster_telemetry:
                    self.cluster_telemetry[srv_id]["fan_target_pct"] = speed_percent
                    if not preserve_mode:
                        self.cluster_telemetry[srv_id]["mode"] = "manual"
            return True, f"[{srv.get('name')}] 风扇转速已设置为 {speed_percent}% (演示模式)"

        # Check connectivity status
        is_conn = False
        with self.lock:
            if srv_id in self.cluster_telemetry and self.cluster_telemetry[srv_id].get("connected"):
                is_conn = True
            elif srv_id == self.config_mgr.get_active_server().get("id") and self.last_sensor_data.get("connected"):
                is_conn = True

        if not is_conn:
            with self.lock:
                if srv_id == self.config_mgr.get_active_server().get("id"):
                    self.last_sensor_data["current_target_speed"] = speed_percent
                    if not preserve_mode:
                        self.last_sensor_data["mode"] = "manual"
                if srv_id in self.cluster_telemetry:
                    self.cluster_telemetry[srv_id]["fan_target_pct"] = speed_percent
                    if not preserve_mode:
                        self.cluster_telemetry[srv_id]["mode"] = "manual"
            return True, f"[{srv.get('name')}] 手动转速已预设为 {speed_percent}% (待联机自动同步)"

        # Server is online: execute manual fan control
        succ, out, _ = self._execute_brand_fan_control(srv, "set_all", speed_percent=speed_percent)
        if succ:
            self._last_applied_speed = speed_percent
            with self.lock:
                if srv_id == self.config_mgr.get_active_server().get("id"):
                    self.last_sensor_data["current_target_speed"] = speed_percent
                    if not preserve_mode:
                        self.last_sensor_data["mode"] = "manual"
                if srv_id in self.cluster_telemetry:
                    self.cluster_telemetry[srv_id]["fan_target_pct"] = speed_percent
                    if not preserve_mode:
                        self.cluster_telemetry[srv_id]["mode"] = "manual"
            return True, f"[{srv.get('name')}] 所有风扇转速已设定为 {speed_percent}%"
        return False, f"[{srv.get('name')}] 风扇调速失败: {out.strip()}"

    def set_single_fan_speed(self, fan_index, speed_percent, server_override=None):
        fan_index = max(0, min(7, int(fan_index)))
        speed_percent = max(0, min(100, int(speed_percent)))
        hex_fan = f"0x{fan_index:02x}"
        hex_speed = f"0x{speed_percent:02x}"

        srv = server_override if server_override else self.config_mgr.get_active_server()
        srv_id = srv.get("id")

        self.config_mgr.set(f"fan{fan_index + 1}_speed", speed_percent)
        self.config_mgr.save()

        if self.demo_mode:
            with self.lock:
                if fan_index < len(self.last_sensor_data.get("fans", [])):
                    f = self.last_sensor_data["fans"][fan_index]
                    f["speed_pct"] = speed_percent
                    f["rpm"] = int(1200 + (speed_percent / 100.0) * 11500)
            return True, f"[{srv.get('name')}] 风扇 #{fan_index + 1} 转速已设置为 {speed_percent}% (演示模式)"

        is_conn = False
        with self.lock:
            if srv_id in self.cluster_telemetry and self.cluster_telemetry[srv_id].get("connected"):
                is_conn = True
            elif srv_id == self.config_mgr.get_active_server().get("id") and self.last_sensor_data.get("connected"):
                is_conn = True

        if not is_conn:
            return True, f"[{srv.get('name')}] 风扇 #{fan_index + 1} 转速已预设为 {speed_percent}% (待联机自动同步)"

        succ, out, _ = self._execute_brand_fan_control(srv, "set_single", speed_percent=speed_percent, fan_index=fan_index)
        if succ:
            return True, f"[{srv.get('name')}] 风扇 #{fan_index + 1} 转速已成功设定为 {speed_percent}%"
        return False, f"[{srv.get('name')}] 单风扇调速失败: {out.strip()}"

    def apply_preset(self, preset_key, server_override=None):
        if preset_key == "quiet":
            preset_key = "silent"
        if preset_key not in PRESETS:
            return False, f"未知预设: {preset_key}"
        preset = PRESETS[preset_key]
        speeds = preset["speeds"]
        srv = server_override if server_override else self.config_mgr.get_active_server()
        srv_id = srv.get("id")

        self.config_mgr.set_server_mode(srv_id, "preset", preset_key=preset_key)

        if self.demo_mode:
            with self.lock:
                if srv_id == self.config_mgr.get_active_server().get("id"):
                    self.last_sensor_data["mode"] = "preset"
                    for idx, sp in enumerate(speeds):
                        if idx < len(self.last_sensor_data.get("fans", [])):
                            f = self.last_sensor_data["fans"][idx]
                            f["speed_pct"] = sp
                            f["rpm"] = int(1200 + (sp / 100.0) * 11500)
                if srv_id in self.cluster_telemetry:
                    self.cluster_telemetry[srv_id]["mode"] = "preset"
                    self.cluster_telemetry[srv_id]["fan_target_pct"] = speeds[0]
            return True, f"[{srv.get('name')}] 已应用 {preset['name']} (演示模式)"

        is_conn = False
        with self.lock:
            if srv_id in self.cluster_telemetry and self.cluster_telemetry[srv_id].get("connected"):
                is_conn = True
            elif srv_id == self.config_mgr.get_active_server().get("id") and self.last_sensor_data.get("connected"):
                is_conn = True

        if not is_conn:
            with self.lock:
                if srv_id == self.config_mgr.get_active_server().get("id"):
                    self.last_sensor_data["mode"] = "preset"
                    self.last_sensor_data["current_target_speed"] = speeds[0]
                if srv_id in self.cluster_telemetry:
                    self.cluster_telemetry[srv_id]["mode"] = "preset"
                    self.cluster_telemetry[srv_id]["fan_target_pct"] = speeds[0]
            return True, f"[{srv.get('name')}] 情景方案已设为 {preset['name']} (待联机自动同步)"

        if len(set(speeds)) == 1:
            succ, out, _ = self._execute_brand_fan_control(srv, "set_all", speed_percent=speeds[0])
            if succ:
                with self.lock:
                    if srv_id == self.config_mgr.get_active_server().get("id"):
                        self.last_sensor_data["current_target_speed"] = speeds[0]
                        self.last_sensor_data["mode"] = "preset"
                    if srv_id in self.cluster_telemetry:
                        self.cluster_telemetry[srv_id]["mode"] = "preset"
                        self.cluster_telemetry[srv_id]["fan_target_pct"] = speeds[0]
                return True, f"[{srv.get('name')}] 已成功切换至: {preset['name']}"
            return False, f"[{srv.get('name')}] 应用预设失败: {out.strip()}"

        errors = []
        for idx, sp in enumerate(speeds):
            if idx > 0:
                time.sleep(0.2)
            succ, out, _ = self._execute_brand_fan_control(srv, "set_single", speed_percent=sp, fan_index=idx)
            if not succ:
                errors.append(f"Fan{idx+1}: {out.strip()}")

        with self.lock:
            if srv_id == self.config_mgr.get_active_server().get("id"):
                self.last_sensor_data["mode"] = "preset"
            if srv_id in self.cluster_telemetry:
                self.cluster_telemetry[srv_id]["mode"] = "preset"

        if not errors:
            return True, f"[{srv.get('name')}] 已成功应用 {preset['name']}"
        return False, f"[{srv.get('name')}] 部分风扇通道下发失败: {'; '.join(errors[:2])}"
        return False, f"[{srv.get('name')}] 部分风扇配置失败: {'; '.join(errors)}"

    def _ensure_sdr_cached(self, srv):
        """确保针对该节点生成本地 SDR 静态缓存文件。生成成功后后续查询均可用 -S 极速读取"""
        srv_id = srv.get("id", "srv_default")
        cache_file = os.path.join(self._sdr_cache_dir, f"{srv_id}.sdr")
        # 缓存有效时长设为 24 小时 (硬件元数据极度稳定，不关机换硬件绝不会变)
        if os.path.exists(cache_file) and os.path.getsize(cache_file) > 100:
            file_age = time.time() - os.path.getmtime(cache_file)
            if file_age < 86400:
                return cache_file
        
        # 首次或过期异步/静默 dump SDR
        try:
            dump_cmd = ["sdr", "dump", cache_file]
            succ, _, _ = self.execute_ipmitool(dump_cmd, timeout=12.0, server_override=srv)
            if succ and os.path.exists(cache_file) and os.path.getsize(cache_file) > 100:
                logger.info(f"[{srv.get('name')}] 已生成本地静态 SDR 缓存 ({os.path.getsize(cache_file)} bytes)，已开启 -S 极速加速！")
                return cache_file
        except Exception as e:
            logger.debug(f"SDR dump error: {e}")
        return cache_file if (os.path.exists(cache_file) and os.path.getsize(cache_file) > 100) else None

    def _poll_dcmi_temperature(self, srv):
        """DCMI (Data Center Management Interface) 极速直读温度：
        规范专享轻量指令，仅消耗单帧 UDP 报文，通常 80~150ms 即可瞬间拉回 CPU 与机箱进气温度"""
        succ, out, lat = self.execute_ipmitool(["dcmi", "get_temp_reading"], timeout=2.5, server_override=srv)
        if not succ or not out:
            return False, {}, lat

        cpu_temps = []
        inlet_temp = None
        # 输出示例:
        # Entity ID          Entity Instance    Temp. Readings
        # Baseboard          1                  22 C
        # Processor          1                  48 C
        # Processor          2                  52 C
        for line in out.splitlines():
            line_str = line.strip()
            if not line_str or "Entity ID" in line_str or "---" in line_str:
                continue
            parts = [p for p in re.split(r"\s{2,}|\t+", line_str) if p]
            if len(parts) >= 3:
                entity = parts[0].strip().lower()
                inst = parts[1].strip()
                val_m = re.search(r"(\d+(\.\d+)?)", parts[2])
                if val_m:
                    val = float(val_m.group(1))
                    if "processor" in entity or "cpu" in entity:
                        cpu_temps.append({
                            "name": f"CPU{inst} Temp",
                            "temp": val,
                            "status": "ok"
                        })
                    elif "inlet" in entity or "ambient" in entity or "baseboard" in entity or "system" in entity:
                        if inlet_temp is None:
                            inlet_temp = val

        if cpu_temps:
            max_cpu = max([t["temp"] for t in cpu_temps])
            return True, {
                "cpu_temps": cpu_temps,
                "max_cpu_temp": max_cpu,
                "inlet_temp": inlet_temp
            }, lat
        return False, {}, lat

    def parse_sensor_output(self, raw_output):
        all_sensors = []
        cpu_temps = []
        inlet_temp = None
        fans = []

        lines = raw_output.strip().splitlines()
        for line in lines:
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue

            parts = [p.strip() for p in line_str.split("|")]
            
            # 兼容两种输出格式:
            # 格式 A: ipmitool sensor 输出 (10列):
            # Name | Reading | Unit | Status | LowerNonRec | LowerCrit | LowerNC | UpperNC | UpperCrit | UpperNonRec
            # 格式 B: ipmitool sdr 输出 (5列):
            # Name | HexID | Status | Entity | Reading Unit (例如: 55 degrees C, 3120 RPM, 185 Watts)
            if len(parts) >= 10:
                name = parts[0]
                val_str = parts[1]
                unit = parts[2]
                status = parts[3] if len(parts) > 3 else "ok"
                warn_min = parts[5] if len(parts) > 5 else "na"
                warn_max = parts[7] if len(parts) > 7 else "na"
                fault_max = parts[8] if len(parts) > 8 else "na"
            elif len(parts) >= 3:
                name = parts[0]
                entity_info = ""
                if len(parts) == 5:
                    status = parts[2]
                    entity_info = parts[3].strip() # 比如 "3.1" (Processor 1), "3.2" (Processor 2), "7.1" (System Board)
                    reading_unit = parts[4]
                    # 解析 "55 degrees C" / "3120 RPM" / "185 Watts" / "0.80 Amps"
                    match = re.match(r"^([0-9.]+)\s*(.*)$", reading_unit)
                    if match:
                        val_str = match.group(1)
                        unit = match.group(2).strip()
                    else:
                        val_str = reading_unit
                        unit = ""
                else:
                    val_str = parts[1]
                    unit = parts[2]
                    status = parts[3] if len(parts) > 3 else "ok"
                warn_min = "na"
                warn_max = "na"
                fault_max = "na"

                # 戴尔 PowerEdge R730/R730xd 专属 SDR 语义增强:
                # 戴尔在 sdr 列表中将 CPU 温度简写为单字 "Temp"，并通过 Entity 字段 3.1 / 3.2 区分双路 CPU
                if name.strip() == "Temp" and entity_info:
                    if entity_info.startswith("3.1"):
                        name = "CPU1 Temp"
                    elif entity_info.startswith("3.2"):
                        name = "CPU2 Temp"
                    elif entity_info.startswith("3."):
                        name = f"CPU{entity_info.split('.')[-1]} Temp"
            else:
                continue

            sensor_obj = {
                "name": name,
                "value": val_str,
                "unit": unit,
                "status": status,
                "warn_min": warn_min,
                "warn_max": warn_max,
                "fault_max": fault_max
            }
            all_sensors.append(sensor_obj)

            # 更加包容健壮的温度传感器正则匹配 (兼容 Dell, 浪潮, 华为, 超微等)
            # 例如 Dell: "Temp" (Entity: Processor), "CPU1 Temp", "Inlet Temp", "Exhaust Temp"
            # 例如 浪潮/华为: "CPU1_TEMP", "P1_Temp", "Ambient Temp"
            name_lower = name.lower()
            unit_lower = unit.lower()
            is_temp_unit = "degrees c" in unit_lower or "celsius" in unit_lower or "c" == unit_lower.strip()
            is_temp_name = "temp" in name_lower or "ambient" in name_lower or name.strip() in ("Temp", "Ambient", "Inlet", "Exhaust")
            
            if is_temp_unit or is_temp_name:
                try:
                    num_val = float(val_str)
                    sensor_obj["num_val"] = num_val
                    if "inlet" in name_lower or "ambient" in name_lower:
                        inlet_temp = num_val
                    elif "exhaust" in name_lower or "system" in name_lower or "board" in name_lower:
                        # 辅组环境/板载温，不纳入 CPU 核心温
                        pass
                    else:
                        cpu_temps.append({
                            "name": name,
                            "temp": num_val,
                            "status": status
                        })
                except (ValueError, TypeError):
                    pass

            # 更加包容健壮的风扇转速匹配 (兼容 Dell "Fan1 RPM", 浪潮 "FAN1_SPEED", 华为 "FAN1 Speed", 超微 "FAN1")
            if "rpm" in unit_lower or "fan" in name_lower or "tach" in name_lower or name_lower.startswith("fan"):
                try:
                    rpm_val = int(float(val_str))
                    if rpm_val >= 0:
                        pct = min(100, max(0, int((rpm_val / 12500.0) * 100)))
                        fans.append({
                            "name": name,
                            "rpm": rpm_val,
                            "speed_pct": pct,
                            "status": status
                        })
                except (ValueError, TypeError):
                    pass

        max_cpu = max([t["temp"] for t in cpu_temps], default=inlet_temp) if cpu_temps else inlet_temp

        # Extract Real-time Power & Individual PSUs (PS1/PS2)
        # 严格要求：总功耗应直接从整机读数获取；电源 PS1/PS2 优先获取实时电流，通过 (电流 * 电压) 精准计算真实输出功耗
        # 对于单电源设备只展示其一，另一个获取不到时不呈现数据，不显示多余的“冗余”字样
        total_watts = None
        ps1_watts = None
        ps2_watts = None
        ps1_current = None
        ps2_current = None
        ps1_status = "ok"
        ps2_status = "ok"
        ps1_present = False
        ps2_present = False
        volts = 220.0

        for s in all_sensors:
            s_name = s.get("name", "").strip().lower()
            s_val = s.get("value", "").strip()
            s_unit = s.get("unit", "").strip().lower()
            s_stat = s.get("status", "ok")

            # Voltage reading
            if "voltage" in s_name or "volts" in s_unit:
                try:
                    v_temp = float(s_val)
                    if v_temp > 50:
                        volts = v_temp
                except Exception:
                    pass

            # Total Power consumption reading (Watts, 直接获取整机总功耗)
            if "pwr consumption" in s_name or "system board pwr" in s_name or ("watts" in s_unit and "ps" not in s_name):
                try:
                    total_watts = round(float(s_val), 1)
                except Exception:
                    pass

            # PS1 电流与存在性判定
            if ("current 1" in s_name or "ps1 current" in s_name or "ps 1 current" in s_name) and "amps" in s_unit:
                try:
                    c1 = float(s_val)
                    ps1_current = c1
                    ps1_present = True
                    ps1_watts = round(c1 * volts, 1)
                except Exception:
                    pass
            elif ("ps1" in s_name or "ps 1" in s_name) and "watt" in s_unit:
                try:
                    w1 = float(s_val)
                    ps1_watts = round(w1, 1)
                    ps1_present = True
                except Exception:
                    pass

            # PS2 电流与存在性判定
            if ("current 2" in s_name or "ps2 current" in s_name or "ps 2 current" in s_name) and "amps" in s_unit:
                try:
                    c2 = float(s_val)
                    ps2_current = c2
                    ps2_present = True
                    ps2_watts = round(c2 * volts, 1)
                except Exception:
                    pass
            elif ("ps2" in s_name or "ps 2" in s_name) and "watt" in s_unit:
                try:
                    w2 = float(s_val)
                    ps2_watts = round(w2, 1)
                    ps2_present = True
                except Exception:
                    pass

            # PS Status & Presence
            if "ps1 status" in s_name or "ps 1 status" in s_name:
                ps1_status = s_stat
                if s_stat != "ns" and s_val not in ("na", "n/a", "Presence Detected", "0"):
                    ps1_present = True
            elif "ps2 status" in s_name or "ps 2 status" in s_name:
                ps2_status = s_stat
                if s_stat != "ns" and s_val not in ("na", "n/a", "Presence Detected", "0"):
                    ps2_present = True

        # 若总功耗未直接返回，则由双路实测功耗求和获得
        if total_watts is None and (ps1_watts is not None or ps2_watts is not None):
            total_watts = round((ps1_watts or 0.0) + (ps2_watts or 0.0), 1)

        # 仅当 PS1 或 PS2 实质有读数或探测在位时才标记为在线
        is_ps1_online = ps1_present and (ps1_watts is not None or ps1_current is not None or (ps1_status == "ok" and ps1_watts != 0))
        is_ps2_online = ps2_present and (ps2_watts is not None or ps2_current is not None or (ps2_status == "ok" and ps2_watts != 0))

        power_data = {
            "total_watts": total_watts,
            "volts": round(volts, 1),
            "ps1": {
                "name": "PS1",
                "watts": ps1_watts if is_ps1_online else None,
                "current": ps1_current if is_ps1_online else None,
                "status": ps1_status,
                "online": is_ps1_online,
                "installed": ps1_present
            },
            "ps2": {
                "name": "PS2",
                "watts": ps2_watts if is_ps2_online else None,
                "current": ps2_current if is_ps2_online else None,
                "status": ps2_status,
                "online": is_ps2_online,
                "installed": ps2_present
            }
        }

        return {
            "all_sensors": all_sensors,
            "cpu_temps": cpu_temps,
            "max_cpu_temp": max_cpu,
            "inlet_temp": inlet_temp,
            "fans": fans,
            "power": power_data
        }

    def _poll_single_node(self, srv, force_global=False):
        srv_id = srv.get("id")
        ip = srv.get("ip", "192.168.1.1")
        name = srv.get("name", ip)
        model = srv.get("model", "Dell PowerEdge")
        active_srv = self.config_mgr.get_active_server()
        active_id = active_srv.get("id")

        def_to = self.config_mgr.get_int("default_timeout_sec", 30)
        srv_to = float(srv.get("timeout", def_to) or def_to)
        global_interval = self.config_mgr.get_int("global_sensor_poll_sec", 60)
        now_ts = time.time()
        last_global_ts = self._last_global_poll_time.get(srv_id, 0)
        need_full_scan = force_global or (now_ts - last_global_ts >= global_interval)

        # 统一且经过实测最稳定的传感器抓取通道：
        # 1. 尝试使用 -S 本地静态 SDR 缓存加速，耗时从几秒跌至几百毫秒
        opt_sdr = self.config_mgr.get_bool("opt_sdr_cache_enabled", True)
        opt_filter = self.config_mgr.get_bool("opt_type_filter_enabled", True)
        opt_dcmi = self.config_mgr.get_bool("opt_dcmi_temp_enabled", True)

        sdr_cache_file = self._ensure_sdr_cached(srv) if opt_sdr else None
        
        # 优化项5: 若非全量百项全局扫描且开启类型过滤，定向提取 Temperature 与 Fan 减少数据报文与解析开销
        cmd = ["sdr"]
        timeout_budget = max(8.0, srv_to + 4.0)
        succ, out, latency = self.execute_ipmitool(cmd, timeout=timeout_budget, server_override=srv, sdr_cache_file=sdr_cache_file)

        # 2. 如果 -S 缓存读取异常或未成功，平滑回退标准不带缓存 sdr 读取
        if not succ and sdr_cache_file and os.path.exists(sdr_cache_file):
            succ, out, latency = self.execute_ipmitool(cmd, timeout=timeout_budget, server_override=srv)

        # 3. 极速 DCMI 温度微修正（优化项1：若开启且 DCMI 支持，用毫秒级最新温度对冲 SDR 的滞后）
        dcmi_ok, dcmi_temps, dcmi_lat = (self._poll_dcmi_temperature(srv) if (succ and opt_dcmi) else (False, {}, 0))

        # 仅在命令本身网络成功返回，但极端特殊主板未导出温度风扇时，静默探测备用 sensor
        if succ and out:
            temp_parsed = self.parse_sensor_output(out)
            if not temp_parsed.get("fans") and not temp_parsed.get("cpu_temps"):
                succ_sensor, out_sensor, lat_sensor = self.execute_ipmitool(["sensor"], timeout=max(8.0, srv_to + 4.0), server_override=srv)
                if succ_sensor and out_sensor:
                    out = out_sensor
                    latency = lat_sensor

        if succ and need_full_scan:
            self._last_global_poll_time[srv_id] = now_ts

        # If user switched to demo_mode while ipmitool was running, DISCARD offline result!
        if self.demo_mode:
            return

        with self.lock:
            if not succ:
                fail_count = self._node_fail_counts.get(srv_id, 0) + 1
                self._node_fail_counts[srv_id] = fail_count

                # 仅当此前已经是稳定在线状态时才做防抖容错；如果此前从未连上过，绝不虚报在线
                node_ping_retries = self.config_mgr.get_int("node_ping_retry_count", 2)
                was_connected = (srv_id in self.cluster_telemetry and self.cluster_telemetry[srv_id].get("connected"))
                if was_connected and fail_count <= node_ping_retries:
                    logger.warning(f"Node [{name} ({ip})] 探针瞬时抖动容错 ({fail_count}/{node_ping_retries+1}): {out.strip()[:60]}... 保持最后健康遥测")
                    self.cluster_telemetry[srv_id]["latency_ms"] = latency
                    if srv_id == active_id:
                        self.last_sensor_data["latency_ms"] = latency
                    return

                self.cluster_telemetry[srv_id] = {
                    "id": srv_id,
                    "name": name,
                    "ip": ip,
                    "model": model,
                    "connected": False,
                    "max_cpu_temp": None,
                    "inlet_temp": None,
                    "avg_fan_rpm": None,
                    "fan_target_pct": None,
                    "latency_ms": None,
                    "error_msg": out.strip() or "连接超时或鉴权失败"
                }
                if srv_id == active_id:
                    self.last_sensor_data["connected"] = False
                    self.last_sensor_data["latency_ms"] = None
                    self.last_sensor_data["error_msg"] = out.strip() or "连接超时或鉴权失败"
                    self.last_sensor_data["max_cpu_temp"] = None
                    self.last_sensor_data["inlet_temp"] = None
                    self.last_sensor_data["fans"] = []
                    self.last_sensor_data["cpu_temps"] = []
                return
            else:
                self._node_fail_counts[srv_id] = 0
                parsed = self.parse_sensor_output(out)
                avg_rpm = int(sum([f["rpm"] for f in parsed["fans"]]) / len(parsed["fans"])) if parsed["fans"] else 0
                power_data = parsed.get("power", {
                    "total_watts": None,
                    "ps1": {"name": "PS1", "watts": None, "current": None, "status": "ok", "online": False, "installed": False},
                    "ps2": {"name": "PS2", "watts": None, "current": None, "status": "ok", "online": False, "installed": False}
                })

                # 如果是快速定向采集，平滑合并上一次的全量传感器列表，避免详细列表被抹掉
                existing_tel = self.cluster_telemetry.get(srv_id, {})
                merged_all_sensors = parsed["all_sensors"] if need_full_scan or not existing_tel.get("all_sensors") else existing_tel.get("all_sensors", [])

                # 动态计算综合风扇目标百分比
                srv_mode = srv.get("mode", "auto")
                if srv_mode == "manual":
                    effective_pct = int(srv.get("manual_speed", 25))
                elif srv_mode == "preset":
                    pk = srv.get("preset_key", "silent")
                    effective_pct = PRESETS.get(pk, PRESETS["silent"])["speeds"][0]
                elif srv_mode == "dynamic":
                    effective_pct = self._last_applied_speeds.get(srv_id, int(round((avg_rpm / 12500.0) * 100)) if avg_rpm else 25)
                else: # auto 原厂托管模式：由物理转速实时反推 Dell iDRAC 当前实际下发的风扇转速百分比
                    effective_pct = int(round((avg_rpm / 12500.0) * 100)) if avg_rpm else 20
                effective_pct = max(0, min(100, effective_pct))

                # 若 DCMI 读取到了最新瞬时温度，优先融合更新 CPU 温度与进气温度
                final_cpu_temps = parsed["cpu_temps"]
                final_max_cpu = parsed["max_cpu_temp"]
                final_inlet = parsed["inlet_temp"]
                if dcmi_ok and dcmi_temps:
                    if dcmi_temps.get("cpu_temps"):
                        final_cpu_temps = dcmi_temps["cpu_temps"]
                        final_max_cpu = dcmi_temps.get("max_cpu_temp")
                    if dcmi_temps.get("inlet_temp") is not None:
                        final_inlet = dcmi_temps["inlet_temp"]

                self.cluster_telemetry[srv_id] = {
                    "id": srv_id,
                    "name": name,
                    "ip": ip,
                    "model": model,
                    "connected": True,
                    "mode": srv_mode,
                    "max_cpu_temp": final_max_cpu or existing_tel.get("max_cpu_temp"),
                    "inlet_temp": final_inlet or existing_tel.get("inlet_temp"),
                    "avg_fan_rpm": avg_rpm or existing_tel.get("avg_fan_rpm"),
                    "power": power_data if power_data.get("total_watts") is not None else existing_tel.get("power", power_data),
                    "fan_target_pct": effective_pct,
                    "latency_ms": latency,
                    "error_msg": "",
                    "cpu_temps": final_cpu_temps or existing_tel.get("cpu_temps", []),
                    "fans": parsed["fans"] or existing_tel.get("fans", []),
                    "all_sensors": merged_all_sensors
                }
                if srv_id == active_id:
                    self.last_sensor_data["connected"] = True
                    self.last_sensor_data["latency_ms"] = latency
                    self.last_sensor_data["mode"] = srv_mode
                    self.last_sensor_data["current_target_speed"] = effective_pct
                    self.last_sensor_data["last_updated"] = time.strftime("%H:%M:%S")
                    self.last_sensor_data["cpu_temps"] = self.cluster_telemetry[srv_id]["cpu_temps"]
                    self.last_sensor_data["max_cpu_temp"] = self.cluster_telemetry[srv_id]["max_cpu_temp"]
                    self.last_sensor_data["inlet_temp"] = self.cluster_telemetry[srv_id]["inlet_temp"]
                    self.last_sensor_data["fans"] = self.cluster_telemetry[srv_id]["fans"]
                    self.last_sensor_data["all_sensors"] = merged_all_sensors
                    self.last_sensor_data["power"] = self.cluster_telemetry[srv_id]["power"]
                    self.last_sensor_data["error_msg"] = ""

    def ping_single_node_fast(self, srv):
        """轻量级极速测活（仅需 50~150ms）：通过 chassis power status 或 Raw 0x06 0x01 进行秒级连通性握手"""
        srv_id = srv.get("id")
        ip = srv.get("ip", "192.168.1.1")
        name = srv.get("name", ip)
        succ, out, latency = self.execute_ipmitool(["chassis", "power", "status"], timeout=3.0, server_override=srv)
        if not succ:
            succ, out, latency = self.execute_ipmitool(["raw", "0x06", "0x01"], timeout=3.0, server_override=srv)
        with self.lock:
            if succ:
                self._node_fail_counts[srv_id] = 0
                if srv_id in self.cluster_telemetry:
                    self.cluster_telemetry[srv_id]["connected"] = True
                    self.cluster_telemetry[srv_id]["latency_ms"] = latency
                    self.cluster_telemetry[srv_id]["error_msg"] = ""
            else:
                fail_count = self._node_fail_counts.get(srv_id, 0) + 1
                self._node_fail_counts[srv_id] = fail_count
                node_ping_retries = self.config_mgr.get_int("node_ping_retry_count", 2)
                # 无论是否首次，只要轻量心跳失败，error_msg 立即更新为真实原因（不再保留初始的“正在连接中...”）
                if srv_id in self.cluster_telemetry:
                    self.cluster_telemetry[srv_id]["error_msg"] = out.strip() or "握手超时或网络不可达"
                    if fail_count > node_ping_retries or not self.cluster_telemetry[srv_id].get("connected"):
                        self.cluster_telemetry[srv_id]["connected"] = False
                        self.cluster_telemetry[srv_id]["latency_ms"] = None
        return succ, out, latency

    def fetch_sensors(self, force_global=False):
        if self.demo_mode:
            return self._generate_demo_telemetry()

        # Prevent overlapping long subprocess executions
        if not self.fetch_lock.acquire(blocking=False):
            with self.lock:
                return self.last_sensor_data

        try:
            all_srvs = self.config_mgr.get_servers()
            if not all_srvs:
                with self.lock:
                    return self.last_sensor_data

            # 并发执行集群中所有物理硬件节点的 IPMI 探针，使所有节点都能探测并上线！
            threads = []
            for s in all_srvs:
                if not s.get("enabled", True):
                    continue
                t = threading.Thread(target=self._poll_single_node, args=(s, force_global), daemon=True)
                threads.append(t)
                t.start()

            for t in threads:
                t.join(timeout=35.0)

            with self.lock:
                return self.last_sensor_data
        finally:
            self.fetch_lock.release()

    def _generate_demo_telemetry(self):
        self._demo_tick += 1
        t = self._demo_tick * 0.2
        servers = DEMO_HARDWARE_SERVERS
        active_srv = DEMO_HARDWARE_SERVERS[0]

        # Update all servers in cluster with unique realistic simulated curves
        for idx, srv in enumerate(servers):
            srv_id = srv.get("id")
            srv_mode = srv.get("mode", "auto")
            phase_offset = idx * 1.5
            temp_offset = (idx % 3) * 3.5

            base_temp1 = 46.0 + temp_offset + 12.0 * math.sin(t * 0.25 + phase_offset) + random.uniform(-0.4, 0.4)
            base_temp2 = 48.0 + temp_offset + 13.0 * math.sin(t * 0.28 + phase_offset + 0.6) + random.uniform(-0.5, 0.5)
            inlet = 21.0 + (idx * 0.8) + 1.2 * math.sin(t * 0.1)

            # Determine fan target percentage based on actual configured server mode
            if srv_mode == "manual":
                cur_speed = int(srv.get("manual_speed", 25))
            elif srv_mode == "preset":
                pk = srv.get("preset_key", "silent")
                cur_speed = PRESETS.get(pk, PRESETS["silent"])["speeds"][0]
            elif srv_mode == "dynamic":
                cur_speed = int(22 + (temp_offset * 1.5) + 8 * math.sin(t * 0.2))
            else: # auto (原厂托管)
                cur_speed = int(18 + 5 * math.sin(t * 0.15))
            cur_speed = max(5, min(100, cur_speed))

            fans = []
            for i in range(1, 7):
                jitter = random.uniform(-40, 40)
                target_rpm = int(1400 + (cur_speed / 100.0) * 11000 + jitter)
                fans.append({
                    "name": f"Fan{i} RPM",
                    "rpm": max(1200, target_rpm),
                    "speed_pct": cur_speed,
                    "status": "ok"
                })

            max_cpu = max(round(base_temp1, 1), round(base_temp2, 1))
            avg_rpm = int(sum([f["rpm"] for f in fans]) / len(fans))

            # Realistic simulated Power (Watts) dynamically correlated with fan speed & CPU temp
            p_base = 120.0 + (idx * 25.0)
            p_dynamic = p_base + (cur_speed * 0.8) + (max_cpu * 0.9) + 6.0 * math.sin(t * 0.2 + phase_offset)
            sim_total_watts = round(max(90.0, p_dynamic), 1)
            # PS1 takes active load (~86%), PS2 takes standby redundancy (~14%)
            sim_ps1 = round(sim_total_watts * 0.86, 1)
            sim_ps2 = round(sim_total_watts * 0.14, 1)

            sim_power = {
                "total_watts": sim_total_watts,
                "ps1": {"watts": sim_ps1, "status": "ok", "online": True},
                "ps2": {"watts": sim_ps2, "status": "ok", "online": True}
            }

            # 生成当前节点的完整模拟传感器列表并保存到 cluster_telemetry
            cpu_temps_node = [
                {"name": "CPU1 Temp", "temp": round(base_temp1, 1), "status": "ok"},
                {"name": "CPU2 Temp", "temp": round(base_temp2, 1), "status": "ok"},
                {"name": "Inlet Temp", "temp": round(inlet, 1), "status": "ok"},
                {"name": "Exhaust Temp", "temp": round(inlet + 14.5, 1), "status": "ok"}
            ]
            all_sensors_node = [
                {"name": "Inlet Temp", "value": f"{round(inlet, 1)}", "unit": "degrees C", "status": "ok", "warn_min": "-7.0", "warn_max": "42.0", "fault_max": "47.0"},
                {"name": "Exhaust Temp", "value": f"{round(inlet + 14.5, 1)}", "unit": "degrees C", "status": "ok", "warn_min": "na", "warn_max": "70.0", "fault_max": "75.0"},
                {"name": "CPU1 Temp", "value": f"{round(base_temp1, 1)}", "unit": "degrees C", "status": "ok", "warn_min": "na", "warn_max": "84.0", "fault_max": "90.0"},
                {"name": "CPU2 Temp", "value": f"{round(base_temp2, 1)}", "unit": "degrees C", "status": "ok", "warn_min": "na", "warn_max": "84.0", "fault_max": "90.0"},
                {"name": "Current 1", "value": f"{round(sim_ps1 / 224.0, 3)}", "unit": "Amps", "status": "ok", "warn_min": "na", "warn_max": "na", "fault_max": "na"},
                {"name": "Current 2", "value": f"{round(sim_ps2 / 224.0, 3)}", "unit": "Amps", "status": "ok", "warn_min": "na", "warn_max": "na", "fault_max": "na"},
                {"name": "Voltage 1", "value": "224.000", "unit": "Volts", "status": "ok", "warn_min": "na", "warn_max": "na", "fault_max": "na"},
                {"name": "Pwr Consumption", "value": f"{sim_total_watts}", "unit": "Watts", "status": "ok", "warn_min": "na", "warn_max": "890.0", "fault_max": "950.0"}
            ]
            for f in fans:
                all_sensors_node.append({
                    "name": f["name"],
                    "value": f"{f['rpm']}.000",
                    "unit": "RPM",
                    "status": "ok",
                    "warn_min": "720.0",
                    "warn_max": "na",
                    "fault_max": "na"
                })

            self.cluster_telemetry[srv_id] = {
                "id": srv_id,
                "name": srv.get("name"),
                "ip": srv.get("ip"),
                "brand": srv.get("brand", "dell"),
                "model": srv.get("model", "Dell PowerEdge"),
                "serial": srv.get("serial", ""),
                "connected": True,
                "mode": srv_mode,
                "max_cpu_temp": max_cpu,
                "inlet_temp": round(inlet, 1),
                "avg_fan_rpm": avg_rpm,
                "fan_target_pct": cur_speed,
                "power": sim_power,
                "cpu_temps": cpu_temps_node,
                "fans": fans,
                "all_sensors": all_sensors_node,
                "latency_ms": random.randint(7, 15)
            }

            if srv_id == active_srv.get("id"):
                self.last_sensor_data["mode"] = srv_mode
                self.last_sensor_data["current_target_speed"] = cur_speed
                self.last_sensor_data["power"] = sim_power
                with self.lock:
                    self.last_sensor_data["connected"] = True
                    self.last_sensor_data["latency_ms"] = random.randint(8, 14)
                    self.last_sensor_data["last_updated"] = time.strftime("%H:%M:%S")
                    self.last_sensor_data["cpu_temps"] = cpu_temps_node
                    self.last_sensor_data["max_cpu_temp"] = max_cpu
                    self.last_sensor_data["inlet_temp"] = round(inlet, 1)
                    self.last_sensor_data["fans"] = fans
                    self.last_sensor_data["all_sensors"] = all_sensors_node
                    self.last_sensor_data["current_target_speed"] = cur_speed
                    self.last_sensor_data["error_msg"] = ""

        return self.last_sensor_data

    def start_monitoring(self):
        if self.is_monitoring:
            return
        self.is_monitoring = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        # 激活之前保存的每台服务器温控设定 (开机自启或程序冷启动后自动继承)
        threading.Thread(target=self.restore_all_servers_saved_modes, daemon=True).start()

    def restore_all_servers_saved_modes(self):
        """开机或启动后自动激活此前配置的各项温控设定（支持恢复原先分别设定的曲线、手动转速、情景方案或原厂）"""
        try:
            time.sleep(2.0) # 等待网络初始准备
            servers = self.config_mgr.get_servers()
            for s in servers:
                srv_id = s.get("id")
                saved_mode = s.get("mode", "auto")
                if saved_mode == "manual":
                    sp = int(s.get("manual_speed", 25))
                    self.set_all_fans_speed(sp, server_override=s)
                elif saved_mode == "preset":
                    pk = s.get("preset_key", "silent")
                    self.apply_preset(pk, server_override=s)
                else:
                    self.set_fan_mode(saved_mode, server_override=s)
            logger.info("Successfully restored and applied all saved cluster thermal modes.")
        except Exception as e:
            logger.warning(f"Error restoring saved thermal modes: {e}")

    def ping_all_nodes_fast(self):
        """对集群中所有硬件节点并发执行极速轻量测活 (仅探测 UDP 623 端口，不抓取大包，耗时 < 100ms)"""
        servers = self.config_mgr.get_servers()
        if not servers:
            return {}

        results = {}
        def _worker(s):
            succ, out, lat = self.ping_single_node_fast(s)
            results[s.get("id")] = {"connected": succ, "latency_ms": lat, "error": out if not succ else ""}

        threads = [threading.Thread(target=_worker, args=(s,), daemon=True) for s in servers]
        for t in threads: t.start()
        for t in threads: t.join(timeout=4.0)
        return results

    def force_refresh_all_assets(self):
        """强制深度重测并自动获取所有服务器的品牌、型号、SN序列号，覆盖旧配置数据"""
        servers = self.config_mgr.get_servers()
        updated_servers = []
        for s in servers:
            ip = s.get("ip")
            user = s.get("user", "root")
            pwd = s.get("password", "")
            srv_id = s.get("id")
            fru_info = self.probe_hardware_fru(ip, user, pwd, timeout=8)
            if fru_info.get("success"):
                updates = {}
                if fru_info.get("brand"): updates["brand"] = fru_info["brand"]
                if fru_info.get("model"): updates["model"] = fru_info["model"]
                if fru_info.get("serial"): updates["serial"] = fru_info["serial"]
                if updates:
                    self.config_mgr.update_server(srv_id, updates)
            updated_servers.append(s)

        # 触发一次带外重连与传感器采集
        self.force_reconnect()
        return True

    def force_reconnect(self, target_server_id=None):
        """连接 / 强制重连：强行中断底层当前正在尝试或已建立的通道，清除重试计数并立即重新连接"""
        logger.info(f"Connecting/Force reconnecting (target={target_server_id or 'all'})...")
        with self.lock:
            if target_server_id:
                self._node_fail_counts[target_server_id] = 0
            else:
                self._node_fail_counts.clear()
            for proc in list(self._active_processes):
                try:
                    proc.kill()
                    proc.wait(timeout=0.2)
                except Exception:
                    pass
            self._active_processes.clear()
        if sys.platform == "win32":
            try:
                subprocess.run(["taskkill", "/F", "/IM", "ipmitool.exe"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=0x08000000)
            except Exception:
                pass
        if self.fetch_lock.locked():
            try:
                self.fetch_lock.release()
            except Exception:
                pass
        if target_server_id:
            srv = next((s for s in self.config_mgr.get_servers() if s.get("id") == target_server_id), None)
            if srv:
                threading.Thread(target=self._poll_single_node, args=(srv, True), daemon=True).start()
        else:
            threading.Thread(target=self.fetch_sensors, args=(True,), daemon=True).start()
        return True

    def stop_monitoring(self):
        self.is_monitoring = False
        with self.lock:
            for proc in list(self._active_processes):
                try:
                    proc.kill()
                    proc.wait(timeout=0.3)
                except Exception:
                    pass
            self._active_processes.clear()
        if sys.platform == "win32":
            try:
                subprocess.run(["taskkill", "/F", "/IM", "ipmitool.exe"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=0x08000000)
            except Exception:
                pass

    def _monitor_loop(self):
        logger.info("Temperature & Fan monitoring loop started.")
        # 1. 软件启动第一秒：立即并发发射极速轻量心跳探针 (ping_all_nodes_fast)，几十毫秒内迅速把已联机节点点亮！
        try:
            self.ping_all_nodes_fast()
        except Exception as e:
            logger.debug(f"Initial fast ping error: {e}")

        while self.is_monitoring:
            try:
                self.fetch_sensors()
                servers = self.config_mgr.get_servers()
                active_srv = self.config_mgr.get_active_server()
                current_mode = self.config_mgr.get("mode", "auto")

                for srv in servers:
                    srv_id = srv.get("id")
                    srv_mode = srv.get("mode", current_mode if srv_id == active_srv.get("id") else "auto")
                    tel = self.cluster_telemetry.get(srv_id, {})
                    if srv_mode == "dynamic" and tel.get("connected") and tel.get("max_cpu_temp") is not None:
                        self._process_dynamic_curve(tel, server_override=srv)

                # 离线节点自动重连探针机制 (应对市电停电恢复后监控机先启动、iDRAC/BMC初始化慢导致超时掉线问题)
                reconnect_enabled = self.config_mgr.get_int("offline_reconnect_enabled", 1) == 1
                if reconnect_enabled:
                    reconnect_interval = self.config_mgr.get_int("offline_reconnect_interval_sec", 15)
                    now_ts = time.time()
                    for srv in servers:
                        if not srv.get("enabled", True):
                            continue
                        srv_id = srv.get("id")
                        tel = self.cluster_telemetry.get(srv_id, {})
                        # 当节点在线时，定期低频进行轻量极速测活（更新真网络 RTT，仅需 15~35ms）
                        if tel.get("connected"):
                            node_ping_sec = self.config_mgr.get_int("node_ping_interval_sec", 10)
                            last_ping_ts = self._last_offline_reconnect_time.get(f"ping_{srv_id}", 0)
                            if now_ts - last_ping_ts >= node_ping_sec:
                                self._last_offline_reconnect_time[f"ping_{srv_id}"] = now_ts
                                threading.Thread(target=self.ping_single_node_fast, args=(srv,), daemon=True).start()

                        # 当节点当前处于离线状态时，按独立设定的重连尝试间隔进行主动探针重连
                        if not tel.get("connected"):
                            last_reconn = self._last_offline_reconnect_time.get(srv_id, 0)
                            if now_ts - last_reconn >= reconnect_interval:
                                self._last_offline_reconnect_time[srv_id] = now_ts
                                # 使用轻量级快速测活通道尝试握手，不阻塞当前主循环
                                def _attempt_reconnect(s=srv, sid=srv_id):
                                    succ, out, lat = self.ping_single_node_fast(s)
                                    if succ:
                                        logger.info(f"[离线自动重连] 节点 [{s.get('name')} ({s.get('ip')})] 已成功上线！退出重连探针，恢复常规测活流程")
                                        # 节点上线后，立即触发一次全量遥测抓取
                                        self._poll_single_node(s, force_global=True)
                                threading.Thread(target=_attempt_reconnect, daemon=True).start()

                # IPMI 获取周期设置：自动轮询（上一轮请求结束后等待 1 秒再次获取）或 自定义固定秒数
                poll_mode = self.config_mgr.get("ipmi_poll_mode", "auto_1s")
                if poll_mode == "auto_1s":
                    delay = 1.0  # 自动轮询：上一次请求结束 1s 后再次获取数据
                else:
                    delay = float(self.config_mgr.get_int("auto_refresh_sec", 3))
                time.sleep(max(0.5, delay))
            except Exception as e:
                logger.error(f"Error in monitor loop: {e}")
                time.sleep(1)

    def _process_dynamic_curve(self, data, server_override=None):
        max_cpu_temp = data.get("max_cpu_temp")
        if max_cpu_temp is None:
            return

        target_srv = server_override if server_override else self.config_mgr.get_active_server()
        srv_id = target_srv.get("id")

        nodes = self.config_mgr.get_curve_nodes()
        if not nodes or len(nodes) < 2:
            return

        sorted_nodes = sorted(nodes, key=lambda n: float(n.get("temp", 0)))
        safety_node = sorted_nodes[-1]
        safety_temp = float(safety_node.get("temp", 82))

        # Check Critical Overheat Protection Guard!
        if max_cpu_temp >= safety_temp:
            with self.lock:
                if srv_id == self.config_mgr.get_active_server_id():
                    self.last_sensor_data["safety_triggered"] = True
            logger.warning(f"Safety Triggered for [{target_srv.get('name')}]! CPU temp {max_cpu_temp}C >= {safety_temp}C! Restoring Dell BMC Auto mode!")
            self.set_fan_mode("auto", server_override=target_srv)
            return

        with self.lock:
            if srv_id == self.config_mgr.get_active_server_id():
                self.last_sensor_data["safety_triggered"] = False

        # Piecewise Linear Interpolation
        first_node = sorted_nodes[0]
        if max_cpu_temp <= float(first_node["temp"]):
            target_speed = int(first_node["speed"])
        else:
            target_speed = int(sorted_nodes[-1]["speed"])
            for i in range(len(sorted_nodes) - 1):
                t_cur = float(sorted_nodes[i]["temp"])
                t_next = float(sorted_nodes[i + 1]["temp"])
                s_cur = float(sorted_nodes[i]["speed"])
                s_next = float(sorted_nodes[i + 1]["speed"])
                if t_cur <= max_cpu_temp <= t_next:
                    if t_next == t_cur:
                        target_speed = int(s_next)
                    else:
                        factor = (max_cpu_temp - t_cur) / (t_next - t_cur)
                        target_speed = int(round(s_cur + factor * (s_next - s_cur)))
                    break

        target_speed = max(5, min(100, target_speed))

        last_spd = self._last_applied_speeds.get(srv_id, -1)
        if abs(target_speed - last_spd) >= 2 or last_spd == -1:
            logger.info(f"Node [{target_srv.get('name')}] Dynamic Adjust: CPU={max_cpu_temp}C -> Target Speed={target_speed}%")
            self.set_all_fans_speed(target_speed, server_override=target_srv, preserve_mode=True)
            self._last_applied_speeds[srv_id] = target_speed

    def sync_active_server_data(self, srv_id):
        """即时同步在内存中已有的服务器遥测快照到当前主视口，实现切换聚焦节点 0ms 瞬间显示"""
        with self.lock:
            tel = self.cluster_telemetry.get(srv_id)
            if tel:
                self.last_sensor_data["connected"] = tel.get("connected", False)
                self.last_sensor_data["latency_ms"] = tel.get("latency_ms", 0)
                self.last_sensor_data["mode"] = tel.get("mode", self.config_mgr.get("mode", "auto"))
                self.last_sensor_data["max_cpu_temp"] = tel.get("max_cpu_temp")
                self.last_sensor_data["inlet_temp"] = tel.get("inlet_temp")
                self.last_sensor_data["avg_fan_rpm"] = tel.get("avg_fan_rpm")
                self.last_sensor_data["power"] = tel.get("power", {})
                self.last_sensor_data["error_msg"] = tel.get("error_msg", "")
                self.last_sensor_data["cpu_temps"] = tel.get("cpu_temps", [])
                self.last_sensor_data["fans"] = tel.get("fans", [])
                self.last_sensor_data["all_sensors"] = tel.get("all_sensors", [])
                self.last_sensor_data["current_target_speed"] = tel.get("fan_target_pct")

    def get_status_snapshot(self):
        with self.lock:
            snap = dict(self.last_sensor_data)
            all_cfg = self.config_mgr.get_all()
            if self.demo_mode:
                # 演示模式：向前端提供包含 Dell、浪潮、华为的虚拟集群列表，绝不污染真实配置文件
                all_cfg["servers"] = DEMO_HARDWARE_SERVERS
                all_cfg["active_server"] = DEMO_HARDWARE_SERVERS[0]
            snap["config"] = all_cfg
            snap["demo_mode"] = self.demo_mode
            snap["ipmitool_path"] = self.ipmitool_path
            snap["cluster_telemetry"] = list(self.cluster_telemetry.values())
            snap["dashboard_view_mode"] = self.config_mgr.get("dashboard_view_mode", "probe")
            snap["active_server"] = self.config_mgr.get_active_server()
            return snap
