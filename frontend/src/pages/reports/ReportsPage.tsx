import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { PageHeader } from "@/components/AppShell"
import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { ApiError, api } from "@/lib/api"
import { canEdit, useAuth } from "@/lib/auth"
import { dateText } from "@/lib/monitor"
import { InspectionSummary } from "@/components/AiNoteCard"

type Severity = "critical" | "warning" | "info"
interface Item { asset_id: string; asset_name: string; site: string; category: string; severity: Severity; title: string; detail: string }
interface Summary { id: string; kind: string; created_at: string; created_by: string; asset_count: number | null; affected_assets: number; by_severity: Record<Severity, number> }
interface Report extends Summary { items: Item[] }

const SEVERITY: Record<Severity, { label: string; cls: string }> = {
  critical: { label: "严重", cls: "bg-bad-bg text-foreground" },
  warning: { label: "警告", cls: "bg-warn-bg text-foreground" },
  info: { label: "提示", cls: "bg-secondary text-muted-foreground" },
}

/** 巡检报告（4.2 P16）：每天定时生成，也可手动生成；按账号的资产范围显示 */
export function ReportsPage({ embedded = false }: { embedded?: boolean }) {
  const { user } = useAuth()
  const [list, setList] = useState<Summary[] | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)

  // 只显示最后一次请求的报告：页面刚打开就点“立即巡检”时，晚到的列表不能把新报告换回旧的
  const latest = useRef(0)
  const open = useCallback((id: string) => {
    const ticket = ++latest.current
    void api.get<Report>(`/api/inspections/${id}`).then((r) => {
      if (ticket === latest.current) setReport(r)
    }, (e: Error) => setError(e.message))
  }, [])
  const load = useCallback(() => void api.get<Summary[]>("/api/inspections").then((rows) => {
    setList((cur) => [...(cur ?? []).filter((r) => !rows.some((x) => x.id === r.id)), ...rows])
    if (rows[0] && latest.current === 0) open(rows[0].id)
  }, (e: Error) => setError(e.message)), [open])
  useEffect(load, [load])

  async function runNow() {
    setBusy(true)
    setError("")
    const ticket = ++latest.current
    try {
      const r = await api.post<Report>("/api/inspections")
      if (ticket === latest.current) setReport(r)
      setList((cur) => [r, ...(cur ?? []).filter((x) => x.id !== r.id)])
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "生成失败")
    } finally {
      setBusy(false)
    }
  }

  const groups = useMemo(() => {
    const out = new Map<string, Item[]>()
    for (const i of report?.items ?? []) out.set(i.category, [...(out.get(i.category) ?? []), i])
    return [...out.entries()]
  }, [report])

  return (
    <div className="min-w-0 pb-8">
      <div className="print:hidden">
        {embedded ? (
          // 4.4 P21 起放在“巡检”板块的“每日巡检报告”里
          <div className="flex flex-wrap items-start justify-between gap-3 px-6 pb-4">
            <div className="min-w-0">
              <h2 className="text-base font-semibold">巡检报告</h2>
              <p className="mt-1 max-w-3xl text-[0.8125rem] text-muted-foreground">每天检查采集、温度、硬件健康与待处理的服务器告警、容量、备份与未处理告警，生成一份可导出、可打印的报告。温度和容量按告警规则判断（超过阈值用规则级别，距阈值 10% 以内记警告），没有规则覆盖的按固定值。</p>
            </div>
            {canEdit(user) ? <Button onClick={() => void runNow()} disabled={busy}>{busy ? "正在巡检…" : "立即巡检"}</Button> : null}
          </div>
        ) : (
          <PageHeader title="巡检报告" description="每天检查采集、温度、硬件健康与待处理的服务器告警、容量、备份与未处理告警，生成一份可导出、可打印的报告。温度和容量按告警规则判断（超过阈值用规则级别，距阈值 10% 以内记警告），没有规则覆盖的按固定值。"
            actions={canEdit(user) ? <Button onClick={() => void runNow()} disabled={busy}>{busy ? "正在巡检…" : "立即巡检"}</Button> : null} />
        )}
      </div>
      <div className="min-w-0 space-y-4 px-6">
        {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
        {list?.length === 0 ? <InlineNotice>还没有报告。每天早上 8 点后会自动生成第一份，也可以点“立即巡检”。</InlineNotice> : null}
        {list?.length ? (
          <label className="flex flex-wrap items-center gap-2 text-[0.8125rem] print:hidden">
            <span className="text-muted-foreground">历史报告</span>
            <select aria-label="历史报告" data-help-plain="" className="h-8 rounded-md border bg-card px-2" value={report?.id ?? ""} onChange={(e) => open(e.target.value)}>
              {list.map((r) => <option key={r.id} value={r.id}>{dateText(r.created_at)} · {r.kind === "daily" ? "每日" : "手动"} · 严重 {r.by_severity.critical} / 警告 {r.by_severity.warning}</option>)}
            </select>
          </label>
        ) : null}
        {report ? (
          <section aria-label="巡检结果" className="space-y-4">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-base font-semibold">巡检报告 · {dateText(report.created_at)}</h2>
              <div className="flex gap-2 print:hidden">
                <a className="inline-flex h-8 items-center rounded-md border px-3 text-[0.8125rem]" href={`/api/inspections/${report.id}/export.csv`} download>导出 CSV</a>
                <Button variant="outline" onClick={() => window.print()}>打印 / 另存 PDF</Button>
              </div>
            </div>
            <p className="text-[0.8125rem]">
              {report.asset_count != null ? `检查了 ${report.asset_count} 台在用资产，` : ""}{report.affected_assets} 台有问题：
              严重 <strong>{report.by_severity.critical}</strong> 项、警告 <strong>{report.by_severity.warning}</strong> 项。
              {report.created_by ? <span className="text-muted-foreground">（{report.created_by}）</span> : null}
            </p>
            <InspectionSummary reportId={report.id} />
            {report.items.length === 0 ? <InlineNotice tone="ok">没有发现问题。</InlineNotice> : groups.map(([category, items]) => (
              <section key={category} aria-label={category}>
                <h3 className="mb-1 text-[0.8125rem] font-semibold">{category}（{items.length}）</h3>
                <ul className="divide-y rounded-md border text-[0.8125rem]">
                  {items.map((i, n) => (
                    <li key={n} className="flex flex-wrap gap-x-3 gap-y-1 px-3 py-2 break-inside-avoid">
                      <span className={`rounded px-1.5 py-0.5 text-xs ${SEVERITY[i.severity].cls}`}>{SEVERITY[i.severity].label}</span>
                      <a className="font-medium hover:underline" href={`#/assets?asset=${i.asset_id}`}>{i.asset_name}</a>
                      {i.site ? <span className="text-muted-foreground">{i.site}</span> : null}
                      <span className="min-w-0 basis-full break-words sm:basis-auto">{i.title}</span>
                      {i.detail ? <span className="min-w-0 basis-full break-words text-xs text-muted-foreground">{i.detail}</span> : null}
                    </li>
                  ))}
                </ul>
              </section>
            ))}
            <p className="text-xs text-muted-foreground">证书到期、时钟漂移等没有数据来源的项目不在检查范围内；过期的指标不参与判断。</p>
          </section>
        ) : null}
      </div>
    </div>
  )
}
