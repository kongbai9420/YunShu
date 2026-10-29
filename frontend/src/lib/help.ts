/**
 * 「!」说明胶囊的文字（集中维护，同一选项在不同页面说法一致）。
 * 规则：一句话（约 20 字内）能说清的写在控件下方（hint）；说不清的才放这里。
 * 每条：做什么（what）、默认值、例子、改了之后的影响；risk 为 true 表示会影响设备或数据，胶囊显示为橙色。
 */
export interface HelpEntry {
  title: string
  what: string
  default?: string
  example?: string
  impact?: string
  risk?: boolean
}

const HELP_DATA = {
  // ---- 资产 ----
  "asset.form": { title: "形态", what: "资产的存在形式：物理机、虚拟机或容器。与“角色”“平台”互相独立。", example: "一台跑 Proxmox 的服务器：形态是物理机，角色是服务器，平台是 proxmox。" },
  "asset.role": { title: "角色", what: "资产在网络中承担的用途，决定列表分类、拓扑图标和自动推断的采集模板。", example: "交换机、路由器、UPS、NAS。" },
  "asset.maintenanceUntil": { title: "预计结束时间", what: "维护预计什么时候结束。过了这个时间资产还是“维护中”，会在总览的告警、每日巡检报告里提醒，并按告警规则“维护超过预计结束时间”发一条通知。", default: "改为维护中时的 7 天后", impact: "只是提醒，不会自动改回“在用”；延长预计结束时间或改回“在用”后提醒结束。" },
  "asset.lifecycle": { title: "生命周期", what: "资产当前所处阶段。只有“在用”的资产会被定时采集、参与告警和巡检。", default: "在用", impact: "改为规划中、维护中或已退役后，停止定时采集，不再产生新告警，也不能控制和巡检；进入维护前已有的告警保留，但不发通知。改回“在用”后恢复采集和告警。历史数据保留。只想在一段时间内不收通知、仍要看到读数时，用告警中心的“维护窗口”。" },
  "asset.profile": { title: "采集模板", what: "决定用哪些协议、按什么顺序采集这台资产。按平台和已有管理端点自动选择时，大多数情况无需手动指定。", default: "按平台和端点自动选择", example: "Dell 服务器 / iDRAC9：SNMP 为主、IPMI 回退、Redfish 补充；服务器同时有两个以上 BMC 协议时，改按自动探测出的采集计划，每项数据只由一个协议提供。", impact: "选错模板时可能读不到数据，或用不到更合适的协议；页面会显示每个协议的采集状态。" },
  "asset.assetNo": { title: "资产编号", what: "单位内部的固定资产编号，全平台唯一。导入时按资产编号判断是新增还是更新同一台资产，不按 IP 合并。", example: "IT-2024-0031", impact: "留空时，批量导入无法识别它是已有资产，可能新建重复记录。" },
  "asset.rackUnit": { title: "起始 U 位", what: "设备在机柜中最下面一个 U 的编号（从 1 开始，自下而上）。占用多少 U 由“设备型号”的 U 高决定。", example: "2U 服务器放在 U5，占用 U5–U6。", impact: "与同机柜其他设备重叠或超出机柜高度时无法保存，并提示被谁占用。" },
  "asset.deviceType": { title: "设备型号", what: "从设备型号库选择，用来确定 U 高、是否全深等机柜信息。型号库可在“机柜与定位 → 设备型号”导入或手工添加。", default: "未指定时按 1U、全深计算", impact: "改型号后占用 U 数可能变化，若与其他设备冲突会被拒绝保存。" },
  "asset.rackFace": { title: "安装面", what: "设备装在机柜正面还是背面。只对半深设备有区别：前后各可装一台；全深设备两面都占。", default: "正面" },
  "asset.endpoint.purpose": { title: "端点用途", what: "这个管理地址用来做什么：监控（只读采集）或控制（开关机、风扇等写操作）。同一地址可以分别建两个端点，用不同凭据。", example: "iDRAC 地址建一个“监控”端点用只读账号，再建一个“控制”端点用有操作权限的账号。", impact: "控制操作只会使用用途为“控制”的端点。" },
  "asset.endpoint.readCredential": { title: "关联只读凭据", what: "给控制端点配一个只读凭据，用来在执行控制前后查询状态，避免用有写权限的账号做只读查询。", impact: "不配时，状态查询会使用控制凭据本身。" },
  "asset.relation": { title: "资产关系", what: "两台资产之间的人工关系，例如宿主承载虚拟机、交换机上联等。总览、拓扑和关机影响提示都依据这些关系。", example: "宿主“pve-01”承载虚拟机“web-01”。", impact: "人工建立的关系不会被自动发现覆盖。" },
  "asset.infoEndpoint": { title: "获取信息使用的管理端点", what: "用哪个管理端点去设备上读取品牌、型号、序列号、主机名等资料，读到后由你逐项确认是否替换现有值。", impact: "只读取，不修改设备；不勾选的字段保持原值。" },
  "asset.infoReplace": { title: "替换字段", what: "勾选后，用设备上读到的值替换平台里当前的值。不勾选的字段保持不变。" },
  "asset.tagsFilter": { title: "标签筛选", what: "只显示带有该标签的资产，按完整标签精确匹配。" },
  "asset.recycle.days": { title: "回收站保留天数", what: "删除的资产在回收站里保留多少天，期间运维和管理员可以恢复。到期后由平台自动彻底删除，连同管理端点和关联。", default: "30 天", example: "改成 7 天：7 天前删除的资产会在一小时内被彻底删除。", impact: "改短后，已在回收站里、超过新期限的资产会在下一次清理时彻底删除，不能再恢复。", risk: true },

  // ---- 外观（P23） ----
  "appearance.defaultStyle": { title: "默认风格", what: "没有自己选过风格的账号用哪个主题。简洁：不透明的浅灰 / 深灰界面；玻璃：苹果风格的半透明毛玻璃界面；也可以选导入的主题。", default: "简洁", impact: "只改外观，不影响数据和权限。玻璃效果比简洁更耗显卡，老电脑或远程桌面里可能卡顿，系统开了“降低透明度”时会自动改为不透明。" },
  "appearance.wallpaper": { title: "玻璃主题壁纸", what: "玻璃风格背后的背景图。不上传时用内置的彩色渐变。图片存在平台数据库里，随备份一起保存。", example: "公司品牌色的抽象图、机房照片；颜色柔和的图片效果最好，文字更清楚。", impact: "所有使用玻璃风格的账号都会看到这张图；深色模式下自动压暗。" },
  "appearance.import": { title: "导入主题包", what: "导入别人做好的主题：一个 JSON 文件，写明 id、名称、基于简洁还是玻璃，以及浅色、深色各自要改的变量值。平台会逐项校验，只接受颜色、长度、阴影和渐变，不能带脚本、外部图片或样式规则。", example: "点“下载示例主题包”，改颜色后再导入。变量清单见源码 frontend/src/themes/README.md。", impact: "导入后所有账号都能选用；同一 id 再导入会覆盖原来的版本。" },

  // ---- 管理端点与凭据 ----
  "conn.protocol": { title: "协议", what: "平台用什么方式连接设备。SNMP 适合交换机、UPS、NAS 和 iDRAC 监控；SSH 适合 Linux；IPMI / Redfish 是服务器带外管理；PVE / ESXi API 读取虚拟化平台；可达性只检查端口是否通。", example: "Dell 服务器的 iDRAC：推荐 SNMP（监控）+ Redfish（补充硬盘与日志）。", impact: "协议决定能采集哪些指标；SNMP 不产生登录会话，IPMI 每次采集都会在 BMC 留下登录记录。" },
  "conn.credential": { title: "登录凭据", what: "连接设备用的账号或团体名。凭据加密保存，可以被多台设备共用；页面和接口都不会显示明文。", impact: "共用凭据被修改后，所有使用它的设备都会用新值。" },
  "conn.authMode": { title: "认证方式", what: "SNMP 的版本与安全级别。v2c 只用团体名，明文传输；v3 支持用户名、认证和加密。", default: "推荐 SNMPv3 认证 + 加密（authPriv）", impact: "v2c 团体名在网络上是明文，只建议在隔离的管理网使用。" },
  "conn.authAlgo": { title: "认证算法", what: "SNMPv3 校验报文未被篡改所用的摘要算法，必须与设备上该用户的设置一致。", default: "SHA", example: "iDRAC9、较新的交换机支持 SHA-256；老设备可能只支持 MD5 或 SHA。", impact: "与设备不一致时会提示认证失败，采集不到数据。" },
  "conn.privAlgo": { title: "加密算法", what: "SNMPv3 加密报文内容所用的算法，必须与设备上该用户的设置一致。", default: "AES（128 位）", impact: "与设备不一致时请求会超时或解密失败。" },
  "asset.endpoint.packages": { title: "读取软件包清单", what: "每天用这个 SSH 账号执行一次只读命令（dpkg-query / apk / rpm），记录已安装的软件包和版本，用于漏洞比对。不需要 root 权限，不修改设备。", default: "关闭", example: "Debian、Ubuntu、Proxmox VE 主机建议开启；目前只有这几种系统能做软件包漏洞比对。", impact: "每天多一次 SSH 登录，设备的登录日志里会留下一条记录。" },
  "conn.tlsVerify": { title: "校验证书", what: "连接 HTTPS（Redfish、PVE、ESXi）时，第一次记住设备证书的指纹，以后证书变化就拒绝连接，防止中间人冒充设备。", default: "开启", impact: "设备更换证书后需要在端点上“信任新证书”；关闭后不再校验，不推荐。", risk: true },

  // ---- 告警 ----
  "alert.rule.kind": { title: "规则类型", what: "阈值规则按指标数值判断；可达性规则按管理端点连续多次连不上判断。", example: "“CPU 温度 ≥ 80℃” 是阈值规则；“端点连续 3 次不可达” 是可达性规则。" },
  "alert.rule.severity": { title: "告警级别", what: "决定告警的显示颜色、排序和通知渠道的路由。", example: "严重：需要立即处理；警告：需要关注；提示：仅记录。" },
  "alert.rule.metric": { title: "指标键", what: "要判断的指标名，可用 * 通配多个同类指标。", example: "cpu.temp；disk.used_pct:*（所有磁盘）；fan.rpm:*。", impact: "写错时规则不会匹配任何指标，也就不会产生告警；可在资产的“指标与采集”里查看实际的指标键。" },
  "alert.rule.direction": { title: "触发方向", what: "数值“高于等于”还是“低于等于”阈值时触发。", example: "温度用“高于”；UPS 剩余电量、风扇转速用“低于”。" },
  "alert.rule.threshold": { title: "触发阈值", what: "达到这个值并满足“连续样本数”“持续时间”后进入告警。", example: "CPU 温度 80。" },
  "alert.rule.recovery": { title: "恢复阈值", what: "告警后，数值回到这个值（含）才算恢复。与触发阈值留出间隔，可避免数值在阈值附近来回跳动时反复告警、恢复。", example: "触发 80℃、恢复 75℃：温度在 78℃ 时保持告警，降到 75℃ 才恢复。", default: "触发阈值减 5" },
  "alert.rule.samples": { title: "连续样本数", what: "连续多少次采集都超过阈值才触发，用来过滤偶发的尖峰。", default: "1", example: "采集周期 60 秒、填 3：至少连续约 3 分钟超阈值才告警。" },
  "alert.rule.duration": { title: "持续时间（秒）", what: "超过阈值的状态至少持续多久才触发。与“连续样本数”同时满足才告警。", default: "0（不要求）" },
  "alert.rule.cooldown": { title: "冷却时间（秒）", what: "告警恢复后，在这段时间内再次触发不会重新发通知，避免频繁打扰。", default: "60" },
  "alert.rule.repeat": { title: "重复提醒（秒）", what: "告警一直未处理时，每隔这么久再通知一次。填 0 只在开始时通知一次。", default: "0（关闭）" },
  "alert.rule.tags": { title: "资产标签", what: "只对带有这些标签的资产生效；留空对所有资产生效。可与“限定资产”同时使用。", example: "核心,生产" },
  "alert.rule.assets": { title: "限定资产", what: "只对选中的资产生效；不选时对所有匹配的资产生效。" },
  "alert.rule.suppress": { title: "上游依赖故障时抑制下游通知", what: "当承载它的宿主或上联设备已经在告警时，这台资产的告警照常记录，但不再单独发通知，避免一台交换机故障引发一大片通知。", default: "开启", impact: "依据资产关系判断，关系不完整时可能抑制不到或多发。" },
  "alert.channel.kind": { title: "通知方式", what: "告警通知发送到哪里：邮件（SMTP）或各类机器人 Webhook（企业微信、钉钉、飞书等）。" },
  "alert.channel.security": { title: "安全连接", what: "与邮件服务器之间的加密方式。SSL 通常用 465 端口，STARTTLS 通常用 587 端口；“无”只用于内网测试服务器。", default: "SSL（465 端口）", impact: "与服务器不匹配时会连接失败，可用“发送测试”验证。" },
  "alert.channel.secret": { title: "机器人签名密钥", what: "钉钉、飞书等机器人开启“加签”后提供的密钥，平台用它给每条消息签名。没有开启加签可以留空。", impact: "开启了加签却不填，消息会被机器人拒收。" },
  "alert.channel.routes": { title: "路由标签", what: "只把带有这些标签的资产的告警发到这个渠道；留空则接收所有告警。", example: "网络组的机器人只填“网络”。" },
  "alert.channel.window": { title: "合并窗口（秒）", what: "在这段时间内产生的多条告警合并成一条通知发送，避免刷屏。", default: "60", impact: "越大越省消息，但单条告警的通知越晚到。" },
  "alert.channel.template": { title: "通知模板", what: "自定义通知的标题与正文，可以使用变量，如 {{asset}} 资产名、{{title}} 告警标题、{{severity}} 级别、{{value}} 当前值。留空使用默认模板。" },
  "alert.maintenance.window": { title: "维护窗口", what: "在指定时间段内，匹配的资产照常采集和记录告警，但不发送通知。用于计划内的停机、升级。", impact: "窗口结束后仍在告警的会补发通知；窗口期间已经恢复的，只有之前真的发出过触发通知，才补发一条恢复通知。" },
  "alert.maintenance.tags": { title: "限定标签", what: "只对带这些标签的资产生效。同时选了资产时，取两者都满足的资产。不选资产也不填标签时，需要勾选“对全部资产生效”才能保存。" },

  // ---- 网段发现 ----
  "discovery.scope": { title: "授权范围", what: "允许扫描的网段或地址，只扫描你有权管理的网络。单次最多 1024 个地址。", example: "192.0.2.0/24；192.0.2.10-192.0.2.50", impact: "扫描不属于你的网络可能触发对方安全告警。", risk: true },
  "discovery.exclude": { title: "排除地址 / 网段", what: "在授权范围内跳过这些地址，例如打印机、不能被探测的工控设备。", example: "192.0.2.1, 192.0.2.200/29" },
  "discovery.method": { title: "只读探测方式", what: "用什么方式判断地址上是否有设备：TCP 端口是否开放，或用只读团体名 / v3 账号查询 SNMP 系统信息（能拿到设备名与型号）。", impact: "两种方式都只读，不登录、不修改设备。" },
  "discovery.concurrency": { title: "并发上限", what: "同时探测多少个地址。", default: "8", impact: "越大越快，但对网络和防火墙的压力越大；老旧设备较多时建议调小。" },
  "discovery.timeout": { title: "每次连接超时（秒）", what: "单个地址等多久没有响应就判为无设备。", default: "1", impact: "跨广域网或设备较慢时适当调大；调大会延长整次扫描时间。" },
  "discovery.duplicate": { title: "同 IP 新建独立资产", what: "平台不按 IP 合并资产：同一个地址可能属于不同设备（例如 DHCP 换过地址、NAT 后的多台主机）。勾选表示你确认这是一台新设备，而不是已有资产。", impact: "确认错了会出现两台重复的资产，可之后删除或合并关系。" },
  "discovery.link": { title: "关联到已有资产", what: "把这次发现的观察记录挂到一台已有资产上，便于对照。只关联观察，不修改资产本身的字段。" },
  "discovery.confirm": { title: "确认管理端口与协议", what: "把发现的地址建成资产时，同时建一个管理端点。请选择实际要用来采集的端口和协议。", impact: "选错只会导致采集失败，可在资产详情里修改。" },

  // ---- 运维操作 ----
  "ops.reason": { title: "原因（写入审计）", what: "说明为什么执行这次操作，会和操作人、时间、目标一起写进审计日志，便于事后追查。", example: "机房断电演练，计划内关机。" },
  "ops.confirmPhrase": { title: "输入确认语", what: "高风险操作需要手动输入提示的确认语，防止误点。", risk: true },
  "ops.pwm": { title: "目标转速", what: "要设置的风扇转速百分比（0–100 的整数）。低于该机型安全下限会被拒绝，不会自动调整。", impact: "转速过低可能导致过热；平台在 CPU 温度达到熔断温度时会自动恢复原厂温控。", risk: true },
  "ops.preset": { title: "情景方案", what: "预设好的一组风扇转速，每个风扇各自固定一个百分比（与旧版的六个方案相同）。也可以选“自定义”，自己填每个风扇的转速。", example: "单节点负载（左）：25 / 40 / 30 / 25 / 20 / 20%。", impact: "只有部分机型支持按风扇设定（Dell 旧固件 IPMI、按风扇报告转速的华为 iBMC，或管理员自定义了按风扇命令的机型）；设备风扇数与方案不一致时，多出来的不下发。", risk: true },
  "ops.speeds": { title: "每个风扇的转速", what: "按 BMC 的风扇编号顺序填写百分比（0–100 的整数），第 1 个对应编号 0。任何一个低于机型安全下限，整组都不下发。", example: "30, 40, 30, 30", risk: true },
  "ops.ack": { title: "核对结果", what: "执行结果不确定（例如超时）的操作，需要有人到设备上核对实际状态后写下结果，才能解除锁定、继续操作同一台设备。", example: "已登录 BMC 确认电源已关闭。" },
  "ops.snmpCredential": { title: "SNMP 凭据", what: "开启 SNMP 时，平台用它读取这台 BMC（团体名或 SNMPv3），会写进 BMC：团体名以只读方式追加，不覆盖已在用的；SNMPv3 需要 BMC 上已有同名用户（iDRAC 用本地用户，密钥就是该用户的密码）。配置 Trap 目标时，BMC 发送 Trap 用这个团体名，平台据此认出是哪台设备（只支持团体名 v2c）。", example: "iDRAC 选 SNMPv3 凭据“idrac-snmp”；配置 Trap 目标选团体名凭据“idrac-public”。", impact: "开启并读通后，会在资产上自动添加一个使用这个凭据的 SNMP 监控端点。", risk: true },
  "inspection.template": { title: "巡检模板", what: "要在设备上执行哪些命令、怎么判断正常。内置模板有 Linux、Proxmox VE、华为与 H3C 交换机，只读；可以在“巡检模板”里复制后修改。", example: "服务器选“Linux 通用巡检”，交换机选“H3C 交换机巡检”。", impact: "模板的平台要和设备一致，否则命令会报错，结果记为“无法判断”。" },
  "inspection.schedule": { title: "执行方式", what: "单次可以立即执行，也可以指定一个时间执行一次；每天、每周、每月按平台时区（默认北京时间）在指定时间执行；更复杂的时间用“高级（cron 表达式）”。", default: "每天 08:00", example: "每周一、周四 07:30。", impact: "单次任务执行后自动停用；每月选 31 日时，小月在最后一天执行。" },
  "inspection.cron": { title: "cron 表达式", what: "5 段，用空格分开：分（0-59）、时（0-23）、日（1-31）、月（1-12）、星期（0-7，0 和 7 都是周日）。每段可以写 *、列表 1,15、范围 1-5、步长 */15。日和星期都限定时，满足其中一个就执行。", example: "30 8 * * 1-5：工作日 08:30；0 */6 * * *：每 6 小时；0 2 1,15 * *：每月 1 日和 15 日 02:00。", impact: "时间按平台时区（默认北京时间）。" },
  "inspection.platform": { title: "适用平台", what: "Linux / Proxmox VE 用 SSH 逐条执行命令并拿到退出码；华为、H3C、Cisco 交换机走交互式命令行，会先关闭分页（只对本次会话生效）。华为 / H3C 只允许 display 类命令，Cisco 只允许 show 类命令。" },
  "inspection.command": { title: "命令", what: "一行只读命令，用 SSH 端点的账号执行。删除、重启、关机、写文件（> 重定向）、改服务、改权限、装卸软件、改虚拟机或存储池、进入交换机配置模式、reset / delete / save 等会被拒绝保存。", example: "df -P | awk 'NR>1{print $5, $6}'；display fan", impact: "建议给巡检用专用的只读账号，命令能做什么取决于账号权限。", risk: true },
  "inspection.check": { title: "判定方式", what: "只记录输出：不判定。输出为空算正常：例如失败的服务列表。匹配正则算正常 / 算异常：按输出文字判断。数值阈值：用正则的第一个括号取出数值（可以多处，例如每个分区），超过警告值或严重值算异常。退出码为 0 算正常：只适用于 Linux / PVE。", example: "磁盘：正则 (\d+)%，越大越差，警告 85、严重 95。" },
  "inspection.severity": { title: "异常时记为", what: "这条检查项判定为异常时的严重程度。只有“严重”会生成告警（告警规则“巡检发现严重异常”），警告和提示只出现在执行记录里。", default: "警告" },
  "inspection.confirm": { title: "确认保存", what: "自定义命令会登录设备执行，保存前输入模板名称确认。每次新建或修改都会写入审计，记录全部命令。", risk: true },
  "serverEvents.note": { title: "处理结果", what: "说明这条服务器告警是怎么处理的，会和处理人、时间一起保存，并写入审计。只改变平台里的状态，不会清空 BMC 上的日志。", example: "已更换 2 号电源，BMC 显示恢复正常。" },
  "ops.serverList": { title: "选择服务器", what: "勾选要批量操作的服务器。一次最多可选的数量有限制，超过后其余选项会变灰。" },
  "thermal.mode": { title: "温控模式", what: "原厂自动：交给 BMC 自己调速；固定转速：始终保持一个百分比；按曲线：按 CPU 最高温度在曲线上计算转速；情景方案：每个风扇各自保持一个转速。", default: "原厂自动", impact: "固定转速和按曲线会接管风扇；温度过期、缺失或达到熔断温度时自动恢复原厂温控。", risk: true },
  "thermal.preset": { title: "情景方案", what: "策略模式为“情景方案”时，平台按所选方案设定每个风扇的转速，并一直保持；被别人改动后会重新设定。", impact: "CPU 温度达到熔断温度时照样恢复原厂温控；温度过期或缺失时不会因此降低任何风扇的转速。", risk: true },
  "thermal.fixed": { title: "固定转速 %", what: "固定模式下的风扇转速百分比。不能低于机型安全下限。", risk: true },
  "thermal.enabled": { title: "启用策略", what: "勾选后由平台按所选模式控制这台服务器的风扇；不勾选则不下发任何风扇命令。", risk: true },
  "thermal.curve": { title: "温控曲线节点", what: "每个节点是“温度 → 转速”，两点之间按直线插值。最后一个节点的温度就是熔断温度：达到后恢复原厂温控，降到熔断温度减回差以下才恢复按曲线调速。", example: "40℃→20%、60℃→35%、75℃→60%、82℃→100%。", risk: true },
  "thermal.cmd.vendor": { title: "厂商", what: "设备报告的厂商名以这里填的文字开头就算匹配，不区分大小写。厂商以设备实际读出的为准，资产上填写的品牌不参与匹配。", example: "lenovo、inspur、h3c" },
  "thermal.cmd.model": { title: "型号匹配规则", what: "正则表达式，要与设备读出的完整型号匹配；留空表示这个厂商的所有型号。", example: "ThinkSystem SR6.* 匹配 SR630、SR650 等。" },
  "thermal.cmd.firmware": { title: "BMC 固件范围", what: "设备读出的 BMC 固件版本在这个范围内才使用这组命令，按数字逐段比较；留空表示不限。通过 IPMI 读到的版本只有两段（如 3.30）。", example: "最低 2.60、最高 3.30.30.30。" },
  "thermal.cmd.restore": { title: "恢复原厂温控的命令", what: "每行一条，写法同 ipmitool raw：netfn command 数据…，数字可写 0x1f 或 31。只允许 OEM 段的 netfn（0x2c–0x3f），不能发送电源、用户、网络等标准命令。", example: "0x30 0x30 0x01 0x01", impact: "温度达到熔断温度、读数过期时平台会用它把风扇交还给 BMC，必须确认它真的能恢复自动。", risk: true },
  "thermal.cmd.set": { title: "设定转速的命令", what: "每行一条，按顺序执行，任何一条失败整体失败；{pwm} 处填入目标转速。", example: "第一行 0x30 0x30 0x01 0x00，第二行 0x30 0x30 0x02 0xff {pwm}", impact: "写错命令可能让风扇停在很低的转速，请先在非生产设备上验证。", risk: true },
  "thermal.cmd.fan": { title: "按风扇设定转速的命令（可选）", what: "填了才支持情景方案和按风扇设定。每行一条：含 {fan} 的行按风扇逐个发送（{fan} 是风扇编号，从 0 开始，{pwm} 是这个风扇的转速），不含的行按顺序发一次。", example: "第一行 0x30 0x30 0x01 0x00，第二行 0x30 0x30 0x02 {fan} {pwm}", risk: true },
  "thermal.cmd.scale": { title: "转速刻度", what: "{pwm} 填入的数值：百分比（0–100）原样填入；0–255 会按比例换算，例如 50% 填 128。", default: "百分比" },
  "thermal.cmd.safeMin": { title: "安全下限 %", what: "低于这个转速的请求直接拒绝，不会自动改成下限。", default: "20", risk: true },
  "thermal.addServer": { title: "为服务器添加策略", what: "只能为风扇控制可用的服务器添加（执行时按设备型号与固件匹配风扇命令）。添加后默认停用，确认参数后再启用。" },
  "cutover.checklist": { title: "切换前逐项确认", what: "每一项都确认无误后才能把控制归属切到新平台，例如旧版控制器已停止、温控曲线已核对。", risk: true },
  "cutover.note": { title: "切换说明", what: "说明这次切换或回滚的原因与安排，写入审计日志。" },

  // ---- 总览、拓扑、机柜 ----
  "guest.action": { title: "关联方式", what: "为发现的虚拟机或容器新建一个资产，或者关联到已有资产（不重复建）。", impact: "关联后建立“宿主承载”关系；人工建立的关系不会被覆盖。" },
  "guest.power": { title: "虚拟机电源控制", what: "虚拟机和容器通过发现它的 PVE 或 vSphere 平台开关机，不使用宿主的 IPMI。需要：部署开启 YUNSHU_GUEST_POWER=1、控制归属已切到新平台、虚拟机资产为“在用”，并在平台端点所属资产上为同一地址添加用途为“控制”、带电源管理权限凭据的端点。", default: "关闭", impact: "会真实开关虚拟机。正常关机需要客户机工具（PVE 的 QEMU Guest Agent 或 ACPI、VMware Tools），失败时不会自动改成强制断电。", risk: true },
  "guest.linked": { title: "先关闭其上的虚拟机", what: "关宿主前，先对它承载的每台虚拟机发起“正常关机”，等全部确认关闭后才执行宿主操作。任何一台没有关成（失败、超时、被拒绝），宿主操作就取消，不会发送任何宿主命令。", default: "勾选", impact: "只处理已确认承载关系的虚拟机；关系不完整时可能漏掉，宿主上的其他虚拟机仍会被硬关机。", risk: true },
  "guest.timeout": { title: "最长等待", what: "等待虚拟机完成正常关机的最长时间。数据库、Windows 等关机较慢的虚拟机可以适当调大。", default: "300 秒", impact: "超时后这台虚拟机记为“结果不确定”，宿主联动时宿主操作取消。" },
  "guest.instance": { title: "虚拟化实例范围", what: "只看来自某个 PVE 集群或 vCenter / ESXi 的发现结果。每个管理端点是独立的实例范围，不会互相合并。" },
  "topology.link.kind": { title: "链路类型", what: "这条连线代表什么关系：以太网、光纤、聚合链路、无线或其他。只影响图上的样式与筛选。" },
  "topology.view": { title: "拓扑视图", what: "不同的画布布局，例如“全部”“机房 A”。同一份链路数据可以有多种布局，删除视图不会删除链路。" },
  "topology.showCandidates": { title: "显示候选链路", what: "显示由 LLDP 自动发现、尚未确认的连线。确认后变为实线，忽略后以后不再提出。" },
  "topology.rescan": { title: "重新扫描", what: "根据交换机最新采集到的 LLDP 邻居表重新生成候选链路，先显示差异预览，确认后才合并。", impact: "只会新增、更新或移除“候选”链路；已确认、手动和已忽略的链路以及画布布局都不会被改动。" },
  "layout.overwrite": { title: "用我的版本覆盖", what: "别人在你编辑期间保存了新版本。选择覆盖时，用你画布上的版本替换对方保存的版本。", impact: "对方那次保存的改动会被替换掉（历史修订号保留在审计日志里）；不确定时先“载入最新”。", risk: true },
  "discovery.preview": { title: "预览范围（不联网）", what: "只在平台内展开并核对要扫描的地址数量、排除项和端口，不向网络发送任何探测包。确认无误后再开始扫描。" },
  "racks.locate": { title: "按 MAC / IP 查找", what: "根据交换机的 MAC 地址表和 ARP 表，查找这个地址最后出现在哪台交换机的哪个口。学到很多 MAC 或有 LLDP 邻居的端口标为上联口，排在后面。", impact: "只是参考证据，不会据此合并资产或认定连线；交换机需开启 SNMP 并被采集。" },
  "racks.typeImport": { title: "导入型号库", what: "上传社区维护的 devicetype-library（CC0 公有领域）ZIP 包，提取厂商、型号、U 高、是否全深等信息。可只导入指定厂商。", impact: "重复导入只更新变化的型号；手工录入的型号不会被覆盖。" },

  // ---- 监控 ----
  "monitor.metric": { title: "历史指标", what: "选择要查看历史曲线的指标。时间范围较长时自动使用分钟或小时汇总数据。" },
  "monitor.range": { title: "时间范围", what: "曲线显示的时间段。原始数据保留 48 小时、分钟汇总 30 天、小时汇总 1 年（可在部署时调整）。" },

  // ---- 设置 ----
  "settings.credential.kind": { title: "凭据类型", what: "凭据用于哪种认证：用户名 + 密码、SNMP 团体名、SNMPv3 账号、SSH 私钥或 API 令牌。" },
  "settings.credential.rotate": { title: "更换密码 / 密钥", what: "为已有凭据设置新值，所有使用它的设备下次采集起使用新值。", impact: "新值错误会导致这些设备全部采集失败。", risk: true },
  "settings.site.kind": { title: "场所类型", what: "场所的层级含义，例如园区、机房、房间。仅用于显示与筛选。" },
  "settings.site.parent": { title: "上级场所", what: "场所可以逐级嵌套。资产范围授权给上级场所时，自动包含它的全部下级场所。" },
  "settings.user.role": { title: "角色", what: "管理员：全部功能；运维：查看并修改资产、告警、执行操作；只读：只能查看。资产范围在角色之上再限定能看到哪些资产。" },
  "settings.user.tempPassword": { title: "临时密码", what: "重置后用户用这个密码登录，首次登录必须修改。该用户的其他登录会话会被注销。", risk: true },
  "settings.scope.all": { title: "可见全部资产", what: "勾选时不限制资产范围；取消后只能看到下面授权的场所（含下级）和单独授权的资产。", impact: "取消且一项都不选时，这个账号看不到任何资产。", risk: true },
  "settings.backup": { title: "备份", what: "轻量版复制数据库文件，标准版调用 pg_dump。备份不包含主密钥，主密钥需单独导出保管。", impact: "恢复会用备份覆盖当前全部数据。", risk: true },
  "settings.token": { title: "只读 API 令牌", what: "供 Prometheus 等工具抓取 /metrics，以及外部 AI 工具通过只读 MCP 接口（/mcp）查询使用。权限和资产范围与当前账号相同，只能读取。", impact: "令牌泄露后，持有者能读取你可见资产的指标；不用时请删除。" },

  // ---- 漏洞情报（4.3 P18c）----
  "vuln.kev": { title: "已被利用", what: "收录在美国 CISA“已知被利用漏洞”（KEV）清单里的漏洞，说明已经在真实攻击中使用过，应优先处理。" },
  "vuln.online": { title: "外网同步", what: "允许平台访问 NVD、CISA、OSV 等网站下载漏洞数据。关闭时平台不访问任何漏洞网站，只能导入离线包或接收推送。", default: "关闭", impact: "打开后，平台按各数据源的间隔访问对应网站；只下载数据，不上传资产信息（OSV 查询时会发送软件包名与版本）。", risk: true },
  "vuln.nvdKey": { title: "NVD API 密钥", what: "在 NVD 官网免费申请，可把请求频率从每 30 秒 5 次提高到 50 次，同步更快。加密保存。", default: "不填" },
  "vuln.source.enabled": { title: "启用数据源", what: "外网同步打开后，只有启用的数据源才会按间隔同步。国内来源和厂商公告默认不启用，由你决定。", impact: "国内来源使用网站的非正式接口，网站改版时可能失效，失效只影响该来源。" },
  "vuln.rss": { title: "厂商公告地址", what: "填写厂商安全公告的 RSS 或 Atom 地址。条目标题或正文里只出现一个 CVE 编号时，会与其他来源的同一漏洞合并。", example: "厂商 PSIRT 页面上的“订阅 RSS”链接。" },
  "vuln.sub.kind": { title: "订阅类型", what: "产品：按 NVD 的“厂商:产品”精确订阅；关键词：标题或描述里出现该词的新漏洞。" },
  "vuln.sub.value": { title: "订阅内容", what: "产品写成“厂商:产品”，与 NVD 的 CPE 写法一致（可在 NVD 网站搜索 CPE 查到）；也可以直接粘贴 cpe:2.3:a:厂商:产品。", example: "openbsd:openssh、fortinet:fortios、apache:struts" },
  "vuln.sub.minSeverity": { title: "最低严重程度", what: "低于这个级别的新漏洞不通知（仍会记录在漏洞列表里）。", default: "不限" },
  "vuln.bundle": { title: "离线包", what: "在能上网的机器上运行 python -m app.vulndb.bundle 生成的 zip 文件。重复导入只会更新已有数据。", impact: "导入可能需要几分钟（Debian 软件包数据较多）。" },

  "vuln.local": { title: "影响本地资产", what: "只显示与本平台资产的版本比对后命中的漏洞（不含已标为不受影响或误报的）。" },
  "vuln.triage": { title: "处置状态", what: "待处理：刚发现；确认受影响：已核实需要修复；不受影响：如该功能未启用；已缓解：采取了临时措施；接受风险：暂不处理；误报：比对结果有误。", impact: "不受影响、误报不再告警；已缓解、接受风险到了复查日期会自动回到待处理。只有管理员能修改，每次修改都写审计。" },

  "vuln.notify": { title: "漏洞通知", what: "开启后按规则发送即时通知和每日汇总，走告警中心配置的通知渠道，投递记录也在那里查看。", default: "开启（还需要至少一个启用的通知渠道）" },
  "vuln.early": { title: "订阅命中预警", what: "新漏洞命中你订阅的产品或关键词、且已被利用或为严重级别，但还没有版本数据能确认是否影响本地设备时，先发一条“漏洞预警·待确认”。之后版本比对确认命中时再发“漏洞确认”。", default: "开启", impact: "能更早收到通知；按标题和描述判断，可能有不相关的预警。只对两天内新出现的漏洞发，每个漏洞只发一次。" },
  "vuln.aggregation": { title: "汇总窗口", what: "新漏洞第一次出现后等待这么久再发即时通知，期间其他来源发布的同一漏洞会并入同一条消息，消息里列出所有发布来源。", default: "5 分钟", impact: "设为 0 表示立即发送；之后才出现的来源不再单独推送，只更新详情与每日汇总。" },

  // ---- AI（4.3 P18a）----
  "ai.enabled": { title: "AI 总开关", what: "关闭时平台不会调用任何 AI 接口。打开后，还要在下面的“功能”里逐个开启，才会真正调用。", default: "关闭", impact: "打开并开启某个功能后，平台会把该功能需要的数据（按隐私模式处理后）发送给你配置的 AI 接口。", risk: true },
  "ai.privacy": { title: "隐私模式", what: "脱敏：发送前把资产名、主机名、IP、MAC、序列号替换成代号（如 ASSET_1、IP_2），AI 回答后在本地换回原名。完整：原样发送，只建议用于本机或内网部署的模型。", default: "脱敏", impact: "无论哪种模式，设备密码、密钥、令牌都不会发送。", risk: true },
  "ai.defaultProvider": { title: "默认模型接口", what: "功能没有单独指定模型接口时使用这个。可以给不同功能指定不同接口，比如漏洞分析用能力强的模型，摘要用便宜的模型。" },
  "ai.feature": { title: "AI 功能", what: "每个功能单独开启并可指定模型接口。AI 只负责解释、汇总和建议；是否受影响、是否告警等判断由程序完成，AI 不能执行任何操作。", impact: "关闭某个功能后，该功能显示程序算出的结果，不再调用 AI。" },
  "ai.limits": { title: "调用上限", what: "过去 24 小时内所有 AI 调用的次数和 token 总数上限，达到后暂停调用，避免意外费用。", default: "200 次、50 万 token", impact: "达到上限后各功能只显示程序算出的结果，并提示已达上限。" },
  "ai.timeout": { title: "超时", what: "等待 AI 回复的最长时间。推理类模型（如 deepseek-reasoner）回复较慢，可适当调大。", default: "60 秒" },
  "ai.proxy": { title: "出网代理", what: "平台访问境外 AI 接口需要代理时填写，支持 http、https、socks5。", example: "http://192.0.2.20:7890", impact: "只用于 AI 调用，不影响设备采集。" },
  "ai.logContent": { title: "记录调用内容", what: "开启后在调用记录里保存发送的内容和返回结果，便于排查；关闭时只记录时间、功能、模型、耗时和 token 数。", default: "关闭", impact: "内容是脱敏后的文本，但仍可能包含设备型号、版本等信息，请按单位要求决定是否开启。" },
  "ai.preset": { title: "厂商预设", what: "选择厂商后自动填好接口地址、协议和兼容选项，只需再填密钥和模型。预设地址来自各厂商官方文档，可以修改。“已实测”表示维护者用真实密钥跑通过；其余为按文档适配，请务必做一次连接自检。" },
  "ai.baseUrl": { title: "接口地址", what: "AI 接口的基础地址。OpenAI 兼容接口一般填到 /v1 为止，平台会自动拼接 /chat/completions 和 /models。", example: "https://api.deepseek.com、http://192.168.1.20:11434/v1", impact: "明文 http 只允许本机或内网地址，公网接口必须用 https，避免密钥在公网上明文传输。" },
  "ai.apiVersion": { title: "API 版本", what: "Azure OpenAI 需要的 api-version 参数。", default: "2024-10-21" },
  "ai.model": { title: "模型", what: "调用的模型名称；Azure 填部署名称，豆包可填模型名或推理接入点 ID。可点“获取模型列表”从接口读取。", impact: "同一厂商不同模型的能力差别很大（如是否支持按结构输出、工具调用），换模型后请重新做连接自检。" },
  "ai.apiKey": { title: "接口密钥", what: "加密保存，页面和接口都不会再显示。编辑时留空表示不修改。本地模型（Ollama 等）一般不需要。" },
  "ai.quirks": { title: "兼容选项", what: "个别厂商与标准接口的差异，选了预设会自动勾选，一般不用改。", example: "DeepSeek、智谱只支持 json_object；通义开启结构化输出时不能传 max_tokens；Mistral 的工具调用 ID 必须是 9 位字母数字。" },
  "ai.selfcheck": { title: "连接自检", what: "依次测试：普通回复（地址与密钥）、模型列表、结构化输出（逐级尝试）、工具调用、流式输出，并记下这条配置实际支持的能力。之后调用时按这个结果选择方式。", impact: "会调用 6～10 次接口，产生少量 token 费用。" },

  // ---- 首次启动 ----
  "setup.tier": { title: "数据库档位", what: "轻量版把数据存在本机 SQLite 文件里，适合个人与小机房；标准版连接 PostgreSQL，支持多个 Worker，适合资产多、多人使用。", impact: "以后可以用升级工具把轻量版迁到 PostgreSQL，反过来不支持。" },
  "setup.sslmode": { title: "加密连接", what: "与 PostgreSQL 之间是否使用 TLS。优先：服务器支持就加密；必须：不支持就拒绝；校验证书：还要验证服务器证书；关闭：只用于本机或隔离网络。", default: "优先" },
  "setup.token": { title: "设置口令", what: "不是从服务器本机打开时，需要输入服务启动日志里打印的一次性口令，防止别人抢先把平台指向他们的数据库。" },
} satisfies Record<string, HelpEntry>

export type HelpKey = keyof typeof HELP_DATA
export const HELP: Record<HelpKey, HelpEntry> = HELP_DATA
