import { Dialog } from "radix-ui"
import { useEffect, useState } from "react"

import { PageHeader } from "@/components/AppShell"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Card, InlineNotice, Segmented, Select, panelClass } from "@/components/ui/controls"
import { ModalContent, ModalFooter, ModalHeader } from "@/components/ui/dialog"
import { CheckField, TextField } from "@/components/ui/form"
import { ApiError, api, query, type Asset, type Page } from "@/lib/api"
import { canEdit, useAuth } from "@/lib/auth"
import { dateText, usePoll } from "@/lib/monitor"
import { cn } from "@/lib/utils"
import { ReportsPage } from "@/pages/reports/ReportsPage"

/** 巡检（4.4 P21）：命令巡检任务、执行记录、巡检模板，以及原来的每日巡检报告。 */

type Tab = "jobs" | "runs" | "templates" | "reports"
type Verdict = "ok" | "abnormal" | "unknown" | "error"
type Severity = "critical" | "warning" | "info"

interface Check {
  type: string
  pattern?: string
  ok_if?: string
  op?: "gte" | "lte"
  warning?: number | null
  critical?: number | null
}
interface Item {
  key: string
  name: string
  command: string
  check: Check
  severity: Severity
  timeout: number
}
interface Template {
  id: string
  name: string
  platform: string
  platform_label: string
  description: string
  builtin: boolean
  item_count: number
  revision: number
  updated_by: string
  updated_at: string
  items: Item[]
}
interface TemplateList {
  items: Template[]
  platforms: Record<string, string>
}
interface Schedule {
  kind: "once" | "daily" | "weekly" | "monthly" | "cron"
  at?: string
  expr?: string
  time?: string
  weekdays?: number[]
  day?: number
}
interface Job {
  id: string
  name: string
  template_id: string
  template_name: string
  assets: { id: string; name: string }[]
  asset_total: number
  schedule: Schedule
  enabled: boolean
  next_run_at: string | null
  last_run_at: string | null
  created_by: string
  revision: number
}
interface Run {
  id: string
  name: string
  template_name: string
  triggered_by: string
  created_at: string
  finished_at: string | null
  status: string
  asset_total: number
  assets_done: number
  counts: Partial<Record<Verdict | Severity, number>>
}
interface Result {
  id: string
  asset_name: string
  item_key: string
  item_name: string
  command: string
  verdict: Verdict
  severity: Severity | ""
  detail: string
  exit_code: number | null
  observed_at: string
  output_size: number
}
interface ResultFull {
  item_name: string
  asset_name: string
  command: string
  output: string
  verdict: Verdict
  detail: string
  exit_code: number | null
  observed_at: string
  previous: { output: string; verdict: Verdict; observed_at: string } | null
}

const VERDICT: Record<Verdict, { label: string; tone: "ok" | "bad" | "warn" | "neutral" }> = {
  ok: { label: "正常", tone: "ok" },
  abnormal: { label: "异常", tone: "bad" },
  unknown: { label: "无法判断", tone: "neutral" },
  error: { label: "未执行", tone: "warn" },
}
const SEVERITY: Record<Severity, string> = { critical: "严重", warning: "警告", info: "提示" }
const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
const CHECK_LABEL: Record<string, string> = {
  record: "只记录输出",
  empty: "输出为空算正常",
  regex: "输出匹配正则算正常",
  not_regex: "输出匹配正则算异常",
  threshold: "数值阈值",
  exit0: "退出码为 0 算正常",
}

export function scheduleText(s: Schedule): string {
  if (s.kind === "once") return s.at ? `单次 · ${dateText(s.at)}` : "单次 · 立即"
  if (s.kind === "daily") return `每天 ${s.time}`
  if (s.kind === "weekly") return `每周${(s.weekdays ?? []).map((d) => WEEKDAYS[d].slice(1)).join("、")} ${s.time}`
  if (s.kind === "cron") return `cron · ${s.expr}`
  return `每月 ${s.day} 日 ${s.time}`
}

function checkText(c: Check): string {
  if (c.type === "threshold") {
    const sign = c.op === "lte" ? "≤" : "≥"
    return `数值${sign}${c.warning} 警告${c.critical != null ? `，${sign}${c.critical} 严重` : ""}`
  }
  return CHECK_LABEL[c.type] ?? c.type
}

