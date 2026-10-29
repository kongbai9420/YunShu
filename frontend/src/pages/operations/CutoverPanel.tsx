import { useState } from "react"

import { HelpPill } from "@/components/HelpPill"
import { LabeledField } from "@/components/ui/form"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Card, InlineNotice } from "@/components/ui/controls"
import { ApiError, api } from "@/lib/api"
import { dateText, usePoll } from "@/lib/monitor"

interface Check {
  key: string
  label: string
  ok: boolean | null
  detail: string
  blocking: boolean
}
interface Cutover {
  owner: "legacy" | "new"
  revision: number
  changed_by: string
  changed_at: string | null
  note: string
  cutover_enabled: boolean
  checks: Check[]
  ready: boolean
  checklist: { key: string; label: string }[]
  confirm_new: string
  confirm_legacy: string
}

export function CutoverPanel({ isAdmin, changed }: { isAdmin: boolean; changed: () => void }) {
  // 检查会探测旧版地址，不定时轮询，手动刷新
  const info = usePoll<Cutover>("/api/control/cutover", 600000)
  const [checked, setChecked] = useState<string[]>([])
  const [confirm, setConfirm] = useState("")
  const [note, setNote] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  if (info.error)
    return (
      <InlineNotice tone="bad" role="alert">
        {info.error}
      </InlineNotice>
    )
  if (!info.data) return <p className="text-muted-foreground">正在读取控制归属…</p>
  const d = info.data
  const toNew = d.owner === "legacy"
  const phrase = toNew ? d.confirm_new : d.confirm_legacy
  const canSubmit =
    isAdmin && confirm.trim() === phrase && (!toNew || (d.ready && d.checklist.every((c) => checked.includes(c.key))))
  async function submit() {
    if (busy || !canSubmit) return
    setBusy(true)
    setError("")
    try {
      await api.post("/api/control/owner", {
        owner: toNew ? "new" : "legacy",
        revision: d.revision,
        confirm,
        checklist: checked,
        note,
      })
      setConfirm("")
      setChecked([])
      info.refresh()
      changed()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "操作失败")
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="min-w-0 space-y-4">
      <Card
        title="控制归属"
        actions={
          <Button size="xs" variant="outline" onClick={info.refresh}>
            重新检查
          </Button>
        }
      >
        <p className="flex flex-wrap items-center gap-2">
          当前：
          <Badge tone={d.owner === "new" ? "ok" : "warn"}>{d.owner === "new" ? "新平台负责控制" : "旧控制器负责控制"}</Badge>
          <span className="text-xs text-muted-foreground">
            修订 {d.revision}
            {d.changed_at ? ` · ${d.changed_by} · ${dateText(d.changed_at)}` : ""}
            {d.note ? ` · ${d.note}` : ""}
          </span>
        </p>
        <p className="mt-2 text-xs text-muted-foreground">
          新旧两套系统不能同时向同一台 BMC 发命令。切到新平台前先在旧版里把风扇切回自动并停止旧版服务；切回旧控制器后新平台立即停止下发，再按手册重新启动旧版。
        </p>
      </Card>
      <Card title="切换前检查">
        <ul className="space-y-1">
          {d.checks.map((c) => (
            <li key={c.key} className="flex flex-wrap items-center gap-2">
              <Badge tone={c.ok ? "ok" : c.ok === null ? "neutral" : c.blocking ? "bad" : "warn"}>
                {c.ok ? "通过" : c.ok === null ? "需人工确认" : c.blocking ? "未通过" : "提醒"}
              </Badge>
              <span>{c.label}</span>
              <span className="text-xs text-muted-foreground">{c.detail}</span>
            </li>
          ))}
        </ul>
        {!d.cutover_enabled && toNew ? (
          <InlineNotice tone="warn" className="mt-3">
            这套部署关闭了控制切换开关（YUNSHU_CONTROL_CUTOVER=0），不能切到新平台。需要切换时，在部署配置里去掉该设置并重启，必须先得到负责人同意。
          </InlineNotice>
        ) : null}
      </Card>
      {isAdmin ? (
        <Card title={toNew ? "切换到新平台" : "切回旧控制器（回滚）"}>
          {toNew ? (
            <fieldset className="space-y-1">
              <legend className="mb-1 inline-flex items-center text-xs font-medium text-muted-foreground">逐项确认<HelpPill topic="cutover.checklist" /></legend>
              {d.checklist.map((c) => (
                <label key={c.key} className="flex items-start gap-2 text-[0.8125rem]">
                  <input
                    type="checkbox"
                    data-help-plain=""
                    className="mt-1"
                    checked={checked.includes(c.key)}
                    onChange={(e) => setChecked(e.target.checked ? [...checked, c.key] : checked.filter((k) => k !== c.key))}
                  />
                  {c.label}
                </label>
              ))}
            </fieldset>
          ) : (
            <p className="text-[0.8125rem]">
              切回后新平台不再向设备发送任何控制命令，排队中的操作执行前会被拒绝。之后请按手册重新启动旧版服务，并在旧版里确认温控模式。
            </p>
          )}
          <LabeledField label="说明（写入审计）" help="cutover.note" className="mt-3">
            <input data-help-wrapped="" className="h-8 rounded-md border bg-card px-2 text-[0.8125rem] text-foreground" value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} />
          </LabeledField>
          <LabeledField label={`请输入“${phrase}”确认`} help="ops.confirmPhrase" className="mt-3">
            <input
              data-help-wrapped=""
              className="h-8 rounded-md border bg-card px-2 text-[0.8125rem] text-foreground"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="off"
            />
          </LabeledField>
          {error ? (
            <InlineNotice tone="bad" role="alert" className="mt-3">
              {error}
            </InlineNotice>
          ) : null}
          <Button className="mt-3" size="sm" variant={toNew ? "default" : "destructive"} disabled={!canSubmit || busy} onClick={() => void submit()}>
            {toNew ? "切换到新平台" : "切回旧控制器"}
          </Button>
        </Card>
      ) : (
        <InlineNotice>只有管理员可以切换控制归属。</InlineNotice>
      )}
    </div>
  )
}
