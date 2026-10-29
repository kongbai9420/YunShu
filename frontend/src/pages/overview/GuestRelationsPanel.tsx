import { useEffect, useState } from "react"
import { Dialog } from "radix-ui"
import { ChevronDown, ChevronRight } from "lucide-react"

import { Button } from "@/components/ui/button"
import { InlineNotice, Select, panelClass } from "@/components/ui/controls"
import { ModalContent, ModalFooter, ModalHeader } from "@/components/ui/dialog"
import { TextField } from "@/components/ui/form"
import { api, query, type Asset, type Page } from "@/lib/api"
import { canEdit, useAuth } from "@/lib/auth"
import { dateText, usePoll } from "@/lib/monitor"
import { cn } from "@/lib/utils"

interface Ref { id: string; name: string }
interface Source extends Ref {
  platform: string; target: string; native_uuid: string; quality: string; complete: boolean; observed_at: string
}
interface Observation {
  id: string; kind: string; external_id: string; name: string; host_external_id: string
  present: boolean; state: string; revision: number; observed_at: string; power: string; quality: string
  asset: Ref | null; host_asset: Ref | null
}
interface ObservationPage extends Page<Observation> { source: Source }
interface Plan {
  preview_token: string; action: string; name: string; asset: Ref | null; host_asset: Ref | null
  remove_relation: string | null; remove_host: string | null
  preserved_relations: { id: string; origin: string; host_name: string }[]
}
type Action = "link" | "create" | "ignore" | "unlink"
const KIND: Record<string, string> = { host: "宿主", vm: "虚拟机", container: "容器" }
const STATE: Record<string, string> = { candidate: "待确认", confirmed: "已关联", review: "需要复核", ignored: "已忽略", unseen: "本次未见" }
const QUALITY: Record<string, string> = { valid: "有效", failed: "采集失败", stale: "已过期", identity_conflict: "来源身份变化", not_seen: "本次未观察到", unavailable: "来源不可用", untrusted: "等待信任", unconfigured: "来源未配置", skipped: "本次未采集" }

function Pager({ offset, limit, count, total, onChange, label }: { offset: number; limit: number; count: number; total: number; onChange: (v: number) => void; label: string }) {
  return <div role="group" aria-label={label} className="flex flex-wrap items-center gap-2 text-xs">
    <span className="text-muted-foreground">当前 {count ? offset + 1 : 0}–{offset + count} / {total} 项</span>
    <Button size="xs" variant="outline" disabled={!offset} onClick={() => onChange(Math.max(0, offset - limit))}>上一页</Button>
    <Button size="xs" variant="outline" disabled={offset + count >= total} onClick={() => onChange(offset + limit)}>下一页</Button>
  </div>
}

