import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { HelpPill } from "@/components/HelpPill"
import {
  Background,
  Controls,
  MarkerType,
  ReactFlow,
  type Connection,
  type Edge,
  type NodeChange,
  type ReactFlowInstance,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import { Check, EyeOff, Lock, LockOpen, Redo2, RefreshCw, Save, ScanSearch, Undo2, Unlink, Workflow, X } from "lucide-react"

import { PageHeader } from "@/components/AppShell"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice, Select, panelClass } from "@/components/ui/controls"
import { CheckField } from "@/components/ui/form"
import { ApiError } from "@/lib/api"
import { canEdit, seesAll, useAuth } from "@/lib/auth"
import { hrefFor } from "@/lib/router"
import {
  LINK_KIND_LABEL,
  VIEW_KIND_LABEL,
  topologyApi,
  type LayoutPos,
  type RescanPreview,
  type TopoGraph,
  type TopoLink,
  type TopoView,
} from "@/lib/topology"
import { cn } from "@/lib/utils"
import { RELATION_LABEL, labelOf } from "@/lib/vocab"
import { AssetNode, type AssetFlowNode } from "./AssetNode"
import { elkLayout } from "./elkLayout"
import { RescanDialog } from "./RescanDialog"

/** 画布上的四类线：三类网络链路 + 资产关系（承载、安装于等，在资产页维护） */
type Category = "candidate" | "confirmed" | "manual" | "relation"

const CAT_LABEL: Record<Category, string> = { candidate: "候选", confirmed: "已确认", manual: "人工", relation: "资产关系" }
const CAT_DESC: Record<Category, string> = {
  candidate: "LLDP 扫描发现，尚未确认；重新扫描可能新增、更新或移除",
  confirmed: "人工确认过的扫描结果；重新扫描不会改动或删除",
  manual: "手动连线；重新扫描永远不会改动",
  relation: "承载、安装于等资产关系，只读，在资产页维护",
}
const CAT_TONE: Record<Category, "neutral" | "info" | "manual"> = {
  candidate: "neutral",
  confirmed: "info",
  manual: "manual",
  relation: "neutral",
}
const CAT_STROKE: Record<Category, { stroke: string; dash?: string; width: number }> = {
  candidate: { stroke: "var(--status-neutral)", dash: "6 4", width: 1.5 },
  confirmed: { stroke: "var(--status-info)", width: 2 },
  manual: { stroke: "var(--status-manual)", width: 3 },
  relation: { stroke: "var(--muted-foreground)", dash: "1 4", width: 1.5 },
}
/** 超过这个数量时连线标签只在悬停或选中时显示，避免重叠 */
const DENSE_EDGES = 12
const VIEW_KEY = "yunshu-topology-view"

const nodeTypes = { asset: AssetNode }

interface LayoutState {
  positions: Record<string, LayoutPos>
  hidden: string[]
}

function categoryOf(link: TopoLink): Exclude<Category, "relation"> | null {
  if (link.state === "rejected") return null
  if (link.origin === "manual") return "manual"
  return link.state === "confirmed" ? "confirmed" : "candidate"
}

function serialize(s: LayoutState): string {
  const pos = Object.keys(s.positions)
    .sort()
    .map((k) => [k, Math.round(s.positions[k].x), Math.round(s.positions[k].y), s.positions[k].locked])
  return JSON.stringify([pos, [...s.hidden].sort()])
}

/** 两个布局状态之间变化的节点（撤销 / 重做时用来在画布上标出） */
function changedIds(a: LayoutState, b: LayoutState): string[] {
  const ids = new Set([...Object.keys(a.positions), ...Object.keys(b.positions), ...a.hidden, ...b.hidden])
  return [...ids].filter((id) => {
    const p = a.positions[id]
    const q = b.positions[id]
    const moved = !p || !q ? p !== q : Math.round(p.x) !== Math.round(q.x) || Math.round(p.y) !== Math.round(q.y) || p.locked !== q.locked
    return moved || a.hidden.includes(id) !== b.hidden.includes(id)
  })
}

function portText(link: { source_port: string; target_port: string }) {
  return link.source_port || link.target_port ? `${link.source_port || "?"} ↔ ${link.target_port || "?"}` : ""
}

function readViewPref(): string {
  try {
    return localStorage.getItem(VIEW_KEY) ?? ""
  } catch {
    return ""
  }
}

function SvgLegendLine({ cat }: { cat: Category }) {
  const s = CAT_STROKE[cat]
  return (
    <svg aria-hidden width="32" height="10" className="shrink-0">
      <line x1="0" y1="5" x2={cat === "manual" ? 26 : 32} y2="5" stroke={s.stroke} strokeWidth={s.width} strokeDasharray={s.dash} />
      {cat === "manual" ? <path d="M25 1 L32 5 L25 9 Z" fill={s.stroke} /> : null}
    </svg>
  )
}

