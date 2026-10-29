import { HelpPill } from "@/components/HelpPill"
import type { HelpKey } from "@/lib/help"
import { cn } from "@/lib/utils"

/** 原生 select 加统一样式：键盘、读屏与系统深浅色都由浏览器保证 */
export function Select({
  label,
  value,
  onChange,
  options,
  className,
  id,
  disabled = false,
  hint,
  help,
  plain,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  options: { value: string; label: string }[]
  className?: string
  id: string
  disabled?: boolean
  /** 控件下方的一句话说明 */
  hint?: string
  /** 一句话说不清时，标签旁显示「!」胶囊（lib/help.ts） */
  help?: HelpKey
  /** 标签与选项本身就足够说明 */
  plain?: boolean
}) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)} data-field="" data-help-plain={plain ? "" : undefined}>
      <span className="flex items-center">
        <label htmlFor={id} className="text-xs font-medium text-muted-foreground">
          {label}
        </label>
        {help ? <HelpPill topic={help} /> : null}
      </span>
      <select
        id={id}
        value={value}
        disabled={disabled}
        aria-describedby={hint ? `${id}-hint` : undefined}
        onChange={(e) => onChange(e.target.value)}
        className="h-8 min-w-0 max-w-full rounded-md border border-input bg-card px-2 text-[0.8125rem] text-foreground sm:min-w-32"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      {hint ? <p id={`${id}-hint`} className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  )
}

/** 分段控件：一组互斥按钮，当前项用 aria-pressed 标出 */
export function Segmented<T extends string>({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: T
  onChange: (value: T) => void
  options: { value: T; label: string; count?: number }[]
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex flex-wrap rounded-lg bg-secondary p-0.5">
      {options.map((o) => {
        const active = o.value === value
        return (
          <button
            key={o.value}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(o.value)}
            className={cn(
              "inline-flex h-7 items-center gap-1.5 rounded-md px-3 text-[0.8125rem] font-medium",
              active ? "bg-card text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {o.label}
            {o.count !== undefined ? <span className="tabular text-xs text-muted-foreground">{o.count}</span> : null}
          </button>
        )
      })}
    </div>
  )
}

/**
 * 卡片容器的统一材质：12px 圆角、细边框、卡片底色和 --card-shadow（对照旧版 .card）。
 * 只管外观，不加内边距，可用在任意元素上（表格外框、列表项、details）；需要标题栏时用下面的 Card。
 */
export const panelClass = "rounded-lg border bg-card text-card-foreground shadow-card"

export function Card({
  title,
  actions,
  children,
  className,
}: {
  title?: React.ReactNode
  actions?: React.ReactNode
  children: React.ReactNode
  className?: string
}) {
  return (
    <section className={cn(panelClass, "flex min-h-0 flex-col", className)}>
      {title || actions ? (
        <header className="flex items-center justify-between gap-2 px-4 pt-3 pb-2">
          {title ? <h2 className="truncate text-[0.8125rem] font-semibold">{title}</h2> : <span />}
          {actions ? <div className="flex shrink-0 items-center gap-1">{actions}</div> : null}
        </header>
      ) : null}
      <div className="min-h-0 flex-1 px-4 pb-3">{children}</div>
    </section>
  )
}

export function InlineNotice({
  tone = "info",
  children,
  className,
  role = "status",
}: {
  tone?: "info" | "warn" | "bad" | "ok"
  children: React.ReactNode
  className?: string
  role?: "status" | "alert"
}) {
  const toneClass = {
    info: "bg-info-bg",
    warn: "bg-warn-bg",
    bad: "bg-bad-bg",
    ok: "bg-ok-bg",
  }[tone]
  return (
    <div role={role} className={cn("rounded-lg px-3 py-2 text-[0.8125rem] text-foreground", toneClass, className)}>
      {children}
    </div>
  )
}
