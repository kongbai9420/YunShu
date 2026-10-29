# Docker 部署指南

[English](README.en.md)

云枢的镜像同时支持 **amd64 和 arm64**，群晖、飞牛 fnOS、威联通、Unraid、普通 Linux 服务器都能直接运行。下面给出四种方式，任选其一。

| 方式 | 适合 | 数据库 |
| --- | --- | --- |
| ① 一条 `docker run` 命令 | 先试一试 | SQLite |
| ② Compose 轻量版（推荐） | 个人、家庭实验室、NAS | SQLite |
| ③ Compose 标准版 | 资产多、多人同时使用 | PostgreSQL 17 |
| ④ NAS 图形界面 | 不想敲命令的群晖 / 飞牛用户 | 同 ② 或 ③ |
| Linux 原生安装（不用 Docker） | 不想装 Docker 的 Debian / Ubuntu / Rocky / Alma / openEuler / 麒麟 V10 服务器，systemd 管理 | SQLite 或外部 PostgreSQL |
| Windows 服务版（MSI） | Windows Server / Windows 10、11 上长期运行、局域网访问 | SQLite 或外部 PostgreSQL |

> Linux 原生安装用自带 Python 运行时的压缩包加 systemd 服务，步骤见 [linux/README.md](linux/README.md)（[English](linux/README.en.md)）。
>
> Windows 服务版不用 Docker，安装与使用见 [windows/README.md](windows/README.md)。

> [!TIP]
> 想试用还没发布的代码时，可以在本仓库根目录自己构建：`docker build -f docker/Dockerfile.ops -t yunshu:local .`，再把示例里的 `image:` 改成 `yunshu:local`。

部署完成后，浏览器打开 `http://<主机或 NAS 的 IP>:8080`，用 `admin` / `admin123` 登录，**按提示立即修改密码**。

---

## ① 一条 docker run 命令

```bash
docker run -d --name yunshu --restart unless-stopped \
  -p 8080:8080 \
  -v yunshu-data:/data \
  -v yunshu-keys:/etc/yunshu \
  -e YUNSHU_EMBEDDED_WORKER=1 \
  -e YUNSHU_SETUP_WIZARD=0 \
  ghcr.io/kongbai9420/yunshu:4.4.0-beta.1
```

- `yunshu-data`：数据库、平台备份。`yunshu-keys`：加密设备密码用的**主密钥**，必须和数据分开放。
- 去掉 `-e YUNSHU_SETUP_WIZARD=0` 时，首次打开页面会出现设置向导，可以在页面里改选 PostgreSQL。

## ② Compose 轻量版（推荐）

新建一个目录，保存为 `docker-compose.yml`，然后执行 `docker compose up -d`：

```yaml
# 云枢 轻量版：单容器，SQLite，适合个人 / 家庭实验室 / NAS
services:
  yunshu:
    image: ghcr.io/kongbai9420/yunshu:4.4.0-beta.1
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

仓库里的 [`deploy/lite/compose.yml`](lite/compose.yml) 内容基本相同（没有 `container_name`；`TZ` 是注释行，镜像默认就是北京时间），另外设置了日志轮转。

## ③ Compose 标准版（PostgreSQL）

包含 PostgreSQL 17、结构升级任务、页面与接口、一个后台 Worker。**把两处 `change-me-to-a-long-random-password` 改成同一个长密码**：

```yaml
# 云枢 标准版：PostgreSQL 17 + 迁移 + API + Worker
x-yunshu: &yunshu
  image: ghcr.io/kongbai9420/yunshu:4.4.0-beta.1
  volumes:
    - yunshu-data:/data
    - yunshu-keys:/etc/yunshu
  environment:
    # 第 1 处密码
    YUNSHU_DATABASE_URL: postgresql+psycopg://yunshu:change-me-to-a-long-random-password@db:5432/yunshu
    YUNSHU_SECURE_COOKIES: "0"
    TZ: Asia/Shanghai

services:
  db:
    image: postgres:17@sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3   # 按摘要固定
    environment:
      POSTGRES_DB: yunshu
      POSTGRES_USER: yunshu
      POSTGRES_PASSWORD: change-me-to-a-long-random-password   # 第 2 处密码
    volumes:
      - yunshu-pg:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U yunshu -d yunshu"]
      interval: 10s
      timeout: 5s
      retries: 10
    restart: unless-stopped
  migrate:          # 每次启动先完成数据库结构升级，然后退出
    <<: *yunshu
    command: ["migrate"]
    depends_on:
      db:
        condition: service_healthy
    restart: "no"
  api:              # 页面与接口
    <<: *yunshu
    command: ["api"]
    ports:
      - "8080:8080"
    depends_on:
      migrate:
        condition: service_completed_successfully
    restart: unless-stopped
  worker:           # 采集、告警、定时任务
    <<: *yunshu
    command: ["worker"]
    depends_on:
      api:
        condition: service_healthy
    restart: unless-stopped

