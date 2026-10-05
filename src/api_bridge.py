import os
import sys
import time
import math
import random
try:
    import winreg
except ImportError:
    winreg = None
import webbrowser
import threading
import logging
from log_manager import LogManager

logger = logging.getLogger("APIBridge")

REG_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_REG_NAME = "云枢"

def get_current_exe_path():
    if getattr(sys, "frozen", False):
        return sys.executable
    exe = os.path.abspath(os.path.join(os.getcwd(), "云枢.exe"))
    if os.path.exists(exe):
        return exe
    exe2 = os.path.abspath(os.path.join(os.getcwd(), "dist", "云枢.exe"))
    if os.path.exists(exe2):
        return exe2
    return sys.executable

def check_registry_autostart():
    if winreg is None:
        return False, ""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_READ)
        val, _ = winreg.QueryValueEx(key, APP_REG_NAME)
        winreg.CloseKey(key)
        return True, val
    except FileNotFoundError:
        return False, ""
    except Exception as e:
        return False, str(e)

def update_registry_autostart(enable: bool):
    if winreg is None:
        return False, "当前平台不支持 Windows 注册表开机自启"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_WRITE)
        if enable:
            target_path = get_current_exe_path()
            cmd_val = f'"{target_path}"'
            winreg.SetValueEx(key, APP_REG_NAME, 0, winreg.REG_SZ, cmd_val)
            winreg.CloseKey(key)
            return True, f"开机自启动已启用: {cmd_val}"
        else:
            try:
                winreg.DeleteValue(key, APP_REG_NAME)
            except FileNotFoundError:
                pass
            winreg.CloseKey(key)
            return True, "开机自启动已关闭"
    except Exception as e:
        logger.error(f"Error updating registry autostart: {e}")
        return False, f"配置开机自启失败: {str(e)}"

def check_system_autostart():
    if sys.platform == "win32" and winreg:
        return check_registry_autostart()
    elif sys.platform == "darwin":
        plist_path = os.path.expanduser("~/Library/LaunchAgents/com.dell.fansense.plist")
        return os.path.exists(plist_path), plist_path
    return False, ""

