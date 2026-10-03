"""
Dell Fan Sense - Linux System Server Real-time SSH Probe
Long-lived connection stream for zero-install, minimal overhead telemetry.
Decoupled: Servers can be bound to hardware nodes, or exist independently.
"""

import time
import json
import logging
import threading
import socket
from typing import Dict, Any, Optional, List, Tuple

try:
    import paramiko
except ImportError:
    paramiko = None

logger = logging.getLogger("ssh_probe")

def _enable_legacy_ssh_algorithms():
    """
    Paramiko 3.0+ 默认禁用了 ssh-rsa 与部分旧版 Diffie-Hellman 密钥交换算法。
    在支持 Dropbear SSH（如路由器/OpenWrt/老旧设备）时：
    不仅要在 Transport._preferred_keys 中协商允许 'ssh-rsa'，
    还必须在 Transport._key_info 映射表中注册 'ssh-rsa' 的反序列化解析类（RSAKey），
    否则密钥协商完成后，在 _verify_key 中根据 host_key_type 解析公钥时会触发 KeyError: 'ssh-rsa'！
    """
    if not paramiko:
        return
    try:
        from paramiko.transport import Transport
        from paramiko.rsakey import RSAKey

        # 1. 注册公钥解析类型映射表，杜绝 KeyError: 'ssh-rsa'
        Transport._key_info['ssh-rsa'] = RSAKey
        Transport._key_info['ssh-rsa-cert-v01@openssh.com'] = RSAKey

        # 2. 拓展客户端偏好协商的 host key 算法
        keys = list(Transport._preferred_keys)
        for k in ('ssh-rsa',):
            if k not in keys:
                keys.append(k)
        Transport._preferred_keys = tuple(keys)

        # 3. 拓展老旧服务器常用的 Diffie-Hellman 密钥交换算法
        kex = list(Transport._preferred_kex)
        for x in ('diffie-hellman-group14-sha1', 'diffie-hellman-group1-sha1', 'diffie-hellman-group-exchange-sha1'):
            if x not in kex:
                kex.append(x)
        Transport._preferred_kex = tuple(kex)

        # 4. 彻底静音 paramiko 内部的底层调试与握手底噪
        logging.getLogger("paramiko").setLevel(logging.WARNING)
        logging.getLogger("paramiko.transport").setLevel(logging.WARNING)
    except Exception as e:
        logger.debug(f"enable_legacy_ssh_algorithms error: {e}")

_enable_legacy_ssh_algorithms()

