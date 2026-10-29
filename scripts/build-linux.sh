#!/bin/bash
# 构建云枢 Linux 压缩包：yunshu-<版本>-linux-<架构>.tar.gz 与 .sha256（自带 Python 运行时 + systemd 单元 + 安装脚本）。
#
#   scripts/build-linux.sh --docker                 在固定摘要的 almalinux:8 容器里构建（本机架构）
#   scripts/build-linux.sh [--frontend-dist DIR]    已在 EL8（glibc 2.28）+ Python 3.11 环境里时直接构建（CI 用）
#   选项：--output DIR（默认 artifacts/linux）
#
# glibc 基线 2.28：在 AlmaLinux 8 上用 AppStream 的 python3.11（带共享库 libpython3.11.so，PyInstaller 需要；
# PyPA manylinux_2_28 镜像里的 CPython 是 --disable-shared 静态构建，PyInstaller 用不了）构建，
# 以便同一个包能在麒麟 V10、openEuler 20.03、EL8 及更新的系统上运行。构建后检查所有 ELF 文件实际需要的
# glibc / libstdc++ 符号版本，超过 YUNSHU_GLIBC_MAX（默认 2.28）/ YUNSHU_GLIBCXX_MAX（默认 3.4.25，GCC 8）就失败；
# 实际需要的 glibc 版本写进包里的 GLIBC_REQUIRED，install.sh 安装前对照主机检查。
# EL8 自带的 SQLite 是 3.26，而平台的任务认领用 UPDATE … RETURNING（3.35+），所以从 sqlite.org 的源码
# （版本与 SHA-256 固定在下面）编译 libsqlite3.so.0，让 PyInstaller 打进包里替代系统库。
# 依赖按哈希安装：运行时用 backend/requirements-standard.lock（含 PostgreSQL 驱动），
# PyInstaller 用 backend/requirements-package.lock 里的同一版本与哈希（deploy/linux/lock_subset.py 筛选）。
# 冒烟测试只用临时目录与应用自带的初始账号 admin / admin123（测试凭据），不读 /etc/yunshu。

set -euo pipefail

# AlmaLinux 8（glibc 2.28），多架构索引摘要，2026-10-28 取得
BUILD_IMAGE="almalinux:8@sha256:2d0f2eb2a83cc09dbc2eb92fade4dbe8f709628149e81aa00aec70630406603b"
GLIBC_MAX="${YUNSHU_GLIBC_MAX:-2.28}"
GLIBCXX_MAX="${YUNSHU_GLIBCXX_MAX:-3.4.25}"
SQLITE_VERSION=3.54.1
SQLITE_URL="https://www.sqlite.org/2026/sqlite-autoconf-3540100.tar.gz"
# sqlite.org 公布的是 SHA3-256（b6ced585…657d9c4），已核对一致；这里固定同一文件的 SHA-256
SQLITE_SHA256=926ef8d3eab93fbf6e06011901273eec076b25594a4e1cdadd50c987f4f6d64e

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
OUT="$REPO/artifacts/linux"
FRONTEND_DIST=""
USE_DOCKER=0
PASS=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --docker) USE_DOCKER=1 ;;
        --frontend-dist) FRONTEND_DIST="$(cd "$2" && pwd -P)"; PASS+=(--frontend-dist "${2}"); shift ;;
        --output) mkdir -p "$2"; OUT="$(cd "$2" && pwd -P)"; PASS+=(--output "${2}"); shift ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
    shift
done

log() { printf '\n==> %s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

if [ "$USE_DOCKER" -eq 1 ]; then
    command -v docker >/dev/null || die "没有 docker"
    log "在 $BUILD_IMAGE 里构建"
    exec docker run --rm -v "$REPO:/src" -w /src -e YUNSHU_GLIBC_MAX="$GLIBC_MAX" \
        -e YUNSHU_GLIBCXX_MAX="$GLIBCXX_MAX" -e SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-}" "$BUILD_IMAGE" \
        bash scripts/build-linux.sh "${PASS[@]}"
