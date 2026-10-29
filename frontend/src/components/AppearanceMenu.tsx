import { Popover } from "radix-ui"
import { Check, Monitor, Moon, Palette, Sun, type LucideIcon } from "lucide-react"

import { MODES, MODE_LABEL, themeName, useAppearance, type Mode } from "@/lib/theme"
import { hrefFor } from "@/lib/router"
import { cn } from "@/lib/utils"
import type { ThemeInfo } from "@/themes"

const MODE_ICON: Record<Mode, LucideIcon> = { system: Monitor, light: Sun, dark: Moon }

/** 一组单选：“平台默认”加各选项；value 为 null 表示跟平台默认 */
function Choices<T extends string>({
  label,
  value,
  fallback,
  options,
  onChange,
}: {
  label: string
  value: T | null
  fallback: string
  options: { value: T; label: string; hint?: string; icon?: LucideIcon; swatch?: React.ReactNode }[]
  onChange: (value: T | null) => void
}) {
  const items: { value: T | null; label: string; hint?: string; icon?: LucideIcon; swatch?: React.ReactNode }[] = [
    { value: null, label: `平台默认（${fallback}）` },
    ...options,
  ]
  return (
    <div role="radiogroup" aria-label={label} className="space-y-0.5">
      {items.map((item) => {
        const checked = item.value === value
        const Icon = item.icon
        return (
          <button
            key={item.value ?? "default"}
            type="button"
            role="radio"
            aria-checked={checked}
            onClick={() => onChange(item.value)}
            className={cn(
              "flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-[0.8125rem] hover:bg-accent",
              checked && "bg-accent",
            )}
          >
            <span className="mt-0.5 inline-flex size-4 shrink-0 items-center justify-center">
              {checked ? <Check aria-hidden className="size-3.5 text-primary" /> : null}
            </span>
            {item.swatch}
            {Icon ? <Icon aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" /> : null}
            <span className="min-w-0">
              <span className="block font-medium">{item.label}</span>
              {item.hint ? <span className="block text-xs text-muted-foreground">{item.hint}</span> : null}
            </span>
          </button>
        )
      })}
    </div>
  )
}

/** 主题色块：简洁 / 玻璃用固定示意色，主题包用它的主色与背景 */
export function ThemeSwatch({ theme }: { theme: ThemeInfo }) {
  const glass = theme.base === "glass"
  const bg = theme.light["app-backdrop"] ?? theme.light.background
  const primary = theme.light.primary ?? "#0062cc"
  return (
    <span
      aria-hidden
      className="relative mt-0.5 inline-flex h-4 w-6 shrink-0 overflow-hidden rounded border border-input"
      style={{
        background:
          bg ??
          (glass
            ? "linear-gradient(135deg, #ffc1d3 0%, #a9d4ff 50%, #c8b6ff 100%)"
            : "linear-gradient(90deg, #f5f5f7 50%, #212124 50%)"),
      }}
    >
      <span className="absolute right-0.5 bottom-0.5 size-1.5 rounded-full" style={{ background: primary }} />
    </span>
  )
}

/** 自己的外观：风格与深浅分开选，都可以回到平台默认。顶栏菜单与“平台设置 → 外观”共用 */
export function AppearancePicker() {
  const { state, themes, setMine } = useAppearance()
  const mine = state?.mine ?? { style: null, mode: null }
  const def = state?.default ?? { style: "simple", mode: "system" as Mode }
  return (
    <div className="grid gap-3">
      <div>
        <p className="mb-1 px-2 text-xs font-semibold text-muted-foreground">风格</p>
        <Choices
          label="风格"
          value={mine.style}
          fallback={themeName(themes, def.style)}
          options={themes.map((t) => ({
            value: t.id,
            label: t.name,
            hint: t.description,
            swatch: <ThemeSwatch theme={t} />,
          }))}
          onChange={(style) => void setMine({ style, mode: mine.mode })}
        />
      </div>
      <div>
        <p className="mb-1 px-2 text-xs font-semibold text-muted-foreground">深浅</p>
        <Choices
          label="深浅"
          value={mine.mode}
          fallback={MODE_LABEL[def.mode]}
          options={MODES.map((m) => ({ value: m, label: MODE_LABEL[m], icon: MODE_ICON[m] }))}
          onChange={(mode) => void setMine({ style: mine.style, mode })}
        />
      </div>
    </div>
  )
}

/** 顶栏“外观”按钮：图标显示当前深浅，点开选风格与深浅 */
export function AppearanceMenu({ className }: { className?: string }) {
  const { themes, style, mode, error } = useAppearance()
  const Icon = mode === "system" ? Palette : MODE_ICON[mode]
  const hint = `外观：${themeName(themes, style)} · ${MODE_LABEL[mode]}`
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button type="button" className={className} aria-label={hint} title={hint}>
          <Icon aria-hidden className="size-4" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={8}
          collisionPadding={12}
          aria-label="外观"
          className="z-50 max-h-[min(36rem,calc(100svh-5rem))] w-[min(20rem,calc(100vw-24px))] overflow-y-auto rounded-xl border bg-popover p-2 text-popover-foreground shadow-modal"
        >
          <AppearancePicker />
          {error ? (
            <p role="alert" className="mt-2 px-2 text-xs text-bad">
              {error}
            </p>
          ) : null}
          <p className="mt-2 border-t px-2 pt-2 text-xs text-muted-foreground">
            跟账号保存，换电脑登录也一样。平台默认在
            <a className="text-link hover:underline" href={`${hrefFor("settings")}?tab=appearance`}>
              平台设置 → 外观
            </a>
            。
          </p>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}
