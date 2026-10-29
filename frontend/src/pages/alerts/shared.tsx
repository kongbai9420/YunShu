import { isValidElement, useState, type ComponentProps, type ReactElement, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { CheckField, LabeledField } from "@/components/ui/form"
import type { HelpKey } from "@/lib/help"
import { type Asset, type Page, query } from "@/lib/api"
import { usePoll } from "@/lib/monitor"

export type Severity = "info" | "warning" | "critical"
export interface Alert {
  id: string
  rule_id: string
  asset_id: string
  asset_name: string | null
  metric_key: string
  state: string
  severity: Severity
  title: string
  value: number | null
  unit: string
  threshold: number | null
  first_seen_at: string
  last_seen_at: string
  activated_at: string | null
  resolved_at: string | null
  acknowledged_by: string | null
  silenced_until: string | null
  suppression_reason: string | null
}
export interface Rule {
  id: string
  name: string
  enabled: boolean
  kind: "threshold" | "availability"
  metric_key: string
  comparison: "gte" | "lte"
  threshold: number
  recovery_threshold: number
  consecutive_samples: number
  duration_seconds: number
  cooldown_seconds: number
  repeat_seconds: number
  severity: Severity
  asset_ids: string[]
  tags: string[]
  channel_ids: string[]
  suppress_by_dependency: boolean
  revision: number
}
export const CHANNEL_KINDS = {
  email: "邮件 SMTP",
  custom_webhook: "自定义 Webhook",
  serverchan: "Server酱",
  dingtalk: "钉钉",
  feishu: "飞书",
  wechat_work: "企业微信",
  bark: "Bark",
  pushplus: "PushPlus",
}
export interface Channel {
  id: string
  name: string
  kind: keyof typeof CHANNEL_KINDS
  enabled: boolean
  revision: number
  has_secret: boolean
  config: {
    host?: string
    port?: number
    tls?: string
    from_address?: string
    recipients?: string[]
    subject_template?: string
    text_template?: string
    html_template?: string
    severities?: string[]
    tags?: string[]
    group_seconds?: number
  }
}
export interface Delivery {
  id: string
  channel_id: string
  channel_name: string
  status: string
  attempts: number
  error: string
  created_at: string
  finished_at: string | null
  event_ids: string[]
  is_test: boolean
}
export const severityLabels = {
  info: "提示",
  warning: "警告",
  critical: "严重",
}
export const stateLabels: Record<string, string> = {
  active: "告警中",
  pending: "待满足条件",
  acknowledged: "已确认",
  silenced: "已静默",
  resolved: "已结束",
  suppressed: "已抑制",
}
export function alertStateLabel(alert: Alert) {
  if (alert.state === "resolved") {
    if (["规则已停用或删除", "资产或指标已移出规则作用范围"].includes(alert.suppression_reason ?? ""))
      return "已关闭（规则变更）"
    return alert.activated_at ? "已恢复" : "未达到触发条件"
  }
  return stateLabels[alert.state] ?? alert.state
}
export const inputClass =
  "min-h-9 w-full min-w-0 rounded-md border border-input bg-card px-2 py-1 text-sm text-foreground"
/** 告警页的表单项：单个控件时用 LabeledField（标签、一句话说明、「!」胶囊）；plain 表示标签已足够说明 */
export function Field({ label, children, hint, help, plain }: { label: string; children: ReactNode; hint?: string; help?: HelpKey; plain?: boolean }) {
  if (isValidElement(children)) {
    return (
      <LabeledField label={label} hint={hint} help={help} plain={plain}>
        {children as ReactElement<{ id?: string }>}
      </LabeledField>
    )
  }
  return (
    <label className="flex min-w-0 flex-col gap-1 text-xs font-medium text-muted-foreground" data-field="" data-help-plain={plain ? "" : undefined}>
      {label}
      {children}
    </label>
  )
}
export function Input(props: ComponentProps<"input">) {
  return <input data-help-wrapped="" {...props} className={inputClass} />
}
export function Check({
  label,
  checked,
  onChange,
  disabled,
  help,
  hint,
  plain,
}: {
  label: string
  checked: boolean
  onChange: (value: boolean) => void
  disabled?: boolean
  help?: HelpKey
  hint?: string
  /** 勾选项文字本身已足够说明 */
  plain?: boolean
}) {
  return (
    <div data-help-plain={plain ? "" : undefined}>
      <CheckField label={label} checked={checked} onChange={onChange} disabled={disabled} help={help} hint={hint} className="min-h-9 justify-center text-sm" />
    </div>
  )
}
export function split(value: string) {
  return [
    ...new Set(
      value
        .split(/[,，\n]/)
        .map((v) => v.trim())
        .filter(Boolean),
    ),
  ]
}
export function useMutation() {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("")
  async function run(work: () => Promise<unknown>, success = "已保存") {
    if (busy) return false
    setBusy(true)
    setError("")
    setNotice("")
    try {
      await work()
      setNotice(success)
      return true
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败，请重试")
      return false
    } finally {
      setBusy(false)
    }
  }
  return {
    busy,
    run,
    feedback: (
      <>
        {error ? (
          <InlineNotice role="alert" tone="bad">
            {error}；输入内容已保留。
          </InlineNotice>
        ) : null}
        {notice ? <InlineNotice tone="ok">{notice}</InlineNotice> : null}
      </>
    ),
  }
}
export function ConfirmButton({
  label,
  message,
  onConfirm,
  disabled,
}: {
  label: string
  message: string
  onConfirm: () => Promise<unknown>
  disabled?: boolean
}) {
  const [asking, setAsking] = useState(false)
  if (!asking)
    return (
      <Button size="sm" type="button" variant="outline" disabled={disabled} onClick={() => setAsking(true)}>
        {label}
      </Button>
    )
  return (
    <div className="space-y-2 rounded-md border border-input p-2">
      <p className="text-xs">{message}</p>
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          type="button"
          disabled={disabled}
          onClick={() => void onConfirm().then(() => setAsking(false))}
        >
          确认{label}
        </Button>
        <Button size="sm" type="button" variant="outline" onClick={() => setAsking(false)}>
          取消
        </Button>
      </div>
    </div>
  )
}

/** 跨分页保存选择，不要求操作者复制内部资产 ID。 */
export function AssetChoices({
  value,
  onChange,
  disabled,
}: {
  value: string[]
  onChange: (ids: string[]) => void
  disabled?: boolean
}) {
  const [search, setSearch] = useState(""),
    [offset, setOffset] = useState(0)
  const assets = usePoll<Page<Asset>>(`/api/assets${query({ q: search, limit: 50, offset })}`, 60000)
  return (
    <div className="min-w-0 space-y-2">
      <Field label="搜索限定资产" help="alert.rule.assets">
        <Input
          value={search}
          maxLength={100}
          disabled={disabled}
          onChange={(e) => {
            setSearch(e.target.value)
            setOffset(0)
          }}
        />
      </Field>
      <p className="text-xs text-muted-foreground">
        已选择 {value.length} 台；不选则包含全部在用资产。填写标签时还需匹配标签。
      </p>
      {assets.error ? <InlineNotice tone="bad">{assets.error}</InlineNotice> : null}
      <div className="max-h-56 overflow-y-auto">
        {assets.data?.items.map((a) => (
          <Check
            key={a.id}
            plain
            label={a.name}
            disabled={disabled}
            checked={value.includes(a.id)}
            onChange={(checked) => onChange(checked ? [...value, a.id] : value.filter((id) => id !== a.id))}
          />
        ))}
      </div>
      {assets.data?.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">没有符合搜索条件的资产。</p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={disabled || offset === 0}
          onClick={() => setOffset((n) => Math.max(0, n - 50))}
        >
          上一页资产
        </Button>
        <span>共 {assets.data?.total ?? "—"} 台</span>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={disabled || !assets.data || offset + 50 >= assets.data.total}
          onClick={() => setOffset((n) => n + 50)}
        >
          下一页资产
        </Button>
        {value.length ? (
          <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => onChange([])}>
            清空资产选择
          </Button>
        ) : null}
      </div>
    </div>
  )
}
