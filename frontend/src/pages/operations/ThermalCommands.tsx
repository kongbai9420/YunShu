import { useState } from "react"

import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Card, InlineNotice } from "@/components/ui/controls"
import { CheckField, LabeledField, TextField } from "@/components/ui/form"
import { ApiError, api } from "@/lib/api"
import { dateText, usePoll } from "@/lib/monitor"

interface Builtin {
  id: string
  label: string
  vendor: string
  vendor_aliases: string[]
  model_pattern: string
  firmware_min: string
  firmware_max: string
  transport: string
  source: string
  source_label: string
  safe_min_pwm: number
  safe_max_pwm: number
  restore_commands: string
  set_commands: string
  description: string
}
interface Custom {
  id: string
  name: string
  vendor: string
  model_pattern: string
  firmware_min: string
  firmware_max: string
  restore_commands: string
  set_commands: string
  pwm_scale: "percent" | "byte"
  safe_min_pwm: number
  safe_max_pwm: number
  enabled: boolean
  note: string
  updated_by: string
  updated_at: string | null
}
interface Listing {
  builtin: Builtin[]
  custom: Custom[]
  can_edit: boolean
}

type Draft = Omit<Custom, "id" | "updated_by" | "updated_at" | "safe_min_pwm" | "safe_max_pwm"> & { safe_min_pwm: string; safe_max_pwm: string }

const EMPTY: Draft = {
  name: "",
  vendor: "",
  model_pattern: "",
  firmware_min: "",
  firmware_max: "",
  restore_commands: "",
  set_commands: "",
  pwm_scale: "percent",
  safe_min_pwm: "20",
  safe_max_pwm: "100",
  enabled: true,
  note: "",
}

function range(min: string, max: string) {
  if (!min && !max) return "不限"
  return `${min || "不限"} ～ ${max || "不限"}`
}

