import { useEffect, useState } from "react"

import { HelpPill } from "@/components/HelpPill"
import { CheckField, LabeledField } from "@/components/ui/form"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Card, InlineNotice } from "@/components/ui/controls"
import { ApiError, api, query, type Asset, type Page } from "@/lib/api"
import { dateText, usePoll } from "@/lib/monitor"

interface Node {
  temp: number
  speed: number
}
interface Curve {
  key: string
  nodes: Node[]
  revision: number
  safety_temp: number
  source: string
  updated_by: string
}
interface Policy {
  asset_id: string
  asset_name: string
  mode: "auto" | "fixed" | "curve"
  fixed_pwm: number | null
  enabled: boolean
  revision: number
  source: string
  legacy_mode: string
  pending_note: string
  fuse_state: string
  last_decision: string
  last_decision_at: string | null
}
interface Listing {
  policies: Policy[]
  curve: Curve
  fuse_hysteresis: number
}

const MODE_LABEL = { auto: "原厂自动", fixed: "固定转速", curve: "按曲线" }
const LEGACY_LABEL: Record<string, string> = { auto: "自动", manual: "手动", dynamic: "曲线", preset: "情景方案" }
const inputClass = "h-8 w-20 rounded-md border bg-card px-2 text-[0.8125rem] text-foreground"

export function ThermalPolicies({ editable, legacy, curveEditable = editable }: { editable: boolean; legacy: boolean; curveEditable?: boolean }) {
  const data = usePoll<Listing>("/api/control/thermal/policies", 10000)
  if (data.error)
    return (
      <InlineNotice tone="bad" role="alert">
        {data.error}
      </InlineNotice>
    )
  if (!data.data) return <p className="text-muted-foreground">正在读取温控策略…</p>
  return (
    <div className="min-w-0 space-y-4">
      {legacy ? (
        <InlineNotice>控制仍由旧控制器负责：这里的曲线与策略只是保存的期望状态，切换到新平台之前不会执行，“最近判断”也不会更新。</InlineNotice>
      ) : null}
      <CurveEditor curve={data.data.curve} hysteresis={data.data.fuse_hysteresis} editable={curveEditable} saved={data.refresh} />
      <Card title="每台服务器的温控策略">
        <p className="mb-2 text-xs text-muted-foreground">
          策略是期望状态：只有控制归属为新平台、策略启用、资产在用且门禁通过时才会下发。旧版设置导入后在这里显示；无法安全转换的（如情景方案）保持“原厂自动”并停用，等待人工确认。
        </p>
        {editable ? <AddPolicy existing={data.data.policies.map((p) => p.asset_id)} added={data.refresh} /> : null}
        {data.data.policies.length ? (
          <ul className="space-y-3">
            {data.data.policies.map((p) => (
              <PolicyRow key={`${p.asset_id}-${p.revision}`} policy={p} editable={editable} saved={data.refresh} />
            ))}
          </ul>
        ) : (
          <p className="text-muted-foreground">还没有温控策略。导入旧版设置后会自动生成。</p>
        )}
      </Card>
    </div>
  )
}

function AddPolicy({ existing, added }: { existing: string[]; added: () => void }) {
  const assets = usePoll<Page<Asset>>(`/api/assets${query({ role: "server", lifecycle: "active", limit: 200 })}`, 60000)
  const [selected, setSelected] = useState("")
  const [error, setError] = useState("")
  const options = (assets.data?.items ?? []).filter((a) => !existing.includes(a.id))
  if (!options.length) return null
  async function add() {
    setError("")
    try {
      await api.put(`/api/control/thermal/policies/${selected}`, { revision: 0, mode: "auto", fixed_pwm: null, enabled: false })
      setSelected("")
      added()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "添加失败")
    }
  }
  return (
    <div className="mb-3 flex flex-wrap items-end gap-2">
      <LabeledField label="为服务器添加策略" help="thermal.addServer">
        <select
          data-help-wrapped=""
          className="h-8 min-w-0 rounded-md border bg-card px-2 text-[0.8125rem] text-foreground"
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
        >
          <option value="">选择服务器</option>
          {options.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      </LabeledField>
      <Button size="sm" variant="outline" disabled={!selected} onClick={() => void add()}>
        添加（先为原厂自动、停用）
      </Button>
      {error ? <span className="text-xs text-bad">{error}</span> : null}
    </div>
  )
}

