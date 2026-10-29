# Linux 原生安装

[English](README.en.md) · [Docker 部署](../README.md)

不想装 Docker 时，可以用 Linux 压缩包直接安装到服务器上：包里**自带 Python 运行时**，不依赖系统 Python；由 systemd 管理服务，配置写在 `/etc/yunshu/yunshu.env`。

## 系统要求

| 项目 | 要求 |
| --- | --- |
| 系统 | Debian 12 / 13、Ubuntu 22.04 / 24.04、Rocky Linux 8 / 9、AlmaLinux 8 / 9、openEuler 20.03 / 22.03 / 24.03 LTS（CI 在这些系统的官方容器镜像上逐个测试）；麒麟 V10 见下文 |
| 架构 | amd64（x86_64）、arm64（aarch64） |
| glibc | 不低于包里 `GLIBC_REQUIRED` 文件写的版本（构建基线 2.28，即 EL8 / openEuler 20.03 / 麒麟 V10）。查看本机：`getconf GNU_LIBC_VERSION`。Alpine 等 musl 系统不支持，请用 Docker |
| 服务管理 | systemd（没有 systemd 的环境可以用 `--no-systemd` 安装后手动运行） |
| 资源 | 1 核 / 1 GB 内存可运行轻量版；磁盘按资产数量与保留期预留，备份默认放在数据目录 |
| 其他 | 不需要 ipmitool；PostgreSQL 模式备份需要本机有 `pg_dump` / `pg_restore`（见下文） |

### 麒麟（Kylin）

压缩包按 glibc 2.28 基线构建，麒麟 V10（SP1–SP3，glibc 2.28）在支持范围内。**但麒麟本身没有经过测试**：没有可信的公开麒麟容器镜像。CI 用与麒麟 V10 同为 glibc 2.28 的 openEuler 20.03 LTS SP4 与 AlmaLinux / Rocky Linux 8 覆盖，另测 openEuler 22.03 / 24.03；这些结果说明同一基线的系统可用，不等于在麒麟上验证过。

- 安装前先看 `getconf GNU_LIBC_VERSION`：低于 `GLIBC_REQUIRED` 时 `install.sh` 会直接拒绝安装。
- 按下文 RHEL 系 / openEuler 的说明安装，并留意 SELinux 一节。
- 包里自带 SQLite（系统自带的 3.26 版不够新）、Python 运行时与 OpenSSL，不使用系统的这些库。

## 安装

从发布页 <https://github.com/kongbai9420/YunShu/releases> 下载对应架构的 `yunshu-<版本>-linux-<架构>.tar.gz` 和同名 `.sha256`：

```bash
sha256sum -c yunshu-4.4.0-beta.2-linux-amd64.tar.gz.sha256
tar xzf yunshu-4.4.0-beta.2-linux-amd64.tar.gz
cd yunshu-4.4.0-beta.2
sudo ./install.sh --start
```

浏览器打开 `http://<服务器地址>:8080`，用 `admin` / `admin123` 登录，**按提示立即修改密码**。

`install.sh` 做的事（可以重复运行）：

| 内容 | 位置 | 属主 / 权限 |
| --- | --- | --- |
| 系统用户与组 `yunshu`（不能登录） | — | — |
| 程序 | `/opt/yunshu/<版本>/`，`/opt/yunshu/current` 指向当前版本 | root，只读 |
| 命令 | `/usr/local/bin/yunshu`（root 运行时自动切换到 `yunshu` 用户执行） | root |
| 配置 | `/etc/yunshu/yunshu.env`（只在不存在时写入） | `root:yunshu` 0640，目录 0750 |
| 主密钥 | `/etc/yunshu/keys/master.key`（首次启动时生成） | `yunshu` 0600，目录 0700 |
| 数据与平台备份 | `/var/lib/yunshu`（`yunshu.db`、`backups/`） | `yunshu` 0750 |
| 日志 | `/var/log/yunshu`（另有 `journalctl -u yunshu-api`） | `yunshu` 0750 |
| systemd 单元 | `/etc/systemd/system/yunshu-{api,worker,migrate}.service` | — |

