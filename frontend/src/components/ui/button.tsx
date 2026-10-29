import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"
import { Slot } from "radix-ui"

/*
 * 尺寸、圆角与状态对照旧版 src/ui/styles.css 的 .primary-btn / .secondary-btn / .icon-btn / .link-btn：
 * 8px 圆角、12.5px 字、7px×16px 内边距，悬停换色、按下缩到 0.98。
 * 禁用不用半透明（暗色下半透明的蓝和可用的蓝分不清），统一换成灰底灰字，并去掉阴影。
 */
const buttonVariants = cva(
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-1.5 rounded-[8px] text-[0.78125rem] font-medium whitespace-nowrap transition-[background-color,border-color,color,box-shadow,transform] duration-150 ease-out outline-none select-none active:scale-[0.98] disabled:pointer-events-none disabled:cursor-default disabled:shadow-none aria-invalid:border-destructive [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5",
  {
    variants: {
      variant: {
        default:
          "bg-primary font-semibold text-primary-foreground shadow-[0_1px_3px_rgba(0,98,204,0.25)] hover:bg-primary-hover active:bg-primary-active disabled:bg-disabled disabled:text-disabled-foreground",
        destructive:
          "bg-destructive-solid font-semibold text-white shadow-[0_1px_3px_rgba(215,0,21,0.25)] hover:bg-destructive-solid-hover disabled:bg-disabled disabled:text-disabled-foreground",
        outline:
          "border border-border bg-card text-foreground hover:border-input hover:bg-secondary-hover disabled:border-transparent disabled:bg-disabled disabled:text-disabled-foreground",
        secondary:
          "border border-border bg-secondary text-secondary-foreground hover:border-input hover:bg-secondary-hover disabled:border-transparent disabled:bg-disabled disabled:text-disabled-foreground",
        ghost:
          "text-muted-foreground hover:bg-accent hover:text-foreground active:bg-accent-strong disabled:text-disabled-foreground",
        link: "h-auto px-0 text-link hover:underline underline-offset-4 active:scale-100 disabled:text-disabled-foreground",
      },
      size: {
        default: "h-8 px-4 has-[>svg]:px-3.5",
        xs: "h-6 gap-1 rounded-[6px] px-2 text-[0.71875rem] max-sm:h-8 max-sm:px-3 has-[>svg]:px-1.5 [&_svg:not([class*='size-'])]:size-3",
        sm: "h-7 gap-1 rounded-[7px] px-3 text-xs has-[>svg]:px-2.5",
        lg: "h-9 px-5 text-[0.8125rem] has-[>svg]:px-4 [&_svg:not([class*='size-'])]:size-4",
        icon: "size-8 rounded-[7px] [&_svg:not([class*='size-'])]:size-4",
        "icon-xs": "size-6 rounded-[6px] max-sm:size-8 [&_svg:not([class*='size-'])]:size-3",
        "icon-sm": "size-7 rounded-[7px]",
        "icon-lg": "size-9 [&_svg:not([class*='size-'])]:size-4",
      },
    },
    compoundVariants: [{ variant: "link", className: "h-auto px-0 has-[>svg]:px-0" }],
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
  }) {
  const Comp = asChild ? Slot.Root : "button"

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
