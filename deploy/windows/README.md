# Windows 服务版（MSI）

[English](README.en.md) · [返回部署指南](../README.md)

在 Windows 上把云枢作为**系统服务**运行：开机自动启动、不用登录也在运行，局域网里的其他电脑用浏览器访问 `http://<这台电脑的 IP>:8080`。默认使用 SQLite，也可以连接外部 PostgreSQL。

只给自己一个人在本机用、不想装服务的，用[免安装 ZIP（桌面版）](../../scripts/desktop-guide.md)即可。

## 要求

- 64 位（amd64）的 Windows Server 2019 / 2022 / 2025，或 Windows 10 / 11。不支持 32 位与 ARM 版 Windows。
- 安装与维护命令需要管理员权限。
- 不需要另装 Python、Node 或 .NET；页面用浏览器访问，服务器上不需要 WebView2。
- 不附带 ipmitool；采集与控制使用 Redfish / IPMI 的内置实现。

## 安装

从发布页下载 `Yunshu-<版本>-windows-amd64.msi` 和同名的 `.sha256`，先核对校验值：

```powershell
(Get-FileHash -Algorithm SHA256 .\Yunshu-4.4.0-beta.2-windows-amd64.msi).Hash.ToLower()
Get-Content .\Yunshu-4.4.0-beta.2-windows-amd64.msi.sha256
```

> [!NOTE]
> 安装包暂时**没有代码签名**。双击时 Windows SmartScreen 可能提示“Windows 已保护你的电脑”，确认校验值与发布页一致后，点“更多信息 → 仍要运行”。发布页提供 SHA-256；附有 GitHub 构建来源证明（provenance）时，可以用 `gh attestation verify <文件> --repo kongbai9420/YunShu` 核对文件由本仓库的工作流构建。

**图形界面**：双击 MSI，按提示安装。“自定义安装”页面可以修改程序目录，并可勾选“Windows 防火墙规则（TCP 8080 入站）”（默认不勾选）。

**静默安装**（管理员 PowerShell / 命令提示符）：

```powershell
msiexec /i Yunshu-4.4.0-beta.2-windows-amd64.msi /qn /l*v install.log
# 同时添加防火墙规则：
msiexec /i Yunshu-4.4.0-beta.2-windows-amd64.msi /qn ADDLOCAL=ALL /l*v install.log
```

安装程序做的事：

| 内容 | 位置 / 设置 |
| --- | --- |
| 程序 | `C:\Program Files\Yunshu\`（`yunshu.exe` 加入系统 PATH） |
| 配置文件 | `C:\ProgramData\Yunshu\yunshu.env`（只在不存在时写入默认内容，升级不会覆盖） |
| 数据库与自动备份 | `C:\ProgramData\Yunshu\data\` |
| 主密钥 | `C:\ProgramData\Yunshu\keys\master.key`（服务首次启动时生成） |
| 日志 | `C:\ProgramData\Yunshu\logs\`（每个文件 10 MB，保留 5 个） |
| 服务 | 名称 `Yunshu`，显示名“云枢运维平台”，自动（延迟）启动，失败后 10 秒 / 30 秒 / 60 秒自动重启 |
| 开始菜单 | “打开云枢运维平台”（浏览器打开 `http://localhost:8080/`）、“云枢命令行” |

安装完成后服务会自动启动（首次启动要初始化数据库，可能需要十几秒）。用浏览器打开 `http://localhost:8080`，以 `admin` / `admin123` 登录，**按提示立即修改密码**。

### 服务账号与权限

服务以虚拟账号 **`NT SERVICE\Yunshu`** 运行：它是 Windows 为每个服务自动提供的专用低权限账号，不需要密码、不能交互登录，权限只够访问上面这些目录；不像 LocalSystem 那样拥有整台机器的权限，也不像 LocalService 那样与其他服务共用同一个账号（那样别的服务也能读主密钥）。

