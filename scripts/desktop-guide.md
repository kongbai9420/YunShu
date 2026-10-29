# 云枢运维平台 · Windows 免安装版（单人桌面）

双击就能用的单人版：程序在本机运行，只有这台电脑能访问，数据库是 SQLite。需要让局域网里其他电脑访问、开机自启或使用 PostgreSQL 时，请改用 Windows 服务版（MSI）、Linux 安装包或 Docker，见 [部署指南](https://github.com/kongbai9420/YunShu/blob/main/deploy/README.md)。

## 系统要求

- Windows 10 / 11，amd64。
- Microsoft Edge WebView2 Runtime（Windows 11 自带；Windows 10 若没有，可从微软官网安装）。
- 不需要安装 Python、Node，也不需要管理员权限。

## 开始使用

1. 把 `Yunshu-windows-amd64.zip` 完整解压到一个可写的目录（不要在压缩包里直接运行），保留 `_internal` 目录。
2. 双击 `Yunshu/Yunshu.exe`，打开窗口。
3. 用 `admin` / `admin123` 登录，按提示修改密码。
4. 在“资产列表 → 新增资产”里添加第一台设备。

程序没有代码签名，第一次运行时 Windows 可能提示“已保护你的电脑”：点“更多信息 → 仍要运行”。下载后可以先按 [校验说明](https://github.com/kongbai9420/YunShu/blob/main/deploy/VERIFY.md) 核对校验值。

## 数据放在哪里

| 内容 | 位置 |
| --- | --- |
| 数据库与平台备份 | EXE 同级的 `data\`（数据库 `data\yunshu.db`） |
| 主密钥（加密设备密码用） | `%APPDATA%\yunshu\master.key`，不在程序目录里 |
| 启动失败日志 | EXE 同级的 `logs\desktop.log` |

**主密钥丢失后，已保存的设备密码都无法解密。** 请单独备份它（见下文），并和数据库备份分开存放。

## 命令行工具

包里的 `cli\yunshu.exe` 是维护工具，在 `cli` 目录打开命令行运行：

```powershell
.\yunshu.exe backup create                 # 立即备份数据库
.\yunshu.exe backup list                   # 查看备份
.\yunshu.exe backup restore <备份文件> --yes  # 恢复（先关闭窗口）
.\yunshu.exe keytool export <目标文件>       # 导出主密钥，单独保存
.\yunshu.exe legacy_import --config <config.ini> --users <users.json>   # 从 3.x 旧版导入
```

旁边的 `desktop-portable.env` 让命令行使用桌面版的目录（数据 `..\data`、主密钥 `%APPDATA%\yunshu\master.key`），不会读取 Windows 服务版的配置。如果你用环境变量 `YUNSHU_DATA_DIR` 改过数据目录，在这个文件里写同样的 `YUNSHU_DATA_DIR`。

## 升级

1. 先备份：`.\yunshu.exe backup create`，并导出主密钥。
2. 把新版本解压到**新目录**，把旧目录里的 `data\` 复制过去（或用环境变量 `YUNSHU_DATA_DIR` 指向原来的数据目录）。主密钥位置不变，不用动。
3. 启动新版本：会先自动备份，再升级数据库结构。
4. 确认数据正常后再删除旧目录。

## 换成服务版

需要局域网访问或开机自启时，安装 Windows 服务版（MSI），再把桌面版的数据和主密钥迁过去，步骤见 [Windows 服务版说明](https://github.com/kongbai9420/YunShu/blob/main/deploy/windows/README.md)。

## 使用说明

- 窗口关闭后程序停止：正在执行的任务会等完成或超时，不会中途丢弃；关闭窗口不会删除数据库或主密钥，也不会改变设备的风扇模式。
- 每次重新打开都需要重新登录。
- 控制归属默认仍是旧控制器：安装或启动桌面版不会切换控制归属，也不会对设备执行写操作。需要由本平台接管电源 / 风扇控制时，在“运维操作 → 控制归属”里按页面提示切换；不打算接管时可以设环境变量 `YUNSHU_CONTROL_CUTOVER=0`，只查看检查结果。
- 包内附带 `THIRD_PARTY_NOTICES.txt` 和 `licenses\`（第三方组件的许可证）。

## 从源码运行（开发者）

需要 Python 3.11、Node 20（≥ 20.19）和 WebView2 Runtime。在仓库根目录：

```powershell
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install --require-hashes -r backend/requirements-desktop.lock
cd frontend
npm ci
npm run build
cd ..
scripts/desktop.bat
```

打包：在装好 `backend/requirements-package.lock` 的 Python 3.11 环境里先构建前端，再运行 `scripts/build-desktop.ps1 -Python <该环境的 python.exe>`，输出在 `artifacts/desktop/`。脚本会先对打好的 EXE 运行隔离自检（临时数据库与密钥，不读用户配置），通过后才压缩并显示 SHA-256。