function DecisionDialog({ observation: o, onClose, onDone }: { observation: Observation; onClose: () => void; onDone: () => void }) {
  const [action, setAction] = useState<Action>("link")
  const [name, setName] = useState(o.name)
  const [assetId, setAssetId] = useState(o.asset?.id ?? "")
  const [search, setSearch] = useState("")
  const [lookup, setLookup] = useState({ q: "", version: 0 })
  const [assets, setAssets] = useState<Page<Asset> | null>(null)
  const [plan, setPlan] = useState<Plan | null>(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (action !== "link") return
    let stopped = false
    setAssets(null)
    void api.get<Page<Asset>>(`/api/assets${query({ q: lookup.q, role: "server", form: o.kind === "host" ? "physical,vm" : o.kind, limit: 50 })}`)
      .then((result) => { if (!stopped) setAssets(result) }).catch((e: Error) => { if (!stopped) setError(e.message) })
    return () => { stopped = true }
  }, [action, lookup, o.kind])
  const invalidate = () => { setPlan(null); setError("") }
  const body = { action, revision: o.revision, name, asset_id: assetId || null }
  const preview = async () => {
    setBusy(true); setError(""); setPlan(null)
    try { setPlan(await api.post<Plan>(`/api/virtualization/observations/${o.id}/preview`, body)) }
    catch (e) { setError((e as Error).message) }
    finally { setBusy(false) }
  }
  const confirm = async () => {
    if (!plan) return
    setBusy(true); setError("")
    try { await api.post(`/api/virtualization/observations/${o.id}/confirm`, { ...body, preview_token: plan.preview_token }); onDone() }
    catch (e) { setError((e as Error).message); setPlan(null) }
    finally { setBusy(false) }
  }
  const options = [{ value: "link", label: o.asset ? "重新确认关联与宿主" : "关联已有资产" },
    ...(o.asset ? [{ value: "unlink", label: "解除平台绑定（保留资产）" }] : [{ value: "create", label: "新建资产并关联" }, { value: "ignore", label: "忽略候选" }])]
  const assetOptions = assets?.items.map((a) => ({ value: a.id, label: `${a.name} · ${a.asset_no || a.id.slice(0, 8)}` })) ?? []
  if (o.asset && !assetOptions.some((a) => a.value === o.asset!.id)) assetOptions.unshift({ value: o.asset.id, label: `${o.asset.name} · ${o.asset.id.slice(0, 8)}（已绑定）` })
  return <Dialog.Root open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
    <ModalContent width={640} aria-describedby="guest-decision-description">
      <ModalHeader title="预览虚拟资源变更" description="先核对原生身份、目标资产与关系变化，再显式确认。" closeDisabled={busy} />
      <div id="guest-decision-description" className="mb-4 space-y-1 break-words [overflow-wrap:anywhere]">
        <p className="font-semibold">{o.name} · {KIND[o.kind]}</p><p className="text-xs text-muted-foreground">原生标识：{o.external_id}</p>
        <p className="text-xs text-muted-foreground">原生宿主：{o.host_external_id || "未提供"} · {o.host_asset?.name || "宿主尚未关联"}</p>
      </div>
      <div className="space-y-3">
        <Select id="guest-action" help="guest.action" label="操作" value={action} disabled={busy} options={options}
          onChange={(value) => { invalidate(); setAction(value as Action); setAssetId(o.asset?.id ?? "") }} />
        {action === "create" ? <><TextField id="guest-new-name" plain label="新资产名称" value={name} maxLength={200} disabled={busy} onChange={(value) => { invalidate(); setName(value) }} />
          <p className="text-xs text-muted-foreground">将按{KIND[o.kind]}建档为“规划中”的服务器，不复制宿主管理端点、凭据或采集模板。</p></> : null}
        {action === "link" ? <>
          <div className="flex flex-wrap items-end gap-2"><TextField id="guest-asset-search" plain label="查询已有服务器资产" value={search} disabled={busy} maxLength={100} onChange={setSearch} className="min-w-0 flex-1 basis-40" />
            <Button size="sm" variant="outline" disabled={busy} onClick={() => { invalidate(); setAssetId(o.asset?.id ?? ""); setLookup((old) => ({ q: search.trim(), version: old.version + 1 })) }}>查询资产</Button></div>
          <Select id="guest-target" hint="不会按名称或 IP 自动匹配，请手动选择" label="目标资产" value={assetId} disabled={busy || !assets} options={[{ value: "", label: "请选择，不按名称或 IP 自动匹配" }, ...assetOptions]}
            onChange={(value) => { invalidate(); setAssetId(value) }} />
          <p className="text-xs text-muted-foreground">{assets ? `显示 ${assets.items.length} / ${assets.total} 项；超过 50 项请缩小查询。` : "正在读取可选资产…"}关联不改已有资产名称或端点。</p>
        </> : null}
        {action === "unlink" ? <InlineNotice tone="warn">仅解除此平台身份绑定并移除它创建的承载关系；资产、指标和人工关系保留。其他已确认子资源关系不会自动改变。</InlineNotice> : null}
        {error ? <InlineNotice tone="bad" role="alert">{error}。观察变化时请关闭窗口、刷新候选后重试。</InlineNotice> : null}
        {plan ? <section aria-label="虚拟资源变更预览" className="space-y-3 rounded-lg border p-3 [overflow-wrap:anywhere]">
          <h3 className="font-semibold">请核对本次变更</h3>
          <p>{action === "create" ? `新建「${plan.name}」（规划中）` : action === "ignore" ? "忽略此候选，不创建资产或关系" : action === "unlink" ? `解除「${plan.asset?.name}」的平台绑定，资产保留` : `关联「${plan.asset?.name}」`}</p>
          {plan.asset ? <p className="text-xs text-muted-foreground">资产 UUID：{plan.asset.id}</p> : null}
          {plan.host_asset ? <p>确认承载宿主：「{plan.host_asset.name}」<span className="mt-1 block text-xs text-muted-foreground">{plan.host_asset.id}</span></p> : null}
          {plan.remove_relation ? <InlineNotice tone="warn">将移除本流程创建的旧宿主关系：{plan.remove_host || plan.remove_relation}。这不是向虚拟化平台执行迁移。</InlineNotice> : null}
          {plan.preserved_relations.length ? <div><p className="font-medium">以下现有承载关系将保留（可能与新宿主并存）：</p>
            <ul className="mt-1 list-inside list-disc space-y-1">{plan.preserved_relations.map((r) => <li key={r.id}>{r.host_name} · {r.origin === "manual" ? "人工关系" : "已有关系"}</li>)}</ul></div> : null}
          <p className="text-xs text-muted-foreground">确认只修改平台内建档与关系，不执行虚拟机启停、迁移或任何设备控制。</p>
        </section> : null}
      </div>
      <ModalFooter><Button variant="ghost" disabled={busy} onClick={onClose}>取消</Button>
        <Button variant="outline" disabled={busy} onClick={() => void preview()}>{busy ? "正在处理…" : "预览变更"}</Button>
        <Button disabled={busy || !plan} onClick={() => void confirm()}>确认本次变更</Button></ModalFooter>
    </ModalContent>
  </Dialog.Root>
}

