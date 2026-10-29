import { Dialog, Tabs } from "radix-ui"
import { useCallback, useEffect, useState } from "react"

import { PageHeader } from "@/components/AppShell"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice, Select } from "@/components/ui/controls"
import { ModalContent, ModalFooter, ModalHeader } from "@/components/ui/dialog"
import { CheckField, TextField } from "@/components/ui/form"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { dateText } from "@/lib/monitor"

import { BundleTab } from "./BundleTab"
import { AiAnalysis } from "./AiAnalysis"
import { MatchList, type Match } from "./MatchList"
import { NotifyTab } from "./NotifyTab"
import { RemediationSection, type Remediation } from "./Remediation"
import { SourcesTab } from "./SourcesTab"
import { SubscriptionsTab } from "./SubscriptionsTab"

/** 漏洞情报（4.3 P18c）：多来源汇总的漏洞、订阅、数据源、离线包与外部推送。 */

export interface SourceRef { name: string; label: string }
export interface VulnItem {
  id: string; local_assets: number; title: string; severity: string; severity_text: string; cvss: number | null; kev: boolean; kev_added: string
  epss: number | null; epss_percentile: number | null; published: string | null; first_seen_at: string | null; sources: SourceRef[]
}
interface VulnDetail extends Omit<VulnItem, "sources"> {
  summary: string; cvss_vector: string; cvss_source: string; aliases: string[]; references: { url: string; source: string }[]; cwe: string[]
  kev_ransomware: boolean; kev_due: string; modified: string | null; matches: Match[]; remediation: Remediation
  sources: (SourceRef & { native_id: string; title: string; url: string; severity: string; published: string | null; seen_at: string | null })[]
}

export const SEVERITY_TONE: Record<string, "bad" | "warn" | "info" | "neutral"> = { critical: "bad", high: "warn", medium: "info", low: "neutral", unknown: "neutral" }
export const SEVERITY_OPTIONS = [
  { value: "", label: "全部" }, { value: "critical", label: "严重" }, { value: "high", label: "高危" },
  { value: "medium", label: "中危" }, { value: "low", label: "低危" }, { value: "unknown", label: "未评级" },
]

export function VulnsPage() {
  const { user } = useAuth()
  const isAdmin = user?.role === "admin"
  return (
    <div className="pb-8">
      <PageHeader title="漏洞情报" description="汇总 NVD、CISA KEV、OSV、国内漏洞库、厂商公告等多个来源；同一漏洞多个来源发布时合并显示。" />
      <Tabs.Root defaultValue="list" className="px-6">
        <Tabs.List aria-label="漏洞情报" className="mb-4 flex gap-1 overflow-x-auto border-b">
          {[["list", "漏洞"], ["subs", "订阅"], ["sources", "数据源"], ["notify", "通知"], ["bundle", "离线包与推送"]].map(([v, l]) => (
            <Tabs.Trigger key={v} value={v}
              className="-mb-px shrink-0 border-b-2 border-transparent px-2.5 py-2 text-[0.8125rem] font-medium text-muted-foreground hover:text-foreground data-[state=active]:border-primary data-[state=active]:text-foreground">
              {l}
            </Tabs.Trigger>
          ))}
        </Tabs.List>
        <Tabs.Content value="list"><VulnList editable={isAdmin} /></Tabs.Content>
        <Tabs.Content value="subs"><SubscriptionsTab editable={isAdmin} /></Tabs.Content>
        <Tabs.Content value="sources"><SourcesTab editable={isAdmin} /></Tabs.Content>
        <Tabs.Content value="notify"><NotifyTab editable={isAdmin} /></Tabs.Content>
        <Tabs.Content value="bundle"><BundleTab editable={isAdmin} /></Tabs.Content>
      </Tabs.Root>
    </div>
  )
}

function initialOpen(): string | null {
  const m = window.location.hash.match(/[?&]id=([^&]+)/)
  return m ? decodeURIComponent(m[1]) : null
}

