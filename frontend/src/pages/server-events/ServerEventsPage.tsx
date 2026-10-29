import { Dialog } from "radix-ui"
import { useState } from "react"

import { PageHeader } from "@/components/AppShell"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice, Select, panelClass } from "@/components/ui/controls"
import { ModalContent, ModalFooter, ModalHeader } from "@/components/ui/dialog"
import { TextField } from "@/components/ui/form"
import { api, query } from "@/lib/api"
import { canEdit, useAuth } from "@/lib/auth"
import { dateText, sourceLabel, usePoll } from "@/lib/monitor"
import { cn } from "@/lib/utils"

/** 服务器告警（4.4 P20c）：BMC 系统事件日志里的严重 / 警告事件，逐条查看与处理。 */

interface ServerEvent {
  id: string
  asset_id: string
  asset_name: string
  source: string
  occurred_at: string
  severity: "critical" | "warning" | "info"
  category: string
  category_label: string
  message: string
  first_seen_at: string
  state: "open" | "handled" | "archived"
  handled_by: string
  handled_at: string | null
  note: string
}
interface EventPage {
  total: number
  items: ServerEvent[]
  categories: Record<string, string>
}
interface Summary {
  open: Record<"critical" | "warning", { events: number; servers: number }>
}

const SEVERITY: Record<string, { label: string; tone: "bad" | "warn" | "neutral" }> = {
  critical: { label: "严重", tone: "bad" },
  warning: { label: "警告", tone: "warn" },
  info: { label: "提示", tone: "neutral" },
}
const STATE_LABEL: Record<string, string> = { open: "待处理", handled: "已处理", archived: "已归档" }
const PAGE = 50

