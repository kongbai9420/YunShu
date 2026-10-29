#!/bin/bash
# 云枢运维平台 Linux 安装 / 升级脚本（在解压后的 yunshu-<版本>/ 目录里以 root 运行）。
# Yunshu ops platform installer / upgrader for Linux. Run as root from the extracted yunshu-<version>/ folder.
#
#   sudo ./install.sh             安装或升级；之前在运行的服务会自动重启，否则只打印下一步
#   sudo ./install.sh --start     并启用、启动 yunshu-api（标准版时连同 yunshu-migrate、yunshu-worker）
#   sudo ./install.sh --no-systemd  不安装 systemd 单元（容器、chroot 等没有 systemd 的环境）
#
# 可重复运行（幂等）：已存在的用户、目录、/etc/yunshu/yunshu.env、主密钥都不会被覆盖。
# 不修改防火墙，只提示需要开放的端口。

set -euo pipefail

PREFIX=/opt/yunshu
DATA_DIR=/var/lib/yunshu
CONF_DIR=/etc/yunshu
KEY_DIR=/etc/yunshu/keys
LOG_DIR=/var/log/yunshu
UNIT_DIR=/etc/systemd/system
BIN_LINK=/usr/local/bin/yunshu
SVC_USER=yunshu
UNITS=(yunshu-api.service yunshu-worker.service yunshu-migrate.service)
WRAPPER_MARK="# yunshu-cli-wrapper"

say() { printf '%s\n' "$*"; }
warn() { printf '注意 / Warning: %s\n' "$*" >&2; }
die() { printf '错误 / Error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法 / Usage: sudo ./install.sh [--start] [--no-systemd]

  --start       安装后启用并启动服务 / enable and start the services after installing
  --no-systemd  不安装 systemd 单元 / do not install systemd units (containers, chroots)
  -h, --help    显示本帮助 / show this help
EOF
}

version_ge() {  # $1 >= $2（按版本号比较）
    [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n 1)" = "$2" ]
}

env_get() {  # 读取 yunshu.env 里最后一次赋值（与 yunshu 命令的解析规则一致：去掉两端一对引号）
    [ -r "$CONF_DIR/yunshu.env" ] || return 0
    sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?$1[[:space:]]*=[[:space:]]*//p" "$CONF_DIR/yunshu.env" \
        | tail -n 1 | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

is_postgres() {
    case "$(env_get YUNSHU_DATABASE_URL)" in
        postgres*) return 0 ;;
        *) return 1 ;;
    esac
}

http_status() {  # 只用 bash 的 /dev/tcp，不依赖 curl：http_status <主机> <端口> <路径>
    local code=""
    exec 3<>"/dev/tcp/$1/$2" || return 1
    printf 'GET %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n' "$3" "$1" >&3
    read -r -t 10 _ code _ <&3 || true
    exec 3<&- 3>&-
    printf '%s' "$code"
}

wait_ready() {
    local bind port host i
    bind="$(env_get YUNSHU_BIND)"
    port="$(env_get YUNSHU_PORT)"
    port="${port:-8080}"
    case "$bind" in ""|0.0.0.0|::|"[::]"|127.0.0.1) host=127.0.0.1 ;; *) host="$bind" ;; esac
    for i in $(seq 1 90); do
        if [ "$(http_status "$host" "$port" /api/ready 2>/dev/null || true)" = 200 ]; then
            say "就绪 / Ready: http://$host:$port/api/ready"
            return 0
        fi
        sleep 2
    done
    return 1
}

check_arch() {
    local want have
    want="$(cat "$PKG_DIR/ARCH")"
    case "$(uname -m)" in
        x86_64|amd64) have=amd64 ;;
        aarch64|arm64) have=arm64 ;;
        *) have="$(uname -m)" ;;
    esac
    [ "$want" = "$have" ] || die "安装包是 $want，本机是 $have / package is $want, host is $have"
}

check_glibc() {
    local need have
    need="$(cat "$PKG_DIR/GLIBC_REQUIRED" 2>/dev/null || true)"
    have="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
    [ -n "$have" ] || die "没有检测到 glibc（例如 Alpine 的 musl 不受支持），请改用 Docker 部署 / glibc not found; use the Docker image"
    if [ -n "$need" ] && ! version_ge "$have" "$need"; then
        die "本机 glibc $have 低于程序要求的 $need，请改用 Docker 部署或升级系统 / host glibc $have is older than required $need"
    fi
}

