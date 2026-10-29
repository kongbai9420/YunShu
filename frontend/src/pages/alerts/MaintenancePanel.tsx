import { useState, type FormEvent } from "react"
import { Button } from "@/components/ui/button"
import { Card, InlineNotice } from "@/components/ui/controls"
import { CheckField } from "@/components/ui/form"
import { api } from "@/lib/api"
import { dateText, localInput as localTime, usePoll, utcDate } from "@/lib/monitor"
import { AssetChoices, ConfirmButton, Field, Input, split, useMutation } from "./shared"
interface Maintenance {
  id: string
  name: string
  starts_at: string
  ends_at: string
  asset_ids: string[]
  tags: string[]
}
export function MaintenancePanel({ editable }: { editable: boolean }) {
  const windows = usePoll<Maintenance[]>("/api/alerts/maintenance", 15000),
    [creating, setCreating] = useState(false),
    [editing, setEditing] = useState<Maintenance | null>(null),
    mut = useMutation()
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="flex-1 text-sm text-muted-foreground">
          维护窗口期间仍保留告警事件，但抑制范围内的通知。时间按当前浏览器时区填写。
        </p>
        {editable ? (
          <Button
            onClick={() => {
              setEditing(null)
              setCreating(true)
            }}
          >
            新增维护窗口
          </Button>
        ) : null}
      </div>
      {windows.error ? (
        <InlineNotice tone="bad" role="alert">
          {windows.error}
        </InlineNotice>
      ) : null}
      {mut.feedback}
      {creating || editing ? (
        <MaintenanceForm
          key={editing?.id ?? "new"}
          window={editing}
          saved={() => {
            setCreating(false)
            setEditing(null)
            windows.refresh()
          }}
          cancel={() => {
            setCreating(false)
            setEditing(null)
          }}
        />
      ) : null}
      <div className="grid min-w-0 grid-cols-1 gap-3 xl:grid-cols-2">
        {windows.data?.map((w) => (
          <Card key={w.id} title={w.name}>
            <div className="space-y-2 text-sm">
              <p>
                {dateText(w.starts_at)} → {dateText(w.ends_at)}
              </p>
              <p className="text-xs text-muted-foreground">
                {w.asset_ids.length || w.tags.length
                  ? `${w.asset_ids.length ? `${w.asset_ids.length} 台指定资产` : "资产不限"} · 标签 ${w.tags.join("、") || "不限"}`
                  : "全部资产"}
              </p>
              {editable ? (
                <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  disabled={mut.busy}
                  onClick={() => {
                    setCreating(false)
                    setEditing(w)
                  }}
                >
                  修改
                </Button>
                <ConfirmButton
                  label="删除维护窗口"
                  message={`删除“${w.name}”后，该窗口将不再抑制通知。`}
                  disabled={mut.busy}
                  onConfirm={async () => {
                    await mut.run(async () => {
                      await api.del(`/api/alerts/maintenance/${w.id}`)
                      windows.refresh()
                    }, "维护窗口已删除")
                  }}
                />
                </div>
              ) : null}
            </div>
          </Card>
        ))}
      </div>
      {windows.data?.length === 0 ? <InlineNotice>暂无维护窗口。</InlineNotice> : null}
    </div>
  )
}
function MaintenanceForm({ window: current, saved, cancel }: { window: Maintenance | null; saved: () => void; cancel: () => void }) {
  const [name, setName] = useState(current?.name ?? ""),
    [start, setStart] = useState(localTime(current ? utcDate(current.starts_at) : new Date())),
    [end, setEnd] = useState(localTime(current ? utcDate(current.ends_at) : new Date(Date.now() + 3600000))),
    [tags, setTags] = useState(current?.tags.join(", ") ?? ""),
    [ids, setIds] = useState<string[]>(current?.asset_ids ?? []),
    [all, setAll] = useState(current ? !current.asset_ids.length && !current.tags.length : false),
    mut = useMutation(),
    limited = ids.length > 0 || split(tags).length > 0
  async function save(e: FormEvent) {
    e.preventDefault()
    if (
      await mut.run(async () => {
        const starts = new Date(start),
          ends = new Date(end)
        if (!Number.isFinite(starts.getTime()) || !Number.isFinite(ends.getTime()) || ends <= starts)
          throw new Error("结束时间必须晚于开始时间")
        if (!limited && !all) throw new Error("请选择维护资产或填写标签；要对全部资产生效，请勾选“对全部资产生效”")
        const body = {
          name: name.trim(),
          starts_at: starts.toISOString(),
          ends_at: ends.toISOString(),
          asset_ids: ids,
          tags: split(tags),
          all_assets: !limited && all,
        }
        if (current) await api.put(`/api/alerts/maintenance/${current.id}`, body)
        else await api.post("/api/alerts/maintenance", body)
      })
    )
      saved()
  }
  return (
    <Card title={current ? `修改维护窗口：${current.name}` : "新建维护窗口"}>
      <form onSubmit={(e) => void save(e)} className="space-y-4">
        <fieldset disabled={mut.busy} className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="维护名称" help="alert.maintenance.window">
            <Input required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="开始时间" plain>
            <Input required type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} />
          </Field>
          <Field label="结束时间" plain>
            <Input required type="datetime-local" min={start} value={end} onChange={(e) => setEnd(e.target.value)} />
          </Field>
          <Field label="限定标签（逗号分隔）" help="alert.maintenance.tags">
            <Input maxLength={2000} value={tags} onChange={(e) => setTags(e.target.value)} />
          </Field>
        </fieldset>
        <details open={ids.length > 0}>
          <summary className="cursor-pointer text-sm text-link">选择维护资产{ids.length ? `（已选 ${ids.length} 台）` : ""}</summary>
          <AssetChoices value={ids} onChange={setIds} disabled={mut.busy} />
        </details>
        {limited ? null : (
          <CheckField
            label="对全部资产生效"
            checked={all}
            onChange={setAll}
            disabled={mut.busy}
            hint="没有选择资产也没有填写标签。勾选后，这段时间内全平台的告警都不发通知。"
          />
        )}
        {mut.feedback}
        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={mut.busy}>
            {mut.busy ? "正在保存…" : "保存维护窗口"}
          </Button>
          <Button type="button" variant="outline" disabled={mut.busy} onClick={cancel}>
            取消编辑
          </Button>
        </div>
      </form>
    </Card>
  )
}
