import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  CircleCheck,
  CircleDashed,
  CircleX,
  Download,
  FileUp,
  KeyRound,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  TriangleAlert,
} from "lucide-react"

import { PageHeader } from "@/components/AppShell"
import { LiveIndicator } from "@/components/LiveIndicator"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice, Segmented, Select, panelClass } from "@/components/ui/controls"
import { api, query, type Asset, type Page, type Site } from "@/lib/api"
import { canEdit, useAuth } from "@/lib/auth"
import { type LiveCollectionItem, useLiveEvents } from "@/lib/live"
import { dateText } from "@/lib/monitor"
import { FORM_LABEL, LIFECYCLE_LABEL, PROTOCOL_LABEL, ROLE_LABEL, labelOf, optionsFrom } from "@/lib/vocab"
import { cn } from "@/lib/utils"
import { AssetDetailDrawer } from "./AssetDetailDrawer"
import { CreateAssetDialog } from "./CreateAssetDialog"
import { RecycleBinPanel } from "./RecycleBinPanel"

const ALL = ""
const PAGE_SIZE = 100

/** 四类入口只是筛选预设，不限制底层分类 */
const ENTRIES = {
  all: { label: "全部资产", params: {} },
  server: { label: "服务器", params: { role: "server", form: "physical" } },
  network: { label: "网络设备", params: { role: "switch,router,ap,firewall" } },
  virtual: { label: "虚拟化", params: { form: "vm,container" } },
  other: { label: "其他", params: { role: "nas,storage,ups,other" } },
} as const
type Entry = keyof typeof ENTRIES

/** 后端 GET /api/assets 的 sort 白名单 */
type SortKey = "name" | "asset_no" | "form" | "role" | "platform" | "vendor" | "lifecycle" | "updated_at"
type Order = "asc" | "desc"
const SORT_LABEL: Record<SortKey, string> = {
  name: "名称",
  asset_no: "资产编号",
  form: "形态",
  role: "角色",
  platform: "平台",
  vendor: "品牌",
  lifecycle: "生命周期",
  updated_at: "最近更新",
}

type ColumnId = "name" | "form" | "role" | "platform" | "vendor" | "endpoints" | "collection" | "site" | "lifecycle"
type Column = { id: ColumnId; label: string; sort?: SortKey; width: number }
const COLUMNS: Column[] = [
  { id: "name", label: "名称", sort: "name", width: 220 },
  { id: "form", label: "形态", sort: "form", width: 96 },
  { id: "role", label: "角色", sort: "role", width: 96 },
  { id: "platform", label: "平台", sort: "platform", width: 120 },
  { id: "vendor", label: "品牌 / 型号", sort: "vendor", width: 160 },
  { id: "endpoints", label: "管理端点", width: 150 },
  { id: "collection", label: "采集", width: 136 },
  { id: "site", label: "场所", width: 120 },
  { id: "lifecycle", label: "状态", sort: "lifecycle", width: 150 },
]
const MIN_WIDTH = 64
const MAX_WIDTH = 640
const WIDTH_KEY = "yunshu.assets.columnWidths.v1"
type Widths = Record<ColumnId, number>
const DEFAULT_WIDTHS = Object.fromEntries(COLUMNS.map((c) => [c.id, c.width])) as Widths
const clampWidth = (w: number) => Math.round(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, w)))

/** 列宽按浏览器记住；读写 localStorage 失败（隐私模式、禁用存储）时用默认值 */
function useColumnWidths() {
  const [widths, setWidths] = useState<Widths>(() => {
    const out = { ...DEFAULT_WIDTHS }
    try {
      const saved = JSON.parse(window.localStorage.getItem(WIDTH_KEY) ?? "{}") as Record<string, unknown>
      for (const c of COLUMNS) {
        const w = saved[c.id]
        if (typeof w === "number" && Number.isFinite(w)) out[c.id] = clampWidth(w)
      }
    } catch {
      // 读不到就用默认列宽
    }
    return out
  })
  useEffect(() => {
    const t = window.setTimeout(() => {
      try {
        window.localStorage.setItem(WIDTH_KEY, JSON.stringify(widths))
      } catch {
        // 存不下就只在本次打开期间有效
      }
    }, 200)
    return () => window.clearTimeout(t)
  }, [widths])
  const setWidth = useCallback((id: ColumnId, w: number) => setWidths((prev) => ({ ...prev, [id]: clampWidth(w) })), [])
  const reset = useCallback(() => setWidths({ ...DEFAULT_WIDTHS }), [])
  const customized = COLUMNS.some((c) => widths[c.id] !== c.width)
  return { widths, setWidth, reset, customized }
}

