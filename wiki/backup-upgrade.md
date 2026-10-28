# 备份、升级与迁移

命令在各平台的写法（`yunshu` / `yunshu-entrypoint` / `cli\yunshu.exe`）见 [配置项参考](configuration.md)。下面以 `yunshu` 为例。

## 平台备份

- 轻量版（SQLite）：在线复制数据库文件，服务运行中也能得到一致的快照，备份后做完整性检查。
- PostgreSQL：调用 `pg_dump`，写完用 `pg_restore --list` 校验可读。Docker 镜像自带 17 版客户端；Linux / Windows 需要本机装有**大版本不低于服务器**的 `pg_dump` / `pg_restore`（客户端比服务器新时，恢复还需要 `psql`），不在 PATH 里时设置 `YUNSHU_PG_BIN_DIR`。
- 默认每 24 小时自动备份一次（`YUNSHU_BACKUP_INTERVAL_HOURS`，`0` 关闭），保留最近 7 份；手动、升级前、恢复前的备份各保留最近 10 份。
- 位置：数据目录下的 `backups/`（`YUNSHU_BACKUP_DIR` 可改）。每份备份旁有一个 `.json`，记录时间、原因、数据库版本、SHA-256 和所需的主密钥编号。
- 管理员可以在“平台设置 → 备份”手动备份和下载。**恢复只能用命令行。**
- 备份和服务器在同一块盘上不算离机备份，请定期把 `backups/` 复制到别的机器。

```bash
yunshu backup create                  # 立即备份
yunshu backup list                    # 列出备份
yunshu backup verify <备份文件>        # 校验完整性
yunshu backup restore <备份文件> --yes # 恢复（必须先停止服务）
```

## 主密钥

设备密码、通知渠道密钥、AI 接口密钥等都用**主密钥**（AES-256-GCM）加密后存进数据库。主密钥是一个单独的文件，**不在任何备份里**。

- 丢了主密钥：备份仍能恢复资产，但已保存的设备密码等都要重新录入。
- 主密钥不能放在数据目录里（会拒绝启动）。这样即使数据库备份泄露，密码也无法解密。
- 安装后**立刻导出一份**，放在与数据备份不同的地方（不同的移动硬盘、保险库）。

```bash
yunshu keytool path                    # 显示密钥文件位置
yunshu keytool fingerprint             # 当前密钥编号，用来核对备份与密钥是否配套
yunshu keytool export <目标文件>        # 导出（目标文件不能已存在）；不会把密钥打印到屏幕
```

| 平台 | 主密钥位置 | 导出示例 |
| --- | --- | --- |
| Docker | 卷 `yunshu-keys` 里的 `/etc/yunshu/master.key` | `docker exec yunshu yunshu-entrypoint keytool export /tmp/master.key`，再 `docker cp` 出来并删除容器里的副本 |
| Linux | `/etc/yunshu/keys/master.key` | `sudo cp -a /etc/yunshu/keys /安全位置/yunshu-keys-$(date +%F)` |
| Windows 服务版 | `C:\ProgramData\Yunshu\keys\master.key` | `yunshu keytool export D:\safe-place\yunshu-master.key` |
| Windows 免安装包 | `%APPDATA%\yunshu\master.key` | 在 `cli` 目录 `.\yunshu.exe keytool export D:\yunshu-desktop.key` |

## 恢复

1. 停止 API 与 Worker（`docker compose stop`、`sudo systemctl stop yunshu-api yunshu-worker`、`Stop-Service Yunshu`，或关闭免安装包窗口）。
2. 确认主密钥是备份时用的那一把（`keytool fingerprint` 与备份 `.json` 里的编号一致）。换机器恢复时先把导出的密钥放回原位置。
3. 执行 `yunshu backup restore <备份文件> --yes`。恢复前会自动把当前数据库另存一份，恢复后自动升级到当前版本的结构。
4. 启动服务，登录检查资产和凭据是否正常。

## 升级版本

**升级前**：先 `yunshu backup create`，并确认主密钥已经单独导出。

| 平台 | 怎么升级 |
| --- | --- |
| Docker 轻量版 | 把 `image:` 改成新版本，`docker compose pull && docker compose up -d`（NAS 图形界面里是“停止 → 重新创建 → 启动”）。启动时先自动备份，再升级数据库 |
| Docker 标准版 / 企业版 | 同上。模板里的 `migrate` 服务先升级数据库，成功后 API 和 Worker 才启动 |
| Linux 安装包 | 解开新版本压缩包，`sudo ./install.sh`。新版装到 `/opt/yunshu/<新版本>/`，旧版目录保留；之前在运行的服务自动重启。PostgreSQL 时脚本先运行迁移，失败会自动切回旧版本 |
| Windows 服务版 | 直接运行新版本的 MSI。配置、数据、主密钥、日志全部保留；SQLite 启动时自动备份并升级；PostgreSQL 请先运行 `yunshu migrate` 再启动服务 |
| Windows 免安装包 | 解压到新目录，用 `YUNSHU_DATA_DIR` 指向原来的数据目录，主密钥路径不变；确认新版正常前保留旧目录 |

- SQLite：启动时发现结构落后，会先生成 `pre-migrate` 备份再自动升级。
- PostgreSQL：启动时只检查版本，不一致时拒绝启动，必须先运行 `yunshu migrate`（容器里 `yunshu-entrypoint migrate`）。单实例部署也可以设 `YUNSHU_AUTO_MIGRATE=1` 让它自动升级。

