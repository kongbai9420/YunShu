import { Dialog } from "radix-ui"
import { useCallback, useEffect, useMemo, useState } from "react"

import { Button } from "@/components/ui/button"
import { InlineNotice, Select } from "@/components/ui/controls"
import { ModalContent, ModalFooter, ModalHeader } from "@/components/ui/dialog"
import { CheckField, TextField } from "@/components/ui/form"
import { ApiError, api } from "@/lib/api"
import { dateText } from "@/lib/monitor"

/** AI 设置（4.3 P18a）：总开关、隐私、按功能分配模型接口、上限；模型接口的增删改与连接自检；调用用量。仅管理员。 */

interface Preset {
  id: string; name: string; group: string; protocol: Protocol; base_url: string; api_version?: string
  key_required: boolean; models: string[]; quirks: string[]; notes?: string; tested: boolean
}
type Protocol = "openai" | "anthropic" | "gemini" | "azure"
interface CheckStep { step: string; label: string; ok: boolean; detail: string; kind?: string; ms: number }
interface Provider {
  id: string; name: string; preset_id: string; preset_name: string; tested_preset: boolean; protocol: Protocol; base_url: string
  api_version: string; model: string; has_key: boolean; enabled: boolean; quirks: string[]
  capabilities: { chat?: boolean; models?: boolean; json_modes?: string[]; best_json?: string | null; tools?: boolean; stream?: boolean; error?: string }
  check_result: CheckStep[]; checked_at: string | null; revision: number
}
interface Feature { name: string; enabled: boolean; provider_id: string | null }
interface Settings {
  enabled: boolean; privacy: "redact" | "full"; default_provider_id: string | null; features: Record<string, Feature>
  daily_call_limit: number; daily_token_limit: number; timeout_seconds: number; proxy_url: string; log_content: boolean
  revision: number; changed_by: string; changed_at: string | null
}
interface Usage {
  today: { calls: number; tokens: number }
  by_feature: { feature: string; name: string; calls: number; input_tokens: number; output_tokens: number; failed: number }[]
  recent: { at: string; user: string; feature: string; provider: string; model: string; mode: string; ok: boolean; error_kind: string; latency_ms: number; tokens: number }[]
}

const PROTOCOL_LABEL: Record<Protocol, string> = { openai: "OpenAI 兼容", anthropic: "Claude 原生", gemini: "Gemini 原生", azure: "Azure OpenAI" }
const QUIRK_LABEL: Record<string, string> = {
  json_object_only: "只支持 json_object",
  no_max_tokens_with_json: "结构化输出时不传 max_tokens",
  mistral_tool_ids: "工具调用 ID 用 9 位字母数字",
  no_temperature: "不传 temperature",
}
const FEATURE_NAMES: Record<string, string> = { selfcheck: "连接自检" }

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback
}

