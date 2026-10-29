import { useCallback, useEffect, useState } from "react"

import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { CheckField, TextField } from "@/components/ui/form"
import { ApiError, api } from "@/lib/api"
import { dateText } from "@/lib/monitor"

interface Feed { name: string; url: string }
interface Source {
  name: string; label: string; group: string; description: string; offline: boolean; enabled: boolean; interval_minutes: number
  state: string; message: string; items: number; new_items: number; last_run_at: string | null; last_ok_at: string | null; feeds: Feed[] | null
}
interface Settings { online_enabled: boolean; aggregation_minutes: number; has_nvd_key: boolean; revision: number }

const STATE: Record<string, { text: string; tone: "ok" | "bad" | "info" | "neutral" }> = {
  ok: { text: "正常", tone: "ok" }, failed: { text: "失败", tone: "bad" }, running: { text: "同步中", tone: "info" }, never: { text: "未运行", tone: "neutral" },
}

/** 数据源（4.3 P18c）：外网同步总开关默认关闭；每个来源单独开关、间隔与状态。 */
export function SourcesTab({ editable }: { editable: boolean }) {
  const [rows, setRows] = useState<Source[] | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [viaWebhook, setViaWebhook] = useState<string[]>([])
  const [nvdKey, setNvdKey] = useState("")
  const [error, setError] = useState("")
  const [note, setNote] = useState("")

  const load = useCallback(() => {
    void Promise.all([api.get<Source[]>("/api/vuln/sources"), api.get<Settings>("/api/vuln/settings"), api.get<{ via_webhook: string[] }>("/api/vuln/sources-info")])
      .then(([s, st, info]) => { setRows(s); setSettings(st); setViaWebhook(info.via_webhook) }, (e: Error) => setError(e.message))
  }, [])
  useEffect(load, [load])

  async function saveSettings(patch: Partial<Settings> & { nvd_api_key?: string }) {
    if (!settings) return
    setError("")
    try {
      setSettings(await api.put<Settings>("/api/vuln/settings", { online_enabled: settings.online_enabled, aggregation_minutes: settings.aggregation_minutes, revision: settings.revision, ...patch }))
      setNvdKey("")
      setNote("已保存")
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "保存失败")
    }
  }

  async function patch(name: string, body: Record<string, unknown>) {
    setError("")
    try {
      await api.patch(`/api/vuln/sources/${name}`, body)
      load()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "保存失败")
    }
  }

  async function syncNow(name: string) {
    setError("")
    try {
      await api.post(`/api/vuln/sources/${name}/sync`)
      setNote("已安排同步，稍后刷新查看")
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "安排同步失败")
    }
  }

  if (!rows || !settings) return error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : <p className="text-[0.8125rem] text-muted-foreground">正在加载…</p>

  return (
    <div className="max-w-4xl space-y-4 text-[0.8125rem]">
      <section aria-labelledby="vuln-online" className="space-y-2 rounded-lg border p-4">
        <h3 id="vuln-online" className="font-medium">外网同步</h3>
        <CheckField label="允许平台访问外网同步漏洞数据" checked={settings.online_enabled} help="vuln.online"
          onChange={(v) => (editable ? void saveSettings({ online_enabled: v }) : undefined)} disabled={!editable} />
        {!settings.online_enabled ? <InlineNotice>外网同步已关闭。不能上网的环境请在“离线包与推送”里导入离线包。</InlineNotice> : null}
        {editable ? (
          <div className="flex flex-wrap items-end gap-2">
            <TextField id="vuln-nvd-key" label="NVD API 密钥" type="password" value={nvdKey} onChange={setNvdKey} autoComplete="off"
              placeholder={settings.has_nvd_key ? "已保存，留空不修改" : "可选"} help="vuln.nvdKey" />
            <Button size="sm" variant="outline" disabled={!nvdKey.trim()} onClick={() => void saveSettings({ nvd_api_key: nvdKey })}>保存密钥</Button>
            {settings.has_nvd_key ? <Button size="sm" variant="outline" onClick={() => void saveSettings({ nvd_api_key: "" })}>清除密钥</Button> : null}
          </div>
        ) : null}
      </section>
      {note ? <InlineNotice tone="ok">{note}</InlineNotice> : null}
      {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
      <ul className="space-y-2" aria-label="数据源">
        {rows.map((s) => {
          const st = STATE[s.state] ?? STATE.never
          return (
            <li key={s.name} className="space-y-2 rounded-lg border p-3">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="font-medium">{s.label} <span className="text-xs font-normal text-muted-foreground">· {s.group}{s.offline ? " · 可离线导入" : ""}</span></p>
                  <p className="text-xs text-muted-foreground">{s.description}</p>
                  <p className="text-xs">
                    <Badge tone={st.tone}>{st.text}</Badge>
                    {s.last_ok_at ? <span className="text-muted-foreground"> 上次成功 {dateText(s.last_ok_at)}，{s.items} 条（新 {s.new_items}）</span> : null}
                    {s.message ? <span className="text-bad"> · {s.message}</span> : null}
                  </p>
                </div>
                {editable ? (
                  <div className="flex flex-wrap items-end gap-2">
                    <CheckField label="启用" checked={s.enabled} onChange={(v) => void patch(s.name, { enabled: v })} help="vuln.source.enabled" />
                    <TextField id={`vuln-int-${s.name}`} className="w-24" label="间隔（分钟）" type="number" min={5} value={String(s.interval_minutes)}
                      onChange={(v) => setRows(rows.map((r) => (r.name === s.name ? { ...r, interval_minutes: Number(v) || 5 } : r)))}
                      onBlur={() => void patch(s.name, { interval_minutes: s.interval_minutes })} hint="至少 5 分钟" />
                    <Button size="sm" variant="outline" disabled={!settings.online_enabled} onClick={() => void syncNow(s.name)}>立即同步</Button>
                  </div>
                ) : null}
              </div>
              {s.name === "rss" ? <FeedsEditor feeds={s.feeds ?? []} editable={editable} onSave={(feeds) => void patch("rss", { feeds })} /> : null}
            </li>
          )
        })}
      </ul>
      <p className="text-xs text-muted-foreground">
        {viaWebhook.join("、")}：这些网站设有防爬验证或需要解析网页，平台不内置抓取。需要时可自行部署开源的 watchvuln，把推送地址设为“离线包与推送”里生成的接收地址。
      </p>
    </div>
  )
}