fi

# ---- 环境 --------------------------------------------------------------------
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
    almalinux:8*|rocky:8*|rhel:8*) ;;
    *) echo "注意：当前不是 EL8（$PRETTY_NAME），glibc 版本检查仍会把关" >&2 ;;
esac
# 构建工具：python3.11（AppStream）、gcc（编译 SQLite）、binutils（PyInstaller 用 objdump，基线检查用 readelf）
if [ "$(id -u)" -eq 0 ] && command -v dnf >/dev/null; then
    log "安装构建工具"
    dnf install -y -q python3.11 python3.11-pip gcc binutils findutils tar gzip >/dev/null
fi
for tool in python3.11 gcc objdump readelf; do
    command -v "$tool" >/dev/null || die "缺少 $tool"
done
PY="$(command -v python3.11)"
"$PY" -c 'import sys; assert sys.version_info[:2] == (3, 11), sys.version' || die "需要 Python 3.11"

case "$(uname -m)" in
    x86_64) ARCH=amd64 ;;
    aarch64) ARCH=arm64 ;;
    *) die "不支持的架构：$(uname -m)" ;;
esac
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$REPO/backend/app/__init__.py")"
[ -n "$VERSION" ] || die "读不到 backend/app/__init__.py 里的版本号"
NAME="yunshu-$VERSION"
TARBALL="$NAME-linux-$ARCH.tar.gz"
BUILD="$OUT/build-$ARCH"
STAGE="$BUILD/stage/$NAME"
log "云枢 $VERSION linux-$ARCH，输出 $OUT"

rm -rf "$BUILD"
mkdir -p "$BUILD" "$OUT"

# ---- SQLite（替代 EL8 的 3.26）-------------------------------------------------
log "编译 SQLite $SQLITE_VERSION"
SQLITE_DIR="$BUILD/sqlite"
mkdir -p "$SQLITE_DIR"
"$PY" - "$SQLITE_URL" "$SQLITE_SHA256" "$SQLITE_DIR/src.tar.gz" <<'PY'
import hashlib, sys, urllib.request
url, want, dst = sys.argv[1:]
data = urllib.request.urlopen(url, timeout=120).read()
got = hashlib.sha256(data).hexdigest()
if got != want:
    raise SystemExit(f"SQLite 源码校验失败：{got} != {want}")
open(dst, "wb").write(data)
PY
tar xzf "$SQLITE_DIR/src.tar.gz" -C "$SQLITE_DIR"
# 与发行版常见的编译选项一致；保留扩展加载等默认 API，保证 Python 的 _sqlite3 需要的符号都在
gcc -O2 -fPIC -shared -Wl,-soname,libsqlite3.so.0 \
    -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_COLUMN_METADATA -DSQLITE_ENABLE_UNLOCK_NOTIFY \
    -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_RTREE -DSQLITE_ENABLE_MATH_FUNCTIONS -DSQLITE_ENABLE_DBSTAT_VTAB \
    -DSQLITE_SECURE_DELETE \
    "$SQLITE_DIR/$(basename "$SQLITE_URL" .tar.gz)/sqlite3.c" -o "$SQLITE_DIR/libsqlite3.so.0" -lpthread -ldl -lm
export LD_LIBRARY_PATH="$SQLITE_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
"$PY" - <<'PY' || die "编译出的 SQLite 不可用"
import sqlite3
v = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
assert v >= (3, 35), sqlite3.sqlite_version
c = sqlite3.connect(":memory:")
c.execute("create table t (id integer primary key, s text)")
c.execute("insert into t (s) values ('queued')")
assert c.execute("update t set s = 'running' where s = 'queued' returning id").fetchall() == [(1,)]
print("SQLite", sqlite3.sqlite_version, "UPDATE ... RETURNING ok")
PY