export function AiTab() {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [providers, setProviders] = useState<Provider[]>([])
  const [presets, setPresets] = useState<Preset[]>([])
  const [usage, setUsage] = useState<Usage | null>(null)
  const [editing, setEditing] = useState<Provider | "new" | null>(null)
  const [error, setError] = useState("")
  const [saved, setSaved] = useState("")

  const load = useCallback(() => {
    void Promise.all([
      api.get<Settings>("/api/ai/settings"),
      api.get<Provider[]>("/api/ai/providers"),
      api.get<Preset[]>("/api/ai/presets"),
      api.get<Usage>("/api/ai/usage"),
    ]).then(([s, p, pre, u]) => { setSettings(s); setProviders(p); setPresets(pre); setUsage(u) }, (e: Error) => setError(e.message))
  }, [])
  useEffect(load, [load])

  if (!settings) return error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : <p className="text-[0.8125rem] text-muted-foreground">正在加载…</p>

  const providerOptions = [{ value: "", label: "未指定" }, ...providers.map((p) => ({ value: p.id, label: `${p.name}（${p.model}）` }))]

  async function save() {
    if (!settings) return
    setError("")
    setSaved("")
    try {
      const body = { ...settings, features: Object.fromEntries(Object.entries(settings.features).map(([k, f]) => [k, { enabled: f.enabled, provider_id: f.provider_id }])) }
      setSettings(await api.put<Settings>("/api/ai/settings", body))
      setSaved("已保存")
    } catch (e) {
      setError(errText(e, "保存失败"))
    }
  }

  const set = (patch: Partial<Settings>) => setSettings({ ...settings, ...patch })

  return (
    <div className="max-w-4xl space-y-6 text-[0.8125rem]">
      <p className="text-muted-foreground">
        自己配置 AI 接口（国内外主流厂商、本地模型或网关）。AI 只负责解释、汇总和建议：是否受影响、是否告警由程序判断，AI 不能执行任何操作；
        设备密码、密钥、令牌永远不会发送。
      </p>

      <section aria-labelledby="ai-switch" className="space-y-3 rounded-lg border p-4">
        <h3 id="ai-switch" className="font-medium">总开关与隐私</h3>
        <CheckField label="启用 AI 功能" checked={settings.enabled} onChange={(v) => set({ enabled: v })} help="ai.enabled" />
        <div className="grid gap-3 sm:grid-cols-2">
          <Select id="ai-privacy" label="隐私模式" value={settings.privacy} onChange={(v) => set({ privacy: v as Settings["privacy"] })} help="ai.privacy"
            options={[{ value: "redact", label: "脱敏（推荐）" }, { value: "full", label: "完整（仅本地 / 内网模型）" }]} />
          <Select id="ai-default" label="默认模型接口" value={settings.default_provider_id ?? ""} onChange={(v) => set({ default_provider_id: v || null })}
            options={providerOptions} help="ai.defaultProvider" />
        </div>
        <div className="space-y-2">
          <p className="flex items-center text-xs font-medium text-muted-foreground">功能</p>
          <ul className="divide-y rounded-md border" aria-label="AI 功能">
            {Object.entries(settings.features).map(([key, f]) => (
              <li key={key} className="flex flex-wrap items-end gap-3 px-3 py-2">
                <CheckField className="min-w-40" label={f.name} checked={f.enabled} help="ai.feature"
                  onChange={(v) => set({ features: { ...settings.features, [key]: { ...f, enabled: v } } })} />
                {key === "mcp" ? (
                  <p className="min-w-56 flex-1 text-xs text-muted-foreground">
                    供 Claude、Cursor、Dify 等外部 AI 工具查询本平台（只读）。地址 <code>{window.location.origin}/mcp</code>，
                    在请求头里带“Authorization: Bearer 只读 API 令牌”（平台设置 → API 令牌）。不使用这里的模型接口，也不受总开关影响。
                  </p>
                ) : (
                  <Select id={`ai-feature-${key}`} className="min-w-56" label="使用的模型接口" value={f.provider_id ?? ""} hint="未指定时使用默认模型接口"
                    onChange={(v) => set({ features: { ...settings.features, [key]: { ...f, provider_id: v || null } } })}
                    options={[{ value: "", label: "使用默认" }, ...providerOptions.slice(1)]} />
                )}
              </li>
            ))}
          </ul>
        </div>
        <div className="grid gap-3 sm:grid-cols-4">
          <TextField id="ai-calls" label="24 小时调用上限（次）" type="number" min={1} value={String(settings.daily_call_limit)}
            onChange={(v) => set({ daily_call_limit: Number(v) || 1 })} help="ai.limits" />
          <TextField id="ai-tokens" label="24 小时 token 上限" type="number" min={1000} value={String(settings.daily_token_limit)}
            onChange={(v) => set({ daily_token_limit: Number(v) || 1000 })} hint="输入与输出 token 合计" />
          <TextField id="ai-timeout" label="超时（秒）" type="number" min={5} max={600} value={String(settings.timeout_seconds)}
            onChange={(v) => set({ timeout_seconds: Number(v) || 60 })} help="ai.timeout" />
          <TextField id="ai-proxy" label="出网代理" value={settings.proxy_url} onChange={(v) => set({ proxy_url: v })} placeholder="不使用代理时留空" help="ai.proxy" />
        </div>
        <CheckField label="在调用记录里保存发送内容与结果" checked={settings.log_content} onChange={(v) => set({ log_content: v })} help="ai.logContent" />
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={() => void save()}>保存设置</Button>
          {saved ? <span className="text-ok">{saved}</span> : null}
          {settings.changed_at ? <span className="text-xs text-muted-foreground">上次由 {settings.changed_by} 于 {dateText(settings.changed_at)} 修改</span> : null}
        </div>
        {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
      </section>

      <section aria-labelledby="ai-providers" className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 id="ai-providers" className="font-medium">模型接口</h3>
          <Button size="sm" onClick={() => setEditing("new")}>新增模型接口</Button>
        </div>
        {providers.length === 0 ? <p className="text-muted-foreground">还没有模型接口。点“新增模型接口”，选择厂商、填写密钥和模型。</p> : null}
        {providers.map((p) => <ProviderCard key={p.id} p={p} onEdit={() => setEditing(p)} onChanged={load} />)}
      </section>

      {usage ? <UsageSection usage={usage} settings={settings} /> : null}

      {editing ? (
        <ProviderDialog presets={presets} provider={editing === "new" ? null : editing}
          onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load() }} />
      ) : null}
    </div>
  )
}

