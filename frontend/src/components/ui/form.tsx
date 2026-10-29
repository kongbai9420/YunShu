import { cloneElement, useId } from "react"

import { HelpPill } from "@/components/HelpPill"
import type { HelpKey } from "@/lib/help"
import { cn } from "@/lib/utils"

const INPUT =
  "h-8 w-full rounded-md border border-input bg-card px-2 text-[0.8125rem] text-foreground placeholder:text-muted-foreground aria-invalid:border-bad"

/** 带标签、提示与错误文本的输入框；错误通过 aria-describedby 关联给读屏 */
export function TextField({
  id,
  label,
  value,
  onChange,
  type = "text",
  hint,
  error,
  className,
  multiline,
  help,
  plain,
  ...rest
}: {
  id: string
  label: string
  value: string
  onChange: (v: string) => void
  type?: string
  hint?: string
  error?: string
  className?: string
  multiline?: boolean
  /** 一句话说不清时，标签旁显示「!」胶囊（lib/help.ts） */
  help?: HelpKey
  /** 标签本身就足够说明（名称、密码、搜索等），不需要额外说明 */
  plain?: boolean
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, "onChange" | "value" | "type" | "id">) {
  const describedBy = [hint ? `${id}-hint` : "", error ? `${id}-err` : ""].filter(Boolean).join(" ") || undefined
  return (
    <div className={cn("flex flex-col gap-1", className)} data-field="" data-help-plain={plain ? "" : undefined}>
      <span className="flex items-center">
        <label htmlFor={id} className="text-xs font-medium text-muted-foreground">
          {label}
          {rest.required ? <span aria-hidden> *</span> : null}
        </label>
        {help ? <HelpPill topic={help} /> : null}
      </span>
      {multiline ? (
        <textarea
          id={id}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          rows={3}
          className={cn(INPUT, "h-auto py-1.5")}
        />
      ) : (
        <input
          id={id}
          type={type}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          className={INPUT}
          {...rest}
        />
      )}
      {hint && !error ? (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-err`} className="text-xs text-bad">
          {error}
        </p>
      ) : null}
    </div>
  )
}

/**
 * 给自带控件（原生 input / select / textarea 或第三方控件）加标签与说明：
 * 自动生成 id 关联 label，hint 通过 aria-describedby 关联，help 在标签旁显示「!」胶囊。
 */
export function LabeledField({
  label,
  hint,
  help,
  plain,
  className,
  children,
}: {
  label: React.ReactNode
  hint?: React.ReactNode
  help?: HelpKey
  plain?: boolean
  className?: string
  children: React.ReactElement<{ id?: string; "aria-describedby"?: string }>
}) {
  const auto = useId()
  const id = children.props.id ?? auto
  const control = cloneElement(children, { id, "aria-describedby": hint ? `${id}-hint` : children.props["aria-describedby"] })
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)} data-field="" data-help-plain={plain ? "" : undefined}>
      <span className="flex items-center">
        <label htmlFor={id} className="text-xs font-medium text-muted-foreground">{label}</label>
        {help ? <HelpPill topic={help} /> : null}
      </span>
      {control}
      {hint ? <p id={`${id}-hint`} className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  )
}

/** 勾选框加文字与可选说明胶囊；胶囊放在 label 外面，不会混进勾选框的名称 */
export function CheckField({
  label,
  checked,
  onChange,
  disabled,
  help,
  hint,
  plain,
  className,
}: {
  label: React.ReactNode
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
  help?: HelpKey
  hint?: React.ReactNode
  /** 标签本身已说清楚（如一组“周一…周日”），不需要说明 */
  plain?: boolean
  className?: string
}) {
  const id = useId()
  return (
    <div className={cn("flex flex-col gap-0.5", className)} data-field="" data-help-plain={plain ? "" : undefined}>
      <span className="inline-flex items-center gap-1 text-[0.8125rem]">
        <input id={id} type="checkbox" data-help-wrapped="" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)}
          aria-describedby={hint ? `${id}-hint` : undefined} />
        <label htmlFor={id}>{label}</label>
        {help ? <HelpPill topic={help} /> : null}
      </span>
      {hint ? <p id={`${id}-hint`} className="pl-5 text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  )
}
