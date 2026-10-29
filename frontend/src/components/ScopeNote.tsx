import { Lock } from "lucide-react"

import { InlineNotice } from "@/components/ui/controls"
import { SCOPE_ONLY } from "@/lib/auth"

/** 整块功能对范围受限账号不可用时的说明（4.1 P11：页签保留并提示） */
export function ScopeNotice({ what }: { what: string }) {
  return (
    <section aria-label="资产范围限制">
      <InlineNotice>
        <Lock aria-hidden className="mr-1 inline size-3.5" />
        {what}{SCOPE_ONLY}。你的账号只能看到授权场所与资产的数据。
      </InlineNotice>
    </section>
  )
}

/** 灰键旁的原因（4.1 P11：看得到但无权操作的按钮禁用并说明原因） */
export function ScopeReason({ what }: { what: string }) {
  return (
    <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
      <Lock aria-hidden className="size-3" />
      {what}{SCOPE_ONLY}
    </span>
  )
}