# ---- 依赖（按哈希）-------------------------------------------------------------
log "安装锁定的依赖"
export PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
"$PY" -m venv "$BUILD/venv"
VPY="$BUILD/venv/bin/python"
"$VPY" -m pip install -q --require-hashes --no-deps -r "$REPO/backend/requirements-standard.lock"
"$PY" "$REPO/deploy/linux/lock_subset.py" "$REPO/backend/requirements-package.lock" >"$BUILD/pyinstaller.lock"
"$VPY" -m pip install -q --require-hashes --no-deps -r "$BUILD/pyinstaller.lock"
"$VPY" -m pip check

# ---- 前端 --------------------------------------------------------------------
if [ -z "$FRONTEND_DIST" ]; then
    if [ -f "$REPO/frontend/dist/index.html" ]; then
        FRONTEND_DIST="$REPO/frontend/dist"
    elif command -v npm >/dev/null; then
        log "构建前端"
        (cd "$REPO/frontend" && npm ci --no-audit --no-fund --loglevel=error && npm run build)
        FRONTEND_DIST="$REPO/frontend/dist"
    else
        die "没有前端构建结果：先在 frontend 运行 npm ci && npm run build，或用 --frontend-dist 指定"
    fi
fi
[ -f "$FRONTEND_DIST/index.html" ] || die "$FRONTEND_DIST 里没有 index.html"

# ---- PyInstaller -------------------------------------------------------------
log "PyInstaller 打包"
YUNSHU_FRONTEND_DIST="$FRONTEND_DIST" "$VPY" -m PyInstaller "$REPO/scripts/yunshu-linux.spec" \
    --noconfirm --clean --log-level WARN --distpath "$BUILD/dist" --workpath "$BUILD/work"
APP="$BUILD/dist/yunshu"
BIN="$APP/yunshu"
[ -x "$BIN" ] || die "没有生成 $BIN"

# ---- glibc / libstdc++ 基线 ---------------------------------------------------
# 逐个 ELF 文件读取动态符号版本：主程序、libpython、扩展模块、随包的 C 库（libssl、libffi、libsqlite3、
# 轮子自带的 *.libs/）都在内。随包带了 libstdc++ 时它本身定义的 GLIBCXX 版本不算需求，只看它依赖的 glibc。
log "检查符号版本（glibc ≤ $GLIBC_MAX，libstdc++ ≤ $GLIBCXX_MAX）"
find "$APP" -type f \( -name '*.so*' -o -perm -u+x \) -print0 \
    | xargs -0 -r sh -c 'for f; do [ "$(head -c 4 "$f" | od -An -c | tr -d " ")" = "177ELF" ] && echo "$f"; done; true' sh \
    >"$BUILD/elf-files.txt"
[ -s "$BUILD/elf-files.txt" ] || die "没有找到 ELF 文件"
: >"$BUILD/symbol-needs.txt"
while IFS= read -r f; do
    # objdump -T 的 UND 行是本文件需要的外部符号；带版本号的写成 GLIBC_2.x / GLIBCXX_3.4.x
    objdump -T "$f" 2>/dev/null | awk -v f="$f" '$0 ~ /\*UND\*/ { for (i = 1; i <= NF; i++) if ($i ~ /^\(?GLIBC(XX)?_[0-9.]+\)?$/) { gsub(/[()]/, "", $i); print $i, f } }' \
        >>"$BUILD/symbol-needs.txt"