volumes:              # 卷名固定；与轻量版的 yunshu-data / yunshu-keys 是同一份
  yunshu-pg:
    name: yunshu-pg
  yunshu-data:
    name: yunshu-data
  yunshu-keys:
    name: yunshu-keys
```

密码中如果有 `@ : / ? #` 等字符，在第 1 处要做 URL 编码；最简单的办法是只用字母和数字。用命令行部署时也可以用仓库里的 [`deploy/standard/`](standard/)：把 `.env.example` 复制为 `.env` 写入密码，模板会自动读取。

资产很多、需要外部数据库与多个执行 Worker 时，见 [`deploy/enterprise/`](enterprise/)。

## ④ 在 NAS 图形界面里部署

### 群晖 DSM 7.2+（Container Manager）

1. 套件中心安装 **Container Manager**。
2. File Station 里新建文件夹，例如 `docker/yunshu`。
3. 打开 Container Manager → **项目** → **新增**：
   - 项目名称：`yunshu`
   - 路径：选第 2 步的文件夹
   - 来源：**创建 docker-compose.yml**，粘贴上面 ② 或 ③ 的 YAML
4. 一路下一步，勾选创建后启动，完成。拉取镜像需要几分钟。
5. 打开 `http://<NAS IP>:8080`。

DSM 自己占用 5000 / 5001，8080 若被其他套件占用，把 `"8080:8080"` 改成 `"8090:8080"` 等。

### 飞牛 fnOS

1. 打开 **Docker** 应用 → **Compose** → **新增项目**。
2. 项目名称 `yunshu`，存放路径选一个文件夹（例如 `docker/yunshu`）。
3. 选择创建 `docker-compose` 配置，粘贴上面 ② 或 ③ 的 YAML，勾选创建后立即启动。
4. 打开 `http://<NAS IP>:8080`。

### 其他

- **威联通 Container Station**：应用程序 → 创建 → 粘贴 YAML。
- **Portainer**：Stacks → Add stack → Web editor 粘贴 YAML。
- **Unraid**：安装 Docker Compose Manager 插件后新建 stack 粘贴 YAML。

## 想把数据放在 NAS 的共享文件夹里？

示例用的是 Docker 命名卷，不需要处理权限，最省事。如果想用共享文件夹（方便用 NAS 自带工具备份），把 `volumes:` 改成目录挂载，并删掉文件末尾的 `volumes:` 段：

```yaml
    volumes:
      - ./data:/data          # 相对于项目文件夹
      - ./keys:/etc/yunshu
```

容器内以 UID **10001** 运行，所以要先让这两个目录可写（SSH 登录 NAS 执行一次）：

```bash
cd /volume1/docker/yunshu        # 群晖；飞牛一般在 /vol1/1000/docker/yunshu
mkdir -p data keys
sudo chown -R 10001:10001 data keys
```

不想用 SSH 的话，继续用命名卷即可。

## 部署之后

**备份主密钥（重要）**：设备密码用主密钥加密，主密钥丢失后已保存的密码都无法解密。

```bash
docker exec yunshu yunshu-entrypoint keytool export /tmp/master.key
docker cp yunshu:/tmp/master.key ./yunshu-master.key
docker exec yunshu rm /tmp/master.key
```

标准版把 `yunshu` 换成 api 容器名（`docker ps` 查看）。导出的文件请放到与数据备份不同的地方。

**升级版本**：把 `image:` 的版本号改成新版本，然后 `docker compose pull && docker compose up -d`（图形界面里是“停止 → 构建 / 重新创建 → 启动”）。轻量版启动时会先自动备份再升级数据库；标准版由 `migrate` 先完成升级。

**从 4.0.0-beta.1 升级（用过 `docker/docker-compose.ops.yml` 的，该文件已从仓库删除）**：4.0 的示例没有固定卷名，Docker 按“目录名_卷名”命名，例如在 `docker` 目录里启动的是 `docker_yunshu-data`、`docker_yunshu-keys`。现在的模板卷名固定为 `yunshu-data`、`yunshu-keys`，直接换成新模板会挂上**两个空卷**，看起来像数据没了（旧数据还在旧卷里）。按下面做：