- 不带 `--start` 时只安装，最后打印下一步；`sudo systemctl enable --now yunshu-api` 启动。
- 没有 systemd 的环境（容器、chroot）用 `sudo ./install.sh --no-systemd`，然后 `sudo yunshu api` 前台运行。
- RHEL 系（Rocky、Alma、openEuler）的 `sudo` 默认不搜索 `/usr/local/bin`，请写全路径：`sudo /usr/local/bin/yunshu ...`。

常用命令：

```bash
sudo systemctl status yunshu-api
sudo journalctl -u yunshu-api -f
sudo yunshu worker --check     # Worker 心跳（轻量版即内嵌 Worker）
sudo yunshu backup create      # 立即备份
sudo yunshu version
```

## 配置（yunshu.env）

`/etc/yunshu/yunshu.env` 每行 `KEY=VALUE`，`#` 开头为注释；systemd 单元和 `yunshu` 命令都读它。修改后 `sudo systemctl restart yunshu-api`。默认内容（完整示例见安装包的 `yunshu.env.example`，升级时会另存为 `/etc/yunshu/yunshu.env.example` 供对照）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `YUNSHU_DATA_DIR` | `/var/lib/yunshu` | SQLite 数据库与平台备份 |
| `YUNSHU_MASTER_KEY_FILE` | `/etc/yunshu/keys/master.key` | 主密钥，必须在数据目录以外 |
| `YUNSHU_LOG_DIR` | `/var/log/yunshu` | 日志文件（10 MB × 5 轮转） |
| `YUNSHU_BIND` / `YUNSHU_PORT` | `0.0.0.0` / `8080` | 监听地址与端口 |
| `YUNSHU_EMBEDDED_WORKER` | `1` | 轻量版：采集与告警在 API 进程里运行 |
| `YUNSHU_SETUP_WIZARD` | `0` | 不显示首次设置向导（数据库在这里选定） |
| `YUNSHU_SECURE_COOKIES` | `0` | 经 HTTPS 反向代理访问时改为 `1` |
| `FORWARDED_ALLOW_IPS` | （注释） | 反向代理不在本机时填代理地址，才能记录真实客户端地址 |
| `TZ` | （注释） | 默认跟随主机时区；只想让云枢用别的时区时再设置 |
| `YUNSHU_SNMP_TRAP` 等 | （注释） | SNMP Trap 接收，见下文 |

改了数据目录、日志目录等路径时，systemd 单元的 `ReadWritePaths=` 也要加上新路径：`sudo systemctl edit yunshu-api`，写入

```ini
[Service]
ReadWritePaths=/data/yunshu
```

用 `systemctl edit` 写的覆盖文件在升级时不会被覆盖；`/etc/systemd/system/yunshu-*.service` 本身每次安装都会更新。

## 标准版：外部 PostgreSQL

1. 准备 PostgreSQL 16 / 17 数据库和账号。
2. 安装与服务器同大版本或更新的客户端工具，供备份恢复使用（例如 Debian / Ubuntu 用 PostgreSQL 官方 apt 源的 `postgresql-client-17`，RHEL 系用官方 yum 源的 `postgresql17`）。不在 `PATH` 里时设置 `YUNSHU_PG_BIN_DIR`。
3. 编辑 `/etc/yunshu/yunshu.env`：

   ```ini
   YUNSHU_DATABASE_URL=postgresql+psycopg://yunshu:fake-change-me@db.example.invalid:5432/yunshu
   YUNSHU_EMBEDDED_WORKER=0
   ```

4. 启动（`yunshu-migrate` 启用后，每次启动或重启 API / Worker 前都会先升级数据库结构）：

   ```bash
   sudo systemctl enable --now yunshu-migrate yunshu-api yunshu-worker
   ```

   全新安装时也可以直接 `sudo ./install.sh --start`：检测到 PostgreSQL 连接串时会一并启用这三个单元。
5. Worker 健康检查：`sudo yunshu worker --check`。

已有的轻量版数据迁到 PostgreSQL 用 `sudo yunshu upgrade_to_pg ...`（参数同 Docker 文档）。

## 升级

```bash
tar xzf yunshu-<新版本>-linux-amd64.tar.gz
cd yunshu-<新版本>
sudo ./install.sh
```

