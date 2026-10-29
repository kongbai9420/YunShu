import { useCallback, useEffect, useState } from "react"

import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { ApiError, api } from "@/lib/api"
import { canEdit, useAuth } from "@/lib/auth"
import { dateText } from "@/lib/monitor"

/** AI 影响分析（4.3 P18e）：程序给出的比对结果之外，AI 只做解释与建议，结果标注“AI 生成，仅供参考”。 */

interface Result { conclusion: string; impact: string; exploit_conditions: string[]; priority: string; actions: string[]; evidence: string[] }
interface Analysis { result: Result; provider: string; model: string; created_by: string; created_at: string }

const PRIORITY_TONE: Record<string, "bad" | "warn" | "info" | "neutral"> = { 立即处理: "bad", 尽快处理: "warn", 计划处理: "info", 可暂缓: "neutral" }

export function AiAnalysis({ recordId }: { recordId: string }) {
  const { user } = useAuth()
  const [state, setState] = useState<{ enabled: boolean; analysis: Analysis | null } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [preview, setPreview] = useState<unknown>(null)
  const [hidden, setHidden] = useState(false)

  const load = useCallback(() => {
    api.get<{ enabled: boolean; analysis: Analysis | null }>(`/api/vuln/records/${encodeURIComponent(recordId)}/analysis`).then(setState, (e) => {
      if (e instanceof ApiError && e.status === 403) setHidden(true)  // 只对可见全部资产的账号开放
      else setError((e as Error).message)
    })
  }, [recordId])
  useEffect(load, [load])

  async function run(force: boolean) {
    setBusy(true)
    setError("")
    try {
      setState(await api.post<{ enabled: boolean; analysis: Analysis }>(`/api/vuln/records/${encodeURIComponent(recordId)}/analysis${force ? "?force=true" : ""}`))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "分析失败")
    } finally {
      setBusy(false)
    }
  }

  async function showPreview() {
    try {
      setPreview(await api.get(`/api/vuln/records/${encodeURIComponent(recordId)}/analysis/preview`))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "预览失败")
    }
  }

  if (hidden) return null
  if (!state) return null
  const a = state.analysis
  const editable = canEdit(user)
  return (
    <section aria-label="AI 影响分析" className="space-y-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="font-medium">AI 影响分析 <span className="text-xs font-normal text-muted-foreground">AI 生成，仅供参考；是否受影响以上面的比对结果为准</span></h4>
        {state.enabled && editable ? (
          <div className="flex gap-2">
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(Boolean(a))}>{busy ? "分析中…" : a ? "重新分析" : "AI 分析"}</Button>
            <Button size="sm" variant="ghost" onClick={() => void showPreview()}>预览将发送的内容</Button>
          </div>
        ) : null}
      </div>
      {!state.enabled ? <p className="text-xs text-muted-foreground">未开启。管理员可以在“平台设置 → AI”里开启“漏洞影响分析”。</p> : null}
      {a ? (
        <div className="space-y-1.5 text-[0.8125rem]">
          <p className="font-medium">{a.result.conclusion} <Badge tone={PRIORITY_TONE[a.result.priority] ?? "neutral"}>{a.result.priority}</Badge></p>
          <p className="whitespace-pre-wrap">{a.result.impact}</p>
          {a.result.exploit_conditions.length ? <div><span className="text-xs text-muted-foreground">利用条件：</span><ul className="ml-4 list-disc text-xs">{a.result.exploit_conditions.map((x, i) => <li key={i}>{x}</li>)}</ul></div> : null}
          {a.result.actions.length ? <div><span className="text-xs text-muted-foreground">处置建议：</span><ol className="ml-4 list-decimal text-xs">{a.result.actions.map((x, i) => <li key={i}>{x}</li>)}</ol></div> : null}
          {a.result.evidence.length ? <div><span className="text-xs text-muted-foreground">依据：</span><ul className="ml-4 list-disc text-xs">{a.result.evidence.map((x, i) => <li key={i}>{x}</li>)}</ul></div> : null}
          <p className="text-xs text-muted-foreground">{a.provider} · {a.model} · {dateText(a.created_at)}{a.created_by ? ` · ${a.created_by}` : ""}</p>
        </div>
      ) : state.enabled ? <p className="text-xs text-muted-foreground">还没有分析。新漏洞命中本地且需要即时通知时会自动分析一次。</p> : null}
      {preview ? (
        <details open className="text-xs">
          <summary className="cursor-pointer">将发送给 AI 的内容（已脱敏；代号对照只在本地显示）</summary>
          <pre className="max-h-64 overflow-auto rounded bg-secondary p-2">{JSON.stringify(preview, null, 1)}</pre>
        </details>
      ) : null}
      {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
    </section>
  )
}
