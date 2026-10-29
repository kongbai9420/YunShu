import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Download, FileUp, RotateCcw, Upload } from "lucide-react"

import { PageHeader } from "@/components/AppShell"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Card, InlineNotice, Segmented, Select } from "@/components/ui/controls"
import { api } from "@/lib/api"
import { canEdit, seesAll, useAuth } from "@/lib/auth"
import { ScopeNotice } from "@/components/ScopeNote"

interface TemplateInfo {
  kind: string
  label: string
  headers: string[]
}

interface ImportItem {
  row_no: number
  asset_no: string
  name: string
  action: string
  messages: string[]
  asset_id: string | null
  undo_result: string
}

interface Batch {
  id: string
  template_kind: string
  filename: string
  status: "preview" | "applied" | "undone"
  created_at: string
  created_by: string
  applied_at: string | null
  undone_at: string | null
  summary: { mapping?: Record<string, string | null>; preview?: Record<string, number>; result?: Record<string, number>; undo?: Record<string, number> }
  items?: ImportItem[]
}

type Tone = "ok" | "warn" | "bad" | "info" | "neutral"
const ACTION: Record<string, { label: string; tone: Tone }> = {
  create: { label: "新增", tone: "ok" },
  update: { label: "更新", tone: "info" },
  unchanged: { label: "无变化", tone: "neutral" },
  conflict: { label: "冲突", tone: "warn" },
  invalid: { label: "无效", tone: "bad" },
  created: { label: "已新增", tone: "ok" },
  updated: { label: "已更新", tone: "info" },
  skipped: { label: "已跳过", tone: "bad" },
}
const UNDO: Record<string, string> = {
  removed: "已撤销（删除）",
  restored: "已撤销（恢复原值）",
  kept_modified: "未撤销：导入后已被修改",
  gone: "资产已不存在",
}
const FIELD_LABEL: Record<string, string> = {
  asset_no: "资产编号", name: "名称", role: "类型", form: "形态", platform: "平台", vendor: "品牌", model: "型号",
  serial_no: "序列号", address: "管理地址", port: "端口", protocol: "协议", site: "场所", tags: "标签",
  collection_profile: "采集模板", credential_alias: "凭据别名", related_asset_no: "关联资产编号", notes: "备注",
}
const STATUS: Record<Batch["status"], string> = { preview: "预览", applied: "已提交", undone: "已撤销" }

function when(iso: string | null): string {
  if (!iso) return ""
  return new Date(iso + (iso.endsWith("Z") || iso.includes("+") ? "" : "Z")).toLocaleString()
}

