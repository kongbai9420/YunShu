import { useEffect, useMemo, useRef, useState } from "react"
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight, GripVertical, RefreshCw } from "lucide-react"

import { PageHeader } from "@/components/AppShell"
import { Badge, QualityBadge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice, Select, Segmented, panelClass } from "@/components/ui/controls"
import { TextField } from "@/components/ui/form"
import { api, query, type Site } from "@/lib/api"
import { canEdit, seesAll, useAuth } from "@/lib/auth"
import { dateText, numberText, usePoll } from "@/lib/monitor"
import { COLLECTION_LABEL, groupLabel, moveNear, type EffectiveOrder, type Resource, type ResourceOrder, type ResourcePage } from "@/lib/resources"
import { FORM_LABEL, LIFECYCLE_LABEL, PROTOCOL_LABEL, ROLE_LABEL, labelOf, optionsFrom } from "@/lib/vocab"
import { cn } from "@/lib/utils"
import { AssetDetailDrawer } from "@/pages/assets/AssetDetailDrawer"
import { LayoutToolbar, useRevisionedLayout } from "@/pages/dashboard/useRevisionedLayout"
import { GuestRelationsPanel } from "./GuestRelationsPanel"
import { PowerEntry } from "./PowerEntry"
import { ScopeNotice } from "@/components/ScopeNote"

const PAGE_SIZE = 25
const ALL = { value: "", label: "全部" }
const METRICS = [["cpu.temp", "CPU 温度"], ["cpu.load", "CPU 负载"], ["mem.used_pct", "内存"], ["power.watts", "功耗"]] as const
const EMPTY_ORDER: ResourceOrder = { groups: [], order: [], children: {} }
const EDIT_HINT = "编辑排序：拖动卡片，或用“上移 / 下移”按钮调整。只改变显示顺序，不改变宿主关系、类别或场所；跨页调整请先用筛选缩小范围。未保存的修改会暂存在本浏览器。"

/** 按草稿排序：位置相同（未排过）的按名称 / UUID 稳定排列 */
function byPosition<T extends { id: string; name: string }>(items: T[], order: string[]): T[] {
  const pos = new Map(order.map((id, i) => [id, i]))
  return [...items].sort((a, b) => (pos.get(a.id) ?? Infinity) - (pos.get(b.id) ?? Infinity) || a.name.localeCompare(b.name, "zh-CN") || a.id.localeCompare(b.id))
}

/** 编辑时每张卡片的移动控件；拖动与按钮效果一致，按钮可用键盘操作 */
function MoveControls({ name, index, count, onMove }: { name: string; index: number; count: number; onMove: (delta: -1 | 1) => void }) {
  return <div className="mb-2 flex flex-wrap items-center gap-1 border-b pb-2" role="group" aria-label={`调整 ${name} 的位置`}>
    <GripVertical aria-hidden className="size-4 cursor-grab text-muted-foreground" />
    <span className="text-xs text-muted-foreground">第 {index + 1} / {count} 位</span>
    <Button size="xs" variant="outline" disabled={index === 0} aria-label={`上移 ${name}`} onClick={() => onMove(-1)}><ArrowUp aria-hidden />上移</Button>
    <Button size="xs" variant="outline" disabled={index === count - 1} aria-label={`下移 ${name}`} onClick={() => onMove(1)}><ArrowDown aria-hidden />下移</Button>
  </div>
}