`C:\ProgramData\Yunshu` 不继承 ProgramData 默认给普通用户的权限：

| 目录 | SYSTEM、Administrators | `NT SERVICE\Yunshu` | 普通用户 |
| --- | --- | --- | --- |
| `Yunshu\`（含 `yunshu.env`） | 完全控制 | 只读 | 无 |
| `data\`、`logs\` | 完全控制 | 修改 | 无 |
| `keys\`、`master.key` | 完全控制 | 完全控制 | 无 |

`yunshu.env` 里可能写有 PostgreSQL 口令，所以普通用户不能读。可以用 `icacls C:\ProgramData\Yunshu\keys\master.key` 查看。

## 防火墙

Windows 防火墙默认阻止其他电脑访问 8080。安装时勾选防火墙功能（或静默安装加 `ADDLOCAL=ALL`）会添加入站规则“云枢运维平台 (TCP 8080)”，卸载时一并删除。默认不勾选，是为了不在你不知道的情况下对外开放端口，也方便只在本机或经反向代理访问的部署。

不想通过安装程序管理时，也可以自己添加（管理员 PowerShell）：

```powershell
New-NetFirewallRule -DisplayName "云枢运维平台 (TCP 8080)" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
```

改了 `YUNSHU_PORT` 时，请放行对应端口（安装程序的规则固定为 8080）。

## 配置

编辑 `C:\ProgramData\Yunshu\yunshu.env`（需要管理员权限，例如以管理员身份运行记事本），保存后重启服务：

```powershell
Restart-Service Yunshu
```

默认内容：

```ini
YUNSHU_DATA_DIR=C:\ProgramData\Yunshu\data
YUNSHU_MASTER_KEY_FILE=C:\ProgramData\Yunshu\keys\master.key
YUNSHU_LOG_DIR=C:\ProgramData\Yunshu\logs
YUNSHU_EMBEDDED_WORKER=1
YUNSHU_SETUP_WIZARD=0
YUNSHU_PORT=8080
```

文件里还有注释掉的示例：PostgreSQL（`YUNSHU_DATABASE_URL`、`YUNSHU_PG_BIN_DIR`）、时区 `TZ`（默认跟随本机时区）、反向代理 `FORWARDED_ALLOW_IPS`、HTTPS 下的 `YUNSHU_SECURE_COOKIES=1`。只想本机访问时加 `YUNSHU_BIND=127.0.0.1`。其余变量的含义见 [backend/README.md](../../backend/README.md)。

## 命令行维护

开始菜单“云枢命令行”**右键 → 以管理员身份运行**，或在管理员 PowerShell 里直接使用 `yunshu`（已在 PATH 中；安装前就打开的窗口需要重新打开）：

```powershell
yunshu version
yunshu backup create            # 立即备份（服务运行中也可以）
yunshu backup list
yunshu keytool fingerprint      # 当前主密钥编号
yunshu worker --check           # 后台 Worker 是否在运行
```

命令读取同一个 `yunshu.env`，所以不需要额外参数。主密钥与配置只允许管理员访问，非管理员窗口会提示权限不足。

> [!IMPORTANT]
> 第一次安装后请等服务启动过一次（页面能打开）再运行维护命令，让主密钥由服务账号创建。如果先用管理员账号运行了会生成密钥的命令，`master.key` 的权限会只给那个管理员，服务就读不到；这时删除 `keys\master.key`（尚未保存任何设备凭据时）后重启服务即可。

## 备份与主密钥

- 自动备份：每 24 小时一次，放在 `C:\ProgramData\Yunshu\data\backups\`；升级数据库结构前也会自动备份。
- **备份不含主密钥**。设备密码等凭据用主密钥加密，丢了密钥这些凭据就无法解密。安装后请立刻单独导出一份，放到这台电脑以外的地方：

  ```powershell
  yunshu keytool export D:\safe-place\yunshu-master.key
  ```

- 把 `data\backups\` 与导出的密钥分开保存（例如不同的移动硬盘或保险库）。
- 恢复：

  ```powershell
  Stop-Service Yunshu
  yunshu backup restore C:\ProgramData\Yunshu\data\backups\yunshu-<时间>-manual.db --yes
  Start-Service Yunshu
  ```

  换了一台电脑恢复时，先安装、停止服务，删除新生成的 `C:\ProgramData\Yunshu\keys\master.key`，把导出的密钥复制成这个文件（新文件继承 keys 目录的权限，只有 SYSTEM、Administrators 与服务账号可以访问），再恢复数据库。

## 使用外部 PostgreSQL（标准版）

适合资产多、多人同时使用。需要你自己准备 PostgreSQL 服务器（16 或 17），以及备份用的 [PostgreSQL 官方 Windows 客户端](https://www.postgresql.org/download/windows/)（`pg_dump` / `pg_restore`，大版本不低于服务器；安装时只选 Command Line Tools 即可）。

新装、还没有数据：

```powershell
# 1. 在 yunshu.env 里取消注释并改成实际值
#    YUNSHU_DATABASE_URL=postgresql+psycopg://yunshu:<口令>@<服务器>:5432/yunshu
#    YUNSHU_PG_BIN_DIR=C:\Program Files\PostgreSQL\17\bin
# 2. 建表，再重启服务
yunshu migrate
Restart-Service Yunshu
```

已经在用 SQLite、要迁到 PostgreSQL（目标必须是空库；SQLite 只读，原文件不动）：

```powershell
Stop-Service Yunshu
yunshu upgrade_to_pg precheck --target "postgresql+psycopg://yunshu:<口令>@<服务器>:5432/yunshu"
yunshu upgrade_to_pg run --target "postgresql+psycopg://yunshu:<口令>@<服务器>:5432/yunshu" --yes
# 再把 YUNSHU_DATABASE_URL、YUNSHU_PG_BIN_DIR 写进 yunshu.env
Start-Service Yunshu
```

PostgreSQL 模式下服务启动时只检查数据库结构版本、不会自动升级：**升级本程序后先运行 `yunshu migrate` 再启动服务**（单实例部署也可以在 yunshu.env 里设 `YUNSHU_AUTO_MIGRATE=1` 让它自动升级）。`yunshu backup create` 在 PostgreSQL 模式下调用 `pg_dump`。

## 升级

直接运行新版本的 MSI（图形界面或 `msiexec /i 新版本.msi /qn`）。安装程序会停止服务、替换程序、再启动服务：

- `C:\ProgramData\Yunshu` 下的配置、数据库、主密钥、日志全部保留，`yunshu.env` 不会被覆盖。
- SQLite：服务启动时自动先备份（`data\backups\yunshu-<时间>-pre-migrate.db`）再升级数据库结构。
- PostgreSQL：见上一节，先 `yunshu migrate`。
- 之前选择的防火墙规则会保留。

建议升级前先手动 `yunshu backup create` 并确认已经单独导出过主密钥。

## 回滚到旧版本

安装程序不允许直接用旧版本覆盖新版本。

1. 卸载新版本（数据保留）：`msiexec /x 新版本.msi /qn`，或在“应用和功能”里卸载。
2. 安装旧版本的 MSI。旧版本启动时如果发现数据库结构比自己新，会启动失败（服务会反复重试），这是正常的。
3. 恢复升级前的自动备份：

   ```powershell
   Stop-Service Yunshu
   yunshu backup list      # 找 pre-migrate 那一份
   yunshu backup restore C:\ProgramData\Yunshu\data\backups\yunshu-<时间>-pre-migrate.db --yes
   Start-Service Yunshu
   ```

PostgreSQL 模式用升级前的 `pg_dump` 备份恢复（`yunshu backup restore <文件>.dump --yes`）。

## 日志与排查

- 服务日志：`C:\ProgramData\Yunshu\logs\yunshu-service.log`；维护命令的日志在同一目录（如 `yunshu-backup.log`）。
- 服务状态：`Get-Service Yunshu`、`sc.exe qc Yunshu`；启动失败时“事件查看器 → Windows 日志 → 系统”里有服务控制管理器的记录。
- 就绪检查：`Invoke-WebRequest http://127.0.0.1:8080/api/ready`（200 表示数据库可用、结构版本一致、Worker 在运行）。
- 端口被占用时服务会启动失败并按恢复策略重试：在 `yunshu.env` 里改 `YUNSHU_PORT` 后 `Restart-Service Yunshu`。
- 在控制台里用与服务完全相同的方式运行（先停止服务，用管理员窗口，Ctrl+C 停止）：

  ```powershell
  Stop-Service Yunshu
  yunshu service --foreground
  ```

