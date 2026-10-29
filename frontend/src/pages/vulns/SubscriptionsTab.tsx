import { useCallback, useEffect, useState } from "react"

import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice, Select } from "@/components/ui/controls"
import { CheckField, TextField } from "@/components/ui/form"
import { ApiError, api } from "@/lib/api"

import { SEVERITY_OPTIONS } from "./VulnsPage"

interface Sub { id: string; kind: string; value: string; label: string; min_severity: string; kev_only: boolean; auto: boolean; enabled: boolean; created_by: string }

const KIND_TEXT: Record<string, string> = { product: "产品", keyword: "关键词", ecosystem: "发行版软件包" }

/** 订阅（4.3 P18c）：按软件清单自动订阅“我有的设备”，也可以手动加产品或关键词。 */
export function SubscriptionsTab({ editable }: { editable: boolean }) {
  const [rows, setRows] = useState<Sub[] | null>(null)
  const [kind, setKind] = useState("product")
  const [value, setValue] = useState("")
  const [minSev, setMinSev] = useState("unknown")
  const [kevOnly, setKevOnly] = useState(false)
  const [error, setError] = useState("")
  const load = useCallback(() => void api.get<Sub[]>("/api/vuln/subscriptions").then(setRows, (e: Error) => setError(e.message)), [])
  useEffect(load, [load])

  async function add(e: React.FormEvent) {
    e.preventDefault()
    setError("")
    try {
      await api.post("/api/vuln/subscriptions", { kind, value, min_severity: minSev, kev_only: kevOnly })
      setValue("")
      load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "添加失败")
    }
  }

  async function update(s: Sub, body: Partial<Sub>) {
    try {
      await api.patch(`/api/vuln/subscriptions/${s.id}`, body)
      load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "保存失败")
    }
  }

  async function remove(s: Sub) {
    try {
      await api.del(`/api/vuln/subscriptions/${s.id}`)
      load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "删除失败")
    }
  }

  return (
    <div className="max-w-4xl space-y-4 text-[0.8125rem]">
      <p className="text-muted-foreground">
        订阅决定同步哪些产品的漏洞、通知哪些新漏洞。“自动”订阅由资产的软件清单生成（Dell iDRAC、ESXi、群晖 DSM、RouterOS、Debian / Ubuntu 软件包等），新增设备后自动补上。
      </p>
      {editable ? (
        <form onSubmit={add} className="flex flex-wrap items-end gap-3 rounded-lg border p-3">
          <Select id="sub-kind" label="类型" value={kind} onChange={setKind} help="vuln.sub.kind"
            options={[{ value: "product", label: "产品（厂商:产品）" }, { value: "keyword", label: "关键词" }]} />
          <TextField id="sub-value" className="min-w-64 flex-1" label={kind === "product" ? "产品" : "关键词"} value={value} onChange={setValue} required
            placeholder={kind === "product" ? "如 openbsd:openssh、fortinet:fortios" : "如 OpenSSH、Struts2"} help="vuln.sub.value" />
          <Select id="sub-sev" label="最低严重程度" value={minSev} onChange={setMinSev} help="vuln.sub.minSeverity"
            options={SEVERITY_OPTIONS.filter((o) => o.value).map((o) => (o.value === "unknown" ? { value: "unknown", label: "不限" } : o))} />
          <CheckField label="只通知已被利用的" checked={kevOnly} onChange={setKevOnly} help="vuln.kev" />
          <Button type="submit" disabled={value.trim().length < 2}>添加订阅</Button>
        </form>
      ) : null}
      {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
      {rows ? (
        rows.length ? (
          <ul className="divide-y rounded-lg border" aria-label="订阅列表">
            {rows.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                <div className="min-w-0">
                  <span className="font-medium">{s.label}</span>
                  {s.label !== s.value ? <span className="text-muted-foreground">（{s.value}）</span> : null}
                  <span className="ml-2 inline-flex gap-1 align-middle">
                    <Badge tone="neutral">{KIND_TEXT[s.kind] ?? s.kind}</Badge>
                    {s.auto ? <Badge tone="info">自动</Badge> : null}
                    {s.kev_only ? <Badge tone="warn">只看已被利用</Badge> : null}
                    {s.min_severity !== "unknown" ? <Badge tone="neutral">≥ {SEVERITY_OPTIONS.find((o) => o.value === s.min_severity)?.label}</Badge> : null}
                  </span>
                </div>
                {editable ? (
                  <div className="flex items-center gap-2">
                    <CheckField label="启用" checked={s.enabled} onChange={(v) => void update(s, { enabled: v })} hint={s.auto ? "停用后不再自动重建" : undefined} />
                    {!s.auto ? <Button size="sm" variant="ghost" onClick={() => void remove(s)}>删除</Button> : null}
                  </div>
                ) : <span className="text-xs text-muted-foreground">{s.enabled ? "启用" : "停用"}</span>}
              </li>
            ))}
          </ul>
        ) : <p className="text-muted-foreground">还没有订阅。采集到设备版本后会自动生成。</p>
      ) : <p className="text-muted-foreground">正在加载…</p>}
    </div>
  )
}