function Badge({ ok, children }: { ok: boolean | undefined; children: React.ReactNode }) {
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs ${ok ? "bg-ok-bg" : ok === false ? "bg-bad-bg" : "bg-secondary text-muted-foreground"}`}>
      {ok ? "✓ " : ok === false ? "✗ " : ""}{children}
    </span>
  )
}

function ProviderCard({ p, onEdit, onChanged }: { p: Provider; onEdit: () => void; onChanged: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [open, setOpen] = useState(false)
  const caps = p.capabilities
  const checked = Boolean(p.checked_at)

  async function check() {
    setBusy(true)
    setError("")
    try {
      await api.post(`/api/ai/providers/${p.id}/check`)
      setOpen(true)
      onChanged()
    } catch (e) {
      setError(errText(e, "自检失败"))
    } finally {
      setBusy(false)
    }
  }
  async function remove() {
    if (!window.confirm(`删除模型接口“${p.name}”？使用它的功能会改用默认接口。`)) return
    try {
      await api.del(`/api/ai/providers/${p.id}`)
      onChanged()
    } catch (e) {
      setError(errText(e, "删除失败"))
    }
  }

  return (
    <article aria-label={`模型接口 ${p.name}`} className="space-y-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-medium">{p.name}{p.enabled ? "" : <span className="ml-2 text-xs text-muted-foreground">（已停用）</span>}</p>
          <p className="break-all text-xs text-muted-foreground">
            {p.preset_name} · {PROTOCOL_LABEL[p.protocol]} · {p.base_url} · 模型 {p.model}{p.has_key ? "" : " · 未设密钥"}
          </p>
          <p className="text-xs text-muted-foreground">{p.tested_preset ? "该厂商已实测" : "该厂商按文档适配，未实测；请以连接自检结果为准"}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void check()}>{busy ? "自检中…" : "连接自检"}</Button>
          <Button size="sm" variant="outline" onClick={onEdit}>编辑</Button>
          <Button size="sm" variant="outline" onClick={() => void remove()}>删除</Button>
        </div>
      </div>
      <div className="flex flex-wrap gap-1.5" aria-label="能力">
        {checked ? (
          <>
            <Badge ok={caps.chat}>普通回复</Badge>
            <Badge ok={caps.models}>模型列表</Badge>
            <Badge ok={Boolean(caps.best_json)}>结构化输出{caps.best_json ? `（${caps.best_json}）` : ""}</Badge>
            <Badge ok={caps.tools}>工具调用</Badge>
            <Badge ok={caps.stream}>流式输出</Badge>
            <span className="text-xs text-muted-foreground">自检于 {dateText(p.checked_at!)}</span>
            <button type="button" className="text-xs text-primary underline" onClick={() => setOpen(!open)}>{open ? "收起详情" : "查看详情"}</button>
          </>
        ) : <Badge ok={undefined}>尚未自检</Badge>}
      </div>
      {open && checked ? (
        <ol className="space-y-1 rounded-md bg-secondary p-2 text-xs" aria-label="自检详情">
          {p.check_result.map((r) => (
            <li key={r.step}><span className={r.ok ? "text-ok" : "text-bad"}>{r.ok ? "✓" : "✗"}</span> {r.label}：{r.detail}{r.ms ? `（${r.ms} ms）` : ""}</li>
          ))}
        </ol>
      ) : null}
      {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
    </article>
  )
}

function ProviderDialog({ presets, provider, onClose, onSaved }: { presets: Preset[]; provider: Provider | null; onClose: () => void; onSaved: () => void }) {
  const initialPreset = provider ? presets.find((x) => x.id === provider.preset_id) ?? null : presets[0] ?? null
  const [presetId, setPresetId] = useState(provider?.preset_id ?? initialPreset?.id ?? "custom")
  const [name, setName] = useState(provider?.name ?? initialPreset?.name ?? "")
  const [protocol, setProtocol] = useState<Protocol>(provider?.protocol ?? initialPreset?.protocol ?? "openai")
  const [baseUrl, setBaseUrl] = useState(provider?.base_url ?? initialPreset?.base_url ?? "")
  const [apiVersion, setApiVersion] = useState(provider?.api_version ?? initialPreset?.api_version ?? "")
  const [model, setModel] = useState(provider?.model ?? initialPreset?.models[0] ?? "")
  const [key, setKey] = useState("")
  const [clearKey, setClearKey] = useState(false)
  const [enabled, setEnabled] = useState(provider?.enabled ?? true)
  const [quirks, setQuirks] = useState<string[]>(provider?.quirks ?? initialPreset?.quirks ?? [])
  const [models, setModels] = useState<string[]>(initialPreset?.models ?? [])
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const preset = presets.find((x) => x.id === presetId)
  const groups = useMemo(() => presets.map((x) => ({ value: x.id, label: `${x.group} · ${x.name}${x.tested ? "（已实测）" : ""}` })), [presets])

  function choosePreset(id: string) {
    const pre = presets.find((x) => x.id === id)
    setPresetId(id)
    if (!pre) return
    if (!provider) setName(pre.name)
    setProtocol(pre.protocol)
    setBaseUrl(pre.base_url)
    setApiVersion(pre.api_version ?? "")
    setQuirks(pre.quirks)
    setModels(pre.models)
    if (pre.models.length) setModel(pre.models[0])
  }

  async function fetchModels() {
    setError("")
    setBusy(true)
    try {
      const list = await api.post<string[]>("/api/ai/models", { protocol, base_url: baseUrl, api_version: apiVersion, api_key: key || null, provider_id: provider?.id ?? null })
      setModels(list)
      if (list.length && !list.includes(model)) setModel(list[0])
      if (!list.length) setError("接口返回了空的模型列表，请手动填写模型名")
    } catch (e) {
      setError(errText(e, "获取模型列表失败"))
    } finally {
      setBusy(false)
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError("")
    try {
      if (provider) {
        await api.patch(`/api/ai/providers/${provider.id}`, {
          name, protocol, base_url: baseUrl, api_version: apiVersion, model, enabled, quirks, revision: provider.revision,
          ...(key ? { api_key: key } : clearKey ? { api_key: "" } : {}),
        })
      } else {
        await api.post("/api/ai/providers", { name, preset_id: presetId, protocol, base_url: baseUrl, api_version: apiVersion, model, enabled, quirks, api_key: key || null })
      }
      onSaved()
    } catch (err) {
      setError(errText(err, "保存失败"))
    }
  }

  return (
    <Dialog.Root open onOpenChange={(o) => (!o ? onClose() : undefined)}>
      <ModalContent width={640}>
        <ModalHeader title={provider ? `编辑模型接口：${provider.name}` : "新增模型接口"} description="保存后建议立即做一次连接自检。" />
        <form onSubmit={submit} className="space-y-3">
          {!provider ? <Select id="ai-preset" label="厂商" value={presetId} onChange={choosePreset} options={groups} help="ai.preset" /> : null}
          {preset?.notes ? <InlineNotice>{preset.notes}</InlineNotice> : null}
          <div className="grid gap-3 sm:grid-cols-2">
            <TextField id="ai-name" label="名称" value={name} onChange={setName} required maxLength={100} plain />
            <Select id="ai-protocol" label="协议" value={protocol} onChange={(v) => setProtocol(v as Protocol)} hint="选了厂商会自动设置"
              options={(Object.keys(PROTOCOL_LABEL) as Protocol[]).map((k) => ({ value: k, label: PROTOCOL_LABEL[k] }))} />
          </div>
          <TextField id="ai-base" label="接口地址" value={baseUrl} onChange={setBaseUrl} required maxLength={500} help="ai.baseUrl" />
          {protocol === "azure" ? <TextField id="ai-version" label="API 版本" value={apiVersion} onChange={setApiVersion} placeholder="2024-10-21" help="ai.apiVersion" /> : null}
          <TextField id="ai-key" label="接口密钥" type="password" value={key} onChange={setKey} autoComplete="off" maxLength={8000}
            placeholder={provider?.has_key ? "已保存，留空不修改" : preset && !preset.key_required ? "本地模型一般不需要" : ""} help="ai.apiKey" />
          {provider?.has_key ? <CheckField label="清除已保存的密钥" checked={clearKey} onChange={setClearKey} hint="勾选并保存后删除密钥" /> : null}
          <div className="flex flex-wrap items-end gap-2">
            <TextField id="ai-model" className="min-w-64 flex-1" label="模型" value={model} onChange={setModel} required maxLength={200} list="ai-model-list" help="ai.model" />
            <datalist id="ai-model-list">{models.map((m) => <option key={m} value={m} />)}</datalist>
            <Button type="button" variant="outline" disabled={busy || !baseUrl} onClick={() => void fetchModels()}>{busy ? "读取中…" : "获取模型列表"}</Button>
          </div>
          {models.length ? <p className="text-xs text-muted-foreground">可选模型 {models.length} 个，在“模型”输入框里选择或输入。</p> : null}
          <fieldset className="space-y-1">
            <legend className="flex items-center text-xs font-medium text-muted-foreground">兼容选项</legend>
            <div className="flex flex-wrap gap-x-4">
              {Object.entries(QUIRK_LABEL).map(([q, label]) => (
                <CheckField key={q} label={label} checked={quirks.includes(q)} help="ai.quirks"
                  onChange={(v) => setQuirks(v ? [...quirks, q] : quirks.filter((x) => x !== q))} />
              ))}
            </div>
          </fieldset>
          <CheckField label="启用这个模型接口" checked={enabled} onChange={setEnabled} hint="停用后各功能不会使用它" />
          {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
          <ModalFooter>
            <Dialog.Close asChild><Button type="button" variant="outline">取消</Button></Dialog.Close>
            <Button type="submit" disabled={!name.trim() || !baseUrl.trim() || !model.trim()}>保存</Button>
          </ModalFooter>
        </form>
      </ModalContent>
    </Dialog.Root>
  )
}

function UsageSection({ usage, settings }: { usage: Usage; settings: Settings }) {
  return (
    <section aria-labelledby="ai-usage" className="space-y-2">
      <h3 id="ai-usage" className="font-medium">调用记录</h3>
      <p className="text-muted-foreground">
        过去 24 小时：{usage.today.calls} / {settings.daily_call_limit} 次，{usage.today.tokens.toLocaleString()} / {settings.daily_token_limit.toLocaleString()} token。
      </p>
      {usage.by_feature.length ? (
        <table className="w-full text-left text-xs">
          <caption className="sr-only">近 7 天按功能统计</caption>
          <thead><tr className="text-muted-foreground"><th className="py-1">功能（近 7 天）</th><th>次数</th><th>失败</th><th>输入 token</th><th>输出 token</th></tr></thead>
          <tbody>
            {usage.by_feature.map((f) => (
              <tr key={f.feature} className="border-t"><td className="py-1">{f.name}</td><td>{f.calls}</td><td>{f.failed}</td><td>{f.input_tokens}</td><td>{f.output_tokens}</td></tr>
            ))}
          </tbody>
        </table>
      ) : <p className="text-xs text-muted-foreground">还没有调用记录（连接自检不计入）。</p>}
      {usage.recent.length ? (
        <ul className="divide-y rounded-md border text-xs" aria-label="最近调用">
          {usage.recent.slice(0, 20).map((r, i) => (
            <li key={i} className="flex flex-wrap gap-x-3 px-3 py-1.5">
              <span>{dateText(r.at)}</span><span>{FEATURE_NAMES[r.feature] ?? settings.features[r.feature]?.name ?? r.feature}</span>
              <span>{r.provider} · {r.model}</span><span className={r.ok ? "text-ok" : "text-bad"}>{r.ok ? "成功" : `失败（${r.error_kind}）`}</span>
              <span>{r.latency_ms} ms</span><span>{r.tokens} token</span><span className="text-muted-foreground">{r.user}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  )
}