export function ImportPage() {
  const { user } = useAuth()
  const editable = canEdit(user)
  const [templates, setTemplates] = useState<TemplateInfo[]>([])
  const [kind, setKind] = useState("physical_server")
  const [file, setFile] = useState<File | null>(null)
  const [batch, setBatch] = useState<Batch | null>(null)
  const [history, setHistory] = useState<Batch[]>([])
  const [filter, setFilter] = useState("all")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirmUndo, setConfirmUndo] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const loadHistory = useCallback(() => {
    api.get<{ items: Batch[] }>("/api/import/batches").then((r) => setHistory(r.items)).catch(() => setHistory([]))
  }, [])

  useEffect(() => {
    api.get<TemplateInfo[]>("/api/import/templates").then(setTemplates).catch((e: Error) => setError(e.message))
    loadHistory()
  }, [loadHistory])

  const tpl = templates.find((t) => t.kind === kind)

  async function run(fn: () => Promise<Batch>) {
    setBusy(true)
    setError(null)
    try {
      const b = await fn()
      setBatch(b)
      setFilter("all")
      setConfirmUndo(false)
      loadHistory()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const preview = () =>
    run(() => {
      const form = new FormData()
      form.set("kind", kind)
      form.set("file", file as File)
      return api.upload<Batch>("/api/import/preview", form)
    })
  const commit = () => batch && run(() => api.post<Batch>(`/api/import/${batch.id}/commit`))
  const undo = () => batch && run(() => api.post<Batch>(`/api/import/${batch.id}/undo`))
  const open = (id: string) => run(() => api.get<Batch>(`/api/import/${id}`))

  const counts = useMemo(() => {
    const c: Record<string, number> = {}
    for (const i of batch?.items ?? []) c[i.action] = (c[i.action] ?? 0) + 1
    return c
  }, [batch])
  const rows = (batch?.items ?? []).filter((i) => filter === "all" || i.action === filter)
  const actionable = (counts.create ?? 0) + (counts.update ?? 0)

  if (!seesAll(user))
    // 批量导入会产生或更新任意场所的资产（4.1 P11）：页面保留并说明原因
    return (
      <div className="space-y-4 pb-8">
        <PageHeader title="批量导入" description="下载模板填写后上传，预览确认后提交。" />
        <div className="px-6"><ScopeNotice what="批量导入会新增或更新任意场所的资产，" /></div>
      </div>
    )
  return (
    <div className="space-y-4 pb-8">
      <PageHeader
        title="批量导入"
        description="下载模板填写后上传：先预览每一行会新增、更新还是有问题，确认后再提交；提交后整批可撤销。资产编号是唯一的去重键，不按 IP 合并；模板里不填密码，用“凭据别名”引用已保存的凭据。"
      />

      <div className="grid grid-cols-1 gap-4 px-6 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        <div className="min-w-0 space-y-4">
          <Card title="1. 下载模板">
            <div className="space-y-3">
              <Select
                id="tpl-kind"
                label="模板类型"
                hint="按设备类别下载，各类别的列与默认协议不同"
                value={kind}
                onChange={setKind}
                options={templates.map((t) => ({ value: t.kind, label: t.label }))}
              />
              {tpl ? <p className="text-xs break-words text-muted-foreground">列：{tpl.headers.join("、")}（* 为必填）</p> : null}
              <div className="flex flex-wrap gap-2">
                <Button asChild variant="outline" size="sm">
                  <a href={`/api/import/templates/${kind}.xlsx`} download>
                    <Download aria-hidden />
                    Excel 模板
                  </a>
                </Button>
                <Button asChild variant="outline" size="sm">
                  <a href={`/api/import/templates/${kind}.csv`} download>
                    <Download aria-hidden />
                    CSV 模板
                  </a>
                </Button>
              </div>
            </div>
          </Card>

          <Card title="2. 上传并预览">
            {editable ? (
              <div className="space-y-3">
                <p className="text-xs text-muted-foreground">支持 .xlsx 与 .csv（UTF-8 或 GBK），最大 5 MB、5000 行；含公式的单元格整行判为无效。</p>
                <input
                  ref={inputRef}
                  id="import-file"
                  type="file"
                  data-help-plain=""
                  accept=".csv,.xlsx"
                  className="sr-only"
                  onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                />
                <div className="flex flex-wrap items-center gap-2">
                  <Button variant="outline" size="sm" onClick={() => inputRef.current?.click()}>
                    <FileUp aria-hidden />
                    选择文件
                  </Button>
                  <span className="min-w-0 truncate text-[0.8125rem]">{file ? file.name : "未选择文件"}</span>
                </div>
                <Button size="sm" className="h-auto max-w-full whitespace-normal py-2" onClick={() => void preview()} disabled={!file || busy}>
                  <Upload aria-hidden />
                  {busy ? "正在处理…" : `按“${tpl?.label ?? ""}”模板预览`}
                </Button>
              </div>
            ) : (
              <p className="text-[0.8125rem] text-muted-foreground">只读角色可以下载模板和查看导入记录，不能导入。</p>
            )}
          </Card>

          <Card title="导入记录">
            {history.length === 0 ? (
              <p className="text-[0.8125rem] text-muted-foreground">还没有提交过的导入。</p>
            ) : (
              <ul className="space-y-1">
                {history.map((h) => (
                  <li key={h.id}>
                    <button
                      type="button"
                      onClick={() => void open(h.id)}
                      className="flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-[0.8125rem] hover:bg-accent"
                    >
                      <span className="min-w-0">
                        <span className="block truncate font-medium">{h.filename || "未命名文件"}</span>
                        <span className="text-xs text-muted-foreground">
                          {when(h.applied_at ?? h.created_at)} · {h.created_by}
                        </span>
                      </span>
                      <Badge tone={h.status === "undone" ? "neutral" : "ok"}>{STATUS[h.status]}</Badge>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        <div className="min-w-0 space-y-3">
          {error ? (
            <InlineNotice tone="bad" role="alert">
              {error}
            </InlineNotice>
          ) : null}
          {!batch ? (
            <Card>
              <p className="py-10 text-center text-[0.8125rem] text-muted-foreground">上传文件后，这里会逐行显示预览结果。</p>
            </Card>
          ) : (
            <Card
              title={`${batch.filename || "导入批次"} · ${STATUS[batch.status]}`}
              actions={
                editable ? (
                  batch.status === "preview" ? (
                    <Button size="sm" onClick={() => void commit()} disabled={busy || actionable === 0}>
                      确认导入 {actionable} 行
                    </Button>
                  ) : batch.status === "applied" ? (
                    confirmUndo ? (
                      <>
                        <span className="text-xs">只撤销导入后没被改过的行。</span>
                        <Button size="sm" variant="destructive" onClick={() => void undo()} disabled={busy}>
                          确认撤销
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => setConfirmUndo(false)}>
                          取消
                        </Button>
                      </>
                    ) : (
                      <Button size="sm" variant="outline" onClick={() => setConfirmUndo(true)}>
                        <RotateCcw aria-hidden />
                        撤销整批
                      </Button>
                    )
                  ) : null
                ) : null
              }
            >
              <div className="space-y-3">
                {batch.status === "preview" ? (
                  <InlineNotice>
                    这是预览，资产还没有改动。无效和冲突的行提交时会跳过；预览后如果有人改了同一资产，该行提交时会标为冲突、不覆盖。
                  </InlineNotice>
                ) : null}
                {batch.summary.mapping ? (
                  <p className="text-xs break-words text-muted-foreground">
                    列识别：
                    {Object.entries(batch.summary.mapping)
                      .map(([h, f]) => `${h} → ${f ? (FIELD_LABEL[f] ?? f) : "忽略"}`)
                      .join("；")}
                  </p>
                ) : null}
                <Segmented
                  label="按结果筛选"
                  value={filter}
                  onChange={setFilter}
                  options={[
                    { value: "all", label: "全部", count: batch.items?.length ?? 0 },
                    ...Object.entries(counts).map(([a, n]) => ({ value: a, label: ACTION[a]?.label ?? a, count: n })),
                  ]}
                />
                <div className="overflow-x-auto rounded-lg border">
                  <table className="data-table w-full min-w-[640px]">
                    <caption className="sr-only">逐行结果</caption>
                    <thead>
                      <tr>
                        <th scope="col">行</th>
                        <th scope="col">资产编号</th>
                        <th scope="col">名称</th>
                        <th scope="col">结果</th>
                        <th scope="col">说明</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((i) => (
                        <tr key={i.row_no} className="align-top">
                          <td className="tabular">{i.row_no}</td>
                          <td>{i.asset_no || <span className="text-muted-foreground">空</span>}</td>
                          <td>{i.name}</td>
                          <td className="whitespace-nowrap">
                            <Badge tone={ACTION[i.action]?.tone ?? "neutral"}>{ACTION[i.action]?.label ?? i.action}</Badge>
                            {i.undo_result ? <div className="mt-1 text-xs text-muted-foreground">{UNDO[i.undo_result] ?? i.undo_result}</div> : null}
                          </td>
                          <td>
                            {/* 提交后“将新建场所”改成过去时 */}
                            {(batch.status === "preview" ? i.messages : i.messages.map((m) => m.replace(/^将新建场所/, "新建场所"))).join("；")}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {batch.status !== "preview" ? (
                  <Button asChild variant="ghost" size="sm">
                    <a href={`/api/import/${batch.id}/results.csv`} download>
                      <Download aria-hidden />
                      导出逐行结果
                    </a>
                  </Button>
                ) : null}
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}
