import os
import sys
import logging
import webview

from config_manager import ConfigManager
from log_manager import LogManager
from ipmi_core import IPMICore
from ssh_probe import SubsystemProbeManager
from alert_engine import AlertEngine
from api_bridge import APIBridge

# Initialize dedicated daily logging engine
config_mgr = ConfigManager()
log_mgr = LogManager.get_instance(config_mgr=config_mgr)
logger = logging.getLogger("App")

def get_asset_path(relative_path):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)

def apply_dark_window_attributes(window):
    """Enable immersive dark mode in Windows DWM so any system elements match dark mode."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        dwmapi = ctypes.windll.dwmapi
        if hasattr(window, "native") and window.native:
            hwnd = int(window.native.Handle.ToInt64())
            true_val = ctypes.c_int(1)
            # DWMWA_USE_IMMERSIVE_DARK_MODE: 20 (Windows 10 20H1+ and Windows 11), 19 (Windows 10 1809-1909)
            dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(true_val), 4)
            dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(true_val), 4)
            # Windows 11 border & caption colors: match #161618 (BGR: 0x00181616)
            dark_color = ctypes.c_uint(0x00181616)
            dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(dark_color), 4)
            dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(dark_color), 4)
    except Exception as e:
        logger.warning(f"Could not apply DWM dark attributes: {e}")

def main():
    logger.info("Starting 云枢 (YunShu) - Multi-brand Server Cluster & Thermal Management Center...")

    # Initialize Subsystem Probe & Alert Engine
    ssh_probe_mgr = SubsystemProbeManager(config_mgr)
    alert_engine = AlertEngine(config_mgr)

    # Initialize IPMI Engine
    ipmi_core = IPMICore(config_mgr)
    
    # Start background monitoring
    ipmi_core.start_monitoring()
    ssh_probe_mgr.start_monitoring()

    # Create JS Bridge
    bridge = APIBridge(ipmi_core, config_mgr, ssh_probe_mgr, alert_engine, log_mgr)

    # Resolve UI entry path
    ui_index_path = get_asset_path(os.path.join("ui", "index.html"))
    if not os.path.exists(ui_index_path):
        ui_index_path = os.path.abspath(os.path.join(os.getcwd(), "src", "ui", "index.html"))

    logger.info(f"Loading UI from: {ui_index_path}")

    # Create Frameless Window:
    window = webview.create_window(
        title="云枢 · 多品牌服务器集群温控与运维中心",
        url=ui_index_path,
        js_api=bridge,
        width=1100,
        height=740,
        min_size=(960, 640),
        frameless=True,       # No duplicate Windows title bar!
        easy_drag=False,      # Disabled: prevents Windows Hook message-pump starvation deadlock!
        resizable=True,
        text_select=False,
        background_color="#161618"
    )
    bridge.set_window(window)
    if sys.platform == "win32":
        window.events.shown += lambda: apply_dark_window_attributes(window)

    try:
        if sys.platform == "darwin":
            webview.start(gui="cocoa", debug=False)
        else:
            webview.start(gui="edgechromium", http_server=True, debug=False)
    except Exception as e:
        logger.warning(f"GUI start fallback: {e}")
        webview.start(http_server=True, debug=False)
    finally:
        try:
            ipmi_core.stop_monitoring()
        except Exception:
            pass
        try:
            ssh_probe_mgr.stop_monitoring()
        except Exception:
            pass
        if sys.platform == "win32":
            try:
                import subprocess
                subprocess.run(["taskkill", "/F", "/IM", "ipmitool.exe"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=0x08000000)
            except Exception:
                pass
        import time
        time.sleep(0.15)
        logger.info("Application closed.")

if __name__ == "__main__":
    main()