LINUX_PROBE_SCRIPT = r"""
if command -v python3 >/dev/null 2>&1; then
  PYCMD="python3"
elif command -v python >/dev/null 2>&1; then
  PYCMD="python"
else
  PYCMD=""
fi

if [ -n "$PYCMD" ]; then
  $PYCMD -c "
import sys, os, time, json, socket

def get_stats():
    try:
        with open('/proc/stat', 'r') as f:
            lines = f.readlines()
        cpu_parts = [float(x) for x in lines[0].split()[1:]]
        idle = cpu_parts[3] + (cpu_parts[4] if len(cpu_parts) > 4 else 0)
        total = sum(cpu_parts)
    except:
        idle, total = 0, 1

    time.sleep(0.3)

    try:
        with open('/proc/stat', 'r') as f:
            lines = f.readlines()
        cpu_parts2 = [float(x) for x in lines[0].split()[1:]]
        idle2 = cpu_parts2[3] + (cpu_parts2[4] if len(cpu_parts2) > 4 else 0)
        total2 = sum(cpu_parts2)
        diff_total = total2 - total
        diff_idle = idle2 - idle
        cpu_pct = round(100.0 * (diff_total - diff_idle) / max(1, diff_total), 1)
    except:
        cpu_pct = 0.0

    try:
        cores = os.cpu_count() or 1
    except:
        try:
            import multiprocessing
            cores = multiprocessing.cpu_count()
        except:
            cores = 1

    try:
        with open('/proc/loadavg', 'r') as f:
            lavg = f.read().split()[:3]
        load_1m, load_5m, load_15m = float(lavg[0]), float(lavg[1]), float(lavg[2])
    except:
        load_1m, load_5m, load_15m = 0.0, 0.0, 0.0

    mem_total_kb, mem_avail_kb = 0, 0
    swap_total_kb, swap_free_kb = 0, 0
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                parts = line.split()
                if parts[0] == 'MemTotal:': mem_total_kb = int(parts[1])
                elif parts[0] == 'MemAvailable:': mem_avail_kb = int(parts[1])
                elif parts[0] == 'SwapTotal:': swap_total_kb = int(parts[1])
                elif parts[0] == 'SwapFree:': swap_free_kb = int(parts[1])
    except:
        pass

    if mem_avail_kb == 0 and mem_total_kb > 0:
        try:
            free, buf, cache = 0, 0, 0
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    p = line.split()
                    if p[0] == 'MemFree:': free = int(p[1])
                    elif p[0] == 'Buffers:': buf = int(p[1])
                    elif p[0] == 'Cached:': cache = int(p[1])
            mem_avail_kb = free + buf + cache
        except:
            pass

    mem_used_kb = max(0, mem_total_kb - mem_avail_kb)
    mem_pct = round(100.0 * mem_used_kb / max(1, mem_total_kb), 1) if mem_total_kb > 0 else 0.0
    mem_total_gb = round(mem_total_kb / 1048576.0, 2)
    mem_used_gb = round(mem_used_kb / 1048576.0, 2)

    swap_used_kb = max(0, swap_total_kb - swap_free_kb)
    swap_pct = round(100.0 * swap_used_kb / max(1, swap_total_kb), 1) if swap_total_kb > 0 else 0.0
    swap_total_gb = round(swap_total_kb / 1048576.0, 2)
    swap_used_gb = round(swap_used_kb / 1048576.0, 2)

    try:
        st = os.statvfs('/')
        disk_total_b = st.f_blocks * st.f_frsize
        disk_free_b = st.f_bavail * st.f_frsize
        disk_used_b = disk_total_b - disk_free_b
        disk_pct = round(100.0 * disk_used_b / max(1, disk_total_b), 1) if disk_total_b > 0 else 0.0
        disk_total_gb = round(disk_total_b / (1024**3), 1)
        disk_used_gb = round(disk_used_b / (1024**3), 1)
    except:
        disk_pct, disk_total_gb, disk_used_gb = 0.0, 0.0, 0.0

    try:
        hostname = socket.gethostname()
    except:
        hostname = 'linux-node'

    uptime_sec = 0
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_sec = int(float(f.read().split()[0]))
    except:
        pass

    return {
        'connected': True,
        'hostname': hostname,
        'cpu_pct': max(0.0, min(100.0, cpu_pct)),
        'cpu_cores': cores,
        'load_1m': load_1m,
        'load_5m': load_5m,
        'load_15m': load_15m,
        'mem_pct': mem_pct,
        'mem_used_gb': mem_used_gb,
        'mem_total_gb': mem_total_gb,
        'swap_pct': swap_pct,
        'swap_used_gb': swap_used_gb,
        'swap_total_gb': swap_total_gb,
        'disk_pct': disk_pct,
        'disk_used_gb': disk_used_gb,
        'disk_total_gb': disk_total_gb,
        'uptime_sec': uptime_sec
    }

print(json.dumps(get_stats()))
" 2>/dev/null
else
  # 100% POSIX sh + awk pure fallback (works even on embedded/Alpine/minimal Linux with no Python)
  awk '
  BEGIN {
    total_mem = 0; free_mem = 0; avail_mem = 0; buffers = 0; cached = 0; swap_total = 0; swap_free = 0;
  }
  /^MemTotal:/ { total_mem = $2 }
  /^MemFree:/ { free_mem = $2 }
  /^MemAvailable:/ { avail_mem = $2 }
  /^Buffers:/ { buffers = $2 }
  /^Cached:/ { cached = $2 }
  /^SwapTotal:/ { swap_total = $2 }
  /^SwapFree:/ { swap_free = $2 }
  END {
    if (avail_mem == 0) { avail_mem = free_mem + buffers + cached; }
    used_mem = total_mem - avail_mem;
    if (used_mem < 0) used_mem = 0;
    mem_pct = (total_mem > 0) ? (used_mem * 100.0 / total_mem) : 0;
    mem_total_gb = total_mem / 1048576.0;
    mem_used_gb = used_mem / 1048576.0;

    swap_used = swap_total - swap_free;
    if (swap_used < 0) swap_used = 0;
    swap_pct = (swap_total > 0) ? (swap_used * 100.0 / swap_total) : 0;
    swap_total_gb = swap_total / 1048576.0;
    swap_used_gb = swap_used / 1048576.0;

    "hostname 2>/dev/null || cat /proc/sys/kernel/hostname 2>/dev/null || echo linux" | getline hname;
    "nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null || echo 1" | getline cores;
    "awk \"{print int(\\$1)}\" /proc/uptime 2>/dev/null || echo 0" | getline uptime;
    "awk \"{print \\$1, \\$2, \\$3}\" /proc/loadavg 2>/dev/null || echo \"0.0 0.0 0.0\"" | getline loads;
    split(loads, larr, " ");
    l1 = (larr[1] != "") ? larr[1] : 0.0;
    l5 = (larr[2] != "") ? larr[2] : 0.0;
    l15 = (larr[3] != "") ? larr[3] : 0.0;

    # If CPU % is not directly available via python, infer instantaneous CPU utilization from 1-min load average and core count
    c_cores = (cores > 0) ? cores : 1;
    calc_cpu = (l1 * 100.0) / c_cores;
    if (calc_cpu > 100.0) calc_cpu = 100.0;
    if (calc_cpu < 0.0) calc_cpu = 0.0;

    "df -k / 2>/dev/null | tail -1 | awk \"{print \\$2, \\$3, \\$5}\"" | getline df_out;
    split(df_out, df_arr, " ");
    d_total_gb = df_arr[1] / 1048576.0;
    d_used_gb = df_arr[2] / 1048576.0;
    gsub("%", "", df_arr[3]);
    d_pct = (df_arr[3] != "") ? df_arr[3] : 0;

    printf("{\"connected\":true,\"hostname\":\"%s\",\"cpu_pct\":%.1f,\"cpu_cores\":%d,\"load_1m\":%s,\"load_5m\":%s,\"load_15m\":%s,\"mem_pct\":%.1f,\"mem_used_gb\":%.2f,\"mem_total_gb\":%.2f,\"swap_pct\":%.1f,\"swap_used_gb\":%.2f,\"swap_total_gb\":%.2f,\"disk_pct\":%.1f,\"disk_used_gb\":%.1f,\"disk_total_gb\":%.1f,\"uptime_sec\":%d}\n",
           hname, calc_cpu, cores, l1, l5, l15, mem_pct, mem_used_gb, mem_total_gb, swap_pct, swap_used_gb, swap_total_gb, d_pct, d_used_gb, d_total_gb, uptime);
  }' /proc/meminfo
fi
"""


