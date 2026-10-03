import os
import subprocess
import sys

def build():
    print("=== Building 云枢 (云枢.exe) Standalone Single-File Executable ===")
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(base_dir, "src")
    entry_script = os.path.join(src_dir, "app.py")
    icon_path = os.path.join(src_dir, "assets", "app_icon.ico")
    
    # Windows PyInstaller data format: src;dst
    ui_data = f"{os.path.join(src_dir, 'ui')};ui"
    assets_data = f"{os.path.join(src_dir, 'assets')};assets"
    
    cmd = [
        sys.executable,
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath=.",     # 直接输出到根目录，不另外创建 dist 文件夹
        "--onefile",       # Single standalone exe
        "--windowed",      # No console window
        "--name=云枢",
        f"--icon={icon_path}",
        "--add-data", ui_data,
        "--add-data", assets_data,
        "--hidden-import=webview",
        "--hidden-import=bottle",
        "--hidden-import=clr_loader",
        "--hidden-import=pythonnet",
        "--hidden-import=configparser",
        "--hidden-import=logging",
        "--hidden-import=webbrowser",
        "--hidden-import=subprocess",
        "--hidden-import=paramiko",
        "--hidden-import=bcrypt",
        "--hidden-import=cryptography",
        "--hidden-import=pynacl",
        "--hidden-import=winsound",
        entry_script
    ]
    
    print("Running command:", " ".join(cmd))
    res = subprocess.run(cmd)
    if res.returncode == 0:
        print("\n=======================================================")
        print("  Build completed successfully!")
        print("  Output binary: 云枢.exe")
        print("=======================================================\n")
    else:
        print("Build failed with code:", res.returncode)

if __name__ == "__main__":
    build()
