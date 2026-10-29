#!/bin/bash
# CI：在目标发行版容器里以 root 测试 Linux 压缩包的安装、运行、备份、卸载与重装（不随安装包分发）。
#
#   install-test.sh direct  <tarball>   没有 systemd 的普通容器：install.sh --no-systemd，以 yunshu 用户直接运行
#   install-test.sh systemd <tarball>   systemd 为 1 号进程的容器：install.sh / --start、单元、升级、回滚、卸载
#
# 只用伪造数据与应用自带的初始账号（admin / admin123 → fake-ci-Passw0rd），不连接任何设备。

set -euo pipefail

MODE="${1:?mode: direct|systemd}"
TARBALL="$(readlink -f "${2:?tarball}")"
BASE=http://127.0.0.1:8080
NEW_PASSWORD=fake-ci-Passw0rd
JAR=/tmp/yunshu-ci.cookies
CSRF=""

step() { printf '\n----- [%s] %s\n' "$MODE" "$*"; }
fail() { printf '失败 / FAIL: %s\n' "$*" >&2; diagnostics; exit 1; }
diagnostics() {
    set +e
    echo "===== diagnostics" >&2
    ls -la /opt/yunshu /var/lib/yunshu /etc/yunshu /var/log/yunshu >&2 2>/dev/null
    tail -n 80 /var/log/yunshu/*.log >&2 2>/dev/null
    tail -n 80 /tmp/yunshu-api.out >&2 2>/dev/null
    if [ "$MODE" = systemd ]; then
        systemctl --no-pager status yunshu-api yunshu-worker yunshu-migrate >&2
        journalctl --no-pager -n 120 -u yunshu-api -u yunshu-worker -u yunshu-migrate >&2
    fi
}

prereqs() {
    local missing=()
    local cmd
    for cmd in tar gzip curl useradd runuser getconf sha256sum find; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    [ "${#missing[@]}" -eq 0 ] && return 0
    step "安装测试所需工具：${missing[*]}"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
            ca-certificates curl tar gzip passwd util-linux findutils >/dev/null
    elif command -v dnf >/dev/null 2>&1; then
        # 按文件安装：RHEL 系与 openEuler 的包名不同（shadow-utils / shadow），文件路径相同
        dnf install -y -q /usr/bin/tar /usr/bin/gzip /usr/sbin/useradd /usr/sbin/runuser /usr/bin/find >/dev/null
        command -v curl >/dev/null 2>&1 || dnf install -y -q /usr/bin/curl >/dev/null
    elif command -v yum >/dev/null 2>&1; then
        yum install -y -q /usr/bin/tar /usr/bin/gzip /usr/sbin/useradd /usr/sbin/runuser /usr/bin/find /usr/bin/curl >/dev/null
    else
        fail "不认识的包管理器"
    fi
}

# ---- HTTP ---------------------------------------------------------------------
wait_ready() {
    local i
    for i in $(seq 1 120); do
        if [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/ready" || true)" = 200 ]; then return 0; fi
        sleep 2
    done
    fail "/api/ready 没有在 240 秒内返回 200"
}

LOGIN_BODY=/tmp/yunshu-login.json
login() {  # login <密码>：成功时保存 Cookie 与 CSRF 令牌，响应 JSON 写到 $LOGIN_BODY
    # 不往标准输出写：调用方若用管道（login … | grep），login 在子 shell 里运行，设置的 CSRF 变量就丢了
    rm -f "$JAR" "$LOGIN_BODY"
    curl -sS -f -c "$JAR" -o "$LOGIN_BODY" -H 'Content-Type: application/json' \
        -d "{\"username\":\"admin\",\"password\":\"$1\"}" "$BASE/api/auth/login" || return 1
    CSRF="$(grep -o '"csrf_token":"[^"]*"' "$LOGIN_BODY" | cut -d'"' -f4)"
    [ -n "$CSRF" ]
}

api() {  # api <方法> <路径> [JSON]
    if [ -n "${3:-}" ]; then
        curl -sS -f -b "$JAR" -X "$1" -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRF" -d "$3" "$BASE$2"
    else
        curl -sS -f -b "$JAR" -X "$1" -H "X-CSRF-Token: $CSRF" "$BASE$2"
    fi
}

first_use() {
    step "首次登录要求改密码；改密码、建一台伪造资产"
    { login admin123 && grep -q '"require_password_change":true' "$LOGIN_BODY"; } || fail "首次登录没有要求修改密码"
    api POST /api/auth/password "{\"old_password\":\"admin123\",\"new_password\":\"$NEW_PASSWORD\"}" >/dev/null || fail "修改密码失败"
    # 改密码会结束当前会话，用新密码重新登录
    login "$NEW_PASSWORD" || fail "改密码后用新密码登录失败"
    api POST /api/assets '{"name":"ci-fake-server-1","form":"physical","role":"server","vendor":"Example","notes":"CI fake"}' >/dev/null \
        || fail "创建资产失败"
    assert_data
}

assert_data() {  # 数据还在：新密码能登录、旧密码不能、资产数量为 1
    step "检查数据：新密码登录、资产数量"
    { login "$NEW_PASSWORD" && grep -q '"require_password_change":false' "$LOGIN_BODY"; } || fail "新密码登录失败（数据没有保留？）"
    api GET /api/assets | grep -q '"total":1' || fail "资产数量不是 1"
    if login admin123 >/dev/null 2>&1; then fail "初始密码仍然可以登录"; fi
    login "$NEW_PASSWORD" || fail "新密码登录失败"
}

# ---- 检查 ---------------------------------------------------------------------
perm_is() {  # perm_is <路径> <属主:属组> <权限>
    local got
    got="$(stat -c '%U:%G %a' "$1")"
    [ "$got" = "$2 $3" ] || fail "$1 应为 $2 $3，实际 $got"
}

assert_layout() {
    step "检查安装布局与权限"
    [ "$(readlink -f /opt/yunshu/current)" = "/opt/yunshu/$1" ] || fail "current 没有指向 $1"
    [ -x /usr/local/bin/yunshu ] || fail "没有 /usr/local/bin/yunshu"
    [ "$(yunshu version)" = "$VERSION" ] || fail "yunshu version 不正确"
    getent passwd yunshu | grep -Eq ':(/usr/sbin/nologin|/sbin/nologin|/bin/false)$' || fail "yunshu 用户不应有登录 shell"
    perm_is /var/lib/yunshu yunshu:yunshu 750
    perm_is /etc/yunshu root:yunshu 750
    perm_is /etc/yunshu/keys yunshu:yunshu 700
    perm_is /var/log/yunshu yunshu:yunshu 750
    perm_is /etc/yunshu/yunshu.env root:yunshu 640
    grep -q '^YUNSHU_MASTER_KEY_FILE=/etc/yunshu/keys/master.key$' /etc/yunshu/yunshu.env || fail "yunshu.env 没有设置主密钥路径"
}

worker_check() {
    local i
    step "Worker 心跳（yunshu worker --check，从 root 的家目录运行包装命令）"
    for i in $(seq 1 45); do
        if (cd /root && yunshu worker --check); then return 0; fi
        sleep 2
    done
    fail "Worker 没有心跳"
}

backup_create() {
    step "yunshu backup create"
    (cd /root && yunshu backup create) || fail "备份失败"
    BACKUP="$(ls -t /var/lib/yunshu/backups/*.db | head -n 1)"
    [ "$(stat -c %U "$BACKUP")" = yunshu ] || fail "备份文件属主应为 yunshu"
    [ ! -e /var/lib/yunshu/master.key ] || fail "数据目录里不应有主密钥"
}

assert_kept() {
    step "卸载后：程序已删除，数据、配置、主密钥保留"
    [ ! -e /opt/yunshu ] || fail "/opt/yunshu 应已删除"
    [ ! -e /usr/local/bin/yunshu ] || fail "/usr/local/bin/yunshu 应已删除"
    [ -s /var/lib/yunshu/yunshu.db ] || fail "数据库没有保留"
    [ -s /etc/yunshu/yunshu.env ] || fail "配置没有保留"
    [ "$(sha256sum /etc/yunshu/keys/master.key | cut -d' ' -f1)" = "$KEY_SHA" ] || fail "主密钥被改动"
    [ -e "$BACKUP" ] || fail "备份文件没有保留"
    getent passwd yunshu >/dev/null || fail "yunshu 用户应保留"
}

purge_tests() {
    step "uninstall.sh --purge：输入错误时取消"
    if echo nope | /opt/yunshu/current/uninstall.sh --purge; then fail "确认错误时不应继续"; fi
    [ -s /var/lib/yunshu/yunshu.db ] && [ -x /opt/yunshu/current/yunshu/yunshu ] || fail "取消后不应删除任何内容"
    step "uninstall.sh --purge：输入 PURGE"
    echo PURGE | /opt/yunshu/current/uninstall.sh --purge
    for p in /opt/yunshu /var/lib/yunshu /etc/yunshu /var/log/yunshu /usr/local/bin/yunshu; do
        [ ! -e "$p" ] || fail "$p 应已删除"
    done
    if getent passwd yunshu >/dev/null; then fail "yunshu 用户应已删除"; fi
}

yunshu_pids() {  # 程序位于 /opt/yunshu 下的进程号
    # 容器里默认没有 CAP_SYS_PTRACE：root 读不到别的用户进程的 exe 链接，改看对所有人可读的 cmdline
    local p cmd pids=""
    for p in /proc/[0-9]*; do
        cmd="$(tr '\0' ' ' <"$p/cmdline" 2>/dev/null || true)"
        case "$cmd" in /opt/yunshu/*) pids="$pids ${p#/proc/}" ;; esac
    done
    printf '%s' "${pids# }"
}

assert_bundled_libs() {
    # 主机自带的 SQLite（EL8 / openEuler 20.03 是 3.26，不支持 UPDATE … RETURNING）、libpython、OpenSSL 都不能被用上。
    # 包里的 Python 运行时把 SQLite、OpenSSL 静态编译进了 libpython，所以只要求加载包里的 libpython，且不加载任何包外的这些库
    local pid found=0
    step "运行中的进程只加载包里的 libpython，不加载主机的 libsqlite3 / libssl"
    for pid in $(yunshu_pids); do
        found=1
        # maps 同样受 ptrace 权限限制：以进程自己的用户 yunshu 去读
        runuser -u yunshu -- cat "/proc/$pid/maps" \
            | awk '$6 ~ /(libsqlite3|libpython3|libssl|libcrypto)/ {print $6}' | sort -u >/tmp/yunshu-libs.txt
        cat /tmp/yunshu-libs.txt
        grep -q '^/opt/yunshu/.*/libpython3' /tmp/yunshu-libs.txt || fail "没有加载包里的 libpython"
        if grep -v '^/opt/yunshu/' /tmp/yunshu-libs.txt | grep -q .; then fail "加载了包外的库（见上）"; fi
    done
    [ "$found" -eq 1 ] || fail "没有找到云枢进程"
}

