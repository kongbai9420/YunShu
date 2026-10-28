# 常见问题

## 安装与启动

**`/api/ready` 返回 503** —— 返回内容里的 `reason` 说明了原因：

| reason | 处理 |
| --- | --- |
| 数据库不可用 | 检查 `YUNSHU_DATABASE_URL`、PostgreSQL 是否在运行、网络与口令。标准版看 `docker compose logs db` |
| 数据库结构版本为 …，需要 …，请先运行迁移 | PostgreSQL 升级后没有迁移：`docker compose run --rm migrate`、`sudo systemctl start yunshu-migrate` 或 `yunshu migrate`；单实例也可以设 `YUNSHU_AUTO_MIGRATE=1` |
| 内嵌 Worker 没有在运行 | 轻量版的后台线程停了，看日志里的错误后重启服务 |

`/api/health` 正常而 `/api/ready` 不正常，说明进程活着但不能正常服务。独立 Worker 用 `yunshu worker --check` 检查。

**端口 8080 被占用** —— Docker：改 `ports` 左边，例如 `"8090:8080"`（群晖 DSM 自己占用 5000 / 5001，其他套件可能占 8080）。Linux / Windows：在 `yunshu.env` 里改 `YUNSHU_PORT`，然后重启服务；Windows 服务版端口被占用时会启动失败并反复重试，防火墙规则也要放行新端口。

**容器提示“主密钥文件不能放在数据目录里”** —— 把 `/etc/yunshu` 挂成单独的卷，不要和 `/data` 是同一个目录。Linux / Windows 检查 `YUNSHU_MASTER_KEY_FILE`。

**目录挂载时日志里有权限错误** —— 容器内以 UID 10001 运行：`sudo chown -R 10001:10001 data keys`。

**升级 4.0.0-beta.1 后资产都没了** —— 多半是挂上了新的空卷，旧数据还在旧卷里。按 [备份、升级与迁移](backup-upgrade.md) 里“从 4.0.0-beta.1 升级到 4.4”处理，**不要**删除旧卷。

**打开网页显示“首次设置”** —— 全新安装且没有指定数据库，按 [快速开始](quick-start.md) 里的首次设置向导选择即可。不需要向导时设置 `YUNSHU_SETUP_WIZARD=0`。

**从别的电脑打开向导要求“设置口令”** —— 在服务启动日志里找设置口令（Docker：`docker logs yunshu`）。从服务器本机打开不需要口令。

**免安装包的向导里选 PostgreSQL 提示只能用轻量版** —— Windows 免安装包不含 PostgreSQL 驱动，只支持 SQLite。需要 PostgreSQL 请用 Docker 标准版、Linux 安装包或 Windows 服务版。

**Windows 提示“Windows 已保护你的电脑”（SmartScreen）** —— 安装包没有代码签名。先按 [校验下载](verify.md) 核对 SHA-256 与来源证明，一致后点“更多信息 → 仍要运行”。

**Windows 服务版的维护命令提示权限不足** —— 用管理员身份运行（开始菜单“云枢命令行”右键以管理员身份运行）。如果第一次安装后先用管理员运行了会生成密钥的命令，服务会读不到 `master.key`：在还没保存任何设备凭据时，删除 `C:\ProgramData\Yunshu\keys\master.key` 后重启服务即可。

**麒麟（Kylin）能不能用 Linux 安装包** —— 安装包按 glibc 2.28 构建，麒麟 V10（glibc 2.28）在支持范围内，但**尚未在麒麟本身上测试过**；CI 用同为 glibc 2.28 的 openEuler 20.03 与 Rocky / Alma 8 验证。安装前看 `getconf GNU_LIBC_VERSION`，低于要求时 `install.sh` 会拒绝安装。按 RHEL 系的说明安装，留意 SELinux。也可以直接用 Docker，不受操作系统影响。欢迎反馈试用结果。

**RHEL 系上 `sudo yunshu` 提示找不到命令** —— `sudo` 默认不搜索 `/usr/local/bin`，写全路径 `sudo /usr/local/bin/yunshu ...`。

