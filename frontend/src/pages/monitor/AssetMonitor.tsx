import { useEffect, useId, useRef, useState } from "react"
import { LiveIndicator } from "@/components/LiveIndicator"
import { Badge, QualityBadge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice, Select, panelClass } from "@/components/ui/controls"
import { api, query, type Asset } from "@/lib/api"
import { type LiveMetricUpdate, useLiveEvents } from "@/lib/live"
import {
  type Collection,
  type CollectionPlan,
  type LiveMetric,
  dateText,
  numberText,
  sourceLabel,
  STATE,
  usePoll,
} from "@/lib/monitor"
import { cn } from "@/lib/utils"

const NUMERIC = ["valid", "estimated"]

/** 推送来的值比接口读到的新时覆盖显示（只影响显示，不涉及任何表单） */
function applyLive(m: LiveMetric, u: LiveMetricUpdate | undefined): LiveMetric {
  if (!u || new Date(u.observed_at).getTime() <= new Date(m.observed_at).getTime()) return m
  const numeric = NUMERIC.includes(u.quality) && u.value !== null
  return {
    ...m,
    value: u.value,
    quality: u.quality,
    unit: u.unit,
    estimated: u.quality === "estimated",
    observed_at: u.observed_at,
    interval_seconds: u.interval,
    message: numeric ? "" : m.message,
    last_valid_value: numeric ? u.value : m.last_valid_value,
    last_valid_at: numeric ? u.observed_at : m.last_valid_at,
  }
}