def update_system_autostart(enable: bool):
    if sys.platform == "win32" and winreg:
        return update_registry_autostart(enable)
    elif sys.platform == "darwin":
        plist_path = os.path.expanduser("~/Library/LaunchAgents/com.dell.fansense.plist")
        try:
            if enable:
                exe_path = get_current_exe_path()
                plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.dell.fansense</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>"""
                os.makedirs(os.path.dirname(plist_path), exist_ok=True)
                with open(plist_path, "w", encoding="utf-8") as f:
                    f.write(plist_content)
                return True, "已配置 macOS 登录自启动项"
            else:
                if os.path.exists(plist_path):
                    os.remove(plist_path)
                return True, "已关闭 macOS 登录自启动项"
        except Exception as e:
            return False, f"配置 macOS 自启失败: {e}"
    return False, "当前平台不支持开机自启动设置"

class APIBridge:
    def __init__(self, ipmi_core, config_mgr, ssh_probe_mgr=None, alert_engine=None, log_mgr=None):
        # NOTE: Attribute names must start with _ so pywebview's JS API generator
        # does not recursively scan their members/COM objects and deadlock the UI!
        self._ipmi_core = ipmi_core
        self._config_mgr = config_mgr
        self._ssh_probe_mgr = ssh_probe_mgr
        self._alert_engine = alert_engine
        self._log_mgr = log_mgr or (LogManager.get_instance(config_mgr) if config_mgr else LogManager.get_instance())
        self._window = None
        self._is_maximized = False
        self._node_uptime_tracker = {}

    def set_window(self, window):
        self._window = window

    def get_status(self):
        try:
            # If in demo mode, advance demo tick and trigger synthetic telemetry update on each poll
            if self._ipmi_core.demo_mode:
                self._ipmi_core.fetch_sensors()

            snap = self._ipmi_core.get_status_snapshot()
            is_reg_on, _ = check_system_autostart()
            snap["autostart_active"] = is_reg_on

            # Append System Servers (SSH) Data
            system_servers_data = []
            if self._ssh_probe_mgr:
                system_servers_data = self._ssh_probe_mgr.get_all_servers_telemetry()
                
            # Populate System Servers Telemetry:
            # If in demo mode, OR if a server cannot connect in physical mode, provide realistic telemetry so demo/unconnected states are never 0核0G!
            import random
            cfg_sys_srvs = self._config_mgr.get_system_servers()
            if self._ipmi_core.demo_mode:
                cfg_sys_srvs = [
                    {
                        "id": "srv_pve_bound",
                        "name": "PVE 虚拟化母机 (已绑定 Dell 节点)",
                        "host": "192.168.1.100",
                        "port": 22,
                        "username": "root",
                        "password": "",
                        "node_id": "srv_primary",
                        "os_name": "Proxmox VE 8.1",
                        "enabled": True
                    },
                    {
                        "id": "srv_ceph_bound",
                        "name": "Ceph OSD 存储守护 (已绑定浪潮节点)",
                        "host": "192.168.1.120",
                        "port": 22,
                        "username": "root",
                        "password": "",
                        "node_id": "srv_inspur",
                        "os_name": "Debian 12 Bookworm",
                        "enabled": True
                    },
                    {
                        "id": "srv_standalone_ubuntu",
                        "name": "独立应用服务器",
                        "host": "192.168.1.150",
                        "port": 22,
                        "username": "ubuntu",
                        "password": "",
                        "node_id": "",
                        "os_name": "Ubuntu 22.04 LTS",
                        "enabled": True
                    }
                ]
            system_servers_map = {s["id"]: s for s in system_servers_data} if system_servers_data else {}
            final_system_servers = []

            for s in cfg_sys_srvs:
                sid = s.get("id")
                real_tel = system_servers_map.get(sid)
                configured_os = s.get("os_name", "")
                is_bound = bool(s.get("node_id"))

                if self._ipmi_core.demo_mode:
                    # ONLY in demo mode: generate realistic smooth breathing demo baseline telemetry
                    cores = 16 if is_bound else 8
                    ram_total = 64.0 if is_bound else 32.0
                    
                    # Use demo tick and math.sin for smooth, realistic dynamic load fluctuations
                    dtick = getattr(self._ipmi_core, "_demo_tick", 0)
                    toffset = hash(sid) % 7
                    wave = math.sin((dtick * 0.3) + toffset)
                    base_cpu = 32.0 if is_bound else 24.0
                    cpu_p = max(5.0, min(95.0, round(base_cpu + 14.0 * wave + random.uniform(-1.5, 1.5), 1)))
                    
                    mem_p = max(20.0, min(90.0, round((45.0 if is_bound else 35.0) + 4.0 * math.sin((dtick * 0.1) + toffset) + random.uniform(-0.5, 0.5), 1)))
                    mem_u = round(ram_total * mem_p / 100.0, 1)
                    swap_p = round(max(2.0, min(40.0, 6.0 + 2.0 * math.sin(dtick * 0.05))), 1)
                    swap_u = round(8.0 * swap_p / 100.0, 1)
                    disk_p = 38 if is_bound else 29
                    disk_tot = 1024.0 if is_bound else 512.0
                    disk_u = round(disk_tot * disk_p / 100.0, 1)

                    simulated_entry = {
                        "id": sid,
                        "name": s.get("name"),
                        "host": s.get("host"),
                        "port": s.get("port", 22),
                        "node_id": s.get("node_id", ""),
                        "os_name": configured_os or ("Proxmox VE 8.1" if is_bound else "Ubuntu 22.04 LTS"),
                        "connected": True,
                        "latency_ms": random.randint(3, 14),
                        "last_error": "",
                        "last_updated": time.strftime("%H:%M:%S"),
                        "cpu_pct": cpu_p,
                        "cpu_cores": cores,
                        "load_1m": round(max(0.2, (cpu_p / 100.0) * (cores * 0.45)), 2),
                        "load_5m": round(max(0.3, (cpu_p / 100.0) * (cores * 0.40)), 2),
                        "mem_pct": mem_p,
                        "mem_used_gb": mem_u,
                        "mem_total_gb": ram_total,
                        "swap_pct": swap_p,
                        "swap_used_gb": swap_u,
                        "swap_total_gb": 8.0,
                        "disk_pct": disk_p,
                        "disk_used_gb": disk_u,
                        "disk_total_gb": disk_tot,
                        "uptime_sec": 86400 * 24 + 3600 * 5,
                        "hostname": "pve-cluster-node1" if is_bound else "app-server-standalone",
                        "username": s.get("username", ""),
                        "password": s.get("password", "")
                    }
                    final_system_servers.append(simulated_entry)
                else:
                    # In REAL mode: Strictly reflect actual SSH connection & liveness! Never fake online!
                    if real_tel and real_tel.get("connected"):
                        entry = dict(real_tel)
                        entry["node_id"] = s.get("node_id", "")
                        entry["name"] = s.get("name", entry.get("name", ""))
                        entry["username"] = s.get("username", "")
                        entry["password"] = s.get("password", "")
                        entry["os_name"] = configured_os or entry.get("os_name", "Linux OS")
                        entry["latency_ms"] = real_tel.get("latency_ms", 10)
                        final_system_servers.append(entry)
                    else:
                        err_msg = ""
                        if real_tel and real_tel.get("last_error"):
                            err_msg = real_tel.get("last_error")
                        else:
                            err_msg = f"未连接 ({s.get('host')}:{s.get('port', 22)})"

                        offline_entry = {
                            "id": sid,
                            "name": s.get("name"),
                            "host": s.get("host"),
                            "port": s.get("port", 22),
                            "username": s.get("username", ""),
                            "password": s.get("password", ""),
                            "node_id": s.get("node_id", ""),
                            "os_name": configured_os or (real_tel.get("os_name") if real_tel else "") or "Linux OS",
                            "connected": False,
                            "latency_ms": None,
                            "last_error": err_msg,
                            "last_updated": real_tel.get("last_updated", time.strftime("%H:%M:%S")) if real_tel else time.strftime("%H:%M:%S"),
                            "cpu_pct": 0.0,
                            "cpu_cores": real_tel.get("cpu_cores", 0) if real_tel else 0,
                            "load_1m": 0.0,
                            "load_5m": 0.0,
                            "mem_pct": 0.0,
                            "mem_used_gb": 0.0,
                            "mem_total_gb": real_tel.get("mem_total_gb", 0.0) if real_tel else 0.0,
                            "swap_pct": 0.0,
                            "swap_used_gb": 0.0,
                            "swap_total_gb": 0.0,
                            "disk_pct": 0.0,
                            "disk_used_gb": 0.0,
                            "disk_total_gb": real_tel.get("disk_total_gb", 0.0) if real_tel else 0.0,
                            "uptime_sec": 0,
                            "hostname": s.get("host")
                        }
                        final_system_servers.append(offline_entry)

            snap["system_servers"] = final_system_servers

            # Calculate / inherit node uptime (优先同步绑定的 Linux 宿主探针内核运行时间，无绑定时基于心跳状态自动追踪)
            cluster_tel = snap.get("cluster_telemetry", [])
            for node_t in cluster_tel:
                nid = node_t.get("id")
                bound_sys = next((s for s in final_system_servers if s.get("node_id") == nid and s.get("connected")), None)
                if bound_sys and bound_sys.get("uptime_sec"):
                    node_t["uptime_sec"] = bound_sys["uptime_sec"]
                elif self._ipmi_core.demo_mode:
                    node_t["uptime_sec"] = 86400 * 32 + 3600 * 8
                else:
                    if node_t.get("connected"):
                        if nid not in self._node_uptime_tracker:
                            self._node_uptime_tracker[nid] = time.time()
                        node_t["uptime_sec"] = int(time.time() - self._node_uptime_tracker[nid])
                    else:
                        self._node_uptime_tracker.pop(nid, None)
                        node_t["uptime_sec"] = 0

            # Append Subsystem poll interval setting
            snap["subsystem_poll_sec"] = self._config_mgr.get_int("subsystem_poll_sec", 2)

            # Check Alert Engine & Evaluate
            if self._alert_engine:
                cluster_tel = snap.get("cluster_telemetry", [])
                self._alert_engine.check_telemetry(snap, cluster_tel, snap["system_servers"])
                snap["alert_history"] = self._alert_engine.get_history()
                snap["alert_config"] = self._alert_engine.get_config()
            else:
                snap["alert_history"] = []
                snap["alert_config"] = {}

            return {"success": True, "data": snap}
        except Exception as e:
            logger.error(f"Error in get_status: {e}")
            return {"success": False, "error": str(e)}

    def force_reconnect(self, target_node_id=None):
        """连接 / 强制重连：中断目标或全部硬件节点与系统服务器通道，重新建立连接"""
        try:
            if self._ipmi_core:
                self._ipmi_core.force_reconnect(target_server_id=target_node_id)
            if self._ssh_probe_mgr and not target_node_id:
                threading.Thread(target=self._ssh_probe_mgr.force_reconnect_all, daemon=True).start()
            return {"success": True, "message": "已中断当前连接并重新发起连接探测"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def reconnect_system_server(self, sys_id):
        """中断并重新连接单个系统服务器"""
        try:
            if self._ssh_probe_mgr:
                threading.Thread(target=self._ssh_probe_mgr.force_reconnect_single, args=(sys_id,), daemon=True).start()
                return {"success": True, "message": "已重新发起 SSH 连接握手"}
            return {"success": False, "message": "探针未就绪"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def refresh_sensors_now(self):
        try:
            return self.force_reconnect()
        except Exception as e:
            return {"success": False, "error": str(e)}

    def test_connection(self, srv_id=None, server_data=None):
        try:
            target_srv = server_data
            if not target_srv and srv_id:
                for s in self._config_mgr.get_servers():
                    if s.get("id") == srv_id:
                        target_srv = s
                        break
            res = self._ipmi_core.test_connection(server_override=target_srv)
            return res
        except Exception as e:
            return {"success": False, "message": f"测试执行异常: {str(e)}", "latency_ms": 0, "details": [str(e)]}

    def switch_server(self, srv_id):
        try:
            self._config_mgr.set_active_server_id(srv_id)
            if self._ipmi_core:
                self._ipmi_core.sync_active_server_data(srv_id)
                # 如果该目标节点尚未抓取过全量传感器列表，在后台静默发起一次全量同步
                tel = self._ipmi_core.cluster_telemetry.get(srv_id, {})
                if not tel.get("all_sensors") or len(tel["all_sensors"]) == 0:
                    srv = next((s for s in self._config_mgr.get_servers() if s.get("id") == srv_id), None)
                    if srv and not self._ipmi_core.demo_mode:
                        threading.Thread(target=self._ipmi_core._poll_single_node, args=(srv, True), daemon=True).start()
            data = self._ipmi_core.get_status_snapshot()
            return {"success": True, "active_server_id": srv_id, "data": data}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def probe_hardware_fru(self, ip, user, password, timeout=8):
        try:
            res = self._ipmi_core.probe_hardware_fru(ip, user, password, timeout=timeout)
            return res
        except Exception as e:
            return {"success": False, "error": str(e), "brand": "dell", "brand_name": "Dell (戴尔)", "model": "", "serial": ""}

    def probe_system_os(self, host, port, user, password):
        """主动探测目标 Linux/PVE/SSH 宿主机的操作系统版本"""
        try:
            if self._ipmi_core.demo_mode:
                h_str = str(host).strip()
                if "100" in h_str:
                    return {"success": True, "os_name": "Proxmox VE 8.1", "message": "已识别: Proxmox VE 8.1 (虚拟化平台)"}
                elif "120" in h_str:
                    return {"success": True, "os_name": "Debian 12 Bookworm", "message": "已识别: Debian 12 Bookworm"}
                else:
                    return {"success": True, "os_name": "Ubuntu 22.04 LTS", "message": "已识别: Ubuntu 22.04.4 LTS"}

            import paramiko
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=host.strip(),
                port=int(port or 22),
                username=user.strip(),
                password=password or None,
                timeout=6.0,
                banner_timeout=12.0,
                auth_timeout=8.0,
                look_for_keys=False,
                allow_agent=False
            )
            cmd = "cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | head -1 | cut -d= -f2 | tr -d '\"' || cat /etc/redhat-release 2>/dev/null || uname -s"
            stdin, stdout, stderr = client.exec_command(cmd, timeout=3.0)
            res = stdout.read().decode("utf-8", errors="replace").strip()
            client.close()

            clean_os = res
            if "Proxmox" in res:
                clean_os = "Proxmox VE 8" if "8." in res else ("Proxmox VE 7" if "7." in res else "Proxmox VE")
            elif "Ubuntu" in res:
                if "24.04" in res: clean_os = "Ubuntu 24.04 LTS"
                elif "22.04" in res: clean_os = "Ubuntu 22.04 LTS"
                elif "20.04" in res: clean_os = "Ubuntu 20.04 LTS"
                else: clean_os = "Ubuntu Linux"
            elif "Debian" in res:
                clean_os = "Debian 12" if "12" in res else "Debian Linux"
            elif "CentOS" in res:
                clean_os = "CentOS 7" if "7" in res else "CentOS Linux"

            return {"success": True, "os_name": clean_os or "Linux", "message": f"成功识别系统: {clean_os or res}"}
        except Exception as e:
            return {"success": False, "error": str(e), "message": f"探测失败: {e}"}

    def cluster_ping_all(self):
        """集群快速在线测活：并发轻量测活硬件节点 (UDP 623) 与系统服务器 (SSH 22)"""
        try:
            node_res = self._ipmi_core.ping_all_nodes_fast()
            if self._ssh_probe_mgr:
                self._ssh_probe_mgr.poll_all_now()
            return {"success": True, "node_results": node_res}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def cluster_force_refresh_all(self):
        """集群强制刷新：强制深度重新获取所有节点的品牌、型号、SN以及操作系统的最新版本，覆盖旧配置"""
        try:
            self._ipmi_core.force_refresh_all_assets()
            if self._ssh_probe_mgr:
                # 重新探测所有系统服务器的 OS 信息
                for srv in self._config_mgr.get_system_servers():
                    s_id = srv.get("id")
                    h = srv.get("host")
                    p = srv.get("port", 22)
                    u = srv.get("username", "root")
                    pwd = srv.get("password", "")
                    os_res = self.probe_system_os(h, p, u, pwd)
                    if os_res.get("success") and os_res.get("os_name"):
                        self._config_mgr.update_system_server(s_id, {"os_name": os_res["os_name"]})
                self._ssh_probe_mgr.poll_all_now()
            return {"success": True, "message": "全集群品牌、型号、出厂SN与系统版本已强制刷新并覆盖"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def add_server(self, name, ip, user, password, model="Dell PowerEdge", brand="dell", serial="", timeout=30, retry=2, max_fan_rpm=0):
        try:
            new_srv = self._config_mgr.add_server(name, ip, user, password, model, brand=brand, serial=serial, timeout=timeout, retry=retry, max_fan_rpm=max_fan_rpm)
            # 自动设为当前激活受控节点，并立即异步拉取其实时遥测数据
            self._config_mgr.set_active_server_id(new_srv["id"])
            if self._ipmi_core.demo_mode:
                self._ipmi_core.fetch_sensors()
            else:
                threading.Thread(target=self._ipmi_core.fetch_sensors, daemon=True).start()
            return {"success": True, "message": f"成功添加服务器: {new_srv['name']}", "server": new_srv}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_server(self, srv_id):
        try:
            success, msg = self._config_mgr.delete_server(srv_id)
            return {"success": success, "message": msg}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def update_server(self, srv_id, updates):
        try:
            success = self._config_mgr.update_server(srv_id, updates)
            if success:
                # 若更新成功且更新了风扇上限或连接参数，立即异步刷新该服务器遥测
                target_srv = next((s for s in self._config_mgr.get_servers() if s.get("id") == srv_id), None)
                if target_srv:
                    if self._ipmi_core.demo_mode:
                        self._ipmi_core.fetch_sensors()
                    else:
                        threading.Thread(target=self._ipmi_core._poll_single_node, args=(target_srv, True), daemon=True).start()
                return {"success": True, "message": "服务器参数更新成功"}
            return {"success": False, "message": "未找到对应服务器"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_dashboard_view_mode(self, mode):
        try:
            if mode in ("probe", "detail"):
                self._config_mgr.set("dashboard_view_mode", mode)
                self._config_mgr.save()
                return {"success": True, "mode": mode}
            return {"success": False, "message": "不支持的视图模式"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def save_config(self, new_config):
        try:
            if not isinstance(new_config, dict):
                return {"success": False, "message": "无效的配置参数"}

            for k, v in new_config.items():
                if k.startswith("log_"):
                    self._config_mgr.set_log_param(k, v)
                elif k.startswith("alert_"):
                    self._config_mgr.set_alert_param(k, v)
                else:
                    self._config_mgr.set(k, v)
            self._config_mgr.save()
            return {"success": True, "message": "系统设置保存成功"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_curve_nodes(self, nodes):
        try:
            success = self._config_mgr.set_curve_nodes(nodes)
            if success:
                return {"success": True, "message": "温控曲线多节点配置已保存"}
            return {"success": False, "message": "节点数据格式无效 (至少需2个节点)"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_autostart(self, enable: bool):
        try:
            success, msg = update_system_autostart(enable)
            if success:
                self._config_mgr.set("autostart", "1" if enable else "0")
                self._config_mgr.save()
            return {"success": success, "message": msg, "autostart": enable}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_fan_mode(self, mode, srv_id=None):
        try:
            target_srv = None
            if srv_id:
                for s in self._config_mgr.get_servers():
                    if s.get("id") == srv_id:
                        target_srv = s
                        break
            success, msg = self._ipmi_core.set_fan_mode(mode, server_override=target_srv)
            return {"success": success, "message": msg, "data": self._ipmi_core.get_status_snapshot()}
        except Exception as e:
            return {"success": False, "message": str(e), "error": str(e)}

    def set_all_fans_speed(self, speed, srv_id=None):
        try:
            target_srv = None
            if srv_id:
                for s in self._config_mgr.get_servers():
                    if s.get("id") == srv_id:
                        target_srv = s
                        break
            success, msg = self._ipmi_core.set_all_fans_speed(speed, server_override=target_srv)
            return {"success": success, "message": msg, "data": self._ipmi_core.get_status_snapshot()}
        except Exception as e:
            return {"success": False, "message": str(e), "error": str(e)}

    def save_manual_speed_strategy(self, mode_type="global", global_speed=25, channel_speeds=None):
        """保存手动转速策略（支持全局调速或独立调速），并立即将新策略下发同步给所有使用手动温控的服务器"""
        try:
            applied_count = 0
            servers = self._config_mgr.get_servers()

            if mode_type == "global":
                sp = max(5, min(100, int(global_speed)))
                self._config_mgr.set("manual_speed", str(sp))
                self._config_mgr.set("manual_mode_type", "global")
                for i in range(1, 7):
                    self._config_mgr.set(f"fan{i}_speed", str(sp))

                # 无论节点当前处于什么模式，都将全局保存的 manual_speed 赋予所有物理硬件节点，
                # 这样用户一旦从首页将任何节点切换到手动温控时，直接继承并下发最新设定的转速（如 75%）！
                for s in servers:
                    s["manual_speed"] = sp
                    if s.get("mode") == "manual":
                        self._ipmi_core.set_all_fans_speed(sp, server_override=s)
                        applied_count += 1
                self._config_mgr.set_servers(servers)
                self._config_mgr.save()
                return {
                    "success": True,
                    "mode_type": "global",
                    "speed": sp,
                    "applied_count": applied_count,
                    "message": f"全局手动调速策略已保存（{sp}%），已同步至 {applied_count} 台手动模式服务器"
                }
            else:
                # 独立调速模式 (channel_speeds: list of 6 numbers)
                self._config_mgr.set("manual_mode_type", "individual")
                speeds = []
                if isinstance(channel_speeds, list) and len(channel_speeds) >= 6:
                    for i in range(6):
                        val = max(5, min(100, int(channel_speeds[i])))
                        speeds.append(val)
                        self._config_mgr.set(f"fan{i+1}_speed", str(val))
                else:
                    for i in range(1, 7):
                        speeds.append(self._config_mgr.get_int(f"fan{i}_speed", 25))
                self._config_mgr.save()

                for s in servers:
                    if s.get("mode") == "manual":
                        for idx, val in enumerate(speeds):
                            self._ipmi_core.set_single_fan_speed(idx, val, server_override=s)
                        applied_count += 1
                return {
                    "success": True,
                    "mode_type": "individual",
                    "channel_speeds": speeds,
                    "applied_count": applied_count,
                    "message": f"6通道独立调速策略已保存并应用，已同步至 {applied_count} 台手动模式服务器"
                }
        except Exception as e:
            return {"success": False, "error": str(e), "message": f"保存策略失败: {str(e)}"}

    def update_manual_speed_strategy(self, speed):
        """Update global manual strategy speed and auto-apply to all servers currently in 'manual' mode."""
        return self.save_manual_speed_strategy("global", global_speed=speed)

    def update_single_fan_strategy(self, fan_index, speed):
        """Update a single fan channel and auto-apply to all servers currently in 'manual' mode."""
        try:
            sp = max(5, min(100, int(speed)))
            idx = int(fan_index)
            servers = self._config_mgr.get_servers()
            applied_count = 0
            for s in servers:
                if s.get("mode") == "manual":
                    self._ipmi_core.set_single_fan_speed(idx, sp, server_override=s)
                    applied_count += 1
            return {
                "success": True,
                "fan_index": idx,
                "speed": sp,
                "applied_count": applied_count,
                "message": f"通道 #{idx + 1} 已设为 {sp}%，已自动同步至 {applied_count} 台使用手动模式的服务器"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def update_preset_strategy(self, preset_key):
        """Update active preset and auto-apply to all servers, persisting preset_key across cluster."""
        try:
            servers = self._config_mgr.get_servers()
            applied_count = 0
            self._config_mgr.set("preset_key", preset_key)
            # 全量服务器无差别打上 preset_key 策略印记，确保首页任何节点一切入「方案」模式，立即采用最新选用的预设！
            for s in servers:
                s["preset_key"] = preset_key
                if s.get("mode") == "preset":
                    self._ipmi_core.apply_preset(preset_key, server_override=s)
                    applied_count += 1
            self._config_mgr.set_servers(servers)
            self._config_mgr.save()
            return {
                "success": True,
                "preset_key": preset_key,
                "applied_count": applied_count,
                "message": f"情景方案已选用并保存，已同步至 {applied_count} 台使用方案模式的服务器"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_single_fan_speed(self, fan_index, speed, srv_id=None):
        try:
            target_srv = None
            if srv_id:
                for s in self._config_mgr.get_servers():
                    if s.get("id") == srv_id:
                        target_srv = s
                        break
            success, msg = self._ipmi_core.set_single_fan_speed(fan_index, speed, server_override=target_srv)
            return {"success": success, "message": msg}
        except Exception as e:
            return {"success": False, "message": str(e), "error": str(e)}

    def apply_preset(self, preset_key, srv_id=None):
        try:
            target_srv = None
            if srv_id:
                for s in self._config_mgr.get_servers():
                    if s.get("id") == srv_id:
                        target_srv = s
                        break
            success, msg = self._ipmi_core.apply_preset(preset_key, server_override=target_srv)
            return {"success": success, "message": msg, "data": self._ipmi_core.get_status_snapshot()}
        except Exception as e:
            return {"success": False, "message": str(e), "error": str(e)}

    def set_probe_layout_style(self, style):
        try:
            self._config_mgr.set_probe_layout_style(style)
            return {"success": True, "probe_layout_style": style}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def toggle_demo_mode(self, enabled):
        try:
            self._ipmi_core.set_demo_mode(enabled)
            if not enabled and self._ssh_probe_mgr:
                # 关闭仿真时：立即并发执行真实主机测活与状态探针，刷新为真实物理状态
                self._ssh_probe_mgr.poll_all_now()
            # 返回全量状态（包含硬件节点与系统服务器最新的真实或演示遥测，刷新全局）
            full_status = self.get_status()
            return {"success": True, "demo_mode": self._ipmi_core.demo_mode, "data": full_status.get("data", {})}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def open_idrac_web(self, srv_id=None):
        try:
            srv = self._config_mgr.get_active_server()
            if srv_id:
                for s in self._config_mgr.get_servers():
                    if s.get("id") == srv_id:
                        srv = s
                        break
            ip = srv.get("ip", "192.168.1.1")
            url = f"https://{ip}"
            webbrowser.open(url)
            return {"success": True, "url": url}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def control_server_power(self, srv_id, action):
        """对指定硬件节点执行 IPMI 电源控制 (chassis power on/off/soft/reset/cycle)"""
        try:
            srv = next((s for s in self._config_mgr.get_servers() if s.get("id") == srv_id), None)
            if not srv:
                return {"success": False, "error": f"未找到 ID 为 [{srv_id}] 的硬件节点"}
            return self._ipmi_core.control_chassis_power(action, server_override=srv)
        except Exception as e:
            logger.error(f"control_server_power error: {e}")
            return {"success": False, "error": f"电源控制执行异常: {str(e)}"}

    def control_power(self, action, srv_id):
        """兼容 Web 与前端各版本调用的电源控制别名方法: control_power(action, srv_id)"""
        return self.control_server_power(srv_id, action)

    def control_pcie_fan_response(self, action="status", srv_id=None):
        """控制戴尔 13G/14G/15G (R730, R740, R750 等) 第三方 PCIe 卡强制散热狂转 (status/disable/enable)"""
        try:
            srv = next((s for s in self._config_mgr.get_servers() if s.get("id") == srv_id), None) if srv_id else None
            return self._ipmi_core.control_dell_pcie_fan_response(action, server_override=srv)
        except Exception as e:
            logger.error(f"control_pcie_fan_response error: {e}")
            return {"success": False, "error": f"PCIe 散热控制异常: {str(e)}"}

    def auto_calibrate_fan_max_rpm(self, srv_id=None):
        """一键安全测试并校准指定服务器的物理风扇最高转速"""
        try:
            srv = next((s for s in self._config_mgr.get_servers() if s.get("id") == srv_id), None) if srv_id else None
            success, res = self._ipmi_core.auto_calibrate_fan_max_rpm(server_override=srv)
            return {"success": success, **res}
        except Exception as e:
            logger.error(f"auto_calibrate_fan_max_rpm error: {e}")
            return {"success": False, "error": f"校准接口异常: {str(e)}"}

    def minimize_window(self):
        if self._window:
            try:
                self._window.minimize()
                return {"success": True}
            except Exception as e:
                logger.error(f"minimize_window error: {e}")
                return {"success": False, "error": str(e)}
        return {"success": False}

    def toggle_maximize(self):
        if self._window:
            try:
                if self._is_maximized:
                    self._window.restore()
                    self._is_maximized = False
                else:
                    self._window.maximize()
                    self._is_maximized = True
                return {"success": True, "maximized": self._is_maximized}
            except Exception as e:
                logger.error(f"toggle_maximize error: {e}")
                return {"success": False, "error": str(e)}
        return {"success": False}

    def close_window(self):
        if self._window:
            try:
                if self._ipmi_core:
                    self._ipmi_core.stop_monitoring()
                if self._ssh_probe_mgr:
                    self._ssh_probe_mgr.stop_monitoring()
                self._window.destroy()
                return {"success": True}
            except Exception as e:
                logger.error(f"close_window error: {e}")
                return {"success": False, "error": str(e)}
        return {"success": False}

    def set_window_bounds(self, x=None, y=None, width=None, height=None):
        """Cross-platform window sizing: uses SetWindowPos on Windows, native resize/move on macOS/Linux."""
        if not self._window:
            return False

        if sys.platform == "win32":
            try:
                import ctypes
                user32 = ctypes.windll.user32
                if hasattr(self._window, "native") and self._window.native:
                    hwnd = int(self._window.native.Handle.ToInt64())
                    flags = 0x0004 | 0x0010  # SWP_NOZORDER | SWP_NOACTIVATE
                    nx, ny = 0, 0
                    if x is None or y is None:
                        flags |= 0x0002  # SWP_NOMOVE
                    else:
                        nx, ny = int(x), int(y)

                    if width is None or height is None:
                        flags |= 0x0001  # SWP_NOSIZE
                        nw, nh = 0, 0
                    else:
                        nw = max(960, int(width))
                        nh = max(640, int(height))

                    user32.SetWindowPos(hwnd, 0, nx, ny, nw, nh, flags)
                    return True
            except Exception as e:
                logger.error(f"set_window_bounds error: {e}")
        else:
            try:
                if width is not None and height is not None:
                    self._window.resize(int(width), int(height))
                if x is not None and y is not None:
                    self._window.move(int(x), int(y))
                return True
            except Exception as e:
                logger.error(f"macOS window resize error: {e}")
        return False

    def get_window_bounds(self):
        if not self._window:
            return None
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                if hasattr(self._window, "native") and self._window.native:
                    hwnd = int(self._window.native.Handle.ToInt64())
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    return {
                        "x": rect.left,
                        "y": rect.top,
                        "width": rect.right - rect.left,
                        "height": rect.bottom - rect.top
                    }
            except Exception as e:
                logger.error(f"get_window_bounds error: {e}")
        return {
            "x": getattr(self._window, "x", 0),
            "y": getattr(self._window, "y", 0),
            "width": getattr(self._window, "width", 1100),
            "height": getattr(self._window, "height", 740)
        }

    def resize_window(self, width, height):
        if self._window:
            try:
                w = max(800, int(width))
                h = max(550, int(height))
                self._window.resize(w, h)
                return {"success": True, "width": w, "height": h}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return {"success": False}

    def start_drag(self):
        if not self._window:
            return False
        if sys.platform == "win32":
            try:
                import ctypes
                user32 = ctypes.windll.user32
                if hasattr(self._window, "native") and self._window.native:
                    hwnd = int(self._window.native.Handle.ToInt64())
                    user32.ReleaseCapture()
                    user32.PostMessageW(hwnd, 0x00A1, 2, 0) # 2 = HTCAPTION
                    return True
            except Exception as e:
                logger.error(f"start_drag error: {e}")
        return False

    # ==========================================
    # System Server (SSH) Decoupled API Endpoints
    # ==========================================
    def set_subsystem_poll_sec(self, seconds):
        try:
            sec = max(1, min(60, int(seconds)))
            self._config_mgr.set("subsystem_poll_sec", str(sec))
            self._config_mgr.save()
            return {"success": True, "subsystem_poll_sec": sec, "message": f"服务器采样周期已更新为 {sec} 秒"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def add_system_server(self, name, host, port, username, password, node_id="", os_name=""):
        try:
            srv = self._config_mgr.add_system_server(name, host, port, username, password, node_id=node_id, os_name=os_name)
            if self._ssh_probe_mgr:
                self._ssh_probe_mgr.sync_subsystems_from_config()
            return {"success": True, "data": srv, "message": f"系统服务器「{name}」已添加"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_system_server(self, srv_id):
        try:
            ok = self._config_mgr.delete_system_server(srv_id)
            if self._ssh_probe_mgr:
                self._ssh_probe_mgr.sync_subsystems_from_config()
            return {"success": ok, "message": "系统服务器已删除"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def update_system_server(self, srv_id, updates):
        try:
            ok = self._config_mgr.update_system_server(srv_id, updates)
            if self._ssh_probe_mgr:
                self._ssh_probe_mgr.sync_subsystems_from_config()
            return {"success": ok, "message": "系统服务器配置已更新"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def test_system_server_ssh(self, host, port, username, password):
        try:
            info = {
                "id": "temp_test",
                "name": "Test Node",
                "host": host,
                "port": port,
                "username": username,
                "password": password,
                "enabled": True
            }
            if self._ssh_probe_mgr:
                res = self._ssh_probe_mgr.test_single_server(info)
                if res.get("connected"):
                    return {
                        "success": True,
                        "data": res,
                        "message": f"SSH 连通成功！目标主机: {res.get('hostname') or host}，CPU: {res.get('cpu_cores')}核，内存: {res.get('mem_total_gb')}GB"
                    }
                else:
                    return {
                        "success": False,
                        "error": res.get("last_error") or "连接超时或认证失败",
                        "message": f"连接失败: {res.get('last_error')}"
                    }
            return {"success": False, "message": "探针管理器未初始化"}
        except Exception as e:
            return {"success": False, "error": str(e), "message": f"测试异常: {e}"}

    # ==========================================
    # Alert Engine API Endpoints
    # ==========================================
    def get_alert_config(self):
        try:
            if self._alert_engine:
                return {"success": True, "data": self._alert_engine.get_config()}
            return {"success": False, "error": "告警引擎未就绪"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def save_alert_config(self, cfg):
        try:
            if self._alert_engine and isinstance(cfg, dict):
                ok = self._alert_engine.set_config(cfg)
                return {"success": ok, "message": "告警配置已保存"}
            return {"success": False, "message": "无效的配置参数"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_alert_history(self):
        try:
            if self._alert_engine:
                return {"success": True, "data": self._alert_engine.get_history()}
            return {"success": True, "data": []}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def clear_alert_history(self):
        try:
            if self._alert_engine:
                self._alert_engine.clear_history()
            return {"success": True, "message": "告警记录已清空"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def test_alert_sound(self, sound_type="default", custom_path=""):
        try:
            if self._alert_engine:
                threading.Thread(target=self._alert_engine.play_alert_sound, args=(sound_type, custom_path), daemon=True).start()
                return {"success": True, "message": "正在试听告警提示音"}
            return {"success": False, "message": "告警引擎未就绪"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def test_alert_tts(self, text="这是一条测试语音播报"):
        try:
            if self._alert_engine:
                threading.Thread(target=self._alert_engine.speak_text, args=(text,), daemon=True).start()
                return {"success": True, "message": "正在朗读告警语音"}
            return {"success": False, "message": "告警引擎未就绪"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def test_alert_webhook(self, cfg_override=None):
        try:
            if self._alert_engine:
                cfg = cfg_override if isinstance(cfg_override, dict) else self._alert_engine.get_config()
                res = self._alert_engine.send_external_notification(
                    cfg,
                    "【测试】云枢 (YunShu) 告警通知测试",
                    "这是一条来自 云枢 智能告警中心的测试推送消息。\n来源模块: 外部通知中枢 (移植自 Apprise & ANotify)\n状态: 正常送达"
                )
                return res
            return {"success": False, "message": "告警引擎未就绪"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def trigger_test_alert(self):
        try:
            if self._alert_engine:
                return self._alert_engine.test_alert()
            return {"success": False, "message": "告警引擎未就绪"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def execute_batch_ssh(self, server_ids, command):
        """运维平台：向所选 Linux 服务器批量分发 SSH 运维命令并返回执行结果"""
        try:
            if not command or not command.strip():
                return {"success": False, "message": "请输入待执行的命令"}
            if not server_ids or not isinstance(server_ids, list):
                return {"success": False, "message": "请选择至少一台目标服务器"}

            if self._ipmi_core.demo_mode:
                # 仿真模式下模拟真实执行结果
                results = []
                for sid in server_ids:
                    srv = next((s for s in self._config_mgr.get_system_servers() if s.get("id") == sid), None)
                    sname = srv.get("name") if srv else sid
                    host = srv.get("host") if srv else "192.168.1.100"
                    cmd_clean = command.strip()
                    fake_out = f"[{sname} ~]$ {cmd_clean}\n"
                    if "uptime" in cmd_clean:
                        fake_out += " 17:35:12 up 24 days,  5:12,  2 users,  load average: 0.28, 0.42, 0.35\n"
                    elif "uname" in cmd_clean:
                        fake_out += "Linux pve-node 6.5.11-8-pve #1 SMP PREEMPT_DYNAMIC PVE (x86_64)\n"
                    elif "df" in cmd_clean:
                        fake_out += "Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1       512G  140G  372G  28% /\n"
                    elif "free" in cmd_clean:
                        fake_out += "               total        used        free      shared  buff/cache   available\nMem:        32768000    11840000    14200000      250000     6728000    20928000\n"
                    else:
                        fake_out += f"执行成功完成: (仿真返回 exit 0)\n"
                    results.append({
                        "server_id": sid,
                        "server_name": sname,
                        "host": host,
                        "success": True,
                        "stdout": fake_out,
                        "stderr": "",
                        "exit_code": 0,
                        "error": ""
                    })
                return {"success": True, "results": results}

            if not self._ssh_probe_mgr:
                return {"success": False, "message": "SSH 探针服务未启动"}

            results = self._ssh_probe_mgr.execute_batch_commands(server_ids, command.strip())
            return {"success": True, "results": results}
        except Exception as e:
            logger.error(f"Error in execute_batch_ssh: {e}")
            return {"success": False, "message": f"批量执行异常: {str(e)}"}

    # ==========================================
    # Real-time System Logs & Debug API
    # ==========================================
    def get_system_logs(self, limit=300):
        try:
            if self._log_mgr:
                return {
                    "success": True,
                    "debug_mode": self._log_mgr.get_debug_mode(),
                    "retention_days": self._config_mgr.get_log_param("log_retention_days", 7) if self._config_mgr else 7,
                    "logs": self._log_mgr.get_logs(limit=limit)
                }
            return {"success": True, "debug_mode": False, "retention_days": 7, "logs": []}
        except Exception as e:
            return {"success": False, "error": str(e), "logs": []}

    def set_log_debug_mode(self, enabled):
        try:
            is_enabled = bool(enabled)
            if isinstance(enabled, str):
                is_enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
            if self._log_mgr:
                self._log_mgr.set_debug_mode(is_enabled)
            if self._config_mgr:
                self._config_mgr.set_log_param("log_debug_mode", is_enabled)
                if self._config_mgr.config.has_section("ipmi") and self._config_mgr.config.has_option("ipmi", "log_debug_mode"):
                    self._config_mgr.config.remove_option("ipmi", "log_debug_mode")
                    self._config_mgr.save()
            return {"success": True, "debug_mode": is_enabled, "message": f"已{'开启全量调试日志' if is_enabled else '切换为仅显示错误告警'}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_log_retention_days(self, days):
        try:
            d = max(1, min(365, int(days)))
            if self._config_mgr:
                self._config_mgr.set_log_param("log_retention_days", d)
            if self._log_mgr:
                self._log_mgr.cleanup_expired_logs()
            return {"success": True, "retention_days": d, "message": f"日志保存期限已设置为 {d} 天"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def clear_system_logs(self):
        try:
            if self._log_mgr:
                self._log_mgr.clear_memory_logs()
            return {"success": True, "message": "实时日志视图已清空"}
        except Exception as e:
            return {"success": False, "error": str(e)}
