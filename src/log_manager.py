"""
Dell Fan Sense - Log Manager
- Separate log file persistence: logs/app.log (or app_YYYYMMDD.log)
- In-memory real-time ring buffer for Web UI log streaming
- Real-time Error-only filter vs Debug mode (All logs: INFO, WARNING, ERROR, DEBUG)
- Configurable retention period (e.g. 7 days, 15 days, 30 days) with automatic cleanup on startup/rotation
"""

import os
import sys
import time
import glob
import logging
from collections import deque
import threading
from typing import List, Dict, Any

class SmartLogFilter(logging.Filter):
    """
    智能日志过滤器：
    即使用户开启调试模式，也严禁无异常的周期性轮询与底噪（如 Paramiko 通道握手包、动态温控微调、心跳探测）刷屏。
    只保留：
    1. 所有级别为 WARNING / ERROR / CRITICAL 的异常报错信息
    2. 关键业务与生命周期操作的重要信息（开关机、节点上下线状态变更、用户配置与调速、预警通知、资产检测）
    """
    NOISY_LOGGERS = {
        "paramiko", "paramiko.transport", "paramiko.transport.sftp",
        "urllib3", "urllib3.connectionpool", "webview", "clr_loader",
        "werkzeug", "bottle"
    }

    NOISY_KEYWORDS = [
        "dynamic adjust",
        "探针瞬时抖动容错",
        "sending packet",
        "received packet",
        "eof received",
        "kex algos",
        "ciphers",
        "initial fast ping",
        "probe loop tick",
        "fast cwd",
        "error reading ssh protocol banner",
        "incompatible ssh peer",
        "eof in transport thread"
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        # 1. 过滤第三方库内部输出的冗余 Traceback 或瞬态中断堆栈（如 paramiko 的 _check_banner EOFError）
        # 此类网络握手瞬态异常由业务层 (ssh_probe) 汇总为简洁友好的错误提示，不直接在底层打印多行 Python 堆栈刷屏
        logger_name = (record.name or "").lower()
        msg = (record.getMessage() or "").lower()

        if "paramiko" in logger_name:
            for kw in ("error reading ssh protocol banner", "incompatible ssh peer", "traceback (most recent call last)"):
                if kw in msg:
                    return False

        # 2. 任何警告、错误及严重故障，100% 绝对保留
        if record.levelno >= logging.WARNING:
            return True

        # 3. 静音纯噪音第三方库的 INFO / DEBUG
        for noisy in self.NOISY_LOGGERS:
            if logger_name == noisy or logger_name.startswith(noisy + "."):
                return False

        # 4. 过滤无报错周期性轮询的重复底噪消息
        for kw in self.NOISY_KEYWORDS:
            if kw in msg:
                return False

        # 5. 其余重要生命周期与操作日志保留
        return True


class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity=500):
        super().__init__()
        self.capacity = capacity
        self.buffer = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            msg = self.format(record)
            entry = {
                "id": f"log_{int(time.time() * 1000)}_{record.created}",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
                "time_short": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "formatted": msg
            }
            with self._lock:
                self.buffer.append(entry)
        except Exception:
            self.handleError(record)

    def get_logs(self, debug_mode=False, limit=200) -> List[Dict[str, Any]]:
        with self._lock:
            all_entries = list(self.buffer)
        
        # If debug_mode is False: ONLY return WARNING, ERROR, CRITICAL
        if not debug_mode:
            filtered = [e for e in all_entries if e["level"] in ("ERROR", "CRITICAL", "WARNING")]
        else:
            # 开启调试模式：返回重要事件与报错信息（轮询底噪已被 SmartLogFilter 过滤）
            filtered = all_entries

        # Return latest entries up to limit
        return filtered[-limit:]

    def clear(self):
        with self._lock:
            self.buffer.clear()


class LogManager:
    _instance = None
    _lock = threading.Lock()

    def __init__(self, log_dir="logs", config_mgr=None):
        self.log_dir = os.path.abspath(log_dir)
        self.config_mgr = config_mgr
        self.memory_handler = MemoryLogHandler(capacity=1000)
        self.debug_mode = False
        
        os.makedirs(self.log_dir, exist_ok=True)
        self.current_log_file = os.path.join(self.log_dir, f"app_{time.strftime('%Y%m%d')}.log")
        self._setup_logging()
        self.cleanup_expired_logs()

    @classmethod
    def get_instance(cls, config_mgr=None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = LogManager(config_mgr=config_mgr)
            elif config_mgr and not cls._instance.config_mgr:
                cls._instance.config_mgr = config_mgr
            return cls._instance

    def _setup_logging(self):
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        smart_filter = SmartLogFilter()

        self.memory_handler.setFormatter(formatter)
        self.memory_handler.setLevel(logging.DEBUG)
        self.memory_handler.addFilter(smart_filter)

        # Mute verbose third-party loggers so routine polling never spams
        for noisy_name in ("paramiko", "paramiko.transport", "urllib3", "urllib3.connectionpool", "webview", "clr_loader", "werkzeug", "bottle"):
            logging.getLogger(noisy_name).setLevel(logging.WARNING)

        # File handler for dedicated daily/app log
        try:
            file_handler = logging.FileHandler(self.current_log_file, encoding="utf-8", delay=True)
            file_handler.setFormatter(formatter)
            file_handler.setLevel(logging.DEBUG)
            file_handler.addFilter(smart_filter)
        except Exception as e:
            file_handler = None
            print(f"Warning: Could not create file log handler: {e}")

        # Attach to root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(self.memory_handler)
        if file_handler:
            root_logger.addHandler(file_handler)

    def set_debug_mode(self, enabled: bool):
        self.debug_mode = bool(enabled)
        if self.config_mgr:
            self.config_mgr.set("log_debug_mode", self.debug_mode)
            self.config_mgr.save()

    def get_debug_mode(self) -> bool:
        if self.config_mgr:
            return bool(self.config_mgr.get("log_debug_mode", False))
        return self.debug_mode

    def get_logs(self, limit=300) -> List[Dict[str, Any]]:
        debug = self.get_debug_mode()
        return self.memory_handler.get_logs(debug_mode=debug, limit=limit)

    def clear_memory_logs(self):
        self.memory_handler.clear()

    def cleanup_expired_logs(self):
        """Clean up log files older than configured retention days (default: 7 days)"""
        try:
            retention_days = 7
            if self.config_mgr:
                retention_days = self.config_mgr.get_int("log_retention_days", 7)
            
            if retention_days <= 0:
                return  # 0 or negative means never delete

            cutoff_sec = time.time() - (retention_days * 86400)
            pattern = os.path.join(self.log_dir, "app_*.log")
            for fpath in glob.glob(pattern):
                try:
                    if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff_sec:
                        os.remove(fpath)
                except Exception:
                    pass
        except Exception as e:
            logging.getLogger("log_manager").warning(f"Error cleaning expired logs: {e}")
