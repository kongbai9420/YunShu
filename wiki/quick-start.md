# 快速开始

五种安装方式任选其一，都只要几分钟。**先在测试环境试用**，不要一开始就把生产设备的控制功能交给平台。

| 方式 | 适合 | 数据库 |
| --- | --- | --- |
| 一条 docker run | 先试一试 | SQLite |
| Docker Compose 轻量版 | 个人、家庭实验室、NAS | SQLite |
| Linux 安装包 | 不想装 Docker 的 Linux 服务器 | SQLite 或外部 PostgreSQL |
| Windows 服务版（MSI） | Windows 上长期运行、局域网访问 | SQLite 或外部 PostgreSQL |
| Windows 免安装包（ZIP） | 一个人在本机用 | 只支持 SQLite |

需要 PostgreSQL 的标准版、企业版见 [部署与运行](deployment.md)。

所有文件都在 [Releases](https://github.com/kongbai9420/YunShu/releases) 页面下载，镜像是 `ghcr.io/kongbai9420/yunshu:4.4.0-beta.2`（amd64 / arm64）。下载后可以按 [校验下载](verify.md) 核对签名与校验值。

## 方式一：一条 docker run 命令

```bash
docker run -d --name yunshu --restart unless-stopped \
  -p 8080:8080 \
  -v yunshu-data:/data \
  -v yunshu-keys:/etc/yunshu \
  -e YUNSHU_EMBEDDED_WORKER=1 \
  -e YUNSHU_SETUP_WIZARD=0 \
  ghcr.io/kongbai9420/yunshu:4.4.0-beta.2
```

- `yunshu-data`：数据库与平台备份。
- `yunshu-keys`：加密设备密码用的**主密钥**，必须和数据分开放。
- 去掉 `-e YUNSHU_SETUP_WIZARD=0` 时，第一次打开页面会出现[首次设置向导](#首次设置向导)，可以在页面里改选 PostgreSQL。

## 方式二：Docker Compose 轻量版（推荐）

新建一个目录，保存为 `docker-compose.yml`，然后执行 `docker compose up -d`。在群晖 Container Manager、飞牛 fnOS 里，新建“项目 / Compose”后直接粘贴即可。

```yaml
services:
  yunshu:
    image: ghcr.io/kongbai9420/yunshu:4.4.0-beta.2
    container_name: yunshu
    command: ["api"]
    ports:
      - "8080:8080"            # 端口被占用时改左边，例如 "8090:8080"
    volumes:
      - yunshu-data:/data      # 数据库与平台备份
      - yunshu-keys:/etc/yunshu  # 主密钥，请单独备份
    environment:
      YUNSHU_EMBEDDED_WORKER: "1"   # 采集与告警在同一个容器里运行
      YUNSHU_SETUP_WIZARD: "0"      # 已选定轻量版，不显示首次设置向导
      YUNSHU_SECURE_COOKIES: "0"    # 经 HTTPS 反向代理访问时改为 "1"
      TZ: Asia/Shanghai
    restart: unless-stopped

volumes:              # 卷名固定，换目录、换模板都还是同一份数据
  yunshu-data:
    name: yunshu-data
  yunshu-keys:
    name: yunshu-keys
```

NAS 图形界面的逐步说明、把数据放进共享文件夹，见仓库里的 [Docker 部署指南](https://github.com/kongbai9420/YunShu/blob/main/deploy/README.md)。

## 方式三：Linux 安装包（不用 Docker）

包里自带 Python 运行时，由 systemd 管理。支持 Debian 12 / 13、Ubuntu 22.04 / 24.04、Rocky / AlmaLinux 8 / 9、openEuler 20.03 / 22.03 / 24.03，amd64 与 arm64。麒麟 V10 在支持范围内但尚未实测，见 [常见问题](troubleshooting.md)。

下载 `yunshu-4.4.0-beta.2-linux-<amd64|arm64>.tar.gz` 和同名 `.sha256`：

```bash
sha256sum -c yunshu-4.4.0-beta.2-linux-amd64.tar.gz.sha256
tar xzf yunshu-4.4.0-beta.2-linux-amd64.tar.gz
cd yunshu-4.4.0-beta.2
sudo ./install.sh --start
```

- 程序装到 `/opt/yunshu/`，配置在 `/etc/yunshu/yunshu.env`，数据在 `/var/lib/yunshu`，主密钥在 `/etc/yunshu/keys/master.key`。
- 安装脚本**不改防火墙**，需要时自己放行 TCP 8080。
- 详细步骤见 [Linux 安装说明](https://github.com/kongbai9420/YunShu/blob/main/deploy/linux/README.md)。

## 方式四：Windows 服务版（MSI）

适合 Windows Server 2019 / 2022 / 2025 或 Windows 10 / 11（64 位）。作为系统服务开机自启，局域网里其他电脑可以访问。

1. 下载 `Yunshu-4.4.0-beta.2-windows-amd64.msi` 和同名 `.sha256`，核对校验值：

   ```powershell
   (Get-FileHash -Algorithm SHA256 .\Yunshu-4.4.0-beta.2-windows-amd64.msi).Hash.ToLower()
   Get-Content .\Yunshu-4.4.0-beta.2-windows-amd64.msi.sha256
   ```

2. 双击安装。安装包**没有代码签名**，SmartScreen 可能提示“未知发布者”；确认校验值一致后点“更多信息 → 仍要运行”。
3. 需要其他电脑访问时，在“自定义安装”里勾选“Windows 防火墙规则（TCP 8080 入站）”（默认不勾选）。
4. 安装完成后服务自动启动，首次启动要初始化数据库，可能需要十几秒。

配置、数据、主密钥和日志都在 `C:\ProgramData\Yunshu\` 下。详见 [Windows 安装说明](https://github.com/kongbai9420/YunShu/blob/main/deploy/windows/README.md)。

## 方式五：Windows 免安装包（ZIP）

只给自己一个人在本机用、不想装服务时用。

1. 把 `Yunshu-windows-amd64.zip` 完整解压到一个可写目录（保留 `_internal` 目录）。
2. 双击 `Yunshu/Yunshu.exe`。需要系统已安装 Microsoft Edge WebView2 Runtime（Windows 10 / 11 通常自带），不需要管理员权限。
3. 第一次启动会显示[首次设置向导](#首次设置向导)，选“轻量版”。

- **免安装包只支持轻量版（SQLite）**，不含 PostgreSQL 驱动；在向导里选 PostgreSQL 会提示只能用轻量版。需要 PostgreSQL 请用 Docker 标准版、Linux 安装包或 Windows 服务版。
- 数据默认在 EXE 同级的 `data/` 目录，日志在 `logs/`，主密钥在 `%APPDATA%\yunshu\master.key`。
- 包里的 `cli\yunshu.exe` 是维护命令行（备份、导出主密钥等）。
- 关闭窗口后平台停止运行，采集和告警也停止。要长期运行请用服务版。

## 首次设置向导

全新安装、并且部署没有指定数据库时，打开网页会先看到向导：

| 选项 | 说明 |
| --- | --- |
| 轻量版（SQLite） | 数据存在数据目录的 `yunshu.db`，不需要额外服务 |
| 标准版（PostgreSQL） | 填写主机、端口、数据库名、用户名、密码，点“测试连接”。要求 PostgreSQL 16 或更高，且是**空库**或本平台以前用过的库。Windows 免安装包没有 PostgreSQL 驱动，选这一项会提示只能用轻量版 |

- 保存后平台立即建表并创建初始管理员，**不需要重启**。
- 从服务器本机打开时不需要口令；从其他电脑打开时，要填服务启动日志里打印的一次性**设置口令**，防止别人抢先把平台指向他们的数据库。
- 选择保存在主密钥旁边的 `database.json`。环境变量 `YUNSHU_DATABASE_URL` 始终优先。
- 不想要向导：设置 `YUNSHU_SETUP_WIZARD=0`，全新安装会直接使用轻量版。
- 以后可以用升级工具把轻量版迁到 PostgreSQL，反过来不支持。

## 第一次登录

1. 浏览器打开 `http://<服务器或 NAS 的 IP>:8080`（Windows 本机是 `http://localhost:8080`）。
2. 用初始账号 `admin` / `admin123` 登录。
3. 系统会**强制先修改密码**，改完之前不能使用其他功能。

## 添加第一台设备

1. 进入“资产管理 → 资产列表 → 新增资产”。
2. 填名称，选形态（物理机 / 虚拟机 / 容器）和角色（服务器、交换机……）。
3. 添加管理端点：选协议，填设备地址（只填 IP 或主机名，例如 `192.0.2.10`）和端口，选择已有凭据或填写新凭据。
4. 可以先点“获取设备信息”，从设备上读取品牌、型号、序列号，逐项确认后填入。
5. 保存后打开资产详情的“指标与采集”，等一个采集周期（默认 60 秒），确认有数据。

各类设备该用什么协议、设备上要做什么准备，见 [设备接入](devices.md)。

## 接下来建议做的事

1. 到“平台设置 → 用户与角色”为同事建账号，不要共用管理员。
2. **备份主密钥**。主密钥丢失后，已保存的设备密码都无法解密。Docker 部署：

   ```bash
   docker exec yunshu yunshu-entrypoint keytool export /tmp/master.key
   docker cp yunshu:/tmp/master.key ./yunshu-master.key
   docker exec yunshu rm /tmp/master.key
   ```

   把导出的文件放到与数据备份不同的地方。其他平台见 [备份、升级与迁移](backup-upgrade.md)。
3. 在“告警中心 → 通知与投递”配一个通知渠道，点“测试通知”。
4. 需要从别的网络访问时，放到 HTTPS 反向代理后面，并设置 `YUNSHU_SECURE_COOKIES=1`，见 [部署与运行](deployment.md)。不要把 8080 直接暴露到公网。
