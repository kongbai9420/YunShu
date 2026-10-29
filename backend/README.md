# backend（4.0 后端）

FastAPI + SQLAlchemy 2.x + Alembic，Python 3.11。模块导入时不创建应用、
不连数据库、不启动 Worker。

## 数据库与迁移

4.0 只做 SQLite 轻量版，文件库开启 WAL 和外键约束，主键全部是 UUID。

- 数据目录：环境变量 `YUNSHU_DATA_DIR`（默认 `./data`），库文件为其中的 `yunshu.db`；
  也可用 `YUNSHU_DATABASE_URL` 直接指定。
  标准版（PostgreSQL，4.1 起，安装 `requirements-standard.lock`）：`postgresql+psycopg://用户@主机:5432/库名`。
  数据库结构升级只有一个入口 `python -m app.migrate`；SQLite 下 API / Worker 启动时自动执行，PostgreSQL 下启动时只检查版本、不一致即拒绝启动，需先运行迁移（`YUNSHU_AUTO_MIGRATE=1` 可改为自动，`0` 强制只检查）。
- 模型：`app/models/`；迁移：`migrations/versions/`，每个迁移都写回退。
- 升级到最新：`python -m app.migrate`；回退：`python -m app.migrate downgrade <revision>`。
- 改了模型后生成迁移：`alembic revision --autogenerate -m "说明"`，检查生成内容后提交；
  `tests/test_migrations.py` 会比对模型与迁移，不一致即失败。

## 虚拟资源观察与确认（P7）

迁移 head `0016`。成功的 PVE / VMware 采集只更新独立观察表；每个管理端点一个实例范围，不按名称 / IP 合并。管理员 / 运维先确认宿主，再预览并确认 VM / 容器的 `link / create / ignore / unlink`；新资产为规划中，无端点和凭据。三角色可读，写入校验 CSRF、预览版本并记录审计。

| 接口 | 用途 |
| --- | --- |
| `GET /api/virtualization/sources` | 实例范围，`limit / offset` 分页 |
| `GET /api/virtualization/sources/{id}/observations` | 候选、绑定、当前宿主与质量；`kind / q / limit / offset` |
| `POST /api/virtualization/observations/{id}/preview` | `action / revision / asset_id / name`，返回差异和 `preview_token`，不写库 |
| `POST /api/virtualization/observations/{id}/confirm` | 原请求加 `preview_token`；变化 / 重复 / 并发确认返回 409 |

迁移确认仅替换当前观察创建且仍归其所有的旧关系；人工关系逐项展示并保留。失联、过期、目标 / 原生服务身份变化或本次未见拒绝新建 / 关联，电源显示未知；不完整快照不自动判定消失。原生电源只在发现面板展示，不自动写 guest 指标。回退 `0015` 保留业务资产 / 关系但删除观察和映射，需先备份。专项 `tests/test_virtualization.py`，实机兼容性仍待验证。

## 本地运行

```bash
cd backend
uv venv .venv --python 3.11            # 或 python -m venv .venv
uv pip install --require-hashes -r requirements-dev.lock   # 或 pip install --require-hashes -r ...
.venv/Scripts/python -m pytest          # Linux/macOS: .venv/bin/python -m pytest
.venv/Scripts/python -m uvicorn app.main:create_app --factory --port 8000
```

## 统一命令行与配置文件

Linux 压缩包、Windows 服务版和免安装包都用同一个命令 `yunshu`（源码里是 `python -m app`，见 `app/cli.py`）：

```bash
yunshu api                 # 页面与接口，默认 0.0.0.0:8080（YUNSHU_BIND / YUNSHU_PORT）
yunshu worker              # 后台 Worker；yunshu worker --check 是健康检查
yunshu backup create       # 维护工具：migrate / backup / keytool / legacy_import / upgrade_to_pg，参数同 python -m app.<名字>
yunshu version
```

- 启动前读配置文件 `yunshu.env`（每行 `KEY=VALUE`，`#` 开头为注释）：`YUNSHU_CONFIG` 指定的文件，否则 Linux `/etc/yunshu/yunshu.env`、Windows `%ProgramData%\Yunshu\yunshu.env`。进程里已有的环境变量优先。
- `YUNSHU_LOG_DIR`：日志另写到该目录（每个文件 10 MB，保留 5 个），否则只写标准输出。
- 时区：`TZ` 优先；没设时跟随主机（Linux 读 `/etc/timezone` 或 `/etc/localtime`，Windows 按当前 UTC 偏移）；都取不到时用北京时间。Docker 镜像默认设了 `TZ=Asia/Shanghai`。
- 就绪检查 `GET /api/ready`：数据库可用、结构版本一致、内嵌 Worker 在运行时返回 200，否则 503 和原因；`/api/health` 只说明进程活着。

## 依赖锁

- `pyproject.toml`：直接依赖（不写版本，由锁文件固定）。
- `requirements.lock`：运行依赖的完整解析集，精确版本 + SHA-256 哈希。
- `requirements-dev.lock`：运行依赖 + pytest/httpx，以 `requirements.lock` 为约束，版本一致。
- `requirements-desktop.lock`：运行依赖 + pywebview 6.2.1，以 `requirements.lock` 为约束。桌面启动见 [desktop-guide.md](../scripts/desktop-guide.md)，Windows 可使用 `scripts/desktop.bat`。

锁文件用 uv 生成（`--universal`，同一份锁覆盖 Linux 与 Windows）：

```bash
uv pip compile pyproject.toml --universal --python-version 3.11 --generate-hashes --no-header -o requirements.lock
uv pip compile pyproject.toml --extra dev -c requirements.lock --universal --python-version 3.11 --generate-hashes --no-header -o requirements-dev.lock
uv pip compile pyproject.toml --extra desktop -c requirements.lock --universal --python-version 3.11 --generate-hashes --no-header -o requirements-desktop.lock
uv pip compile requirements-package.in --universal --python-version 3.11 --generate-hashes --no-header -o requirements-package.lock
```

## 直接依赖许可证

| 包 | 锁定版本 | 许可证 | 用途 |
| --- | --- | --- | --- |
| fastapi | 0.143.2 | MIT | Web 框架 |
| uvicorn | 0.54.0 | BSD-3-Clause | ASGI 服务器 |
| SQLAlchemy | 2.1.4 | MIT | ORM |
| alembic | 1.20.0 | MIT | 数据库迁移 |
| openpyxl | 3.1.5 | MIT | 模板导入读写 XLSX（依赖 et-xmlfile 2.0.0，MIT） |
| python-multipart | 0.0.32 | Apache-2.0 | 文件上传 |
| pysnmp | 7.1.30 | BSD-2-Clause | SNMP 采集（依赖 pyasn1 0.6.4，BSD-2-Clause） |
| paramiko | 5.0.0 | LGPL-2.1 | SSH 采集；原样使用、不修改，旧版已在用（依赖 bcrypt、pynacl Apache-2.0，invoke BSD-2-Clause） |
| pyghmi | 1.6.19 | Apache-2.0 | IPMI 采集（依赖 python-dateutil Apache-2.0 / BSD、six MIT） |
| httpx | 0.28.1 | BSD-3-Clause | Redfish / PVE / ESXi 接口（依赖 httpcore BSD-3-Clause、certifi MPL-2.0） |
| pyvmomi | 9.1.1.0 | Apache-2.0 | ESXi / vCenter 采集（vSphere API） |
| tzdata | 2026.5 | Apache-2.0 | 时区数据：Windows 没有系统时区库，按 Asia/Shanghai 等时区计算时间要靠它 |
| snmpsim / pysmi | 1.2.2 / 2.0.0 | BSD-2-Clause | 仅测试：本机 SNMP 模拟器 |
| pywebview | 6.2.1 | BSD-3-Clause | 桌面窗口（可选依赖）；Windows 的 pythonnet / clr-loader 为 MIT，bottle 为 MIT，proxy-tools 为 BSD；macOS 的 PyObjC 为 MIT |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | 凭据加密（AES-GCM） |
| pytest | 9.1.1 | MIT | 测试（仅开发） |