## 从免安装 ZIP（桌面版）迁到服务版

桌面版的数据在解压目录的 `data\yunshu.db`，主密钥在 `%APPDATA%\yunshu\master.key`。新版免安装 ZIP 的 `cli\yunshu.exe` 可以直接做备份与导出（它按桌面版的目录工作，不会读服务版的配置）。

1. 关闭桌面版窗口。在解压目录的 `cli` 文件夹打开命令行：

   ```powershell
   .\yunshu.exe backup create
   .\yunshu.exe keytool export D:\yunshu-desktop.key
   ```

   记下 `backup create` 输出的备份文件路径。
2. 安装服务版 MSI，等页面能打开后，在管理员 PowerShell 里：

   ```powershell
   Stop-Service Yunshu
   Remove-Item C:\ProgramData\Yunshu\keys\master.key   # 新装时生成、还没有加密任何东西的密钥
   Copy-Item D:\yunshu-desktop.key C:\ProgramData\Yunshu\keys\master.key
   yunshu backup restore <第 1 步的备份文件> --yes
   Start-Service Yunshu
   ```

3. 用桌面版里的账号密码登录，确认资产与凭据都在。确认无误后再删除桌面版目录和 `D:\yunshu-desktop.key`（导出的密钥请另外妥善保存一份）。

