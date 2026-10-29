云枢运维平台 Linux 安装包 / Yunshu ops platform for Linux
================================================================

本压缩包自带 Python 运行时，不需要安装 Python。
This package bundles its own Python runtime; no system Python is needed.

支持 / Supported: Debian 12/13, Ubuntu 22.04/24.04, Rocky Linux 8/9, AlmaLinux 8/9,
openEuler 20.03/22.03/24.03, 麒麟 Kylin V10; amd64 / arm64.
需要 glibc 不低于 GLIBC_REQUIRED 文件里的版本（2.28；查看：getconf GNU_LIBC_VERSION）。
麒麟 V10 由同为 glibc 2.28 的 openEuler 20.03 / EL8 覆盖测试，未在麒麟本身上测试。
Kylin V10 is covered via glibc-2.28-compatible openEuler 20.03 / EL8 bases, not tested on Kylin itself.

完整说明 / Full guide: https://github.com/kongbai9420/YunShu/blob/main/deploy/linux/README.md


安装 / Install
--------------
  sha256sum -c yunshu-<版本>-linux-<架构>.tar.gz.sha256
  tar xzf yunshu-<版本>-linux-<架构>.tar.gz
  cd yunshu-<版本>
  sudo ./install.sh --start

浏览器打开 http://<本机地址>:8080，用 admin / admin123 登录，按提示立即修改密码。
Open http://<host>:8080, sign in as admin / admin123 and change the password right away.

安装位置 / Locations
  /opt/yunshu/<版本>/、/opt/yunshu/current   程序 / program
  /usr/local/bin/yunshu                      命令（root 运行时自动切换到 yunshu 用户）/ CLI
  /etc/yunshu/yunshu.env                     配置 / config
  /etc/yunshu/keys/master.key                主密钥，请单独备份 / master key, back it up separately
  /var/lib/yunshu                            数据库与备份 / database and backups
  /var/log/yunshu                            日志 / logs（也可 journalctl -u yunshu-api）

RHEL 系的 sudo 默认不含 /usr/local/bin，请写全路径：sudo /usr/local/bin/yunshu ...
On RHEL-family systems sudo's PATH lacks /usr/local/bin; use the full path.


常用命令 / Common commands
--------------------------
  sudo systemctl status yunshu-api
  sudo yunshu backup create            立即备份 / back up now
  sudo yunshu backup list
  sudo yunshu worker --check           Worker 心跳检查 / worker heartbeat check
  sudo yunshu version


标准版（PostgreSQL）/ PostgreSQL mode
------------------------------------
1. 在 /etc/yunshu/yunshu.env 设置 YUNSHU_DATABASE_URL，并把 YUNSHU_EMBEDDED_WORKER 改为 0。
   Set YUNSHU_DATABASE_URL and YUNSHU_EMBEDDED_WORKER=0 in /etc/yunshu/yunshu.env.
2. sudo systemctl enable --now yunshu-migrate yunshu-api yunshu-worker
   （yunshu-migrate 启用后，每次启动 API / Worker 前先升级数据库结构）
备份恢复需要本机安装与服务器同版本或更新的 pg_dump / pg_restore。
Backups need pg_dump / pg_restore of the server's major version or newer.


升级 / Upgrade
--------------
解压新版本，运行它的 install.sh。之前在运行的服务会自动重启：SQLite 先备份再自动升级；
PostgreSQL 先停服务、运行 yunshu-migrate，再启动。旧版本目录保留在 /opt/yunshu/<旧版本>。
Extract the new version and run its install.sh. Running services restart automatically.


回滚 / Rollback
---------------
  sudo systemctl stop yunshu-api yunshu-worker
  sudo /opt/yunshu/<旧版本>/install.sh        把 current 指回旧版本 / point current back
  sudo yunshu backup list                     找到升级前的 pre-migrate 备份 / find the pre-migrate backup
  sudo yunshu backup restore <备份文件> --yes   用旧版本恢复 / restore with the OLD version
  sudo systemctl start yunshu-api
不要让旧版本直接打开新版本升级过的数据库。/ Never point the old version at an upgraded database.


卸载 / Uninstall
----------------
  sudo /opt/yunshu/current/uninstall.sh           保留数据、配置与主密钥 / keeps data, config, key
  sudo /opt/yunshu/current/uninstall.sh --purge   全部删除（含主密钥）/ deletes everything incl. the key


防火墙与 SNMP Trap / Firewall and SNMP trap
-------------------------------------------
安装脚本不修改防火墙。firewalld：sudo firewall-cmd --permanent --add-port=8080/tcp && sudo firewall-cmd --reload
SNMP Trap 监听 UDP 1162；BMC 发往 162，需要在主机上把 UDP 162 转发到 1162（见完整说明）。
The trap receiver listens on UDP 1162; forward host UDP 162 to 1162 (see the full guide).

许可 / License: LICENSE；第三方组件 / third-party: NOTICES.txt, licenses/