传递依赖均为 MIT / BSD / Apache-2.0 / PSF-2.0；例外两个，都是原样使用、不修改源码：
- paramiko（LGPL-2.1，弱 copyleft；Python 包以导入方式使用，用户可自行替换，旧版已在用）；
- certifi（MPL-2.0，文件级弱 copyleft，随 httpx 引入的 CA 证书包）。
没有 GPL/AGPL 依赖。

## 凭据加密与主密钥

凭据用 AES-256-GCM 加密存库，主密钥放在独立的密钥文件里，不与数据库同目录：

- 位置：`YUNSHU_MASTER_KEY_FILE`；未设置时 Docker 为 `/etc/yunshu/master.key`（单独挂卷），
  Windows 为 `%APPDATA%\yunshu\master.key`，Linux 为 `~/.config/yunshu/master.key`。
- 首次启动自动生成；放在数据目录内会拒绝启动。
- `python -m app.keytool init | fingerprint | export <文件>`：生成、查看编号、导出备份。不会打印密钥本身。
- 数据库备份不含主密钥。主密钥丢失后已保存的设备密码无法解密，需要重新录入；资产数据不受影响。

## 登录与权限

资源全景只读接口：`GET /api/overview/resources` 支持 q、role、form、platform、lifecycle、site_id、tag、limit（默认 25，最大 100）和 offset；`GET /api/overview/resources/{id}/children` 仅查询直接 `hosts` 子项并独立分页。返回采集状态、关键指标的六种质量 / 观测时间、活跃告警数及有限宿主引用，不触发采集或控制、不返回凭据或端点指纹。三角色均沿用 reader 权限。

- 首次启动库里没有用户时，自动创建 `admin`，初始密码与旧版相同（`admin123`），首次登录必须先改密码，
  改完之前所有业务接口返回 403。导入旧版 users.json 时，未改过密码的初始管理员会被旧账号替换。
- 密码哈希与旧版 users.json 相同（PBKDF2-HMAC-SHA256 十万次），旧账号可直接导入；
  同一来源 + 同一账号 5 分钟内失败 5 次锁定这一组 5 分钟，同一来源合计失败 30 次锁定整个来源 5 分钟（内存计数，重启清零）。
  经反向代理访问且代理不在本机时，设置 `FORWARDED_ALLOW_IPS=<代理地址>` 才能拿到真实客户端地址。
- 会话：HttpOnly Cookie `yunshu_session`（库里只存令牌哈希，24 小时、访问顺延）；改数据的请求
  必须带 `X-CSRF-Token` 头，值见登录响应或 `yunshu_csrf` Cookie。经 HTTPS 访问时设 `YUNSHU_SECURE_COOKIES=1`。
- 角色：

| 操作 | 管理员 admin | 运维 operator | 只读 viewer |
| --- | --- | --- | --- |
| 查看资产、场所、机柜、凭据元数据 | ✓ | ✓ | ✓ |
| 增删改资产、端点、关系、场所、机柜、凭据 | ✓ | ✓ | |
| 用户管理、审计查询 | ✓ | | |

凭据接口只返回别名、类型、用户名、备注等元数据，任何角色都拿不到密文或明文。
所有写操作记审计，详情里的密码、令牌类字段写入前替换为 `***`。

## 资产 API（P1）

| 方法与路径 | 说明 |
| --- | --- |
| `POST /api/auth/login` · `POST /api/auth/logout` · `GET /api/auth/me` · `POST /api/auth/password` | 登录、退出、当前用户、改密码 |
| `GET/POST /api/assets`，`GET/PATCH/DELETE /api/assets/{id}` | 资产；列表支持 `q`（名称/编号/型号/管理地址）、`form`、`role`、`platform`、`lifecycle`、`site_id`、`tag`，排序 `sort`（name / asset_no / form / role / platform / vendor / lifecycle / updated_at，默认 name）与 `order`（asc / desc），未填写的值总排在最后，同值按名称再按 id 保证翻页稳定；PATCH 必须带读取时的 `revision`，不一致返回 409 |
| `GET /api/assets/recycle-bin`，`POST /api/assets/{id}/restore`，`DELETE /api/assets/{id}/purge`，`GET/PUT /api/assets/recycle-bin/settings` | 回收站：`DELETE /api/assets/{id}` 只移入回收站（生命周期改为已退役，所有列表、计数、关系当作不存在），保留期 `retention_days` 默认 30 天（1–365，管理员可改），到期由 Worker 每小时 `assets.purge` 彻底删除；运维和管理员可恢复，只有管理员能提前彻底删除；回收站里的资产仍占用资产编号 |
| `POST /api/assets/{id}/endpoints`，`PATCH/DELETE /api/endpoints/{id}` | 管理端点 |
| `POST /api/relations`，`DELETE /api/relations/{id}` | 资产关系（hosts / installed_in / belongs_to / depends_on） |
| `GET/POST /api/sites`，`PATCH/DELETE /api/sites/{id}`；`/api/racks` 同理 | 场所与机柜 |
| `GET/POST /api/credentials`，`PATCH/DELETE /api/credentials/{id}` | 凭据（只返回元数据） |
| `GET/POST /api/users`，`PATCH /api/users/{id}`；`GET /api/audit` | 用户与审计（仅管理员） |

资产分类：形态 `physical / vm / container`；角色 `server / switch / router / ap / firewall / nas / storage / ups / other`；
平台为自由文本。去重只认资产编号或旧系统编号，同一管理地址可以属于多台资产。

`POST /api/assets` 的 `endpoints[]` 可传 `credential_id` 引用已有凭据，或传 `new_credential`
（`alias / kind / username / secret / notes`，与凭据创建接口相同），二者互斥。
资产、端点、新凭据及审计使用同一事务；资产编号或凭据别名冲突返回 409 并整体回滚。
新凭据按协议校验类型；需要用户名的类型必须填写设备用户名。响应只包含凭据引用；422 校验错误不返回原始输入或上下文中的秘密。

## 旧数据导入（P1）

```bash
python -m app.legacy_import --config <旧版目录>/config.ini --users <旧版目录>/src/users.json           # 预检：完整走一遍后回滚，只出报告
python -m app.legacy_import --config <旧版目录>/config.ini --users <旧版目录>/src/users.json --apply   # 确认导入
```