function Facts({ asset: r, onDetails, compact = false }: { asset: Resource; onDetails: (id: string) => void; compact?: boolean }) {
  const power = r.metrics["power.on"]
  const knownPower = power && ["valid", "estimated"].includes(power.quality) && [0, 1].includes(power.value ?? -1)
  return <>
    <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
      <div className="min-w-0 flex-1 basis-44">
        <button type="button" className="max-w-full break-words text-left font-semibold text-link hover:underline [overflow-wrap:anywhere]"
          onClick={() => onDetails(r.id)}>{r.name}</button>
        <p className="mt-1 break-words text-xs text-muted-foreground">{labelOf(FORM_LABEL, r.form)} · {labelOf(ROLE_LABEL, r.role)} · {r.platform || "平台未填写"}</p>
      </div>
      <Badge tone={r.collection_state === "ok" ? "ok" : ["failed", "untrusted"].includes(r.collection_state) ? "bad" : "neutral"}>
        {COLLECTION_LABEL[r.collection_state] ?? r.collection_state}
      </Badge>
    </div>
    <p className="mt-2 break-words text-xs text-muted-foreground">{r.site_name || "未设置场所"} · {labelOf(LIFECYCLE_LABEL, r.lifecycle)}{r.asset_no ? ` · ${r.asset_no}` : ""}</p>
    {r.endpoints.length ? <p className="mt-1 break-words text-xs text-muted-foreground [overflow-wrap:anywhere]">
      {r.endpoints.map((e) => `${labelOf(PROTOCOL_LABEL, e.protocol)} ${e.address}${e.port ? `:${e.port}` : ""}`).join(" · ")}
    </p> : <p className="mt-1 text-xs text-muted-foreground">尚未配置管理端点</p>}
    {r.tags.length ? <p className="mt-1 break-words text-xs text-muted-foreground">标签：{r.tags.join("、")}</p> : null}
    <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
      <span>电源：{knownPower ? `${power.quality === "estimated" ? "估算" : ""}${power.value === 1 ? "开机" : "关机"}` : "未知"}</span>
      {power ? <><QualityBadge quality={power.quality} /><span className="text-muted-foreground">{dateText(power.observed_at)}</span></> : <span className="text-muted-foreground">没有电源读数</span>}
      <span className="break-words">活跃告警：{r.active_alerts} 条</span>
    </div>
    <dl className={cn("mt-3 grid grid-cols-1 gap-2 min-[480px]:grid-cols-2", compact && "lg:grid-cols-4")}>
      {METRICS.map(([key, title]) => {
        const m = r.metrics[key]
        const numeric = m && ["valid", "estimated"].includes(m.quality) && m.value !== null
        return <div key={key} className="min-w-0 rounded-md bg-secondary px-2 py-1.5 text-xs">
          <dt className="text-muted-foreground">{title}</dt>
          <dd className="mt-1 flex flex-wrap items-center gap-1.5"><span className="font-medium tabular-nums">{numeric ? `${m.estimated ? "≈ " : ""}${numberText(m.value)} ${m.unit}` : "—"}</span>
            {m ? <QualityBadge quality={m.quality} /> : <span className="text-muted-foreground">暂无读数</span>}</dd>
          {m ? <dd className="mt-1 break-words text-muted-foreground">{dateText(m.observed_at)}</dd> : null}
        </div>
      })}
    </dl>
    <p className="mt-2 text-xs text-muted-foreground">最近采集尝试：{dateText(r.collection_at)}</p>
    {r.parent_count ? <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs"><span>已确认宿主：</span>
      {r.parents.map((p) => <button type="button" className="max-w-full break-words text-link underline [overflow-wrap:anywhere]" key={p.id} onClick={() => onDetails(p.id)}>{p.name}</button>)}
      {r.parent_count > r.parents.length ? <button type="button" className="text-link underline" onClick={() => onDetails(r.id)}>另有 {r.parent_count - r.parents.length} 个，查看全部关系</button> : null}
    </div> : <p className="mt-2 text-xs text-muted-foreground">{r.form === "physical" ? "独立资源 · 未记录宿主" : "尚未关联已确认宿主"}</p>}
  </>
}