## 回滚

**升级后的数据库不能直接给旧版本用**。回滚要用**旧版本**恢复升级前的备份：

1. 停止新版本的服务。
2. 换回旧版本：Docker 把 `image:` 改回旧标签；Linux 运行 `sudo /opt/yunshu/<旧版本>/install.sh`；Windows 先卸载新版本（数据保留）再装旧版本 MSI。
3. `yunshu backup list` 找到升级时生成的 `pre-migrate` 备份，`yunshu backup restore <文件> --yes`。PostgreSQL 用升级前的 `.dump` 备份。
4. 启动服务。

各平台的完整命令见 [Linux 安装说明](https://github.com/kongbai9420/YunShu/blob/main/deploy/linux/README.md) 和 [Windows 安装说明](https://github.com/kongbai9420/YunShu/blob/main/deploy/windows/README.md)。

## 从 4.0.0-beta.1 升级到 4.4

两处变化：

- **镜像名变了**：新镜像是 `ghcr.io/kongbai9420/yunshu`（原来是 `ghcr.io/kongbai9420/yunshu-ops`），`image:` 写 `ghcr.io/kongbai9420/yunshu:4.4.0-beta.1`。
- **卷名变了**：如果你用过 4.0 的 `docker/docker-compose.ops.yml` 示例，它没有固定卷名，Docker 按“目录名_卷名”命名（例如 `docker_yunshu-data`、`docker_yunshu-keys`）。现在的模板卷名固定为 `yunshu-data`、`yunshu-keys`。**直接换新模板会挂上两个空卷，看起来像数据没了**（旧数据还在旧卷里）。

步骤概要：

1. 在旧目录先备份数据库，并导出主密钥。
2. `docker volume ls --filter name=yunshu` 查出旧卷名。
3. 在旧目录 `docker compose down` 停掉旧服务（**不要**加 `-v`，否则会删掉卷）。
4. 二选一：在新模板里把卷的 `name:` 改成旧卷名直接沿用；或把旧卷复制到新卷名（旧卷保留，可以退回）。
5. 用新模板 `docker compose up -d`，启动时会先自动备份再升级数据库。
6. 登录核对资产数量，打开一台设备的凭据确认能解密；都正常后再决定是否删除旧卷。

完整命令见 [Docker 部署指南](https://github.com/kongbai9420/YunShu/blob/main/deploy/README.md) 的“部署之后 → 从 4.0.0-beta.1 升级”。用 `docker run` 示例部署的，卷名本来就是 `yunshu-data` / `yunshu-keys`，不受影响。

## 从轻量版迁到 PostgreSQL

资产变多、多人使用后，可以把 SQLite 数据整体迁到 PostgreSQL：

```bash
yunshu upgrade_to_pg precheck --target "postgresql+psycopg://yunshu:fake-password@db.example.invalid:5432/yunshu"   # 只读预检
yunshu upgrade_to_pg run --target "postgresql+psycopg://yunshu:fake-password@db.example.invalid:5432/yunshu" --yes  # 执行
```

- 目标必须是**空库**（PostgreSQL 16 或 17）；源 SQLite 只读，始终保留。
- 服务必须已停止；有执行中的任务或未完成的控制操作时会拒绝。执行前会先备份。
- 在一个事务里复制全部数据，然后核对行数、抽样内容和凭据解密；任何一步失败都整体回滚，修正后可以重跑。
- 完成后把 `YUNSHU_DATABASE_URL` 指向 PostgreSQL（或改用标准版模板），再启动。Linux 还要设 `YUNSHU_EMBEDDED_WORKER=0` 并启用 `yunshu-worker`、`yunshu-migrate`。
- 不支持从 PostgreSQL 迁回 SQLite。Windows 免安装包没有 PostgreSQL 驱动，请先迁到服务版。

## 从 3.x 旧版导入

`legacy_import` 读取 3.x 的 `config.ini` 和 `users.json`，导入到 4.x：

```bash
yunshu legacy_import --config <旧版目录>/config.ini --users <旧版目录>/src/users.json           # 预检：完整走一遍后回滚，只出报告
yunshu legacy_import --config <旧版目录>/config.ini --users <旧版目录>/src/users.json --apply   # 确认导入
```

- 只读旧文件，导入前后文件内容不变。
- 导入内容：物理服务器（品牌、型号、序列号）+ IPMI 端点 + 加密凭据；Linux 服务器 + SSH 端点 + 加密凭据，以及与宿主的承载关系；用户（密码哈希原样带过来，旧密码照常能登录）。
- 旧的温控曲线、温控模式、告警设置一起迁入；Webhook 地址与密钥不保存，需要在通知渠道里重新填写。
- 可以重复执行：旧记录没变就跳过；新平台改过的列为冲突、不覆盖；新平台删掉的不重建。
- 加 `--json` 输出机器可读的报告。
- Docker 部署时先把旧文件放进容器能读到的位置（例如 `docker cp`），再用 `yunshu-entrypoint legacy_import ...`。

导入后控制归属仍是“旧版控制器”。确认旧版已经停止、不会再向 BMC 发命令后，再在“运维操作 → 控制归属”切换，见 [运维操作](operations.md)。
