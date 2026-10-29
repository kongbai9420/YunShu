import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
backend = root / "backend"
# collect_submodules 在子进程里导入包：不先把 backend 放进 sys.path 会什么都收不到（窗口能用只是因为入口直接导入了 app.main）
sys.path.insert(0, str(backend))
app_modules = collect_submodules("app")
if "app.main" not in app_modules:
    raise SystemExit("没有收集到 app 包的模块，请检查 backend 路径")
a = Analysis(
    [str(root / "scripts" / "desktop_entry.py")],
    pathex=[str(backend)],
    datas=[
        (str(root / "frontend" / "dist"), "frontend"),
        (str(backend / "alembic.ini"), "."),
        (str(backend / "migrations"), "migrations"),
        (str(backend / "app" / "collect" / "profiles.json"), "app/collect"),
        (str(backend / "app" / "ai" / "presets.json"), "app/ai"),
        (str(root / "LICENSE"), "."),
    ] + copy_metadata("pywebview")
    # Windows 没有系统时区库：zoneinfo 运行时才按需读 tzdata 包，静态分析看不到，显式打进包里
    + collect_data_files("tzdata"),
    hiddenimports=app_modules + collect_submodules("uvicorn")
        + collect_submodules("pysnmp.smi.mibs") + collect_submodules("tzdata")
        + ["webview.platforms.winforms", "webview.platforms.edgechromium"],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "gtk", "gi", "pytest", "snmpsim"],
    binaries=[], hookspath=[], runtime_hooks=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Yunshu",
          console=False, debug=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Yunshu", strip=False, upx=False)