**Linux 服务起不来，日志里有 `avc: denied`** —— SELinux 拦截，常见原因是把数据目录改到了非常规位置。看 `sudo ausearch -m avc -ts recent`，处理方法见 [Linux 安装说明](https://github.com/kongbai9420/YunShu/blob/main/deploy/linux/README.md)。

## 时间与时区

**巡检报告、定时巡检、日志时间差了几个小时** —— 时区不对。Docker 镜像默认 `TZ=Asia/Shanghai`；Linux / Windows 默认跟随主机。在 compose 或 `yunshu.env` 里设 `TZ`（如 `TZ=Asia/Shanghai`）后重启。Windows 在有夏令时的地区请显式设置 `TZ`。详见 [配置项参考](configuration.md)。

## 登录

**忘记管理员密码** —— 让另一位管理员在“用户与角色”重置临时密码；只有一个管理员时，只能从备份恢复。

**登录后很快又要重新登录** —— 经 HTTPS 访问但没有设置 `YUNSHU_SECURE_COOKIES=1`，或反向代理没有转发 `X-Forwarded-Proto`。反过来，只用 http 访问时不要设 `YUNSHU_SECURE_COOKIES=1`。

**很多人同时被锁定登录 / 审计里来源地址都一样** —— 反向代理不在本机，平台把所有请求都当成来自代理。设置 `FORWARDED_ALLOW_IPS=<代理地址>`，见 [部署与运行](deployment.md)。

**单点登录提示同名账号已存在** —— 平台不会让单点登录账号接管同名的本地账号。管理员改名或删除本地账号后再试。

## 采集

**SNMP 一直超时** —— 检查 UDP 161 是否放行、团体名或 v3 用户 / 算法是否与设备一致、设备是否限制了允许访问的来源地址。SNMPv1 设备读不到扩展信息属于正常。

**iDRAC 日志里还是有很多登录记录** —— 确认资产的采集模板是 `dell-server`，有 SNMP 端点且采集成功。SNMP 不通时会回退到 IPMI（每次都会登录）。

**某个协议显示“跳过”** —— 服务器按采集计划采集，这个协议不负责任何数据。需要时在“指标与采集”里“重新探测”。

**显示“身份不受信”** —— 设备证书或 SSH 主机密钥变了。确认是你自己更换的，再在端点上“信任新证书”。

**指标显示“过期”** —— 超过 3 个周期没有新数据：检查 Worker 是否在运行（“平台设置 → Worker 状态”）、资产是否为“在用”，以及该资产的采集状态。

**PVE 读不到备份任务** —— API 令牌缺少 `Sys.Audit` 权限，其余指标不受影响，采集详情里会写明原因。

**收不到 SNMP Trap** —— 确认 `YUNSHU_SNMP_TRAP=1`，162/udp 已映射或转发到 1162，BMC 的来源 IP 是该资产某个端点的 IP，团体名一致。本机自己发往 162 的测试报文不经过转发，请从别的机器测试。只支持 v1 / v2c。

## 告警与通知

**规则没有触发** —— 检查指标键是否与资产“指标与采集”里显示的完全一致（可用 `*` 通配）、资产是否在规则的标签 / 限定资产范围内、是否为“在用”、是否在维护窗口内、连续样本数和持续时间是否满足。

**邮件发不出去** —— 用“测试通知”看具体错误；QQ、163 等邮箱要用授权码；TLS（465）与 STARTTLS（587）的端口不要填反；确认渠道已启用。

**维护窗口结束后没收到“已恢复”** —— 只有窗口期间之前确实发出过触发通知的告警，才会补发恢复通知。

## 运维操作

**所有控制按钮都是灰的，提示“旧控制器”** —— 控制归属还是旧版，管理员在“运维操作 → 控制归属”切换，见 [运维操作](operations.md)。

**显示“结果不确定”，无法再次操作** —— 上一次命令超时，平台无法确认设备是否已执行。到 BMC 或设备上核对实际状态，在页面上填写核对结果并“确认解除”。

**虚拟机没有开关机按钮或按钮是灰的** —— 见 [运维操作](operations.md) 里虚拟机电源的四个开启条件，按钮旁会写明具体原因。

**宿主操作被取消，提示其上虚拟机没有全部确认关闭** —— 有虚拟机正常关机失败或超时（常见原因：没有安装客户机工具）。先单独处理那台虚拟机，再重新操作宿主。

**“开启 SNMP”标签里按钮不可用** —— 需要部署设置 `YUNSHU_BMC_SNMP=1`、管理员账号、控制归属在新平台、资产“在用”且有控制 / 管理用途的 Redfish 端点。

## 性能

**仪表盘或列表很慢** —— 轻量版适合个人和小规模使用；资产更多时迁到 PostgreSQL 标准版，并按 [部署与运行](deployment.md) 规划 Worker 数量。
