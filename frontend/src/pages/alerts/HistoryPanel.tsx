import { useState } from "react"
import { Button } from "@/components/ui/button"
import { InlineNotice, panelClass } from "@/components/ui/controls"
import { type Page, query } from "@/lib/api"
import { dateText, numberText, usePoll } from "@/lib/monitor"
import { cn } from "@/lib/utils"
import { Field, Input } from "./shared"

interface Event {
  id: string
  alert_id: string
  kind: string
  title: string
  message: string
  value: number | null
  threshold: number | null
  unit: string | null
  metric_key: string | null
  severity: string
  episode: number
  created_at: string
}
const labels: Record<string, string> = {
  pending: "等待条件",
  trigger: "触发",
  repeat: "重复提醒",
  recover: "恢复",
  cancelled: "未达到触发条件",
  closed: "规则实例关闭",
  acknowledge: "确认",
  silence: "静默",
  unsilence: "解除静默",
  unsilenced: "静默到期",
  suppressed: "抑制",
  unsuppressed: "解除抑制",
}
export function HistoryPanel() {
  const [search, setSearch] = useState("")
  const [q, setQ] = useState("")
  const [offset, setOffset] = useState(0)
  const history = usePoll<Page<Event>>(`/api/alerts/events${query({ q, offset, limit: 50 })}`, 5000)
  return (
    <div className="min-w-0 space-y-3">
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          setQ(search)
          setOffset(0)
        }}
      >
        <Field label="搜索历史事件" plain>
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            maxLength={100}
            placeholder="资产、规则或事件说明"
          />
        </Field>
        <Button type="submit" variant="outline">
          搜索历史
        </Button>
        <Button type="button" variant="outline" onClick={history.refresh}>
          刷新历史
        </Button>
      </form>
      <p className="text-xs text-muted-foreground">
        历史按事件发生时间保存；数值与阈值使用当时快照，修改规则不会改写历史。
      </p>
      {history.error ? (
        <InlineNotice role="alert" tone="bad">
          {history.error}
        </InlineNotice>
      ) : null}
      <div className={cn(panelClass, "overflow-x-auto")}>
        <table className="data-table w-full min-w-[720px]">
          <caption className="sr-only">告警历史事件</caption>
          <thead>
            <tr>
              {["时间", "事件 / 告警", "当时数值 / 阈值", "说明"].map((name) => (
                <th key={name} scope="col">
                  {name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {history.data?.items.map((event) => (
              <tr key={event.id}>
                <td className="text-xs">{dateText(event.created_at)}</td>
                <th scope="row" className="max-w-64 break-words text-left font-normal">
                  <p className="font-medium">{event.title}</p>
                  <p className="mt-1 text-xs">
                    {labels[event.kind] ?? event.kind} · 第 {event.episode} 次触发周期
                  </p>
                </th>
                <td>
                  {numberText(event.value)} {event.unit ?? ""}
                  <p className="text-xs text-muted-foreground">
                    阈值 {numberText(event.threshold)} · {event.metric_key ?? ""}
                  </p>
                </td>
                <td className="max-w-80 break-words text-xs">{event.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {history.data?.total === 0 ? (
          <p className="p-6 text-sm text-muted-foreground">暂无符合条件的历史事件。</p>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <span>共 {history.data?.total ?? "—"} 条事件 · 每页 50 条</span>
        <Button
          size="sm"
          variant="outline"
          disabled={offset === 0}
          onClick={() => setOffset((n) => Math.max(0, n - 50))}
        >
          上一页历史
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={!history.data || offset + 50 >= history.data.total}
          onClick={() => setOffset((n) => n + 50)}
        >
          下一页历史
        </Button>
      </div>
    </div>
  )
}
