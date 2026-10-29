import { useCallback, useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { CheckField, TextField } from "@/components/ui/form"
import { ApiError, api } from "@/lib/api"
import { dateText } from "@/lib/monitor"

/** 漏洞通知设置（4.3 P18e）：即时通知规则固定为“命中本地且已被利用或 CVSS ≥ 9.0”，这里选渠道、汇总窗口与每日汇总时间。 */

interface Settings {
  online_enabled: boolean; aggregation_minutes: number; notify_enabled: boolean; notify_channel_ids: string[]; digest_hour: number; early_warning: boolean
  last_digest_at: string | null; revision: number
}
interface Channel { id: string; name: string; kind: string; enabled: boolean }

export function NotifyTab({ editable }: { editable: boolean }) {
  const [s, setS] = useState<Settings | null>(null)
  const [channels, setChannels] = useState<Channel[]>([])
  const [msg, setMsg] = useState("")
  const [error, setError] = useState("")
  const load = useCallback(() => {
    void api.get<Settings>("/api/vuln/settings").then(setS, (e: Error) => setError(e.message))
    void api.get<Channel[]>("/api/alerts/channels").then(setChannels, () => setChannels([]))
  }, [])
  useEffect(load, [load])
  if (!s) return error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : <p className="text-[0.8125rem] text-muted-foreground">正在加载…</p>

  async function save() {
    if (!s) return
    setError("")
    setMsg("")
    try {
      setS(await api.put<Settings>("/api/vuln/settings", {
        online_enabled: s.online_enabled, aggregation_minutes: s.aggregation_minutes, notify_enabled: s.notify_enabled,
        notify_channel_ids: s.notify_channel_ids, digest_hour: s.digest_hour, early_warning: s.early_warning, revision: s.revision,
      }))
      setMsg("已保存")
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "保存失败")
    }
  }

  async function digestNow() {
    setError("")
    try {
      const r = await api.post<{ sent: boolean; new?: number; hits?: number }>("/api/vuln/digest-now")
      setMsg(r.sent ? `已发送每日汇总（新漏洞 ${r.new ?? 0} 个，新命中 ${r.hits ?? 0} 个）` : "没有可发送的通知渠道")
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "发送失败")
    }
  }

  const enabledChannels = channels.filter((c) => c.enabled)
  return (
    <div className="max-w-3xl space-y-4 text-[0.8125rem]">
      <InlineNotice>
        即时通知：本地资产命中、并且已被利用（CISA KEV）或 CVSS ≥ 9.0 的漏洞，每个漏洞一条消息，列出受影响资产和发布来源。
        拉到新数据或收到推送后立即检查，不等定时。
        每日汇总：订阅范围内的新漏洞、新命中与待处理数量。处于维护窗口的资产、已处置为不受影响或误报的不通知。
      </InlineNotice>
      <CheckField label="发送漏洞通知" checked={s.notify_enabled} onChange={(v) => setS({ ...s, notify_enabled: v })} disabled={!editable} help="vuln.notify" />
      <CheckField label="订阅命中预警（版本数据到之前先发待确认）" checked={s.early_warning} onChange={(v) => setS({ ...s, early_warning: v })} disabled={!editable || !s.notify_enabled} help="vuln.early" />
      <div className="grid gap-3 sm:grid-cols-2">
        <TextField id="vuln-agg" label="汇总窗口（分钟）" type="number" min={0} max={1440} value={String(s.aggregation_minutes)} disabled={!editable}
          onChange={(v) => setS({ ...s, aggregation_minutes: Number(v) || 0 })} help="vuln.aggregation" />
        <TextField id="vuln-digest-hour" label="每日汇总时间（点）" type="number" min={0} max={23} value={String(s.digest_hour)} disabled={!editable}
          onChange={(v) => setS({ ...s, digest_hour: Math.min(23, Math.max(0, Number(v) || 0)) })} hint="按平台时区，0～23" />
      </div>
      <fieldset className="space-y-1">
        <legend className="text-xs font-medium text-muted-foreground">通知渠道（都不勾选表示所有启用的渠道）</legend>
        {enabledChannels.length ? enabledChannels.map((c) => (
          <CheckField key={c.id} label={`${c.name}（${c.kind}）`} checked={s.notify_channel_ids.includes(c.id)} disabled={!editable}
            onChange={(v) => setS({ ...s, notify_channel_ids: v ? [...s.notify_channel_ids, c.id] : s.notify_channel_ids.filter((x) => x !== c.id) })}
            hint="与告警共用渠道配置" />
        )) : <p className="text-xs text-muted-foreground">还没有启用的通知渠道，请先在“告警中心 → 通知渠道”里配置。</p>}
      </fieldset>
      {s.last_digest_at ? <p className="text-xs text-muted-foreground">上次每日汇总：{dateText(s.last_digest_at)}</p> : null}
      {editable ? (
        <div className="flex gap-2">
          <Button onClick={() => void save()}>保存</Button>
          <Button variant="outline" onClick={() => void digestNow()}>立即发送一次汇总</Button>
        </div>
      ) : null}
      {msg ? <InlineNotice tone="ok">{msg}</InlineNotice> : null}
      {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
    </div>
  )
}