不要让桌面版和服务版同时运行并控制同一批设备。

## 卸载

“设置 → 应用 → 云枢运维平台 → 卸载”，或：

```powershell
msiexec /x Yunshu-4.4.0-beta.2-windows-amd64.msi /qn
```

卸载会停止并删除服务、删除程序文件、PATH 项、开始菜单和防火墙规则，但**保留 `C:\ProgramData\Yunshu`**（配置、数据库、备份、主密钥、日志），以便以后重新安装继续使用。确定不再需要、并且已经备份过数据和主密钥时，再彻底删除：

```powershell
Remove-Item -Recurse -Force C:\ProgramData\Yunshu
```

## 从源码构建

在 Windows amd64 上安装 Python 3.11、Node 20 与 .NET 6 以上运行时，在仓库根目录：

```powershell
.\scripts\build-windows.ps1            # 生成 artifacts\windows\Yunshu-<版本>-windows-amd64.msi 与 .sha256
.\scripts\build-windows.ps1 -Desktop   # 同时生成带 cli\yunshu.exe 的免安装 ZIP
```

脚本只构建与做隔离自检（临时目录、127.0.0.1），不会安装 MSI 或注册服务；安装、升级、卸载的测试在 GitHub Actions（`.github/workflows/windows-package.yml`，windows-2022 / windows-2025）里进行。安装包用 [WiX Toolset](https://wixtoolset.org/) 5.0.2 构建（Microsoft Reciprocal License，MS-RL，只作为构建工具，不随安装包分发），脚本从 nuget.org 下载固定版本并核对 SHA-256，只放在 `artifacts\windows\cache` 里，不做全局安装。
