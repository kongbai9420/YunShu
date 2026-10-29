import { useEffect, useState, type ReactNode } from "react"
import { Background, Controls, ReactFlow, type Edge, type Node } from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import { Dialog } from "radix-ui"
import { ChevronDown, ChevronRight, CircleHelp, HardDrive, Network, Server } from "lucide-react"

import { PageHeader } from "@/components/AppShell"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Segmented, Select, panelClass } from "@/components/ui/controls"
import { ModalContent, ModalFooter, ModalHeader } from "@/components/ui/dialog"
import { canEdit, useAuth } from "@/lib/auth"
import { cn } from "@/lib/utils"
import { ALL_RESOURCES, DEMO_GROUPS, RESOURCE_BY_ID, type DemoResource } from "./fixtures"
import { SortableList } from "./SortableList"

type View = "dashboard" | "resources" | "topology"
type Order = Record<string, string[]>
const VIEW_OPTIONS: { value: View; label: string }[] = [
  { value: "dashboard", label: "仪表盘" }, { value: "resources", label: "资源全景" }, { value: "topology", label: "拓扑视图" },
]
const GROUP_BY_ID = new Map(DEMO_GROUPS.map((g) => [g.id, g]))
const defaultOrder = (): Order => Object.fromEntries([
  ["groups", DEMO_GROUPS.map((g) => g.id)],
  ...DEMO_GROUPS.map((g) => [g.id, g.resources.map((r) => r.id)]),
  ...ALL_RESOURCES.filter((r) => r.children).map((r) => [r.id, r.children!.map((c) => c.id)]),
])
const nameOf = (id: string) => RESOURCE_BY_ID.get(id)?.name ?? GROUP_BY_ID.get(id)?.title ?? id
const readView = (): View => {
  const value = new URLSearchParams(window.location.hash.split("?")[1]).get("view")
  return VIEW_OPTIONS.some((v) => v.value === value) ? value as View : "resources"
}

const GRAPH_NODES: Node[] = ALL_RESOURCES.map((r, index) => {
  const parentIndex = DEMO_GROUPS[0].resources.findIndex((p) => p.children?.some((c) => c.id === r.id))
  const rootIndex = DEMO_GROUPS[0].resources.findIndex((p) => p.id === r.id)
  return { id: r.id, data: { label: `${r.name}\n${r.form} · ${r.state}` },
    position: { x: parentIndex >= 0 ? 340 : rootIndex >= 0 ? 20 : 700 + Math.floor((index - 7) / 5) * 260,
      y: parentIndex >= 0 ? parentIndex * 230 + DEMO_GROUPS[0].resources[parentIndex].children!.findIndex((c) => c.id === r.id) * 80
        : rootIndex >= 0 ? rootIndex * 230 : ((index - 7) % 5) * 110 },
    style: { width: 220, background: "var(--card)", color: "var(--foreground)", border: "1px solid var(--input)",
      borderRadius: 12, whiteSpace: "pre-line", fontSize: 13 },
  }
})
const HOST_EDGES: Edge[] = DEMO_GROUPS[0].resources.flatMap((p) => (p.children ?? []).map((c) => ({
  id: `${p.id}-${c.id}`, source: p.id, target: c.id, label: "承载", style: { stroke: "var(--primary)" },
})))
const GRAPH_EDGES: Edge[] = [...HOST_EDGES, { id: "network-demo", source: "switch", target: "host-a", label: "演示网络连线",
  style: { stroke: "var(--muted-foreground)", strokeDasharray: "5 4" } }]

