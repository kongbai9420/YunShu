# 部署与运行

本页讲怎么选档位、各平台怎么运行、怎么检查健康、怎么配 HTTPS。完整步骤以仓库里的部署说明为准：

- [Docker 部署指南](https://github.com/kongbai9420/YunShu/blob/main/deploy/README.md)（含 NAS 图形界面、标准版 YAML）
- [Linux 原生安装](https://github.com/kongbai9420/YunShu/blob/main/deploy/linux/README.md)
- [Windows 服务版](https://github.com/kongbai9420/YunShu/blob/main/deploy/windows/README.md)

## 选哪个档位

| 档位 | 适合 | 数据库 | 进程 | 模板 |
| --- | --- | --- | --- | --- |
| 轻量版 | 个人、家庭实验室、NAS、小机房 | SQLite | 一个进程，Worker 在 API 里运行 | `deploy/lite` |
| 标准版 | 资产多、多人同时使用、需要可靠备份 | 同机 PostgreSQL 17 | PostgreSQL + 迁移任务 + API + Worker | `deploy/standard` |
| 企业版 | 资产多、采集重，已有 DBA 维护的数据库 | 独立 PostgreSQL 16 / 17 | API + 调度 Worker + 多个执行 Worker | `deploy/enterprise` |

拿不准时先用轻量版，以后用 `upgrade_to_pg` 迁到 PostgreSQL，见 [备份、升级与迁移](backup-upgrade.md)。不支持从 PostgreSQL 迁回 SQLite。

Linux 安装包和 Windows 服务版默认是轻量版，也可以在 `yunshu.env` 里配置外部 PostgreSQL，相当于标准版。Windows 免安装包（ZIP）不含 PostgreSQL 驱动，**只支持轻量版**。

| 安装方式 | SQLite | PostgreSQL |
| --- | --- | --- |
| Docker（轻量 / 标准 / 企业模板） | 支持 | 支持 |
| Linux 安装包 | 支持 | 支持（外部 PostgreSQL） |
| Windows 服务版（MSI） | 支持 | 支持（外部 PostgreSQL） |
| Windows 免安装包（ZIP） | 支持 | 不支持 |

## Docker 与 NAS

镜像 `ghcr.io/kongbai9420/yunshu:4.4.0-beta.1` 同时支持 amd64 和 arm64，群晖、飞牛 fnOS、威联通、Unraid、普通 Linux 服务器都能运行。轻量版的命令和 YAML 见 [快速开始](quick-start.md)。

**数据卷**

| 卷（容器内路径） | 内容 | 备份方式 |
| --- | --- | --- |
| `yunshu-data`（`/data`） | 轻量版数据库、平台自动备份 `backups/` | 平台备份，或整卷备份 |
| `yunshu-keys`（`/etc/yunshu`） | 主密钥 `master.key`、首次设置向导保存的 `database.json` | **单独备份**，不要和数据放在一起 |
| `yunshu-pg`（标准版） | PostgreSQL 的全部业务数据 | 平台 `pg_dump` 备份，或数据库侧备份 |

- 模板里的卷名是固定的（`name: yunshu-data`），换目录、换模板都还是同一份数据。
- `/data` 和 `/etc/yunshu` 必须是两个不同的卷或目录，否则容器拒绝启动。这样即使数据库备份泄露，设备密码也无法解密。
- 想把数据放进 NAS 共享文件夹：改成目录挂载，并把两个目录的属主改为 UID **10001**（容器内的运行用户）。

**标准版**：PostgreSQL 17 + `migrate`（每次启动先升级数据库结构，然后退出）+ `api` + `worker`。把 YAML 里两处示例密码改成同一个长密码；用仓库的 `deploy/standard/` 时，把 `.env.example` 复制为 `.env` 再填密码。密码里有 `@ : / ? #` 等字符时，在连接串里要做 URL 编码，最简单是只用字母和数字。

**企业版**：连接你自己维护的 PostgreSQL 16 / 17，在 `.env` 里填 `YUNSHU_DATABASE_URL`。一个 `scheduler`（负责定时计划，也执行任务）加若干 `executor`（只执行任务）：

```bash
docker compose up -d --scale executor=4
```

## 多个 Worker

- 只有 PostgreSQL 支持多个 Worker。Worker 通过数据库认领任务，同一任务不会被执行两次。
- `YUNSHU_WORKER_ROLE=all`（默认）的 Worker 中只有一个持有调度锁、负责定时计划；`executor` 只执行任务，不参与调度。
- SQLite 只允许一个 Worker。
- 运行状态在“平台设置 → Worker 状态”查看：心跳、当前任务、谁持有调度锁、最近失败。

## Linux 原生安装

压缩包自带 Python 运行时，`install.sh` 建立 `yunshu` 系统用户、安装 systemd 单元：

| 内容 | 位置 |
| --- | --- |
| 程序 | `/opt/yunshu/<版本>/`，`/opt/yunshu/current` 指向当前版本 |
| 命令 | `/usr/local/bin/yunshu` |
| 配置 | `/etc/yunshu/yunshu.env` |
| 主密钥 | `/etc/yunshu/keys/master.key` |
| 数据与备份 | `/var/lib/yunshu` |
| 日志 | `/var/log/yunshu`，以及 `journalctl -u yunshu-api` |

| systemd 单元 | 用途 | 默认 |
| --- | --- | --- |
| `yunshu-api` | 页面与接口；轻量版同时运行内嵌 Worker | `--start` 时启用 |
| `yunshu-worker` | 独立 Worker（PostgreSQL 时用） | 不启用 |
| `yunshu-migrate` | 一次性数据库迁移，在 API / Worker 启动前运行 | 不启用（PostgreSQL 时启用） |

```bash
sudo systemctl status yunshu-api
sudo journalctl -u yunshu-api -f
sudo yunshu worker --check     # Worker 心跳
sudo yunshu version
```

- 修改 `yunshu.env` 后 `sudo systemctl restart yunshu-api`。
- RHEL 系（Rocky、Alma、openEuler）的 `sudo` 默认不搜索 `/usr/local/bin`，请写全路径 `sudo /usr/local/bin/yunshu ...`。
- 改了数据目录、日志目录时，要用 `sudo systemctl edit yunshu-api` 把新路径加进 `ReadWritePaths=`。
- SELinux 为 enforcing 时安装脚本会处理主程序的标签；不建议为此关闭 SELinux。
- 用外部 PostgreSQL：在 `yunshu.env` 里设 `YUNSHU_DATABASE_URL` 和 `YUNSHU_EMBEDDED_WORKER=0`，然后 `sudo systemctl enable --now yunshu-migrate yunshu-api yunshu-worker`。备份需要本机装有同大版本或更新的 `pg_dump` / `pg_restore`。

## Windows 服务版

MSI 把云枢装成 Windows 服务 `Yunshu`（显示名“云枢运维平台”），自动（延迟）启动，以低权限虚拟账号 `NT SERVICE\Yunshu` 运行。

| 内容 | 位置 |
| --- | --- |
| 程序 | `C:\Program Files\Yunshu\`（`yunshu.exe` 加入 PATH） |
| 配置 | `C:\ProgramData\Yunshu\yunshu.env` |
| 数据库与自动备份 | `C:\ProgramData\Yunshu\data\` |
| 主密钥 | `C:\ProgramData\Yunshu\keys\master.key` |
| 日志 | `C:\ProgramData\Yunshu\logs\` |

```powershell
Restart-Service Yunshu          # 改了 yunshu.env 后
yunshu version
yunshu worker --check
```

- 编辑 `yunshu.env` 和运行 `yunshu` 维护命令都需要管理员权限（开始菜单“云枢命令行”右键以管理员身份运行）。
- 第一次安装后，**先等服务启动过一次**（页面能打开）再运行维护命令，让主密钥由服务账号创建。
- 防火墙规则可在安装时勾选，或自己添加入站 TCP 8080。
- 只想本机访问：在 `yunshu.env` 里加 `YUNSHU_BIND=127.0.0.1`。

## 端口与网络

- 网页与接口：8080（Docker 容器内固定 8080，主机端口改 `ports` 左边；Linux / Windows 用 `YUNSHU_PORT`）。
- 平台要能访问设备的管理端口：SNMP UDP 161、SSH TCP 22、IPMI UDP 623、Redfish / vSphere HTTPS 443、PVE 8006。
- 接收 SNMP Trap 时（默认关闭）：平台监听 UDP 1162，BMC 只发往 162，需要把 162 转发到 1162，见 [设备接入](devices.md)。

## 健康检查

| 地址 | 含义 | 用途 |
| --- | --- | --- |
| `GET /api/health` | 进程活着，返回 `{"status": "ok", "version": "…"}`；首次设置向导未完成时多一个 `"setup": true` | 只看进程是否在 |
| `GET /api/ready` | 能正常服务：数据库连得上、数据库结构与程序版本一致、内嵌 Worker 在运行。不满足时返回 **503** 和原因 | 外部监控、负载均衡 |

- 镜像自带的健康检查：api 容器用 `/api/ready`；worker 容器检查本容器的 Worker 最近 90 秒内有没有心跳。结果在 `docker inspect` 的 Health 里。
- 独立 Worker 的健康检查：`yunshu worker --check`（容器内 `yunshu-entrypoint worker --check`）。
- `/api/ready` 返回 503 的常见原因见 [常见问题](troubleshooting.md)。

## 日志

| 部署方式 | 在哪里看 |
| --- | --- |
| Docker | `docker logs yunshu`（标准版 `docker compose logs api worker`）；模板已设置日志轮转（10 MB × 5） |
| Linux | `/var/log/yunshu/`，以及 `sudo journalctl -u yunshu-api` |
| Windows 服务版 | `C:\ProgramData\Yunshu\logs\yunshu-service.log`；服务启动失败时看“事件查看器 → Windows 日志 → 系统” |
| Windows 免安装包 | 解压目录下的 `logs/desktop.log` |

设置 `YUNSHU_LOG_DIR` 后，日志除标准输出外还写到该目录（每个文件 10 MB，保留 5 个）。

## HTTPS 与反向代理

需要从别的网络访问时，放在 Nginx、Caddy、Traefik、群晖“登录门户 → 反向代理”等之后，由代理终结 HTTPS。不要把 8080 直接暴露到公网。

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_buffering off;          # 实时推送（SSE）需要
}
```

然后：

1. 设置 `YUNSHU_SECURE_COOKIES=1`，让登录 Cookie 只走 HTTPS。
2. 代理和平台**不在同一台机器或同一个容器**时，设置 `FORWARDED_ALLOW_IPS=<代理的地址>`（多个用逗号分隔，例如 `198.51.100.5`）。平台只采信这些地址转发来的真实客户端地址。
3. 经代理访问时，Linux 可以把 `YUNSHU_BIND` 改为 `127.0.0.1`，只放行代理的端口。

**为什么要设 `FORWARDED_ALLOW_IPS`**：不设置时，所有人在平台看来都来自代理的地址——审计里的来源地址都一样；登录失败锁定按“来源 + 账号”计，同一来源合计失败 30 次会锁住整个来源 5 分钟，等于所有人一起被锁。**不要设成 `*`**，否则任何人都能伪造来源地址。

接收 BMC 的 Redfish 事件推送时（默认关闭），BMC 要能经 HTTPS 访问 `/api/bmc-events/` 路径，只需对 BMC 所在网段放行这一个路径。