function VulnList({ editable }: { editable: boolean }) {
  const [q, setQ] = useState("")
  const [local, setLocal] = useState(false)
  const [severity, setSeverity] = useState("")
  const [kev, setKev] = useState(false)
  const [data, setData] = useState<{ total: number; items: VulnItem[] } | null>(null)
  const [offset, setOffset] = useState(0)
  const [open, setOpen] = useState<string | null>(initialOpen)
  const [error, setError] = useState("")

  const load = useCallback((off = 0) => {
    const params = new URLSearchParams({ q: q.trim(), severity, limit: "50", offset: String(off) })
    if (kev) params.set("kev", "true")
    if (local) params.set("local", "true")
    void api.get<{ total: number; items: VulnItem[] }>(`/api/vuln/records?${params}`).then((d) => { setData(d); setOffset(off) }, (e: Error) => setError(e.message))
  }, [q, severity, kev, local])
  useEffect(() => load(0), [load])

  return (
    <div className="space-y-3 text-[0.8125rem]">
      <form className="flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); load(0) }}>
        <TextField id="vuln-q" label="搜索" value={q} onChange={setQ} placeholder="CVE 编号或标题" plain />
        <Select id="vuln-sev" label="严重程度" value={severity} onChange={setSeverity} options={SEVERITY_OPTIONS} plain />
        <CheckField label="只看已被利用" checked={kev} onChange={setKev} help="vuln.kev" />
        <CheckField label="只看影响本地资产的" checked={local} onChange={setLocal} help="vuln.local" />
      </form>
      {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
      {data ? (
        data.items.length ? (
          <>
            <p className="text-xs text-muted-foreground">共 {data.total} 个</p>
            <ul className="divide-y rounded-lg border" aria-label="漏洞列表">
              {data.items.map((v) => (
                <li key={v.id} className="flex flex-wrap items-start justify-between gap-2 px-3 py-2">
                  <button type="button" className="min-w-0 text-left" onClick={() => setOpen(v.id)}>
                    <span className="font-medium text-primary">{v.id}</span> <span>{v.title}</span>
                    <span className="mt-0.5 block text-xs text-muted-foreground">
                      来源：{v.sources.map((s) => s.label).join("、") || "—"}{v.published ? ` · 发布于 ${dateText(v.published)}` : ""}
                    </span>
                  </button>
                  <span className="flex flex-wrap gap-1">
                    {v.local_assets ? <Badge tone="bad">影响本地 {v.local_assets} 台</Badge> : null}
                    {v.kev ? <Badge tone="bad">已被利用</Badge> : null}
                    <Badge tone={SEVERITY_TONE[v.severity] ?? "neutral"}>{v.severity_text}{v.cvss != null ? ` ${v.cvss}` : ""}</Badge>
                    {v.epss != null ? <Badge tone="neutral">EPSS {(v.epss * 100).toFixed(1)}%</Badge> : null}
                  </span>
                </li>
              ))}
            </ul>
            <div className="flex gap-2">
              <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => load(Math.max(0, offset - 50))}>上一页</Button>
              <Button size="sm" variant="outline" disabled={offset + 50 >= data.total} onClick={() => load(offset + 50)}>下一页</Button>
            </div>
          </>
        ) : <p className="text-muted-foreground">还没有漏洞数据。管理员可以在“数据源”里开启外网同步，或在“离线包与推送”里导入离线包。</p>
      ) : <p className="text-muted-foreground">正在加载…</p>}
      {open ? <VulnDialog id={open} editable={editable} onClose={() => { setOpen(null); load(offset) }} /> : null}
    </div>
  )
}

export function VulnDialog({ id, editable, onClose }: { id: string; editable: boolean; onClose: () => void }) {
  const [d, setD] = useState<VulnDetail | null>(null)
  const load = useCallback(() => void api.get<VulnDetail>(`/api/vuln/records/${encodeURIComponent(id)}`).then(setD), [id])
  useEffect(load, [load])
  return (
    <Dialog.Root open onOpenChange={(o) => (!o ? onClose() : undefined)}>
      <ModalContent width={760}>
        <ModalHeader title={d ? `${d.id} ${d.title}` : id} description={d ? `${d.severity_text}${d.cvss != null ? `，CVSS ${d.cvss}（${d.cvss_source}）` : ""}` : undefined} />
        {d ? (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-1">
              {d.kev ? <Badge tone="bad">已被利用（CISA KEV，{d.kev_added} 收录{d.kev_ransomware ? "，已用于勒索" : ""}）</Badge> : null}
              {d.epss != null ? <Badge tone="neutral">EPSS {(d.epss * 100).toFixed(2)}%（高于 {((d.epss_percentile ?? 0) * 100).toFixed(0)}% 的漏洞）</Badge> : null}
              {d.cwe.map((c) => <Badge key={c} tone="neutral">{c}</Badge>)}
            </div>
            {d.summary ? <p className="whitespace-pre-wrap">{d.summary}</p> : null}
            <section aria-label="本地影响范围">
              <h4 className="mb-1 font-medium">本地影响（{d.local_assets} 台）</h4>
              <p className="mb-1 text-xs text-muted-foreground">按采集到的版本与漏洞数据的受影响版本区间比对得出，仅供参考；没有采集到版本的设备不在其中。</p>
              <MatchList matches={d.matches} editable={editable} onChanged={load} suggestions={d.remediation.texts} />
            </section>
            <RemediationSection r={d.remediation} kev={d.kev} />
            <AiAnalysis recordId={d.id} />
            <section aria-label="发布来源">
              <h4 className="mb-1 font-medium">发布来源（{d.sources.length}）</h4>
              <ul className="space-y-1 text-xs">
                {d.sources.map((s) => (
                  <li key={s.name}>
                    <span className="font-medium">{s.label}</span>
                    {s.native_id && s.native_id !== d.id ? `（${s.native_id}）` : ""}
                    {s.url ? <> · <a className="text-primary underline" href={s.url} target="_blank" rel="noreferrer noopener">查看原文</a></> : null}
                    {s.seen_at ? <span className="text-muted-foreground"> · 收到于 {dateText(s.seen_at)}</span> : null}
                  </li>
                ))}
              </ul>
            </section>
            {d.aliases.length ? <p className="text-xs text-muted-foreground">其他编号：{d.aliases.join("、")}</p> : null}
            {d.references.length ? (
              <details className="text-xs">
                <summary className="cursor-pointer">参考链接（{d.references.length}）</summary>
                <ul className="mt-1 space-y-0.5 break-all">
                  {d.references.map((r) => <li key={r.url}><a className="text-primary underline" href={r.url} target="_blank" rel="noreferrer noopener">{r.url}</a></li>)}
                </ul>
              </details>
            ) : null}
          </div>
        ) : <p className="text-muted-foreground">正在加载…</p>}
        <ModalFooter><Dialog.Close asChild><Button variant="outline">关闭</Button></Dialog.Close></ModalFooter>
      </ModalContent>
    </Dialog.Root>
  )
}