- 新版本装到 `/opt/yunshu/<新版本>/`，`current` 切换过去，旧版本目录保留。
- 之前在运行的服务会自动重启；之前没运行就不启动。
- **SQLite**：API 启动时发现结构落后，会先在 `/var/lib/yunshu/backups/` 生成 `pre-migrate` 备份，再自动升级。
- **PostgreSQL**：脚本先停止服务，运行 `yunshu-migrate`（同样先用 `pg_dump` 做 `pre-migrate` 备份），成功后再启动；迁移失败时自动把 `current` 切回旧版本并启动旧版本。
- 升级前最好再手动备份一次：`sudo yunshu backup create`，并确认主密钥已备份。

确认新版本正常后，可以删掉不再需要的旧版本目录 `/opt/yunshu/<旧版本>`。

## 回滚

不要让旧版本直接打开新版本升级过的数据库，要用**旧版本**恢复升级前的备份：

```bash
sudo systemctl stop yunshu-api yunshu-worker
sudo /opt/yunshu/<旧版本>/install.sh             # 把 current 指回旧版本（服务已停止，不会自动启动）
sudo yunshu backup list                          # 找到升级时生成的 pre-migrate 备份
sudo yunshu backup restore /var/lib/yunshu/backups/<备份文件> --yes
sudo systemctl start yunshu-api                  # 标准版再启动 yunshu-worker
```

PostgreSQL 的 `.dump` 备份同样用 `yunshu backup restore` 恢复。

## 备份与恢复

- 平台默认每 24 小时备份一次到 `/var/lib/yunshu/backups/`（`YUNSHU_BACKUP_INTERVAL_HOURS` 可改，`0` 关闭），也可以手动 `sudo yunshu backup create`。
- 恢复前必须停止服务：`sudo systemctl stop yunshu-api yunshu-worker`，然后 `sudo yunshu backup restore <备份文件> --yes`，再启动。恢复前会自动再备份一次当前数据库。
- 数据目录和服务器在同一块盘上，**不是离机备份**：请把 `backups/` 定期复制到别的机器。

## 主密钥备份

主密钥 `/etc/yunshu/keys/master.key` 用来加密已保存的设备密码。**平台备份里不含主密钥**；丢了它，备份能恢复资产，但设备密码要重新录入。

```bash
sudo cp -a /etc/yunshu/keys /安全的位置/yunshu-keys-$(date +%F)
```

在另一台机器恢复时，先 `install.sh`，把密钥文件放回 `/etc/yunshu/keys/`（再运行一次 `install.sh` 会把属主和权限改正），然后恢复数据库备份。也可以用 `sudo yunshu keytool ...` 导出 / 导入（参数同 Docker 文档）。

## 卸载

```bash
sudo /opt/yunshu/current/uninstall.sh            # 停止并禁用服务，删除程序、命令和单元；保留数据、配置、主密钥、日志和 yunshu 用户
sudo /opt/yunshu/current/uninstall.sh --purge    # 全部删除，需要输入 PURGE 确认
```

`--purge` 会删除主密钥，之后任何备份里的设备密码都无法解密。执行前请先把备份和 `/etc/yunshu/keys` 复制到别处。保留数据卸载后重新安装，会继续使用原来的数据。

## 防火墙

安装脚本**不修改防火墙**，只提示命令：

```bash
# firewalld（RHEL 系、openEuler、麒麟）
sudo firewall-cmd --permanent --add-port=8080/tcp && sudo firewall-cmd --reload
# ufw（Ubuntu）
sudo ufw allow 8080/tcp
```

经反向代理访问时只需放行代理的端口，并把 `YUNSHU_BIND` 改为 `127.0.0.1`。

## SELinux（RHEL 系 / openEuler / 麒麟）

- SELinux 为 enforcing 时，`install.sh` 会把主程序 `/opt/yunshu/<版本>/yunshu/yunshu` 标为 `bin_t`（有 `semanage` 时登记为永久规则，否则用 `chcon`），systemd 按普通服务启动它；其余目录用系统默认标签。
- 服务不能启动、日志里有 `avc: denied` 时：`sudo ausearch -m avc -ts recent`。常见原因是把数据目录改到了非常规位置，用 `semanage fcontext -a -t var_lib_t '/data/yunshu(/.*)?' && restorecon -R /data/yunshu` 处理。
- 监听 8080 / 1162 不需要额外的端口标签：服务以 `unconfined_service_t` 运行。
- 不建议为此关闭 SELinux。

