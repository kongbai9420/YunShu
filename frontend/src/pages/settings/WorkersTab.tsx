import { Badge } from "@/components/status"
import { InlineNotice, panelClass } from "@/components/ui/controls"
import { dateText, usePoll } from "@/lib/monitor"
import { cn } from "@/lib/utils"

interface WorkerRow {
  owner: string
  host: string
  pid: number
  role: string
  version: string
  started_at: string
  last_seen: string
  online: boolean
  scheduler: boolean
  task_kind: string
}
interface WorkersInfo {
  workers: WorkerRow[]
  online: number
  queue: { queued: number; running: number; oldest_wait_seconds: number }
  failures_1h: { kind: string; error: string; count: number }[]
  multi_worker: boolean
}

function waitText(s: number) {
  return s < 60 ? `${s} 秒` : s < 3600 ? `${Math.floor(s / 60)} 分钟` : `${Math.floor(s / 3600)} 小时`
}

/** Worker 状态（4.1 P9）：判断扩容是否有效——在线数、谁在调度、各自在做什么、队列等多久、最近为什么失败。只读。 */
export function WorkersTab() {
  const { data, error, refresh } = usePoll<WorkersInfo>("/api/workers", 10000)
  if (error) return <InlineNotice tone="bad" role="alert">{error} <button type="button" className="underline" onClick={refresh}>重试</button></InlineNotice>
  if (!data) return <p role="status" className="text-[0.8125rem] text-muted-foreground">正在读取 Worker 状态…</p>
  return (
    <div className="max-w-4xl space-y-4">
      <p role="status" aria-label="队列概况" className="text-[0.8125rem]">
        在线 Worker {data.online} 个 · 排队 {data.queue.queued} 个任务 · 执行中 {data.queue.running} 个 · 最早等待 {waitText(data.queue.oldest_wait_seconds)}
      </p>
      <p className="text-xs text-muted-foreground">
        {data.multi_worker
          ? "标准版（PostgreSQL）：可以运行多个 Worker 进程并行执行任务；只有一个调度者负责定时计划和回收超时任务，它退出后由其他 Worker 自动接替。"
          : "轻量版（SQLite）：只运行一个 Worker；再启动的 Worker 会等待，不会并行执行。"}
      </p>
      <ul className="space-y-2" aria-label="Worker 列表">
        {data.workers.map((w) => (
          <li key={w.owner} className={cn(panelClass, "flex flex-wrap items-center justify-between gap-2 p-3 text-[0.8125rem]")}>
            <span className="min-w-0">
              <span className="block break-all font-medium">{w.owner}</span>
              <span className="text-xs text-muted-foreground">
                {w.role === "executor" ? "只执行任务" : "执行任务并参与调度"} · 版本 {w.version || "未知"} · 启动 {dateText(w.started_at)} · 最近心跳 {dateText(w.last_seen)}
              </span>
            </span>
            <span className="flex flex-wrap items-center gap-1.5">
              <Badge tone={w.online ? "ok" : "bad"}>{w.online ? "在线" : "离线"}</Badge>
              {w.scheduler ? <Badge tone="info">调度者</Badge> : null}
              <Badge tone="neutral">{w.task_kind ? `执行中：${w.task_kind}` : "空闲"}</Badge>
            </span>
          </li>
        ))}
        {data.workers.length === 0 ? <li className="text-[0.8125rem] text-muted-foreground">没有 Worker 在运行：采集、告警和控制任务都不会执行。请启动 Worker（Docker 为 worker 容器，桌面版内置）。</li> : null}
      </ul>
      <section aria-label="最近 1 小时失败原因" className="space-y-2">
        <h2 className="text-[0.8125rem] font-semibold">最近 1 小时失败原因</h2>
        {data.failures_1h.length ? (
          <ul className="space-y-1 text-xs">
            {data.failures_1h.map((f) => (
              <li key={`${f.kind}:${f.error}`} className="break-words">
                <Badge tone="bad">{f.count} 次</Badge> <span className="font-medium">{f.kind}</span> — {f.error || "（无错误信息）"}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">没有失败的任务。</p>
        )}
      </section>
    </div>
  )
}