done <"$BUILD/elf-files.txt"
# 没有任何文件需要 libstdc++ 时 grep 什么也找不到、返回 1；在 pipefail 下会让赋值失败、脚本无声退出
max_of() { { grep "^$1_" "$BUILD/symbol-needs.txt" || true; } | cut -d' ' -f1 | sed "s/^$1_//" | sort -uV | tail -n 1; }
over() { [ -n "$1" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n 1)" != "$2" ]; }
NEED="$(max_of GLIBC)"
NEED_CXX="$(max_of GLIBCXX)"
[ -n "$NEED" ] || die "没有读到任何 glibc 符号版本"
echo "实际需要 glibc $NEED；libstdc++ ${NEED_CXX:-（不需要）}"
if over "$NEED" "$GLIBC_MAX"; then
    awk -v max="$GLIBC_MAX" '$1 ~ /^GLIBC_/ { v = substr($1, 7); print v, $2 }' "$BUILD/symbol-needs.txt" \
        | sort -uV | awk -v max="$GLIBC_MAX" '{ split($1, a, "."); split(max, b, "."); if (a[1] > b[1] || (a[1] == b[1] && a[2] > b[2])) print "  GLIBC_" $1, $2 }'
    die "程序需要 glibc $NEED，高于支持基线 $GLIBC_MAX（上面列出的文件）"
fi
if over "$NEED_CXX" "$GLIBCXX_MAX"; then
    grep '^GLIBCXX_' "$BUILD/symbol-needs.txt" | sort -uV | tail -n 20
    die "程序需要 libstdc++ GLIBCXX_$NEED_CXX，高于支持基线 $GLIBCXX_MAX"
fi
# 随包的关键 C 库：SQLite 必须是上面编译的那份；列出 OpenSSL / libstdc++ 供核对
SQLITE_BUNDLED="$(find "$APP/_internal" -maxdepth 1 -name 'libsqlite3.so*' | head -n 1)"
[ -n "$SQLITE_BUNDLED" ] || die "包里没有 libsqlite3"
# 精简的 AlmaLinux 镜像没有 cmp（diffutils），用 sha256sum 比较
[ "$(sha256sum <"$SQLITE_BUNDLED")" = "$(sha256sum <"$SQLITE_DIR/libsqlite3.so.0")" ] || die "包里的 libsqlite3 不是编译的 $SQLITE_VERSION（$SQLITE_BUNDLED）"
find "$APP/_internal" -name 'libssl*.so*' -o -name 'libcrypto*.so*' -o -name 'libstdc++*.so*' | sed 's/^/  随包：/'
unset LD_LIBRARY_PATH
[ -f "$APP/_internal/tzdata/zoneinfo/Asia/Shanghai" ] || die "包里没有 tzdata 时区数据"
[ -f "$APP/_internal/frontend/index.html" ] || die "包里没有前端页面"
[ -f "$APP/_internal/alembic.ini" ] && [ -d "$APP/_internal/migrations" ] || die "包里没有迁移文件"

# ---- 冒烟测试 ----------------------------------------------------------------
log "冒烟测试"
got="$("$BIN" version)"
[ "$got" = "$VERSION" ] || die "yunshu version 输出 $got，应为 $VERSION"
SMOKE="$(mktemp -d)"
API_PID=""
cleanup() {
    if [ -n "$API_PID" ]; then kill "$API_PID" 2>/dev/null || true; wait "$API_PID" 2>/dev/null || true; fi
    rm -rf "$SMOKE"
}
trap cleanup EXIT
export YUNSHU_CONFIG="$SMOKE/none.env" YUNSHU_DATA_DIR="$SMOKE/data" \
    YUNSHU_MASTER_KEY_FILE="$SMOKE/keys/master.key" YUNSHU_LOG_DIR="$SMOKE/logs" \
    YUNSHU_SETUP_WIZARD=0 YUNSHU_EMBEDDED_WORKER=1 YUNSHU_BACKUP_INTERVAL_HOURS=0 \
    YUNSHU_BIND=127.0.0.1 YUNSHU_PORT=18080
"$BIN" api >"$SMOKE/api.out" 2>&1 &
API_PID=$!
"$VPY" - <<'PY' || { cat "$SMOKE/api.out" >&2; die "API 冒烟测试失败"; }
import json, time, urllib.error, urllib.request
base = "http://127.0.0.1:18080"
deadline = time.monotonic() + 180
while True:
    try:
        with urllib.request.urlopen(base + "/api/ready", timeout=5) as r:
            assert r.status == 200, r.status
            break
    except (urllib.error.URLError, ConnectionError, OSError):
        if time.monotonic() > deadline:
            raise SystemExit("/api/ready 没有在 180 秒内返回 200")
        time.sleep(2)
with urllib.request.urlopen(base + "/", timeout=10) as r:
    assert b"<!doctype html" in r.read(2048).lower(), "首页不是前端页面"
req = urllib.request.Request(base + "/api/auth/login", method="POST", headers={"Content-Type": "application/json"},
                             data=json.dumps({"username": "admin", "password": "admin123"}).encode())
with urllib.request.urlopen(req, timeout=10) as r:
    body = json.load(r)
assert body["user"]["require_password_change"] is True, body
print("API 就绪，首次登录要求修改密码")
PY
# 运行中的进程实际加载的是包里的 SQLite / Python 库，而不是系统的（EL8 系统里就有 3.26 版的 libsqlite3）
grep -E 'libsqlite3|libpython3' "/proc/$API_PID/maps" | awk '{print $6}' | sort -u >"$SMOKE/libs.txt"
cat "$SMOKE/libs.txt"
grep -q "^$APP/_internal/libsqlite3" "$SMOKE/libs.txt" || die "API 没有加载包里的 libsqlite3"
! grep -v "^$APP/" "$SMOKE/libs.txt" | grep -q . || die "API 加载了包外的 libsqlite3 / libpython"
for i in $(seq 1 30); do
    if "$BIN" worker --check; then break; fi
    [ "$i" -lt 30 ] || { cat "$SMOKE/api.out" >&2; die "内嵌 Worker 没有心跳"; }
    sleep 2
done
"$BIN" backup create
ls "$SMOKE/data/backups/"*.db >/dev/null || die "备份文件没有生成"
[ -s "$SMOKE/keys/master.key" ] || die "主密钥没有生成"
[ -s "$SMOKE/logs/yunshu-api.log" ] || die "YUNSHU_LOG_DIR 没有日志文件"
kill "$API_PID"
wait "$API_PID" 2>/dev/null || true
API_PID=""
"$BIN" migrate
unset YUNSHU_CONFIG YUNSHU_DATA_DIR YUNSHU_MASTER_KEY_FILE YUNSHU_LOG_DIR YUNSHU_SETUP_WIZARD \
    YUNSHU_EMBEDDED_WORKER YUNSHU_BACKUP_INTERVAL_HOURS YUNSHU_BIND YUNSHU_PORT

# ---- 组装压缩包 --------------------------------------------------------------
log "组装 $TARBALL"
mkdir -p "$STAGE"
cp -a "$APP" "$STAGE/yunshu"
install -m 0755 "$REPO/deploy/linux/install.sh" "$REPO/deploy/linux/uninstall.sh" "$STAGE/"
install -d -m 0755 "$STAGE/systemd"
install -m 0644 "$REPO/deploy/linux/systemd/"*.service "$STAGE/systemd/"
install -m 0644 "$REPO/deploy/linux/yunshu.env.example" "$REPO/deploy/linux/README.txt" "$REPO/LICENSE" "$STAGE/"
printf '%s\n' "$VERSION" >"$STAGE/VERSION"
printf '%s\n' "$ARCH" >"$STAGE/ARCH"
printf '%s\n' "$NEED" >"$STAGE/GLIBC_REQUIRED"

# 第三方许可：Python 包（含 PyInstaller 引导程序）、随包的 EL8 系统库与 SQLite、前端依赖
"$VPY" - "$STAGE" "$REPO" "$SQLITE_VERSION" <<'PY'
import importlib.metadata, json, shutil, subprocess, sys
from pathlib import Path
stage, repo = Path(sys.argv[1]), Path(sys.argv[2])
lic = stage / "licenses"
notes = [
    "云枢运维平台 Linux 压缩包的第三方组件清单 / Third-party components in the Yunshu Linux package.",
    "许可文本见 licenses/ / License texts are under licenses/.",
    "Python 运行时 / Python runtime: PSF License, https://docs.python.org/3/license.html",
    "PyInstaller 引导程序 / bootloader: GPL-2.0-or-later with Bootloader exception; the application is not GPL-licensed because of it.",
    "elkjs 使用 EPL-2.0（双许可选项之一），原样分发，源码 / source: https://github.com/kieler/elkjs",
    "",
]
for name in ("LICENSE.txt", "LICENSE"):
    for p in (Path(sys.base_prefix) / "lib" / "python3.11" / name, Path("/usr/share/licenses/python3.11") / name):
        if p.is_file():
            break
    if p.is_file():
        (lic / "python-runtime").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, lic / "python-runtime" / "LICENSE.txt")
        break
