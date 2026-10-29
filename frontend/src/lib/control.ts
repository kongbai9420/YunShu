/** P5 控制接口的类型与文案。 */

export interface ControlStatus {
  owner: "legacy" | "new"
  revision: number
  changed_by: string
  changed_at: string | null
  note: string
  power_actions: { key: string; label: string; high_risk: boolean }[]
  max_batch: number
  thermal_catalog_size: number
  indeterminate_targets: { key: string; message: string; updated_at: string }[]
}

export interface ControlOperation {
  id: string
  asset_id: string | null
  asset_name: string
  kind: string
  action: string
  action_label: string
  status: string
  message: string
  observed: Record<string, unknown>
  requested_by: string
  reason: string
  batch_id: string | null
  target_key: string
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface ControlAction {
  key: string
  label: string
  high_risk: boolean
}

export interface AssetCapability {
  asset_id: string
  asset_name: string
  endpoint: { id: string; address: string; purpose: string } | null
  target: {
    key: string
    state: string
    message: string
    thermal_mode: string
    applied_pwm: number | null
    thermal_updated_at: string | null
  } | null
  actions: (ControlAction & { allowed: boolean; reason: string; code?: string })[]
  catalog_size?: number
  note?: string
  /** bmc-snmp：功能开关与资产上已有的 SNMP 端点 */
  enabled?: boolean
  snmp_endpoints?: { id: string; address: string }[]
  /** bmc-snmp：BMC 主动推送（4.4 P20c）——平台这端的接收设置与已配置的推送 */
  push?: {
    trap: { enabled: boolean; target: string; port: number; subscription: PushSubscription | null }
    events: { url: string; subscription: PushSubscription | null }
  }
}

export interface PushSubscription {
  destination: string
  created_at: string
  created_by: string
  last_event_at: string | null
  events_received: number
}

export const THERMAL_ACTIONS: ControlAction[] = [
  { key: "restore_auto", label: "恢复原厂自动温控", high_risk: false },
  { key: "set_speed_all", label: "全局固定转速", high_risk: true },
]

/** 4.4 P19：一键开启 / 关闭 BMC 的 SNMP（经 Redfish 修改 BMC 设置，只允许管理员） */
export const BMC_SNMP_ACTIONS: ControlAction[] = [
  { key: "enable", label: "开启 BMC 的 SNMP", high_risk: false },
  { key: "disable", label: "关闭 BMC 的 SNMP", high_risk: true },
  // 4.4 P20c：让 BMC 主动推送告警到“服务器告警”
  { key: "trap_on", label: "在 BMC 上配置 Trap 目标", high_risk: false },
  { key: "trap_off", label: "移除平台的 Trap 目标", high_risk: false },
  { key: "events_on", label: "订阅 BMC 的 Redfish 事件", high_risk: false },
  { key: "events_off", label: "取消 Redfish 事件订阅", high_risk: false },
]
/** 需要选择 SNMP 凭据的操作：开启时用于采集，配置 Trap 目标时用其团体名 */
export const SNMP_NEEDS_CREDENTIAL = ["enable", "trap_on"]

export const THERMAL_MODE: Record<string, string> = {
  unknown: "未知",
  auto: "原厂自动（已下发）",
  manual: "手动固定转速（已下发）",
}

export const OP_STATUS: Record<string, { label: string; tone: "ok" | "warn" | "bad" | "info" | "neutral" }> = {
  queued: { label: "排队中", tone: "info" },
  running: { label: "执行中", tone: "info" },
  accepted: { label: "指令已接受（未确认）", tone: "warn" },
  verified: { label: "已查询确认", tone: "ok" },
  unconfirmed: { label: "已接受，无法确认", tone: "warn" },
  failed: { label: "失败", tone: "bad" },
  indeterminate: { label: "结果不确定", tone: "bad" },
  rejected: { label: "已拒绝（未发送）", tone: "neutral" },
  cancelled: { label: "已取消", tone: "neutral" },
}

/** 高风险操作要求输入的确认文字：单台为资产名称，多台为“N 台”（与后端一致） */
export function confirmPhrase(names: string[]): string {
  return names.length === 1 ? names[0] : `${names.length} 台`
}

export function newRequestKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}
