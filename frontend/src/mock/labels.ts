import type { Capability, Entry, Form, Health, Platform, Quality, Role } from "./types"

export const FORM_LABEL: Record<Form, string> = {
  physical: "物理服务器",
  vm: "虚拟机",
  container: "容器",
  network: "网络设备",
  storage: "存储",
  power: "供电设备",
}

export const ROLE_LABEL: Record<Role, string> = {
  compute: "计算",
  storage: "存储",
  gateway: "网关",
  switching: "交换",
  hypervisor: "虚拟化宿主",
  backup: "备份",
  monitoring: "监控",
}

export const PLATFORM_LABEL: Record<Platform, string> = {
  "dell-idrac": "Dell iDRAC",
  "inspur-bmc": "浪潮 BMC",
  "supermicro-bmc": "超微 BMC",
  proxmox: "Proxmox VE",
  esxi: "VMware ESXi",
  linux: "Linux",
  windows: "Windows Server",
  openwrt: "OpenWrt",
  "h3c-comware": "H3C Comware",
  synology: "群晖 DSM",
  "apc-ups": "APC UPS",
}

export const ENTRY_LABEL: Record<Entry, string> = {
  server: "服务器",
  network: "网络",
  virtualization: "虚拟化",
  other: "其他",
}

export const CAPABILITY_LABEL: Record<Capability, string> = {
  sensors: "传感器",
  power: "电源",
  fans: "风扇",
  interfaces: "网络接口",
  vms: "虚拟机",
  storage: "存储卷",
  ssh: "系统信息",
}

export const QUALITY_LABEL: Record<Quality, string> = {
  valid: "有效",
  stale: "已过期",
  unsupported: "不支持",
  unconfigured: "未配置",
  failed: "采集失败",
  estimated: "估算值",
}

export const HEALTH_LABEL: Record<Health, string> = {
  ok: "正常",
  warning: "警告",
  critical: "严重",
  offline: "离线",
  unknown: "未知",
}
