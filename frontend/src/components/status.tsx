import {
  Ban,
  Calculator,
  CircleCheck,
  CircleDashed,
  CircleOff,
  CircleX,
  Clock,
  OctagonAlert,
  TriangleAlert,
  Unplug,
  type LucideIcon,
} from "lucide-react"

import { HEALTH_LABEL, QUALITY_LABEL } from "@/mock/labels"
import type { Health, Metric, Quality } from "@/mock/types"
import { cn } from "@/lib/utils"

type Tone = "ok" | "warn" | "bad" | "info" | "manual" | "neutral"

const TONE_CLASS: Record<Tone, string> = {
  ok: "bg-ok-bg text-foreground [&_svg]:text-ok",
  warn: "bg-warn-bg text-foreground [&_svg]:text-warn",
  bad: "bg-bad-bg text-foreground [&_svg]:text-bad",
  info: "bg-info-bg text-foreground [&_svg]:text-info",
  manual: "bg-manual-bg text-foreground [&_svg]:text-manual",
  neutral: "bg-neutral-bg text-foreground [&_svg]:text-neutral",
}

/** 状态徽标：图标 + 文字 + 浅底，颜色从不单独承载含义 */
export function Badge({
  tone,
  icon: Icon,
  children,
  className,
}: {
  tone: Tone
  icon?: LucideIcon
  children: React.ReactNode
  className?: string
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        TONE_CLASS[tone],
        className,
      )}
    >
      {Icon ? <Icon aria-hidden className="size-3.5 shrink-0" /> : null}
      {children}
    </span>
  )
}

const HEALTH_META: Record<Health, { tone: Tone; icon: LucideIcon }> = {
  ok: { tone: "ok", icon: CircleCheck },
  warning: { tone: "warn", icon: TriangleAlert },
  critical: { tone: "bad", icon: OctagonAlert },
  offline: { tone: "neutral", icon: Unplug },
  unknown: { tone: "neutral", icon: CircleDashed },
}

export function HealthBadge({ health }: { health: Health }) {
  const meta = HEALTH_META[health]
  return (
    <Badge tone={meta.tone} icon={meta.icon}>
      {HEALTH_LABEL[health]}
    </Badge>
  )
}

const QUALITY_META: Record<Quality, { tone: Tone; icon: LucideIcon }> = {
  valid: { tone: "ok", icon: CircleCheck },
  stale: { tone: "warn", icon: Clock },
  unsupported: { tone: "neutral", icon: Ban },
  unconfigured: { tone: "neutral", icon: CircleOff },
  failed: { tone: "bad", icon: CircleX },
  estimated: { tone: "info", icon: Calculator },
}

export function QualityBadge({ quality }: { quality: Quality }) {
  const meta = QUALITY_META[quality]
  return (
    <Badge tone={meta.tone} icon={meta.icon}>
      {QUALITY_LABEL[quality]}
    </Badge>
  )
}

export function formatAge(observedAt: number | null, now = Date.now() / 1000): string {
  if (observedAt === null) return "从未采到"
  const s = Math.max(0, Math.round(now - observedAt))
  if (s < 60) return `${s} 秒前`
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`
  return `${Math.round(s / 3600)} 小时前`
}

/**
 * 指标值：只有“有效”和“估算”显示数字（估算带 ≈）；过期值显示上次读数并标注过期；
 * 其余状态显示状态文字，绝不显示成 0。
 */
export function MetricValue({ metric, compact = false }: { metric: Metric; compact?: boolean }) {
  const { value, unit, quality } = metric
  const hasNumber = value !== null && (quality === "valid" || quality === "estimated" || quality === "stale")
  if (!hasNumber) {
    return compact ? (
      <span className="text-muted-foreground">
        <span aria-hidden>— </span>
        {QUALITY_LABEL[quality]}
      </span>
    ) : (
      <QualityBadge quality={quality} />
    )
  }
  const text = `${quality === "estimated" ? "≈" : ""}${value}${unit === "%" || unit === "°C" ? "" : " "}${unit}`
  if (quality === "valid") return <span className="tabular">{text}</span>
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={cn("tabular", quality === "stale" && "text-muted-foreground")}>
        {text}
      </span>
      {compact ? (
        <span className="text-xs text-muted-foreground">{QUALITY_LABEL[quality]}</span>
      ) : (
        <QualityBadge quality={quality} />
      )}
    </span>
  )
}