/** 不用拖动也能新增人工连线（键盘、读屏用户） */
function ManualLinkForm({
  options,
  onAdd,
}: {
  options: { id: string; name: string }[]
  onAdd: (body: { source: string; target: string; sourcePort: string; targetPort: string; kind: string }) => Promise<boolean>
}) {
  const [source, setSource] = useState("")
  const [target, setTarget] = useState("")
  const [sourcePort, setSourcePort] = useState("")
  const [targetPort, setTargetPort] = useState("")
  const [kind, setKind] = useState("ethernet")
  const [busy, setBusy] = useState(false)
  const opts = [{ value: "", label: "选择设备" }, ...options.map((o) => ({ value: o.id, label: o.name }))]
  const input = "h-8 w-full rounded-md border border-input bg-card px-2 text-[0.8125rem]"
  return (
    <section className={cn(panelClass, "p-4")}>
      <h2 className="mb-2 text-[0.8125rem] font-semibold">新增人工连线</h2>
      <form
        className="space-y-2"
        onSubmit={async (e) => {
          e.preventDefault()
          setBusy(true)
          const ok = await onAdd({ source, target, sourcePort, targetPort, kind })
          setBusy(false)
          if (ok) {
            setSource("")
            setTarget("")
            setSourcePort("")
            setTargetPort("")
          }
        }}
      >
        <div className="grid grid-cols-[1fr_6rem] gap-2">
          <Select id="ml-source" plain label="起点设备" value={source} onChange={setSource} options={opts} />
          <div className="flex flex-col gap-1">
            <label htmlFor="ml-sport" className="text-xs font-medium text-muted-foreground">端口（可选）</label>
            <input id="ml-sport" data-help-plain="" title="同一对设备之间有多条线时，填端口区分每一条" className={input} value={sourcePort} maxLength={200} onChange={(e) => setSourcePort(e.target.value)} />
          </div>
          <Select id="ml-target" plain label="终点设备" value={target} onChange={setTarget} options={opts} />
          <div className="flex flex-col gap-1">
            <label htmlFor="ml-tport" className="text-xs font-medium text-muted-foreground">端口（可选）</label>
            <input id="ml-tport" data-help-plain="" title="同一对设备之间有多条线时，填端口区分每一条" className={input} value={targetPort} maxLength={200} onChange={(e) => setTargetPort(e.target.value)} />
          </div>
        </div>
        <Select
          id="ml-kind"
          label="类型"
          help="topology.link.kind"
          value={kind}
          onChange={setKind}
          options={Object.entries(LINK_KIND_LABEL).map(([value, label]) => ({ value, label }))}
        />
        <Button type="submit" size="xs" disabled={busy || !source || !target || source === target}>
          新增连线
        </Button>
      </form>
    </section>
  )
}

