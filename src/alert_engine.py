"""
Dell Fan Sense - Alert Engine
Multi-dimensional threshold monitoring, Sound alerts & TTS speech synthesis.
Strictly decoupled:
- Section A: Hardware Nodes (IPMI physical CPU temperature)
- Section B: System Servers (Linux CPU, Memory, Swap, Disk)
"""

import os
import sys
import time
import json
import logging
import threading
import subprocess
import urllib.request
import urllib.parse
from typing import Dict, Any, List

logger = logging.getLogger("alert_engine")

DEFAULT_ALERT_CONFIG = {
    "enabled": True,
    "cooldown_sec": 60,
    "sound_enabled": True,
    "sound_type": "default",
    "custom_sound_path": "",
    "tts_enabled": True,
    "tts_speech_rate": 0,
    # Section A: 硬件节点阈值
    "node_cpu_temp_enabled": True,
    "node_cpu_temp_threshold": 80,    # °C
    # Section B: 系统服务器阈值
    "server_cpu_enabled": True,
    "server_cpu_threshold": 90,       # %
    "server_mem_enabled": True,
    "server_mem_threshold": 90,       # %
    "server_swap_enabled": True,
    "server_swap_threshold": 80,      # %
    "server_disk_enabled": True,
    "server_disk_threshold": 92,      # %
    # Section C: 多渠道外部消息通知 (移植借鉴自 GitHub 开源项目 Apprise 与 ANotify)
    "webhook_enabled": False,
    "webhook_channel": "custom_webhook",  # custom_webhook, serverchan, dingtalk, feishu, wechat_work, bark, pushplus
    "webhook_url": "",
    "webhook_secret": "",                # 密钥 / Token
    # Section D: 自定义告警模版内容
    "custom_alert_title_template": "【{level_text}】{source} 触发告警",
    "custom_alert_body_template": "告警项目: {rule_title}\n告警源: {source}\n当前数值: {current_val}\n设定阈值: {threshold_val}\n详情说明: {detail_msg}\n发生时间: {alert_time}"
}