function Observations({ sourceId, onChanged }: { sourceId: string; onChanged: () => void }) {
  const { user } = useAuth()
  const [kind, setKind] = useState("")
  const [q, setQ] = useState("")
  const [search, setSearch] = useState("")
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<Observation | null>(null)
  const [message, setMessage] = useState("")
  const { data, error, refresh } = usePoll<ObservationPage>(`/api/virtualization/sources/${sourceId}/observations${query({ kind, q, limit: 25, offset })}`, 30000)
  return <div className="space-y-3">
    {data ? <div className="space-y-1 break-words text-xs text-muted-foreground [overflow-wrap:anywhere]">
      <p>{data.source.platform === "pve" ? "PVE" : "VMware"} · {data.source.target}</p>
      {data.source.native_uuid ? <p>平台服务标识：{data.source.native_uuid}</p> : null}
      <p>来源状态：{QUALITY[data.source.quality] ?? data.source.quality} · 最近观察：{dateText(data.source.observed_at)}</p>
      {data.source.quality === "identity_conflict" ? <InlineNotice tone="bad">管理目标或平台实例身份已变化，原绑定保留。请新建管理端点并重新采集，建立独立范围。</InlineNotice> : null}
      {!data.source.complete ? <InlineNotice tone="warn">本次快照不完整，未返回的资源不会被判定消失；旧观察也不能据此确认关联。</InlineNotice> : null}
    </div> : null}
    <form className="flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); setQ(search.trim()); setOffset(0); refresh() }}>
      <Select id="guest-kind" hint="虚拟机或容器" label="原生资源类型" value={kind} onChange={(value) => { setKind(value); setOffset(0) }} options={[{ value: "", label: "全部" }, ...Object.entries(KIND).map(([value, label]) => ({ value, label }))]} />
      <TextField id="guest-query" plain label="搜索原生名称或标识" value={search} onChange={setSearch} maxLength={100} className="min-w-0 flex-1 basis-40" />
      <Button type="submit" size="sm" variant="outline">筛选候选</Button>
      <Button size="sm" type="button" variant="outline" onClick={refresh}>刷新候选</Button>
    </form>
    {message ? <InlineNotice>{message}</InlineNotice> : null}
    {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
    {!data && !error ? <p role="status" className="text-xs">正在读取原生观察…</p> : null}
    {data?.total === 0 ? <p className="text-[0.8125rem] text-muted-foreground">没有匹配的原生观察。</p> : null}
    <div className="grid items-start gap-3 xl:grid-cols-2">
      {data?.items.map((o) => <article key={o.id} aria-label={`原生资源 ${KIND[o.kind]} ${o.name} ${o.external_id}`} className="min-w-0 space-y-2 rounded-lg border bg-background p-3 text-[0.8125rem] [overflow-wrap:anywhere]">
        <div className="flex flex-wrap items-start justify-between gap-2"><h3 className="min-w-0 flex-1 basis-40 font-semibold">{o.name}</h3><span className="rounded-full bg-secondary px-2 py-0.5 text-xs">{STATE[o.state] ?? o.state}</span></div>
        <p className="text-xs text-muted-foreground">{KIND[o.kind]} · 原生标识：{o.external_id}</p>
        <p className="text-xs">关联资产：{o.asset ? `${o.asset.name} · ${o.asset.id}` : "尚未关联"}</p>
        {o.kind !== "host" ? <><p className="text-xs">原生宿主：{o.host_external_id || "未提供"} · {o.host_asset?.name || "尚未关联宿主资产"}</p>
          <p className="text-xs">平台电源观察：{o.power === "on" ? "开机" : o.power === "off" ? "关机" : "未知"} · {QUALITY[o.quality] ?? o.quality}</p></> : null}
        <p className="text-xs text-muted-foreground">最近见到：{dateText(o.observed_at)}{!o.present ? " · 本次未见（可能已消失或权限变化），原资产与关系保留" : ""}</p>
        {canEdit(user) ? <Button size="xs" variant="outline" onClick={() => setSelected(o)}>{o.asset ? "复核关联" : "处理候选"}</Button> : null}
      </article>)}
    </div>
    {data ? <Pager offset={offset} limit={25} count={data.items.length} total={data.total} onChange={setOffset} label="原生资源分页" /> : null}
    {selected ? <DecisionDialog key={selected.id} observation={selected} onClose={() => setSelected(null)} onDone={() => { setSelected(null); setMessage("本次变更已保存；原资产和保留的人工关系不变。"); refresh(); onChanged() }} /> : null}
  </div>
}

function Sources({ onChanged }: { onChanged: () => void }) {
  const [offset, setOffset] = useState(0)
  const [sourceId, setSourceId] = useState("")
  const { data, error, refresh } = usePoll<Page<Source>>(`/api/virtualization/sources${query({ offset, limit: 50 })}`, 30000)
  useEffect(() => { if (data && !data.items.some((s) => s.id === sourceId)) setSourceId(data.items[0]?.id ?? "") }, [data, sourceId])
  return <div className="mt-4 space-y-4 border-t pt-4">
    <p className="text-xs text-muted-foreground">先为 PVE / VMware 管理端点采集一次，再关联宿主，最后确认 VM / 容器。每个端点单独管理原生身份，名称和 IP 不自动匹配。宿主迁移需重新预览确认，人工关系保留。</p>
    {error ? <InlineNotice tone="bad" role="alert">{error} <button type="button" onClick={refresh} className="underline">重试</button></InlineNotice> : null}
    {data?.total === 0 ? <p className="text-[0.8125rem]">尚无虚拟化观察。请在资产管理配置 API 端点并执行只读采集；旧快照将在下一次成功采集时更新。</p> : null}
    {data?.items.length ? <><Select id="guest-source" help="guest.instance" label="虚拟化实例范围" value={sourceId} onChange={setSourceId} options={data.items.map((s) => ({ value: s.id, label: `${s.name} · ${s.target}` }))} />
      <Pager offset={offset} limit={50} count={data.items.length} total={data.total} onChange={(v) => { setOffset(v); setSourceId("") }} label="实例范围分页" /></> : null}
    {sourceId ? <Observations key={sourceId} sourceId={sourceId} onChanged={() => { refresh(); onChanged() }} /> : null}
  </div>
}

export function GuestRelationsPanel({ onChanged }: { onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  return <section aria-label="虚拟资源发现与关联" className={cn(panelClass, "p-4")}>
    <button type="button" className="flex min-h-8 max-w-full items-center gap-2 text-left text-[0.8125rem] font-semibold" aria-expanded={open} onClick={() => setOpen(!open)}>
      {open ? <ChevronDown aria-hidden className="size-4 shrink-0" /> : <ChevronRight aria-hidden className="size-4 shrink-0" />}虚拟资源发现与关联
    </button>
    {open ? <Sources onChanged={onChanged} /> : <p className="mt-1 text-xs text-muted-foreground">预览平台原生宿主、VM 和容器，人工确认后再建档或关联。</p>}
  </section>
}
