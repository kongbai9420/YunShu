import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { api } from "@/lib/api"
import type { AssetDraft } from "./AssetFields"

export interface InformationResult {
  protocol: string
  fields: Partial<Record<InformationField, string>>
  tls_fingerprint: string
  ssh_host_key: string
  message: string
}
const LABELS = { name: "名称", platform: "平台", vendor: "品牌", model: "型号", serial_no: "序列号" }
type InformationField = keyof typeof LABELS
const KEYS = Object.keys(LABELS) as InformationField[]

export function InformationPreview({ source, draft, onApply, onResult, disabled = false }: {
  source: Record<string, unknown> | null
  draft: AssetDraft
  onApply: (values: Partial<AssetDraft>) => void
  onResult?: (result: InformationResult) => void
  disabled?: boolean
}) {
  const [result, setResult] = useState<InformationResult | null>(null)
  const [selected, setSelected] = useState<InformationField[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [applied, setApplied] = useState(false)
  const generation = useRef(0)
  const sourceKey = JSON.stringify(source)
  useEffect(() => {
    generation.current += 1
    setResult(null)
    setSelected([])
    setError("")
    setBusy(false)
    setApplied(false)
    return () => { generation.current += 1 }
  }, [sourceKey])

  async function fetchInformation() {
    if (!source || busy) return
    const current = ++generation.current
    setBusy(true)
    setError("")
    setResult(null)
    setSelected([])
    setApplied(false)
    try {
      const value = await api.post<InformationResult>("/api/asset-information/preview", source)
      if (current !== generation.current) return
      setResult(value)
      onResult?.(value)
    } catch (err) {
      if (current === generation.current) setError((err as Error).message)
    } finally {
      if (current === generation.current) setBusy(false)
    }
  }

  const available = KEYS.filter((key) => result?.fields[key] && result.fields[key] !== draft[key].trim())
  const chosen = selected.filter((key) => available.includes(key))
  return (
    <section aria-label="设备信息对比" className="min-w-0 space-y-3 rounded-lg border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">获取设备信息</h3>
        <Button type="button" variant="outline" disabled={!source || disabled || busy} onClick={() => void fetchInformation()}>
          {busy ? "正在获取…" : "获取设备信息"}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">使用所选协议只读连接设备。勾选后替换表单值，保存资产后生效；未采到的字段不会清空原值。</p>
      {busy ? <p role="status" className="text-sm">正在连接设备并读取资料，请稍候…</p> : null}
      {error ? <InlineNotice tone="bad" role="alert">{error} 可以修改连接信息后重试，或直接手动填写。</InlineNotice> : null}
      {result ? <>
        <p role="status" className="text-xs text-muted-foreground">{result.message}</p>
        <div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" variant="outline" disabled={!available.length || disabled} onClick={() => setSelected(available)}>全选可替换项</Button>
          <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => setSelected(available.filter((key) => !draft[key].trim()))}>只选空字段</Button>
          <Button type="button" size="sm" variant="ghost" disabled={disabled} onClick={() => setSelected([])}>取消选择</Button>
        </div>
        <ul className="divide-y rounded-md border" aria-label="采集字段对比">
          {KEYS.map((key) => {
            const value = result.fields[key]
            const same = !!value && value === draft[key].trim()
            return <li key={key} className="min-w-0 p-3">
              <label className="flex min-h-8 items-center gap-2 text-sm font-medium">
                <input type="checkbox" data-help-plain="" aria-label={`替换${LABELS[key]}`} checked={chosen.includes(key)} disabled={!available.includes(key) || disabled}
                  onChange={(e) => setSelected((previous) => e.target.checked ? [...previous, key] : previous.filter((item) => item !== key))} />
                {LABELS[key]} <span className="text-xs font-normal text-muted-foreground">{!value ? "未采到" : same ? "相同" : !draft[key].trim() ? "可补充" : "有差异"}</span>
              </label>
              <dl className="mt-1 grid min-w-0 grid-cols-1 gap-2 pl-5 text-xs sm:grid-cols-2">
                <div className="min-w-0"><dt className="text-muted-foreground">当前值</dt><dd className="mt-1 break-all whitespace-pre-wrap">{draft[key] || "（空）"}</dd></div>
                <div className="min-w-0"><dt className="text-muted-foreground">采集值</dt><dd className="mt-1 break-all whitespace-pre-wrap">{value || "未采到"}</dd></div>
              </dl>
            </li>
          })}
        </ul>
        <Button type="button" disabled={!chosen.length || disabled} className="h-auto min-h-9 max-w-full whitespace-normal" onClick={() => {
          onApply(Object.fromEntries(chosen.map((key) => [key, result.fields[key]])))
          setSelected([])
          setApplied(true)
        }}>替换选中项（{chosen.length}）</Button>
        {applied ? <p role="status" className="text-xs text-muted-foreground">已带入表单，尚未保存。可继续编辑或撤销修改。</p> : null}
      </> : null}
    </section>
  )
}