class AlertEngine:
    def __init__(self, config_mgr=None):
        self.config_mgr = config_mgr
        self._lock = threading.Lock()
        self._last_alert_time: Dict[str, float] = {}
        self._alert_history: List[Dict[str, Any]] = []
        self._max_history = 100

    def get_config(self) -> Dict[str, Any]:
        if not self.config_mgr:
            return dict(DEFAULT_ALERT_CONFIG)
        cfg = dict(DEFAULT_ALERT_CONFIG)
        for k in DEFAULT_ALERT_CONFIG.keys():
            val = self.config_mgr.get_alert_param(k)
            if val is not None:
                cfg[k] = val
        return cfg

    def set_config(self, new_cfg: Dict[str, Any]) -> bool:
        if not self.config_mgr:
            return False
        for k, v in new_cfg.items():
            if k in DEFAULT_ALERT_CONFIG:
                self.config_mgr.set_alert_param(k, v)
        self.config_mgr.save()
        return True

    def get_history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._alert_history)

    def clear_history(self):
        with self._lock:
            self._alert_history.clear()

    def check_telemetry(self, ipmi_status: Dict[str, Any], cluster_telemetry: List[Dict[str, Any]], system_servers_telemetry: List[Dict[str, Any]]):
        cfg = self.get_config()
        if not cfg.get("enabled", True):
            return

        cooldown = int(cfg.get("cooldown_sec", 60))
        now = time.time()

        # ==========================================
        # 板块 A: 硬件物理节点报警检测 (Node Section)
        # ==========================================
        if cfg.get("node_cpu_temp_enabled", True):
            temp_limit = float(cfg.get("node_cpu_temp_threshold", 80))
            for node_snap in cluster_telemetry:
                node_id = node_snap.get("id")
                node_name = node_snap.get("name", "硬件节点")
                cpu_temp = node_snap.get("max_cpu_temp")
                if cpu_temp and cpu_temp >= temp_limit:
                    key = f"node_temp:{node_id}"
                    if now - self._last_alert_time.get(key, 0) >= cooldown:
                        self._last_alert_time[key] = now
                        self._trigger_alert(
                            rule_id="node_temp",
                            title="硬件节点 CPU 高温警报",
                            message=f"物理节点「{node_name}」核心温度达 {cpu_temp}°C，超过安全阈值 {temp_limit}°C！",
                            level="danger",
                            source=f"节点: {node_name}"
                        )

        # ==========================================
        # 板块 B: 系统服务器报警检测 (Server Section)
        # ==========================================
        srv_cpu_on = cfg.get("server_cpu_enabled", True)
        srv_cpu_limit = float(cfg.get("server_cpu_threshold", 90))
        srv_mem_on = cfg.get("server_mem_enabled", True)
        srv_mem_limit = float(cfg.get("server_mem_threshold", 90))
        srv_swap_on = cfg.get("server_swap_enabled", True)
        srv_swap_limit = float(cfg.get("server_swap_threshold", 80))
        srv_disk_on = cfg.get("server_disk_enabled", True)
        srv_disk_limit = float(cfg.get("server_disk_threshold", 92))

        for srv in system_servers_telemetry:
            if not srv.get("connected"):
                continue
            srv_id = srv.get("id")
            srv_name = srv.get("name", "系统服务器")

            if srv_cpu_on:
                c_pct = float(srv.get("cpu_pct", 0))
                if c_pct >= srv_cpu_limit:
                    key = f"srv_cpu:{srv_id}"
                    if now - self._last_alert_time.get(key, 0) >= cooldown:
                        self._last_alert_time[key] = now
                        self._trigger_alert(
                            rule_id="srv_cpu",
                            title="系统服务器 CPU 满载告警",
                            message=f"服务器「{srv_name}」CPU 负载达 {c_pct}%，超过报警阈值 {srv_cpu_limit}%！",
                            level="warning",
                            source=f"服务器: {srv_name}"
                        )

            if srv_mem_on:
                m_pct = float(srv.get("mem_pct", 0))
                if m_pct >= srv_mem_limit:
                    key = f"srv_mem:{srv_id}"
                    if now - self._last_alert_time.get(key, 0) >= cooldown:
                        self._last_alert_time[key] = now
                        self._trigger_alert(
                            rule_id="srv_mem",
                            title="系统服务器 物理内存告急",
                            message=f"服务器「{srv_name}」内存已占用 {m_pct}% ({srv.get('mem_used_gb', 0)}/{srv.get('mem_total_gb', 0)} GB)！",
                            level="danger",
                            source=f"服务器: {srv_name}"
                        )

            if srv_swap_on:
                sw_pct = float(srv.get("swap_pct", 0))
                sw_tot = float(srv.get("swap_total_gb", 0))
                if sw_tot > 0 and sw_pct >= srv_swap_limit:
                    key = f"srv_swap:{srv_id}"
                    if now - self._last_alert_time.get(key, 0) >= cooldown:
                        self._last_alert_time[key] = now
                        self._trigger_alert(
                            rule_id="srv_swap",
                            title="系统服务器 Swap 剧烈换页警报",
                            message=f"服务器「{srv_name}」Swap 交换空间使用达 {sw_pct}%，有卡死风险！",
                            level="warning",
                            source=f"服务器: {srv_name}"
                        )

            if srv_disk_on:
                d_pct = float(srv.get("disk_pct", 0))
                if d_pct >= srv_disk_limit:
                    key = f"srv_disk:{srv_id}"
                    if now - self._last_alert_time.get(key, 0) >= cooldown:
                        self._last_alert_time[key] = now
                        self._trigger_alert(
                            rule_id="srv_disk",
                            title="系统服务器 磁盘空间耗尽警报",
                            message=f"服务器「{srv_name}」根分区已使用 {d_pct}%，即将写满！",
                            level="danger",
                            source=f"服务器: {srv_name}",
                            current_val=f"{d_pct}%",
                            threshold_val=f"{srv_disk_limit}%"
                        )

    def _trigger_alert(self, rule_id: str, title: str, message: str, level: str, source: str, current_val: str = "--", threshold_val: str = "--"):
        alert_time_str = time.strftime("%Y-%m-%d %H:%M:%S")
        cfg = self.get_config()

        # 自定义告警模版动态变量渲染
        level_map = {"danger": "紧急", "warning": "警告", "info": "提示"}
        level_text = level_map.get(level, "告警")

        tmpl_title = cfg.get("custom_alert_title_template") or "【{level_text}】{source} 触发告警"
        tmpl_body = cfg.get("custom_alert_body_template") or "告警项目: {rule_title}\n告警源: {source}\n当前数值: {current_val}\n设定阈值: {threshold_val}\n详情说明: {detail_msg}\n发生时间: {alert_time}"

        render_vars = {
            "level_text": level_text,
            "level": level,
            "source": source,
            "rule_id": rule_id,
            "rule_title": title,
            "detail_msg": message,
            "current_val": current_val,
            "threshold_val": threshold_val,
            "alert_time": alert_time_str
        }

        try:
            rendered_title = tmpl_title.format(**render_vars)
        except Exception:
            rendered_title = f"【{level_text}】{title}"

        try:
            rendered_body = tmpl_body.format(**render_vars)
        except Exception:
            rendered_body = f"{message} (时间: {alert_time_str})"

        item = {
            "id": f"evt_{int(time.time() * 1000)}",
            "time": time.strftime("%H:%M:%S"),
            "date": time.strftime("%Y-%m-%d"),
            "rule_id": rule_id,
            "title": rendered_title,
            "message": rendered_body,
            "raw_message": message,
            "level": level,
            "source": source
        }

        with self._lock:
            self._alert_history.insert(0, item)
            if len(self._alert_history) > self._max_history:
                self._alert_history = self._alert_history[:self._max_history]

        logger.warning(f"ALERT: [{rendered_title}] {rendered_body}")

        threading.Thread(target=self._play_notification, args=(cfg, rendered_title, rendered_body, message), daemon=True).start()

    def _play_notification(self, cfg: Dict[str, Any], rendered_title: str, rendered_body: str, orig_message: str):
        # 1. 声音播报
        if cfg.get("sound_enabled", True):
            self.play_alert_sound(cfg.get("sound_type", "default"), cfg.get("custom_sound_path", ""))

        # 2. TTS 语音合成朗读
        if cfg.get("tts_enabled", True):
            time.sleep(0.4)
            speech_text = f"警告！{rendered_title}。{orig_message}"
            self.speak_text(speech_text)

        # 3. 外部 Webhook / 消息通知管道 (移植借鉴自开源项目 Apprise 与 ANotify)
        if cfg.get("webhook_enabled", False):
            self.send_external_notification(cfg, rendered_title, rendered_body)

    def send_external_notification(self, cfg: Dict[str, Any], title: str, content: str) -> Dict[str, Any]:
        """
        跨平台消息通知发送模块 (架构设计移植借鉴自 GitHub 开源项目 Apprise 与 ANotify)
        支持通道:
        - custom_webhook (通用 Webhook POST JSON)
        - serverchan (Server酱 方糖推送 / SendKey)
        - dingtalk (钉钉机器人 Webhook)
        - feishu (飞书群机器人 Webhook)
        - wechat_work (企业微信群机器人 Webhook)
        - bark (iOS Bark 推送)
        - pushplus (PushPlus 推送加)
        """
        channel = cfg.get("webhook_channel", "custom_webhook")
        url = (cfg.get("webhook_url") or "").strip()
        secret = (cfg.get("webhook_secret") or "").strip()

        if not url and channel not in ("serverchan", "pushplus", "bark"):
            return {"success": False, "error": "通知通道未配置有效的目标 URL / Webhook 地址"}

        try:
            req_url = url
            payload = None
            headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "YunShu-AlertEngine/3.6"}

            if channel == "custom_webhook":
                payload = json.dumps({"title": title, "content": content, "timestamp": int(time.time())}).encode("utf-8")

            elif channel == "serverchan":
                # 支持 https://sctapi.ftqq.com/<SendKey>.send 或直接在 secret 填 key
                key = secret if secret else url
                if not key.startswith("http"):
                    req_url = f"https://sctapi.ftqq.com/{key}.send"
                payload = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"

            elif channel == "dingtalk":
                # 钉钉自定义机器人
                data = {
                    "msgtype": "markdown",
                    "markdown": {
                        "title": title,
                        "text": f"### {title}\n\n" + content.replace("\n", "\n\n")
                    }
                }
                payload = json.dumps(data).encode("utf-8")

            elif channel == "feishu":
                # 飞书自定义机器人
                data = {
                    "msg_type": "post",
                    "content": {
                        "post": {
                            "zh_cn": {
                                "title": title,
                                "content": [[{"tag": "text", "text": content}]]
                            }
                        }
                    }
                }
                payload = json.dumps(data).encode("utf-8")

            elif channel == "wechat_work":
                # 企业微信机器人
                data = {
                    "msgtype": "markdown",
                    "markdown": {
                        "content": f"### <font color=\"warning\">{title}</font>\n" + content
                    }
                }
                payload = json.dumps(data).encode("utf-8")

            elif channel == "bark":
                # Bark iOS 极简推送
                b_url = url.rstrip("/")
                if not b_url.startswith("http"):
                    b_url = f"https://api.day.app/{b_url}"
                data = {"title": title, "body": content, "group": "云枢", "icon": "https://img.icons8.com/color/96/server.png"}
                req_url = b_url
                payload = json.dumps(data).encode("utf-8")

            elif channel == "pushplus":
                token = secret if secret else url
                data = {
                    "token": token,
                    "title": title,
                    "content": content.replace("\n", "<br/>"),
                    "template": "html"
                }
                req_url = "http://www.pushplus.plus/send"
                payload = json.dumps(data).encode("utf-8")

            req = urllib.request.Request(req_url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                resp_text = resp.read().decode("utf-8", errors="replace")
                logger.info(f"External notification sent via [{channel}]: status {resp.status}, resp {resp_text[:100]}")
                return {"success": True, "channel": channel, "response": resp_text}
        except Exception as e:
            logger.error(f"Failed to send external notification via [{channel}]: {e}")
            return {"success": False, "channel": channel, "error": str(e)}

    def play_alert_sound(self, sound_type="default", custom_path=""):
        try:
            target_wav = ""
            if sound_type == "custom" and custom_path and os.path.isfile(custom_path):
                target_wav = custom_path
            else:
                if sys.platform == "win32":
                    candidates = [
                        r"C:\Windows\Media\Alarm01.wav",
                        r"C:\Windows\Media\Windows Critical Stop.wav",
                        r"C:\Windows\Media\Windows Exclamation.wav",
                        r"C:\Windows\Media\tada.wav"
                    ]
                    for p in candidates:
                        if os.path.exists(p):
                            target_wav = p
                            break
                elif sys.platform == "darwin":
                    candidates = [
                        "/System/Library/Sounds/Sosumi.aiff",
                        "/System/Library/Sounds/Ping.aiff",
                        "/System/Library/Sounds/Basso.aiff"
                    ]
                    for p in candidates:
                        if os.path.exists(p):
                            target_wav = p
                            break

            if target_wav:
                if sys.platform == "win32":
                    import winsound
                    winsound.PlaySound(target_wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
                elif sys.platform == "darwin":
                    subprocess.run(["afplay", target_wav], capture_output=True)
            else:
                if sys.platform == "win32":
                    import winsound
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                else:
                    print("\a")
        except Exception as e:
            logger.error(f"Failed to play alert sound: {e}")

    def speak_text(self, text: str):
        if not text:
            return
        safe_text = text.replace("'", " ").replace('"', " ").replace("\n", " ").strip()
        try:
            if sys.platform == "win32":
                # 方案 1 (首选): 进程内原生调用，零外部进程，火绒等安全软件 0 拦截
                # 利用 pythonnet / CLR 原生调用 Windows .NET System.Speech.Synthesis
                try:
                    import clr
                    clr.AddReference('System.Speech, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35')
                    from System.Speech.Synthesis import SpeechSynthesizer
                    synth = SpeechSynthesizer()
                    synth.SpeakAsync(safe_text)
                    return
                except Exception as clr_err:
                    logger.debug(f"In-process .NET TTS not available: {clr_err}")

                # 方案 2 (温和降级): 若无 TTS 引擎，使用 Windows 原生消息蜂鸣提示，绝不拉起隐藏 PowerShell 进程
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            elif sys.platform == "darwin":
                subprocess.run(["say", safe_text], capture_output=True, timeout=10)
        except Exception as e:
            logger.error(f"TTS Speech error: {e}")

    def test_alert(self, sound=True, tts=True):
        self._trigger_alert(
            rule_id="manual_test",
            title="Dell Fan Sense 告警测试",
            message="这是一条告警引擎测试广播，节点与服务器监控系统工作正常。",
            level="info",
            source="系统测试"
        )
        return {"success": True, "message": "告警测试已触发"}
