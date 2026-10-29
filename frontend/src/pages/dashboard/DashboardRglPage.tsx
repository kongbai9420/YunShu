import { useEffect, useMemo, useState } from "react"
import { Responsive, useContainerWidth, type Layout, type LayoutItem } from "react-grid-layout"
import { ArrowDown, ArrowUp, ChevronsLeftRight, GripVertical, Minus, Plus, RefreshCw } from "lucide-react"
import "react-grid-layout/css/styles.css"
import "react-resizable/css/styles.css"

import { PageHeader } from "@/components/AppShell"
import { Button } from "@/components/ui/button"
import { InlineNotice, panelClass } from "@/components/ui/controls"
import { api, query, type Asset, type Page } from "@/lib/api"
import { canEdit, useAuth } from "@/lib/auth"
import { cn } from "@/lib/utils"
import { LayoutToolbar, useRevisionedLayout } from "./useRevisionedLayout"
import { WIDGET_IDS, WIDGET_TITLE, WidgetBody, type Summary, type WidgetId } from "./widgets"

type Bp = "lg" | "md" | "sm"
type Item = { i: string; x: number; y: number; w: number; h: number; minW?: number; minH?: number }
interface DashboardData {
  layouts: Record<Bp, Item[]>
  watch: string[]
}

const LAYOUT_PATH = "/api/dashboard/layout"
const SUMMARY_MS = 30_000
const BREAKPOINTS: Record<Bp, number> = { lg: 1100, md: 640, sm: 0 }
const COLS: Record<Bp, number> = { lg: 12, md: 6, sm: 1 }
const BP_LABEL: Record<Bp, string> = { lg: "宽屏 12 列", md: "中等 6 列", sm: "窄屏 1 列" }
const MAX_WATCH = 50

const DEFAULTS: DashboardData = {
  layouts: {
    lg: [
      { i: "health", x: 0, y: 0, w: 4, h: 3, minW: 3, minH: 2 },
      { i: "issues", x: 4, y: 0, w: 4, h: 4, minW: 3, minH: 3 },
      { i: "watch", x: 8, y: 0, w: 4, h: 6, minW: 3, minH: 3 },
      { i: "temps", x: 0, y: 3, w: 4, h: 4, minW: 3, minH: 3 },
      { i: "power", x: 4, y: 4, w: 4, h: 3, minW: 3, minH: 2 },
      { i: "quality", x: 8, y: 6, w: 4, h: 2, minW: 3, minH: 2 },
      { i: "vulns", x: 0, y: 7, w: 4, h: 4, minW: 3, minH: 3 },
    ],
    md: [
      { i: "health", x: 0, y: 0, w: 3, h: 3, minW: 2, minH: 2 },
      { i: "issues", x: 3, y: 0, w: 3, h: 4, minW: 2, minH: 3 },
      { i: "watch", x: 0, y: 3, w: 3, h: 6, minW: 2, minH: 3 },
      { i: "temps", x: 3, y: 4, w: 3, h: 4, minW: 2, minH: 3 },
      { i: "power", x: 0, y: 9, w: 6, h: 3, minW: 2, minH: 2 },
      { i: "quality", x: 3, y: 8, w: 3, h: 2, minW: 2, minH: 2 },
      { i: "vulns", x: 0, y: 12, w: 6, h: 4, minW: 2, minH: 3 },
    ],
    sm: WIDGET_IDS.map((id, idx) => ({ i: id, x: 0, y: idx * 4, w: 1, h: id === "watch" ? 6 : 4, minH: 2 })),
  },
  watch: [],
}

function sortByPosition(items: Item[]) {
  return [...items].sort((a, b) => a.y - b.y || a.x - b.x)
}

function toItems(layout: Layout): Item[] {
  return layout.map((l: LayoutItem) => {
    const it: Item = { i: l.i, x: l.x, y: l.y, w: l.w, h: l.h }
    if (l.minW !== undefined) it.minW = l.minW
    if (l.minH !== undefined) it.minH = l.minH
    return it
  })
}

/** 编辑态下往关注列表里加资产：按名称 / 编号 / 地址搜索 */
function WatchPicker({ watch, onAdd }: { watch: string[]; onAdd: (a: Asset) => void }) {
  const [q, setQ] = useState("")
  const [results, setResults] = useState<Asset[]>([])
  useEffect(() => {
    if (!q.trim()) {
      setResults([])
      return
    }
    const t = window.setTimeout(() => {
      api
        .get<Page<Asset>>(`/api/assets${query({ q: q.trim(), limit: 8 })}`)
        .then((p) => setResults(p.items))
        .catch(() => setResults([]))
    }, 250)
    return () => window.clearTimeout(t)
  }, [q])
  const full = watch.length >= MAX_WATCH
  return (
    <div className="mt-3 rounded-lg border p-3">
      <label htmlFor="watch-search" className="text-xs font-medium text-muted-foreground">
        添加关注资产{full ? `（已达 ${MAX_WATCH} 台上限）` : ""}
      </label>
      <input
        id="watch-search" data-help-plain=""
        type="search"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        disabled={full}
        placeholder="名称、编号或地址"
        className="mt-1 h-8 w-full max-w-sm rounded-md border border-input bg-card px-2 text-[0.8125rem] placeholder:text-muted-foreground"
      />
      {results.length ? (
        <ul className="mt-2 space-y-1">
          {results.map((a) => {
            const added = watch.includes(a.id)
            return (
              <li key={a.id} className="flex items-center gap-2 text-[0.8125rem]">
                <span className="min-w-0 flex-1 truncate">
                  {a.name} <span className="text-xs text-muted-foreground">{a.asset_no ?? ""}</span>
                </span>
                <Button size="xs" variant="outline" disabled={added || full} onClick={() => onAdd(a)}>
                  {added ? "已在列表中" : "加入"}
                </Button>
              </li>
            )
          })}
        </ul>
      ) : null}
    </div>
  )
}

