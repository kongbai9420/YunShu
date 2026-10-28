# 配置项参考

部署级的配置全部通过**环境变量**设置。页面上能改的设置（告警规则、通知渠道、温控曲线、AI、漏洞数据源、外观等）保存在数据库里，见各功能页面。

带 ⚠️ 的选项会影响设备或数据，修改前请阅读说明。

## 在哪里设置

| 部署方式 | 写在哪里 | 改完后 |
| --- | --- | --- |
| Docker / Compose | compose 文件的 `environment:`，或 `docker run -e` | `docker compose up -d` 重新创建容器 |
| Linux 安装包 | `/etc/yunshu/yunshu.env` | `sudo systemctl restart yunshu-api`（有独立 Worker 时一起重启） |
| Windows 服务版 | `C:\ProgramData\Yunshu\yunshu.env`（需管理员权限编辑） | `Restart-Service Yunshu` |
| Windows 免安装包 | 系统环境变量；维护命令行读包内 `cli\desktop-portable.env` | 重新打开程序 |

### yunshu.env 的格式

```ini
# 注释以 # 开头
YUNSHU_PORT=8080
YUNSHU_SECURE_COOKIES=1
TZ=Asia/Shanghai
```

- 每行 `KEY=VALUE`，空行和 `#` 开头的行忽略；值两端的一对引号会去掉；不做变量展开。
- 文件位置：`YUNSHU_CONFIG` 指定的文件；没指定时 Linux 为 `/etc/yunshu/yunshu.env`，Windows 为 `%ProgramData%\Yunshu\yunshu.env`。
- **进程里已有的环境变量优先**，文件不会覆盖它们。
- `yunshu` 命令行和服务读的是同一个文件，所以维护命令不需要额外参数。
- Linux 升级时会把新版的示例另存为 `/etc/yunshu/yunshu.env.example` 供对照，不会覆盖你的 `yunshu.env`。

## 数据库与存储

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `YUNSHU_DATABASE_URL` | 未设置 | 数据库连接串。PostgreSQL 示例：`postgresql+psycopg://yunshu:fake-password@db.example.invalid:5432/yunshu?sslmode=require`。未设置时依次使用首次设置向导保存的选择、数据目录里的 SQLite |
| `YUNSHU_DATA_DIR` | Docker `/data`；Linux 包 `/var/lib/yunshu`；Windows 服务版 `C:\ProgramData\Yunshu\data`；免安装包为 EXE 同级 `data` | 数据目录：SQLite 数据库 `yunshu.db` 与平台备份 |
| `YUNSHU_MASTER_KEY_FILE` | Docker `/etc/yunshu/master.key`；Linux 包 `/etc/yunshu/keys/master.key`；Windows 服务版 `C:\ProgramData\Yunshu\keys\master.key`；免安装包 `%APPDATA%\yunshu\master.key` | ⚠️ 主密钥路径。不能放在数据目录里；丢失后已保存的设备密码无法解密 |
| `YUNSHU_SETUP_WIZARD` | `1`（模板与安装包里设为 `0`） | 全新安装且没有指定数据库时显示首次设置向导；`0` 关闭，直接使用轻量版 |
| `YUNSHU_AUTO_MIGRATE` | 未设置：SQLite 自动升级，PostgreSQL 只检查 | 启动时是否自动升级数据库结构。`1` 强制自动，`0` 只检查（版本不一致时拒绝启动） |
| `YUNSHU_PG_BIN_DIR` | 系统 PATH | `pg_dump` / `pg_restore` 所在目录。Docker 镜像已自带 17 版客户端，一般不用设 |

## 备份与数据保留

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `YUNSHU_BACKUP_DIR` | 数据目录下的 `backups/` | 平台备份存放位置 |
| `YUNSHU_BACKUP_INTERVAL_HOURS` | `24` | 定时备份间隔（小时），`0` 关闭。以数据库侧备份为准时可以关闭 |
| `YUNSHU_RAW_RETENTION_HOURS` | `48` | 原始指标保留小时数 |
| `YUNSHU_MINUTE_RETENTION_DAYS` | `30` | 分钟汇总保留天数 |
| `YUNSHU_HOUR_RETENTION_DAYS` | `365` | 小时汇总保留天数 |
| `YUNSHU_HISTORY_RETENTION_DAYS` | `180` | 已结束的告警及其事件、已完成的通知投递、已结束的非采集任务、AI 用量记录的保留天数；`0` 永久保留 |
| `YUNSHU_AUDIT_RETENTION_DAYS` | `365` | 审计日志与控制操作记录保留天数；`0` 永久保留 |