function FeedsEditor({ feeds, editable, onSave }: { feeds: Feed[]; editable: boolean; onSave: (f: Feed[]) => void }) {
  const [rows, setRows] = useState<Feed[]>(feeds)
  useEffect(() => setRows(feeds), [feeds])
  return (
    <div className="space-y-2">
      {rows.map((f, i) => (
        <div key={i} className="flex flex-wrap items-end gap-2">
          <TextField id={`feed-name-${i}`} label="名称" value={f.name} onChange={(v) => setRows(rows.map((r, j) => (j === i ? { ...r, name: v } : r)))} disabled={!editable} plain />
          <TextField id={`feed-url-${i}`} className="min-w-72 flex-1" label="RSS / Atom 地址" value={f.url} help="vuln.rss"
            onChange={(v) => setRows(rows.map((r, j) => (j === i ? { ...r, url: v } : r)))} disabled={!editable} />
          {editable ? <Button size="sm" variant="ghost" onClick={() => setRows(rows.filter((_, j) => j !== i))}>移除</Button> : null}
        </div>
      ))}
      {editable ? (
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => setRows([...rows, { name: "", url: "" }])}>添加地址</Button>
          <Button size="sm" onClick={() => onSave(rows.filter((r) => r.url.trim()))}>保存地址</Button>
        </div>
      ) : null}
    </div>
  )
}