export function DashboardRglPage({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void } = {}) {
  const { user } = useAuth()
  const editable = canEdit(user)
  const state = useRevisionedLayout<DashboardData>(LAYOUT_PATH, DEFAULTS)
  const { editing } = state
  useEffect(() => {
    onDirtyChange?.(state.editing && state.dirty)
    return () => onDirtyChange?.(false)
  }, [onDirtyChange, state.editing, state.dirty])
  const [draft, setDraft] = useState<DashboardData>(DEFAULTS)
  const [announce, setAnnounce] = useState("")
  const [summary, setSummary] = useState<Summary | null>(null)
  const [summaryError, setSummaryError] = useState<string | null>(null)
  const [watchNames, setWatchNames] = useState<Record<string, string>>({})
  const [refreshKey, setRefreshKey] = useState(0)
  // 先量出容器宽度再渲染网格：否则会先按默认 1280 宽排一次，再动画挪到实际断点，首屏卡片乱飞并出现横向滚动条
  const { width, containerRef, mounted } = useContainerWidth({ measureBeforeMount: true })
  // 与 react-grid-layout 相同的断点规则（宽度大于断点值才进入该断点）
  const bp: Bp = width > BREAKPOINTS.lg ? "lg" : width > BREAKPOINTS.md ? "md" : "sm"

  // 查看态始终展示已保存的修订；编辑态展示草稿
  const data = editing ? draft : state.data

  useEffect(() => {
    let cancelled = false
    const load = () =>
      api
        .get<Summary>("/api/dashboard/summary")
        .then((s) => {
          if (cancelled) return
          setSummary(s)
          setSummaryError(null)
          setWatchNames((n) => ({ ...n, ...Object.fromEntries(s.watch.map((w) => [w.asset_id, w.name])) }))
        })
        .catch((e: Error) => !cancelled && setSummaryError(e.message))
    void load()
    const t = window.setInterval(() => !document.hidden && void load(), SUMMARY_MS)
    return () => {
      cancelled = true
      window.clearInterval(t)
    }
  }, [refreshKey, state.record?.revision])

  const change = (next: DashboardData) => {
    setDraft(next)
    state.noteDraft(next)
  }

  const startEdit = async () => {
    const latest = await state.startEdit()
    setDraft(structuredClone(latest))
  }

  const recover = () => {
    const d = state.recover()
    if (d) setDraft(structuredClone(d))
  }

  const updateItems = (fn: (items: Item[]) => Item[]) => change({ ...draft, layouts: { ...draft.layouts, [bp]: fn(draft.layouts[bp]) } })

  const move = (id: string, dir: -1 | 1) => {
    const ordered = sortByPosition(draft.layouts[bp])
    const idx = ordered.findIndex((it) => it.i === id)
    const other = ordered[idx + dir]
    if (!other) return
    const self = ordered[idx]
    updateItems((items) =>
      items.map((it) =>
        it.i === self.i ? { ...it, x: other.x, y: other.y } : it.i === other.i ? { ...it, x: self.x, y: self.y } : it,
      ),
    )
    setAnnounce(`已将「${WIDGET_TITLE[id as WidgetId]}」${dir < 0 ? "上移" : "下移"}一位`)
  }

  const resize = (id: string, delta: number) => {
    const cols = COLS[bp]
    updateItems((items) =>
      items.map((it) => {
        if (it.i !== id) return it
        const w = Math.max(it.minW ?? 1, Math.min(cols, it.w + delta))
        return { ...it, w, x: Math.min(it.x, cols - w) }
      }),
    )
    setAnnounce(`已${delta > 0 ? "加宽" : "收窄"}「${WIDGET_TITLE[id as WidgetId]}」`)
  }

  const order = useMemo(() => sortByPosition(data.layouts[bp]).map((it) => it.i as WidgetId), [data, bp])

  return (
    <div className="pb-8">
      <PageHeader
        title="总览"
        description="小组件的数据来自真实的采集状态与指标，每 30 秒刷新；失败、过期或不支持的值不显示成 0。布局所有人共用，按宽屏、中等、窄屏分别保存并带修订号；只读账号不能修改布局。"
        actions={
          <Button size="sm" variant="outline" onClick={() => setRefreshKey((k) => k + 1)}>
            <RefreshCw aria-hidden />
            刷新数据
          </Button>
        }
      />
      <LayoutToolbar
        state={state}
        canEdit={editable}
        onStartEdit={() => void startEdit()}
        onSave={() => void state.save(draft)}
        onOverwrite={() => void state.overwrite(draft)}
        onResetDefault={() => {
          change({ ...structuredClone(DEFAULTS), watch: draft.watch })
          setAnnounce("已恢复默认布局（尚未保存，关注列表保留）")
        }}
        onRecover={recover}
      />
      <div className="px-6">
        {summaryError ? (
          <InlineNotice tone="bad" role="alert" className="mb-3">
            小组件数据加载失败：{summaryError}
          </InlineNotice>
        ) : null}
        <p className="mb-2 text-xs text-muted-foreground">
          当前断点：{BP_LABEL[bp]}
          {editing ? " · 只修改当前断点的布局" : ""}
        </p>
        <p className="sr-only" aria-live="polite">
          {announce}
        </p>
        <div ref={containerRef as React.RefObject<HTMLDivElement>}>
          {mounted ? (
            <Responsive
              width={width}
              breakpoints={BREAKPOINTS}
              cols={COLS}
              layouts={data.layouts}
              rowHeight={64}
              margin={[12, 12]}
              containerPadding={[0, 0]}
              dragConfig={{ enabled: editing, handle: ".rgl-drag-handle", cancel: "button" }}
              resizeConfig={{ enabled: editing, handles: ["se"] }}
              onLayoutChange={(layout) => {
                if (!editing) return
                const next = toItems(layout)
                if (JSON.stringify(next) !== JSON.stringify(draft.layouts[bp])) updateItems(() => next)
              }}
            >
              {WIDGET_IDS.map((id) => {
                const pos = order.indexOf(id)
                return (
                  <div key={id} className="flex">
                    <section
                      aria-label={WIDGET_TITLE[id]}
                      className={cn(
                        panelClass,
                        "flex min-h-0 w-full flex-col overflow-hidden",
                        editing && "ring-1 ring-ring/40",
                      )}
                    >
                      <header
                        className={cn(
                          "flex items-center gap-1 px-3 pt-2.5 pb-1.5",
                          editing && "rgl-drag-handle cursor-move",
                        )}
                      >
                        {editing ? <GripVertical aria-hidden className="size-4 shrink-0 text-muted-foreground" /> : null}
                        <h2 className="min-w-0 flex-1 truncate text-[0.8125rem] font-semibold">{WIDGET_TITLE[id]}</h2>
                        {editing ? (
                          <div className="flex shrink-0 items-center">
                            <Button
                              size="icon-xs"
                              variant="ghost"
                              aria-label={`上移「${WIDGET_TITLE[id]}」`}
                              disabled={pos <= 0}
                              onClick={() => move(id, -1)}
                            >
                              <ArrowUp aria-hidden />
                            </Button>
                            <Button
                              size="icon-xs"
                              variant="ghost"
                              aria-label={`下移「${WIDGET_TITLE[id]}」`}
                              disabled={pos >= order.length - 1}
                              onClick={() => move(id, 1)}
                            >
                              <ArrowDown aria-hidden />
                            </Button>
                            {COLS[bp] > 1 ? (
                              <>
                                <Button size="icon-xs" variant="ghost" aria-label={`收窄「${WIDGET_TITLE[id]}」`} onClick={() => resize(id, -1)}>
                                  <Minus aria-hidden />
                                </Button>
                                <Button size="icon-xs" variant="ghost" aria-label={`加宽「${WIDGET_TITLE[id]}」`} onClick={() => resize(id, 1)}>
                                  <Plus aria-hidden />
                                </Button>
                              </>
                            ) : null}
                          </div>
                        ) : null}
                      </header>
                      <div className="min-h-0 flex-1 overflow-auto px-3 pb-3">
                        <WidgetBody
                          id={id}
                          summary={summary}
                          editing={editing}
                          watchOrder={data.watch}
                          watchNames={watchNames}
                          onReorderWatch={(next) => change({ ...draft, watch: next })}
                        />
                      </div>
                    </section>
                  </div>
                )
              })}
            </Responsive>
          ) : null}
        </div>
        {editing ? (
          <>
            <WatchPicker
              watch={draft.watch}
              onAdd={(a) => {
                setWatchNames((n) => ({ ...n, [a.id]: a.name }))
                change({ ...draft, watch: [...draft.watch, a.id] })
                setAnnounce(`已把「${a.name}」加入关注列表（尚未保存）`)
              }}
            />
            <p className="mt-3 flex items-center gap-1.5 text-xs text-muted-foreground">
              <ChevronsLeftRight aria-hidden className="size-3.5" />
              键盘操作：Tab 到卡片上的按钮可上移、下移、收窄、加宽；关注列表按空格拿起行，方向键移动。离开页面前没保存的修改会暂存在本浏览器。
            </p>
          </>
        ) : null}
      </div>
    </div>
  )
}
