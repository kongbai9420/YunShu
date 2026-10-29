import type { Asset, Capability, Endpoint, Entry, Form, Health, Metric, Platform, Quality, Role } from "./types"

/** 演示数据：地址全部取自 RFC 5737 文档保留网段，凭据只有引用名 */
const NOW = Math.floor(Date.now() / 1000)

function metric(value: number | null, unit: string, quality: Quality, ageSec: number | null, source: string): Metric {
  return { value, unit, quality, observedAt: ageSec === null ? null : NOW - ageSec, source }
}

type Reading = [value: number | null, quality: Quality, ageSec: number | null]

interface Seed {
  id: string
  name: string
  form: Form
  role: Role
  platform: Platform
  entries: Entry[]
  location: string
  health: Health
  caps: Capability[]
  endpoints: Omit<Endpoint, "id">[]
  temp: Reading
  power: Reading
  load: Reading
  tags?: string[]
}

const SEEDS: Seed[] = [
  {
    id: "a-r740-01", name: "R740-计算01", form: "physical", role: "compute", platform: "dell-idrac",
    entries: ["server"], location: "机房 A / 机柜 03 / U12", health: "ok",
    caps: ["sensors", "power", "fans", "ssh"],
    endpoints: [
      { address: "192.0.2.11", protocol: "ipmi", purpose: "带外传感器与风扇", credentialRef: "cred:bmc-dell-ops", tls: "n/a" },
      { address: "192.0.2.11", protocol: "redfish", purpose: "硬盘与 RAID 补充", credentialRef: "cred:bmc-dell-ops", tls: "pinned" },
      { address: "198.51.100.11", protocol: "ssh", purpose: "系统负载", credentialRef: "cred:ssh-ops", tls: "verified" },
    ],
    temp: [58, "valid", 4], power: [212, "valid", 4], load: [37, "valid", 6], tags: ["生产"],
  },
  {
    id: "a-r740-02", name: "R740-计算02", form: "physical", role: "compute", platform: "dell-idrac",
    entries: ["server"], location: "机房 A / 机柜 03 / U14", health: "warning",
    caps: ["sensors", "power", "fans", "ssh"],
    endpoints: [
      { address: "192.0.2.12", protocol: "ipmi", purpose: "带外传感器与风扇", credentialRef: "cred:bmc-dell-ops", tls: "n/a" },
      { address: "198.51.100.12", protocol: "ssh", purpose: "系统负载", credentialRef: "cred:ssh-ops", tls: "verified" },
    ],
    temp: [79, "valid", 3], power: [348, "valid", 3], load: [88, "valid", 5], tags: ["生产", "高负载"],
  },
  {
    id: "a-r750-01", name: "R750-数据库", form: "physical", role: "storage", platform: "dell-idrac",
    entries: ["server"], location: "机房 A / 机柜 04 / U08", health: "ok",
    caps: ["sensors", "power", "fans", "storage"],
    endpoints: [
      { address: "192.0.2.21", protocol: "ipmi", purpose: "带外传感器与风扇", credentialRef: "cred:bmc-dell-ops", tls: "n/a" },
      { address: "192.0.2.21", protocol: "redfish", purpose: "硬盘与 RAID 补充", credentialRef: "cred:bmc-dell-ops", tls: "pinned" },
    ],
    temp: [52, "valid", 5], power: [276, "valid", 5], load: [null, "unconfigured", null],
  },
  {
    id: "a-nf5280-01", name: "浪潮-NF5280", form: "physical", role: "compute", platform: "inspur-bmc",
    entries: ["server"], location: "机房 B / 机柜 01 / U20", health: "unknown",
    caps: ["sensors", "power", "fans"],
    endpoints: [{ address: "192.0.2.31", protocol: "ipmi", purpose: "带外传感器与风扇", credentialRef: "cred:bmc-inspur", tls: "n/a" }],
    temp: [61, "stale", 1260], power: [190, "stale", 1260], load: [null, "unconfigured", null],
  },
  {
    id: "a-smc-01", name: "超微-备份机", form: "physical", role: "backup", platform: "supermicro-bmc",
    entries: ["server"], location: "机房 B / 机柜 02 / U04", health: "critical",
    caps: ["sensors", "power", "fans"],
    endpoints: [{ address: "192.0.2.41", protocol: "ipmi", purpose: "带外传感器与风扇", credentialRef: "cred:bmc-smc", tls: "n/a" }],
    temp: [null, "failed", 95], power: [null, "failed", 95], load: [null, "unconfigured", null], tags: ["待巡检"],
  },
  {
    id: "a-pve-01", name: "PVE-宿主01", form: "physical", role: "hypervisor", platform: "proxmox",
    entries: ["server", "virtualization"], location: "机房 A / 机柜 05 / U10", health: "ok",
    caps: ["sensors", "power", "vms", "storage", "ssh"],
    endpoints: [
      { address: "192.0.2.51", protocol: "ipmi", purpose: "带外传感器", credentialRef: "cred:bmc-dell-ops", tls: "n/a" },
      { address: "198.51.100.51", protocol: "https-api", purpose: "虚拟机清单与资源", credentialRef: "cred:pve-token", tls: "pinned" },
      { address: "198.51.100.51", protocol: "ssh", purpose: "系统负载", credentialRef: "cred:ssh-ops", tls: "verified" },
    ],
    temp: [55, "valid", 4], power: [240, "valid", 4], load: [42, "valid", 4],
  },
  {
    id: "a-esxi-01", name: "ESXi-宿主01", form: "physical", role: "hypervisor", platform: "esxi",
    entries: ["server", "virtualization"], location: "机房 A / 机柜 05 / U14", health: "ok",
    caps: ["power", "vms", "storage"],
    endpoints: [{ address: "198.51.100.61", protocol: "https-api", purpose: "虚拟机清单与资源", credentialRef: "cred:esxi-ro", tls: "unverified" }],
    temp: [null, "unsupported", null], power: [198, "estimated", 30], load: [31, "valid", 30],
  },
  {
    id: "a-vm-web", name: "web-前端01", form: "vm", role: "compute", platform: "linux",
    entries: ["virtualization"], location: "PVE-宿主01", health: "ok",
    caps: ["ssh"],
    endpoints: [{ address: "198.51.100.71", protocol: "ssh", purpose: "系统负载", credentialRef: "cred:ssh-ops", tls: "verified" }],
    temp: [null, "unsupported", null], power: [null, "unsupported", null], load: [23, "valid", 5],
  },
  {
    id: "a-vm-db", name: "db-主库", form: "vm", role: "storage", platform: "linux",
    entries: ["virtualization"], location: "PVE-宿主01", health: "warning",
    caps: ["ssh", "storage"],
    endpoints: [{ address: "198.51.100.72", protocol: "ssh", purpose: "系统负载", credentialRef: "cred:ssh-ops", tls: "verified" }],
    temp: [null, "unsupported", null], power: [null, "unsupported", null], load: [91, "valid", 5],
  },
  {
    id: "a-vm-win", name: "win-域控", form: "vm", role: "compute", platform: "windows",
    entries: ["virtualization"], location: "ESXi-宿主01", health: "offline",
    caps: [],
    endpoints: [{ address: "198.51.100.73", protocol: "winrm", purpose: "系统负载", credentialRef: null, tls: "unverified" }],
    temp: [null, "unsupported", null], power: [null, "unsupported", null], load: [null, "unconfigured", null],
  },
  {
    id: "a-ct-prom", name: "监控-容器", form: "container", role: "monitoring", platform: "linux",
    entries: ["virtualization", "other"], location: "PVE-宿主01", health: "ok",
    caps: ["ssh"],
    endpoints: [{ address: "198.51.100.74", protocol: "ssh", purpose: "系统负载", credentialRef: "cred:ssh-ops", tls: "verified" }],
    temp: [null, "unsupported", null], power: [null, "unsupported", null], load: [12, "valid", 8],
  },
  {
    id: "a-gw-01", name: "出口网关", form: "network", role: "gateway", platform: "openwrt",
    entries: ["network"], location: "机房 A / 机柜 01 / U02", health: "ok",
    caps: ["interfaces", "ssh"],
    endpoints: [
      { address: "198.51.100.1", protocol: "snmp", purpose: "接口流量", credentialRef: "cred:snmp-v3", tls: "n/a" },
      { address: "198.51.100.1", protocol: "ssh", purpose: "系统信息", credentialRef: "cred:ssh-net", tls: "verified" },
    ],
    temp: [47, "valid", 10], power: [null, "unsupported", null], load: [18, "valid", 10],
  },
  {
    id: "a-sw-core", name: "核心交换机", form: "network", role: "switching", platform: "h3c-comware",
    entries: ["network"], location: "机房 A / 机柜 01 / U04", health: "ok",
    caps: ["interfaces", "sensors"],
    endpoints: [{ address: "198.51.100.2", protocol: "snmp", purpose: "接口、LLDP 与温度", credentialRef: "cred:snmp-v3", tls: "n/a" }],
    temp: [41, "valid", 12], power: [96, "estimated", 12], load: [9, "valid", 12],
  },
  {
    id: "a-sw-b", name: "接入交换机-B", form: "network", role: "switching", platform: "h3c-comware",
    entries: ["network"], location: "机房 B / 机柜 01 / U01", health: "warning",
    caps: ["interfaces"],
    endpoints: [{ address: "198.51.100.3", protocol: "snmp", purpose: "接口与 LLDP", credentialRef: "cred:snmp-v2-ro", tls: "n/a" }],
    temp: [null, "unsupported", null], power: [null, "unsupported", null], load: [14, "stale", 900],
  },
  {
    id: "a-nas-01", name: "群晖-NAS", form: "storage", role: "backup", platform: "synology",
    entries: ["other"], location: "机房 B / 机柜 02 / U10", health: "ok",
    caps: ["storage", "interfaces"],
    endpoints: [{ address: "198.51.100.81", protocol: "snmp", purpose: "卷容量与温度", credentialRef: "cred:snmp-v3", tls: "n/a" }],
    temp: [39, "valid", 20], power: [null, "unsupported", null], load: [7, "valid", 20],
  },
  {
    id: "a-ups-01", name: "UPS-机房A", form: "power", role: "monitoring", platform: "apc-ups",
    entries: ["other"], location: "机房 A / 地面", health: "ok",
    caps: ["power"],
    endpoints: [{ address: "198.51.100.91", protocol: "snmp", purpose: "负载与电池", credentialRef: null, tls: "n/a" }],
    temp: [null, "unconfigured", null], power: [1340, "valid", 15], load: [null, "unsupported", null],
  },
]

