/**
 * SSE 实时推送（GET /api/events）。
 *
 * 一个浏览器标签页只开一条 EventSource，所有使用 useLiveEvents 的组件共用；最后一个组件卸载后关闭。
 * - 页面切到后台时断开，回到前台时带上次的事件 id 重新连接，后端补发期间的变化；
 * - 浏览器自动重连失败（例如 401、后端重启）时按 2、4、8…最多 30 秒退避重连；
 * - 未连上时 status 不是 "live"，传入 onRefetch 的组件会按 fallbackMs 定时重新读取接口（降级为轮询）。
 *
 * 推送只更新显示的数据；正在编辑的表单不能被推送覆盖，组件自己负责不把推送值写进表单状态。
 */
import { type RefObject, useEffect, useRef, useState } from "react"
import type { Quality } from "@/lib/monitor"

export type LiveStatus = "connecting" | "live" | "reconnecting" | "paused"

export interface LiveMetricUpdate {
  key: string
  value: number | null
  quality: Quality
  unit: string
  observed_at: string
  interval: number
}
export interface MetricsEvent {
  asset_id: string
  items: LiveMetricUpdate[]
}
export interface LiveCollectionItem {
  endpoint_id: string
  asset_id: string
  protocol: string
  state: string
  error_kind: string
  consecutive_failures: number
  last_attempt_at: string
  last_success_at: string | null
}
export interface CollectionEvent {
  /** 新连接时先发一次全部端点状态（可能分成多个事件，最后一个 final 为 true） */
  snapshot: boolean
  final?: boolean
  items: LiveCollectionItem[]
}
export interface TasksEvent {
  counts: Record<string, number>
}

export interface LiveHandlers {
  metrics?: (e: MetricsEvent) => void
  collection?: (e: CollectionEvent) => void
  tasks?: (e: TasksEvent) => void
  /** 未连上实时推送时定时调用，用来重新读取接口 */
  onRefetch?: () => void
}

type Listener = { handlers: RefObject<LiveHandlers>; setStatus: (s: LiveStatus) => void }

const EVENT_NAMES = ["metrics", "collection", "tasks"] as const
const listeners = new Set<Listener>()
let source: EventSource | null = null
let status: LiveStatus = "connecting"
let lastId = ""
let failures = 0
let retryTimer: number | undefined
let closeTimer: number | undefined
// 采集状态缓存：连接建立时服务器只发一次全量快照。后挂载的组件（按需加载的页面、从别的页面切过来）
// 会错过它，一直显示“读取中”；订阅时用缓存补发一次快照，之后照常接收变化。
const collectionCache = new Map<string, LiveCollectionItem>()
let collectionReady = false
let snapshotOpen = false

function remember(e: CollectionEvent) {
  if (e.snapshot && !snapshotOpen) {
    collectionCache.clear()
    snapshotOpen = true
    collectionReady = false
  }
  for (const item of e.items) collectionCache.set(item.endpoint_id, item)
  if (e.snapshot && e.final !== false) {
    snapshotOpen = false
    collectionReady = true
  }
}

function setStatus(next: LiveStatus) {
  if (status === next) return
  status = next
  listeners.forEach((l) => l.setStatus(next))
}

function dispatch(name: (typeof EVENT_NAMES)[number], ev: MessageEvent<string>) {
  if (ev.lastEventId) lastId = ev.lastEventId
  let data: unknown
  try {
    data = JSON.parse(ev.data)
  } catch {
    return
  }
  if (name === "collection") remember(data as CollectionEvent)
  listeners.forEach((l) => {
    const h = l.handlers.current
    try {
      if (name === "metrics") h?.metrics?.(data as MetricsEvent)
      else if (name === "collection") h?.collection?.(data as CollectionEvent)
      else h?.tasks?.(data as TasksEvent)
    } catch (e) {
      console.error(e)
    }
  })
}

function close() {
  window.clearTimeout(retryTimer)
  retryTimer = undefined
  source?.close()
  source = null
}