function Children({ id, onDetails, order, onOrder }: {
  id: string; onDetails: (id: string) => void
  /** 编辑排序时传入该宿主下的子资源草稿顺序 */
  order?: string[]; onOrder?: (next: string[]) => void
}) {
  const [offset, setOffset] = useState(0)
  const { data, error, refresh } = usePoll<ResourcePage>(`/api/overview/resources/${id}/children${query({ offset, limit: PAGE_SIZE })}`, 30000)
  const items = data ? (order ? byPosition(data.items, order) : data.items) : []
  const move = (child: string, delta: -1 | 1) => {
    if (!order || !onOrder) return
    const i = items.findIndex((r) => r.id === child)
    const target = items[i + delta]
    // 先把当前显示顺序补进草稿，再只移动这一项
    if (target) onOrder(moveNear([...order, ...items.map((r) => r.id).filter((x) => !order.includes(x))], child, target.id, delta > 0))
  }
  return <div className="mt-2 border-l-2 pl-3" aria-label="已确认下挂资源">
    <p className="mb-3 text-xs text-muted-foreground">以下为已确认的承载关系引用，不受上方筛选限制，不另计资产总数；仅展开一层。各资源状态独立显示。</p>
    {error ? <InlineNotice tone="bad" role="alert">{error} <button type="button" className="underline" onClick={refresh}>重试</button></InlineNotice> : null}
    {!data && !error ? <p role="status" className="text-xs">正在读取下挂资源…</p> : null}
    {data ? <>
      <p className="mb-2 text-xs text-muted-foreground">已显示 {data.items.length ? offset + 1 : 0}–{offset + data.items.length} / {data.total} 项关系引用</p>
      <div className="space-y-3">{items.map((r, i) => <article key={r.id} aria-label={`下挂资源 ${r.name}`} className="min-w-0 rounded-lg border bg-background p-3">
        {order ? <MoveControls name={r.name} index={i} count={items.length} onMove={(d) => move(r.id, d)} /> : null}
        <Facts asset={r} onDetails={onDetails} />
        {r.children_count ? <p className="mt-2 text-xs text-muted-foreground">另有关联：还承载 {r.children_count} 项，可在资产详情查看。</p> : null}
      </article>)}</div>
      <Pagination offset={offset} count={data.items.length} total={data.total} setOffset={setOffset} label="下挂资源分页" />
    </> : null}
  </div>
}