## SNMP Trap 转发（162 → 1162）

Trap 接收默认关闭。开启时在 `yunshu.env` 设置 `YUNSHU_SNMP_TRAP=1` 和 `YUNSHU_SNMP_TRAP_TARGET=<BMC 能访问到的本机 IP>`；平台监听 **UDP 1162**（不需要特权端口），而 BMC 只会发往 162，所以要在主机上转发：

```bash
# firewalld
sudo firewall-cmd --permanent --add-forward-port=port=162:proto=udp:toport=1162
sudo firewall-cmd --reload

# nftables（没有 firewalld 时）
sudo nft add table ip yunshu
sudo nft add chain ip yunshu prerouting '{ type nat hook prerouting priority dstnat; }'
sudo nft add rule ip yunshu prerouting udp dport 162 redirect to :1162
# 永久生效：把上面三条写进 /etc/nftables.conf（或发行版对应的配置）
```

- 本机自己发往 162 的测试报文不经过 `prerouting`，测试时请从别的机器或直接发往 1162。
- 只收 SNMP v1 / v2c，来源 IP 和团体名要与资产对得上，否则丢弃。
- 标准版由独立 Worker 监听，轻量版由 API 进程里的内嵌 Worker 监听。

## systemd 单元说明

| 单元 | 用途 | 默认 |
| --- | --- | --- |
| `yunshu-api.service` | 页面与接口；轻量版同时运行内嵌 Worker | `--start` 时启用 |
| `yunshu-worker.service` | 独立 Worker（标准版） | 不启用 |
| `yunshu-migrate.service` | 一次性数据库迁移（`Type=oneshot`）；启用后在 API / Worker 启动前运行 | 不启用（标准版时启用） |

三个单元都以 `yunshu` 用户运行，`EnvironmentFile=/etc/yunshu/yunshu.env`，`NoNewPrivileges=yes`、`ProtectSystem=strict`（只允许写 `/var/lib/yunshu /etc/yunshu /var/log/yunshu`）、`PrivateTmp=yes`，没有任何特权能力。API 与 Worker 失败时自动重启（`Restart=on-failure`）；迁移失败不会反复重试，需要人查看原因。

需要“启动成功 = 页面已就绪”时（例如被其他单元依赖），可以用 `sudo systemctl edit yunshu-api` 加一条（需要 curl；单元文件里 `$` 写成 `$$`）：

```ini
[Service]
ExecStartPost=/bin/sh -c 'i=0; while [ $$i -lt 90 ]; do curl -fsS -o /dev/null http://127.0.0.1:8080/api/ready && exit 0; i=$$((i+1)); sleep 2; done; exit 1'
```

外部监控可以直接请求 `http://<服务器>:8080/api/ready`（200 表示数据库可用、结构版本一致、内嵌 Worker 在运行），Worker 用 `sudo yunshu worker --check`。

## 从源码构建压缩包

需要 Docker（在固定摘要的 AlmaLinux 8 镜像里构建；Python 运行时用 python-build-standalone 的 CPython 3.11（版本与 SHA-256 固定），依赖按哈希安装）：

```bash
cd frontend && npm ci && npm run build && cd ..
scripts/build-linux.sh --docker      # 输出 artifacts/linux/yunshu-<版本>-linux-<本机架构>.tar.gz 与 .sha256
```

构建脚本会从 sqlite.org 源码（版本与 SHA-256 固定）编译 SQLite 打进包里，检查包内所有程序与库实际需要的 glibc / libstdc++ 版本（超过 2.28 / GLIBCXX 3.4.25 即失败），并用临时目录做冒烟测试（`yunshu version`、启动 API、`/api/ready`、首次登录要求改密码、进程只加载包里的 SQLite 与 libpython、Worker 心跳、备份、迁移）。CI 见 `.github/workflows/linux-package.yml`。