for dist in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
    name = dist.metadata["Name"]
    if name.lower() in ("pip",):
        continue
    meta = dist.metadata
    notes.append(f"Python: {name} {dist.version}; {meta.get('License-Expression') or meta.get('License') or 'see license files'}; {meta.get('Home-page') or ''}".rstrip("; "))
    for item in dist.files or []:
        if any(part.lower().startswith(("license", "licence", "copying", "notice", "authors")) for part in item.parts):
            src = Path(dist.locate_file(item))
            if src.is_file():
                dst = lic / "python" / name / item.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
# 随包复制的系统库（libpython3.11、libssl、libffi 等，来自 EL8 的 RPM）：按 rpm 归属复制许可文本
notes.append(f"SQLite {sys.argv[3]}: public domain, compiled from https://www.sqlite.org/ source")
pkgs = set()
for so in (stage / "yunshu" / "_internal").glob("*.so*"):
    for base in ("/usr/lib64", "/lib64", "/usr/lib"):
        cand = Path(base) / so.name
        if not cand.exists():
            continue
        try:
            out = subprocess.run(["rpm", "-qf", "--qf", "%{NAME}\n", str(cand)], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            break
        if out.returncode == 0:
            pkgs.update(line.strip() for line in out.stdout.splitlines() if line.strip())
        break
for pkg in sorted(pkgs):
    try:
        info = subprocess.run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}; %{LICENSE}", pkg], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        info = ""
    notes.append(f"RPM: {pkg} {info} (system library bundled by PyInstaller)")
    for folder in (Path("/usr/share/licenses") / pkg, Path("/usr/share/doc") / pkg):
        if folder.is_dir():
            for src in folder.iterdir():
                if src.is_file() and src.name.lower().startswith(("license", "licence", "copying", "copyright", "notice")):
                    dst = lic / "rpm" / pkg / src.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