export function OverviewPrototypePage() {
  const { user } = useAuth()
  const editable = canEdit(user)
  const [view, setView] = useState<View>(readView)
  const [mode, setMode] = useState<"cards" | "rows">("cards")
  const [q, setQ] = useState("")
  const [category, setCategory] = useState("all")
  const [site, setSite] = useState("all")
  const [status, setStatus] = useState("all")
  const [saved, setSaved] = useState<Order>(defaultOrder)
  const [draft, setDraft] = useState<Order>(defaultOrder)
  const [editing, setEditing] = useState(false)
  const [collapsed, setCollapsed] = useState<string[]>([])
  const [message, setMessage] = useState("")
  const [dialog, setDialog] = useState<{ id: string; kind: "detail" | "power" } | null>(null)
  const [action, setAction] = useState("soft")
  const [confirm, setConfirm] = useState("")
  const order = editing ? draft : saved
  const selected = dialog ? RESOURCE_BY_ID.get(dialog.id) : null
  const highRisk = action === "reset" || action === "off"

  useEffect(() => {
    const update = () => setView(readView())
    window.addEventListener("hashchange", update)
    return () => window.removeEventListener("hashchange", update)
  }, [])
  useEffect(() => {
    if (!editing) return
    const warn = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener("beforeunload", warn)
    return () => window.removeEventListener("beforeunload", warn)
  }, [editing])

  function selectView(next: View) {
    window.location.hash = `/overview-prototype?view=${next}`
  }
  const matches = (r: DemoResource) => {
    const search = q.trim().toLocaleLowerCase()
    return (!search || `${r.name} ${r.role} ${r.platform}`.toLocaleLowerCase().includes(search))
      && (category === "all" || r.role === category || r.form === category)
      && (site === "all" || r.site === site) && (status === "all" || r.state === status)
  }
  const visible = (r: DemoResource) => matches(r) || r.children?.some(matches)
  const visibleGroups = order.groups.filter((id) => GROUP_BY_ID.get(id)!.resources.some(visible))
  const matchedCount = ALL_RESOURCES.filter(matches).length
  const changeOrder = (key: string, ids: string[]) => {
    setDraft((old) => ({ ...old, [key]: ids }))
    setMessage("已调整演示顺序，尚未保存；资产关系不变。")
  }
  function openPower(r: DemoResource) {
    setAction(r.state === "已关机" ? "on" : "soft")
    setConfirm("")
    setDialog({ id: r.id, kind: "power" })
  }
  const details = (r: DemoResource) => setDialog({ id: r.id, kind: "detail" })
  const stateBadge = (r: DemoResource) => <Badge tone={r.state === "运行中" ? "ok" : "neutral"}
    icon={r.state === "未知" ? CircleHelp : undefined}>{r.state}</Badge>

  function resourceCard(r: DemoResource, controls: ReactNode, child = false, parentMatched = false): ReactNode {
    const children = r.children ? order[r.id] : []
    const childIds = children.filter((id) => matches(r) || parentMatched || matches(RESOURCE_BY_ID.get(id)!))
    const folded = collapsed.includes(r.id)
    return <article aria-label={r.name} data-resource-id={r.id}
      className={cn(child ? "min-w-0 rounded-lg border bg-background/60 p-3" : panelClass, !child && "h-full p-4")}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 grow basis-full items-start gap-2 sm:basis-0">
          {!child ? <span className="mt-0.5 shrink-0 text-muted-foreground">{r.role === "服务器" ? <Server className="size-4" />
            : ["NAS", "存储", "UPS"].includes(r.role) ? <HardDrive className="size-4" /> : <Network className="size-4" />}</span> : null}
          <div className="min-w-0 flex-1">
            <h3 className="break-words text-[0.8125rem] font-semibold">{r.name}</h3>
            <p className="mt-1 break-words text-xs text-muted-foreground">{r.form} · {r.role} · {r.platform}</p>
          </div>
        </div>
        {stateBadge(r)}
      </div>
      {controls ? <div className="mt-2 flex justify-end">{controls}</div> : null}
      <div className={cn("mt-3 flex flex-wrap gap-x-5 gap-y-2", mode === "rows" && "sm:items-center")}>
        {r.metrics.map(([label, value]) => <div key={label} className="min-w-0">
          <span className="text-xs text-muted-foreground">{label} </span><span className="text-[0.8125rem] font-medium tabular-nums">{value}</span>
        </div>)}
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t pt-2">
        <span className="text-xs text-muted-foreground">{r.site} · 采集{r.quality}{r.quality === "有效" ? "（演示快照）" : ""}</span>
        <div className="flex flex-wrap gap-1">
          <Button size="xs" variant="ghost" onClick={() => details(r)}>详情</Button>
          {!child ? <Button size="xs" variant="outline" disabled={!editable || !r.power}
            title={!editable ? "只读账号不能操作" : !r.power ? "没有已支持的电源控制能力" : "演示确认流程，不创建任务"}
            onClick={() => openPower(r)}>电源操作（演练）</Button> : <span className="self-center text-xs text-muted-foreground">仅查看，未适配启停</span>}
        </div>
      </div>
      {children.length ? <div className="mt-3 border-t pt-3">
        <button type="button" className="flex min-h-8 items-center gap-1 rounded-md text-xs font-medium"
          aria-expanded={!folded} onClick={() => setCollapsed((old) => folded ? old.filter((id) => id !== r.id) : [...old, r.id])}>
          {folded ? <ChevronRight aria-hidden className="size-4" /> : <ChevronDown aria-hidden className="size-4" />}
          已确认承载 · {children.length} 项
        </button>
        {!folded ? <SortableList ids={childIds} allIds={children} nameOf={nameOf} editing={editing}
          onChange={(ids) => changeOrder(r.id, ids)} className="mt-2 space-y-2">
          {(id, handle) => resourceCard(RESOURCE_BY_ID.get(id)!, handle, true)}
        </SortableList> : null}
      </div> : null}
    </article>
  }

  return <>
    <PageHeader title="总览原型" description="把整体状态、资源承载关系和连接关系放在三个视角中查看。"
      actions={<a href="#/overview" className="rounded-lg border bg-card px-3 py-1.5 text-xs text-link">返回当前总览</a>} />
    <div className="space-y-4 px-4 pb-8 sm:px-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Segmented label="总览类型" value={view} onChange={selectView} options={VIEW_OPTIONS} />
        <span className="text-xs text-muted-foreground">固定演示快照 · 不代表设备实测</span>
      </div>
      <p role="status" aria-label="原型反馈" className="text-[0.8125rem] text-muted-foreground">{message || (editing ? "正在编辑演示排序" : "原型中的修改只保留到本次页面关闭；正式总览与已保存布局不受影响。")}</p>
      {!editable ? <p className="rounded-lg border p-3 text-[0.8125rem]">只读账号：可以查看与切换视角，不能编辑共享排序或进行操作演练。</p> : null}
      {view === "dashboard" ? <section aria-label="演示仪表盘" className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {[["资源", String(ALL_RESOURCES.length), "按资源身份去重"], ["虚拟机与容器", String(ALL_RESOURCES.filter((r) => r.form !== "物理设备").length), "包括待确认宿主的资源"],
            ["明确关机", String(ALL_RESOURCES.filter((r) => r.state === "已关机").length), "未知状态不算关机"], ["采集异常", String(ALL_RESOURCES.filter((r) => ["过期", "失败"].includes(r.quality)).length), "过期与失败单独标识"]].map(([title, value, desc]) =>
            <div key={title} className={cn(panelClass, "p-4")}><h2 className="text-[0.8125rem] font-medium">{title}</h2>
              <p className="mt-2 text-2xl font-semibold tabular-nums">{value}</p><p className="mt-1 text-xs text-muted-foreground">{desc}</p></div>)}
        </div>
        <div className={cn(panelClass, "p-4")}><h2 className="font-semibold">巡检摘要</h2>
          <p className="mt-2 text-[0.8125rem]">演示主机 B 采集失败；其数据库 VM 状态过期，不能据此推断 VM 已关机。</p>
          <Button className="mt-3" variant="outline" onClick={() => selectView("resources")}>查看资源全景</Button>
        </div>
        <p className="text-xs text-muted-foreground">这里用于核对视角切换。当前正式仪表盘的网格、关注列表和共享布局仍保留在“返回当前总览”。</p>
      </section> : null}
      {view === "resources" ? <section aria-label="资源全景" className="space-y-4">
        <div className={cn(panelClass, "flex flex-wrap items-end gap-3 p-3")}>
          <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs text-muted-foreground" htmlFor="prototype-search">搜索演示资源
            <input id="prototype-search" data-help-plain="" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="名称、类别或平台"
              className="h-8 w-full min-w-0 rounded-md border border-input bg-card px-2 text-[0.8125rem] text-foreground" />
          </label>
          <Select id="prototype-category" plain label="类别 / 形态" value={category} onChange={setCategory} options={[{ value: "all", label: "全部类别" },
            ...["服务器", "虚拟机", "容器", "交换机", "路由器", "AP", "防火墙", "NAS", "存储", "UPS", "其他"].map((v) => ({ value: v, label: v }))]} />
          <Select id="prototype-site" plain label="场所" value={site} onChange={setSite} options={[{ value: "all", label: "全部场所" },
            ...["家庭实验室", "演示机房"].map((v) => ({ value: v, label: v }))]} />
          <Select id="prototype-state" plain label="电源状态" value={status} onChange={setStatus} options={[{ value: "all", label: "全部状态" },
            ...["运行中", "已关机", "未知"].map((v) => ({ value: v, label: v }))]} />
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-3"><Segmented label="展示方式" value={mode} onChange={setMode}
            options={[{ value: "cards", label: "卡片" }, { value: "rows", label: "长条" }]} />
            <span className="text-xs text-muted-foreground">匹配 {matchedCount} / {ALL_RESOURCES.length} 项；关联宿主保留为上下文</span></div>
          {editable ? <div className="flex flex-wrap gap-2">{editing ? <>
            <Button size="sm" onClick={() => { setSaved(structuredClone(draft)); setEditing(false); setMessage("已保留本次演示顺序，刷新后恢复默认；未写入数据库。") }}>保存演示排序</Button>
            <Button size="sm" variant="outline" onClick={() => { setEditing(false); setMessage("已取消本次排序修改。") }}>取消排序</Button>
            <Button size="sm" variant="ghost" onClick={() => { setDraft(defaultOrder()); setMessage("已恢复默认草稿，保存后生效。") }}>恢复默认</Button>
          </> : <Button size="sm" variant="outline" onClick={() => { setDraft(structuredClone(saved)); setEditing(true); setMessage("正在编辑演示排序；拖动只改变显示位置，不会迁移虚拟机。") }}>编辑排序</Button>}</div> : null}
        </div>
        {visibleGroups.length === 0 ? <p className={cn(panelClass, "p-6 text-[0.8125rem]")}>没有匹配的演示资源，请调整搜索或筛选。</p> : null}
        <SortableList ids={visibleGroups} allIds={order.groups} nameOf={nameOf} editing={editing} onChange={(ids) => changeOrder("groups", ids)}>
          {(groupId, groupControls) => {
            const group = GROUP_BY_ID.get(groupId)!
            const ids = order[groupId].filter((id) => visible(RESOURCE_BY_ID.get(id)!))
            return <section aria-label={group.title} className="min-w-0 space-y-3">
              <header className="flex flex-wrap items-center justify-between gap-2 pt-2"><div className="min-w-0">
                <h2 className="text-sm font-semibold">{group.title}</h2><p className="mt-1 text-xs text-muted-foreground">{group.description}</p>
              </div>{groupControls}</header>
              <SortableList ids={ids} allIds={order[groupId]} nameOf={nameOf} editing={editing} onChange={(next) => changeOrder(groupId, next)}
                className={mode === "cards" ? "grid items-start gap-3 xl:grid-cols-2" : "space-y-3"}>
                {(id, handle) => resourceCard(RESOURCE_BY_ID.get(id)!, handle)}
              </SortableList>
            </section>
          }}
        </SortableList>
      </section> : null}
      {view === "topology" ? <section aria-label="演示拓扑" className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-[0.8125rem] text-muted-foreground">实线：已确认承载 · 虚线：演示网络连线。卡片排序不改变连线。</p>
          <a className="text-xs text-link underline" href="#/topology">打开当前正式拓扑</a></div>
        <div className={cn(panelClass, "h-[38rem] overflow-hidden")}>
          <ReactFlow nodes={GRAPH_NODES} edges={GRAPH_EDGES} fitView minZoom={0.15} maxZoom={1.5}
            nodesDraggable={false} nodesConnectable={false} onNodeClick={(_, node) => details(RESOURCE_BY_ID.get(node.id)!)}>
            <Background /><Controls showInteractive={false} />
          </ReactFlow>
        </div>
        <details className={cn(panelClass, "p-3")} open><summary className="cursor-pointer text-[0.8125rem] font-medium">承载关系文字列表（键盘可查看详情）</summary>
          <ul className="mt-2 space-y-2">{HOST_EDGES.map((edge) => <li key={edge.id} className="flex flex-wrap items-center gap-1 text-xs">
            {nameOf(edge.source)} → <button className="rounded text-link underline" onClick={() => details(RESOURCE_BY_ID.get(edge.target)!)}>{nameOf(edge.target)}</button><span>（已确认承载，演示）</span>
          </li>)}</ul>
        </details>
      </section> : null}
    </div>
    <Dialog.Root open={!!dialog} onOpenChange={(open) => { if (!open) setDialog(null) }}>
      <ModalContent>
        <ModalHeader title={dialog?.kind === "power" ? "电源操作确认演练" : "演示资源详情"}
          description="固定演示数据；不会访问设备或创建后台任务。" />
        {selected ? <div className="space-y-3">
          <p className="break-words font-semibold">{selected.name}</p>
          {dialog?.kind === "detail" ? <>
            <p>{selected.form} · {selected.role} · {selected.platform} · {selected.site}</p>
            <p>电源状态：{selected.state}；采集质量：{selected.quality}。数值均为演示快照。</p>
            <p className="break-words text-xs text-muted-foreground">演示标识：{selected.id}；示例管理地址：192.0.2.10（保留地址，不访问）。</p>
            <Button variant="outline" onClick={() => { selectView("topology"); setDialog(null) }}>在演示拓扑中查看</Button>
          </> : <>
            <Select id="prototype-power" hint="原型演练，不会下发任何命令" label="操作类型" value={action} onChange={(next) => { setAction(next); setConfirm("") }}
              options={[{ value: "on", label: "开机" }, { value: "soft", label: "正常关机" }, { value: "reset", label: "硬件重启" }, { value: "off", label: "强制断电" }]} />
            <p className="rounded-lg border bg-warn-bg p-3">{selected.children?.length
              ? `已确认下挂 ${selected.children.length} 项资源：${selected.children.map((r) => r.name).join("、")}。宿主操作可能影响这些资源。`
              : "没有确认的下挂资源；不能据此保证没有影响。"}</p>
            <p className="text-xs text-muted-foreground">正式接入时会检查账号权限、设备能力和控制归属。这里仅核对确认流程，不下发指令，也不更改电源状态。</p>
            {highRisk ? <label className="block text-[0.8125rem]" htmlFor="prototype-confirm">输入“{selected.name}”确认演练对象
              <input id="prototype-confirm" data-help-plain="" className="mt-1 h-9 w-full rounded-md border bg-card px-2" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
            </label> : null}
          </>}
        </div> : null}
        <ModalFooter><Dialog.Close asChild><Button variant="outline">关闭</Button></Dialog.Close>
          {dialog?.kind === "power" ? <Button disabled={!editable || (highRisk && confirm !== selected?.name)} onClick={() => {
            setMessage("确认演练完成：未创建任务、未下发指令，设备状态未改变。")
            setDialog(null)
          }}>完成确认演练</Button> : null}
        </ModalFooter>
      </ModalContent>
    </Dialog.Root>
  </>
}
