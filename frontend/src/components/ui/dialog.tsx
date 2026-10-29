import * as React from "react"
import { Dialog } from "radix-ui"
import { X } from "lucide-react"
import { cn } from "@/lib/utils"

/*
 * 弹窗外观统一在这里，对照旧版 .apple-modal-backdrop / .apple-modal-card：
 * 遮罩 55% 黑 + 16px 毛玻璃；卡片 16px 圆角、强边框、大阴影，24×28 内边距（窄屏收为 20）；
 * 标题区下分隔线（16px 粗体标题 + 次要色说明 + 圆形关闭钮），按钮区上分隔线、右对齐。
 */

export function ModalOverlay({ className }: { className?: string }) {
  return (
    <Dialog.Overlay
      className={cn(
        "fixed inset-0 z-40 bg-black/55 backdrop-blur-[16px] backdrop-saturate-[1.2] data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=closed]:fade-out-0",
        className,
      )}
    />
  )
}

/** 居中弹窗：Overlay + Content；width 是最大宽度（px），窄屏自动留 16px 边距 */
export function ModalContent({
  width = 560,
  className,
  children,
  ...props
}: React.ComponentProps<typeof Dialog.Content> & { width?: number }) {
  return (
    <Dialog.Portal>
      <ModalOverlay />
      <Dialog.Content
        {...props}
        style={{ width: `min(${width}px, calc(100vw - 2rem))`, ...props.style }}
        className={cn(
          "fixed top-1/2 left-1/2 z-50 max-h-[min(88svh,calc(100svh-2rem))] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-xl border border-input bg-popover px-5 py-5 text-[0.8125rem] text-popover-foreground shadow-modal outline-none sm:px-7 sm:py-6 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95",
          className,
        )}
      >
        {children}
      </Dialog.Content>
    </Dialog.Portal>
  )
}

export function ModalHeader({
  title,
  description,
  closeDisabled,
  showClose = true,
}: {
  title: React.ReactNode
  description?: React.ReactNode
  closeDisabled?: boolean
  showClose?: boolean
}) {
  return (
    <header className="mb-4 flex items-start justify-between gap-3 border-b pb-3">
      <div className="min-w-0">
        <Dialog.Title className="text-base font-bold">{title}</Dialog.Title>
        {description ? (
          <Dialog.Description className="mt-1 text-xs text-muted-foreground">{description}</Dialog.Description>
        ) : null}
      </div>
      {showClose ? (
        <Dialog.Close asChild>
          <button
            type="button"
            aria-label="关闭"
            disabled={closeDisabled}
            className="inline-flex size-7 shrink-0 cursor-pointer items-center justify-center rounded-full border bg-secondary text-muted-foreground transition-colors hover:border-input hover:bg-secondary-hover hover:text-foreground disabled:pointer-events-none disabled:text-disabled-foreground"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </Dialog.Close>
      ) : null}
    </header>
  )
}

export function ModalFooter({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <footer className={cn("mt-5 flex flex-wrap items-center justify-end gap-2 border-t pt-4", className)}>
      {children}
    </footer>
  )
}
