import { useEffect, useRef, useState } from "react"
import { api } from "@/lib/api"
import { PROTOCOL_LABEL, labelOf } from "@/lib/vocab"

export type Quality = "valid" | "stale" | "unsupported" | "unconfigured" | "failed" | "estimated"
export interface LiveMetric {
  key: string
  value: number | null
  unit: string
  quality: Quality
  estimated: boolean
  source: string
  message: string
  observed_at: string
  age_seconds: number
  interval_seconds: number
  last_valid_value: number | null
  last_valid_at: string | null
}
export interface Profile {
  name: string
  label: string
  batch: string
  interval: number
  verified: boolean
  steps: { protocol: string; mode: string }[]
  platforms: string[]
  metrics: string[]
  prerequisites: string
  limitations: string
  evidence: string
}
/** 4.4 P20a：服务器 BMC 的采集计划（每项数据的主来源与兜底） */
export interface CollectionPlan {
  exists: boolean
  items: { key: string; label: string; slow: boolean; primary: string | null; fallbacks: string[] }[]
  /** 协议 → 探测时读到的数据项；null 表示探测时不可用 */
  probed: Record<string, string[] | null>
  probed_at: string | null
  reprobe: boolean
  slow_every?: number
  /** 4.4 P20e 双来源核对：ok 为 null 表示两个来源没有能对上的读数 */
  checked_at?: string | null
  check?: { key: string; label: string; ok: boolean | null; primary: string; other: string; at: string; detail: string }[]
  pinned?: Record<string, string>
}

export interface Collection {
  profile: { name: string; label: string; verified: boolean; inferred: boolean } | null
  plan?: CollectionPlan | null
  endpoints: {
    endpoint_id: string
    protocol: string
    state: string
    error_kind: string
    message: string
    capabilities: string[]
    facts: Record<string, unknown>
    last_attempt_at: string
    last_success_at: string | null
    consecutive_failures: number
    duration_ms: number
    retry_after: string | null
  }[]
  /** 4.4 P20d：临时提频（告警、接近阈值或收到推送事件）；没有时为 null */
  boost?: { interval: number; until: string; since: string; reason: string } | null
  last_task: {
    status: string
    /** deferred / deferred_reason：4.4 P20b 每台 BMC 排队或每分钟访问上限导致本轮顺延 */
    result: { degraded?: boolean; failed?: string[]; unconfigured?: string[]; deferred?: string[]; deferred_reason?: string }
    error: string
    finished_at: string | null
  } | null
}
export interface Task {
  asset_name?: string | null
  id: string
  kind: string
  asset_id: string | null
  status: string
  attempts: number
  max_attempts: number
  available_at: string
  started_at: string | null
  finished_at: string | null
  error: string
  result: { degraded?: boolean; failed?: string[]; unconfigured?: string[] }
}
export function sourceLabel(protocol: string): string {
  return protocol === "reach" ? "TCP 可达性" : labelOf(PROTOCOL_LABEL, protocol)
}
export const STATE: Record<string, string> = {
  queued: "等待执行",
  running: "执行中",
  succeeded: "任务完成",
  failed: "失败",
  cancelled: "已取消",
  indeterminate: "结果不确定",
  ok: "采集成功",
  skipped: "未使用",
  untrusted: "指纹不匹配",
  unconfigured: "待配置凭据",
  paused: "已暂停",
  completed: "发现完成",
}
/** 接口时间按 UTC 解析：SQLite 读回的时间不带时区后缀 */
export function utcDate(value: string): Date {
  return new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`)
}
/** Date → datetime-local 输入框的本地时间文字 */
export function localInput(date: Date): string {
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}
export function dateText(value: string | null): string {
  if (!value) return "从未"
  return utcDate(value).toLocaleString()
}
export function numberText(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

/** 卸载、切换资产及刷新时丢弃旧请求；后台标签页不轮询。 */
export function usePoll<T>(path: string, interval = 5000) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [key, setKey] = useState(0)
  const previousPath = useRef(path)
  useEffect(() => {
    let stopped = false
    let pending = false
    if (previousPath.current !== path) {
      previousPath.current = path
      setData(null)
      setError(null)
    }
    const load = async () => {
      if (pending) return
      pending = true
      try {
        const value = await api.get<T>(path)
        if (!stopped) {
          setData(value)
          setError(null)
        }
      } catch (e) {
        if (!stopped) setError((e as Error).message)
      } finally {
        pending = false
      }
    }
    void load()
    const timer = window.setInterval(() => {
      if (!document.hidden) void load()
    }, interval)
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [path, interval, key])
  return { data, error, refresh: () => setKey((v) => v + 1) }
}