export function TopologyPage({ onDirtyChange, requestedViewId, onViewChange }: {
  onDirtyChange?: (dirty: boolean) => void
  requestedViewId?: string
  onViewChange?: (id: string) => void
} = {}) {
  const { user } = useAuth()
  // 拓扑是共享的：范围受限的账号只能查看（4.1 P11）
  const editable = canEdit(user) && seesAll(user)

  const [views, setViews] = useState<TopoView[]>([])
  const [view, setView] = useState<TopoView | null>(null)
  const [graph, setGraph] = useState<TopoGraph | null>(null)
  const [layout, setLayoutState] = useState<LayoutState>({ positions: {}, hidden: [] })
  const layoutRef = useRef(layout)
  const [baseline, setBaseline] = useState("")
  const [auto, setAuto] = useState<Record<string, { x: number; y: number }>>({})
  const [measured, setMeasured] = useState<Record<string, { width: number; height: number }>>({})
  const [past, setPast] = useState<LayoutState[]>([])
  const [future, setFuture] = useState<LayoutState[]>([])
  const [flash, setFlash] = useState<{ ids: string[]; kind: "undo" | "redo" } | null>(null)
  const flashTimer = useRef<number | undefined>(undefined)

  const [loadError, setLoadError] = useState("")
  const [actionError, setActionError] = useState("")
  const [conflict, setConflict] = useState("")
  const [saving, setSaving] = useState(false)
  const [layingOut, setLayingOut] = useState(false)
  const [announce, setAnnounce] = useState("")
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null)
  const [selectedNode, setSelectedNode] = useState<string | null>(null)
  const [hoverEdge, setHoverEdge] = useState<string | null>(null)
  const [showRelations, setShowRelations] = useState(true)

  const [scanOpen, setScanOpen] = useState(false)
  const [scanLoading, setScanLoading] = useState(false)
  const [scanPreview, setScanPreview] = useState<RescanPreview | null>(null)
  const [scanError, setScanError] = useState("")
  const [scanApplying, setScanApplying] = useState(false)

  const rf = useRef<ReactFlowInstance<AssetFlowNode, Edge> | null>(null)
  // 载入或自动布局后适应画布：要等新节点渲染出来再调用，否则按旧节点（或空画布）计算，部分设备落在可视区外
  const [fitTick, setFitTick] = useState(0)
  const requestFit = useCallback(() => setFitTick((t) => t + 1), [])
  useEffect(() => {
    if (!fitTick) return
    const id = requestAnimationFrame(() => void rf.current?.fitView({ padding: 0.15 }))
    return () => cancelAnimationFrame(id)
  }, [fitTick])
  const dragSnapshot = useRef<LayoutState | null>(null)

  const setLayout = (next: LayoutState) => {
    layoutRef.current = next
    setLayoutState(next)
  }
  const dirty = !!view && serialize(layout) !== baseline
  useEffect(() => {
    onDirtyChange?.(dirty)
    return () => onDirtyChange?.(false)
  }, [dirty, onDirtyChange])
  const loadGeneration = useRef(0)

  const names = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n.name])), [graph])
  const nameOf = useCallback((id: string) => names.get(id) ?? "已删除的资产", [names])

  // ---- 载入 ----

  const loadView = useCallback(async (id: string, announceMsg?: (v: TopoView) => string) => {
    const generation = ++loadGeneration.current
    setLoadError("")
    setConflict("")
    try {
      const [v, g] = await Promise.all([topologyApi.view(id), topologyApi.graph(id)])
      if (generation !== loadGeneration.current) return
      const initial: LayoutState = { positions: { ...v.layout }, hidden: [...v.hidden] }
      // 没有保存过位置的节点用 ELK 自动排；已保存的当作锁定，不移动
      const missing = g.nodes.filter((n) => !initial.positions[n.id])
      let autoPos: Record<string, { x: number; y: number }> = {}
      if (missing.length) {
        setLayingOut(true)
        try {
          const laid = await elkLayout(
            g.nodes.map((n) => ({ id: n.id, locked: !!initial.positions[n.id] })),
            g.links.filter((l) => l.state !== "rejected").map((l) => ({ id: l.id, source: l.source_asset_id, target: l.target_asset_id })),
          )
          autoPos = Object.fromEntries(laid)
        } catch {
          autoPos = Object.fromEntries(missing.map((n, i) => [n.id, { x: (i % 5) * 220, y: Math.floor(i / 5) * 110 }]))
          if (generation === loadGeneration.current) setActionError("自动布局失败，已按网格排列；可以手动拖动后保存")
        } finally {
          if (generation === loadGeneration.current) setLayingOut(false)
        }
      }
      if (generation !== loadGeneration.current) return
      setView(v)
      setGraph(g)
      setAuto(autoPos)
      setLayout(initial)
      setBaseline(serialize(initial))
      setPast([])
      setFuture([])
      setFlash(null)
      setAnnounce(announceMsg?.(v) ?? "")
      setSelectedEdge(null)
      setSelectedNode(null)
      requestFit()
    } catch (e) {
      if (generation === loadGeneration.current) setLoadError(e instanceof ApiError ? e.message : "载入拓扑失败")
    }
  }, [requestFit])

  useEffect(() => {
    let cancelled = false
    setView(null)
    setGraph(null)
    setLayingOut(false)
    setLoadError("")
    topologyApi
      .views()
      .then((list) => {
        if (cancelled) return
        setViews(list)
        const pref = readViewPref()
        const first = requestedViewId ? list.find((v) => v.id === requestedViewId) : list.find((v) => v.id === pref) ?? list[0]
        if (first) {
          try { localStorage.setItem(VIEW_KEY, first.id) } catch { /* 浏览器禁用存储时仍可查看 */ }
          void loadView(first.id)
        }
        else setLoadError(requestedViewId ? "链接指定的拓扑视图不存在，请选择一个可用视图。" : "还没有拓扑视图")
      })
      .catch((e) => { if (!cancelled) setLoadError(e instanceof ApiError ? e.message : "载入拓扑失败") })
    return () => { cancelled = true; loadGeneration.current++ }
  }, [loadView, requestedViewId])

  const refreshGraph = useCallback(async () => {
    if (!view) return
    const g = await topologyApi.graph(view.id)
    setGraph(g)
    // 新出现的资产放在已有节点下方，不进撤销历史，也不算未保存的修改
    setAuto((cur) => {
      const next = { ...cur }
      const all = [...Object.values(layoutRef.current.positions), ...Object.values(cur)]
      let y = all.length ? Math.max(...all.map((p) => p.y)) + 120 : 0
      let x = 0
      for (const n of g.nodes) {
        if (layoutRef.current.positions[n.id] || next[n.id]) continue
        next[n.id] = { x, y }
        x += 220
        if (x > 880) {
          x = 0
          y += 110
        }
      }
      return next
    })
  }, [view])

  // 未保存时离开页面先确认
  useEffect(() => {
    if (!dirty) return
    const onBefore = (e: BeforeUnloadEvent) => e.preventDefault()
    window.addEventListener("beforeunload", onBefore)
    return () => window.removeEventListener("beforeunload", onBefore)
  }, [dirty])

  // ---- 布局编辑与撤销 / 重做（只针对布局；连线改动立即保存到服务器） ----

  const commit = (next: LayoutState, msg: string, prev: LayoutState = layoutRef.current) => {
    setPast((p) => [...p.slice(-99), prev])
    setFuture([])
    setLayout(next)
    setAnnounce(msg)
  }

  const showFlash = (ids: string[], kind: "undo" | "redo") => {
    window.clearTimeout(flashTimer.current)
    setFlash({ ids, kind })
    flashTimer.current = window.setTimeout(() => setFlash(null), 2500)
  }

  const describe = (ids: string[]) =>
    ids.length === 1 ? `「${nameOf(ids[0])}」` : ids.length ? `${ids.length} 个节点` : "布局"

  const undo = () => {
    if (!past.length) return
    const cur = layoutRef.current
    const prev = past[past.length - 1]
    setFuture((f) => [cur, ...f])
    setPast(past.slice(0, -1))
    setLayout(prev)
    const ids = changedIds(cur, prev)
    showFlash(ids, "undo")
    setAnnounce(`已撤销对${describe(ids)}的修改`)
  }

  const redo = () => {
    if (!future.length) return
    const cur = layoutRef.current
    const next = future[0]
    setPast((p) => [...p, cur])
    setFuture(future.slice(1))
    setLayout(next)
    const ids = changedIds(cur, next)
    showFlash(ids, "redo")
    setAnnounce(`已重做对${describe(ids)}的修改`)
  }
  const undoRef = useRef(undo)
  const redoRef = useRef(redo)
  undoRef.current = undo
  redoRef.current = redo

  useEffect(() => {
    if (!editable) return
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return
      if (!(e.ctrlKey || e.metaKey)) return
      const k = e.key.toLowerCase()
      if (k === "z" && !e.shiftKey) {
        e.preventDefault()
        undoRef.current()
      } else if ((k === "z" && e.shiftKey) || k === "y") {
        e.preventDefault()
        redoRef.current()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [editable])

  const posOf = (id: string): LayoutPos => {
    const p = layout.positions[id]
    if (p) return p
    const a = auto[id] ?? { x: 0, y: 0 }
    return { x: a.x, y: a.y, locked: false }
  }

  const flashKind = (id: string) => (flash?.ids.includes(id) ? flash.kind : "")

  const visibleNodes: AssetFlowNode[] = useMemo(
    () =>
      (graph?.nodes ?? [])
        .filter((n) => !layout.hidden.includes(n.id))
        .map((n) => {
          const p = posOf(n.id)
          return {
            id: n.id,
            type: "asset" as const,
            position: { x: p.x, y: p.y },
            data: { asset: n, locked: p.locked, datacenter: view?.kind === "datacenter", flash: flashKind(n.id), connectable: editable },
            draggable: editable && !p.locked,
            selected: n.id === selectedNode,
            measured: measured[n.id],
            ariaLabel: `设备 ${n.name}${p.locked ? "，位置已锁定" : ""}`,
          }
        }),
    [graph, layout, auto, selectedNode, measured, flash, view?.kind, editable],
  )
  const visibleIds = useMemo(() => new Set(visibleNodes.map((n) => n.id)), [visibleNodes])

  const visibleLinks = useMemo(
    () =>
      (graph?.links ?? []).filter(
        (l) => categoryOf(l) && visibleIds.has(l.source_asset_id) && visibleIds.has(l.target_asset_id),
      ),
    [graph, visibleIds],
  )
  const visibleRelations = useMemo(
    () =>
      showRelations
        ? (graph?.relations ?? []).filter((r) => visibleIds.has(r.source_asset_id) && visibleIds.has(r.target_asset_id))
        : [],
    [graph, visibleIds, showRelations],
  )
  const dense = visibleLinks.length + visibleRelations.length > DENSE_EDGES

  const edges: Edge[] = useMemo(() => {
    const make = (id: string, source: string, target: string, cat: Category, text: string, reconnectable: boolean): Edge => {
      const s = CAT_STROKE[cat]
      const active = id === selectedEdge || id === hoverEdge
      return {
        id,
        source,
        target,
        label: !dense || active ? text : undefined,
        selected: id === selectedEdge,
        reconnectable,
        ariaLabel: `${text}：${nameOf(source)} 到 ${nameOf(target)}`,
        style: { stroke: s.stroke, strokeWidth: id === selectedEdge ? s.width + 1.5 : s.width, strokeDasharray: s.dash },
        labelStyle: { fontSize: 11, fill: "var(--foreground)" },
        labelBgStyle: { fill: "var(--card)" },
        labelBgPadding: [4, 2] as [number, number],
        markerEnd: cat === "manual" ? { type: MarkerType.ArrowClosed, color: s.stroke } : undefined,
        zIndex: active ? 10 : 0,
      }
    }
    return [
      ...visibleLinks.map((l) => {
        const cat = categoryOf(l)!
        const ports = portText(l)
        return make(l.id, l.source_asset_id, l.target_asset_id, cat, `${CAT_LABEL[cat]}${ports ? ` · ${ports}` : ""}`, editable && cat === "manual")
      }),
      ...visibleRelations.map((r) =>
        make(`rel-${r.id}`, r.source_asset_id, r.target_asset_id, "relation", labelOf(RELATION_LABEL, r.kind), false),
      ),
    ]
  }, [visibleLinks, visibleRelations, selectedEdge, hoverEdge, dense, nameOf, editable])

  const onNodesChange = (changes: NodeChange<AssetFlowNode>[]) => {
    for (const c of changes) {
      if (c.type === "select" && c.selected) {
        setSelectedNode(c.id)
        setSelectedEdge(null)
      }
    }
    const dims = changes.filter((c) => c.type === "dimensions" && c.dimensions)
    if (dims.length)
      setMeasured((m) => {
        const next = { ...m }
        for (const c of dims) if (c.type === "dimensions" && c.dimensions) next[c.id] = c.dimensions
        return next
      })
    const moves = changes.filter((c) => c.type === "position" && c.position)
    if (!moves.length || !editable) return
    const cur = layoutRef.current
    const positions = { ...cur.positions }
    for (const c of moves) {
      if (c.type !== "position" || !c.position) continue
      const prev = cur.positions[c.id] ?? { ...(auto[c.id] ?? { x: 0, y: 0 }), locked: false }
      if (prev.locked) continue
      positions[c.id] = { ...prev, x: c.position.x, y: c.position.y }
    }
    const next = { ...cur, positions }
    // 键盘方向键移动（不是拖动）：每一步都记入撤销历史
    const keyboardMove = moves.some((c) => c.type === "position" && !c.dragging) && !dragSnapshot.current
    if (keyboardMove) commit(next, `已移动「${nameOf(moves[0].type === "position" ? moves[0].id : "")}」`)
    else setLayout(next)
  }

  const autoLayout = async () => {
    setLayingOut(true)
    try {
      const nodes = visibleNodes.map((n) => ({ id: n.id, locked: n.data.locked }))
      const laid = await elkLayout(
        nodes,
        edges.map((e) => ({ id: e.id, source: e.source, target: e.target })),
      )
      const cur = layoutRef.current
      const positions = { ...cur.positions }
      for (const [id, p] of laid) positions[id] = { x: p.x, y: p.y, locked: false }
      const locked = nodes.filter((n) => n.locked).length
      commit({ ...cur, positions }, locked ? `已自动布局，${locked} 个锁定节点保持原位` : "已自动布局")
      requestFit()
    } catch {
      setActionError("自动布局失败，请稍后重试")
    } finally {
      setLayingOut(false)
    }
  }

  const toggleLock = (id: string) => {
    const cur = layoutRef.current
    const p = posOf(id)
    const locked = !p.locked
    commit(
      { ...cur, positions: { ...cur.positions, [id]: { ...p, locked } } },
      `${locked ? "已锁定" : "已解锁"}「${nameOf(id)}」的位置`,
    )
  }

  const hideNode = (id: string) => {
    const cur = layoutRef.current
    commit({ ...cur, hidden: [...cur.hidden, id] }, `已将「${nameOf(id)}」移出视图，资产本身未删除`)
    setSelectedNode(null)
  }
  const restoreNode = (id: string) => {
    const cur = layoutRef.current
    commit({ ...cur, hidden: cur.hidden.filter((h) => h !== id) }, `已将「${nameOf(id)}」放回视图`)
  }

  // ---- 保存与冲突 ----

  const layoutBody = (revision: number) => {
    const ids = new Set((graph?.nodes ?? []).map((n) => n.id))
    const positions: Record<string, LayoutPos> = {}
    for (const id of ids) {
      const p = posOf(id)
      positions[id] = { x: Math.round(p.x), y: Math.round(p.y), locked: p.locked }
    }
    return { revision, layout: positions, hidden: layout.hidden.filter((h) => ids.has(h)) }
  }

  const afterSave = (saved: TopoView) => {
    const state: LayoutState = { positions: { ...saved.layout }, hidden: [...saved.hidden] }
    setView(saved)
    setViews((vs) => vs.map((v) => (v.id === saved.id ? saved : v)))
    setLayout(state)
    setBaseline(serialize(state))
    setAuto({})
    setConflict("")
    setAnnounce(`布局已保存（修订号 ${saved.revision}）`)
  }

  const save = async () => {
    if (!view) return
    setSaving(true)
    setActionError("")
    try {
      afterSave(await topologyApi.saveView(view.id, layoutBody(view.revision)))
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setConflict(e.message)
      else setActionError(e instanceof ApiError ? e.message : "保存失败")
    } finally {
      setSaving(false)
    }
  }

  const overwrite = async () => {
    if (!view) return
    setSaving(true)
    try {
      const latest = await topologyApi.view(view.id)
      afterSave(await topologyApi.saveView(view.id, layoutBody(latest.revision)))
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setConflict(e.message)
      else setActionError(e instanceof ApiError ? e.message : "保存失败")
    } finally {
      setSaving(false)
    }
  }

  const switchView = (id: string) => {
    if (onViewChange) {
      onViewChange(id)
      return
    }
    if (dirty && !window.confirm("当前视图有未保存的布局修改，切换后会丢失。继续吗？")) return
    try {
      localStorage.setItem(VIEW_KEY, id)
    } catch {
      // 存储不可用时只在本次会话内生效
    }
    void loadView(id)
  }

  // ---- 链路（立即保存到服务器，记审计；不进布局撤销历史） ----

  const linkAction = async (fn: () => Promise<unknown>, msg: string): Promise<boolean> => {
    setActionError("")
    try {
      await fn()
      await refreshGraph()
      setAnnounce(msg)
      return true
    } catch (e) {
      setActionError(e instanceof ApiError ? e.message : "操作失败")
      return false
    }
  }

  const addManual = async (b: { source: string; target: string; sourcePort: string; targetPort: string; kind: string }) => {
    if (!b.source || !b.target || b.source === b.target) {
      setAnnounce("请选择两台不同的设备")
      return false
    }
    return linkAction(
      () =>
        topologyApi
          .createLink({ source_asset_id: b.source, target_asset_id: b.target, source_port: b.sourcePort, target_port: b.targetPort, kind: b.kind })
          .then((l) => {
            setSelectedEdge(l.id)
            setSelectedNode(null)
          }),
      `已新增人工连线：${nameOf(b.source)} 到 ${nameOf(b.target)}`,
    )
  }

  const onConnect = (c: Connection) => {
    if (editable && c.source && c.target)
      void addManual({ source: c.source, target: c.target, sourcePort: "", targetPort: "", kind: "ethernet" })
  }

  const onReconnect = (old: Edge, c: Connection) => {
    const link = graph?.links.find((l) => l.id === old.id)
    if (!editable || !link || link.origin !== "manual" || !c.source || !c.target) return
    const body: { source_asset_id: string; target_asset_id: string; source_port?: string; target_port?: string } = {
      source_asset_id: c.source,
      target_asset_id: c.target,
    }
    // 换了端点设备时原端口不再适用
    if (c.source !== link.source_asset_id) body.source_port = ""
    if (c.target !== link.target_asset_id) body.target_port = ""
    void linkAction(() => topologyApi.updateLink(link.id, body), `已改接人工连线：${nameOf(c.source)} 到 ${nameOf(c.target)}`)
  }

  // ---- 重新扫描 ----

  const openScan = async () => {
    setScanOpen(true)
    setScanLoading(true)
    setScanError("")
    setScanPreview(null)
    try {
      setScanPreview(await topologyApi.preview())
    } catch (e) {
      setScanError(e instanceof ApiError ? e.message : "预览失败")
    } finally {
      setScanLoading(false)
    }
  }

  const applyScan = async () => {
    if (!scanPreview) return
    setScanApplying(true)
    setScanError("")
    try {
      const r = await topologyApi.apply(scanPreview.token)
      await refreshGraph()
      setAnnounce(`已合并扫描结果：新增 ${r.added}，更新 ${r.refreshed}，移除 ${r.removed}；${r.protected} 条已确认或人工链路未改动`)
      setScanOpen(false)
    } catch (e) {
      setScanError(e instanceof ApiError ? e.message : "合并失败")
    } finally {
      setScanApplying(false)
    }
  }

  // ---- 渲染 ----

  const selLink = graph?.links.find((l) => l.id === selectedEdge) ?? null
  const selRelation = selectedEdge?.startsWith("rel-") ? graph?.relations.find((r) => `rel-${r.id}` === selectedEdge) ?? null : null
  const selNode = graph?.nodes.find((n) => n.id === selectedNode && !layout.hidden.includes(n.id)) ?? null
  const selCat = selLink ? categoryOf(selLink) : null
  const rejected = (graph?.links ?? []).filter((l) => l.state === "rejected")
  const hiddenNodes = layout.hidden.filter((id) => names.has(id))

  const evidenceLines = (link: TopoLink) =>
    (link.evidence.observations ?? []).map(
      (o) => `${nameOf(o.reporter_asset_id)} 端口 ${o.local_port || "?"} 看到 ${o.remote_name || "?"} 的 ${o.remote_port || "?"}`,
    )

  return (
    <div className="flex h-full flex-col pb-6">
      <PageHeader
        title="拓扑"
        description="三类链路分开显示：候选（细虚线）、已确认（实线）、人工（粗实线带箭头）；资产关系用点线。重新扫描只改动候选，不覆盖人工连线和锁定位置。"
      />
      <div className="flex flex-wrap items-end gap-2 px-6 pb-3">
        <Select
          id="topo-view"
          label="视图"
          help="topology.view"
          value={view?.id ?? ""}
          onChange={switchView}
          options={views.map((v) => ({ value: v.id, label: `${v.name}（${labelOf(VIEW_KIND_LABEL, v.kind)}）` }))}
          className="w-56"
        />
        {editable ? (
          <>
            <Button size="sm" variant="outline" onClick={autoLayout} disabled={layingOut || !graph}>
              <Workflow aria-hidden />
              {layingOut ? "正在布局…" : "自动布局"}
            </Button>
            <Button size="sm" variant="outline" onClick={openScan} disabled={!graph}>
              <ScanSearch aria-hidden />
              重新扫描
            </Button>
            <HelpPill topic="topology.rescan" className="self-center" />
            <span className="mx-1 h-5 w-px self-center bg-border" aria-hidden />
            <Button size="sm" variant="ghost" onClick={undo} disabled={!past.length} aria-keyshortcuts="Control+Z">
              <Undo2 aria-hidden />
              撤销
            </Button>
            <Button size="sm" variant="ghost" onClick={redo} disabled={!future.length} aria-keyshortcuts="Control+Shift+Z">
              <Redo2 aria-hidden />
              重做
            </Button>
            <Button size="sm" onClick={save} disabled={!dirty || saving}>
              <Save aria-hidden />
              {saving ? "正在保存…" : "保存布局"}
            </Button>
          </>
        ) : null}
        <span className="self-center text-xs text-muted-foreground">
          {view ? (
            <>
              {dirty ? <span className="font-medium text-foreground">有未保存的布局修改 · </span> : null}
              修订号 {view.revision}
              {view.updated_by ? ` · 最后保存：${view.updated_by}` : ""}
              {editable ? "" : canEdit(user) ? " · 拓扑编辑只对可见全部资产的账号开放，如需请联系管理员" : " · 只读角色只能查看"}
            </>
          ) : null}
        </span>
        <p className="sr-only" aria-live="polite">
          {announce}
        </p>
        <span className="self-center text-xs text-muted-foreground" aria-hidden>
          {announce}
        </span>
      </div>

      <div className="space-y-2 px-6 empty:hidden">
        {loadError ? (
          <InlineNotice tone="bad" role="alert" className="mb-3">
            {loadError}
          </InlineNotice>
        ) : null}
        {conflict ? (
          <InlineNotice tone="warn" role="alert" className="mb-3">
            <p>{conflict}</p>
            <p className="mt-1 text-xs text-muted-foreground">你的修改还在画布上，没有被覆盖。</p>
            <div className="mt-2 flex flex-wrap gap-2">
              <Button size="xs" onClick={() => view && loadView(view.id, (v) => `已载入最新布局（修订号 ${v.revision}）`)}>
                <RefreshCw aria-hidden />
                载入最新（放弃我的修改）
              </Button>
              <Button size="xs" variant="outline" onClick={overwrite} disabled={saving}>
                用我的布局覆盖
              </Button>
              <HelpPill topic="layout.overwrite" className="self-center" />
              <Button size="xs" variant="ghost" onClick={() => setConflict("")}>
                稍后处理
              </Button>
            </div>
          </InlineNotice>
        ) : null}
        {actionError ? (
          <InlineNotice tone="bad" role="alert" className="mb-3">
            {actionError}
          </InlineNotice>
        ) : null}
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-4 px-6 lg:flex-row">
        <div className={cn(panelClass, "relative h-[560px] min-h-[420px] flex-1 overflow-hidden lg:h-auto")}>
          {!graph && !loadError ? (
            <p role="status" className="absolute inset-0 z-10 flex items-center justify-center text-[0.8125rem] text-muted-foreground">
              {layingOut ? "正在自动排列设备位置…" : "正在载入拓扑…"}
            </p>
          ) : null}
          {graph && graph.nodes.length === 0 ? (
            <div className="absolute inset-0 z-10 flex items-center justify-center p-6 text-center text-muted-foreground">
              <p>
                这个视图里还没有资产。先到<a className="text-link hover:underline" href={hrefFor("assets")}>资产管理</a>
                添加设备，SNMP 采集到 LLDP 邻居后可以重新扫描生成候选链路。
              </p>
            </div>
          ) : null}
          <ReactFlow<AssetFlowNode, Edge>
            nodes={visibleNodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={(changes) => {
              for (const c of changes)
                if (c.type === "select" && c.selected) {
                  setSelectedEdge(c.id)
                  setSelectedNode(null)
                }
            }}
            onConnect={onConnect}
            onReconnect={onReconnect}
            onEdgeMouseEnter={(_, e) => setHoverEdge(e.id)}
            onEdgeMouseLeave={() => setHoverEdge(null)}
            onInit={(inst) => {
              rf.current = inst
            }}
            onNodeDragStart={() => {
              dragSnapshot.current = layoutRef.current
            }}
            onNodeDragStop={(_, node) => {
              if (dragSnapshot.current) {
                const prev = dragSnapshot.current
                dragSnapshot.current = null
                if (serialize(prev) !== serialize(layoutRef.current))
                  commit(layoutRef.current, `已移动「${node.data.asset.name}」`, prev)
              }
            }}
            onPaneClick={() => {
              setSelectedEdge(null)
              setSelectedNode(null)
            }}
            nodesDraggable={editable}
            nodesConnectable={editable}
            edgesReconnectable={editable}
            deleteKeyCode={null}
            fitView
            minZoom={0.2}
            ariaLabelConfig={{
              "node.a11yDescription.default": editable
                ? "按回车或空格选中设备，选中后用方向键移动位置。"
                : "按回车或空格选中设备。",
              "node.a11yDescription.keyboardDisabled": "按回车或空格选中设备。",
              "edge.a11yDescription.default": "按回车或空格选中这条连线。",
              "controls.ariaLabel": "画布缩放控制",
              "controls.zoomIn.ariaLabel": "放大",
              "controls.zoomOut.ariaLabel": "缩小",
              "controls.fitView.ariaLabel": "适应画布",
              "controls.interactive.ariaLabel": "切换可交互",
              "minimap.ariaLabel": "缩略图",
              "handle.ariaLabel": "连线端点",
            }}
          >
            <Background gap={20} size={1} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>

        <aside aria-label="拓扑详情" className="w-full shrink-0 space-y-4 text-[0.8125rem] lg:w-80 lg:overflow-y-auto">
          <section className={cn(panelClass, "p-4")}>
            <h2 className="mb-2 text-[0.8125rem] font-semibold">图例</h2>
            <ul className="space-y-2">
              {(["candidate", "confirmed", "manual", "relation"] as Category[]).map((k) => (
                <li key={k} className="flex items-start gap-2">
                  <span className="mt-1">
                    <SvgLegendLine cat={k} />
                  </span>
                  <span>
                    <span className="font-medium">{CAT_LABEL[k]}</span>
                    <span className="block text-xs text-muted-foreground">{CAT_DESC[k]}</span>
                  </span>
                </li>
              ))}
            </ul>
            <CheckField className="mt-3 text-xs" label="显示资产关系" hint="叠加显示宿主承载等人工资产关系" checked={showRelations} onChange={setShowRelations} />
            {dense ? <p className="mt-2 text-xs text-muted-foreground">连线较多，标签只在悬停或选中时显示。</p> : null}
          </section>

          <section className={cn(panelClass, "p-4")} aria-live="polite">
            <h2 className="mb-2 text-[0.8125rem] font-semibold">选中项</h2>
            {selNode ? (
              <div className="space-y-2">
                <p className="font-medium">{selNode.name}</p>
                <p className="text-xs text-muted-foreground">
                  {selNode.site_name ? `场所 ${selNode.site_name}` : "未指定场所"}
                  {selNode.rack_name ? ` · 机柜 ${selNode.rack_name}${selNode.rack_unit != null ? ` U${selNode.rack_unit}` : ""}` : ""}
                </p>
                {editable ? (
                  <>
                    <div className="flex flex-wrap gap-2">
                      <Button size="xs" variant="outline" onClick={() => toggleLock(selNode.id)}>
                        {posOf(selNode.id).locked ? <LockOpen aria-hidden /> : <Lock aria-hidden />}
                        {posOf(selNode.id).locked ? "解锁位置" : "锁定位置"}
                      </Button>
                      <Button size="xs" variant="outline" onClick={() => hideNode(selNode.id)}>
                        <EyeOff aria-hidden />
                        移出视图
                      </Button>
                    </div>
                    <p className="text-xs text-muted-foreground">锁定后自动布局和重新扫描都不会移动它。移出视图只是不显示，不删除资产和连线。</p>
                  </>
                ) : null}
              </div>
            ) : selLink && selCat ? (
              <div className="space-y-2">
                <Badge tone={CAT_TONE[selCat]}>{CAT_LABEL[selCat]}链路</Badge>
                <p>
                  {nameOf(selLink.source_asset_id)} → {nameOf(selLink.target_asset_id)}
                </p>
                <p className="text-xs text-muted-foreground">
                  {labelOf(LINK_KIND_LABEL, selLink.kind)}
                  {portText(selLink) ? ` · 端口 ${portText(selLink)}` : ""}
                  {selLink.notes ? ` · ${selLink.notes}` : ""}
                </p>
                {evidenceLines(selLink).length ? (
                  <ul className="space-y-0.5 text-xs text-muted-foreground">
                    {evidenceLines(selLink).map((line) => (
                      <li key={line}>LLDP：{line}</li>
                    ))}
                  </ul>
                ) : null}
                {editable ? (
                  <div className="flex flex-wrap gap-2">
                    {selCat === "candidate" ? (
                      <>
                        <Button size="xs" onClick={() => linkAction(() => topologyApi.confirmLink(selLink.id), "已确认这条链路")}>
                          <Check aria-hidden />
                          确认链路
                        </Button>
                        <Button
                          size="xs"
                          variant="outline"
                          onClick={async () => {
                            if (await linkAction(() => topologyApi.rejectLink(selLink.id), "已忽略这条候选，重新扫描不会再提出"))
                              setSelectedEdge(null)
                          }}
                        >
                          <X aria-hidden />
                          忽略候选
                        </Button>
                      </>
                    ) : null}
                    {selCat === "confirmed" ? (
                      <Button
                        size="xs"
                        variant="outline"
                        onClick={async () => {
                          if (await linkAction(() => topologyApi.rejectLink(selLink.id), "已改为忽略，重新扫描不会再提出"))
                            setSelectedEdge(null)
                        }}
                      >
                        <X aria-hidden />
                        改为忽略
                      </Button>
                    ) : null}
                    {selCat === "manual" ? (
                      <Button
                        size="xs"
                        variant="outline"
                        onClick={async () => {
                          if (await linkAction(() => topologyApi.deleteLink(selLink.id), "已删除人工连线")) setSelectedEdge(null)
                        }}
                      >
                        <Unlink aria-hidden />
                        删除人工连线
                      </Button>
                    ) : null}
                  </div>
                ) : null}
                {editable && selCat === "manual" ? (
                  <p className="text-xs text-muted-foreground">拖动连线两端的端点可以改接到其他设备。</p>
                ) : null}
                {editable ? <p className="text-xs text-muted-foreground">连线修改立即保存；撤销 / 重做只针对布局。</p> : null}
              </div>
            ) : selRelation ? (
              <div className="space-y-2">
                <Badge tone="neutral">资产关系</Badge>
                <p>
                  {nameOf(selRelation.source_asset_id)} {labelOf(RELATION_LABEL, selRelation.kind)} {nameOf(selRelation.target_asset_id)}
                </p>
                <p className="text-xs text-muted-foreground">
                  资产关系在<a className="text-link hover:underline" href={hrefFor("assets")}>资产管理</a>的详情里维护。
                </p>
              </div>
            ) : (
              <p className="text-muted-foreground">在画布或下方列表中选择设备或连线。</p>
            )}
          </section>

          {hiddenNodes.length ? (
            <section className={cn(panelClass, "p-4")}>
              <h2 className="mb-2 text-[0.8125rem] font-semibold">已移出视图</h2>
              <ul className="space-y-1">
                {hiddenNodes.map((id) => (
                  <li
                    key={id}
                    className={cn(
                      "flex items-center justify-between gap-2 rounded-md",
                      flashKind(id) && "outline-2 outline-dashed outline-warn",
                    )}
                  >
                    <span>
                      {nameOf(id)}
                      {flashKind(id) ? <span className="ml-1 text-xs">（{flashKind(id) === "undo" ? "已撤销" : "已重做"}）</span> : null}
                    </span>
                    {editable ? (
                      <Button size="xs" variant="ghost" onClick={() => restoreNode(id)}>
                        放回视图
                      </Button>
                    ) : null}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {editable ? <ManualLinkForm options={visibleNodes.map((n) => ({ id: n.id, name: n.data.asset.name }))} onAdd={addManual} /> : null}

          <section className={cn(panelClass, "p-4")}>
            <h2 className="mb-2 text-[0.8125rem] font-semibold">连线列表</h2>
            {edges.length ? (
              <ul className="max-h-64 space-y-0.5 overflow-y-auto">
                {edges.map((e) => {
                  const link = graph?.links.find((l) => l.id === e.id)
                  const cat: Category = link ? categoryOf(link)! : "relation"
                  return (
                    <li key={e.id}>
                      <button
                        type="button"
                        aria-pressed={selectedEdge === e.id}
                        onClick={() => {
                          setSelectedEdge(e.id)
                          setSelectedNode(null)
                        }}
                        className={cn(
                          "flex w-full items-center gap-2 rounded-md px-2 py-1 text-left hover:bg-accent",
                          selectedEdge === e.id && "bg-accent",
                        )}
                      >
                        <SvgLegendLine cat={cat} />
                        <span className="min-w-0 flex-1 truncate">
                          {nameOf(e.source)} → {nameOf(e.target)}
                        </span>
                        <span className="shrink-0 text-xs text-muted-foreground">{CAT_LABEL[cat]}</span>
                      </button>
                    </li>
                  )
                })}
              </ul>
            ) : (
              <p className="text-xs text-muted-foreground">还没有连线。{editable ? "可以重新扫描生成候选，或手动新增。" : ""}</p>
            )}
          </section>

          {rejected.length ? (
            <section className={cn(panelClass, "p-4")}>
              <h2 className="mb-2 text-[0.8125rem] font-semibold">已忽略 {rejected.length}</h2>
              <p className="mb-2 text-xs text-muted-foreground">重新扫描不会再提出这些链路。</p>
              <ul className="space-y-1">
                {rejected.map((l) => (
                  <li key={l.id} className="flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate">
                      {nameOf(l.source_asset_id)} → {nameOf(l.target_asset_id)}
                    </span>
                    {editable ? (
                      <Button size="xs" variant="ghost" onClick={() => linkAction(() => topologyApi.confirmLink(l.id), "已改为确认")}>
                        改为确认
                      </Button>
                    ) : null}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </aside>
      </div>

      <RescanDialog
        open={scanOpen}
        onOpenChange={setScanOpen}
        loading={scanLoading}
        preview={scanPreview}
        error={scanError}
        applying={scanApplying}
        onApply={applyScan}
        onRetry={openScan}
      />
    </div>
  )
}