export function ServerEventsPage() {
  const { user } = useAuth()
  const editable = canEdit(user)
  const [state, setState] = useState("open")
  const [severity, setSeverity] = useState("alarm")
  const [category, setCategory] = useState("")
  const [days, setDays] = useState("30")
  const [search, setSearch] = useState("")
  const [q, setQ] = useState("")
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<string[]>([])
  const [handling, setHandling] = useState<"handled" | "open" | null>(null)
  const path = `/api/server-events${query({ state, severity, category, days, q, limit: PAGE, offset })}`
  const page = usePoll<EventPage>(path, 30000)
  const summary = usePoll<Summary>("/api/server-events/summary", 30000)
  const items = page.data?.items ?? []
  const reset = (fn: () => void) => {
    fn()
    setOffset(0)
    setSelected([])
  }
  const toggle = (id: string) => setSelected((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]))
  const categories = Object.entries(page.data?.categories ?? {}).map(([value, label]) => ({ value, label }))
  return (
    <div className="min-w-0 pb-8 [&_p]:break-words">
      <PageHeader
        title="服务器告警"
        description="服务器 BMC 自己记录的硬件事件：温度、风扇、电源、内存、硬盘等。按采集计划由 Redfish 定时读取 BMC 日志（不可用时由 IPMI 兜底），在 BMC 上配置了 Trap 目标或 Redfish 事件订阅的会立即推送过来；只读，不会清空 BMC 日志。有待处理的严重事件时按告警规则“服务器出现严重硬件事件”通知，全部标记已处理后告警恢复。"
      />
      <div className="min-w-0 space-y-4 px-6">
        <div className="grid grid-cols-[minmax(0,1fr)] gap-3 sm:grid-cols-2">
          {(["critical", "warning"] as const).map((sev) => {
            const s = summary.data?.open[sev]
            return (
              <div key={sev} className={cn(panelClass, "p-3")}>
                <p className="text-xs text-muted-foreground">待处理的{SEVERITY[sev].label}事件</p>
                <p className="mt-1 text-xl font-semibold">
                  {s ? s.events : "—"}
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    {s ? `涉及 ${s.servers} 台服务器` : ""}
                  </span>
                </p>
              </div>
            )
          })}
        </div>
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            reset(() => setQ(search))
          }}
        >
          <Select
            id="se-state"
            label="状态"
            value={state}
            onChange={(v) => reset(() => setState(v))}
            options={[
              { value: "open", label: "待处理" },
              { value: "handled", label: "已处理" },
              { value: "archived", label: "已归档" },
              { value: "", label: "全部" },
            ]}
            plain
          />
          <Select
            id="se-severity"
            label="级别"
            value={severity}
            onChange={(v) => reset(() => setSeverity(v))}
            options={[
              { value: "alarm", label: "严重与警告" },
              { value: "critical", label: "严重" },
              { value: "warning", label: "警告" },
              { value: "info", label: "提示" },
              { value: "", label: "全部" },
            ]}
            plain
          />
          <Select
            id="se-category"
            label="部件"
            value={category}
            onChange={(v) => reset(() => setCategory(v))}
            options={[{ value: "", label: "全部" }, ...categories]}
            plain
          />
          <Select
            id="se-days"
            label="时间"
            value={days}
            onChange={(v) => reset(() => setDays(v))}
            options={[
              { value: "1", label: "近 24 小时" },
              { value: "7", label: "近 7 天" },
              { value: "30", label: "近 30 天" },
              { value: "180", label: "近 180 天" },
            ]}
            plain
          />
          <TextField
            id="se-q"
            label="搜索"
            value={search}
            onChange={setSearch}
            placeholder="服务器名称或事件文字"
            className="min-w-48 flex-1"
            plain
          />
          <Button type="submit" size="sm" variant="outline">
            搜索
          </Button>
        </form>
        {page.error ? (
          <InlineNotice tone="bad" role="alert">
            {page.error}
          </InlineNotice>
        ) : null}
        {editable && selected.length ? (
          <div className="flex flex-wrap items-center gap-2 text-[0.8125rem]">
            <span>已选 {selected.length} 条</span>
            {state !== "handled" ? (
              <Button size="sm" onClick={() => setHandling("handled")}>
                标记已处理
              </Button>
            ) : null}
            {state !== "open" ? (
              <Button size="sm" variant="outline" onClick={() => setHandling("open")}>
                重新打开
              </Button>
            ) : null}
          </div>
        ) : null}
        {!page.data && !page.error ? <p className="text-muted-foreground">正在读取…</p> : null}
        {page.data && !items.length ? (
          <InlineNotice>
            {state === "open"
              ? "没有待处理的服务器告警。服务器需要有 Redfish 或 IPMI 端点并关联只读凭据，平台随采集定期读取 BMC 日志；首次读到时，7 天前的旧记录和提示类事件直接归档。"
              : "没有符合条件的事件。"}
          </InlineNotice>
        ) : null}
        {items.length ? (
          <div className={cn(panelClass, "min-w-0 overflow-x-auto")}>
            <table className="data-table w-full min-w-[44rem]">
              <caption className="sr-only">服务器告警列表</caption>
              <thead>
                <tr>
                  {editable ? (
                    <th className="w-8">
                      <input
                        type="checkbox"
                        data-help-plain=""
                        aria-label="全选本页"
                        checked={items.every((i) => selected.includes(i.id))}
                        onChange={(e) => setSelected(e.target.checked ? items.map((i) => i.id) : [])}
                      />
                    </th>
                  ) : null}
                  <th>时间</th>
                  <th>服务器</th>
                  <th>级别 / 部件</th>
                  <th>事件</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {items.map((e) => (
                  <tr key={e.id} className="border-t align-top">
                    {editable ? (
                      <td>
                        <input
                          type="checkbox"
                          data-help-plain=""
                          aria-label={`选择 ${e.asset_name} ${dateText(e.occurred_at)} 的事件`}
                          checked={selected.includes(e.id)}
                          onChange={() => toggle(e.id)}
                        />
                      </td>
                    ) : null}
                    <td className="whitespace-nowrap text-xs">{dateText(e.occurred_at)}</td>
                    <td>
                      <a className="text-link" href={`#/assets?asset=${e.asset_id}`}>
                        {e.asset_name}
                      </a>
                      <br />
                      <span className="text-xs text-muted-foreground">来自 {sourceLabel(e.source)}</span>
                    </td>
                    <td>
                      <Badge tone={SEVERITY[e.severity]?.tone ?? "neutral"} className="whitespace-nowrap">{SEVERITY[e.severity]?.label ?? e.severity}</Badge>
                      <br />
                      <span className="text-xs text-muted-foreground">{e.category_label}</span>
                    </td>
                    <td className="max-w-[28rem] break-words">{e.message || "（BMC 没有给出文字）"}</td>
                    <td className="min-w-24 text-xs">
                      <span className="whitespace-nowrap">{STATE_LABEL[e.state] ?? e.state}</span>
                      {e.state === "handled" ? (
                        <>
                          <br />
                          {e.handled_by} · {dateText(e.handled_at)}
                          <br />
                          {e.note}
                        </>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        {page.data && page.data.total > PAGE ? (
          <div className="flex items-center gap-2 text-[0.8125rem]">
            <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
              上一页
            </Button>
            <span>
              第 {offset / PAGE + 1} / {Math.ceil(page.data.total / PAGE)} 页，共 {page.data.total} 条
            </span>
            <Button
              size="sm"
              variant="outline"
              disabled={offset + PAGE >= page.data.total}
              onClick={() => setOffset(offset + PAGE)}
            >
              下一页
            </Button>
          </div>
        ) : null}
      </div>
      {handling ? (
        <HandleDialog
          ids={selected}
          state={handling}
          onClose={() => setHandling(null)}
          onDone={() => {
            setHandling(null)
            setSelected([])
            page.refresh()
            summary.refresh()
          }}
        />
      ) : null}
    </div>
  )
}

function HandleDialog({
  ids,
  state,
  onClose,
  onDone,
}: {
  ids: string[]
  state: "handled" | "open"
  onClose: () => void
  onDone: () => void
}) {
  const [note, setNote] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const handled = state === "handled"
  async function submit() {
    setBusy(true)
    setError("")
    try {
      await api.post("/api/server-events/state", { ids, state, note })
      onDone()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog.Root open onOpenChange={(o) => (!o ? onClose() : undefined)}>
      <ModalContent width={480}>
        <ModalHeader
          title={`${handled ? "标记已处理" : "重新打开"}：${ids.length} 条`}
          description={handled ? "写明处理结果，会和处理人、时间一起保存并写入审计。BMC 上的日志不会被清空。" : "重新打开后回到“待处理”。"}
        />
        <TextField
          id="se-note"
          label={handled ? "处理结果" : "说明（可选）"}
          value={note}
          onChange={setNote}
          placeholder={handled ? "例如：已更换 2 号电源，BMC 显示恢复正常" : ""}
          className="mt-3"
          help="serverEvents.note"
        />
        {error ? (
          <InlineNotice tone="bad" role="alert" className="mt-3">
            {error}
          </InlineNotice>
        ) : null}
        <ModalFooter>
          <Dialog.Close asChild>
            <Button variant="outline">取消</Button>
          </Dialog.Close>
          <Button disabled={busy || (handled && !note.trim())} onClick={() => void submit()}>
            {busy ? "正在提交…" : handled ? "确认已处理" : "重新打开"}
          </Button>
        </ModalFooter>
      </ModalContent>
    </Dialog.Root>
  )
}
