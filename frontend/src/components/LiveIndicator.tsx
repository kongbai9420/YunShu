import { Pause, Radio, RefreshCw, Unplug } from "lucide-react"

import { Badge } from "@/components/status"
import { LIVE_STATUS_TEXT, type LiveStatus } from "@/lib/live"

/** 实时推送状态：图标 + 文字，不只靠颜色区分。 */
export function LiveIndicator({ status, fallbackText }: { status: LiveStatus; fallbackText?: string }) {
  const icon = status === "live" ? Radio : status === "paused" ? Pause : status === "connecting" ? RefreshCw : Unplug
  const tone = status === "live" ? "ok" : status === "reconnecting" ? "warn" : "neutral"
  return (
    <span role="status" aria-live="polite" className="inline-flex flex-wrap items-center gap-1.5">
      <Badge tone={tone} icon={icon}>
        {LIVE_STATUS_TEXT[status]}
      </Badge>
      {status === "reconnecting" && fallbackText ? <span className="text-xs text-muted-foreground">{fallbackText}</span> : null}
    </span>
  )
}
