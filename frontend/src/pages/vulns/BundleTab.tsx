import { useCallback, useEffect, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { LabeledField } from "@/components/ui/form"
import { ApiError, api } from "@/lib/api"
import { dateText } from "@/lib/monitor"

interface Settings { last_bundle_at: string | null; last_bundle_info: { created_at?: string; new?: number; counts?: Record<string, number> }; webhook_enabled: boolean }

/** 离线包与外部推送（4.3 P18c）。 */
export function BundleTab({ editable }: { editable: boolean }) {
  const [s, setS] = useState<Settings | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState("")
  const [error, setError] = useState("")
  const [token, setToken] = useState<{ token: string; path: string } | null>(null)
  const file = useRef<HTMLInputElement>(null)
  const load = useCallback(() => void api.get<Settings>("/api/vuln/settings").then(setS, (e: Error) => setError(e.message)), [])
  useEffect(load, [load])

  async function upload() {
    const f = file.current?.files?.[0]
    if (!f) return
    setBusy(true)
    setError("")
    setMsg("")
    try {
      const body = new FormData()
      body.append("file", f)
      const resp = await fetch("/api/vuln/bundle", { method: "POST", body, credentials: "same-origin",
        headers: { "X-CSRF-Token": decodeURIComponent(document.cookie.match(/(?:^|;\s*)yunshu_csrf=([^;]+)/)?.[1] ?? "") } })
      const data = await resp.json()
      if (!resp.ok) throw new ApiError(resp.status, typeof data.detail === "string" ? data.detail : "导入失败")
      setMsg(`导入完成：新漏洞 ${data.new} 个（离线包生成于 ${data.created_at ? dateText(data.created_at) : "未知"}）`)
      load()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "导入失败")
    } finally {
      setBusy(false)
    }
  }

  async function newToken() {
    if (s?.webhook_enabled && !window.confirm("生成新地址后，旧地址立即失效。继续？")) return
    try {
      setToken(await api.post<{ token: string; path: string }>("/api/vuln/webhook-token"))
      load()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "生成失败")
    }
  }

  async function closeWebhook() {
    await api.del("/api/vuln/webhook-token")
    setToken(null)
    load()
  }

  if (!s) return <p className="text-[0.8125rem] text-muted-foreground">正在加载…</p>
  const url = token ? `${window.location.origin}${token.path}` : ""

  return (
    <div className="max-w-3xl space-y-5 text-[0.8125rem]">
      <section aria-labelledby="bundle-title" className="space-y-2 rounded-lg border p-4">
        <h3 id="bundle-title" className="font-medium">离线包</h3>
        <p className="text-muted-foreground">不能上网的平台：在能上网的机器上生成离线包，再在这里导入。离线包包含 CISA KEV、EPSS、Debian（可选 Ubuntu）软件包漏洞，以及常见设备的 NVD 数据。</p>
        <pre className="overflow-x-auto rounded bg-secondary p-2 text-xs">{`# 在能上网的机器上（任选其一）
docker run --rm -v "$PWD:/out" ghcr.io/kongbai9420/yunshu:<版本> python -m app.vulndb.bundle --out /out/yunshu-vulndb.zip
python -m app.vulndb.bundle --out yunshu-vulndb.zip --ubuntu   # 在源码的 backend 目录里`}</pre>
        {s.last_bundle_at ? (
          <p className="text-xs">上次导入：{dateText(s.last_bundle_at)}，离线包生成于 {s.last_bundle_info.created_at ? dateText(s.last_bundle_info.created_at) : "未知"}，新漏洞 {s.last_bundle_info.new ?? 0} 个</p>
        ) : <p className="text-xs text-muted-foreground">还没有导入过离线包。</p>}
        {editable ? (
          <div className="flex flex-wrap items-end gap-2">
            <LabeledField label="选择离线包" help="vuln.bundle"><input data-help-wrapped="" ref={file} type="file" accept=".zip" className="text-xs" /></LabeledField>
            <Button disabled={busy} onClick={() => void upload()}>{busy ? "导入中…" : "导入"}</Button>
          </div>
        ) : null}
        {msg ? <InlineNotice tone="ok">{msg}</InlineNotice> : null}
      </section>

      <section aria-labelledby="hook-title" className="space-y-2 rounded-lg border p-4">
        <h3 id="hook-title" className="font-medium">接收外部推送</h3>
        <p className="text-muted-foreground">
          接收 watchvuln 或其他系统推送的漏洞（JSON）。适合接入阿里云漏洞库、Seebug、启明星辰等平台不内置的来源。推送只写入漏洞情报，不会触发任何设备操作。
        </p>
        <p className="text-xs">当前状态：{s.webhook_enabled ? "已开启" : "未开启"}</p>
        {editable ? (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => void newToken()}>{s.webhook_enabled ? "重新生成接收地址" : "开启并生成接收地址"}</Button>
            {s.webhook_enabled ? <Button size="sm" variant="outline" onClick={() => void closeWebhook()}>关闭</Button> : null}
          </div>
        ) : null}
        {token ? (
          <div className="space-y-2">
            <InlineNotice tone="warn">接收地址只显示这一次，请复制保存；地址里包含令牌，不要公开。</InlineNotice>
            <code className="block break-all rounded bg-secondary p-2 text-xs">{url}</code>
            <p className="text-xs text-muted-foreground">watchvuln 配置示例：</p>
            <pre className="overflow-x-auto rounded bg-secondary p-2 text-xs">{`pusher:
  - type: webhook
    url: ${url}`}</pre>
            <p className="text-xs text-muted-foreground">通用格式：POST JSON {"{"}"id", "cve", "title", "severity", "url", "description", "published"{"}"}，其中 id（或 cve）与 title 必填。</p>
          </div>
        ) : null}
      </section>
      {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
    </div>
  )
}
