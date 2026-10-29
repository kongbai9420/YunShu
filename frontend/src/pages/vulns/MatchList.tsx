import { useState } from "react"

import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice, Select } from "@/components/ui/controls"
import { TextField } from "@/components/ui/form"
import { ApiError, api } from "@/lib/api"
import { dateText } from "@/lib/monitor"

import type { FixText } from "./Remediation"
import { SEVERITY_TONE } from "./VulnsPage"

/** 本地影响（4.3 P18d）：哪台资产的哪个版本命中、修复版本、处置状态。管理员可以处置。 */

export interface Match {
  id: string; asset_id: string; asset_name: string; record_id: string; product: string; label: string; installed_version: string; fixed_version: string
  confidence: string; note: string; active: boolean; first_seen_at: string | null; resolved_at: string | null; status: string; status_text: string
  reason: string; review_at: string | null; status_by: string; status_at: string | null
  title?: string; severity?: string; severity_text?: string; cvss?: number | null; kev?: boolean
}

const STATUS_OPTIONS = [
  { value: "open", label: "待处理" }, { value: "affected", label: "确认受影响" }, { value: "not_affected", label: "不受影响" },
  { value: "mitigated", label: "已缓解" }, { value: "accepted", label: "接受风险" }, { value: "false_positive", label: "误报" },
]
const STATUS_TONE: Record<string, "bad" | "warn" | "ok" | "neutral" | "info"> = {
  open: "warn", affected: "bad", not_affected: "ok", mitigated: "info", accepted: "neutral", false_positive: "ok",
}

export function MatchList({ matches, editable, showRecord, onChanged, onOpenRecord, suggestions }: {
  matches: Match[]; editable: boolean; showRecord?: boolean; onChanged: () => void; onOpenRecord?: (id: string) => void
  /** 来源给出的缓解 / 修复原文：处置为“已缓解”时可一键引用为理由 */
  suggestions?: FixText[]
}) {
  if (!matches.length) return <p className="text-xs text-muted-foreground">没有命中的资产。</p>
  return (
    <ul className="space-y-2" aria-label="本地影响">
      {matches.map((m) => <MatchRow key={m.id} m={m} editable={editable} showRecord={showRecord} onChanged={onChanged} onOpenRecord={onOpenRecord} suggestions={suggestions} />)}
    </ul>
  )
}

function MatchRow({ m, editable, showRecord, onChanged, onOpenRecord, suggestions }: {
  m: Match; editable: boolean; showRecord?: boolean; onChanged: () => void; onOpenRecord?: (id: string) => void; suggestions?: FixText[]
}) {
  const [editing, setEditing] = useState(false)
  const [status, setStatus] = useState(m.status)
  const [reason, setReason] = useState(m.reason)
  const [review, setReview] = useState(m.review_at ?? "")
  const [error, setError] = useState("")

  async function save() {
    setError("")
    try {
      await api.patch(`/api/vuln/matches/${m.id}`, { status, reason, review_at: review || null })
      setEditing(false)
      onChanged()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "保存失败")
    }
  }

  return (
    <li className={`space-y-1 rounded-lg border p-2.5 text-xs ${m.active ? "" : "opacity-60"}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          {showRecord ? (
            <button type="button" className="font-medium text-primary" onClick={() => onOpenRecord?.(m.record_id)}>{m.record_id} <span className="text-foreground">{m.title}</span></button>
          ) : <span className="font-medium">{m.asset_name}</span>}
          <div className="text-muted-foreground">
            {m.label}：{m.installed_version}{m.fixed_version ? ` → 修复于 ${m.fixed_version}` : ""}
            {m.active ? "" : `（已不再命中${m.resolved_at ? `，${dateText(m.resolved_at)}` : ""}）`}
          </div>
          {m.note ? <div className="text-muted-foreground">{m.note}</div> : null}
        </div>
        <span className="flex flex-wrap gap-1">
          {showRecord && m.kev ? <Badge tone="bad">已被利用</Badge> : null}
          {showRecord && m.severity ? <Badge tone={SEVERITY_TONE[m.severity] ?? "neutral"}>{m.severity_text}{m.cvss != null ? ` ${m.cvss}` : ""}</Badge> : null}
          <Badge tone={m.confidence === "affected" ? "warn" : "neutral"}>{m.confidence === "affected" ? "版本在受影响范围" : "可能受影响"}</Badge>
          <Badge tone={STATUS_TONE[m.status] ?? "neutral"}>{m.status_text}</Badge>
        </span>
      </div>
      {m.reason && !editing ? (
        <p className="whitespace-pre-wrap text-muted-foreground">处置说明：{m.reason}{m.review_at ? `（复查日期 ${m.review_at}）` : ""} — {m.status_by}</p>
      ) : null}
      {editable && !editing ? <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>处置</Button> : null}
      {editing ? (
        <div className="space-y-2 rounded-md bg-secondary p-2">
          <div className="flex flex-wrap items-end gap-2">
            <Select id={`triage-status-${m.id}`} label="处置状态" value={status} onChange={setStatus} options={STATUS_OPTIONS} help="vuln.triage" />
            <TextField id={`triage-review-${m.id}`} label="复查日期" type="date" value={review} onChange={setReview}
              hint="接受风险、已缓解时必填" />
          </div>
          <TextField id={`triage-reason-${m.id}`} label="理由" value={reason} onChange={setReason} multiline
            hint="不受影响、已缓解、接受风险、误报时必填，例如“该服务未开启，已用防火墙隔离”" />
          {status === "mitigated" && suggestions?.some((s) => s.kind !== "action") ? (
            <div className="flex flex-wrap items-center gap-1" aria-label="引用来源的缓解措施">
              <span className="text-muted-foreground">引用来源原文：</span>
              {suggestions.filter((s) => s.kind !== "action").slice(0, 4).map((s, i) => (
                <Button key={i} size="sm" variant="outline" title={s.text}
                  onClick={() => setReason(`已按${s.sources.join("、")}的${s.kind_text}处理：${s.text}`.slice(0, 2000))}>
                  {s.kind_text}（{s.sources[0]}）
                </Button>
              ))}
            </div>
          ) : null}
          {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
          <div className="flex gap-2">
            <Button size="sm" onClick={() => void save()}>保存</Button>
            <Button size="sm" variant="outline" onClick={() => setEditing(false)}>取消</Button>
          </div>
        </div>
      ) : null}
    </li>
  )
}