ensure_tools() {
    local t
    for t in getent groupadd useradd install sed awk sort mktemp; do
        command -v "$t" >/dev/null 2>&1 || die "缺少命令 $t（Debian/Ubuntu：passwd、coreutils；RHEL 系/openEuler/麒麟：shadow-utils、coreutils）/ missing command: $t"
    done
}

ensure_user() {
    local shell=/bin/false p
    for p in /usr/sbin/nologin /sbin/nologin; do
        if [ -x "$p" ]; then shell="$p"; break; fi
    done
    if ! getent group "$SVC_USER" >/dev/null; then
        groupadd --system "$SVC_USER"
        say "已创建系统组 / created group: $SVC_USER"
    fi
    if ! getent passwd "$SVC_USER" >/dev/null; then
        useradd --system --gid "$SVC_USER" --home-dir "$DATA_DIR" --no-create-home \
            --shell "$shell" --comment "Yunshu ops platform" "$SVC_USER"
        say "已创建系统用户 / created user: $SVC_USER (shell $shell)"
    fi
}

ensure_dir() {  # ensure_dir <路径> <属主:属组> <权限>
    mkdir -p "$1"
    chown "$2" "$1"
    chmod "$3" "$1"
}

ensure_dirs() {
    ensure_dir "$DATA_DIR" "$SVC_USER:$SVC_USER" 0750
    ensure_dir "$CONF_DIR" "root:$SVC_USER" 0750
    ensure_dir "$KEY_DIR" "$SVC_USER:$SVC_USER" 0700
    ensure_dir "$LOG_DIR" "$SVC_USER:$SVC_USER" 0750
    # 恢复或手工复制进来的主密钥：属主改为服务用户，只允许它读写
    local f
    for f in "$KEY_DIR"/*; do
        [ -f "$f" ] || continue
        chown "$SVC_USER:$SVC_USER" "$f"
        chmod 0600 "$f"
    done
}

install_program() {
    TARGET="$PREFIX/$VERSION"
    OLD_COPY=""
    mkdir -p "$PREFIX"
    chmod 0755 "$PREFIX"
    if [ -d "$TARGET" ] && [ "$(cd "$TARGET" && pwd -P)" = "$PKG_DIR" ]; then
        say "从已安装的目录运行，不再复制 / running from the installed folder, nothing to copy"
        return 0
    fi
    local tmp
    tmp="$(mktemp -d "$PREFIX/.install.XXXXXX")"
    cp -a "$PKG_DIR/." "$tmp/"
    chown -R root:root "$tmp"
    chmod -R go-w "$tmp"
    chmod 0755 "$tmp"
    if [ -e "$TARGET" ]; then
        OLD_COPY="$PREFIX/.replaced.$VERSION.$$"
        mv "$TARGET" "$OLD_COPY"
    fi
    mv "$tmp" "$TARGET"
    say "程序 / program: $TARGET"
}

switch_current() {  # 原子地切换 /opt/yunshu/current
    ln -sfn "$1" "$PREFIX/.current.tmp"
    mv -Tf "$PREFIX/.current.tmp" "$PREFIX/current"
}

write_config() {
    if [ ! -e "$CONF_DIR/yunshu.env" ]; then
        install -m 0640 -o root -g "$SVC_USER" "$PKG_DIR/yunshu.env.example" "$CONF_DIR/yunshu.env"
        say "已写入默认配置 / wrote default config: $CONF_DIR/yunshu.env"
    else
        say "保留现有配置 / keeping existing config: $CONF_DIR/yunshu.env"
    fi
    install -m 0640 -o root -g "$SVC_USER" "$PKG_DIR/yunshu.env.example" "$CONF_DIR/yunshu.env.example"
}

write_wrapper() {
    # /usr/local/bin/yunshu：root 运行时自动切换到 yunshu 用户，避免备份、迁移、主密钥文件变成 root 所有
    local tmp
    if [ -e "$BIN_LINK" ] && [ ! -L "$BIN_LINK" ] && ! grep -q "$WRAPPER_MARK" "$BIN_LINK" 2>/dev/null; then
        die "$BIN_LINK 已存在且不是云枢安装的，请先移走 / $BIN_LINK exists and was not installed by Yunshu"
    fi
    mkdir -p "$(dirname "$BIN_LINK")"
    tmp="$(mktemp "$BIN_LINK.XXXXXX")"
    cat >"$tmp" <<EOF
#!/bin/sh
$WRAPPER_MARK（由 install.sh 生成，卸载时删除 / generated by install.sh）
# root 运行时以 $SVC_USER 用户执行，配置读 $CONF_DIR/yunshu.env。
PROG=$PREFIX/current/yunshu/yunshu
[ -x "\$PROG" ] || { echo "云枢程序不存在 / not installed: \$PROG" >&2; exit 1; }
if [ "\$(id -u)" = 0 ]; then
    if command -v runuser >/dev/null 2>&1; then exec runuser -u $SVC_USER -- "\$PROG" "\$@"; fi
    if command -v setpriv >/dev/null 2>&1; then exec setpriv --reuid=$SVC_USER --regid=$SVC_USER --init-groups -- "\$PROG" "\$@"; fi
    exec su -s /bin/sh $SVC_USER -c 'exec "\$0" "\$@"' "\$PROG" "\$@"
fi
if [ "\$(id -un)" != $SVC_USER ]; then
    case "\${1:-}" in
        version|help|-h|--help) ;;
        *) echo "请用 sudo 运行（会自动切换到 $SVC_USER 用户）/ run with sudo" >&2; exit 1 ;;
    esac
fi
exec "\$PROG" "\$@"
EOF
    chmod 0755 "$tmp"
    mv -f "$tmp" "$BIN_LINK"
}

label_selinux() {
    command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled || return 0
    # /opt 下的文件默认是 usr_t；把主程序标成 bin_t，systemd 启动时按普通服务程序处理
    if command -v semanage >/dev/null 2>&1; then
        semanage fcontext -a -t bin_t "$PREFIX/[^/]+/yunshu/yunshu" 2>/dev/null \
            || semanage fcontext -m -t bin_t "$PREFIX/[^/]+/yunshu/yunshu" 2>/dev/null || true
        restorecon -R "$PREFIX" 2>/dev/null || true
    else
        chcon -t bin_t "$TARGET/yunshu/yunshu" 2>/dev/null || warn "无法设置 SELinux 标签，见部署文档的 SELinux 一节"
    fi
    restorecon -R "$DATA_DIR" "$CONF_DIR" "$LOG_DIR" 2>/dev/null || true
    say "SELinux 已启用：已为主程序设置 bin_t 标签 / SELinux enforcing: labelled the program bin_t"
}

install_units() {
    local u
    for u in "${UNITS[@]}"; do
        install -m 0644 "$PKG_DIR/systemd/$u" "$UNIT_DIR/$u"
    done
    systemctl daemon-reload
    say "已安装 systemd 单元 / installed units: ${UNITS[*]}"
}

firewall_hint() {
    local port
    port="$(env_get YUNSHU_PORT)"
    port="${port:-8080}"
    say ""
    say "防火墙（本脚本不修改）/ Firewall (not changed by this script):"
    if command -v firewall-cmd >/dev/null 2>&1; then
        say "  sudo firewall-cmd --permanent --add-port=$port/tcp && sudo firewall-cmd --reload"
    elif command -v ufw >/dev/null 2>&1; then
        say "  sudo ufw allow $port/tcp"
    else
        say "  如有防火墙，请放行 TCP $port / allow TCP $port if a firewall is active"
    fi
}

main() {
    local start=0 no_systemd=0 use_systemd=0 arg
    for arg in "$@"; do
        case "$arg" in
            --start) start=1 ;;
            --no-systemd) no_systemd=1 ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; exit 2 ;;
        esac
    done
    [ "$(id -u)" -eq 0 ] || die "需要 root 权限：sudo ./install.sh / must run as root"

    PKG_DIR="$(cd "$(dirname "$0")" && pwd -P)"
    [ -f "$PKG_DIR/VERSION" ] && [ -x "$PKG_DIR/yunshu/yunshu" ] || die "请在解压后的安装包目录里运行 / run from the extracted package folder"
    VERSION="$(tr -d '[:space:]' <"$PKG_DIR/VERSION")"
    [[ "$VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z._+-]*$ ]] || die "VERSION 文件内容不正确 / bad VERSION: $VERSION"
    check_arch
    check_glibc
    ensure_tools

    if [ "$no_systemd" -eq 0 ]; then
        if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
            use_systemd=1
        else
            warn "没有检测到正在运行的 systemd，不安装服务单元（相当于 --no-systemd）/ systemd not running, skipping units"
        fi
    fi
    [ "$start" -eq 1 ] && [ "$use_systemd" -eq 0 ] && die "--start 需要 systemd / --start requires systemd"

    say "安装云枢 / Installing Yunshu $VERSION"
    ensure_user
    ensure_dirs
    write_config

    local active=() previous="" u
    if [ "$use_systemd" -eq 1 ]; then
        for u in yunshu-api.service yunshu-worker.service; do
            if systemctl is-active --quiet "$u"; then active+=("$u"); fi
        done
    fi
    if [ -L "$PREFIX/current" ]; then previous="$(readlink -f "$PREFIX/current" || true)"; fi

    install_program
    label_selinux
    switch_current "$TARGET"
    write_wrapper
    say "当前版本 / current: $PREFIX/current -> $TARGET"
    [ "$use_systemd" -eq 1 ] && install_units

    # 升级：之前在运行的服务按新版本重启。SQLite 由 API 启动时先备份再升级；PostgreSQL 先运行迁移
    if [ "${#active[@]}" -gt 0 ]; then
        if is_postgres; then
            say "标准版：先停止服务并运行数据库迁移 / PostgreSQL: stopping services and migrating first"
            systemctl stop "${active[@]}"
            if ! systemctl start yunshu-migrate.service; then
                if [ -n "$previous" ] && [ "$previous" != "$TARGET" ]; then
                    switch_current "$previous"
                    systemctl start "${active[@]}" || true
                    die "数据库迁移失败，已切回 $previous 并重新启动。查看：journalctl -u yunshu-migrate / migration failed, switched back to $previous"
                fi
                die "数据库迁移失败，服务保持停止。查看：journalctl -u yunshu-migrate / migration failed"
            fi
        fi
        say "重启之前在运行的服务 / restarting: ${active[*]}"
        systemctl restart "${active[@]}"
    fi

    if [ "$start" -eq 1 ]; then
        local units=(yunshu-api.service)
        if is_postgres; then
            systemctl enable yunshu-migrate.service
            [ "$(env_get YUNSHU_EMBEDDED_WORKER)" = 1 ] || units+=(yunshu-worker.service)
        fi
        systemctl enable "${units[@]}"
        systemctl start "${units[@]}"
    fi

    if [ -n "$OLD_COPY" ]; then rm -rf "$OLD_COPY"; fi

    local wait="$start"
    for u in "${active[@]}"; do
        if [ "$u" = yunshu-api.service ]; then wait=1; fi
    done
    if [ "$wait" -eq 1 ]; then
        if ! wait_ready; then
            die "服务没有在 3 分钟内就绪，查看：journalctl -u yunshu-api -n 100 / service not ready; see journalctl -u yunshu-api"
        fi
    fi

    say ""
    say "安装完成 / Installed: Yunshu $VERSION"
    say "  配置 / config:   $CONF_DIR/yunshu.env"
    say "  数据 / data:     $DATA_DIR"
    say "  主密钥 / key:    $KEY_DIR/master.key（请单独备份 / back it up separately）"
    say "  日志 / logs:     $LOG_DIR，journalctl -u yunshu-api"
    if [ -n "$previous" ] && [ "$previous" != "$TARGET" ]; then
        say "  上一版本保留在 / previous version kept at: $previous（回滚见 README.txt / see README.txt for rollback）"
    fi
    if [ "$use_systemd" -eq 1 ] && [ "$start" -eq 0 ] && [ "${#active[@]}" -eq 0 ]; then
        say ""
        say "下一步 / Next steps:"
        say "  1. 按需修改 / edit if needed: sudo vi $CONF_DIR/yunshu.env"
        say "  2. 启动 / start:  sudo systemctl enable --now yunshu-api"
        say "     标准版 / PostgreSQL: sudo systemctl enable --now yunshu-migrate yunshu-api yunshu-worker"
    elif [ "$use_systemd" -eq 0 ]; then
        say ""
        say "没有 systemd 时手动启动 / Without systemd, start manually:"
        say "  sudo yunshu api      # 以 $SVC_USER 用户运行，配置读 $CONF_DIR/yunshu.env"
    fi
    local port
    port="$(env_get YUNSHU_PORT)"
    say "  浏览器打开 / open: http://<本机地址 / host>:${port:-8080}"
    say "  首次登录 admin / admin123，按提示立即修改密码 / first login admin / admin123, change it immediately"
    firewall_hint
}

main "$@"
exit 0
