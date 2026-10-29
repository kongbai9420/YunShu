import { useEffect, useState } from "react"
import { cn } from "@/lib/utils"
import { PageHeader } from "@/components/AppShell"
import { LiveIndicator } from "@/components/LiveIndicator"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Card, InlineNotice, Segmented, Select, panelClass } from "@/components/ui/controls"
import { type Asset, type Page, query } from "@/lib/api"
import { canEdit, seesAll, useAuth } from "@/lib/auth"
import { useLiveEvents } from "@/lib/live"
import { type Profile, type Task, dateText, sourceLabel, STATE, usePoll } from "@/lib/monitor"
import { AssetMonitor } from "./AssetMonitor"
import { DiscoveryPanel } from "./DiscoveryPanel"
import { ScopeNotice } from "@/components/ScopeNote"

type Tab = "metrics" | "tasks" | "discovery" | "profiles"
export function MonitorPage() {
  const [tab, setTab] = useState<Tab>("metrics")
  const { user } = useAuth()
  const editable = canEdit(user)
  const worker = usePoll<{ running: boolean }>("/api/worker/status")
  const live = useLiveEvents()
  return (
    <div className="pb-8">
      <PageHeader
        title="监控中心"
        description="查看真实采集结果、指标质量与历史；通过只读发现添加待确认设备。"
        actions={
          <>
            <LiveIndicator status={live.status} fallbackText="改为定时刷新" />
            <Badge tone={worker.data?.running ? "ok" : "warn"}>
              {worker.error
                ? "调度状态读取失败"
                : !worker.data
                  ? "正在读取调度状态"
                  : worker.data.running
                    ? "采集调度运行中"
                    : "采集调度未运行"}
            </Badge>
          </>
        }
      />
      <div className="min-w-0 space-y-4 px-6">
        {worker.data && !worker.data.running ? (
          <InlineNotice tone="warn">
            后台 Worker 尚未运行，队列中的任务会等待。请使用一键预览，或按后端运行说明启动 Worker。
          </InlineNotice>
        ) : null}
        <Segmented
          label="监控中心内容"
          value={tab}
          onChange={setTab}
          options={[
            { value: "metrics", label: "指标与历史" },
            { value: "tasks", label: "采集任务" },
            { value: "discovery", label: "网段发现" },
            { value: "profiles", label: "设备模板" },
          ]}
        />
        {tab === "metrics" ? (
          <MetricBrowser editable={editable} />
        ) : tab === "tasks" ? (
          <TaskBrowser />
        ) : tab === "discovery" ? (
          seesAll(user) ? <DiscoveryPanel editable={editable} /> : <ScopeNotice what="网段发现会扫描出范围外的设备，" />
        ) : (
          <Profiles />
        )}
      </div>
    </div>
  )
}

function MetricBrowser({ editable }: { editable: boolean }) {
  const [search, setSearch] = useState("")
  const [q, setQ] = useState("")
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState("")
  const assets = usePoll<Page<Asset>>(`/api/assets${query({ q, offset, limit: 100 })}`, 15000)
  useEffect(() => {
    if (assets.data && !assets.data.items.some((a) => a.id === selected)) setSelected(assets.data.items[0]?.id ?? "")
  }, [assets.data, selected])
  const asset = assets.data?.items.find((a) => a.id === selected)
  return (
    <div className="min-w-0 space-y-4">
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          setOffset(0)
          setQ(search.trim())
        }}
      >
        <label className="flex min-w-0 flex-col gap-1 text-xs text-muted-foreground" htmlFor="monitor-search">
          搜索资产
          <input
            id="monitor-search"
            data-help-plain=""
            className="h-8 w-full rounded-md border bg-card px-2 text-[0.8125rem] text-foreground sm:w-64"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="名称、编号或管理地址"
          />
        </label>
        <Button type="submit" size="sm" variant="outline">
          搜索
        </Button>
        {assets.data?.items.length ? (
          <Select
            id="monitor-asset"
            label="当前资产"
            plain
            value={selected}
            onChange={setSelected}
            options={assets.data.items.map((a) => ({
              value: a.id,
              label: `${a.name}${a.asset_no ? `（${a.asset_no}）` : ""}`,
            }))}
            className="min-w-0 max-w-full [&_select]:max-w-full"
          />
        ) : null}
      </form>
      {assets.error ? (
        <InlineNotice tone="bad" role="alert">
          {assets.error}
        </InlineNotice>
      ) : null}
      {!assets.data && !assets.error ? <p className="text-muted-foreground">正在加载资产…</p> : null}
      {assets.data && assets.data.items.length === 0 ? (
        <InlineNotice>
          没有符合条件的资产。可前往
          <a className="text-link underline" href="#/assets">
            资产管理
          </a>
          添加，或使用网段发现。
        </InlineNotice>
      ) : null}
      {assets.data && assets.data.total > 100 ? (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => setOffset((n) => n - 100)}>
            上一页资产
          </Button>
          <span>
            {offset + 1}–{Math.min(offset + 100, assets.data.total)} / {assets.data.total}
          </span>
          <Button
            size="sm"
            variant="outline"
            disabled={offset + 100 >= assets.data.total}
            onClick={() => setOffset((n) => n + 100)}
          >
            下一页资产
          </Button>
        </div>
      ) : null}
      {asset ? (
        <Card title={asset.name}>
          <AssetMonitor key={asset.id} asset={asset} editable={editable} />
        </Card>
      ) : null}
    </div>
  )
}

