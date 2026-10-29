import { useState } from "react"
import { cn } from "@/lib/utils"
import { PageHeader } from "@/components/AppShell"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Card, InlineNotice, Segmented, Select, panelClass } from "@/components/ui/controls"
import { api, query, type Page } from "@/lib/api"
import { canEdit, seesAll, useAuth } from "@/lib/auth"
import { dateText, numberText, usePoll } from "@/lib/monitor"
import { ChannelsPanel } from "./ChannelsPanel"
import { RulesPanel } from "./RulesPanel"
import { MaintenancePanel } from "./MaintenancePanel"
import { HistoryPanel } from "./HistoryPanel"
import { type Alert, Field, Input, severityLabels, stateLabels, alertStateLabel, useMutation } from "./shared"
import { ScopeNotice } from "@/components/ScopeNote"
import { AlertExplain } from "@/components/AiNoteCard"

type Tab = "current" | "history" | "rules" | "channels" | "maintenance"
export function AlertsPage() {
  const [tab, setTab] = useState<Tab>("current")
  const { user } = useAuth(),
    editable = canEdit(user)
  const summary = usePoll<Record<string, number>>("/api/alerts/summary")
  return (
    <div className="min-w-0 pb-8 [&_button]:h-auto [&_button]:min-h-8 [&_button]:min-w-0 [&_button]:max-w-full [&_button]:whitespace-normal [&_button]:break-words [&_button]:py-1.5 [&_p]:break-words">
      <PageHeader
        title="告警中心"
        description="查看告警、确认处置并配置通知。缺失、过期或未配置的数据不会直接判定设备离线。"
      />
      <div className="min-w-0 space-y-4 px-6">
        <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm" aria-label="告警概况">
          {["active", "pending", "acknowledged", "silenced", "suppressed"].map((key) => (
            <span key={key}>
              {stateLabels[key]} <strong className="tabular">{summary.data?.[key] ?? "—"}</strong>
            </span>
          ))}
          <span>
            投递失败 <strong>{summary.data?.failed_deliveries ?? "—"}</strong>
          </span>
        </div>
        {summary.error ? (
          <InlineNotice tone="bad" role="alert">
            {summary.error}
          </InlineNotice>
        ) : null}
        {!editable ? <InlineNotice>当前账户为只读，可查看告警与配置。</InlineNotice> : null}
        <Segmented
          label="告警中心内容"
          value={tab}
          onChange={setTab}
          options={[
            { value: "current", label: "当前告警" },
            { value: "history", label: "告警历史" },
            { value: "rules", label: "告警规则" },
            { value: "channels", label: "通知与投递" },
            { value: "maintenance", label: "维护窗口" },
          ]}
        />
        {tab === "history" ? (
          <HistoryPanel />
        ) : tab === "current" ? (
          <AlertList key={tab} history={false} editable={editable} changed={summary.refresh} />
        ) : !seesAll(user) ? (
          <ScopeNotice what="告警规则、通知渠道与维护窗口是全局配置，" />
        ) : tab === "rules" ? (
          <RulesPanel editable={editable} />
        ) : tab === "channels" ? (
          <ChannelsPanel editable={editable} />
        ) : (
          <MaintenancePanel editable={editable} />
        )}
      </div>
    </div>
  )
}