function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    const d = e.detail as { problems?: string[] } | undefined
    return d?.problems?.length ? d.problems.join("；") : e.message
  }
  return (e as Error).message
}

export function InspectionPage({ initialTab = "jobs" }: { initialTab?: Tab }) {
  const [tab, setTab] = useState<Tab>(initialTab)
  useEffect(() => setTab(initialTab), [initialTab])
  return (
    <div className="min-w-0 pb-8 [&_p]:break-words">
      <div className="print:hidden">
        <PageHeader
          title="巡检"
          description="命令巡检：按模板经 SSH 登录服务器、PVE 或交换机执行只读命令，按规则判断正常或异常，可以单次、每天、每周或每月执行。每日巡检报告按已采集的数据生成，不登录设备。"
        />
        <div className="px-6 pb-4">
          <Segmented
            label="巡检内容"
            value={tab}
            onChange={setTab}
            options={[
              { value: "jobs", label: "巡检任务" },
              { value: "runs", label: "执行记录" },
              { value: "templates", label: "巡检模板" },
              { value: "reports", label: "每日巡检报告" },
            ]}
          />
        </div>
      </div>
      {tab === "jobs" ? <JobsTab onRun={() => setTab("runs")} /> : null}
      {tab === "runs" ? <RunsTab /> : null}
      {tab === "templates" ? <TemplatesTab /> : null}
      {tab === "reports" ? <ReportsPage embedded /> : null}
    </div>
  )
}

// ---- 巡检任务 ----

