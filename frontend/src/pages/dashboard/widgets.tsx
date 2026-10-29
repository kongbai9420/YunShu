import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type Announcements,
  type DragEndEvent,
} from "@dnd-kit/core"
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable"
import { CSS } from "@dnd-kit/utilities"
import {
  CircleCheck,
  CircleDashed,
  CircleX,
  Clock,
  GripVertical,
  Hourglass,
  KeyRound,
  ShieldAlert,
  TriangleAlert,
  X,
  type LucideIcon,
} from "lucide-react"

import { Badge, MetricValue } from "@/components/status"
import { Button } from "@/components/ui/button"
import { QUALITY_LABEL } from "@/mock/labels"
import type { Quality } from "@/mock/types"
import { cn } from "@/lib/utils"

export type WidgetId = "health" | "issues" | "temps" | "power" | "quality" | "watch" | "vulns"

export const WIDGET_TITLE: Record<WidgetId, string> = {
  health: "资产采集状态",
  issues: "告警与采集问题",
  temps: "CPU 温度最高",
  power: "整体功耗（近 1 小时）",
  quality: "指标质量",
  watch: "关注列表",
  vulns: "漏洞",
}

export const WIDGET_IDS: WidgetId[] = ["health", "issues", "temps", "power", "quality", "watch", "vulns"]

// ---- 后端 /api/dashboard/summary 的结构 ----

interface MetricLite {
  value: number | null
  unit: string
  quality: Quality
}

export interface Summary {
  generated_at: string
  total_assets: number
  health: Record<string, number>
  issues: { asset_id: string; name: string; protocol: string; state: string; message: string; at: string }[]
  alerts: { id: string; name: string; title: string; severity: string; state: string; suppression_reason: string }[]
  temps: ({ asset_id: string; name: string } & MetricLite)[]
  power: { current_watts: number; reporting_assets: number; series: { t: number; watts: number; assets: number }[] }
  quality: Partial<Record<Quality, number>>
  watch: { asset_id: string; name: string; state: string; metrics: Record<string, MetricLite> }[]
  vulns?: { open: number; kev: number; critical: number; high: number; top: { id: string; title: string; kev: boolean; cvss: number | null; severity: string; assets: number }[] }
}

const STATE_META: Record<string, { label: string; tone: "ok" | "warn" | "bad" | "info" | "neutral"; icon: LucideIcon }> = {
  ok: { label: "采集正常", tone: "ok", icon: CircleCheck },
  degraded: { label: "部分失败", tone: "warn", icon: TriangleAlert },
  failed: { label: "采集失败", tone: "bad", icon: CircleX },
  untrusted: { label: "证书 / 主机密钥变化", tone: "bad", icon: ShieldAlert },
  unconfigured: { label: "待配置凭据", tone: "warn", icon: KeyRound },
  stale: { label: "数据过期", tone: "warn", icon: Clock },
  pending: { label: "等待首次采集", tone: "info", icon: Hourglass },
  unmonitored: { label: "未配置采集", tone: "neutral", icon: CircleDashed },
}
const STATE_ORDER = ["ok", "degraded", "failed", "untrusted", "unconfigured", "stale", "pending", "unmonitored"]

function StateBadge({ state }: { state: string }) {
  const m = STATE_META[state] ?? { label: state, tone: "neutral" as const, icon: CircleDashed }
  return (
    <Badge tone={m.tone} icon={m.icon}>
      {m.label}
    </Badge>
  )
}

function metric(m: MetricLite) {
  return { ...m, observedAt: null, source: "" }
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="py-2 text-[0.8125rem] text-muted-foreground">{children}</p>
}

function HealthWidget({ s }: { s: Summary }) {
  const states = STATE_ORDER.filter((k) => s.health[k])
  if (s.total_assets === 0) return <Empty>还没有资产。可以在“资产管理”里新增或导入。</Empty>
  return (
    <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3">
      {states.map((k) => (
        <li key={k} className="flex flex-col gap-1 rounded-lg bg-secondary px-3 py-2">
          <span className="tabular text-xl font-semibold">{s.health[k]}</span>
          <StateBadge state={k} />
        </li>
      ))}
    </ul>
  )
}

