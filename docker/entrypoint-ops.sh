#!/bin/sh
# 云枢 4.0 镜像入口：api（默认，含页面）/ worker / migrate / backup / keytool / legacy_import / upgrade_to_pg，其余参数原样执行。
set -eu
cmd="${1:-api}"
[ "$#" -gt 0 ] && shift

if [ "$(dirname "$YUNSHU_MASTER_KEY_FILE")" = "$YUNSHU_DATA_DIR" ]; then
  echo "主密钥文件不能放在数据目录里，请把 /etc/yunshu 挂成单独的卷" >&2
  exit 1
fi

echo "$cmd" > /tmp/yunshu-role

case "$cmd" in
  api)
    # API 启动时会先备份（已有数据且版本落后时）再升级迁移
    exec python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080 --proxy-headers "$@" ;;
  worker)
    exec python -m app.worker "$@" ;;
  migrate|backup|keytool|legacy_import|upgrade_to_pg)
    exec python -m "app.$cmd" "$@" ;;
  *)
    exec "$cmd" "$@" ;;
esac