export function AssetMonitor({ asset, editable }: { asset: Asset; editable: boolean }) {
  const [overlay, setOverlay] = useState<Record<string, LiveMetricUpdate>>({})
  const known = useRef<Map<string, LiveMetric>>(new Map())
  const live = useLiveEvents({
    metrics: (e) => {
      if (e.asset_id !== asset.id) return
      setOverlay((prev) => {
        const next = { ...prev }
        for (const u of e.items) {
          const old = next[u.key]
          if (!old || new Date(u.observed_at) > new Date(old.observed_at)) next[u.key] = u
        }
        return next
      })
      // 新出现的指标或质量变化（需要读取说明文字）时补读一次接口
      if (e.items.some((u) => known.current.get(u.key)?.quality !== u.quality)) refreshMetrics()
    },
    collection: (e) => {
      if (e.items.some((i) => i.asset_id === asset.id)) refreshCollection()
    },
  })
  // 实时推送连上时接口只做低频校准；断开时恢复每 5 秒轮询
  const pollMs = live.live ? 60000 : 5000
  const {
    data: collection,
    error: collectionError,
    refresh: refreshCollection,
  } = usePoll<Collection>(`/api/assets/${asset.id}/collection`, pollMs)
  const {
    data: metrics,
    error: metricError,
    refresh: refreshMetrics,
  } = usePoll<{ items: LiveMetric[] }>(`/api/assets/${asset.id}/metrics`, pollMs)
  known.current = new Map((metrics?.items ?? []).map((m) => [m.key, m]))
  const [notice, setNotice] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const [resetId, setResetId] = useState<string | null>(null)
  const [filter, setFilter] = useState("")
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 5000)
    return () => window.clearInterval(timer)
  }, [])
  const id = useId()
  async function collect() {
    setBusy(true)
    setError("")
    setNotice("")
    try {
      await api.post(`/api/assets/${asset.id}/collect`)
      setNotice("已加入采集队列；数据将自动刷新。任务完成后请同时查看端点结果，确认采集是否成功。")
      refreshCollection()
      refreshMetrics()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  async function reprobe() {
    setBusy(true)
    setError("")
    setNotice("")
    try {
      await api.post(`/api/assets/${asset.id}/collection-plan/reprobe`)
      setNotice("已安排重新探测：下一次采集会把每个协议都读一遍，再重新决定每项数据由谁提供。只读，不改设备。")
      refreshCollection()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  async function reset() {
    if (!resetId) return
    setBusy(true)
    setError("")
    try {
      await api.post(`/api/endpoints/${resetId}/trust/reset`)
      setResetId(null)
      setNotice("已清除固定指纹；下次采集将信任首次收到的证书或主机密钥。")
      refreshCollection()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  const items = (metrics?.items ?? []).map((raw): LiveMetric => {
    const m = applyLive(raw, overlay[raw.key])
    // 后端暂时断连时，屏幕上缓存的有效样本也会随时钟变为过期。
    if (
      ["valid", "estimated"].includes(m.quality) &&
      now - new Date(m.observed_at).getTime() > Math.max(1, m.interval_seconds) * 3000
    ) {
      return { ...m, quality: "stale", value: null }
    }
    return m
  })
  const shown = items.filter((m) => `${m.key} ${m.source}`.toLowerCase().includes(filter.toLowerCase()))
  return (
    <div className="min-w-0 space-y-4 [&_button]:h-auto [&_button]:min-h-8 [&_button]:max-w-full [&_button]:whitespace-normal">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0 space-y-1 text-[0.8125rem]">
          <p className="font-medium">{collection?.profile?.label ?? "尚未配置采集模板"}</p>
          {collection?.profile ? (
            <p className="text-xs text-muted-foreground">
              {collection.profile.inferred ? "按平台与端点自动选择" : "资产指定模板"} ·{" "}
              {collection.profile.verified ? "已实机验证" : "未实机验证"}
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">在资产编辑中选择模板，并为管理端点关联只读凭据。</p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <LiveIndicator status={live.status} fallbackText="改为每 5 秒刷新" />
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              refreshCollection()
              refreshMetrics()
            }}
          >
            刷新指标
          </Button>
          {editable ? (
            <Button size="sm" onClick={() => void collect()} disabled={busy || !collection?.profile}>
              {busy ? "正在提交…" : "立即采集"}
            </Button>
          ) : null}
        </div>
      </div>
      {error || collectionError || metricError ? (
        <InlineNotice tone="bad" role="alert">
          {error || collectionError || metricError}
        </InlineNotice>
      ) : null}
      {notice ? <InlineNotice>{notice}</InlineNotice> : null}
      {collection?.last_task ? (
        <p className="text-xs text-muted-foreground">
          最近任务：{STATE[collection.last_task.status] ?? collection.last_task.status}
          {collection.last_task.result?.degraded ? " · 已降级到备用协议" : ""}
          {collection.last_task.result?.failed?.length
            ? ` · 协议失败：${collection.last_task.result.failed.map(sourceLabel).join("、")}`
            : ""}
          {collection.last_task.result?.unconfigured?.length
            ? ` · 待配置凭据：${collection.last_task.result.unconfigured.map(sourceLabel).join("、")}`
            : ""}
          {collection.last_task.result?.deferred_reason ? ` · ${collection.last_task.result.deferred_reason}` : ""}
          {collection.last_task.result?.deferred?.length
            ? ` · 本轮顺延：${[...new Set(collection.last_task.result.deferred)].map(sourceLabel).join("、")}（这台 BMC 一分钟内的访问已达上限，不算失败）`
            : ""}
          {collection.last_task.error ? ` · ${collection.last_task.error}` : ""}
        </p>
      ) : null}
      {collection?.boost ? (
        <InlineNotice>
          临时提频：每 {collection.boost.interval} 秒采集一次，到 {dateText(collection.boost.until)}（{collection.boost.reason}）。
          Redfish / IPMI 仍受每台 BMC 每分钟访问上限约束。
        </InlineNotice>
      ) : null}
      {collection?.plan ? (
        <CollectionPlanView plan={collection.plan} editable={editable} busy={busy} onReprobe={() => void reprobe()} />
      ) : null}
      <section aria-label="端点采集状态" className="space-y-2">
        {collection?.endpoints.map((ep) => {
          const endpoint = asset.endpoints.find((e) => e.id === ep.endpoint_id)
          return (
            <div key={ep.endpoint_id} className={cn(panelClass, "min-w-0 space-y-1.5 p-3 text-[0.8125rem]")}>
              <div className="flex flex-wrap items-center gap-2">
                <strong className="font-medium">{sourceLabel(ep.protocol)}</strong>
                <span className="break-all text-muted-foreground">
                  {endpoint?.address}
                  {endpoint?.port ? `:${endpoint.port}` : ""}
                </span>
                <Badge
                  tone={ep.state === "ok" ? "ok" : ep.state === "skipped" ? "neutral" : ep.state === "unconfigured" ? "warn" : "bad"}
                >
                  {STATE[ep.state] ?? ep.state}
                </Badge>
              </div>
              {ep.message ? <p className="break-words">{ep.message}</p> : null}
              <p className="text-xs text-muted-foreground">
                最近尝试：{dateText(ep.last_attempt_at)} · 最近成功：{dateText(ep.last_success_at)} · {ep.duration_ms}{" "}
                ms
              </p>
              {ep.retry_after ? (
                <p className="text-xs text-muted-foreground">
                  首选来源冷却至 {dateText(ep.retry_after)}；“立即采集”可提前重试。
                </p>
              ) : null}
              {endpoint?.tls_fingerprint || endpoint?.ssh_host_key ? (
                <details>
                  <summary className="cursor-pointer text-xs text-link">查看已固定指纹</summary>
                  <p className="mt-1 break-all font-mono text-xs">
                    {endpoint.tls_fingerprint || endpoint.ssh_host_key}
                  </p>
                </details>
              ) : null}
              {Object.keys(ep.facts).length ? (
                <details>
                  <summary className="cursor-pointer text-xs text-link">查看设备身份与采集信息</summary>
                  <pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap break-all text-xs">
                    {JSON.stringify(ep.facts, null, 2)}
                  </pre>
                </details>
              ) : null}
              {editable && ["ssh", "redfish", "pve-api", "esxi-api"].includes(ep.protocol) ? (
                <Button size="sm" variant="outline" onClick={() => setResetId(ep.endpoint_id)}>
                  重新信任此端点
                </Button>
              ) : null}
            </div>
          )
        })}
        {collection && collection.endpoints.length === 0 ? (
          <InlineNotice>
            尚无采集记录。{asset.endpoints.length ? "等待定时采集，或点击立即采集。" : "请先添加管理端点。"}
          </InlineNotice>
        ) : null}
      </section>
      {resetId ? (
        <InlineNotice tone="warn">
          <p>请先核对设备证书或 SSH 主机密钥确实已更换。清除后，下次连接将重新信任收到的指纹。</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void reset()}>
              确认清除固定指纹
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setResetId(null)}>
              取消
            </Button>
          </div>
        </InlineNotice>
      ) : null}
      <div className="flex flex-wrap items-end justify-between gap-2">
        <h3 className="text-sm font-semibold">
          最新指标{" "}
          <span className="text-xs font-normal text-muted-foreground">
            {live.live ? "实时推送更新" : "每 5 秒刷新"}
          </span>
        </h3>
        <label className="flex flex-col gap-1 text-xs text-muted-foreground" htmlFor={`${id}-filter`}>
          筛选指标
          <input
            className="h-8 w-full rounded-md border bg-card px-2 text-[0.8125rem] text-foreground"
            id={`${id}-filter`}
            data-help-plain=""
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="指标名称或来源"
          />
        </label>
      </div>
      <div className={cn(panelClass, "overflow-x-auto")}>
        <table className="data-table w-full min-w-[600px]">
          <caption className="sr-only">最新指标，空值用横线和质量状态表示</caption>
          <thead>
            <tr>
              {["指标", "当前值", "质量", "来源 / 时间"].map((v) => (
                <th scope="col" key={v}>
                  {v}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((m) => (
              <tr key={m.key}>
                <th scope="row" className="max-w-64 break-all font-normal">
                  <span className="font-medium">{m.key}</span>
                  {m.message ? <p className="mt-1 text-xs text-muted-foreground">{m.message}</p> : null}
                </th>
                <td className="tabular">
                  {m.quality === "estimated" && m.value !== null ? "≈ " : ""}
                  {numberText(m.value)}
                  {m.value !== null ? ` ${m.unit}` : ""}
                  {m.value === null && m.last_valid_value !== null ? (
                    <p className="mt-1 text-xs text-muted-foreground">
                      上次有效：{numberText(m.last_valid_value)} {m.unit}
                      <br />
                      {dateText(m.last_valid_at)}
                    </p>
                  ) : null}
                </td>
                <td>
                  <QualityBadge quality={m.quality} />
                </td>
                <td className="text-xs text-muted-foreground">
                  {sourceLabel(m.source)}
                  <br />
                  {dateText(m.observed_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {metrics && shown.length === 0 ? (
          <p className="px-3 py-6 text-center text-[0.8125rem] text-muted-foreground">
            {items.length ? "没有符合条件的指标。" : "尚无指标数据；请检查端点和凭据后采集。"}
          </p>
        ) : null}
        {!metrics && !metricError ? <p className="p-3 text-muted-foreground">正在加载指标…</p> : null}
      </div>
      {items.length ? <History key={asset.id} assetId={asset.id} metrics={items} /> : null}
    </div>
  )
}

type HistoryPoint = { t: number; v?: number | null; q?: string; avg?: number; min?: number; max?: number; n?: number }
/** 4.4 P20a：每项数据由哪个协议提供、谁兜底，以及上次探测的结果 */
function CollectionPlanView({
  plan,
  editable,
  busy,
  onReprobe,
}: {
  plan: CollectionPlan
  editable: boolean
  busy: boolean
  onReprobe: () => void
}) {
  return (
    <section aria-label="采集计划" className={cn(panelClass, "min-w-0 space-y-2 p-3 text-[0.8125rem]")}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="font-medium">采集计划</p>
          <p className="text-xs text-muted-foreground">
            {plan.exists
              ? `上次探测：${dateText(plan.probed_at)}${plan.reprobe ? " · 下次采集时重新探测" : ""}。每项数据只由主来源提供，主来源失败才改用兜底并标出“降级”。`
              : "还没有探测：下一次采集会先把每个协议都读一遍，再决定每项数据由谁提供。"}
          </p>
        </div>
        {editable && plan.exists ? (
          <Button size="sm" variant="outline" onClick={onReprobe} disabled={busy || plan.reprobe}>
            重新探测
          </Button>
        ) : null}
      </div>
      {plan.exists ? (
        <>
          <div className="min-w-0 overflow-x-auto">
            <table className="data-table w-full min-w-[28rem]">
              <caption className="sr-only">每项数据的来源</caption>
              <thead>
                <tr>
                  <th>数据</th>
                  <th>主来源</th>
                  <th>兜底</th>
                  <th>频率</th>
                </tr>
              </thead>
              <tbody>
                {plan.items.map((it) => (
                  <tr key={it.key} className="border-t">
                    <td>{it.label}</td>
                    <td>{it.primary ? sourceLabel(it.primary) : <span className="text-muted-foreground">没有协议读到</span>}</td>
                    <td>{it.fallbacks.length ? it.fallbacks.map(sourceLabel).join(" → ") : "—"}</td>
                    <td className="text-xs">{it.slow ? `每 ${plan.slow_every ?? 10} 个周期` : "每个周期"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-muted-foreground">
            探测结果：
            {Object.entries(plan.probed)
              .map(([p, seen]) => `${sourceLabel(p)} ${seen === null ? "不可用（每小时重试）" : `读到 ${seen.length} 项`}`)
              .join("；")}
          </p>
          {plan.check?.length ? (
            <ul className="space-y-0.5 text-xs" aria-label="双来源核对">
              <li className="text-muted-foreground">
                双来源核对（每天一次，温度相差 3℃、功耗相差 10% 以内算一致）：{plan.checked_at ? dateText(plan.checked_at) : ""}
              </li>
              {plan.check.map((c) => (
                <li key={c.key}>
                  <Badge tone={c.ok === false ? "warn" : c.ok ? "ok" : "neutral"} className="mr-1 whitespace-nowrap">
                    {c.label}
                  </Badge>
                  {c.detail}
                </li>
              ))}
            </ul>
          ) : null}
        </>
      ) : null}
    </section>
  )
}

function History({ assetId, metrics }: { assetId: string; metrics: LiveMetric[] }) {
  const id = useId()
  const [key, setKey] = useState(metrics[0]?.key ?? "")
  const [hours, setHours] = useState("1")
  const [history, setHistory] = useState<{ resolution: string; points: HistoryPoint[] } | null>(null)
  const [error, setError] = useState("")
  useEffect(() => {
    let stopped = false
    setHistory(null)
    setError("")
    const end = new Date()
    api
      .get<{ resolution: string; points: HistoryPoint[] }>(
        `/api/assets/${assetId}/metrics/series${query({ key, start: new Date(end.getTime() - Number(hours) * 3600000).toISOString(), end: end.toISOString() })}`,
      )
      .then((v) => {
        if (!stopped) setHistory(v)
      })
      .catch((e: Error) => {
        if (!stopped) setError(e.message)
      })
    return () => {
      stopped = true
    }
  }, [assetId, key, hours])
  return (
    <section className="min-w-0 space-y-3" aria-label="指标历史">
      <h3 className="text-sm font-semibold">指标历史</h3>
      <div className="flex flex-wrap gap-3">
        <Select
          id={`${id}-metric`}
          label="历史指标"
          help="monitor.metric"
          value={key}
          onChange={setKey}
          options={metrics.map((m) => ({ value: m.key, label: m.key }))}
          className="min-w-0 max-w-full [&_select]:max-w-full"
        />
        <Select
          id={`${id}-range`}
          label="时间范围"
          help="monitor.range"
          value={hours}
          onChange={setHours}
          options={[
            { value: "1", label: "最近 1 小时" },
            { value: "24", label: "最近 24 小时" },
            { value: "168", label: "最近 7 天" },
            { value: "720", label: "最近 30 天" },
            { value: "8760", label: "最近 1 年" },
          ]}
        />
      </div>
      {error ? (
        <InlineNotice role="alert" tone="bad">
          {error}
        </InlineNotice>
      ) : !history ? (
        <p className="text-muted-foreground">正在加载历史…</p>
      ) : history.points.length === 0 ? (
        <InlineNotice>此时间范围没有历史样本；汇总在采集后下一分钟生成。</InlineNotice>
      ) : (
        <>
          <p className="text-xs text-muted-foreground">
            {history.resolution === "raw" ? "原始样本" : history.resolution === "minute" ? "分钟汇总" : "小时汇总"} ·{" "}
            {history.points.length} 个时间点 · 汇总仅包含有效和估算值，空值不按 0 计算。
          </p>
          <div className={cn(panelClass, "max-h-72 overflow-auto")}>
            <table className="data-table data-table-compact w-full min-w-[420px]">
              <caption className="sr-only">指标历史样本；最新的在前</caption>
              <thead className="sticky top-0 z-10">
                <tr>
                  {["时间", "读数 / 平均", "最小", "最大", "质量 / 样本数"].map((v) => (
                    <th key={v} scope="col">
                      {v}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {[...history.points].reverse().map((p, i) => (
                  <tr key={`${p.t}-${i}`}>
                    <td>{new Date(p.t * 1000).toLocaleString()}</td>
                    <td className="tabular">{numberText(p.avg ?? p.v)}</td>
                    <td className="tabular">{numberText(p.min)}</td>
                    <td className="tabular">{numberText(p.max)}</td>
                    <td>{p.q ? <QualityBadge quality={p.q as LiveMetric["quality"]} /> : p.n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}