资产回收站的保留天数不是环境变量，在页面上设置，见 [资产与回收站](assets-recycle.md)。

## 进程、端口与日志

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `YUNSHU_EMBEDDED_WORKER` | `0`（轻量版模板、Linux 包、Windows 服务版与免安装包为 `1`） | `1` 时 Worker 在 API 进程里运行 |
| `YUNSHU_WORKER_ROLE` | `all` | `all`：可竞选调度并执行任务；`executor`：只执行任务（仅 PostgreSQL） |
| `YUNSHU_BIND` / `YUNSHU_PORT` | `0.0.0.0` / `8080` | `yunshu api` 监听的地址与端口（Linux 包、Windows 服务版）。Docker 容器内固定 8080，改主机端口请改 `ports` |
| `YUNSHU_LOG_DIR` | Docker 空；Linux 包 `/var/log/yunshu`；Windows 服务版 `C:\ProgramData\Yunshu\logs` | 设置后日志另写到该目录（10 MB × 5 个文件轮转），否则只写标准输出 |
| `YUNSHU_CONFIG` | Linux `/etc/yunshu/yunshu.env`；Windows `%ProgramData%\Yunshu\yunshu.env` | 配置文件位置 |
| `YUNSHU_FRONTEND_DIR` | 镜像和安装包内自带 | 构建好的网页目录，设置后由 API 一并提供页面。一般不用设 |
| `YUNSHU_INSPECTION_HOUR` | `8` | 每日巡检报告在本地时间几点后生成；`-1` 关闭（仍可手动生成） |
| `TZ` | 跟随主机；Docker 镜像内为 `Asia/Shanghai` | 时区，见下文 |

### 时区

`TZ` 影响每日巡检报告、命令巡检的定时、维护超期提醒、漏洞每日汇总的时间，以及日志时间。

- 设了 `TZ`（如 `Asia/Shanghai`）就用它。
- 没设或名字无效时跟随主机：Linux 读 `/etc/timezone` 或 `/etc/localtime`；Windows 按当前 UTC 偏移（有夏令时的地区请显式设置 `TZ`）。
- 都取不到时用北京时间。
- Docker 镜像默认 `TZ=Asia/Shanghai`；在别的时区请在 compose 里改。

## 安全与访问

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `YUNSHU_SECURE_COOKIES` | `0`（企业版模板为 `1`） | 经 HTTPS 访问时设为 `1`，登录 Cookie 只走 HTTPS。只用 http 访问时不要设，否则无法保持登录 |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | 可信反向代理的地址（逗号分隔）。代理不在本机 / 本容器时必须设置，平台才采信转发的真实客户端地址；否则审计来源都是代理地址，登录锁定也按代理地址计。**不要设成 `*`** |

## 服务器告警推送与采集节流

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `YUNSHU_SNMP_TRAP` | `0` | `1` 时 Worker 接收 BMC 发来的 SNMP Trap（v1 / v2c；来源 IP 与团体名要对得上资产） |
| `YUNSHU_SNMP_TRAP_PORT` | `1162` | 接收 Trap 的 UDP 端口。BMC 只发往 162，Docker 用端口映射 `162:1162/udp`，Linux 用防火墙转发 |
| `YUNSHU_SNMP_TRAP_BIND` | `0.0.0.0` | 接收 Trap 的监听地址 |
| `YUNSHU_SNMP_TRAP_TARGET` | 空 | 写到 BMC 上的 Trap 目标地址（BMC 能访问到的平台 IP，如 `192.0.2.5`） |
| `YUNSHU_BMC_EVENTS_URL` | 空 | BMC 推送 Redfish 事件用的平台地址，必须 `https://` 开头，经 HTTPS 反向代理 |
| `YUNSHU_BMC_SNMP` | `0` | ⚠️ `1` 时允许管理员在“运维操作 → 开启 SNMP”里修改 BMC 的 SNMP 设置、Trap 目标与 Redfish 事件订阅（会写设备配置） |
| `YUNSHU_BMC_RATE` | `redfish=2,ipmi=1` | 每台 BMC 每分钟最多定时采集几次（按协议）；`0` 表示不限。只限制定时采集，控制操作不受限 |
| `YUNSHU_BOOST_MAX` | `10` | 同时“提频采集”（接近阈值、有告警或收到推送事件时每 15 秒采一次）的资产上限 |
| `YUNSHU_IPMI_SESSION_REUSE` | `0` | `1` 时 IPMI 采集复用会话，减少 BMC 登录记录（未实机验证）。Dell 服务器推荐改用 SNMP 采集 |