function JobsTab({ onRun }: { onRun: () => void }) {
  const { user } = useAuth()
  const editable = canEdit(user)
  const jobs = usePoll<Job[]>("/api/command-inspection/jobs", 30000)
  const [editing, setEditing] = useState<Job | "new" | null>(null)
  const [error, setError] = useState("")
  async function act(fn: () => Promise<unknown>, thenRuns = false) {
    setError("")
    try {
      await fn()
      jobs.refresh()
      if (thenRuns) onRun()
    } catch (e) {
      setError(errorText(e))
    }
  }
  const items = jobs.data ?? []
  return (
    <div className="min-w-0 space-y-4 px-6">
      <div className="flex flex-wrap items-center gap-2">
        {editable ? <Button onClick={() => setEditing("new")}>新建巡检任务</Button> : null}
        <p className="text-xs text-muted-foreground">
          只用资产上用途为“监控”或“管理”的 SSH 端点与凭据（建议专用只读账号）；同一设备的命令逐条执行。只有“严重”异常会生成告警。
        </p>
      </div>
      {error || jobs.error ? (
        <InlineNotice tone="bad" role="alert">
          {error || jobs.error}
        </InlineNotice>
      ) : null}
      {jobs.data && !items.length ? (
        <InlineNotice>还没有巡检任务。新建时选模板、选设备、定执行方式；选“单次 · 立即”会马上执行一次。</InlineNotice>
      ) : null}
      {items.length ? (
        <div className={cn(panelClass, "min-w-0 overflow-x-auto")}>
          <table className="data-table w-full min-w-[46rem]">
            <caption className="sr-only">巡检任务列表</caption>
            <thead>
              <tr>
                <th>名称</th>
                <th>模板</th>
                <th>设备</th>
                <th>执行方式</th>
                <th>下次 / 上次</th>
                {editable ? <th>操作</th> : null}
              </tr>
            </thead>
            <tbody>
              {items.map((j) => (
                <tr key={j.id} className="border-t align-top">
                  <td>
                    {j.name}
                    {!j.enabled ? (
                      <Badge tone="neutral" className="ml-2 whitespace-nowrap">
                        已停用
                      </Badge>
                    ) : null}
                  </td>
                  <td>{j.template_name}</td>
                  <td className="max-w-[16rem] text-xs">
                    {j.assets.map((a) => a.name).join("、")}
                    {j.asset_total > j.assets.length ? `（另有 ${j.asset_total - j.assets.length} 台不在你的范围内）` : ""}
                  </td>
                  <td className="whitespace-nowrap text-xs">{scheduleText(j.schedule)}</td>
                  <td className="whitespace-nowrap text-xs">
                    {j.next_run_at ? dateText(j.next_run_at) : "—"}
                    <br />
                    <span className="text-muted-foreground">{j.last_run_at ? dateText(j.last_run_at) : "还没执行过"}</span>
                  </td>
                  {editable ? (
                    <td>
                      <div className="flex flex-wrap gap-1">
                        <Button size="xs" variant="outline" onClick={() => void act(() => api.post(`/api/command-inspection/jobs/${j.id}/run`), true)}>
                          立即执行
                        </Button>
                        <Button size="xs" variant="ghost" onClick={() => setEditing(j)}>
                          编辑
                        </Button>
                        <Button
                          size="xs"
                          variant="ghost"
                          onClick={() => {
                            if (window.confirm(`删除巡检任务“${j.name}”？执行记录会保留。`))
                              void act(() => api.del(`/api/command-inspection/jobs/${j.id}`))
                          }}
                        >
                          删除
                        </Button>
                      </div>
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {editing ? (
        <JobDialog
          job={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onDone={(ran) => {
            setEditing(null)
            jobs.refresh()
            if (ran) onRun()
          }}
        />
      ) : null}
    </div>
  )
}

function JobDialog({ job, onClose, onDone }: { job: Job | null; onClose: () => void; onDone: (ran: boolean) => void }) {
  const templates = usePoll<TemplateList>("/api/command-inspection/templates", 60000)
  const [name, setName] = useState(job?.name ?? "")
  const [templateId, setTemplateId] = useState(job?.template_id ?? "")
  const [assetIds, setAssetIds] = useState<string[]>(job?.assets.map((a) => a.id) ?? [])
  const [kind, setKind] = useState<Schedule["kind"]>(job?.schedule.kind ?? "daily")
  const [immediate, setImmediate] = useState(!job?.schedule.at)
  const [at, setAt] = useState(job?.schedule.at?.slice(0, 16) ?? "")
  const [time, setTime] = useState(job?.schedule.time ?? "08:00")
  const [weekdays, setWeekdays] = useState<number[]>(job?.schedule.weekdays ?? [0])
  const [day, setDay] = useState(String(job?.schedule.day ?? 1))
  const [expr, setExpr] = useState(job?.schedule.expr ?? "30 8 * * 1-5")
  const [enabled, setEnabled] = useState(job?.enabled ?? true)
  const [search, setSearch] = useState("")
  const [q, setQ] = useState("")
  const assets = usePoll<Page<Asset>>(`/api/assets${query({ q, limit: 100 })}`, 60000)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const list = templates.data?.items ?? []
  useEffect(() => {
    if (!templateId && list.length) setTemplateId(list[0].id)
  }, [templateId, list])
  const toggle = (id: string) => setAssetIds((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]))
  function schedule(): Schedule {
    if (kind === "once") return immediate || !at ? { kind } : { kind, at: new Date(at).toISOString() }
    if (kind === "weekly") return { kind, time, weekdays }
    if (kind === "monthly") return { kind, time, day: Number(day) }
    if (kind === "cron") return { kind, expr }
    return { kind, time }
  }
  async function submit() {
    setBusy(true)
    setError("")
    const body = { name, template_id: templateId, asset_ids: assetIds, schedule: schedule(), enabled, revision: job?.revision }
    try {
      const r = job
        ? await api.put<Job>(`/api/command-inspection/jobs/${job.id}`, body)
        : await api.post<Job & { run_id: string | null }>("/api/command-inspection/jobs", body)
      onDone(Boolean((r as { run_id?: string | null }).run_id))
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }
  const items = assets.data?.items ?? []
  return (
    <Dialog.Root open onOpenChange={(o) => (!o ? onClose() : undefined)}>
      <ModalContent width={640}>
        <ModalHeader title={job ? `编辑巡检任务：${job.name}` : "新建巡检任务"} description="选模板、选设备、定执行方式。每台设备一个后台任务，命令逐条执行。" />
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <TextField id="ij-name" label="名称" value={name} onChange={setName} placeholder="例如：每日服务器巡检" plain />
          <Select
            id="ij-template"
            label="巡检模板"
            value={templateId}
            onChange={setTemplateId}
            options={list.map((t) => ({ value: t.id, label: `${t.name}（${t.platform_label}）` }))}
            help="inspection.template"
          />
          <Select
            id="ij-kind"
            label="执行方式"
            value={kind}
            onChange={(v) => setKind(v as Schedule["kind"])}
            options={[
              { value: "once", label: "单次" },
              { value: "daily", label: "每天" },
              { value: "weekly", label: "每周" },
              { value: "monthly", label: "每月" },
              { value: "cron", label: "高级（cron 表达式）" },
            ]}
            help="inspection.schedule"
          />
          {kind === "once" ? (
            <div className="space-y-2">
              <CheckField label="立即执行" checked={immediate} onChange={setImmediate} hint="不勾选时指定一个时间执行一次" />
              {!immediate ? <TextField id="ij-at" label="执行时间" type="datetime-local" value={at} onChange={setAt} plain /> : null}
            </div>
          ) : kind === "cron" ? (
            <TextField
              id="ij-cron"
              label="cron 表达式（平台时区）"
              value={expr}
              onChange={setExpr}
              placeholder="分 时 日 月 星期"
              hint="例如 30 8 * * 1-5：工作日 08:30"
              help="inspection.cron"
            />
          ) : (
            <TextField id="ij-time" label="执行时间（平台时区）" type="time" value={time} onChange={setTime} plain />
          )}
          {kind === "weekly" ? (
            <fieldset className="sm:col-span-2">
              <legend className="text-xs font-medium text-muted-foreground">每周哪几天</legend>
              <div className="mt-1 flex flex-wrap gap-3">
                {WEEKDAYS.map((label, d) => (
                  <CheckField
                    key={label}
                    plain
                    label={label}
                    checked={weekdays.includes(d)}
                    onChange={(c) => setWeekdays((cur) => (c ? [...cur, d].sort() : cur.filter((x) => x !== d)))}
                  />
                ))}
              </div>
            </fieldset>
          ) : null}
          {kind === "monthly" ? (
            <TextField id="ij-day" label="每月几号" type="number" min={1} max={31} value={day} onChange={setDay} hint="当月没有这一天时在最后一天执行" />
          ) : null}
          {job ? <CheckField label="启用" checked={enabled} onChange={setEnabled} hint="停用后不再定时执行，仍可手动立即执行" /> : null}
        </div>
        <div className="mt-3">
          <form
            className="flex items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              setQ(search)
            }}
          >
            <TextField id="ij-search" label="搜索设备" value={search} onChange={setSearch} placeholder="名称或管理地址" className="flex-1" plain />
            <Button type="submit" size="sm" variant="outline">
              搜索
            </Button>
          </form>
          <p className="mt-1 text-xs text-muted-foreground">已选 {assetIds.length} 台。设备需要有用途为“监控”或“管理”、关联了凭据的 SSH 端点。</p>
          <ul className="mt-1 max-h-48 space-y-1 overflow-y-auto rounded-md border p-1" aria-label="可选设备">
            {items.map((a) => (
              <li key={a.id} className="flex items-center gap-2 px-1">
                <input type="checkbox" data-help-plain="" id={`ij-a-${a.id}`} checked={assetIds.includes(a.id)} onChange={() => toggle(a.id)} />
                <label htmlFor={`ij-a-${a.id}`} className="min-w-0 flex-1 truncate text-[0.8125rem]">
                  {a.name}
                </label>
              </li>
            ))}
          </ul>
        </div>
        {error ? (
          <InlineNotice tone="bad" role="alert" className="mt-3">
            {error}
          </InlineNotice>
        ) : null}
        <ModalFooter>
          <Dialog.Close asChild>
            <Button variant="outline">取消</Button>
          </Dialog.Close>
          <Button disabled={busy || !name.trim() || !templateId || !assetIds.length} onClick={() => void submit()}>
            {busy ? "正在保存…" : !job && kind === "once" && immediate ? "保存并立即执行" : "保存"}
          </Button>
        </ModalFooter>
      </ModalContent>
    </Dialog.Root>
  )
}

// ---- 执行记录 ----

function Counts({ c }: { c: Run["counts"] }) {
  return (
    <span className="flex flex-wrap gap-1">
      {c.critical ? <Badge tone="bad">严重 {c.critical}</Badge> : null}
      {c.warning ? <Badge tone="warn">警告 {c.warning}</Badge> : null}
      {c.info ? <Badge tone="info">提示 {c.info}</Badge> : null}
      {c.error ? <Badge tone="warn">未执行 {c.error}</Badge> : null}
      {c.unknown ? <Badge tone="neutral">无法判断 {c.unknown}</Badge> : null}
      <Badge tone="ok">正常 {c.ok ?? 0}</Badge>
    </span>
  )
}

function RunsTab() {
  const [fast, setFast] = useState(true)
  // 有进行中的执行时每 3 秒刷新，否则 15 秒
  const runs = usePoll<{ total: number; items: Run[] }>("/api/command-inspection/runs?limit=50", fast ? 3000 : 15000)
  const [open, setOpen] = useState("")
  const items = runs.data?.items ?? []
  const running = items.some((r) => r.status !== "done")
  useEffect(() => setFast(running), [running])
  useEffect(() => {
    if (!open && items[0]) setOpen(items[0].id)
  }, [open, items])
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4 px-6 xl:grid-cols-[minmax(0,18rem)_minmax(0,1fr)]">
      <Card title="最近的执行">
        {runs.error ? (
          <InlineNotice tone="bad" role="alert">
            {runs.error}
          </InlineNotice>
        ) : null}
        {runs.data && !items.length ? <p className="text-muted-foreground">还没有执行过巡检。</p> : null}
        <ul className="space-y-1" aria-label="执行记录">
          {items.map((r) => (
            <li key={r.id}>
              <button
                type="button"
                onClick={() => setOpen(r.id)}
                className={cn("w-full rounded-md px-2 py-1.5 text-left hover:bg-muted", open === r.id && "bg-muted")}
              >
                <span className="block truncate text-[0.8125rem] font-medium">{r.name}</span>
                <span className="block text-xs text-muted-foreground">
                  {dateText(r.created_at)} · {r.triggered_by} ·{" "}
                  {r.status === "done" ? `${r.asset_total} 台` : `进行中 ${r.assets_done}/${r.asset_total}`}
                </span>
                {r.status === "done" ? (
                  <span className="mt-1 block">
                    <Counts c={r.counts} />
                  </span>
                ) : null}
              </button>
            </li>
          ))}
        </ul>
      </Card>
      {open ? <RunDetail id={open} /> : null}
    </div>
  )
}

function RunDetail({ id }: { id: string }) {
  const [verdict, setVerdict] = useState("")
  const run = usePoll<Run & { results: Result[] }>(`/api/command-inspection/runs/${id}${query({ verdict })}`, 5000)
  const [output, setOutput] = useState<string>("")
  if (run.error)
    return (
      <InlineNotice tone="bad" role="alert">
        {run.error}
      </InlineNotice>
    )
  if (!run.data) return <p className="text-muted-foreground">正在读取…</p>
  const r = run.data
  return (
    <Card title={`${r.name}（${r.template_name}）`}>
      <div className="mb-2 flex flex-wrap items-end gap-3">
        <p className="text-xs text-muted-foreground">
          {dateText(r.created_at)} 开始 · {r.finished_at ? `${dateText(r.finished_at)} 完成` : `进行中 ${r.assets_done}/${r.asset_total} 台`}
        </p>
        <Select
          id="ir-verdict"
          label="判定"
          value={verdict}
          onChange={setVerdict}
          options={[
            { value: "", label: "全部" },
            { value: "abnormal", label: "异常" },
            { value: "error", label: "未执行" },
            { value: "unknown", label: "无法判断" },
            { value: "ok", label: "正常" },
          ]}
          plain
        />
      </div>
      {r.status === "done" ? <Counts c={r.counts} /> : null}
      <div className="mt-2 min-w-0 overflow-x-auto">
        <table className="data-table w-full min-w-[40rem]">
          <caption className="sr-only">巡检结果明细</caption>
          <thead>
            <tr>
              <th>设备</th>
              <th>检查项</th>
              <th>判定</th>
              <th>说明</th>
              <th>输出</th>
            </tr>
          </thead>
          <tbody>
            {r.results.map((x) => (
              <tr key={x.id} className="border-t align-top">
                <td className="whitespace-nowrap">{x.asset_name}</td>
                <td>{x.item_name}</td>
                <td>
                  <Badge tone={x.verdict === "abnormal" && x.severity !== "critical" ? (x.severity === "info" ? "info" : "warn") : VERDICT[x.verdict].tone} className="whitespace-nowrap">
                    {VERDICT[x.verdict].label}
                    {x.verdict === "abnormal" && x.severity ? ` · ${SEVERITY[x.severity]}` : ""}
                  </Badge>
                </td>
                <td className="max-w-[24rem] text-xs">{x.detail}</td>
                <td>
                  {x.item_key ? (
                    <Button size="xs" variant="ghost" onClick={() => setOutput(x.id)}>
                      查看
                    </Button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {output ? <OutputDialog id={output} onClose={() => setOutput("")} /> : null}
    </Card>
  )
}

function OutputDialog({ id, onClose }: { id: string; onClose: () => void }) {
  const res = usePoll<ResultFull>(`/api/command-inspection/results/${id}`, 600000)
  const d = res.data
  return (
    <Dialog.Root open onOpenChange={(o) => (!o ? onClose() : undefined)}>
      <ModalContent width={820}>
        <ModalHeader title={d ? `${d.asset_name} · ${d.item_name}` : "命令输出"} description={d ? `命令：${d.command}` : ""} />
        {res.error ? (
          <InlineNotice tone="bad" role="alert">
            {res.error}
          </InlineNotice>
        ) : null}
        {d ? (
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <section aria-label="本次输出" className="min-w-0">
              <p className="text-xs text-muted-foreground">
                本次 · {dateText(d.observed_at)} · {VERDICT[d.verdict].label}
                {d.exit_code != null ? ` · 退出码 ${d.exit_code}` : ""}
              </p>
              <pre className="mt-1 max-h-80 overflow-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap">{d.output || "（没有输出）"}</pre>
            </section>
            <section aria-label="上一次输出" className="min-w-0">
              <p className="text-xs text-muted-foreground">
                {d.previous ? `上一次 · ${dateText(d.previous.observed_at)} · ${VERDICT[d.previous.verdict].label}` : "没有上一次的结果"}
              </p>
              {d.previous ? (
                <pre className="mt-1 max-h-80 overflow-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap">{d.previous.output || "（没有输出）"}</pre>
              ) : null}
            </section>
          </div>
        ) : null}
        <ModalFooter>
          <Dialog.Close asChild>
            <Button variant="outline">关闭</Button>
          </Dialog.Close>
        </ModalFooter>
      </ModalContent>
    </Dialog.Root>
  )
}

// ---- 巡检模板 ----

function TemplatesTab() {
  const { user } = useAuth()
  const admin = user?.role === "admin"
  const templates = usePoll<TemplateList>("/api/command-inspection/templates", 60000)
  const [view, setView] = useState<Template | null>(null)
  const [editing, setEditing] = useState<{ base: Template | null; copy: boolean } | null>(null)
  const [error, setError] = useState("")
  return (
    <div className="min-w-0 space-y-4 px-6">
      <div className="flex flex-wrap items-center gap-2">
        {admin ? <Button onClick={() => setEditing({ base: null, copy: false })}>新建自定义模板</Button> : null}
        <p className="text-xs text-muted-foreground">
          内置模板只读、平台维护，都还没有经过实机验证；可以复制后修改。自定义模板只有管理员能建改，保存时拦截删除、重启、写文件、改配置等危险命令。
        </p>
      </div>
      {error || templates.error ? (
        <InlineNotice tone="bad" role="alert">
          {error || templates.error}
        </InlineNotice>
      ) : null}
      <div className="grid grid-cols-[minmax(0,1fr)] gap-3 md:grid-cols-2">
        {(templates.data?.items ?? []).map((t) => (
          <section key={t.id} aria-label={`模板 ${t.name}`} className={cn(panelClass, "p-3")}>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-[0.875rem] font-semibold">{t.name}</h2>
              <Badge tone={t.builtin ? "info" : "neutral"}>{t.builtin ? "内置" : "自定义"}</Badge>
              <span className="text-xs text-muted-foreground">
                {t.platform_label} · {t.item_count} 项
              </span>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">{t.description || "（没有说明）"}</p>
            <div className="mt-2 flex flex-wrap gap-1">
              <Button size="xs" variant="outline" onClick={() => setView(t)}>
                查看检查项
              </Button>
              {admin ? (
                <Button size="xs" variant="ghost" onClick={() => setEditing({ base: t, copy: true })}>
                  复制为自定义模板
                </Button>
              ) : null}
              {admin && !t.builtin ? (
                <>
                  <Button size="xs" variant="ghost" onClick={() => setEditing({ base: t, copy: false })}>
                    编辑
                  </Button>
                  <Button
                    size="xs"
                    variant="ghost"
                    onClick={async () => {
                      if (!window.confirm(`删除模板“${t.name}”？`)) return
                      setError("")
                      try {
                        await api.del(`/api/command-inspection/templates/${t.id}`)
                        templates.refresh()
                      } catch (e) {
                        setError(errorText(e))
                      }
                    }}
                  >
                    删除
                  </Button>
                </>
              ) : null}
            </div>
          </section>
        ))}
      </div>
      {view ? <TemplateView t={view} onClose={() => setView(null)} /> : null}
      {editing ? (
        <TemplateEditor
          base={editing.base}
          copy={editing.copy}
          platforms={templates.data?.platforms ?? {}}
          onClose={() => setEditing(null)}
          onDone={() => {
            setEditing(null)
            templates.refresh()
          }}
        />
      ) : null}
    </div>
  )
}

function TemplateView({ t, onClose }: { t: Template; onClose: () => void }) {
  return (
    <Dialog.Root open onOpenChange={(o) => (!o ? onClose() : undefined)}>
      <ModalContent width={820}>
        <ModalHeader title={t.name} description={`${t.platform_label} · ${t.description}`} />
        <div className="mt-3 max-h-[60vh] min-w-0 overflow-auto">
          <table className="data-table w-full min-w-[40rem]">
            <caption className="sr-only">检查项</caption>
            <thead>
              <tr>
                <th>检查项</th>
                <th>命令</th>
                <th>判定</th>
              </tr>
            </thead>
            <tbody>
              {t.items.map((i) => (
                <tr key={i.key} className="border-t align-top">
                  <td>{i.name}</td>
                  <td>
                    <code className="text-xs break-all">{i.command}</code>
                  </td>
                  <td className="text-xs">
                    {checkText(i.check)}
                    {i.check.type !== "threshold" && i.check.type !== "record" ? ` · 异常记为${SEVERITY[i.severity]}` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <ModalFooter>
          <Dialog.Close asChild>
            <Button variant="outline">关闭</Button>
          </Dialog.Close>
        </ModalFooter>
      </ModalContent>
    </Dialog.Root>
  )
}

const BLANK: Item = { key: "", name: "", command: "", check: { type: "record" }, severity: "warning", timeout: 30 }

function TemplateEditor({
  base,
  copy,
  platforms,
  onClose,
  onDone,
}: {
  base: Template | null
  copy: boolean
  platforms: Record<string, string>
  onClose: () => void
  onDone: () => void
}) {
  const [name, setName] = useState(base ? (copy ? `${base.name}（自定义）` : base.name) : "")
  const [platform, setPlatform] = useState(base?.platform ?? "linux")
  const [description, setDescription] = useState(copy ? "" : (base?.description ?? ""))
  const [items, setItems] = useState<Item[]>(base ? base.items.map((i) => ({ ...i, check: { ...i.check } })) : [{ ...BLANK }])
  const [confirm, setConfirm] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const update = (n: number, patch: Partial<Item>) => setItems((cur) => cur.map((x, i) => (i === n ? { ...x, ...patch } : x)))
  const updateCheck = (n: number, patch: Partial<Check>) => setItems((cur) => cur.map((x, i) => (i === n ? { ...x, check: { ...x.check, ...patch } } : x)))
  async function submit() {
    setBusy(true)
    setError("")
    const body = { name, platform, description, items, confirm, revision: !copy && base ? base.revision : undefined }
    try {
      if (base && !copy) await api.put(`/api/command-inspection/templates/${base.id}`, body)
      else await api.post("/api/command-inspection/templates", body)
      onDone()
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog.Root open onOpenChange={(o) => (!o ? onClose() : undefined)}>
      <ModalContent width={900}>
        <ModalHeader
          title={base && !copy ? `编辑模板：${base.name}` : "新建自定义模板"}
          description="只能写只读命令：删除、重启、写文件、改服务或权限、装卸软件、进入交换机配置模式等危险命令会被拒绝保存。命令执行时用 SSH 端点的账号权限。"
        />
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <TextField id="it-name" label="模板名称" value={name} onChange={setName} plain />
          <Select
            id="it-platform"
            label="适用平台"
            value={platform}
            onChange={setPlatform}
            options={Object.entries(platforms).map(([value, label]) => ({ value, label }))}
            help="inspection.platform"
          />
          <TextField id="it-desc" label="说明" value={description} onChange={setDescription} className="sm:col-span-2" plain />
        </div>
        <div className="mt-3 max-h-[45vh] space-y-3 overflow-y-auto pr-1" aria-label="检查项">
          {items.map((it, n) => (
            <fieldset key={n} className="rounded-md border p-2">
              <legend className="px-1 text-xs font-medium text-muted-foreground">检查项 {n + 1}</legend>
              <div className="grid gap-2 sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)]">
                <TextField id={`it-${n}-name`} label="名称" value={it.name} onChange={(v) => update(n, { name: v })} plain />
                <TextField id={`it-${n}-cmd`} label="命令" value={it.command} onChange={(v) => update(n, { command: v })} help="inspection.command" />
                <Select
                  id={`it-${n}-type`}
                  label="判定方式"
                  value={it.check.type}
                  onChange={(v) => updateCheck(n, { type: v })}
                  options={Object.entries(CHECK_LABEL).map(([value, label]) => ({ value, label }))}
                  help="inspection.check"
                />
                {["regex", "not_regex", "threshold"].includes(it.check.type) ? (
                  <TextField
                    id={`it-${n}-pattern`}
                    label={it.check.type === "threshold" ? "取数值的正则（第一个括号）" : "正则表达式"}
                    value={it.check.pattern ?? ""}
                    onChange={(v) => updateCheck(n, { pattern: v })}
                    plain
                  />
                ) : (
                  <span />
                )}
                {it.check.type === "threshold" ? (
                  <div className="flex flex-wrap gap-2 sm:col-span-2">
                    <Select
                      id={`it-${n}-op`}
                      label="方向"
                      value={it.check.op ?? "gte"}
                      onChange={(v) => updateCheck(n, { op: v as "gte" | "lte" })}
                      options={[
                        { value: "gte", label: "越大越差（≥）" },
                        { value: "lte", label: "越小越差（≤）" },
                      ]}
                      plain
                    />
                    <TextField id={`it-${n}-warn`} label="警告值" type="number" value={String(it.check.warning ?? "")} onChange={(v) => updateCheck(n, { warning: v === "" ? null : Number(v) })} plain />
                    <TextField id={`it-${n}-crit`} label="严重值（可选）" type="number" value={String(it.check.critical ?? "")} onChange={(v) => updateCheck(n, { critical: v === "" ? null : Number(v) })} plain />
                  </div>
                ) : it.check.type !== "record" ? (
                  <Select
                    id={`it-${n}-sev`}
                    label="异常时记为"
                    value={it.severity}
                    onChange={(v) => update(n, { severity: v as Severity })}
                    options={[
                      { value: "critical", label: "严重（生成告警）" },
                      { value: "warning", label: "警告" },
                      { value: "info", label: "提示" },
                    ]}
                    help="inspection.severity"
                  />
                ) : null}
              </div>
              <Button size="xs" variant="ghost" className="mt-1" onClick={() => setItems((cur) => cur.filter((_, i) => i !== n))} disabled={items.length <= 1}>
                删除这一项
              </Button>
            </fieldset>
          ))}
          <Button size="sm" variant="outline" onClick={() => setItems((cur) => [...cur, { ...BLANK }])} disabled={items.length >= 50}>
            添加检查项
          </Button>
        </div>
        <TextField
          id="it-confirm"
          label={`输入模板名称“${name}”确认保存`}
          value={confirm}
          onChange={setConfirm}
          className="mt-3"
          help="inspection.confirm"
        />
        {error ? (
          <InlineNotice tone="bad" role="alert" className="mt-3">
            {error}
          </InlineNotice>
        ) : null}
        <ModalFooter>
          <Dialog.Close asChild>
            <Button variant="outline">取消</Button>
          </Dialog.Close>
          <Button disabled={busy || !name.trim() || confirm.trim() !== name.trim()} onClick={() => void submit()}>
            {busy ? "正在保存…" : "保存模板"}
          </Button>
        </ModalFooter>
      </ModalContent>
    </Dialog.Root>
  )
}
