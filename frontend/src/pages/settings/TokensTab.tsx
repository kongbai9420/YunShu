import { useCallback, useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { TextField } from "@/components/ui/form"
import { ApiError, api } from "@/lib/api"
import { dateText } from "@/lib/monitor"

interface Token { id: string; name: string; created_at: string; last_used_at: string | null }

/** 只读 API 令牌（4.2 P16）：目前用于 Prometheus 抓取 /metrics，导出范围跟随当前账号 */
export function TokensTab() {
  const [rows, setRows] = useState<Token[] | null>(null)
  const [name, setName] = useState("")
  const [created, setCreated] = useState<string | null>(null)
  const [error, setError] = useState("")
  const load = useCallback(() => void api.get<Token[]>("/api/tokens").then(setRows, (e: Error) => setError(e.message)), [])
  useEffect(load, [load])

  async function create(e: React.FormEvent) {
    e.preventDefault()
    setError("")
    try {
      const r = await api.post<{ token: string }>("/api/tokens", { name: name.trim() })
      setCreated(r.token)
      setName("")
      load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "创建失败")
    }
  }

  return (
    <div className="max-w-2xl space-y-3 text-[0.8125rem]">
      <p className="text-muted-foreground">
        只读令牌用于 Prometheus 等工具抓取 <code>/metrics</code>，也用于外部 AI 工具访问只读 MCP 接口 <code>/mcp</code>（需在“平台设置 → AI”里开启）。导出的资产范围与当前账号相同，只包含有效的最新数值。令牌只在创建时显示一次。
      </p>
      <form onSubmit={create} className="flex flex-wrap items-end gap-2">
        <TextField id="token-name" hint="用来区分用途，例如 Prometheus" label="令牌名称" value={name} onChange={setName} maxLength={100} placeholder="如 Prometheus" />
        <Button type="submit" disabled={!name.trim()}>创建令牌</Button>
      </form>
      {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
      {created ? (
        <section aria-label="新令牌" className="space-y-2 rounded-md border p-3">
          <InlineNotice tone="warn">请现在复制保存，关闭后无法再次查看。</InlineNotice>
          <code className="block break-all rounded bg-secondary p-2">{created}</code>
          <p className="text-xs text-muted-foreground">Prometheus 配置示例：</p>
          <pre className="overflow-x-auto rounded bg-secondary p-2 text-xs">{`- job_name: yunshu
  scheme: https
  metrics_path: /metrics
  authorization:
    credentials: <上面的令牌>
  static_configs:
    - targets: ["<平台地址>"]`}</pre>
          <Button variant="outline" size="sm" onClick={() => setCreated(null)}>我已保存</Button>
        </section>
      ) : null}
      {rows?.length ? (
        <ul className="divide-y rounded-md border" aria-label="我的令牌">
          {rows.map((t) => (
            <li key={t.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
              <span><strong>{t.name}</strong><span className="ml-2 text-xs text-muted-foreground">创建于 {dateText(t.created_at)} · {t.last_used_at ? `最近使用 ${dateText(t.last_used_at)}` : "未使用"}</span></span>
              <Button size="sm" variant="outline" onClick={() => void api.del(`/api/tokens/${t.id}`).then(load)}>删除</Button>
            </li>
          ))}
        </ul>
      ) : rows ? <p className="text-muted-foreground">还没有令牌。</p> : null}
    </div>
  )
}