function IssuesWidget({ s }: { s: Summary }) {
  if (s.issues.length === 0 && !s.alerts?.length) return <Empty>当前没有活跃告警或近期采集失败。可在告警中心配置规则与通知。</Empty>
  return (
    <ul className="divide-y text-[0.8125rem]">
      {s.alerts?.map(a => <li key={a.id} className="space-y-1 py-2">
        <a className="font-medium text-primary hover:underline" href="#/alerts">{a.title}</a>
        <p className="text-xs text-muted-foreground">{a.severity === "critical" ? "严重" : a.severity === "warning" ? "警告" : "提示"} · {a.state === "silenced" ? "已静默" : a.state === "acknowledged" ? "已确认" : "告警中"}{a.suppression_reason ? ` · ${a.suppression_reason}` : ""}</p>
      </li>)}
      {s.issues.map((a) => (
        <li key={`${a.asset_id}-${a.protocol}`} className="flex items-start gap-2 py-2">
          <StateBadge state={a.state} />
          <div className="min-w-0 flex-1">
            <div className="truncate font-medium">
              {a.name} <span className="font-normal text-muted-foreground">· {a.protocol.toUpperCase()}</span>
            </div>
            <div className="text-muted-foreground">{a.message || "没有返回原因"}</div>
          </div>
          <time className="tabular shrink-0 text-xs text-muted-foreground">
            {new Date(a.at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}
          </time>
        </li>
      ))}
    </ul>
  )
}

function TempsWidget({ s }: { s: Summary }) {
  if (s.temps.length === 0) return <Empty>还没有可用的 CPU 温度读数。采集失败或不支持的设备不参与排行。</Empty>
  return (
    <table className="w-full text-[0.8125rem]">
      <caption className="sr-only">按 CPU 温度从高到低</caption>
      <tbody>
        {s.temps.map((t) => (
          <tr key={t.asset_id} className="border-b last:border-b-0">
            <th scope="row" className="truncate py-1.5 text-left font-normal">
              {t.name}
            </th>
            <td className="py-1.5 text-right">
              <MetricValue metric={metric(t)} compact />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function PowerWidget({ s }: { s: Summary }) {
  const series = s.power.series
  if (s.power.reporting_assets === 0) return <Empty>还没有设备报告功耗（需要 IPMI / Redfish 等能读到功耗的端点）。</Empty>
  const values = series.map((p) => p.watts)
  const max = Math.max(...values, s.power.current_watts)
  const min = Math.min(...values, s.power.current_watts)
  const w = 300
  const h = 60
  const pts = series
    .map((p, i) => {
      const x = series.length > 1 ? (i / (series.length - 1)) * w : w
      const y = h - ((p.watts - min) / (max - min || 1)) * (h - 8) - 4
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(" ")
  const partial = series.some((p) => p.assets < s.power.reporting_assets)
  return (
    <div className="flex h-full flex-col gap-2">
      <p className="text-[0.8125rem]">
        当前 <span className="tabular text-lg font-semibold">{s.power.current_watts} W</span>
        <span className="ml-2 text-muted-foreground">
          {s.power.reporting_assets} 台设备报告
          {series.length > 1 ? `，1 小时内 ${min}–${max} W` : ""}
        </span>
      </p>
      {series.length > 1 ? (
        <svg
          viewBox={`0 0 ${w} ${h}`}
          preserveAspectRatio="none"
          className="h-full min-h-10 w-full"
          role="img"
          aria-label={`近 1 小时功耗在 ${min} 到 ${max} 瓦之间`}
        >
          <polyline points={pts} fill="none" stroke="var(--status-info)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
        </svg>
      ) : (
        <Empty>历史不足一分钟，曲线稍后出现。</Empty>
      )}
      {partial ? <p className="text-xs text-muted-foreground">部分分钟有设备缺数据，曲线在这些点偏低。</p> : null}
    </div>
  )
}

function QualityWidget({ s }: { s: Summary }) {
  const order: Quality[] = ["valid", "estimated", "stale", "failed", "unsupported", "unconfigured"]
  const total = order.reduce((n, q) => n + (s.quality[q] ?? 0), 0)
  if (total === 0) return <Empty>还没有采集到指标。</Empty>
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-[0.8125rem] sm:grid-cols-3">
      {order.map((q) => (
        <div key={q} className="flex items-baseline justify-between gap-2 border-b py-1">
          <dt className="text-muted-foreground">{QUALITY_LABEL[q]}</dt>
          <dd className="tabular font-medium">{s.quality[q] ?? 0}</dd>
        </div>
      ))}
    </dl>
  )
}

const WATCH_METRICS: { key: string; label: string }[] = [
  { key: "cpu.temp", label: "CPU 温度" },
  { key: "cpu.load", label: "CPU" },
  { key: "power.watts", label: "功耗" },
]

function SortableRow({
  item,
  editing,
  onRemove,
}: {
  item: Summary["watch"][number] | { asset_id: string; name: string; state: string; metrics: Record<string, MetricLite> }
  editing: boolean
  onRemove: (id: string) => void
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: item.asset_id, disabled: !editing })
  return (
    <li
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={cn("flex flex-wrap items-center gap-2 border-b py-1.5 last:border-b-0", isDragging && "relative z-10 rounded-md bg-card shadow-md")}
    >
      {editing ? (
        <button
          type="button"
          className="inline-flex size-7 shrink-0 cursor-grab items-center justify-center rounded-md text-muted-foreground hover:bg-accent active:cursor-grabbing"
          aria-label={`调整「${item.name}」的顺序：按空格拿起，方向键移动，再按空格放下`}
          {...attributes}
          {...listeners}
        >
          <GripVertical aria-hidden className="size-4" />
        </button>
      ) : null}
      <span className="min-w-0 flex-1 truncate text-[0.8125rem] font-medium">{item.name}</span>
      <span className="flex flex-wrap items-center gap-2 text-xs">
        {WATCH_METRICS.filter((m) => item.metrics[m.key]).map((m) => (
          <span key={m.key} className="text-muted-foreground">
            {m.label} <MetricValue metric={metric(item.metrics[m.key])} compact />
          </span>
        ))}
      </span>
      <StateBadge state={item.state} />
      {editing ? (
        <Button size="icon-xs" variant="ghost" aria-label={`从关注列表移除「${item.name}」`} onClick={() => onRemove(item.asset_id)}>
          <X aria-hidden />
        </Button>
      ) : null}
    </li>
  )
}

/** 长条卡片内的行排序用 dnd-kit；网格库只管卡片之间的布局 */
function WatchWidget({
  s,
  order,
  editing,
  names,
  onReorder,
}: {
  s: Summary
  order: string[]
  editing: boolean
  names: Record<string, string>
  onReorder: (next: string[]) => void
}) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )
  const byId = new Map(s.watch.map((w) => [w.asset_id, w]))
  const rows = order.map((id) => byId.get(id) ?? { asset_id: id, name: names[id] ?? "（已删除的资产）", state: "pending", metrics: {} })
  const nameOf = (id: string | number) => rows.find((r) => r.asset_id === id)?.name ?? String(id)
  const posOf = (id: string | number) => order.indexOf(String(id)) + 1
  const announcements: Announcements = {
    onDragStart: ({ active }) => `已拿起「${nameOf(active.id)}」，当前第 ${posOf(active.id)} 位`,
    onDragOver: ({ active, over }) => (over ? `「${nameOf(active.id)}」移到第 ${posOf(over.id)} 位` : ""),
    onDragEnd: ({ active, over }) => (over ? `已放下「${nameOf(active.id)}」，位于第 ${posOf(over.id)} 位` : "已放下"),
    onDragCancel: ({ active }) => `已取消移动「${nameOf(active.id)}」`,
  }
  const onDragEnd = (e: DragEndEvent) => {
    const { active, over } = e
    if (!over || active.id === over.id) return
    onReorder(arrayMove(order, order.indexOf(String(active.id)), order.indexOf(String(over.id))))
  }
  if (order.length === 0) {
    return <Empty>{editing ? "用下方的“添加关注资产”选择要盯着的设备。" : "还没有关注的资产。编辑布局时可以添加。"}</Empty>
  }
  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      onDragEnd={onDragEnd}
      accessibility={{
        announcements,
        screenReaderInstructions: { draggable: "按空格拿起，方向键上下移动，再按空格放下，按 Esc 取消。" },
      }}
    >
      <SortableContext items={order} strategy={verticalListSortingStrategy}>
        <ul>
          {rows.map((r) => (
            <SortableRow key={r.asset_id} item={r} editing={editing} onRemove={(id) => onReorder(order.filter((x) => x !== id))} />
          ))}
        </ul>
      </SortableContext>
    </DndContext>
  )
}

export function WidgetBody({
  id,
  summary,
  watchOrder,
  watchNames,
  editing,
  onReorderWatch,
}: {
  id: WidgetId
  summary: Summary | null
  watchOrder: string[]
  watchNames: Record<string, string>
  editing: boolean
  onReorderWatch: (next: string[]) => void
}) {
  if (!summary) return <Empty>正在加载…</Empty>
  switch (id) {
    case "health":
      return <HealthWidget s={summary} />
    case "issues":
      return <IssuesWidget s={summary} />
    case "temps":
      return <TempsWidget s={summary} />
    case "power":
      return <PowerWidget s={summary} />
    case "quality":
      return <QualityWidget s={summary} />
    case "watch":
      return <WatchWidget s={summary} order={watchOrder} editing={editing} names={watchNames} onReorder={onReorderWatch} />
    case "vulns":
      return <VulnsWidget s={summary} />
  }
}


/** 4.3 P18e：本地命中、待处理的漏洞 */
function VulnsWidget({ s }: { s: Summary }) {
  const v = s.vulns
  if (!v || v.open === 0) return <Empty>当前没有命中本地资产的待处理漏洞。漏洞数据需要在“漏洞情报”里同步或导入离线包。</Empty>
  return (
    <div className="space-y-2 text-[0.8125rem]">
      <ul className="grid grid-cols-3 gap-2">
        {([["待处理", v.open, "neutral"], ["已被利用", v.kev, "bad"], ["严重", v.critical, "warn"]] as const).map(([label, n, tone]) => (
          <li key={label} className="flex flex-col gap-1 rounded-lg bg-secondary px-3 py-2">
            <span className={cn("tabular text-xl font-semibold", tone === "bad" && n ? "text-bad" : "")}>{n}</span>
            <span className="text-xs text-muted-foreground">{label}</span>
          </li>
        ))}
      </ul>
      <ul className="divide-y">
        {v.top.map((r) => (
          <li key={r.id} className="py-1.5">
            <a className="font-medium text-primary hover:underline" href={`#/vulns?id=${encodeURIComponent(r.id)}`}>{r.id}</a>{" "}
            <span className="text-muted-foreground">{r.title}</span>
            <span className="ml-1 text-xs">{r.kev ? "· 已被利用 " : ""}{r.cvss != null ? `· ${r.cvss} 分 ` : ""}· {r.assets} 台</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