function scheduleRetry() {
  close()
  setStatus("reconnecting")
  const delay = Math.min(30000, 2000 * 2 ** Math.min(failures, 4))
  failures += 1
  retryTimer = window.setTimeout(connect, delay)
}

function connect() {
  close()
  if (!listeners.size) return
  if (document.hidden) {
    setStatus("paused")
    return
  }
  if (status !== "reconnecting") setStatus("connecting")
  // 新建的 EventSource 不会自动带 Last-Event-ID，用查询参数传给后端续传
  const es = new EventSource(`/api/events${lastId ? `?last_event_id=${encodeURIComponent(lastId)}` : ""}`)
  source = es
  es.onopen = () => {
    failures = 0
    setStatus("live")
  }
  es.onerror = () => {
    if (source !== es) return
    // CLOSED：服务器拒绝（如会话过期）或返回的不是事件流，浏览器不会再重连，由我们退避重试；
    // CONNECTING：浏览器正在自动重连，连续失败几次后也改为我们退避，避免频繁请求
    if (es.readyState === EventSource.CLOSED || failures >= 2) scheduleRetry()
    else {
      failures += 1
      setStatus("reconnecting")
    }
  }
  for (const name of EVENT_NAMES) es.addEventListener(name, (ev) => dispatch(name, ev as MessageEvent<string>))
  es.addEventListener("bye", (ev) => {
    const e = ev as MessageEvent<string>
    if (e.lastEventId) lastId = e.lastEventId
    // 服务端定期关闭连接；浏览器会自动带 Last-Event-ID 重连
  })
}

function onVisibility() {
  if (document.hidden) {
    close()
    setStatus("paused")
  } else if (listeners.size && !source) {
    failures = 0
    connect()
  }
}

function subscribe(l: Listener) {
  listeners.add(l)
  window.clearTimeout(closeTimer)
  l.setStatus(status)
  if (collectionReady) {
    // 补发给晚到的订阅者；与真实快照内容相同，重复收到也只是覆盖同样的端点状态
    const items = [...collectionCache.values()]
    window.setTimeout(() => {
      if (!listeners.has(l)) return
      try {
        l.handlers.current?.collection?.({ snapshot: true, final: true, items })
      } catch (e) {
        console.error(e)
      }
    }, 0)
  }
  if (listeners.size === 1) {
    document.addEventListener("visibilitychange", onVisibility)
    if (!source && retryTimer === undefined) connect()
  }
  return () => {
    listeners.delete(l)
    if (!listeners.size) {
      // 切换页面时组件会先卸载再挂载，稍等一下再断开，避免来回重连
      closeTimer = window.setTimeout(() => {
        if (listeners.size) return
        document.removeEventListener("visibilitychange", onVisibility)
        close()
        status = "connecting"
      }, 1000)
    }
  }
}

/**
 * 订阅实时推送。handlers 每次渲染都可以是新对象（内部用 ref 保存最新的一份）。
 * 返回当前连接状态；没有连上时（status !== "live"）每 fallbackMs 调用一次 handlers.onRefetch。
 */
export function useLiveEvents(handlers: LiveHandlers = {}, { fallbackMs = 10000 }: { fallbackMs?: number } = {}) {
  const ref = useRef<LiveHandlers>(handlers)
  ref.current = handlers
  const [current, setCurrent] = useState<LiveStatus>(status)
  useEffect(() => subscribe({ handlers: ref, setStatus: setCurrent }), [])
  useEffect(() => {
    if (current === "live" || current === "paused") return
    const timer = window.setInterval(() => {
      if (!document.hidden) ref.current.onRefetch?.()
    }, fallbackMs)
    return () => window.clearInterval(timer)
  }, [current, fallbackMs])
  return { status: current, live: current === "live" }
}

export const LIVE_STATUS_TEXT: Record<LiveStatus, string> = {
  connecting: "正在连接实时推送",
  live: "实时",
  reconnecting: "已断开，正在重连",
  paused: "页面在后台，已暂停实时推送",
}
