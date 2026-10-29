/** P7 原型专用夹具：不参与资产导入、采集或设备控制。 */
export interface DemoResource {
  id: string
  name: string
  role: string
  form: "物理设备" | "虚拟机" | "容器"
  platform: string
  site: string
  state: "运行中" | "已关机" | "未知"
  quality: "有效" | "过期" | "失败" | "未配置"
  metrics: [string, string][]
  power?: boolean
  children?: DemoResource[]
}

export interface DemoGroup {
  id: string
  title: string
  description: string
  resources: DemoResource[]
}

const resource = (id: string, name: string, patch: Partial<DemoResource> = {}): DemoResource => ({
  id, name, role: "服务器", form: "物理设备", platform: "Debian", site: "家庭实验室",
  state: "运行中", quality: "有效", metrics: [["CPU", "12%"], ["内存", "38%"]], ...patch,
})

export const DEMO_GROUPS: DemoGroup[] = [
  { id: "compute", title: "物理服务器 / 宿主", description: "主机与已确认承载的系统、虚拟机和容器", resources: [
    resource("host-a", "演示主机 A", { platform: "PVE", power: true, metrics: [["CPU 温度", "48 ℃"], ["功耗", "112 W"], ["CPU", "24%"]], children: [
      resource("vm-web", "演示 Web VM", { form: "虚拟机", platform: "Ubuntu" }),
      resource("vm-dns", "演示 DNS VM", { form: "虚拟机", platform: "OpenWrt", state: "已关机", metrics: [["电源", "已关机"]] }),
      resource("ct-tools", "演示工具容器", { form: "容器", platform: "Debian LXC" }),
    ] }),
    resource("host-b", "演示主机 B", { platform: "ESXi", site: "演示机房", power: true, quality: "失败", state: "未知", metrics: [["CPU 温度", "未知"], ["功耗", "未知"]], children: [
      resource("vm-db", "演示数据库 VM", { form: "虚拟机", platform: "Rocky Linux", site: "演示机房", quality: "过期", state: "未知", metrics: [["CPU", "过期"], ["内存", "过期"]] }),
    ] }),
    resource("host-c", "演示备用主机", { platform: "Linux", site: "演示机房", power: true, state: "已关机", metrics: [["电源", "已关机"]] }),
  ] },
  { id: "standalone", title: "独立系统 / 未关联资源", description: "没有确认宿主的资源也独立可见", resources: [
    resource("linux", "演示独立 Linux", { platform: "Ubuntu" }),
    resource("orphan", "演示待关联 VM", { form: "虚拟机", quality: "未配置", state: "未知", metrics: [["宿主", "待确认"]] }),
  ] },
  { id: "network", title: "网络设备", description: "类别展示与监控、控制适配能力分开", resources: [
    resource("switch", "演示核心交换机", { role: "交换机", platform: "标准 SNMP", metrics: [["接口", "24"], ["链路", "18 个活动"]] }),
    resource("router", "演示家庭路由器", { role: "路由器", platform: "OpenWrt", metrics: [["CPU", "8%"], ["内存", "32%"]] }),
    resource("ap", "演示无线 AP", { role: "AP", platform: "待适配", quality: "未配置", state: "未知", metrics: [["采集", "未配置"]] }),
    resource("firewall", "演示防火墙", { role: "防火墙", platform: "Linux" }),
  ] },
  { id: "storage", title: "存储与供电", description: "NAS、存储与 UPS，按现有能力显示指标", resources: [
    resource("nas", "演示 NAS", { role: "NAS", platform: "待适配", quality: "未配置", state: "未知", metrics: [["采集", "未配置"]] }),
    resource("san", "演示存储设备", { role: "存储", platform: "待适配", quality: "未配置", state: "未知", metrics: [["采集", "未配置"]] }),
    resource("ups", "演示 UPS", { role: "UPS", platform: "待适配", quality: "未配置", state: "未知", metrics: [["采集", "未配置"]] }),
  ] },
  { id: "other", title: "其他已建档资产", description: "不因没有指标或控制能力而隐藏", resources: [
    resource("other-device", "演示其他设备", { role: "其他", platform: "未填写", quality: "未配置", state: "未知", metrics: [["状态", "仅建档"]] }),
  ] },
]

export const ALL_RESOURCES = DEMO_GROUPS.flatMap((g) => g.resources.flatMap((r) => [r, ...(r.children ?? [])]))
export const RESOURCE_BY_ID = new Map(ALL_RESOURCES.map((r) => [r.id, r]))
