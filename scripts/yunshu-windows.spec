# Windows 服务版的控制台程序 yunshu.exe（PyInstaller onedir）。scripts/build-windows.ps1 调用；
# 同一个目录也复制进免安装 ZIP 的 cli\（scripts/build-desktop.ps1）。
# 入口 app/winservice.py：`yunshu.exe service` 由服务管理器启动，其他命令与 app.cli.main（`yunshu`）相同。
# 数据文件、时区数据与隐藏导入与 scripts/desktop.spec 保持一致；不含桌面窗口（pywebview / WebView2）。
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent
backend = root / "backend"

# 维护工具由 app.cli 用 runpy 按名字运行、页面由 uvicorn 按字符串 app.main 加载，静态分析都看不到，
# 整个 app 包都收进来（桌面窗口模块除外）。collect_submodules 在子进程里导入，先把 backend 放进 sys.path
sys.path.insert(0, str(backend))
app_modules = collect_submodules("app", filter=lambda name: name != "app.desktop")
# 标准版（PostgreSQL）驱动：构建环境装了 requirements-standard.lock 时一起打包
try:
    import psycopg  # noqa: F401
    pg_modules = collect_submodules("psycopg")  # psycopg_binary 由 pyinstaller-hooks-contrib 的钩子收集
except ImportError:
    pg_modules = []
if "app.main" not in app_modules:
    raise SystemExit("没有收集到 app 包的模块，请检查 backend 路径")

a = Analysis(
    [str(backend / "app" / "winservice.py")],
    pathex=[str(backend)],
    datas=[
        (str(root / "frontend" / "dist"), "frontend"),
        (str(backend / "alembic.ini"), "."),
        (str(backend / "migrations"), "migrations"),
        (str(backend / "app" / "collect" / "profiles.json"), "app/collect"),
        (str(backend / "app" / "ai" / "presets.json"), "app/ai"),
        (str(root / "LICENSE"), "."),
    ]
    # Windows 没有系统时区库：zoneinfo 运行时才按需读 tzdata 包，静态分析看不到，显式打进包里
    + collect_data_files("tzdata"),
    hiddenimports=app_modules + collect_submodules("uvicorn")
        + collect_submodules("pysnmp.smi.mibs") + collect_submodules("tzdata") + pg_modules,
    excludes=["webview", "clr", "clr_loader", "pythonnet", "PyQt5", "PyQt6", "PySide2", "PySide6",
              "gtk", "gi", "tkinter", "pytest", "snmpsim"],
    binaries=[], hookspath=[], runtime_hooks=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="yunshu",
          console=True, debug=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="yunshu", strip=False, upx=False)