/** 列宽拖动条：鼠标 / 触控拖动；键盘左右方向键每次 16 像素（按住 Shift 64 像素），Home 或双击恢复默认 */
function ResizeHandle({ column, width, onResize }: { column: Column; width: number; onResize: (w: number) => void }) {
  const drag = useRef<{ x: number; w: number } | null>(null)
  return (
    <span
      role="separator"
      aria-orientation="vertical"
      aria-label={`调整“${column.label}”列宽`}
      aria-valuenow={width}
      aria-valuemin={MIN_WIDTH}
      aria-valuemax={MAX_WIDTH}
      aria-valuetext={`${width} 像素`}
      tabIndex={0}
      title="拖动调整列宽，双击恢复默认"
      className="absolute top-0 right-0 z-10 flex h-full w-3 cursor-col-resize touch-none justify-end after:h-full after:w-px after:bg-border hover:after:w-0.5 hover:after:bg-ring focus-visible:after:w-0.5 focus-visible:after:bg-ring"
      onClick={(e) => e.stopPropagation()}
      onPointerDown={(e) => {
        e.preventDefault()
        e.currentTarget.setPointerCapture(e.pointerId)
        drag.current = { x: e.clientX, w: width }
      }}
      onPointerMove={(e) => {
        if (drag.current) onResize(drag.current.w + e.clientX - drag.current.x)
      }}
      onPointerUp={() => {
        drag.current = null
      }}
      onPointerCancel={() => {
        drag.current = null
      }}
      onDoubleClick={() => onResize(column.width)}
      onKeyDown={(e) => {
        const step = e.shiftKey ? 64 : 16
        if (e.key === "ArrowLeft") onResize(width - step)
        else if (e.key === "ArrowRight") onResize(width + step)
        else if (e.key === "Home") onResize(column.width)
        else return
        e.preventDefault()
      }}
    />
  )
}

/** 资产的采集概况（来自实时推送的端点状态），图标 + 文字 */
function CollectionCell({
  items,
  ready,
  endpoints,
}: {
  items?: Map<string, LiveCollectionItem>
  ready: boolean
  endpoints: number
}) {
  if (!items?.size) {
    return (
      <span className="text-xs text-muted-foreground">
        {!ready ? "读取中…" : endpoints ? "尚未采集" : "无管理端点"}
      </span>
    )
  }
  const all = [...items.values()]
  const ok = all.filter((i) => i.state === "ok").length
  const bad = all.filter((i) => i.state === "failed" || i.state === "untrusted")
  // 缺凭据时没有连设备，不算采集失败，提示去配置凭据
  const unconfigured = all.some((i) => i.state === "unconfigured")
  const last = all.reduce((a, b) => (a.last_attempt_at > b.last_attempt_at ? a : b))
  const [tone, Icon, text] =
    ok && !bad.length && !unconfigured
      ? (["ok", CircleCheck, "采集正常"] as const)
      : ok && bad.length
        ? (["warn", TriangleAlert, "部分失败"] as const)
        : bad.length
          ? (["bad", CircleX, bad.some((i) => i.state === "untrusted") ? "指纹不匹配" : "采集失败"] as const)
          : unconfigured
            ? (["warn", KeyRound, ok ? "部分待配置凭据" : "待配置凭据"] as const)
            : (["neutral", CircleDashed, "等待采集"] as const)
  return (
    <span title={`最近尝试：${dateText(last.last_attempt_at)}`}>
      <Badge tone={tone} icon={Icon}>
        {text}
      </Badge>
    </span>
  )
}

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms)
    return () => window.clearTimeout(t)
  }, [value, ms])
  return v
}