function Pagination({ offset, count, total, setOffset, label }: { offset: number; count: number; total: number; setOffset: (v: number) => void; label: string }) {
  return <div role="group" aria-label={label} className="mt-3 flex flex-wrap items-center gap-2">
    <Button size="xs" variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</Button>
    <Button size="xs" variant="outline" disabled={offset + count >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</Button>
    <span className="text-xs text-muted-foreground">第 {Math.floor(offset / PAGE_SIZE) + 1} 页</span>
  </div>
}

export function ResourcesPage({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void } = {}) {
  const { user } = useAuth()
  const editable = canEdit(user)
  const layout = useRevisionedLayout<ResourceOrder>("/api/overview/resources/layout", EMPTY_ORDER,
    { draftKey: "yunshu-resources-order-draft", editHint: EDIT_HINT })
  const [draft, setDraft] = useState<ResourceOrder | null>(null)
  const [defaultGroups, setDefaultGroups] = useState<string[]>([])
  // 拖动中不改状态：拖动开始时重绘拖动源会让浏览器中断拖动
  const dragging = useRef<string | null>(null)
  const [orderError, setOrderError] = useState("")
  const editing = layout.editing && draft !== null
  const [sites, setSites] = useState<Site[]>([])
  const [siteError, setSiteError] = useState("")
  const [filters, setFilters] = useState({ q: "", role: "", form: "", site_id: "", lifecycle: "", tag: "", platform: "" })
  const [search, setSearch] = useState("")
  const [tag, setTag] = useState("")
  const [platform, setPlatform] = useState("")
  const [offset, setOffset] = useState(0)
  const [mode, setMode] = useState<"cards" | "rows">("cards")
  const [expanded, setExpanded] = useState<string[]>([])
  const [detailsId, setDetailsId] = useState<string | null>(null)
  const [version, setVersion] = useState(0)
  const { data, error, refresh } = usePoll<ResourcePage>(`/api/overview/resources${query({ ...filters, offset, limit: PAGE_SIZE })}`, 30000)
  const loadSites = () => { void api.get<Site[]>("/api/sites").then((s) => { setSites(s); setSiteError("") }).catch((e: Error) => setSiteError(e.message)) }
  useEffect(loadSites, [])
  const update = (key: keyof typeof filters, value: string) => { setFilters((f) => ({ ...f, [key]: value })); setOffset(0); setExpanded([]) }
  const reload = () => { refresh(); setVersion((v) => v + 1) }
  useEffect(() => { onDirtyChange?.(layout.editing && layout.dirty) }, [onDirtyChange, layout.editing, layout.dirty])
  useEffect(() => { if (!layout.editing) setDraft(null) }, [layout.editing])

  // 编辑排序需要完整的当前顺序（只有 ID），保证筛选外和其他页的资产保持原位
  const loadEffective = async () => {
    const eff = await api.get<EffectiveOrder>("/api/overview/resources/order")
    setDefaultGroups(eff.default_groups)
    return eff
  }
  const beginEdit = async () => {
    setOrderError("")
    try {
      const [, eff] = await Promise.all([layout.startEdit(), loadEffective()])
      setDraft({ groups: eff.groups, order: eff.items.map((i) => i.id), children: eff.children })
    } catch (e) {
      setOrderError(`读取当前排序失败：${(e as Error).message}`)
    }
  }
  const change = (next: ResourceOrder) => { setDraft(next); layout.noteDraft(next) }
  const saved = (ok: boolean) => { if (ok) { setDraft(null); reload() } }

  const groupRank = useMemo(() => {
    const list = draft ? [...draft.groups, ...defaultGroups.filter((g) => !draft.groups.includes(g))] : []
    return new Map(list.map((g, i) => [g, i]))
  }, [draft, defaultGroups])
  const groups = useMemo(() => {
    const map = new Map<string, Resource[]>()
    for (const r of data?.items ?? []) map.set(r.group, [...(map.get(r.group) ?? []), r])
    const list = [...map]
    if (!editing || !draft) return list
    // 编辑中按草稿显示；30 秒刷新只更新数据，不重排草稿
    return list.map(([g, rs]) => [g, byPosition(rs, draft.order)] as [string, Resource[]])
      .sort(([a], [b]) => (groupRank.get(a) ?? Infinity) - (groupRank.get(b) ?? Infinity))
  }, [data, editing, draft, groupRank])

  const moveCard = (group: Resource[], id: string, delta: -1 | 1) => {
    if (!draft) return
    const i = group.findIndex((r) => r.id === id)
    const target = group[i + delta]
    if (target) change({ ...draft, order: moveNear(draft.order, id, target.id, delta > 0) })
  }
  const dropCard = (group: Resource[], source: string | null, target: string) => {
    if (!draft || !source || source === target) return
    const from = group.findIndex((r) => r.id === source)
    const to = group.findIndex((r) => r.id === target)
    if (from < 0 || to < 0) return // 只在同一分组内调整；拖动不改变类别或宿主
    change({ ...draft, order: moveNear(draft.order, source, target, from < to) })
  }
  const moveGroup = (name: string, delta: -1 | 1) => {
    if (!draft) return
    const visible = groups.map(([g]) => g)
    const target = visible[visible.indexOf(name) + delta]
    if (target) change({ ...draft, groups: moveNear([...groupRank.keys()], name, target, delta > 0) })
  }

  return <>
    <PageHeader title="资源全景" description="查看真实建档资产与已确认承载关系。采集失败不等于设备关机，宿主状态不代表子资源状态。"
      actions={<Button size="sm" variant="outline" onClick={reload}><RefreshCw aria-hidden className="size-3.5" />刷新资源</Button>} />
    <LayoutToolbar state={layout} canEdit={editable} labels={{ edit: "编辑排序", save: "保存排序", reset: "恢复默认排序", thing: "排序" }}
      onStartEdit={() => void beginEdit()} onSave={() => { if (draft) void layout.save(draft).then(saved) }}
      onOverwrite={() => { if (draft) void layout.overwrite(draft).then(saved) }}
      onResetDefault={() => change(EMPTY_ORDER)}
      onRecover={() => { const d = layout.recover(); if (d) void loadEffective().then(() => setDraft(d)).catch((e: Error) => setOrderError(e.message)) }} />
    <div className="space-y-4 px-4 pb-8 sm:px-6">
      {orderError ? <InlineNotice tone="bad" role="alert">{orderError}</InlineNotice> : null}
      {seesAll(user) ? <GuestRelationsPanel onChanged={reload} /> : <ScopeNotice what="虚拟资源发现与关联会产生新资产，" />}
      <form className={cn(panelClass, "flex flex-wrap items-end gap-3 p-4")} aria-label="资源筛选" onSubmit={(e) => { e.preventDefault(); setFilters((f) => ({ ...f, q: search.trim(), tag: tag.trim(), platform: platform.trim() })); setOffset(0); setExpanded([]) }}>
        <TextField id="resource-search" plain label="搜索名称、编号、平台或地址" value={search} onChange={setSearch} maxLength={100} className="min-w-0 flex-1 basis-56" />
        <Select id="resource-role" help="asset.role" label="类别" value={filters.role} onChange={(v) => update("role", v)} options={[ALL, ...optionsFrom(ROLE_LABEL)]} />
        <Select id="resource-form" help="asset.form" label="形态" value={filters.form} onChange={(v) => update("form", v)} options={[ALL, ...optionsFrom(FORM_LABEL)]} />
        <Select id="resource-site" plain label="场所" value={filters.site_id} onChange={(v) => update("site_id", v)} options={[ALL, ...sites.map((s) => ({ value: s.id, label: s.name }))]} />
        <Select id="resource-lifecycle" help="asset.lifecycle" label="生命周期" value={filters.lifecycle} onChange={(v) => update("lifecycle", v)} options={[ALL, ...optionsFrom(LIFECYCLE_LABEL)]} />
        <TextField id="resource-platform" hint="如 proxmox，需与资产填写的平台完全一致" label="平台（精确匹配）" value={platform} onChange={setPlatform} maxLength={64} className="min-w-0 flex-1 basis-36" />
        <TextField id="resource-tag" help="asset.tagsFilter" label="标签（精确匹配）" value={tag} onChange={setTag} maxLength={50} className="min-w-0 flex-1 basis-36" />
        <Button type="submit" size="sm">应用筛选</Button>
        <Button type="button" variant="ghost" size="sm" onClick={() => { setSearch(""); setTag(""); setPlatform(""); setFilters({ q: "", role: "", form: "", site_id: "", lifecycle: "", tag: "", platform: "" }); setOffset(0); setExpanded([]) }}>清空筛选</Button>
      </form>
      {siteError ? <InlineNotice tone="warn">场所选项读取失败：{siteError} <button type="button" onClick={loadSites} className="underline">重试</button></InlineNotice> : null}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p role="status" aria-label="资源计数" className="text-[0.8125rem] text-muted-foreground">{data ? `共 ${data.total} 项匹配资产 · 当前页 ${data.items.length ? offset + 1 : 0}–${offset + data.items.length} · 每页 ${PAGE_SIZE} 项` : "正在读取真实资源…"}</p>
        <Segmented label="资源展示方式" value={mode} onChange={setMode} options={[{ value: "cards", label: "卡片" }, { value: "rows", label: "长条" }]} />
      </div>
      <p className="text-xs text-muted-foreground">每项匹配资产保留独立入口，数量按资产身份去重；展开的关系引用可能在其他主卡中出现。按共享的显示排序分组分页，没有排过的按名称排在后面；排序只改变显示位置。物理服务器卡片的电源操作复用运维操作的门禁、确认与审计。每 30 秒读取一次。</p>
      <a className="inline-block text-[0.8125rem] text-link underline" href="#/assets">打开资产管理</a>
      {error ? <InlineNotice tone="bad" role="alert">资源读取失败：{error}。当前内容可能过期。<button type="button" onClick={reload} className="underline">重试</button></InlineNotice> : null}
      {data && data.total === 0 ? <section aria-label="资源空状态" className={cn(panelClass, "space-y-2 p-5 text-[0.8125rem]")}>
        <h2 className="font-semibold">没有匹配的资产</h2><p className="text-muted-foreground">请调整筛选；尚未建档时可在资产管理新增或导入。没有指标的资产也会展示。</p>
      </section> : null}
      {groups.map(([group, resources], gi) => { const name = groupLabel(group, ROLE_LABEL); return <section key={group} aria-label={`资源分类 ${name}`}>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <h2 className="text-[0.8125rem] font-semibold">{name}<span className="ml-2 font-normal text-muted-foreground">本页 {resources.length} 项</span></h2>
          {editing ? <>
            <Button size="xs" variant="ghost" disabled={gi === 0} aria-label={`分组上移 ${name}`} onClick={() => moveGroup(group, -1)}><ArrowUp aria-hidden />分组上移</Button>
            <Button size="xs" variant="ghost" disabled={gi === groups.length - 1} aria-label={`分组下移 ${name}`} onClick={() => moveGroup(group, 1)}><ArrowDown aria-hidden />分组下移</Button>
          </> : null}
        </div>
        <div className={mode === "cards" ? "grid items-start gap-4 xl:grid-cols-2 2xl:grid-cols-3" : "space-y-3"}>
          {resources.map((r, i) => <article key={r.id} aria-label={`资源 ${r.name}`} data-resource-id={r.id}
            className={cn(panelClass, "min-w-0 p-4", editing && "outline-dashed outline-1 outline-border")}
            draggable={editing} onDragStart={(e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", r.id); dragging.current = r.id }}
            onDragEnd={() => { dragging.current = null }} onDragOver={(e) => { if (editing) e.preventDefault() }}
            onDrop={(e) => { e.preventDefault(); dropCard(resources, e.dataTransfer.getData("text/plain") || dragging.current, r.id); dragging.current = null }}>
            {editing ? <MoveControls name={r.name} index={i} count={resources.length} onMove={(d) => moveCard(resources, r.id, d)} /> : null}
            <Facts asset={r} onDetails={setDetailsId} compact={mode === "rows"} />
            {!editing && ((r.form === "physical" && r.role === "server") || r.form === "vm" || r.form === "container") ? <div className="mt-3"><PowerEntry resource={r} editable={editable} /></div> : null}
            {r.form !== "physical" ? <p className="mt-2 text-xs text-muted-foreground">虚拟资源经发现它的 PVE / vSphere 平台控制电源；宿主电源不代表本资源状态。</p> : null}
            {r.children_count ? <div className="mt-3 border-t pt-2"><button type="button" className="flex min-h-8 max-w-full items-center gap-1 rounded-md text-left text-xs font-medium"
              aria-expanded={expanded.includes(r.id)} onClick={() => setExpanded((old) => old.includes(r.id) ? old.filter((id) => id !== r.id) : [...old, r.id])}>
              {expanded.includes(r.id) ? <ChevronDown aria-hidden className="size-4 shrink-0" /> : <ChevronRight aria-hidden className="size-4 shrink-0" />}已确认承载 · {r.children_count} 项
            </button>{expanded.includes(r.id) ? <Children key={`${r.id}:${version}`} id={r.id} onDetails={setDetailsId}
              order={editing && draft ? draft.children[r.id] ?? [] : undefined}
              onOrder={(next) => { if (draft) change({ ...draft, children: { ...draft.children, [r.id]: next } }) }} /> : null}</div> : null}
          </article>)}
        </div>
      </section> })}
      {data ? <><Pagination offset={offset} count={data.items.length} total={data.total} setOffset={(v) => { setOffset(v); setExpanded([]) }} label="资源分页" />
        <p className="text-xs text-muted-foreground">快照读取：{dateText(data.generated_at)}</p></> : null}
    </div>
    <AssetDetailDrawer assetId={detailsId} sites={sites} editable={canEdit(user)} onClose={() => setDetailsId(null)} onChanged={reload} />
  </>
}