def ping_liveness(host: str, port: int = 22, timeout: float = 1.5) -> Tuple[bool, int]:
    """Fast non-blocking TCP socket connection check to probe server liveness with latency (ms)."""
    t0 = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        latency = int((time.time() - t0) * 1000)
        sock.close()
        return (result == 0, max(1, latency))
    except Exception:
        return (False, 0)


class SystemServerWorker:
    """Manages long-lived SSH connection and periodic probe polling for a single system server."""

    def __init__(self, srv_info: Dict[str, Any], manager=None):
        self.manager = manager
        self.srv_id = srv_info.get("id")
        self.name = srv_info.get("name", "Linux 服务器")
        self.host = srv_info.get("host", "127.0.0.1")
        self.port = int(srv_info.get("port", 22))
        self.username = srv_info.get("username", "") or "root"
        self.password = srv_info.get("password", "")
        self.node_id = srv_info.get("node_id", "")  # Optional bound hardware node
        self.os_name = srv_info.get("os_name", "")  # Persistent OS name or custom
        self.enabled = srv_info.get("enabled", True)

        self._client: Optional[paramiko.SSHClient] = None
        self._lock = threading.Lock()

        self.last_telemetry: Dict[str, Any] = {
            "id": self.srv_id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "node_id": self.node_id,
            "os_name": self.os_name,
            "connected": None,  # None: 正在获取/连接探测中; True: 在线; False: 最终离线
            "latency_ms": None,
            "last_error": "",
            "last_updated": "--:--:--",
            "cpu_pct": None,
            "cpu_cores": None,
            "load_1m": None,
            "load_5m": None,
            "load_15m": None,
            "mem_pct": None,
            "mem_used_gb": None,
            "mem_total_gb": None,
            "swap_pct": None,
            "swap_used_gb": None,
            "swap_total_gb": None,
            "disk_pct": None,
            "disk_used_gb": None,
            "disk_total_gb": None,
            "uptime_sec": 0,
            "hostname": ""
        }

    def update_info(self, srv_info: Dict[str, Any]):
        with self._lock:
            need_reconnect = (
                self.host != srv_info.get("host") or
                self.port != int(srv_info.get("port", 22)) or
                self.username != srv_info.get("username") or
                self.password != srv_info.get("password", "")
            )
            self.name = srv_info.get("name", self.name)
            self.host = srv_info.get("host", self.host)
            self.port = int(srv_info.get("port", 22))
            self.username = srv_info.get("username", self.username)
            self.password = srv_info.get("password", "")
            self.node_id = srv_info.get("node_id", "")
            self.os_name = srv_info.get("os_name", self.os_name)
            self.enabled = srv_info.get("enabled", True)

            self.last_telemetry["name"] = self.name
            self.last_telemetry["host"] = self.host
            self.last_telemetry["port"] = self.port
            self.last_telemetry["node_id"] = self.node_id
            self.last_telemetry["os_name"] = self.os_name

            if need_reconnect and self._client:
                self._disconnect()

    def _connect(self) -> bool:
        if not paramiko:
            self.last_telemetry["last_error"] = "缺少 paramiko 库支持"
            return False

        # 1. 快速 TCP 端口测活并测量延时，避免死等超时
        alive, lat_ms = ping_liveness(self.host, self.port, timeout=1.5)
        if not alive:
            self._disconnect()
            self.last_telemetry["connected"] = False
            self.last_telemetry["latency_ms"] = None
            self.last_telemetry["last_error"] = f"主机端口无法连通 ({self.host}:{self.port})"
            self.last_telemetry["last_updated"] = time.strftime("%H:%M:%S")
            return False

        self.last_telemetry["latency_ms"] = lat_ms

        if self._client:
            try:
                transport = self._client.get_transport()
                if transport and transport.is_active():
                    return True
            except Exception:
                pass
            self._disconnect()

        # 针对 Dropbear 或嵌入式 SSH 重试策略：banner 读取超时或瞬态 EOF 时重试一次
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    hostname=self.host,
                    port=self.port,
                    username=self.username,
                    password=self.password if self.password else None,
                    timeout=5.0,
                    banner_timeout=10.0,
                    auth_timeout=8.0,
                    allow_agent=False,
                    look_for_keys=False
                )
                self._client = client
                return True
            except Exception as e:
                self._disconnect()
                err_str = str(e)
                # 若为首次尝试且发生 banner 读取失败/协议中断，轻微间隔 0.3s 后重试一次
                if attempt < max_attempts and ("banner" in err_str.lower() or "eof" in err_str.lower()):
                    time.sleep(0.3)
                    continue
                self.last_telemetry["connected"] = False
                self.last_telemetry["last_error"] = "SSH 握手协议读取中断 (Banner EOF)" if "banner" in err_str.lower() else err_str
                self.last_telemetry["last_updated"] = time.strftime("%H:%M:%S")
                return False

    def _detect_os_if_needed(self):
        """首次获取时尝试获取一次服务器操作系统，成功后持久化保存，后续不再探测"""
        if self.os_name or not self._client:
            return
        try:
            cmd = "cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | head -1 | cut -d= -f2 | tr -d '\"' || cat /etc/redhat-release 2>/dev/null || cat /etc/issue 2>/dev/null | head -1 | tr -d '\\n' || uname -s"
            stdin, stdout, stderr = self._client.exec_command(cmd, timeout=2.5)
            res = stdout.read().decode("utf-8", errors="replace").strip()
            if res:
                # Format clean short name e.g. "Debian GNU/Linux 12 (bookworm)" -> "Debian 12"
                clean_os = res
                if "Proxmox" in res:
                    clean_os = "Proxmox VE"
                    if "8." in res: clean_os = "Proxmox VE 8"
                    elif "7." in res: clean_os = "Proxmox VE 7"
                elif "Ubuntu" in res:
                    if "24.04" in res: clean_os = "Ubuntu 24.04 LTS"
                    elif "22.04" in res: clean_os = "Ubuntu 22.04 LTS"
                    elif "20.04" in res: clean_os = "Ubuntu 20.04 LTS"
                    else: clean_os = "Ubuntu Linux"
                elif "Debian" in res:
                    if "12" in res: clean_os = "Debian 12"
                    elif "11" in res: clean_os = "Debian 11"
                    else: clean_os = "Debian Linux"
                elif "CentOS" in res:
                    if "7" in res: clean_os = "CentOS 7"
                    elif "8" in res: clean_os = "CentOS 8"
                    else: clean_os = "CentOS Linux"
                elif len(clean_os) > 24:
                    clean_os = clean_os[:24]

                self.os_name = clean_os
                self.last_telemetry["os_name"] = self.os_name
                if self.manager:
                    self.manager.save_detected_os(self.srv_id, self.os_name)
                logger.info(f"Detected OS for server {self.srv_id}: {self.os_name}")
        except Exception as e:
            logger.debug(f"OS detection skipped: {e}")

    def _disconnect(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def poll_once(self) -> Dict[str, Any]:
        if not self.enabled:
            return self.last_telemetry

        # If in demo mode simulation or unconnectable local fallback
        success = self._connect()
        if not success or not self._client:
            # Let caller handle or simulate if demo mode
            self.last_telemetry["connected"] = False
            return self.last_telemetry

        try:
            self._detect_os_if_needed()
            stdin, stdout, stderr = self._client.exec_command(LINUX_PROBE_SCRIPT.strip(), timeout=3.5)
            out_str = stdout.read().decode("utf-8", errors="replace").strip()
            if out_str:
                for line in reversed(out_str.split("\n")):
                    line = line.strip()
                    if line.startswith("{") and line.endswith("}"):
                        parsed = json.loads(line)
                        with self._lock:
                            self.last_telemetry.update(parsed)
                            self.last_telemetry["connected"] = True
                            self.last_telemetry["last_error"] = ""
                            self.last_telemetry["os_name"] = self.os_name
                            self.last_telemetry["last_updated"] = time.strftime("%H:%M:%S")
                        return self.last_telemetry
            err_str = stderr.read().decode("utf-8", errors="replace").strip()
            if err_str:
                self.last_telemetry["last_error"] = err_str[:120]
        except Exception as e:
            self._disconnect()
            with self._lock:
                self.last_telemetry["connected"] = False
                self.last_telemetry["last_error"] = str(e)

        return self.last_telemetry

    def test_connection(self) -> Dict[str, Any]:
        return self.poll_once()

    def execute_command(self, command: str, timeout: float = 10.0) -> Dict[str, Any]:
        """Execute a shell command over SSH and return stdout, stderr, exit_code."""
        if not self.enabled:
            return {"success": False, "error": "该服务器当前处于未启用状态", "stdout": "", "stderr": "", "exit_code": -1}

        success = self._connect()
        if not success or not self._client:
            return {"success": False, "error": f"无法建立 SSH 连接: {self.last_telemetry.get('last_error', '连接断开')}", "stdout": "", "stderr": "", "exit_code": -1}

        try:
            stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            out_str = stdout.read().decode("utf-8", errors="replace")
            err_str = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()
            return {
                "success": (exit_code == 0),
                "stdout": out_str,
                "stderr": err_str,
                "exit_code": exit_code,
                "error": "" if exit_code == 0 else (err_str or f"命令返回错误代码 {exit_code}")
            }
        except Exception as e:
            return {"success": False, "error": str(e), "stdout": "", "stderr": "", "exit_code": -1}

    def close(self):
        self._disconnect()


class SubsystemProbeManager:
    """Manages all system server probe workers."""

    def __init__(self, config_mgr=None):
        self.config_mgr = config_mgr
        self._workers: Dict[str, SystemServerWorker] = {}
        self._lock = threading.Lock()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None

    def sync_subsystems_from_config(self):
        if not self.config_mgr:
            return

        active_keys = set()
        system_servers = self.config_mgr.get_system_servers()
        for srv in system_servers:
            srv_id = srv.get("id")
            if not srv_id:
                continue
            active_keys.add(srv_id)
            with self._lock:
                if srv_id in self._workers:
                    self._workers[srv_id].update_info(srv)
                else:
                    worker = SystemServerWorker(srv, manager=self)
                    self._workers[srv_id] = worker

        with self._lock:
            for key in list(self._workers.keys()):
                if key not in active_keys:
                    self._workers[key].close()
                    del self._workers[key]

    def save_detected_os(self, srv_id: str, os_name: str):
        if not self.config_mgr or not os_name:
            return
        sys_srvs = self.config_mgr.get_system_servers()
        changed = False
        for s in sys_srvs:
            if s.get("id") == srv_id and not s.get("os_name"):
                s["os_name"] = os_name
                changed = True
        if changed:
            self.config_mgr.set_system_servers(sys_srvs)
            logger.info(f"Persisted detected OS '{os_name}' for system server {srv_id}")

    def start_monitoring(self):
        if self._running:
            return
        self._running = True
        self.sync_subsystems_from_config()
        self._monitor_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._monitor_thread.start()
        logger.info("System server probe manager started.")

    def stop_monitoring(self):
        self._running = False
        with self._lock:
            for w in self._workers.values():
                w.close()

    def _poll_loop(self):
        while self._running:
            try:
                poll_sec = 2
                if self.config_mgr:
                    # 系统服务器专属巡检与测活周期 (默认 5s，可设置)
                    poll_sec = self.config_mgr.get_int("server_ping_interval_sec", self.config_mgr.get_int("subsystem_poll_sec", 5))
                    poll_sec = max(1, min(60, poll_sec))

                self.sync_subsystems_from_config()

                workers_snapshot = []
                with self._lock:
                    workers_snapshot = list(self._workers.values())

                threads = []
                for w in workers_snapshot:
                    if w.enabled:
                        t = threading.Thread(target=w.poll_once, daemon=True)
                        threads.append(t)
                        t.start()

                for t in threads:
                    t.join(timeout=3.5)

                time.sleep(poll_sec)
            except Exception as e:
                logger.error(f"Error in system server probe loop: {e}")
                time.sleep(2)

    def force_reconnect_single(self, server_id: str):
        """中断并重新连接单台系统服务器"""
        self.sync_subsystems_from_config()
        with self._lock:
            w = self._workers.get(server_id)
            if w:
                try:
                    w.close()
                except Exception:
                    pass
                t = threading.Thread(target=w.poll_once, daemon=True)
                t.start()

    def poll_all_now(self):
        """立即并发轮询所有已启用的系统服务器"""
        self.sync_subsystems_from_config()
        workers_snapshot = []
        with self._lock:
            workers_snapshot = list(self._workers.values())
        threads = []
        for w in workers_snapshot:
            if w.enabled:
                t = threading.Thread(target=w.poll_once, daemon=True)
                threads.append(t)
                t.start()
        for t in threads:
            t.join(timeout=4.0)

    def force_reconnect_all(self):
        """强制重连所有系统服务器：断开已有或处于连接中的 SSH Session 并立即全新并发握手重连"""
        logger.info("Force reconnecting all system SSH servers...")
        self.sync_subsystems_from_config()
        with self._lock:
            for w in self._workers.values():
                try:
                    w.close()
                except Exception:
                    pass
        self.poll_all_now()

    def get_all_servers_telemetry(self) -> List[Dict[str, Any]]:
        """Return list of all system servers telemetry."""
        results = []
        with self._lock:
            for w in self._workers.values():
                results.append(dict(w.last_telemetry))
        return results

    def test_single_server(self, srv_info: Dict[str, Any]) -> Dict[str, Any]:
        temp_worker = SystemServerWorker(srv_info)
        try:
            return temp_worker.test_connection()
        finally:
            temp_worker.close()

    def execute_batch_commands(self, server_ids: List[str], command: str, timeout: float = 12.0) -> List[Dict[str, Any]]:
        """并发向所选服务器批量下发命令并收集结果"""
        self.sync_subsystems_from_config()
        results = []
        threads = []
        lock = threading.Lock()

        def _worker_exec(sid: str):
            with self._lock:
                worker = self._workers.get(sid)
            if not worker:
                # Find in config if worker not spawned
                cfg_srvs = self.config_mgr.get_system_servers() if self.config_mgr else []
                target = next((s for s in cfg_srvs if s.get("id") == sid), None)
                if target:
                    worker = SystemServerWorker(target, manager=self)
            
            res = {
                "server_id": sid,
                "server_name": worker.name if worker else sid,
                "host": worker.host if worker else "",
                "success": False,
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "error": ""
            }

            if not worker:
                res["error"] = "未找到该服务器配置"
            else:
                exec_res = worker.execute_command(command, timeout=timeout)
                res.update(exec_res)
            
            with lock:
                results.append(res)

        for sid in server_ids:
            t = threading.Thread(target=_worker_exec, args=(sid,), daemon=True)
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=timeout + 2.0)

        return results
