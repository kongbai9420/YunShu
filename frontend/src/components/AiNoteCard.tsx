import { useCallback, useEffect, useState } from "react"

import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { ApiError, api } from "@/lib/api"
import { canEdit, useAuth } from "@/lib/auth"
import { dateText } from "@/lib/monitor"

/** AI 告警解读 / 巡检摘要卡片（4.3 P18f）。只对可见全部资产的账号显示；结果标注“AI 生成，仅供参考”。 */

type Result = Record<string, unknown>
interface Note { result: Result; provider: string; model: string; created_by: string; created_at: string }

const TONE: Record<string, "bad" | "warn" | "info" | "ok" | "neutral"> = {
  立即处理: "bad", 尽快处理: "warn", 持续观察: "info", 需立即处理: "bad", 需关注: "warn", 良好: "ok",
}

export function AiNoteCard({ url, title, button, render }: { url: string; title: string; button: string; render: (r: Result) => React.ReactNode }) {
  const { user } = useAuth()
  const [state, setState] = useState<{ enabled: boolean; note: Note | null } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [hidden, setHidden] = useState(false)

  const load = useCallback(() => {
    setState(null)
    api.get<{ enabled: boolean; note: Note | null }>(url).then(setState, (e) => {
      if (e instanceof ApiError && e.status === 403) setHidden(true)
      else setError((e as Error).message)
    })
  }, [url])
  useEffect(load, [load])

  async function run() {
    setBusy(true)
    setError("")
    try {
      setState(await api.post<{ enabled: boolean; note: Note }>(`${url}${state?.note ? "?force=true" : ""}`))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "调用失败")
    } finally {
      setBusy(false)
    }
  }

  if (hidden || !state || (!state.enabled && !state.note)) return null
  const n = state.note
  return (
    <section aria-label={title} className="space-y-1.5 rounded-lg border p-3 text-[0.8125rem] print:break-inside-avoid">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-medium">{title} <span className="text-xs font-normal text-muted-foreground">AI 生成，仅供参考</span></h3>
        {state.enabled && canEdit(user) ? (
          <Button size="sm" variant="outline" className="print:hidden" disabled={busy} onClick={() => void run()}>{busy ? "生成中…" : n ? "重新生成" : button}</Button>
        ) : null}
      </div>
      {n ? (
        <>
          {render(n.result)}
          <p className="text-xs text-muted-foreground">{n.provider} · {n.model} · {dateText(n.created_at)}{n.created_by ? ` · ${n.created_by}` : ""}</p>
        </>
      ) : <p className="text-xs text-muted-foreground">还没有生成。</p>}
      {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
    </section>
  )
}

export function Level({ value }: { value: unknown }) {
  const v = String(value ?? "")
  return v ? <Badge tone={TONE[v] ?? "neutral"}>{v}</Badge> : null
}

export function AlertExplain({ alertId }: { alertId: string }) {
  return (
    <AiNoteCard url={`/api/alerts/${alertId}/explain`} title="AI 告警解读" button="AI 解读" render={(r) => {
      const causes = (r.likely_causes as { cause: string; confidence: string; evidence: string }[]) ?? []
      const checks = (r.checks as string[]) ?? []
      return (
        <div className="space-y-1">
          <p className="font-medium">{String(r.summary ?? "")} <Level value={r.urgency} /></p>
          {causes.length ? <div><span className="text-xs text-muted-foreground">可能原因：</span>
            <ul className="ml-4 list-disc text-xs">{causes.map((c, i) => <li key={i}>{c.cause}（可能性{c.confidence}）{c.evidence ? ` — ${c.evidence}` : ""}</li>)}</ul></div> : null}
          {checks.length ? <div><span className="text-xs text-muted-foreground">建议检查：</span>
            <ol className="ml-4 list-decimal text-xs">{checks.map((c, i) => <li key={i}>{c}</li>)}</ol></div> : null}
          {r.note ? <p className="text-xs text-muted-foreground">{String(r.note)}</p> : null}
        </div>
      )
    }} />
  )
}

export function InspectionSummary({ reportId }: { reportId: string }) {
  return (
    <AiNoteCard url={`/api/inspections/${reportId}/summary`} title="AI 摘要" button="生成摘要" render={(r) => {
      const issues = (r.top_issues as { title: string; why: string; action: string }[]) ?? []
      return (
        <div className="space-y-1">
          <p className="font-medium">{String(r.headline ?? "")} <Level value={r.overall} /></p>
          {issues.length ? <ol className="ml-4 list-decimal text-xs">{issues.map((x, i) => <li key={i}><strong>{x.title}</strong>：{x.why}；建议：{x.action}</li>)}</ol> : null}
          {r.changes ? <p className="text-xs">与上一份相比：{String(r.changes)}</p> : null}
        </div>
      )
    }} />
  )
}
