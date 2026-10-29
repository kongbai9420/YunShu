/** 资产分类词表，与 backend/app/schemas.py 一致（平台为自由文本） */

export const FORM_LABEL: Record<string, string> = { physical: "物理机", vm: "虚拟机", container: "容器" }

export const ROLE_LABEL: Record<string, string> = {
  server: "服务器",
  switch: "交换机",
  router: "路由器",
  ap: "AP",
  firewall: "防火墙",
  nas: "NAS",
  storage: "存储",
  ups: "UPS",
  other: "其他",
}

export const LIFECYCLE_LABEL: Record<string, string> = {
  planned: "规划中",
  active: "在用",
  maintenance: "维护中",
  retired: "已退役",
}

export const PROTOCOL_LABEL: Record<string, string> = {
  ipmi: "IPMI",
  redfish: "Redfish",
  snmp: "SNMP",
  ssh: "SSH",
  https: "HTTPS",
  http: "HTTP",
  "pve-api": "PVE API",
  "esxi-api": "ESXi API",
  winrm: "WinRM",
  other: "其他",
  // 服务器告警的推送来源（4.4 P20c）
  redfish_event: "Redfish 事件推送",
  snmp_trap: "SNMP Trap",
}

export const PURPOSE_LABEL: Record<string, string> = { management: "管理", monitoring: "监控", control: "控制" }

export const RELATION_LABEL: Record<string, string> = {
  hosts: "承载",
  installed_in: "安装于",
  belongs_to: "属于",
  depends_on: "依赖",
}

export const DEFAULT_PORT: Record<string, number> = { ipmi: 623, snmp: 161, ssh: 22, https: 443, http: 80, redfish: 443, "pve-api": 8006, "esxi-api": 443, winrm: 5986 }

export function labelOf(map: Record<string, string>, key: string): string {
  return map[key] ?? key
}

export function optionsFrom(map: Record<string, string>): { value: string; label: string }[] {
  return Object.entries(map).map(([value, label]) => ({ value, label }))
}
