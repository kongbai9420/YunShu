import type { LiveMetric } from "@/lib/monitor"

export interface Resource {
  id: string
  name: string
  asset_no: string | null
  form: string
  role: string
  platform: string
  lifecycle: string
  site_id: string | null
  site_name: string | null
  tags: string[]
  endpoints: { protocol: string; address: string; port: number | null }[]
  collection_state: string
  collection_at: string | null
  metrics: Record<string, LiveMetric>
  active_alerts: number
  parents: { id: string; name: string }[]
  parent_count: number
  children_count: number
  /** 后端按排序计算的分组：server / virtual-linked / virtual-unlinked / role:<类别> */
  group: string
}
export interface ResourcePage { items: Resource[]; total: number; generated_at: string }

export const COLLECTION_LABEL: Record<string, string> = {
  ok: "采集正常", degraded: "部分采集失败", failed: "采集失败", untrusted: "等待信任",
  unconfigured: "采集未配置", stale: "采集过期", unmonitored: "未监控", pending: "等待首次采集",
}

const GROUP_LABEL: Record<string, string> = {
  server: "物理主机与独立系统", "virtual-linked": "已关联虚拟资源", "virtual-unlinked": "未关联虚拟资源",
}

/** 分组显示名：类别分组沿用资产类别名称 */
export function groupLabel(group: string, roleLabels: Record<string, string>) {
  return GROUP_LABEL[group] ?? roleLabels[group.replace(/^role:/, "")] ?? group
}

/** 显示排序（共享布局，存数据库）：分组顺序、主卡完整 ID 顺序、每台宿主下的子资源顺序 */
export interface ResourceOrder {
  groups: string[]
  order: string[]
  children: Record<string, string[]>
}
export interface EffectiveOrder {
  revision: number
  groups: string[]
  default_groups: string[]
  items: { id: string; group: string }[]
  children: Record<string, string[]>
}

/** 在完整顺序里把 id 移到 target 之前或之后；只改变 id 的位置，其余项（含筛选外、其他页）相对顺序不变 */
export function moveNear(list: string[], id: string, target: string, after: boolean): string[] {
  if (id === target) return list
  const base = list.includes(target) ? list : [...list, target]
  const without = base.filter((x) => x !== id)
  const at = without.indexOf(target) + (after ? 1 : 0)
  return [...without.slice(0, at), id, ...without.slice(at)]
}