function CurveEditor({ curve, hysteresis, editable, saved }: { curve: Curve; hysteresis: number; editable: boolean; saved: () => void }) {
  const [nodes, setNodes] = useState<{ temp: string; speed: string }[]>([])
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  useEffect(() => {
    setNodes(curve.nodes.map((n) => ({ temp: String(n.temp), speed: String(n.speed) })))
  }, [curve.revision, curve.nodes])
  async function save() {
    setError("")
    setNotice("")
    try {
      const body = { revision: curve.revision, nodes: nodes.map((n) => ({ temp: Number(n.temp), speed: Number(n.speed) })) }
      const res = await api.put<Curve>("/api/control/thermal/curves/default", body)
      setNotice(`已保存为修订 ${res.revision}，熔断温度 ${res.safety_temp}℃`)
      saved()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "保存失败")
    }
  }
  return (
    <Card title="温控曲线（全局）">
      <p className="text-xs text-muted-foreground">
        按 CPU 最高温度分段线性计算转速。最高节点的温度就是熔断温度（当前 {curve.safety_temp}℃）：达到后请求恢复原厂自动温控，降到{" "}
        {curve.safety_temp - hysteresis}℃ 以下才恢复按曲线调速。温度过期或缺失时不会降速。
        {curve.revision ? ` 修订 ${curve.revision} · ${curve.source === "legacy" ? "旧版导入" : curve.updated_by}` : " 还没有曲线。"}
      </p>
      <div className="mt-2 w-fit max-w-full overflow-x-auto rounded-lg border">
        <table className="data-table data-table-compact">
          <caption className="sr-only">温控曲线节点</caption>
          <thead>
            <tr>
              <th><span className="inline-flex items-center">温度 ℃<HelpPill topic="thermal.curve" /></span></th>
              <th>转速 %</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {nodes.map((n, i) => (
              <tr key={i}>
                <td>
                  <input
                    aria-label={`第 ${i + 1} 个节点温度`}
                    data-help-wrapped=""
                    className={inputClass}
                    inputMode="decimal"
                    value={n.temp}
                    disabled={!editable}
                    onChange={(e) => setNodes(nodes.map((x, j) => (j === i ? { ...x, temp: e.target.value } : x)))}
                  />
                </td>
                <td>
                  <input
                    aria-label={`第 ${i + 1} 个节点转速`}
                    data-help-wrapped=""
                    className={inputClass}
                    inputMode="numeric"
                    value={n.speed}
                    disabled={!editable}
                    onChange={(e) => setNodes(nodes.map((x, j) => (j === i ? { ...x, speed: e.target.value } : x)))}
                  />
                </td>
                <td>
                  {editable ? (
                    <Button size="xs" variant="ghost" onClick={() => setNodes(nodes.filter((_, j) => j !== i))}>
                      删除
                    </Button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {editable ? (
        <div className="mt-2 flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={() => setNodes([...nodes, { temp: "", speed: "" }])}>
            添加节点
          </Button>
          <Button size="sm" onClick={() => void save()}>
            保存曲线
          </Button>
        </div>
      ) : null}
      {error ? (
        <InlineNotice tone="bad" role="alert" className="mt-2">
          {error}
        </InlineNotice>
      ) : null}
      {notice ? <p className="mt-2 text-xs text-muted-foreground">{notice}</p> : null}
    </Card>
  )
}

function PolicyRow({ policy, editable, saved }: { policy: Policy; editable: boolean; saved: () => void }) {
  const [mode, setMode] = useState(policy.mode)
  const [pwm, setPwm] = useState(policy.fixed_pwm != null ? String(policy.fixed_pwm) : "")
  const [enabled, setEnabled] = useState(policy.enabled)
  const [error, setError] = useState("")
  const dirty = mode !== policy.mode || enabled !== policy.enabled || (mode === "fixed" && pwm !== String(policy.fixed_pwm ?? ""))
  async function save() {
    setError("")
    try {
      await api.put(`/api/control/thermal/policies/${policy.asset_id}`, {
        revision: policy.revision,
        mode,
        fixed_pwm: mode === "fixed" && /^\d{1,3}$/.test(pwm) ? Number(pwm) : null,
        enabled,
      })
      saved()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "保存失败")
    }
  }
  return (
    <li className="space-y-1 rounded-lg border p-3" aria-label={`${policy.asset_name} 的温控策略`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{policy.asset_name}</span>
        {policy.legacy_mode ? <Badge tone="neutral">旧版：{LEGACY_LABEL[policy.legacy_mode] ?? policy.legacy_mode}</Badge> : null}
        {policy.fuse_state === "tripped" ? <Badge tone="bad">熔断中</Badge> : null}
        <Badge tone={policy.enabled ? "ok" : "neutral"}>{policy.enabled ? "已启用" : "已停用"}</Badge>
      </div>
      {policy.pending_note ? <InlineNotice tone="warn">{policy.pending_note}</InlineNotice> : null}
      <div className="flex flex-wrap items-end gap-3">
        <LabeledField label="模式" help="thermal.mode">
          <select
            data-help-wrapped=""
            className="h-8 rounded-md border bg-card px-2 text-[0.8125rem] text-foreground"
            value={mode}
            disabled={!editable}
            onChange={(e) => setMode(e.target.value as Policy["mode"])}
          >
            {Object.entries(MODE_LABEL).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </LabeledField>
        {mode === "fixed" ? (
          <LabeledField label="固定转速 %" help="thermal.fixed">
            <input data-help-wrapped="" className={inputClass} inputMode="numeric" value={pwm} disabled={!editable} onChange={(e) => setPwm(e.target.value.trim())} />
          </LabeledField>
        ) : null}
        <CheckField label="启用" help="thermal.enabled" checked={enabled} disabled={!editable} onChange={setEnabled} />
        {editable ? (
          <Button size="sm" variant="outline" disabled={!dirty} onClick={() => void save()}>
            保存策略
          </Button>
        ) : null}
      </div>
      <p className="text-xs text-muted-foreground">
        最近判断：{policy.last_decision || "—"}
        {policy.last_decision_at ? ` · ${dateText(policy.last_decision_at)}` : ""}
      </p>
      {error ? (
        <InlineNotice tone="bad" role="alert">
          {error}
        </InlineNotice>
      ) : null}
    </li>
  )
}
