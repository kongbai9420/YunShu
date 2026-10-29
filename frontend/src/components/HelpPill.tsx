import { useRef, useState } from "react"
import { Popover } from "radix-ui"

import { HELP, type HelpKey } from "@/lib/help"
import { cn } from "@/lib/utils"

/**
 * 「!」说明胶囊：一句话说不清的选项放在标签旁。鼠标悬停、键盘聚焦或点按都能打开，Esc 或移开关闭。
 * 内容统一来自 lib/help.ts；会影响设备或数据的选项用橙色（risk）。
 */
export function HelpPill({ topic, className }: { topic: HelpKey; className?: string }) {
  const help = HELP[topic]
  const [open, setOpen] = useState(false)
  const timer = useRef<number | undefined>(undefined)
  const hover = (next: boolean) => {
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setOpen(next), next ? 120 : 200)
  }
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          data-help-pill={topic}
          // 名称固定为“详细说明”，不含选项名，避免和“按标签找控件”的查找混淆；选项名放在描述里
          aria-label="详细说明"
          aria-description={help.title}
          title={`${help.title}：详细说明`}
          onMouseEnter={() => hover(true)}
          onMouseLeave={() => hover(false)}
          onFocus={() => setOpen(true)}
          onBlur={() => setOpen(false)}
          className={cn(
            // 尺寸加 ! 强制：部分页面给容器内所有按钮设了最小高度，胶囊不能被撑大
            "ml-1 inline-flex !h-4 !min-h-0 !w-auto !min-w-4 !max-w-none shrink-0 items-center justify-center rounded-full !px-1 !py-0 align-middle text-[0.625rem] font-bold leading-none !whitespace-nowrap",
            help.risk ? "bg-warn-bg text-warn ring-1 ring-warn/40" : "bg-info-bg text-info ring-1 ring-info/30",
            className,
          )}
        >
          !
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          side="top"
          align="start"
          sideOffset={6}
          collisionPadding={12}
          onMouseEnter={() => hover(true)}
          onMouseLeave={() => hover(false)}
          onOpenAutoFocus={(e) => e.preventDefault()}
          // 焦点从不移进说明框，关闭时也不要把焦点“还给”胶囊：否则悬停移开关闭后胶囊被聚焦，onFocus 又把说明框打开
          onCloseAutoFocus={(e) => e.preventDefault()}
          className="z-50 w-[min(22rem,calc(100vw-24px))] rounded-lg border bg-card p-3 text-[0.8125rem] leading-relaxed text-foreground shadow-card"
        >
          <p className="mb-1 font-semibold">{help.title}</p>
          <p>{help.what}</p>
          {help.default ? <p className="mt-1"><span className="text-muted-foreground">默认：</span>{help.default}</p> : null}
          {help.example ? <p className="mt-1"><span className="text-muted-foreground">例如：</span>{help.example}</p> : null}
          {help.impact ? (
            <p className={cn("mt-1", help.risk ? "text-warn" : "")}><span className="text-muted-foreground">影响：</span>{help.impact}</p>
          ) : null}
          <Popover.Arrow className="fill-card" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}

/** 标签文字加可选的说明胶囊（自己写 label 的地方用） */
export function LabelText({ text, help, required }: { text: React.ReactNode; help?: HelpKey; required?: boolean }) {
  return (
    <span className="inline-flex flex-wrap items-center">
      {text}
      {required ? <span aria-hidden> *</span> : null}
      {help ? <HelpPill topic={help} /> : null}
    </span>
  )
}
