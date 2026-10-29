/** 指标质量：要求六种状态显式区分，未知/过期不能显示成 0 */
export type Quality = "valid" | "stale" | "unsupported" | "unconfigured" | "failed" | "estimated"

export interface Metric {
  value: number | null
  unit: string
  quality: Quality
  /** 秒级时间戳；null 表示从未采到 */
  observedAt: number | null
  source: string
}

/** 形态、角色、平台三个维度互相独立 */
export type Form = "physical" | "vm" | "container" | "network" | "storage" | "power"
export type Role = "compute" | "storage" | "gateway" | "switching" | "hypervisor" | "backup" | "monitoring"
export type Platform =
  | "dell-idrac"
  | "inspur-bmc"
  | "supermicro-bmc"
  | "proxmox"
  | "esxi"
  | "linux"
  | "windows"
  | "openwrt"
  | "h3c-comware"
  | "synology"
  | "apc-ups"

/** 用户的四类入口；只是筛选预设，不约束底层分类 */
export type Entry = "server" | "network" | "virtualization" | "other"

export type Capability = "sensors" | "power" | "fans" | "interfaces" | "vms" | "storage" | "ssh"

export interface Endpoint {
  id: string
  address: string
  protocol: "ipmi" | "redfish" | "ssh" | "snmp" | "https-api" | "winrm"
  purpose: string
  /** 只保存凭据引用，界面上不出现明文 */
  credentialRef: string | null
  tls: "verified" | "pinned" | "n/a" | "unverified"
}

export type Health = "ok" | "warning" | "critical" | "offline" | "unknown"

export interface Asset {
  id: string
  name: string
  hostname: string
  form: Form
  role: Role
  platform: Platform
  entries: Entry[]
  location: string
  health: Health
  tags: string[]
  notes: string
  endpoints: Endpoint[]
  capabilities: Capability[]
  metrics: {
    cpuTemp: Metric
    power: Metric
    cpuLoad: Metric
  }
}
