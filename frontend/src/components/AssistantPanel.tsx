import { Dialog } from "radix-ui"
import { MessageCircleQuestion, X } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { TextField } from "@/components/ui/form"
import { ApiError, api } from "@/lib/api"

/** 问答助手“问云枢”（4.3 P18g）：只读查询，结果按当前账号的资产范围过滤；回答由 AI 生成，仅供参考。 */

interface Msg { role: "user" | "assistant"; content: string; steps?: { tool: string; args: Record<string, unknown>; ok: boolean }[] }

const TOOL_TEXT: Record<string, string> = {
  search_assets: "查找资产", count_assets: "统计资产", get_asset: "查看资产", top_metric: "按指标排序", metric_history: "查看指标历史",
  list_alerts: "查看告警", alert_ranking: "告警排行", get_software: "查看版本", find_versions: "查找版本", list_vulnerabilities: "查看漏洞",
}
const EXAMPLES = ["哪些服务器 CPU 温度超过 70 度？", "所有 iDRAC 的固件版本是多少？", "最近 7 天哪台设备告警最多？", "有哪些已被利用的漏洞命中了我们的设备？"]

/** 回答里的 #/ 链接渲染成可点击的平台内部链接；其余一律当纯文本 */
function Answer({ text }: { text: string }) {
  const parts = text.split(/(#\/[a-z-]+(?:\?[\w=&%.:-]+)?)/g)
  return <p className="whitespace-pre-wrap">{parts.map((p, i) => (p.startsWith("#/") ? <a key={i} className="text-primary underline" href={p}>打开</a> : p))}</p>
}

export function AssistantButton({ className }: { className: string }) {
  const [enabled, setEnabled] = useState(false)
  const [open, setOpen] = useState(false)
  useEffect(() => {
    api.get<{ enabled: boolean }>("/api/assistant/status", { quiet401: true }).then((r) => setEnabled(r.enabled), () => setEnabled(false))
  }, [])
  if (!enabled) return null
  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button type="button" className={className} aria-label="问云枢（AI 问答助手）" title="问云枢（AI 问答助手）">
          <MessageCircleQuestion aria-hidden className="size-4" />
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Content className="fixed top-12 right-0 bottom-0 z-50 flex w-[min(28rem,100vw)] flex-col border-l bg-popover text-[0.8125rem] shadow-modal outline-none">
          <header className="flex items-center justify-between border-b px-4 py-3">
            <div>
              <Dialog.Title className="font-semibold">问云枢</Dialog.Title>
              <Dialog.Description className="text-xs text-muted-foreground">只能查询，不能执行操作；回答由 AI 生成，仅供参考。</Dialog.Description>
            </div>
            <Dialog.Close asChild><Button variant="ghost" size="icon-sm" aria-label="关闭问答助手"><X aria-hidden /></Button></Dialog.Close>
          </header>
          <Chat />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}

function Chat() {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [q, setQ] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" })
  }, [msgs, busy])

  async function ask(text: string) {
    const question = text.trim()
    if (!question || busy) return
    const history = [...msgs, { role: "user" as const, content: question }]
    setMsgs(history)
    setQ("")
    setBusy(true)
    setError("")
    try {
      const r = await api.post<{ answer: string; steps: Msg["steps"] }>("/api/assistant/chat", { messages: history.map(({ role, content }) => ({ role, content })) })
      setMsgs([...history, { role: "assistant", content: r.answer, steps: r.steps }])
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "提问失败")
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3" aria-live="polite" aria-label="对话">
        {msgs.length === 0 ? (
          <div className="space-y-2">
            <p className="text-muted-foreground">可以这样问：</p>
            {EXAMPLES.map((e) => <Button key={e} variant="outline" size="sm" className="h-auto w-full justify-start py-1.5 text-left whitespace-normal" onClick={() => void ask(e)}>{e}</Button>)}
          </div>
        ) : null}
        {msgs.map((m, i) => (
          <div key={i} className={m.role === "user" ? "ml-8 rounded-lg bg-primary/10 px-3 py-2" : "mr-4 space-y-1 rounded-lg border px-3 py-2"}>
            {m.role === "assistant" && m.steps?.length ? (
              <p className="text-xs text-muted-foreground">查询了：{m.steps.map((s) => TOOL_TEXT[s.tool] ?? s.tool).join("、")}</p>
            ) : null}
            {m.role === "assistant" ? <Answer text={m.content} /> : <p className="whitespace-pre-wrap">{m.content}</p>}
          </div>
        ))}
        {busy ? <p className="text-xs text-muted-foreground">正在查询…</p> : null}
        {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
        <div ref={end} />
      </div>
      <form className="flex items-end gap-2 border-t px-4 py-3" onSubmit={(e) => { e.preventDefault(); void ask(q) }}>
        <TextField id="assistant-q" className="flex-1" label="问题" value={q} onChange={setQ} maxLength={2000} placeholder="例如：哪台服务器最热？" plain />
        <Button type="submit" disabled={busy || !q.trim()}>发送</Button>
        {msgs.length ? <Button type="button" variant="ghost" onClick={() => setMsgs([])}>清空</Button> : null}
      </form>
    </>
  )
}