function AlertList({ history, editable, changed }: { history: boolean; editable: boolean; changed: () => void }) {
  const [state, setState] = useState(history ? "" : "active"),
    [q, setQ] = useState(""),
    [search, setSearch] = useState(""),
    [offset, setOffset] = useState(0),
    [selected, setSelected] = useState("")
  const alerts = usePoll<Page<Alert>>(`/api/alerts${query({ state, q, limit: 100, offset })}`)
  const mut = useMutation()
  async function action(id: string, action: string, minutes?: number) {
    await mut.run(async () => {
      await api.post(`/api/alerts/${id}/action`, { action, minutes })
      alerts.refresh()
      changed()
    }, "告警操作已保存")
  }
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
        <Field label="搜索告警" plain>
          <Input
            value={search}
            maxLength={100}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="资产或告警名称"
          />
        </Field>
        <Select
          id="alert-state"
          label="告警状态"
          plain
          value={state}
          onChange={(v) => {
            setState(v)
            setOffset(0)
          }}
          options={[
            { value: "", label: "全部状态" },
            ...Object.entries(stateLabels).map(([value, label]) => ({
              value,
              label,
            })),
          ]}
        />
        <Button type="submit" variant="outline">
          搜索告警
        </Button>
        <Button type="button" variant="outline" onClick={alerts.refresh}>
          刷新告警
        </Button>
      </form>
      {mut.feedback}
      {alerts.error ? (
        <InlineNotice tone="bad" role="alert">
          {alerts.error}
        </InlineNotice>
      ) : null}
      <div className={cn(panelClass, "overflow-x-auto")}>
        <table className="data-table w-full min-w-[780px]">
          <caption className="sr-only">{history ? "告警历史列表" : "当前告警列表"}</caption>
          <thead>
            <tr>
              {["告警 / 资产", "状态与级别", "当前值 / 阈值", "最近变化", "处置"].map((t) => (
                <th key={t} scope="col">
                  {t}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {alerts.data?.items.map((a) => (
              <tr key={a.id}>
                <th scope="row" className="max-w-64 break-words font-normal">
                  <Button size="sm" variant="link" onClick={() => setSelected(selected === a.id ? "" : a.id)}>
                    {a.title}
                  </Button>
                  <p className="text-xs text-muted-foreground">
                    {a.asset_name ?? "资产已删除"} · {a.metric_key}
                  </p>
                </th>
                <td>
                  <Badge tone={a.state === "resolved" ? "ok" : a.severity === "critical" ? "bad" : "warn"}>
                    {alertStateLabel(a)} · {severityLabels[a.severity]}
                  </Badge>
                  {a.silenced_until ? <p className="mt-1 text-xs">静默至 {dateText(a.silenced_until)}</p> : null}
                  {a.suppression_reason ? <p className="mt-1 max-w-56 text-xs">说明：{a.suppression_reason}</p> : null}
                </td>
                <td>
                  {numberText(a.value)} {a.unit}
                  <p className="text-xs text-muted-foreground">阈值 {numberText(a.threshold)}</p>
                </td>
                <td className="text-xs">
                  {dateText(a.last_seen_at)}
                  <p className="text-muted-foreground">首次 {dateText(a.first_seen_at)}</p>
                  {a.resolved_at ? <p>结束 {dateText(a.resolved_at)}</p> : null}
                </td>
                <td>
                  {editable && !["resolved", "pending"].includes(a.state) ? (
                    <div className="flex flex-wrap gap-2">
                      {!a.acknowledged_by ? (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={mut.busy}
                          onClick={() => void action(a.id, "acknowledge")}
                        >
                          确认告警
                        </Button>
                      ) : (
                        <span className="text-xs">已确认</span>
                      )}
                      {a.silenced_until ? (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={mut.busy}
                          onClick={() => void action(a.id, "unsilence")}
                        >
                          解除静默
                        </Button>
                      ) : (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={mut.busy}
                          onClick={() => void action(a.id, "silence", 60)}
                        >
                          静默 1 小时
                        </Button>
                      )}
                    </div>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!alerts.data ? (
          <p className="p-6 text-sm">正在加载告警…</p>
        ) : alerts.data.items.length === 0 ? (
          <p className="p-6 text-sm text-muted-foreground">没有符合条件的告警。可在告警规则中配置判断条件。</p>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <span>共 {alerts.data?.total ?? "—"} 项 · 每 5 秒刷新</span>
        <Button
          size="sm"
          variant="outline"
          disabled={offset === 0}
          onClick={() => setOffset((n) => Math.max(0, n - 100))}
        >
          上一页
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={!alerts.data || offset + 100 >= alerts.data.total}
          onClick={() => setOffset((n) => n + 100)}
        >
          下一页
        </Button>
      </div>
      {selected ? <AlertExplain alertId={selected} /> : null}
      {selected ? <EventList id={selected} close={() => setSelected("")} /> : null}
    </div>
  )
}
function EventList({ id, close }: { id: string; close: () => void }) {
  const events = usePoll<
    Page<{
      id: string
      kind: string
      title: string
      message: string
      value: number | null
      created_at: string
    }>
  >(`/api/alerts/events${query({ alert_id: id, limit: 100 })}`)
  const labels: Record<string, string> = {
    trigger: "触发",
    recover: "恢复",
    cancelled: "未达到触发条件",
    closed: "规则实例关闭",
    acknowledge: "确认",
    silence: "静默",
    unsilence: "解除静默",
    unsuppressed: "解除抑制",
    activated: "触发",
    resolved: "恢复",
    acknowledged: "确认",
    silenced: "静默",
    unsilenced: "解除静默",
    repeat: "重复通知",
    suppressed: "抑制",
    pending: "等待条件",
    updated: "更新",
  }
  return (
    <Card
      title="告警事件记录"
      actions={
        <Button size="sm" variant="outline" onClick={close}>
          关闭记录
        </Button>
      }
    >
      {events.error ? (
        <InlineNotice tone="bad" role="alert">
          {events.error}
        </InlineNotice>
      ) : null}
      <ol className="divide-y">
        {events.data?.items.map((e) => (
          <li key={e.id} className="space-y-1 py-3 text-sm">
            <p>
              {labels[e.kind] ?? e.kind} · {e.title}
            </p>
            <p className="break-words text-xs text-muted-foreground">{e.message}</p>
            <p className="text-xs">
              {dateText(e.created_at)} · 值 {numberText(e.value)}
            </p>
          </li>
        ))}
      </ol>
      {events.data?.items.length === 0 ? <p className="text-sm text-muted-foreground">暂无事件。</p> : null}
    </Card>
  )
}