- 只读旧文件，导入前后文件内容不变；不经过旧版 ConfigManager（它加载时会回写默认值）。
- `servers_json` → 物理服务器（品牌、型号、序列号）+ IPMI 端点 + 加密凭据；`system_servers_json` → Linux 服务器
  + SSH 端点 + 加密凭据，`node_id` → 与宿主的“承载”关系（未绑定的按虚拟机导入，报告里提示确认）；
  `users.json` → 用户，密码哈希原样带过来，旧密码照常能登录。
- 温控曲线、每台服务器的温控模式、告警设置原样存进 `legacy_mappings`；P4 在启动 / Worker 评估时将旧告警参数迁入规则（人工改过的规则不覆盖），温控在 P5 迁移；webhook 地址与密钥不保存，需重新填写。
- 可重复执行：以旧 ID 为键，旧记录没变就跳过；变了且新平台没改过就更新；新平台改过的列为冲突、不覆盖；
  新平台删掉的不重建；旧配置里删掉的不在新库删除。
- 报告、日志、映射表里都没有密码和管理地址以外的敏感信息；`--json` 输出供脚本读取。

## 模板批量导入（P1）

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/import/templates`，`GET /api/import/templates/{kind}.{csv|xlsx}` | 模板列表与下载：physical_server / linux_server / switch / router / generic |
| `POST /api/import/preview`（表单：file、kind、可选 mapping） | 上传并预览：逐行标出新增 / 更新 / 无变化 / 冲突 / 无效，不改资产 |
| `POST /api/import/{id}/commit` · `POST /api/import/{id}/undo` | 确认提交 · 整批撤销 |
| `GET /api/import/batches` · `GET /api/import/{id}` · `GET /api/import/{id}/results.csv` | 批次列表、逐行结果、导出结果 |

- 资产编号必填，是唯一的去重键；同一文件里重复的编号标为冲突。不按 IP 合并。
- 更新只改填了值的列；预览后到提交前资产被别人改过的行标为冲突、不覆盖。
- 撤销只处理导入后没再被修改过的行，改过的保留并在结果里说明；本批新建的空场所一并删除。
- 关联资产编号表示“承载本资产的宿主”，可以引用同一文件里的资产。
- 限制：5 MB、5000 行；只收 .csv（UTF-8 或 GBK）和 .xlsx；XLSX 公式单元格整行无效；导出结果对 = + - @ 开头的值加单引号。
- 模板不含密码列，凭据用“凭据别名”引用。

## 备份与恢复（P1）

```bash
python -m app.backup create                     # 立即备份
python -m app.backup list
python -m app.backup verify <备份文件名>
python -m app.backup restore <备份文件名> --yes   # 先停止服务
```

- SQLite 在线备份，服务运行中也能得到一致快照；备份后做完整性检查，旁边的 `.json` 记录时间、原因、迁移版本、SHA-256 和所需主密钥编号。
- 位置：`YUNSHU_BACKUP_DIR`，默认数据目录下的 `backups/`。定时备份间隔 `YUNSHU_BACKUP_INTERVAL_HOURS`（默认 24，0 关闭），保留最近 7 份；手动、迁移前、恢复前各保留最近 10 份。
- 每次升级迁移前，如果库里已有数据且版本落后，会先自动备份（启动服务和 `python -m app.migrate` 都会）。
- 恢复前自动把当前库另存一份，恢复后升级到最新迁移。恢复只提供命令行，不提供网页接口。
- **备份不含主密钥**。恢复时若当前密钥文件缺少备份所需的密钥编号，会提示：资产已恢复，但设备密码无法解密，需要恢复对应的密钥文件或重新录入。
- 接口（仅管理员）：`GET /api/backups`、`POST /api/backups`（立即备份）、`GET /api/backups/{name}`（下载）。

## Worker 与任务队列（P2）

```bash
python -m app.worker        # 独立 Worker 进程（与 API 同一镜像、不同入口）
```

- 任务存在数据库 `tasks` 表：认领用一条 `UPDATE … RETURNING` 原子完成，并发 Worker 不会拿到同一个任务；
  认领时发放租约 token，完成 / 失败 / 续租都要带同一个 token，过期 Worker 回来写不进去。
- 采集任务（risk=read）失败按 30 秒起翻倍退避重试，默认最多 3 次；租约过期（Worker 崩溃）自动回收重派。
- 控制任务（risk=control，P5 才有）只执行一次：超时或租约过期标为 `indeterminate`（结果不确定），不自动重试。
- 同一 `idempotency_key` 只入队一次；结果写入前脱敏。
- SQLite 只允许一个 Worker：先拿 `scheduler_locks` 里的锁，第二个 Worker 会等待，不会并行执行。
- 桌面版和本地预览设 `YUNSHU_EMBEDDED_WORKER=1`，由 API 进程在后台线程里运行 Worker。
- 接口（三个角色可看）：`GET /api/tasks`（可按 status / kind / asset_id 筛选）、`GET /api/worker/status`。

## 指标存储（P2）

- **原始数据**：一次采集一行（同一资产的多个指标打包成紧凑 JSON），按 UTC 日期分表 `metric_raw_YYYYMMDD`，
  默认保留 48 小时，过期后整表删除（不逐行清理）。日表由代码按需创建，不归 Alembic 管。
- **分钟汇总**保留 30 天、**小时汇总**保留 1 年，压缩存放：每台资产每小时（分钟）/ 每天（小时）一行，
  里面是 60 / 24 个槽的平均、最小、最大和样本数。只汇总有效和估算的值。
- **最新值** `latest_metrics`：值或空、单位、时间、来源、质量、是否估算，以及最近一次有效值。
- **六种质量**：valid / stale / unsupported / unconfigured / failed / estimated。stale 不落库，读取时超过
  3 个采集周期没有新值即为过期；失败、不支持、未配置时值为空，界面不能显示成 0。
- Worker 定时计划：每分钟汇总一次，每小时清理一次。保留期可用 `YUNSHU_RAW_RETENTION_HOURS`、
  `YUNSHU_MINUTE_RETENTION_DAYS`、`YUNSHU_HOUR_RETENTION_DAYS` 调整。
- 维护：资产改为“维护中”时带预计结束时间 `maintenance_until`（不填默认 7 天后，改成别的状态清空；导入的维护中资产没有）。
  维护中、规划中、已退役的资产不产生新告警；过了预计结束仍是维护中时，默认规则“维护超过预计结束时间”（`asset.maintenance_overdue`）照常告警并通知，
  改回在用或延长后恢复，每日巡检报告也列出。告警在维护窗口、维护中或上游依赖抑制期间恢复的，抑制结束后补发一次恢复通知（之前发过触发通知的才补）。
- 任务优先级（`app/queue.py`）：控制 20 > 推送事件补采 5 > 采集、通知 0 > 耗时的后台任务 -10（命令巡检、软件清单、漏洞同步与匹配、AI 摘要），
  单 Worker 时大批量巡检不会把定时采集挤到过期作废。
- 每日巡检报告（`app/inspection.py`）：温度与容量按覆盖该资产的启用阈值规则判断（超过阈值用规则级别、10% 以内记警告），
  没有规则覆盖时用固定值（CPU 70 / 80 ℃，磁盘与存储池 80 / 90%）；BMC 日志一项读“服务器告警”里待处理的严重事件。
- 其他历史（`app/retention.py`，每小时 `history.prune`）：已结束超过 180 天的告警连同事件（未结束的告警只删旧周期的事件）、
  已结束的通知投递、采集以外已结束的任务、AI 用量，由 `YUNSHU_HISTORY_RETENTION_DAYS` 调整；
  审计与控制操作记录默认保留 365 天，`YUNSHU_AUDIT_RETENTION_DAYS` 调整，0 为永久保留。告警求值只读未结束告警当前周期的通知记录。
- 接口：`GET /api/assets/{id}/metrics`（最新值）、`GET /api/assets/{id}/metrics/series?key=&start=&end=&resolution=auto|raw|minute|hour`。
- 压测：`python -m app.metrics_bench --devices 100 --metrics 30 --interval 60 --days 7`（只写临时库）。

P6 持续运行与恢复演练：`python -m app.stability_bench --seconds 600 --report ../artifacts/p6-stability.json`。
只创建临时库，使用真实 Worker 和假任务；子进程直接退出后恢复读任务，过期控制任务保持结果不确定且不重发；中途关闭 Worker 与数据库引擎后同库重启。
默认 10 台 × 30 指标，每轮指标时间推进 2 小时，独立记录实际运行时长、保留期清理、库 / WAL 增长与完整性检查。
加速指标时间用于覆盖清理边界，不能替代真实设备连续运行或容量测试；P2 的 100 台、一周模拟仍使用 `metrics_bench`。

## 命令巡检（4.4 P21）

- 模板（`inspection_templates`，迁移 0032）：内置 Linux、Proxmox VE、华为 VRP、H3C Comware、Cisco IOS 五个（`app/inspection_templates.py`，
  启动时按键写入或更新，只读，未实机验证）；自定义模板只有管理员能建改删，保存时按危险命令清单拦截（删除 / 移动文件、重启关机、
  分区格式化、启停服务、结束进程、改权限与用户、写文件与重定向、装卸软件、改虚拟机 / ZFS / Ceph / PVE 集群、挂载与网络配置、
  下载执行脚本；华为 / H3C 只允许 display 类、Cisco 只允许 show 类命令，拦截进入配置模式与 reset / delete / save / undo / write / reload 等），并要输入模板名称确认，写审计（含全部命令）。
- 检查项判定：record（只记录）、empty（输出为空正常）、regex / not_regex、threshold（正则第一个捕获组取数值，gte / lte，警告与严重阈值）、
  exit0；可选 ok_if。退出码 126 / 127 或“command not found / Unrecognized command”记为“无法判断”。
- 任务（`inspection_jobs`）：单次（立即或指定时间）/ 每天 / 每周 / 每月（当月没有该日在最后一天）/ 5 段 cron 表达式（列表、范围、步长；
  日和星期都限定时满足其一即可，与标准 cron 一致），按 `TZ`（默认 Asia/Shanghai）；
  定时计划 `inspection.jobs` 每 30 秒检查到期任务。运维与管理员可建任务、立即执行，设备必须都在发起人的资产范围内。
- 执行（`inspection_runs` / `inspection_results`）：每台设备一个 Worker 任务 `inspection.asset`，只用用途为“监控 / 管理”、关联凭据的
  SSH 端点（优先“监控”），首次信任并固定主机密钥；Linux / PVE 每条命令一个 exec 通道（取退出码），华为 / H3C / Cisco 走交互式命令行，
  先关闭分页、遇到“---- More ----”发空格；单条超时（默认 30 秒）、一台设备最长 15 分钟；执行前再按危险命令清单检查一次；
  每条输出最多存 64 KB；保留 90 天（`inspection.prune`）。
- 告警：默认规则“巡检发现严重异常”（合成指标 `inspection.critical` = 每台设备各模板最近一次巡检里判定为严重的异常数），
  下一次巡检正常后恢复；警告与提示只在执行记录里。
- 接口前缀 `/api/command-inspection`：`templates`、`jobs`（含 `/{id}/run`）、`run`（临时执行）、`runs`、`runs/{id}`（按资产范围过滤）、
  `results/{id}`（含上一次同一检查项的输出）。

## 临时提频与双来源核对（4.4 P20d / P20e）

- 临时提频（`app/collect/boost.py`，`collection_boosts`，迁移 0034）：告警求值时，资产有未结束的阈值告警、或某条阈值规则的指标已在阈值 10% 以内
  （维护中、维护窗口内除外），登记提频，持续到条件最后一次成立后 10 分钟；收到推送的严重 / 警告事件（SNMP Trap、Redfish 事件）也登记，
  并立即补采一次（每资产每分钟最多一次）。提频期间每 15 秒入队一次采集，代替模板周期；同时最多 10 台（`YUNSHU_BOOST_MAX`）。
  仍受每台 BMC 排队与每分钟访问上限约束，所以主要加快 SNMP 等不限次数的来源。`GET /api/assets/{id}/collection` 的 `boost` 字段与页面显示。
- 双来源核对（`app/collect/plan.py`，`collection_plans.check / checked_at`，迁移 0035）：采集计划下，温度与功耗每天一次（以及每次探测时）
  用主来源和另一个协议各读一次比对（另一个协议只为核对读取、不写指标）。温度按传感器名称对齐（对不上时比 CPU 最高温），
  相差超过 3℃，或功耗相差超过 10%，且主来源不是 Redfish 而 Redfish 能读到时，该项固定改由 Redfish 提供并提示“该品牌 MIB 对应可能有误”。
  固定在自动探测中保留，页面“重新探测”清除。

## 每台 BMC 排队与访问上限（4.4 P20b）

- `app/bmc_gate.py`：采集、控制操作（电源、风扇、一键开启 SNMP 与推送配置）和页面上的“获取资料”按管理地址（`bmc:<地址>`，与控制互斥同一个键）
  共用一个队列，同一台 BMC 同一时刻只有一个在访问。只管 IPMI、Redfish、SNMP 端点（采集不含用途为“控制”的端点）。
- 控制优先：控制等正在进行的采集结束，最多 90 秒，等不到记为“拒绝”、不发命令；控制或获取资料进行中，这台 BMC 的采集整台本轮顺延
  （任务结果 `deferred_reason`），采集状态不变、不算失败。采集遇到另一次采集最多等 20 秒；获取资料最多等 30 秒，等不到返回 409。
- 每台 BMC 每分钟访问上限：Redfish 2 次、IPMI 1 次（`YUNSHU_BMC_RATE="redfish=2,ipmi=1"`，0 表示不限；按最近 55 秒计，吸收定时误差）。
  只限制定时采集：超出的协议本轮顺延（任务结果 `deferred`），模板方式下不改用兜底协议，采集计划下它负责的数据本轮不采、不算降级。
  控制、获取资料、采集计划的探测与页面上的“立即采集”不受限制，但都计入次数。
- SQLite（只有一个 Worker，内嵌在 API 进程或单独一个）在进程内排队；PostgreSQL 时用 `bmc_leases`（租约带过期时间：采集 10 分钟、
  获取资料 5 分钟、控制 30 分钟，进程崩溃不会永久占用）与 `bmc_accesses`（访问记录，`bmc.prune` 每小时清理 1 小时前的）跨进程排队（迁移 0033）。
  SQLite 部署若把 API 与 Worker 分成两个进程，“获取资料”与采集之间不排队。

## 服务器告警（4.4 P20c）

- BMC 系统事件日志（SEL）逐条存入 `server_events`（迁移 0030，`app/server_events.py`）：Redfish 每次采集读最近 100 条；
  采集计划里 Redfish 不可用、改由 IPMI 负责 BMC 日志时，IPMI 在低频周期（每 10 个周期）同一会话里读最近 200 条（pyghmi `get_event_log`）；
  只有 IPMI、没有 Redfish 的服务器按模板每 10 个周期读一次。都只读，从不清空 BMC 日志。
- 去重键为“来源 + 记录号 + 发生时间”（BMC 清空日志后记录号会重用）；采集计划下只存负责“BMC 日志”这一项的协议读到的条目。
- 首次读到时，近 7 天的严重 / 警告为“待处理”，更早的或提示类为“已归档”；按关键词粗分温度、风扇、电源、内存、硬盘与阵列、CPU、机箱。保留 180 天。
- 接口：`GET /api/server-events`（state、severity（含 `alarm` = 严重与警告）、category、asset_id、q、days、分页）、
  `GET /api/server-events/summary`、`POST /api/server-events/state`（运维以上；标记已处理必须写处理结果，写审计）。按资产范围过滤。
- 告警通知：默认规则“服务器出现严重硬件事件”（合成指标 `server_event.open_critical` = 资产上待处理的严重事件条数，告警求值时直接统计）。
  有待处理的严重事件就触发；已通知过又来了新的严重事件时再提醒一次（已确认的告警也提醒，静默的不提醒，受冷却时间限制）；全部标记已处理后恢复。
  它取代 4.2 P13 按 24 小时条数的“BMC 日志出现严重事件”（`sel.critical_24h`）：升级时新规则沿用旧规则的启用状态、作用范围与通知渠道，旧规则停用并写审计。

### BMC 主动推送（4.4 P20c，默认关闭）

- **SNMP Trap 接收**（`app/snmp_trap.py`）：`YUNSHU_SNMP_TRAP=1` 时由 Worker（非 executor 角色）或内嵌 Worker 起一个线程监听
  `YUNSHU_SNMP_TRAP_BIND`（默认 0.0.0.0）的 UDP `YUNSHU_SNMP_TRAP_PORT`（默认 1162），部署时把 162/udp 映射过来。
  只收 v1 / v2c 的 Trap 与 Inform（Inform 会回应）；来源 IP 必须是某台资产 SNMP / Redfish / IPMI 端点的 IP（主机名不参与），
  团体名必须与该资产端点关联的团体名凭据一致，或等于平台配置 Trap 目标时用的团体名（只存 SHA-256）。每来源每分钟最多 60 条。
  Dell iDRAC 的 Trap 取 IDRAC-MIB 的 alertMessage / alertCurrentStatus；其他厂商取文字变量、按关键词判断级别（判断不出记为警告）。
  SNMPv3 Trap 暂不支持。
- **Redfish 事件接收**（`app/api/routes_bmc_events.py`）：`POST /api/bmc-events/{令牌}`，不需要登录，凭订阅时生成的随机令牌
  认资产（库里只存哈希，认不出一律 404），请求体最大 256 KB、每次最多 100 条事件。只写服务器告警，不触发任何设备操作。
- **在 BMC 上配置推送**：并入“一键开启 SNMP”（同一任务 `control.bmc_snmp`、同一门禁：`YUNSHU_BMC_SNMP=1`、新平台负责控制、在用、
  控制 / 管理用途的 Redfish 端点与凭据、仅管理员），新增四个操作：
  - `trap_on` / `trap_off`：要求 `YUNSHU_SNMP_TRAP=1` 与 `YUNSHU_SNMP_TRAP_TARGET`（BMC 能访问到的平台 IP），且所选凭据是团体名。
    Dell iDRAC 用空闲的 `SNMPAlert.N`（已指向平台的复用，全部被占用时拒绝，不覆盖别人的），需要时打开 `IPMILan.1.AlertEnable`、
    把 `SNMP.1.TrapFormat` 从 SNMPv3 改为 SNMPv2；iDRAC 已有不同团体名时拒绝。按 Redfish 标准支持 SNMP 订阅的 BMC
    建 `SubscriptionType=SNMPTrap` 订阅；其他（HPE iLO、联想、新华三、浪潮等）拒绝并提示手动设置。
  - `events_on` / `events_off`：要求 `YUNSHU_BMC_EVENTS_URL`（https 开头）。先删除指向本平台接收地址的旧订阅再新建，
    BMC 支持时只订阅 `Alert` 类事件。超时记为“结果不确定”，令牌仍保存，订阅若已建成推送仍能被认出。
  - 结果记在 `bmc_event_subscriptions`（迁移 0031，每台资产每种一条）：接收地址、BMC 上的订阅路径或 iDRAC 告警目标序号、
    令牌 / 团体名哈希、最近收到时间与次数；`GET /api/control/assets/{id}/bmc-snmp` 的 `push` 字段返回它们。
- 推送与定时读取的 BMC 日志可能是同一件事：同一资产 2 分钟内文字相同的只存第一条。

## 一键开启 BMC 的 SNMP（4.4 P19，默认关闭）

- 部署设置 `YUNSHU_BMC_SNMP=1` 才可用；只有管理员能发起（`POST /api/control/bmc-snmp`，`GET /api/control/assets/{id}/bmc-snmp` 查看能否执行）。
  与电源控制共用门禁、任务队列（`control.bmc_snmp`）、同一 BMC 地址互斥、“结果不确定”锁定和审计。
- 通过资产上用途为“控制 / 管理”的 Redfish 端点修改 BMC 设置（`app/control/bmc_snmp.py`），先只读识别：
  Dell iDRAC 改 Manager Attributes（`SNMP.1.AgentEnable` / `AgentCommunity`，SNMPv3 打开同名 iDRAC 用户的 `Users.N.SNMPv3Enable`）；
  其他 BMC 按 Redfish 标准 PATCH `NetworkProtocol` 的 `SNMP` 对象，团体名数组只追加（原有条目以 `{}` 占位不改）。
  读不到 SNMP 设置的（联想 XCC 专用扩展、新华三、浪潮、部分 OpenBMC）直接拒绝且不写任何东西。iDRAC 已在用另一个团体名时不覆盖。
- 开启后用所选 SNMP 凭据读一次 sysName，读到才记“已查询确认”，并在资产上补一个用途为“监控”的 SNMP 端点（同地址已有的不动）。
  iLO 修改后需重启管理芯片才生效，平台不自动重启，结果记“指令已接受”。关闭为高风险操作，需输入资产名称确认，保留已有 SNMP 端点。
- 只用伪造 Redfish 测试（`tests/test_bmc_snmp.py`），未实机验证。

## 采集（P2，只读）

- 适配器接口：`probe`（测连接）、`discover`（发现接口 / 传感器）、`collect`（采集）、`capabilities`。
  采集适配器只读，控制操作是 P5 的另一套接口。
- 采集模板（`app/collect/profiles.json`，由 `app/collect/base.py` 校验加载）：每步标为 primary（首选）、fallback（首选全失败才用，指标与状态里标“降级”）、
  supplement（总是额外采集，如 Dell 的 Redfish 补充硬盘与 RAID）。资产没指定模板时按平台和端点推断。
  只有在真实设备或脱敏样本上测过的模板才标“已验证”，目前全部为“未验证”。
- A 批包含 Debian/Ubuntu、OpenWrt/ImmortalWrt/iStoreOS、标准 MIB 网络设备、Dell/iDRAC9 与通用 IPMI；B 批包含 PVE、ESXi/vCenter、Alpine/Rocky/AlmaLinux 与仅 TCP 可达性。
  模板列出指标、前提、限制和离线测试依据；Linux 类按 SNMP 首选、SSH 备用，没有 SNMP 端点时直接使用 SSH。
- **采集计划**（4.4 P20a，`app/collect/plan.py`）：Dell 与通用 BMC 模板的服务器，只要同时有两个以上 BMC 协议（SNMP / Redfish / IPMI）的非控制端点，
  就不按模板步骤执行，而是：首次采集把每个协议都读一遍（只读），记下各自真正读到的数据项（温度、风扇、功耗与电源、整机与部件健康、
  硬盘与阵列、BMC 日志、清单）；每项按偏好挑主来源与兜底（高频数据 SNMP → Redfish → IPMI，低频数据 Redfish → SNMP → IPMI），
  存入 `collection_plans`（迁移 0029）。之后每个周期只连接“某项到期数据的主来源”，只保留它负责的指标；主来源失败时它负责的数据交给下一个协议，
  指标说明里写明“降级”。低频数据每 10 个周期一次。端点变化、超过 7 天、上次探测有协议不可用（每小时）或 `POST /api/assets/{id}/collection-plan/reprobe`
  时重新探测；`GET /api/assets/{id}/collection` 的 `plan` 字段给出计划。计划里不负责任何数据的协议不再连接，状态记为“跳过”并说明原因。
- 通用 BMC 模板（4.4 P20a）改为 Redfish 首选、SNMP 补充、IPMI 兜底；只有 Redfish 端点的服务器也归入通用 BMC。
- 首选失败按 2/4/8 个周期冷却（最多 10 分钟），状态接口返回 `retry_after`；“立即采集”明确提前重试。
  `Task.status=succeeded` 只表示处理函数完成，实际协议失败与降级见 `result.failed` / `degraded` 和端点状态，页面分别展示。
- 某个来源采集失败时，它以前报过的指标写成 failed（值为空），不显示旧值或 0。
- 首次信任并固定：第一次连接记下 TLS 证书指纹 / SSH 主机密钥，之后不一致就拒绝，状态为 untrusted；
  确认后用 `POST /api/endpoints/{id}/trust/reset` 清除重新信任。
- Worker 每 15 秒检查一次，按模板周期（默认 60 秒）为在用资产入队 `collect.asset`，同一周期只入队一次；
  采集任务成功的保留 1 天、失败的保留 7 天。
- 接口：`GET /api/collection/profiles`、`GET /api/assets/{id}/collection`、`POST /api/assets/{id}/collect`（立即采集）。
- **SNMP**（`app/collect/snmp.py`）：只发 GET / GETBULK。系统信息、IF-MIB 接口状态与 64 位流量速率（首次采集只有计数器）、
  HOST-RESOURCES CPU / 内存 / 磁盘，缺少时用 UCD-SNMP。设备不提供的表标为 unsupported。
  凭据：团体名（v2c）或 SNMPv3（密钥为 JSON：auth_key、priv_key、auth_protocol、priv_protocol）。
- **SSH**（`app/collect/ssh.py`）：一次登录执行一组固定的只读命令（/proc、/sys、df、hostname），BusyBox 上同样可用；
  运行时间、负载、CPU、内存、交换区、文件系统、接口速率与错误、温度。先比对主机密钥，一致或首次才发送密码。
  凭据：用户名 + 密码，或 SSH 私钥（Ed25519 / ECDSA / RSA，不支持带口令的私钥）。
- **IPMI**（`app/collect/ipmi.py`）：pyghmi 读传感器与电源状态，失败回退 `ipmitool sdr elist full`（仅当主机装有 ipmitool；4.0 镜像自 beta.1 起不再包含 ipmitool）（密码经环境变量传，
  不出现在命令行）。风扇只记实测 RPM，不知道额定转速时不换算百分比。会话复用 `YUNSHU_IPMI_SESSION_REUSE=1` 才开启（未实机验证）。
  旧版在 P5 之前也在轮询同一批 BMC，注意 BMC 同时会话数。
- **Redfish**（`app/collect/redfish.py`）：iDRAC9 等。复用会话令牌，401 时重新登录；先比对证书指纹再发送密码；
  系统健康、电源、内存、温度、风扇（RPM 与 BMC 报告的百分比分开存）、整机功耗、硬盘健康、RAID 卷健康。
- **PVE**（`app/collect/pve.py`）：REST API（8006）。推荐 API 令牌（用户名 `user@realm!tokenid`，只给 PVEAuditor），也支持用户名密码换票据。
  节点 CPU / 内存 / 负载 / 运行时间 / 根分区、存储使用率、虚拟机与容器运行数和各自状态。
- **ESXi / vCenter**（`app/collect/esxi.py`）：pyVmomi 只读属性。主机 CPU / 内存 / 运行时间 / 连接状态、数据存储、虚拟机电源状态。
  没有模拟器做协议测试，只测了数据映射，需要实机验证。
- **LLDP**：SNMP 采集保存本地端口、远端端口、远端名称和 chassis 观察证据；邻居表不可读时不丢弃主机指标。
  观察保存在采集事实中，不直接创建已确认链路；拓扑重新扫描据此生成候选（见下文“拓扑”）。

## 网段发现（P2）

- `POST /api/discovery/preview`：校验与展开范围，不联网、不建任务。
- `POST /api/discovery/runs`：显式授权后入队；`GET /api/discovery/runs` / `GET /api/discovery/runs/{id}` 查看进度、候选与处理记录。
- 输入 `scope`、`excludes`（IPv4 地址 / CIDR 列表）、`protocol=tcp|snmp`、`ports`、可选 `site_id`，SNMP 需要 `credential_id`。
  每次最多 256 个地址、4 个 TCP 端口或 1 个 SNMP UDP 端口；并发 1–16，超时 0.2–2 秒。
  TCP 只建立连接不发送凭据，SNMP 只查询系统名称和描述，不做口令猜测、写命令或自动扩大范围。
- `POST /api/discovery/runs/{id}/state`：`action=pause|resume|cancel`，必须带当前 `generation`；过期状态返回 409。
  分批执行并落库；等待中的批内地址会检查暂停与租约，有界的在途连接结束后放弃旧代结果。
  Worker 重启通过持久队列恢复；租约丢失后不写观察或进度。
- `POST /api/discovery/observations/{id}/confirm`：`action=create|link|ignore`。
  候选不会自动变成资产；重复确认返回 409。`link` 仅关联观察记录，不修改已有字段、端点或人工关系。
  `create` 需要确认名称、形态、角色、端口与协议；已有同 IP 资产时必须显式传 `allow_duplicate=true` 才能新建独立资产。
- TCP 端口开放不能证明服务或设备类型，默认协议为“其他”、采集模板为“仅 TCP 可达性”。SNMP 确认创建时沿用只读凭据。
  没有响应仅表示本次探测没成功，不标记为设备离线，也不删除已有资产。
- 三个角色可以查看；开始、暂停、取消与处理候选仅管理员 / 运维可执行，写操作有 CSRF 与审计。

## 拓扑（P3）

- 三个平面分开存：**观察**是采集事实里的 LLDP 邻居表（`collection_status.facts`，只读）；**链路** `network_links`；
  **视图** `topology_views`（布局与“移出视图”）。迁移 0010 预置“家庭网络”（家用）和“机房”两个空视图。
- 链路 `origin`：discovered（扫描生成）/ confirmed（扫描生成后人工确认）/ manual（人工新建）；`state`：candidate / confirmed / rejected。
  确认和人工链路总是 `locked`；已忽略的链路作为记录保留，重扫不再提出同一对设备。
- 候选生成：只用 SNMP 采集已保存的 LLDP 邻居表，不连接设备。远端名称与资产名称、采集到的 sys_name / hostname 比对
  （不区分大小写，可去掉域名后缀）；**不按 IP 匹配或合并资产**，远端名称是 IP、同名多台资产、没有对应资产时列为“未匹配”。
  同一对设备只生成一条候选（多条并行链路的端口用逗号分隔），证据里记下双方上报的端口与名称。
- 增量合并：只新增、刷新或移除 discovered + candidate 且未锁定的链路；人工 / 已确认 / 已忽略链路和所有视图布局不动；
  两台资产之间已有人工或已确认链路时不再提候选。候选的上报设备全部这次成功读到邻居表、却都没再报告它，才移除；
  有上报设备没读到（采集失败、不支持）的保留不动。预览返回 `token`，应用时结果已变化返回 409，需重新预览。
- 视图：`layout` 为 `{资产 ID: {x, y, locked}}`，`hidden` 为移出视图的资产 ID；PATCH 必须带读取时的 `revision`，
  不一致返回 409（中文说明含当前修订号与更新者）。移出视图、删除视图都不删资产和链路；删除资产时其链路随之删除。
  视图可指定场所（含下级场所）只显示其中的资产，节点带场所、机柜与 U 位。

| 方法与路径 | 说明 |
| --- | --- |
| `GET/POST /api/topology/views`，`GET/PATCH/DELETE /api/topology/views/{id}` | 视图；PATCH 带 `revision`，可改 name / kind / site_id / layout / hidden |
| `GET /api/topology/graph?view_id=` | 画布数据：节点（资产摘要）、链路（含已忽略）、资产关系（承载 / 安装于等，只读） |
| `GET /api/topology/links?state=&asset_id=`，`POST /api/topology/links` | 链路列表；新建人工连线（两端资产、可选端口、类型 ethernet / fiber / wifi / virtual / other） |
| `PATCH/DELETE /api/topology/links/{id}` | 只能修改 / 删除人工连线，其余返回 409 |
| `POST /api/topology/links/{id}/confirm` · `POST /api/topology/links/{id}/reject` | 确认（候选或已忽略 → 已确认）· 忽略（人工连线不能忽略） |
| `POST /api/topology/rescan/preview` · `POST /api/topology/rescan/apply` | 差异预览（新增 / 更新 / 移除 / 未变化 / 跳过 / 未匹配，不写库）· 按 `token` 应用 |

三个角色都能查看视图、画布与链路；修改、确认 / 忽略、重新扫描仅管理员 / 运维，写操作有 CSRF 与审计（`topology.*`）。

## 实时推送（P3，SSE）

- `GET /api/events`（三个角色可读；未登录 401、未改初始密码 403）返回 `text/event-stream`，实现在 `app/events.py`。
- Worker 可能是独立进程，所以不靠进程内消息：每个连接约每 2 秒查一次数据库，推送水位之后的变化：
  - `metrics`：`{asset_id, items: [{key, value, quality, unit, observed_at, interval}]}`，按资产分组；过期在推送时按同一规则判断，失败等状态值为空；
  - `collection`：端点采集状态 `{endpoint_id, asset_id, protocol, state, error_kind, consecutive_failures, last_attempt_at, last_success_at}`，
    新连接先发一次全部端点（`snapshot: true`，最后一个分片 `final: true`），之后只发变化；
  - `tasks`：`{counts: {状态: 数量}}`，计数变化时才发；`bye`：服务端主动关闭（`reconnect` 或 `unauthorized`）。
- 不推送 message、facts、管理地址和任何凭据；单个事件不超过 32 KB，超出拆成多个事件。
- 事件 id 是水位（`m<微秒>.c<微秒>`），只写在每轮最后一个事件上；浏览器断线重连时带 `Last-Event-ID`（或查询参数 `last_event_id`）续传，
  最多重发一轮，不会漏。每轮回看 10 秒，防止 Worker 晚提交的旧时间戳被跳过，已发过的不重发。
- 首行 `retry: 3000`；空闲时每 15 秒一行 `: ping` 心跳；每 30 秒重新确认会话（退出登录或被禁用后发 `bye` 并断开，不续期会话）；
  一条连接最多 15 分钟后由服务端关闭，浏览器自动重连。客户端断开时流立即停止。
- 查询都很小（latest_metrics 每资产每指标一行、collection_status 每端点一行、任务计数走状态索引），不需要新增索引或迁移。

## Docker 镜像（4.0）

- `docker/Dockerfile.ops`：先用 node 20 构建前端，再装到 Python 3.11 运行镜像；API 在 8080 同时提供接口和页面
  （`YUNSHU_FRONTEND_DIR=/app/frontend`）。构建目标为 linux/amd64 与 linux/arm64，基础镜像按多架构索引摘要固定，apt 走 Debian 快照，Python 依赖按哈希安装，以非 root 用户运行。
  前端在构建主机上生成静态文件；运行阶段选择目标架构。两种架构共用 `requirements.lock`，CI 分别构建并验证 API、首次登录、密钥分卷和 Worker 启动。
  两个架构的冒烟测试全部通过后，已有标签发布流程才会推送多架构镜像；PR 与手动工作流运行不推送。已发布的 alpha.3 仍只有 amd64，本改动不补发或移动已有版本标签。
- 入口：`api`（默认）、`worker`、`migrate`、`backup`、`keytool`、`legacy_import`；同一镜像起两个容器即 API + Worker。
- 卷：`/data`（数据库与备份）和 `/etc/yunshu`（主密钥）必须分开，入口会检查。主密钥卷要单独备份。
- 部署模板：`deploy/lite`、`deploy/standard`、`deploy/enterprise`，说明见 `deploy/README.md`。镜像公开，不需要登录。
- 发布：`.github/workflows/ops-image.yml`。PR 只构建并做冒烟测试（健康接口、页面、初始管理员、密钥不在数据卷、Worker 能启动）；
  推送 `v*.*.*` 标签时推送到 `ghcr.io/kongbai9420/yunshu:<版本>` 和 `alpha` / `beta` 别名。

## 总览仪表盘（P3）

- `GET /api/dashboard/layout`、`PUT /api/dashboard/layout`（运维以上；带读取时的 `revision`，不一致返回 409 和当前版本，
  不静默覆盖）。所有人共用一份布局，按 lg / md / sm 断点分别保存，校验小组件不重复、不超出列数。
- `GET /api/dashboard/summary`：资产采集状态分布、当前告警与最近采集问题、CPU 温度最高、功耗（当前与近 1 小时按分钟合计）、
  指标质量分布、关注列表。全部来自真实采集状态与最新指标；失败、过期、不支持的值不计入排行、不当作 0。

## 资产资料获取（P2 试用反馈）

`POST /api/asset-information/preview`，管理员 / 运维、CSRF 与脱敏审计。请求二选一：`{endpoint: <与新增资产相同的连接信息>}`（支持 `new_credential` 或已有 `credential_id`），或 `{endpoint_id: <已有管理端点 UUID>}`。

只读连接所选的 SSH / SNMP / IPMI / Redfish / PVE API / ESXi API，返回 `fields` 中实际取得的 `name / platform / vendor / model / serial_no` 及所见 SSH / TLS 指纹；不会创建资产 / 端点 / 凭据，不会更新资产资料或指标。缺失、占位、过长资料省略，空结果明确提示，异常不回显原文或秘密。

前端逐项对比及勾选后，将所选字段带入表单，再走原创建 / 带 revision 的 PATCH 接口保存；后台定时采集不自动覆盖人工资产资料。新增端点可随创建提交 `ssh_host_key / tls_fingerprint`，保存本次已读取的指纹；已有端点仍核对原固定值。

SSH 读取固定只读资料命令（序列号可能需设备权限）；SNMP 只映射唯一 chassis ENTITY-MIB；IPMI 读取 System FRU；Redfish 多 ComputerSystem、PVE 多节点或 vCenter 不擅自挑选一台主机的资料。真实兼容性仍待设备联调。

## 告警与通知（P4）

`app/alerting.py` 只读持久采集证据，不联网、不提交事务。Worker 每 15 秒评估：

- 阈值只用新鲜有效、非估算的新样本；按连续次数 / 持续时间触发，用恢复阈值结束。相同观测不因重复调度多计数。
- 默认五条旧指标规则及一条三次连续连接失败规则；旧设置在首次启动或后续 Worker 评估时单次迁入，不覆盖人工修改。
- 可达性需要全部非控制端点均为 timeout / unreachable；认证、缺凭据、信任失败、过期均不能证明设备离线。任一新鲜成功可证明可达。
- 确认、静默、维护、资产状态、`depends_on` 与基础上游网络抑制；候选链路不作为依赖。规则禁用 / 移出范围只关闭实例，不虚构恢复。
- 当前实例按规则 / 资产 / 指标保存；每次触发增加 episode；历史事件带当时的值与阈值快照，规则编辑不改历史。

| 接口 | 说明 |
| --- | --- |
| `GET /api/alerts/summary`、`GET /api/alerts` | 数量与当前实例；支持 `state`（含单独的 `suppressed`）、`asset_id`、`q`、`limit / offset` |
| `GET /api/alerts/events` | 持久历史事件，支持 `alert_id`、`q` 与分页 |
| `POST /api/alerts/{id}/action` | `action=acknowledge / silence / unsilence`，静默可传 `minutes` |
| `GET/POST /api/alerts/rules`、`PATCH/DELETE /api/alerts/rules/{id}` | 规则；PATCH 必须带 revision，冲突 409；删除停用并保留历史 |
| `GET/POST /api/alerts/channels`、`PATCH/DELETE /api/alerts/channels/{id}` | 通知渠道；PATCH 带 revision，秘密不回显，删除保留投递历史 |
| `POST /api/alerts/channels/{id}/test` | 向保存的目标排队测试，返回 202，不是同步送达 |
| `GET /api/alerts/deliveries`、`POST /api/alerts/deliveries/{id}/retry` | 投递记录及手动重试；不确定结果需 `confirm_uncertain=true` |
| `GET/POST /api/alerts/maintenance`、`PUT/DELETE /api/alerts/maintenance/{id}` | 带时区的维护窗口；资产 / 标签可限定范围；都不选时必须带 `all_assets: true` 才对全部资产生效。生命周期为维护中、规划中、已退役的资产不产生新告警，已有的保留并抑制通知 |

读取允许三种角色，写操作只允许管理员 / 运维并校验 CSRF、记录审计。规则和维护中空资产列表表示全部，资产与标签同时指定时取交集；空渠道列表表示全部启用且路由匹配的渠道。空严重程度列表表示不限。

渠道类型：`email / custom_webhook / serverchan / dingtalk / feishu / wechat_work / bark / pushplus`。
邮件 `config` 使用 `host / port / tls=ssl|starttls / from_address / recipients`；通用路由与聚合字段为
`severities / tags / group_seconds`（默认 30 秒，最少 10 秒）。`subject_template / text_template / html_template`
只接受声明的简单占位符，拒绝属性、下标与格式表达式，HTML 的替换值会转义。

秘密另传 `secret` 对象（`url / secret / username / password`），通过原 AES-GCM 凭据机制保存：SMTP 使用 username/password，webhook 使用 URL 或 SendKey/Token，钉钉 / 飞书可选签名密钥。PATCH 省略或留空的秘密字段保留原值，任何读取都只返回 `has_secret`。
所有渠道默认停用；启用只作用于后续事件，持续告警可配重复提醒；规则与渠道均可按资产标签或严重程度路由。

`app/notification_queue.py` 使用持久聚合意图和 `notification.send` 任务，网络操作不持数据库事务。
每个渠道每聚合窗口最多创建一批，最多 500 条事件合一条消息，正文展开前 20 条。
发送前重查当前告警 / 周期 / 路由 / 静默 / 维护 / 渠道修订，过时通知记 cancelled。
已知连接或服务暂时失败最多重试 3 次（30 / 60 秒退避）；失租 / 提交后超时记 uncertain，不自动重发。
accepted 仅表示 SMTP / 推送服务端接受；部分 SMTP 收件人拒绝不会重发已接受部分。传输仅使用验证证书的 TLS，HTTP 不重定向、响应限 32 KB、超时 10 秒；日志与错误不含秘密或原响应。

API 与 Worker 的测试可注入假传输：`build_handlers(settings, notification_sender=...)`，无生产环境关闭证书校验或假投递开关。
实际 SMTP / 机器人服务端仍需配置后联调。