# ---- 无 systemd：直接运行 ---------------------------------------------------------
direct_start() {
    (cd /root && nohup yunshu api >/tmp/yunshu-api.out 2>&1 &)
    wait_ready
}

direct_stop() {
    local pids i
    pids="$(yunshu_pids)"
    [ -n "$pids" ] || return 0
    kill $pids 2>/dev/null || true
    for i in $(seq 1 60); do
        kill -0 $pids 2>/dev/null || return 0
        sleep 1
    done
    fail "进程没有在 60 秒内退出：$pids"
}

direct_api_user() {
    # 不读 /proc/<pid>/exe：容器里默认没有 CAP_SYS_PTRACE，root 也读不到别的用户进程的 exe 链接（为空）。
    # cmdline 与 status 对所有人可读
    local p cmd uid
    for p in /proc/[0-9]*; do
        cmd="$(tr '\0' ' ' <"$p/cmdline" 2>/dev/null || true)"
        case "$cmd" in
            /opt/yunshu/*" api"*)
                uid="$(awk '/^Uid:/{print $2}' "$p/status")"
                [ "$uid" = "$(id -u yunshu)" ] || fail "API 进程应以 yunshu 用户运行（uid $uid）"
                return 0 ;;
        esac
    done
    fail "没有找到 API 进程"
}

run_direct() {
    step "install.sh --no-systemd"
    "$PKG/install.sh" --no-systemd
    assert_layout "$VERSION"
    step "再运行一次（幂等）"
    "$PKG/install.sh" --no-systemd >/dev/null
    assert_layout "$VERSION"
    step "启动 API（yunshu api，root 运行时自动切换到 yunshu 用户）"
    direct_start
    direct_api_user
    assert_bundled_libs
    worker_check
    first_use
    backup_create
    [ -s /var/log/yunshu/yunshu-api.log ] || fail "YUNSHU_LOG_DIR 没有日志"
    KEY_SHA="$(sha256sum /etc/yunshu/keys/master.key | cut -d' ' -f1)"
    step "运行中卸载应被拒绝"
    if /opt/yunshu/current/uninstall.sh; then fail "进程运行中不应允许卸载"; fi
    direct_stop
    step "uninstall.sh（保留数据）"
    /opt/yunshu/current/uninstall.sh
    assert_kept
    step "重新安装"
    "$PKG/install.sh" --no-systemd
    assert_layout "$VERSION"
    direct_start
    assert_data
    direct_stop
    purge_tests
}

# ---- systemd ----------------------------------------------------------------
main_pid() { systemctl show -p MainPID --value "$1"; }

assert_unit_hardening() {
    step "检查单元设置"
    local u
    for u in yunshu-api yunshu-worker yunshu-migrate; do
        [ "$(systemctl show -p User --value "$u")" = yunshu ] || fail "$u 的 User 不是 yunshu"
        [ "$(systemctl show -p NoNewPrivileges --value "$u")" = yes ] || fail "$u 没有 NoNewPrivileges"
        [ "$(systemctl show -p ProtectSystem --value "$u")" = strict ] || fail "$u 没有 ProtectSystem=strict"
        [ "$(systemctl show -p PrivateTmp --value "$u")" = yes ] || fail "$u 没有 PrivateTmp"
    done
}

api_runs_as_yunshu() {
    local pid uid
    pid="$(main_pid yunshu-api)"
    [ "$pid" != 0 ] || fail "yunshu-api 没有主进程"
    uid="$(awk '/^Uid:/{print $2}' "/proc/$pid/status")"
    [ "$uid" = "$(id -u yunshu)" ] || fail "yunshu-api 应以 yunshu 用户运行"
}

run_systemd() {
    step "等待 systemd 启动完成"
    # --wait 需要 systemd 240+；EL8 是 239，工作流里已轮询等待过
    systemctl is-system-running --wait 2>/dev/null || systemctl is-system-running || true

    step "install.sh（不带 --start：只安装，不启动）"
    "$PKG/install.sh"
    assert_layout "$VERSION"
    systemctl cat yunshu-api.service >/dev/null || fail "单元没有安装"
    if systemctl is-active --quiet yunshu-api; then fail "不带 --start 时不应启动"; fi
    [ "$(systemctl is-enabled yunshu-api || true)" = disabled ] || fail "不带 --start 时不应启用"
    assert_unit_hardening

    step "install.sh --start"
    "$PKG/install.sh" --start
    systemctl is-active --quiet yunshu-api || fail "yunshu-api 没有运行"
    systemctl is-enabled --quiet yunshu-api || fail "yunshu-api 没有启用"
    if systemctl is-enabled --quiet yunshu-worker; then fail "轻量版不应启用独立 Worker"; fi
    api_runs_as_yunshu
    wait_ready
    assert_bundled_libs
    worker_check
    first_use
    backup_create
    KEY_SHA="$(sha256sum /etc/yunshu/keys/master.key | cut -d' ' -f1)"

    step "yunshu-migrate.service（一次性单元）"
    systemctl start yunshu-migrate.service || fail "yunshu-migrate 失败"
    [ "$(systemctl show -p Result --value yunshu-migrate)" = success ] || fail "yunshu-migrate 结果不是 success"

    step "yunshu-worker.service（停 API 后单独运行 Worker）"
    systemctl stop yunshu-api
    systemctl start yunshu-worker
    worker_check
    systemctl stop yunshu-worker
    systemctl start yunshu-api
    wait_ready

    step "升级：安装一个更高的版本号，运行中的服务应自动重启"
    local next="$VERSION+ci2" pid_before
    rm -rf /tmp/yunshu-next
    cp -a "$PKG" /tmp/yunshu-next
    echo "$next" >/tmp/yunshu-next/VERSION
    pid_before="$(main_pid yunshu-api)"
    /tmp/yunshu-next/install.sh
    [ "$(readlink -f /opt/yunshu/current)" = "/opt/yunshu/$next" ] || fail "升级后 current 没有切换"
    [ -d "/opt/yunshu/$VERSION" ] || fail "旧版本目录应保留"
    [ "$(main_pid yunshu-api)" != "$pid_before" ] || fail "升级后服务没有重启"
    wait_ready
    assert_data

    step "回滚：停服务、运行旧版本的 install.sh、用旧版本恢复备份、启动"
    systemctl stop yunshu-api
    "/opt/yunshu/$VERSION/install.sh"
    [ "$(readlink -f /opt/yunshu/current)" = "/opt/yunshu/$VERSION" ] || fail "回滚后 current 没有指回旧版本"
    if systemctl is-active --quiet yunshu-api; then fail "服务停止时回滚不应自动启动"; fi
    (cd /root && yunshu backup restore "$BACKUP" --yes) || fail "恢复备份失败"
    systemctl start yunshu-api
    wait_ready
    assert_data

    step "uninstall.sh（保留数据）"
    /opt/yunshu/current/uninstall.sh
    assert_kept
    for u in yunshu-api yunshu-worker yunshu-migrate; do
        [ ! -e "/etc/systemd/system/$u.service" ] || fail "$u 单元应已删除"
    done
    step "重新安装并启动"
    "$PKG/install.sh" --start
    wait_ready
    assert_data
    purge_tests
}

main() {
    prereqs
    step "校验并解压 $(basename "$TARBALL")"
    (cd "$(dirname "$TARBALL")" && sha256sum -c "$(basename "$TARBALL").sha256")
    rm -rf /tmp/yunshu-extract
    mkdir -p /tmp/yunshu-extract
    tar xzf "$TARBALL" -C /tmp/yunshu-extract
    PKG="$(echo /tmp/yunshu-extract/yunshu-*)"
    VERSION="$(cat "$PKG/VERSION")"
    echo "glibc 要求 $(cat "$PKG/GLIBC_REQUIRED")，本机 $(getconf GNU_LIBC_VERSION)"
    for f in install.sh uninstall.sh README.txt LICENSE NOTICES.txt yunshu.env.example \
        systemd/yunshu-api.service systemd/yunshu-worker.service systemd/yunshu-migrate.service; do
        [ -e "$PKG/$f" ] || fail "压缩包缺少 $f"
    done
    case "$MODE" in
        direct) run_direct ;;
        systemd) run_systemd ;;
        *) fail "未知模式 $MODE" ;;
    esac
    printf '\n通过 / PASS: %s %s\n' "$MODE" "$VERSION"
}

main
