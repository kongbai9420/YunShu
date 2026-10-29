import { InlineNotice } from "@/components/ui/controls"
import { type Page } from "@/lib/api"
import { dateText, numberText, usePoll } from "@/lib/monitor"
import { type Alert, severityLabels, alertStateLabel } from "./shared"

export function AssetAlerts({ assetId }: { assetId: string }) {
  const result = usePoll<Page<Alert>>(`/api/alerts?asset_id=${encodeURIComponent(assetId)}&limit=100`, 5000)
  return (
    <div className="min-w-0 space-y-3">
      <p className="text-sm text-muted-foreground">
        本资产的当前告警与最近状态；
        <a className="text-primary hover:underline" href="#/alerts">
          进入告警中心
        </a>
        确认、静默或查看完整历史。
      </p>
      {result.error ? (
        <InlineNotice tone="bad" role="alert">
          {result.error}
        </InlineNotice>
      ) : null}
      {result.data?.total === 0 ? <InlineNotice>本资产尚无告警记录。</InlineNotice> : null}
      <ul className="divide-y">
        {result.data?.items.map((a) => (
          <li key={a.id} className="space-y-1 py-3 text-sm">
            <p className="font-medium">{a.title}</p>
            <p>
              {alertStateLabel(a)} · {severityLabels[a.severity]} · {numberText(a.value)} {a.unit}
            </p>
            {a.suppression_reason ? (
              <p className="text-xs text-muted-foreground">说明：{a.suppression_reason}</p>
            ) : null}
            <p className="text-xs text-muted-foreground">最近变化 {dateText(a.last_seen_at)}</p>
          </li>
        ))}
      </ul>
      {(result.data?.total ?? 0) > 100 ? (
        <p className="text-xs text-muted-foreground">显示最近 100 项，更多记录请进入告警中心。</p>
      ) : null}
    </div>
  )
}