export function AssetsPage() {
  const { user } = useAuth()
  const editable = canEdit(user)
  const [entry, setEntry] = useState<Entry>("all")
  const [form, setForm] = useState(ALL)
  const [role, setRole] = useState(ALL)
  const [lifecycle, setLifecycle] = useState(ALL)
  const [siteId, setSiteId] = useState(ALL)
  const [tag, setTag] = useState("")
  const [text, setText] = useState("")
  const q = useDebounced(text.trim(), 250)
  const tagQ = useDebounced(tag.trim(), 250)
  const [offset, setOffset] = useState(0)
  const [sort, setSort] = useState<SortKey>("name")
  const [order, setOrder] = useState<Order>("asc")
  const { widths, setWidth, reset: resetWidths, customized } = useColumnWidths()
  const tableWidth = COLUMNS.reduce((n, c) => n + widths[c.id], 0)

  // 采集状态由实时推送更新：新连接先收到全部端点状态，之后只收变化；只更新这一列，不触碰任何表单
  const [collection, setCollection] = useState<Map<string, Map<string, LiveCollectionItem>>>(() => new Map())
  const [snapshotReady, setSnapshotReady] = useState(false)
  const live = useLiveEvents({
    collection: (e) => {
      if (e.items.length)
        setCollection((prev) => {
          const next = new Map(prev)
          for (const item of e.items) {
            const eps = new Map(next.get(item.asset_id))
            eps.set(item.endpoint_id, item)
            next.set(item.asset_id, eps)
          }
          return next
        })
      if (e.snapshot && e.final !== false) setSnapshotReady(true)
    },
  })

  const [page, setPage] = useState<Page<Asset> | null>(null)
  const [sites, setSites] = useState<Site[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  // 其他页面用 #/assets?asset=<id> 直接打开某台资产的详情（机柜、定位、巡检报告）
  const [openId, setOpenId] = useState<string | null>(() => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("asset"))
  // 已经在资产页时只改链接里的 ?asset= 也要打开对应资产（页面不会重新挂载，只有 hashchange）
  useEffect(() => {
    const onHash = () => {
      const id = new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("asset")
      if (id) setOpenId(id)
    }
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [])
  const [creating, setCreating] = useState(false)
  // 回收站与资产列表二选一显示（P22）；只读账号看不到回收站
  const [binOpen, setBinOpen] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)

  const params = useMemo(() => {
    const preset: Record<string, string> = { ...ENTRIES[entry].params }
    if (form) preset.form = form
    if (role) preset.role = role
    return { ...preset, lifecycle, site_id: siteId, tag: tagQ, q, sort, order, limit: PAGE_SIZE, offset }
  }, [entry, form, role, lifecycle, siteId, tagQ, q, sort, order, offset])

  useEffect(() => setOffset(0), [entry, form, role, lifecycle, siteId, tagQ, q, sort, order])
  const toggleSort = (key: SortKey) => {
    if (key === sort) setOrder((o) => (o === "asc" ? "desc" : "asc"))
    else {
      setSort(key)
      setOrder("asc")
    }
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api
      .get<Page<Asset>>(`/api/assets${query(params)}`)
      .then((p) => {
        if (!cancelled) {
          setPage(p)
          setError(null)
        }
      })
      .catch((e: Error) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [params, reloadKey])

  useEffect(() => {
    api.get<Site[]>("/api/sites").then(setSites).catch(() => setSites([]))
  }, [reloadKey])

  const reload = useCallback(() => setReloadKey((k) => k + 1), [])
  const siteName = useMemo(() => new Map(sites.map((s) => [s.id, s.name])), [sites])
  const anyFilter = entry !== "all" || form || role || lifecycle || siteId || tag || text
  const clear = () => {
    setEntry("all")
    setForm(ALL)
    setRole(ALL)
    setLifecycle(ALL)
    setSiteId(ALL)
    setTag("")
    setText("")
  }
  const total = page?.total ?? 0

  return (
    <div className="pb-8">
      <PageHeader
        title="资产管理"
        description="形态、角色、平台三个维度互相独立；上方入口只是筛选预设。同一管理地址可以属于多台资产，资产以资产编号区分。"
        actions={
          <>
            <LiveIndicator status={live.status} fallbackText="采集列显示最后收到的状态" />
            <Button variant="outline" size="sm" onClick={reload} disabled={loading}>
              <RefreshCw aria-hidden />
              刷新
            </Button>
            {editable ? (
              <Button variant={binOpen ? "secondary" : "outline"} size="sm" aria-pressed={binOpen} onClick={() => setBinOpen((v) => !v)}>
                <Trash2 aria-hidden />
                {binOpen ? "返回资产列表" : "回收站"}
              </Button>
            ) : null}
            {editable ? (
              <Button size="sm" onClick={() => setCreating(true)}>
                <Plus aria-hidden />
                新增资产
              </Button>
            ) : null}
          </>
        }
      />

      {binOpen && editable ? (
        <div className="px-6">
          <RecycleBinPanel admin={user?.role === "admin"} onRestored={reload} />
        </div>
      ) : null}
      {/* 打开回收站时只隐藏列表，筛选条件保留 */}
      <div className={cn("space-y-3 px-6", binOpen && editable && "hidden")}>
        <Segmented
          label="资产入口"
          value={entry}
          onChange={setEntry}
          options={(Object.keys(ENTRIES) as Entry[]).map((k) => ({ value: k, label: ENTRIES[k].label }))}
        />

        <div className="flex flex-wrap items-end gap-3">
          <div className="flex min-w-0 max-w-full flex-col gap-1">
            <label htmlFor="asset-search" className="text-xs font-medium text-muted-foreground">
              搜索
            </label>
            <div className="relative max-w-full">
              <Search aria-hidden className="pointer-events-none absolute top-2 left-2 size-4 text-muted-foreground" />
              <input
                id="asset-search"
                type="search"
                data-help-plain=""
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder="名称、编号、型号、序列号或地址"
                className="h-8 w-64 max-w-full rounded-md border border-input bg-card pr-2 pl-8 text-[0.8125rem] placeholder:text-muted-foreground"
              />
            </div>
          </div>
          <Select id="f-form" label="形态" help="asset.form" value={form} onChange={setForm} options={[{ value: ALL, label: "全部" }, ...optionsFrom(FORM_LABEL)]} />
          <Select id="f-role" label="角色" help="asset.role" value={role} onChange={setRole} options={[{ value: ALL, label: "全部" }, ...optionsFrom(ROLE_LABEL)]} />
          <Select
            id="f-life"
            label="生命周期"
            help="asset.lifecycle"
            value={lifecycle}
            onChange={setLifecycle}
            options={[{ value: ALL, label: "全部" }, ...optionsFrom(LIFECYCLE_LABEL)]}
          />
          <Select
            id="f-site"
            label="场所"
            plain
            value={siteId}
            onChange={setSiteId}
            options={[{ value: ALL, label: "全部" }, ...sites.map((s) => ({ value: s.id, label: s.name }))]}
          />
          <div className="flex flex-col gap-1">
            <label htmlFor="f-tag" className="text-xs font-medium text-muted-foreground">
              标签
            </label>
            <input
              id="f-tag"
              data-help-plain=""
              title="按完整标签名精确匹配"
              value={tag}
              onChange={(e) => setTag(e.target.value)}
              placeholder="完整标签名"
              className="h-8 w-32 rounded-md border border-input bg-card px-2 text-[0.8125rem] placeholder:text-muted-foreground"
            />
          </div>
          <Select
            id="f-sort"
            label="排序"
            plain
            value={sort}
            onChange={(v) => {
              setSort(v as SortKey)
              setOrder("asc")
            }}
            options={(Object.keys(SORT_LABEL) as SortKey[]).map((k) => ({ value: k, label: SORT_LABEL[k] }))}
          />
          <Button
            variant="outline"
            size="sm"
            onClick={() => setOrder((o) => (o === "asc" ? "desc" : "asc"))}
            aria-label={`排序方向：${order === "asc" ? "升序" : "降序"}，按下切换`}
          >
            {order === "asc" ? <ArrowUp aria-hidden /> : <ArrowDown aria-hidden />}
            {order === "asc" ? "升序" : "降序"}
          </Button>
          {anyFilter ? (
            <Button variant="ghost" size="sm" onClick={clear}>
              清除筛选
            </Button>
          ) : null}
          {customized ? (
            <Button variant="ghost" size="sm" onClick={resetWidths}>
              恢复默认列宽
            </Button>
          ) : null}
        </div>

        {error ? (
          <InlineNotice tone="bad" role="alert">
            {error}
          </InlineNotice>
        ) : null}

        <p className="text-xs text-muted-foreground" aria-live="polite">
          {loading && !page ? "正在加载…" : `共 ${total} 项资产`}
          {total > PAGE_SIZE ? `，当前第 ${offset + 1}–${Math.min(offset + PAGE_SIZE, total)} 项` : ""}
          <span className="ml-2">
            “采集”列随实时推送更新，打开详情的“指标与采集”查看各项指标；点击列头排序，拖动列头右侧分隔线调整列宽。
          </span>
        </p>

        <div className={cn(panelClass, "overflow-x-auto")}>
          <table
            className="data-table min-w-full table-fixed whitespace-nowrap [&_tbody_td]:truncate"
            style={{ width: tableWidth }}
          >
            <caption className="sr-only">
              资产列表，选择名称查看详情；当前按{SORT_LABEL[sort]}
              {order === "asc" ? "升序" : "降序"}排列
            </caption>
            <colgroup>
              {COLUMNS.map((c) => (
                <col key={c.id} style={{ width: widths[c.id] }} />
              ))}
            </colgroup>
            <thead>
              <tr>
                {COLUMNS.map((c) => {
                  const sortKey = c.sort
                  const active = sortKey === sort
                  const Icon = !active ? ArrowUpDown : order === "asc" ? ArrowUp : ArrowDown
                  return (
                    <th
                      key={c.id}
                      scope="col"
                      aria-sort={sortKey ? (active ? (order === "asc" ? "ascending" : "descending") : "none") : undefined}
                      className="relative overflow-hidden"
                    >
                      {sortKey ? (
                        <button
                          type="button"
                          onClick={() => toggleSort(sortKey)}
                          title={`按${SORT_LABEL[sortKey]}排序`}
                          className={`inline-flex max-w-full items-center gap-1 rounded-sm hover:text-foreground ${active ? "text-foreground" : ""}`}
                        >
                          <span className="truncate">{c.label}</span>
                          <Icon aria-hidden className={`size-3.5 shrink-0 ${active ? "" : "opacity-60"}`} />
                        </button>
                      ) : (
                        <span className="block truncate">{c.label}</span>
                      )}
                      <ResizeHandle column={c} width={widths[c.id]} onResize={(w) => setWidth(c.id, w)} />
                    </th>
                  )
                })}
              </tr>
            </thead>
            <tbody>
              {page?.items.map((a) => (
                <tr key={a.id} className="cursor-pointer" onClick={() => setOpenId(a.id)}>
                  <th scope="row" className="truncate font-normal">
                    <button
                      type="button"
                      className="max-w-full truncate text-left font-medium text-foreground hover:text-link hover:underline"
                      onClick={(e) => {
                        e.stopPropagation()
                        setOpenId(a.id)
                      }}
                    >
                      {a.name}
                    </button>
                    <div className="truncate text-xs text-muted-foreground">{a.asset_no ?? "无资产编号"}</div>
                  </th>
                  <td>{labelOf(FORM_LABEL, a.form)}</td>
                  <td>{labelOf(ROLE_LABEL, a.role)}</td>
                  <td>{a.platform || <span className="text-muted-foreground">未填写</span>}</td>
                  <td>{[a.vendor, a.model].filter(Boolean).join(" ") || <span className="text-muted-foreground">未填写</span>}</td>
                  <td>
                    <span className="tabular">{a.endpoints.length}</span>
                    <span className="ml-1 text-xs text-muted-foreground">
                      {Array.from(new Set(a.endpoints.map((e) => labelOf(PROTOCOL_LABEL, e.protocol)))).join(" / ")}
                    </span>
                  </td>
                  <td>
                    <CollectionCell items={collection.get(a.id)} ready={snapshotReady} endpoints={a.endpoints.length} />
                  </td>
                  <td>{a.site_id ? siteName.get(a.site_id) ?? "—" : <span className="text-muted-foreground">未指定</span>}</td>
                  <td>
                    <Badge tone={a.lifecycle === "active" ? "ok" : a.lifecycle === "retired" ? "neutral" : "info"}>
                      {labelOf(LIFECYCLE_LABEL, a.lifecycle)}
                    </Badge>
                    {a.legacy_source ? <span className="ml-1.5 text-xs text-muted-foreground">旧版导入</span> : null}
                  </td>
                </tr>
              ))}
              {page && page.items.length === 0 ? (
                <tr>
                  <td colSpan={COLUMNS.length} className="py-10 text-center text-muted-foreground">
                    {anyFilter ? (
                      <>
                        没有符合条件的资产。
                        <button type="button" className="ml-1 text-link hover:underline" onClick={clear}>
                          清除筛选
                        </button>
                      </>
                    ) : (
                      <div className="mx-auto max-w-lg space-y-3 text-left whitespace-normal">
                        <p className="text-center text-[0.9375rem] font-medium text-foreground">还没有资产</p>
                        <p>新平台的资产存在自己的数据库里，不会自动读取旧版界面里的设备。可以用下面任一种方式添加：</p>
                        <ul className="list-disc space-y-1.5 pl-5">
                          <li>
                            <span className="font-medium text-foreground">导入旧版数据</span>：在 <code>backend</code> 目录运行
                            <code className="mx-1 rounded bg-secondary px-1 py-0.5 text-xs">python -m app.legacy_import --config ..\config.ini</code>
                            先预检，确认后加 <code className="rounded bg-secondary px-1 py-0.5 text-xs">--apply</code>。旧文件只读，不会被修改。
                          </li>
                          <li>
                            <span className="font-medium text-foreground">批量导入</span>：下载模板填好后上传，先预览再提交。
                          </li>
                          <li>
                            <span className="font-medium text-foreground">逐台新增</span>：手动填写一台资产。
                          </li>
                        </ul>
                        {editable ? (
                          <div className="flex flex-wrap justify-center gap-2 pt-1">
                            <Button asChild size="sm" variant="outline">
                              <a href="/api/import/templates/physical_server.xlsx" download>
                                <Download aria-hidden />
                                下载模板
                              </a>
                            </Button>
                            <Button asChild size="sm" variant="outline">
                              <a href="#/import">
                                <FileUp aria-hidden />
                                批量导入
                              </a>
                            </Button>
                            <Button size="sm" onClick={() => setCreating(true)}>
                              <Plus aria-hidden />
                              新增资产
                            </Button>
                          </div>
                        ) : (
                          <p className="text-center">当前是只读账号，请联系管理员或运维添加资产。</p>
                        )}
                      </div>
                    )}
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>

        {total > PAGE_SIZE ? (
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}>
              上一页
            </Button>
            <Button variant="outline" size="sm" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset((o) => o + PAGE_SIZE)}>
              下一页
            </Button>
          </div>
        ) : null}
      </div>

      <AssetDetailDrawer
        assetId={openId}
        sites={sites}
        editable={editable}
        onClose={() => setOpenId(null)}
        onChanged={reload}
      />
      <CreateAssetDialog
        open={creating}
        sites={sites}
        onClose={() => setCreating(false)}
        onCreated={(a) => {
          setCreating(false)
          reload()
          setOpenId(a.id)
        }}
      />
    </div>
  )
}