## 控制功能

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `YUNSHU_CONTROL_CUTOVER` | `1` | ⚠️ 允许管理员在页面上把控制归属切到新平台。`0` 时只能查看切换前检查，不能切换（切回旧控制器不受影响） |
| `YUNSHU_LEGACY_URL` | 空 | 旧版控制器的地址（如 `http://192.0.2.30:8080`）。设置后切换前检查会确认它已停止响应 |
| `YUNSHU_GUEST_POWER` | `0` | ⚠️ `1` 开启经 PVE / vSphere 开关虚拟机和容器，以及关宿主前先关其上虚拟机 |

## 单点登录（OIDC，可选）

`YUNSHU_OIDC_ISSUER`、`YUNSHU_OIDC_CLIENT_ID`、`YUNSHU_OIDC_REDIRECT_URL` 三项都设置后才开启；不设置时登录页不显示单点登录按钮。

| 变量 | 说明 |
| --- | --- |
| `YUNSHU_OIDC_ISSUER` | 身份提供方地址，如 `https://sso.example.test/realms/ops`（必填） |
| `YUNSHU_OIDC_CLIENT_ID` | 客户端 ID（必填） |
| `YUNSHU_OIDC_CLIENT_SECRET` | 客户端密钥（只放在环境变量里，不进数据库） |
| `YUNSHU_OIDC_REDIRECT_URL` | 回调地址：`https://<平台地址>/api/auth/oidc/callback`（必填） |
| `YUNSHU_OIDC_LABEL` | 登录页按钮文字，默认“单点登录” |
| `YUNSHU_OIDC_ADMIN_GROUPS` | 逗号分隔；用户的 groups 包含其一即为管理员 |
| `YUNSHU_OIDC_OPERATOR_GROUPS` | 逗号分隔；包含其一即为运维，否则为只读 |
| `YUNSHU_OIDC_SCOPE_ALL` | 新建的单点登录账号是否可见全部资产，默认 `0`（看不到任何资产，等管理员设置范围） |

## 部署模板与开发用

| 变量 | 用途 |
| --- | --- |
| `YUNSHU_IMAGE` | `deploy/` 下 compose 模板使用的镜像，默认 `ghcr.io/kongbai9420/yunshu:4.4.0-beta.1` |
| `POSTGRES_PASSWORD` | 标准版模板里 PostgreSQL 的密码（写在 `.env`，不要提交到任何仓库） |
| `YUNSHU_API_URL` | 前端开发服务器转发到的后端地址，默认 `http://127.0.0.1:8000` |
| `YUNSHU_PREVIEW_PASSWORD` | 本地预览造数据脚本使用的登录密码 |

## 命令行 yunshu

Linux 安装包、Windows 服务版和免安装包（`cli\yunshu.exe`）用同一个命令 `yunshu`；Docker 镜像里是 `yunshu-entrypoint`，子命令相同。

| 命令 | 作用 |
| --- | --- |
| `yunshu api` | 启动页面与接口（默认 `0.0.0.0:8080`） |
| `yunshu worker` | 启动后台 Worker |
| `yunshu worker --check` | Worker 健康检查（最近有没有心跳） |
| `yunshu migrate` | 升级数据库结构到最新（PostgreSQL 升级版本后必须先运行） |
| `yunshu backup create` / `list` / `verify <文件>` / `restore <文件> --yes` | 备份、列出、校验、恢复（恢复前先停止服务） |
| `yunshu keytool path` / `init` / `fingerprint` / `export <目标文件>` | 显示主密钥位置、生成、查看编号、导出备份（目标文件不能已存在）。不会打印密钥本身 |
| `yunshu upgrade_to_pg precheck\|run --target <连接串> [--yes]` | 把 SQLite 数据迁到空的 PostgreSQL |
| `yunshu legacy_import --config <config.ini> --users <users.json> [--apply]` | 导入 3.x 旧版数据（不加 `--apply` 只预检） |
| `yunshu version` | 显示版本 |
| `yunshu service --foreground` | 仅 Windows 服务版：先停止服务后，在控制台里按服务的方式运行，便于排查 |

各平台的写法：

```bash
# Docker（轻量版容器名 yunshu；标准版用 api 容器）
docker exec yunshu yunshu-entrypoint backup create
docker compose exec api yunshu-entrypoint migrate

# Linux（RHEL 系写全路径 /usr/local/bin/yunshu）
sudo yunshu backup create
```

```powershell
# Windows 服务版（管理员 PowerShell）
yunshu backup create

# Windows 免安装包（在解压目录的 cli 文件夹里，先关闭程序窗口再恢复）
.\yunshu.exe backup create
```

备份、恢复、升级、迁移的完整步骤见 [备份、升级与迁移](backup-upgrade.md)。