export function ThermalCommands() {
  const data = usePoll<Listing>("/api/control/thermal/commands", 30000)
  const [editing, setEditing] = useState<{ id: string | null; draft: Draft } | null>(null)
  if (data.error)
    return (
      <InlineNotice tone="bad" role="alert">
        {data.error}
      </InlineNotice>
    )
  if (!data.data) return <p className="text-muted-foreground">正在读取风扇命令…</p>
  const { builtin, custom, can_edit } = data.data
  return (
    <div className="min-w-0 space-y-4">
      <InlineNotice tone="warn">
        执行风扇控制时，平台先读取设备的厂商、型号与 BMC 固件，再按顺序匹配：先自定义命令，后内置命令，用第一条匹配上的。
        内置命令整理自厂商文档与社区资料，没有在本项目的设备上实机验证过；不同型号、固件的行为可能不同，请先在非生产设备上试用，确认能恢复原厂温控。
      </InlineNotice>
      <Card
        title="自定义命令"
        actions={
          can_edit && !editing ? (
            <Button size="sm" variant="outline" onClick={() => setEditing({ id: null, draft: EMPTY })}>
              新增自定义命令
            </Button>
          ) : null
        }
      >
        <p className="mb-2 text-xs text-muted-foreground">
          内置命令没有覆盖的设备（如联想 XCC、浪潮、新华三、华擎），可以由管理员按自己核实过的 IPMI 命令添加；与内置命令同时匹配时优先使用自定义的。
          {can_edit ? "" : " 只有管理员可以新增和修改。"}
        </p>
        {editing ? (
          <CommandForm
            id={editing.id}
            initial={editing.draft}
            done={() => {
              setEditing(null)
              data.refresh()
            }}
            cancel={() => setEditing(null)}
          />
        ) : null}
        {custom.length ? (
          <ul className="space-y-3">
            {custom.map((c) => (
              <li key={c.id} className="space-y-1 rounded-lg border p-3" aria-label={`自定义风扇命令 ${c.name}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{c.name}</span>
                  <Badge tone={c.enabled ? "ok" : "neutral"}>{c.enabled ? "已启用" : "已停用"}</Badge>
                  <Badge tone="neutral">IPMI</Badge>
                  {can_edit && !editing ? (
                    <span className="ml-auto flex gap-1">
                      <Button
                        size="xs"
                        variant="ghost"
                        onClick={() =>
                          setEditing({ id: c.id, draft: { ...c, safe_min_pwm: String(c.safe_min_pwm), safe_max_pwm: String(c.safe_max_pwm) } })
                        }
                      >
                        修改
                      </Button>
                      <DeleteButton id={c.id} name={c.name} deleted={data.refresh} />
                    </span>
                  ) : null}
                </div>
                <Matching vendor={c.vendor} model={c.model_pattern || "所有型号"} firmware={range(c.firmware_min, c.firmware_max)} />
                <Commands restore={c.restore_commands} set={c.set_commands} />
                <p className="text-xs text-muted-foreground">
                  转速 {c.safe_min_pwm}–{c.safe_max_pwm}%，{c.pwm_scale === "byte" ? "按 0–255 换算" : "按百分比填入"}
                  {c.note ? ` · ${c.note}` : ""}
                  {c.updated_at ? ` · ${c.updated_by} 修改于 ${dateText(c.updated_at)}` : ""}
                </p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted-foreground">还没有自定义命令。</p>
        )}
      </Card>
      <Card title="内置命令">
        <ul className="space-y-3">
          {builtin.map((b) => (
            <li key={b.id} className="space-y-1 rounded-lg border p-3" aria-label={`内置风扇命令 ${b.label}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{b.label}</span>
                <Badge tone="neutral">{b.transport}</Badge>
                <Badge tone={b.source === "builtin_audited" ? "ok" : "warn"}>{b.source === "builtin_audited" ? "已实机验证" : "未实机验证"}</Badge>
              </div>
              <Matching
                vendor={[b.vendor, ...b.vendor_aliases].join(" / ")}
                model={b.model_pattern === ".*" ? "所有型号" : b.model_pattern}
                firmware={range(b.firmware_min, b.firmware_max)}
              />
              {b.restore_commands || b.set_commands ? <Commands restore={b.restore_commands} set={b.set_commands} /> : null}
              <p className="text-xs text-muted-foreground">
                {b.description}
                {b.safe_min_pwm ? ` 安全下限 ${b.safe_min_pwm}%。` : ""}
              </p>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  )
}

function Matching({ vendor, model, firmware }: { vendor: string; model: string; firmware: string }) {
  return (
    <p className="text-xs text-muted-foreground">
      厂商 {vendor} · 型号 {model} · BMC 固件 {firmware}
    </p>
  )
}

function Commands({ restore, set }: { restore: string; set: string }) {
  return (
    <div className="grid gap-2 text-xs sm:grid-cols-2">
      <div className="min-w-0">
        <p className="text-muted-foreground">恢复原厂温控</p>
        <pre className="overflow-x-auto rounded-md bg-muted px-2 py-1 font-mono">{restore || "—"}</pre>
      </div>
      <div className="min-w-0">
        <p className="text-muted-foreground">设定转速</p>
        <pre className="overflow-x-auto rounded-md bg-muted px-2 py-1 font-mono">{set || "—"}</pre>
      </div>
    </div>
  )
}

function DeleteButton({ id, name, deleted }: { id: string; name: string; deleted: () => void }) {
  const [error, setError] = useState("")
  async function remove() {
    if (!window.confirm(`删除自定义风扇命令“${name}”？删除后匹配到它的设备会改用内置命令，或被拒绝。`)) return
    try {
      await api.del(`/api/control/thermal/commands/${id}`)
      deleted()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "删除失败")
    }
  }
  return (
    <>
      <Button size="xs" variant="ghost" onClick={() => void remove()}>
        删除
      </Button>
      {error ? <span className="text-xs text-bad">{error}</span> : null}
    </>
  )
}

const areaClass = "min-h-20 w-full rounded-md border bg-card px-2 py-1.5 font-mono text-[0.8125rem] text-foreground"

function CommandForm({ id, initial, done, cancel }: { id: string | null; initial: Draft; done: () => void; cancel: () => void }) {
  const [d, setD] = useState<Draft>(initial)
  const [error, setError] = useState("")
  const [saving, setSaving] = useState(false)
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD({ ...d, [k]: v })
  async function save() {
    setError("")
    if (!/^\d{1,3}$/.test(d.safe_min_pwm) || !/^\d{1,3}$/.test(d.safe_max_pwm)) {
      setError("安全下限和上限要填 0–100 的整数")
      return
    }
    setSaving(true)
    try {
      const body = { ...d, safe_min_pwm: Number(d.safe_min_pwm), safe_max_pwm: Number(d.safe_max_pwm) }
      if (id) await api.put(`/api/control/thermal/commands/${id}`, body)
      else await api.post("/api/control/thermal/commands", body)
      done()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "保存失败")
    } finally {
      setSaving(false)
    }
  }
  return (
    <div className="mb-3 space-y-3 rounded-lg border p-3" role="group" aria-label={id ? "修改自定义风扇命令" : "新增自定义风扇命令"}>
      <div className="grid gap-3 sm:grid-cols-2">
        <TextField id="tc-name" label="名称" plain value={d.name} onChange={(v) => set("name", v)} />
        <TextField id="tc-vendor" label="厂商" help="thermal.cmd.vendor" value={d.vendor} onChange={(v) => set("vendor", v)} />
        <TextField id="tc-model" label="型号匹配规则" help="thermal.cmd.model" value={d.model_pattern} onChange={(v) => set("model_pattern", v)} />
        <div className="grid grid-cols-2 gap-2">
          <TextField id="tc-fw-min" label="最低 BMC 固件" help="thermal.cmd.firmware" value={d.firmware_min} onChange={(v) => set("firmware_min", v)} />
          <TextField id="tc-fw-max" label="最高 BMC 固件" plain value={d.firmware_max} onChange={(v) => set("firmware_max", v)} />
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <LabeledField label="恢复原厂温控的命令" help="thermal.cmd.restore">
          <textarea
            data-help-wrapped=""
            className={areaClass}
            spellCheck={false}
            placeholder="0x30 0x30 0x01 0x01"
            value={d.restore_commands}
            onChange={(e) => set("restore_commands", e.target.value)}
          />
        </LabeledField>
        <LabeledField label="设定转速的命令" help="thermal.cmd.set">
          <textarea
            data-help-wrapped=""
            className={areaClass}
            spellCheck={false}
            placeholder={"0x30 0x30 0x01 0x00\n0x30 0x30 0x02 0xff {pwm}"}
            value={d.set_commands}
            onChange={(e) => set("set_commands", e.target.value)}
          />
        </LabeledField>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <LabeledField label="转速刻度" help="thermal.cmd.scale">
          <select
            data-help-wrapped=""
            className="h-8 rounded-md border bg-card px-2 text-[0.8125rem] text-foreground"
            value={d.pwm_scale}
            onChange={(e) => set("pwm_scale", e.target.value as Draft["pwm_scale"])}
          >
            <option value="percent">百分比（0–100）</option>
            <option value="byte">0–255</option>
          </select>
        </LabeledField>
        <TextField id="tc-min" label="安全下限 %" help="thermal.cmd.safeMin" className="w-28" inputMode="numeric" value={d.safe_min_pwm} onChange={(v) => set("safe_min_pwm", v.trim())} />
        <TextField id="tc-max" label="上限 %" plain className="w-24" inputMode="numeric" value={d.safe_max_pwm} onChange={(v) => set("safe_max_pwm", v.trim())} />
        <CheckField label="启用" plain checked={d.enabled} onChange={(v) => set("enabled", v)} />
      </div>
      <TextField id="tc-note" label="备注（来源、验证情况）" plain value={d.note} onChange={(v) => set("note", v)} />
      {error ? (
        <InlineNotice tone="bad" role="alert">
          {error}
        </InlineNotice>
      ) : null}
      <div className="flex gap-2">
        <Button size="sm" disabled={saving} onClick={() => void save()}>
          保存
        </Button>
        <Button size="sm" variant="ghost" onClick={cancel}>
          取消
        </Button>
      </div>
    </div>
  )
}
