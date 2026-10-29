#!/bin/bash
# 云枢运维平台 Linux 卸载脚本（root 运行）。
# Yunshu ops platform uninstaller for Linux (run as root).
#
#   sudo /opt/yunshu/current/uninstall.sh          停止并禁用服务，删除程序与命令；保留数据、配置、主密钥和日志
#   sudo /opt/yunshu/current/uninstall.sh --purge  同时删除数据、配置、主密钥、日志和 yunshu 用户（需输入 PURGE 确认）
#
# 保留的数据在重新安装后会继续使用。

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
die() { printf '错误 / Error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法 / Usage: sudo ./uninstall.sh [--purge]

  （无参数）停止服务并删除程序；保留数据、配置、主密钥和日志
            stop the services and remove the program; keep data, config, master key and logs
  --purge   同时删除数据、配置、主密钥、日志和 yunshu 用户（需输入 PURGE 确认）
            also delete data, config, master key, logs and the yunshu user (type PURGE to confirm)
EOF
}

running_pids() {  # 仍在运行、程序位于 /opt/yunshu 下的进程
    # 没有 CAP_SYS_PTRACE 时（例如在容器里）读不到别的用户进程的 exe 链接，再看对所有人可读的 cmdline
    local p exe cmd pids=""
    for p in /proc/[0-9]*; do
        exe="$(readlink "$p/exe" 2>/dev/null || true)"
        [ -n "$exe" ] || exe="$(tr '\0' ' ' <"$p/cmdline" 2>/dev/null || true)"
        case "$exe" in "$PREFIX"/*) pids="$pids ${p#/proc/}" ;; esac
    done
    printf '%s' "${pids# }"
}

main() {
    local purge=0 arg answer u pids
    for arg in "$@"; do
        case "$arg" in
            --purge) purge=1 ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; exit 2 ;;
        esac
    done
    [ "$(id -u)" -eq 0 ] || die "需要 root 权限 / must run as root"

    if [ "$purge" -eq 1 ]; then
        cat <<EOF
================================================================
警告：--purge 会永久删除以下内容，无法恢复：
WARNING: --purge permanently deletes:
  $DATA_DIR   数据库与平台备份 / database and platform backups
  $CONF_DIR   配置与主密钥 / config and the MASTER KEY ($KEY_DIR)
  $LOG_DIR    日志 / logs
  用户和组 / user and group: $SVC_USER

主密钥删除后，已保存的设备密码无法再解密，任何备份也无法恢复这些凭据。
如果还需要这些数据，请先把备份文件和 $KEY_DIR 复制到别处。
Without the master key, saved device passwords cannot be decrypted,
not even from backups. Copy your backups and $KEY_DIR elsewhere first.
================================================================
EOF
        printf '确认删除请输入 PURGE / Type PURGE to confirm: '
        answer=""
        read -r answer || true
        [ "$answer" = PURGE ] || die "未确认，已取消，没有删除任何内容 / not confirmed, nothing was removed"
    fi

    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        for u in "${UNITS[@]}"; do
            systemctl disable --now "$u" >/dev/null 2>&1 || true
        done
    fi
    for u in "${UNITS[@]}"; do
        rm -f "$UNIT_DIR/$u"
        # 用 systemctl edit 加的 drop-in 只在 --purge 时删除
        if [ "$purge" -eq 1 ]; then rm -rf "$UNIT_DIR/$u.d"; fi
    done
    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        systemctl daemon-reload || true
        systemctl reset-failed "${UNITS[@]}" >/dev/null 2>&1 || true
    fi

    pids="$(running_pids)"
    [ -z "$pids" ] || die "仍有云枢进程在运行（PID $pids），请先停止后再卸载 / Yunshu processes still running (PID $pids); stop them first"

    if command -v semanage >/dev/null 2>&1; then
        semanage fcontext -d "$PREFIX/[^/]+/yunshu/yunshu" >/dev/null 2>&1 || true
    fi
    if [ -L "$BIN_LINK" ] || grep -q "$WRAPPER_MARK" "$BIN_LINK" 2>/dev/null; then
        rm -f "$BIN_LINK"
    fi
    rm -rf "$PREFIX"
    say "已删除程序与命令 / removed: $PREFIX, $BIN_LINK, systemd units"

    if [ "$purge" -eq 1 ]; then
        rm -rf "$DATA_DIR" "$CONF_DIR" "$LOG_DIR"
        if getent passwd "$SVC_USER" >/dev/null; then userdel "$SVC_USER" || true; fi
        if getent group "$SVC_USER" >/dev/null; then groupdel "$SVC_USER" || true; fi
        say "已删除数据、配置、主密钥、日志与用户 / removed data, config, master key, logs and user"
    else
        say "已保留 / kept: $DATA_DIR  $CONF_DIR（含主密钥 / incl. master key）  $LOG_DIR  用户 / user $SVC_USER"
        say "重新安装后会继续使用这些数据 / a reinstall will reuse them"
    fi
}

main "$@"
exit 0
