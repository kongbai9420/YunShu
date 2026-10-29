# 云枢 Linux 压缩包的程序目录（PyInstaller onedir，控制台程序 yunshu）。
# 由 scripts/build-linux.sh 在 AlmaLinux 8（glibc 2.28）+ Python 3.11 环境里调用；数据文件与隐式导入与 scripts/desktop.spec 保持一致，
# 去掉窗口相关的部分。入口等同于 `python -m app`（app.cli.main）。
import os
import sys
from pathlib import Path

import importlib.metadata

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
backend = root / "backend"
frontend_dist = Path(os.environ.get("YUNSHU_FRONTEND_DIST") or root / "frontend" / "dist")
if not (frontend_dist / "index.html").is_file():
    raise SystemExit(f"找不到前端构建结果：{frontend_dist}/index.html")

# collect_submodules 在子进程里导入包：不先把 backend 放进 sys.path 会什么都收不到，打出来的程序找不到 app.main。
# 只加 backend（不是 backend/app），不会遮住标准库。桌面窗口与 Windows 服务模块不打进 Linux 包
sys.path.insert(0, str(backend))
app_modules = collect_submodules("app", filter=lambda name: name not in ("app.desktop", "app.winservice"))
if "app.main" not in app_modules or "app.cli" not in app_modules:
    raise SystemExit("没有收集到 app 包的模块，请检查 backend 路径")

# 入口脚本写在构建目录里：不能直接用 backend/app/__main__.py，否则 backend/app 会被加进搜索路径，
# app/queue.py 之类的模块会遮住同名标准库模块
entry = Path(workpath) / "yunshu_entry.py"
entry.parent.mkdir(parents=True, exist_ok=True)
entry.write_text("from app.cli import main\n\nraise SystemExit(main())\n", encoding="utf-8")

# 随包的 OpenSSL 是在 EL8 上编译的，默认证书位置是 /etc/pki/tls/…；Debian / Ubuntu 上没有这个文件，
# 标准库的 ssl / urllib 会因此无法校验 HTTPS 证书。启动时若默认位置不存在，改用本机的系统证书包，都没有时用 certifi。
ca_hook = Path(workpath) / "yunshu_rthook_ca.py"
ca_hook.write_text('''import os, ssl
if not os.environ.get("SSL_CERT_FILE") and not os.environ.get("SSL_CERT_DIR"):
    _paths = ssl.get_default_verify_paths()
    if not (_paths.cafile and os.path.isfile(_paths.cafile)):
        for _candidate in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                           "/etc/ssl/ca-bundle.pem", "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"):
            if os.path.isfile(_candidate):
                os.environ["SSL_CERT_FILE"] = _candidate
                break
        else:
            try:
                import certifi
                os.environ["SSL_CERT_FILE"] = certifi.where()
            except ImportError:
                pass
''', encoding="utf-8")

# 运行时依赖的 *.dist-info 一并打进包：SBOM 工具扫描解压后的程序目录时能按包名、版本、许可证识别
# （只用于构建的 PyInstaller 工具链与 pip 不算运行时依赖）
BUILD_ONLY = {"pip", "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "packaging", "setuptools"}
runtime_metadata = []
for dist in importlib.metadata.distributions():
    name = dist.metadata["Name"]
    if name and name.lower().replace("_", "-") not in BUILD_ONLY:
        runtime_metadata += copy_metadata(name)

a = Analysis(
    [str(entry)],
    pathex=[str(backend)],
    datas=[
        (str(frontend_dist), "frontend"),
        (str(backend / "alembic.ini"), "."),
        (str(backend / "migrations"), "migrations"),
        (str(backend / "app" / "collect" / "profiles.json"), "app/collect"),
        (str(backend / "app" / "ai" / "presets.json"), "app/ai"),
        (str(root / "LICENSE"), "."),
    ]
    # 主机一般有 /usr/share/zoneinfo，但最小化安装或容器里可能没有；zoneinfo 找不到系统时区库时读 tzdata 包
    + collect_data_files("tzdata") + runtime_metadata,
    hiddenimports=app_modules + collect_submodules("uvicorn")
        + collect_submodules("pysnmp.smi.mibs") + collect_submodules("tzdata")
        # 标准版（PostgreSQL）驱动：按连接串在运行时才导入
        + collect_submodules("psycopg") + collect_submodules("psycopg_binary"),
    # readline：服务端用不到，且系统的 libreadline 是 GPL-3.0，不随包分发
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "gtk", "gi", "webview", "readline",
              "pytest", "_pytest", "snmpsim", "IPython"],
    binaries=[], hookspath=[], runtime_hooks=[str(ca_hook)], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="yunshu",
          console=True, debug=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="yunshu", strip=False, upx=False)