1. 在旧目录先备份：`docker compose exec api yunshu-entrypoint backup create`，并按上面“备份主密钥”导出主密钥（容器名用 api 的）。
2. 查旧卷名：`docker volume ls --filter name=yunshu`。
3. 在旧目录停掉旧服务：`docker compose down`（**不要**加 `-v`，否则会删掉卷）。旧示例有单独的 worker 容器，新的轻量版把 Worker 放在同一个容器里，旧的必须停掉。
4. 二选一：
   - **直接沿用旧卷**（不复制，最快）：在新模板末尾把卷名改成第 2 步查到的名字：
     ```yaml
     volumes:
       yunshu-data:
         name: docker_yunshu-data
       yunshu-keys:
         name: docker_yunshu-keys
     ```
   - **复制到新卷名**（旧卷保留，出问题可以退回旧版本）：
     ```bash
     docker run --rm --user 0 -v docker_yunshu-data:/from -v yunshu-data:/to ghcr.io/kongbai9420/yunshu:<新版本> sh -c "cp -a /from/. /to/"
     docker run --rm --user 0 -v docker_yunshu-keys:/from -v yunshu-keys:/to ghcr.io/kongbai9420/yunshu:<新版本> sh -c "cp -a /from/. /to/"
     ```
5. 用新模板 `docker compose up -d`。轻量版启动时会先自动备份，再升级数据库。
6. 登录核对资产数量、打开一台设备的凭据确认能解密；都正常后，再决定是否 `docker volume rm` 旧卷。

用 ① `docker run` 示例部署的，卷名本来就是 `yunshu-data` / `yunshu-keys`，换到 ② 或 ③ 不需要这些步骤。

**对外访问**：放到 HTTPS 反向代理（群晖“登录门户 → 反向代理”、飞牛、Nginx、Caddy 等）之后，并把 `YUNSHU_SECURE_COOKIES` 改为 `"1"`。不要把 8080 直接暴露到公网。

  反向代理和平台不在同一台机器或同一个容器里时，再加一个环境变量 `FORWARDED_ALLOW_IPS: "<反向代理的地址>"`（多个用逗号分隔），平台才会采信代理转发的真实客户端地址。不设置时，所有人在平台看来都来自代理的地址：审计里的来源地址都一样；登录失败锁定按“来源 + 账号”计，同一来源合计失败 30 次会锁住整个来源 5 分钟。不要设成 `*`，否则任何人都能伪造来源地址。

**健康检查**：`/api/health` 只说明进程活着；`/api/ready` 说明能正常服务（数据库连得上、数据库结构与镜像版本一致、内嵌 Worker 在运行），不满足时返回 503 和原因。镜像自带的健康检查：api 容器用 `/api/ready`，worker 容器检查本容器的 Worker 最近 90 秒内有没有心跳（`docker inspect` 里的 Health 状态）。外部监控或负载均衡请用 `/api/ready`。

**接收服务器告警推送（可选，默认关闭）**：平台默认定时读取 BMC 日志；想让 BMC 出现硬件告警时立即推送过来，可以打开下面两种接收方式，
再到“运维操作 → 开启 SNMP”里对服务器执行“在 BMC 上配置 Trap 目标”或“订阅 BMC 的 Redfish 事件”（需要同时设置 `YUNSHU_BMC_SNMP=1`，只有管理员能执行）。

| 方式 | 部署设置 | 说明 |
| --- | --- | --- |
| SNMP Trap | `YUNSHU_SNMP_TRAP=1`、`YUNSHU_SNMP_TRAP_TARGET=<BMC 能访问到的本机 IP>`，端口映射 `162:1162/udp` | 由 Worker 监听 UDP 1162（`YUNSHU_SNMP_TRAP_PORT` 可改），不需要特权端口；只收 v1 / v2c，来源 IP 和团体名都要对得上资产，否则丢弃 |
| Redfish 事件 | `YUNSHU_BMC_EVENTS_URL=https://<BMC 能访问到的平台地址>` | BMC 推送到 `/api/bmc-events/<随机令牌>`，必须经 HTTPS 反向代理；只需放行这一个路径给 BMC 所在网段 |

**常见问题**

| 现象 | 处理 |
| --- | --- |
| 拉取镜像失败 / 很慢 | 检查网络或镜像加速；内网环境可用 Releases 里的离线镜像包（`docker load`），也可以按页首说明自行构建 |
| 端口被占用 | 改 `ports` 左边的端口 |
| 日志里出现权限错误（目录挂载时） | 按上一节把目录属主改为 10001 |
| 主密钥目录不能放在数据目录里 | `/data` 与 `/etc/yunshu` 必须挂成两个不同的卷或目录 |
| 忘记 admin 密码 | 用另一个管理员账号重置；只有一个管理员时需从备份恢复 |