node_modules = repo / "frontend" / "node_modules"
lock = repo / "frontend" / "package-lock.json"
if lock.is_file():
    packages = json.loads(lock.read_text(encoding="utf-8")).get("packages", {})
    for key, info in sorted(packages.items()):
        if not key.startswith("node_modules/") or info.get("dev"):
            continue
        pname = key.split("node_modules/")[-1]
        notes.append(f"Node: {pname} {info.get('version', '')}; {info.get('license', 'see license files')}")
        folder = node_modules / pname
        if folder.is_dir():
            for src in folder.iterdir():
                if src.is_file() and src.name.lower().startswith(("license", "licence", "copying", "notice")):
                    dst = lic / "node" / pname.replace("/", "_") / src.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
(stage / "NOTICES.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")
PY

find "$STAGE" -type d -exec chmod 0755 {} +
chmod -R go-w "$STAGE"
EPOCH="${SOURCE_DATE_EPOCH:-$(date +%s)}"
rm -f "$OUT/$TARBALL" "$OUT/$TARBALL.sha256"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$EPOCH" -C "$BUILD/stage" -cf - "$NAME" \
    | gzip -n -9 >"$OUT/$TARBALL"
(cd "$OUT" && sha256sum "$TARBALL" >"$TARBALL.sha256")
log "完成"
cat "$OUT/$TARBALL.sha256"
du -h "$OUT/$TARBALL"