function TaskBrowser() {
  const [state, setState] = useState("")
  // 任务计数变化时由实时推送触发刷新；推送断开时每 5 秒轮询
  const live = useLiveEvents({ tasks: () => tasks.refresh() })
  const tasks = usePoll<{ items: Task[]; total: number }>(
    `/api/tasks${query({ kind: "collect.asset", status: state, limit: 100 })}`,
    live.live ? 30000 : 5000,
  )
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <Select
          id="task-status"
          label="任务状态"
          plain
          value={state}
          onChange={setState}
          options={[
            { value: "", label: "全部" },
            ...["queued", "running", "succeeded", "failed"].map((v) => ({ value: v, label: STATE[v] })),
          ]}
        />
        <Button variant="outline" size="sm" onClick={tasks.refresh}>
          刷新任务
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        共 {tasks.data?.total ?? "—"} 项，显示最近 100 项，{live.live ? "任务状态变化时实时刷新。" : "每 5 秒刷新。"}
        任务完成仅表示执行结束；协议失败和降级结果见资产的端点采集状态。
      </p>
      {tasks.error ? (
        <InlineNotice role="alert" tone="bad">
          {tasks.error}
        </InlineNotice>
      ) : null}
      <div className={cn(panelClass, "overflow-x-auto")}>
        <table className="data-table w-full min-w-[650px]">
          <caption className="sr-only">采集任务列表</caption>
          <thead>
            <tr>
              {["任务 / 资产", "状态", "尝试次数", "开始 / 完成", "说明"].map((s) => (
                <th key={s} scope="col">
                  {s}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tasks.data?.items.map((t) => (
              <tr key={t.id}>
                <th scope="row" className="max-w-56 break-all text-xs font-normal">
                  {t.id}
                  <br />
                  <span className="text-muted-foreground">{t.asset_name ?? t.asset_id ?? "资产已删除"}</span>
                </th>
                <td>
                  <Badge
                    tone={
                      t.status === "failed"
                        ? "bad"
                        : t.result.failed?.length || t.result.unconfigured?.length
                          ? "warn"
                          : t.status === "succeeded"
                            ? "ok"
                            : "info"
                    }
                  >
                    {STATE[t.status] ?? t.status}
                  </Badge>
                </td>
                <td>
                  {t.attempts} / {t.max_attempts}
                </td>
                <td className="text-xs">
                  {dateText(t.started_at)}
                  <br />
                  {dateText(t.finished_at)}
                </td>
                <td className="max-w-64 break-words text-xs">
                  {t.error ||
                    [
                      t.result.failed?.length
                        ? `${t.result.degraded ? "已降级；" : ""}协议失败：${t.result.failed.map(sourceLabel).join("、")}`
                        : "",
                      t.result.unconfigured?.length ? `待配置凭据：${t.result.unconfigured.map(sourceLabel).join("、")}` : "",
                    ]
                      .filter(Boolean)
                      .join("；") ||
                    "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {tasks.data?.items.length === 0 ? (
          <p className="p-6 text-center text-muted-foreground">尚无此类采集任务。</p>
        ) : null}
      </div>
    </div>
  )
}

function Profiles() {
  const profiles = usePoll<Profile[]>("/api/collection/profiles", 60000)
  return (
    <div className="space-y-3">
      <InlineNotice>
        模板只声明采集协议和指标，不执行自定义脚本。所有设备模板均未实机验证；模拟测试的覆盖范围与限制列在各模板中。
      </InlineNotice>
      {profiles.error ? (
        <InlineNotice role="alert" tone="bad">
          {profiles.error}
        </InlineNotice>
      ) : null}
      <div className="grid min-w-0 gap-3 xl:grid-cols-2">
        {profiles.data?.map((p) => (
          <Card key={p.name} title={p.label} actions={<Badge tone="neutral">{p.batch} 批 · 未实机验证</Badge>}>
            <div className="space-y-2 text-[0.8125rem]">
              <p>
                {p.steps
                  .map(
                    (s) =>
                      `${s.mode === "fallback" ? "备用" : s.mode === "supplement" ? "补充" : "首选"} ${sourceLabel(s.protocol)}`,
                  )
                  .join(" → ")}{" "}
                · {p.interval} 秒
              </p>
              <p className="text-muted-foreground">{p.metrics.join(" · ")}</p>
              <details>
                <summary className="cursor-pointer text-link">前提、限制与测试依据</summary>
                <dl className="mt-2 space-y-2">
                  <div>
                    <dt className="font-medium">使用前提</dt>
                    <dd>{p.prerequisites}</dd>
                  </div>
                  <div>
                    <dt className="font-medium">已知限制</dt>
                    <dd>{p.limitations}</dd>
                  </div>
                  <div>
                    <dt className="font-medium">验证记录</dt>
                    <dd>{p.evidence}</dd>
                  </div>
                </dl>
              </details>
            </div>
          </Card>
        ))}
      </div>
    </div>
  )
}