export const ASSETS: Asset[] = SEEDS.map((s) => ({
  id: s.id,
  name: s.name,
  hostname: s.id.replace(/^a-/, "") + ".lab.example",
  form: s.form,
  role: s.role,
  platform: s.platform,
  entries: s.entries,
  location: s.location,
  health: s.health,
  tags: s.tags ?? [],
  notes: "",
  capabilities: s.caps,
  endpoints: s.endpoints.map((e, i) => ({ ...e, id: `${s.id}-ep${i + 1}` })),
  metrics: {
    cpuTemp: metric(s.temp[0], "°C", s.temp[1], s.temp[2], "ipmi"),
    power: metric(s.power[0], "W", s.power[1], s.power[2], "ipmi"),
    cpuLoad: metric(s.load[0], "%", s.load[1], s.load[2], "ssh"),
  },
}))

export const RENAMED_ASSET_ID = "a-r740-01"

/** 模拟一次采集：有效指标小幅波动并刷新时间，其他质量状态保持不变 */
export function simulateCollection(assets: Asset[], tick: number): Asset[] {
  const now = Math.floor(Date.now() / 1000)
  return assets.map((a, idx) => {
    const jitter = (m: Metric, span: number): Metric =>
      m.quality === "valid" && m.value !== null
        ? { ...m, value: Math.max(0, Math.round(m.value + Math.sin(tick + idx) * span)), observedAt: now }
        : m
    // R740-计算01 每隔约 15 秒上报一次不同的主机名，用来演示“编辑中的表单不被采集覆盖”
    const hostname =
      a.id === RENAMED_ASSET_ID ? (tick % 6 >= 3 ? "r740-01-new.lab.example" : "r740-01.lab.example") : a.hostname
    return {
      ...a,
      hostname,
      metrics: {
        cpuTemp: jitter(a.metrics.cpuTemp, 1.5),
        power: jitter(a.metrics.power, 6),
        cpuLoad: jitter(a.metrics.cpuLoad, 3),
      },
    }
  })
}
