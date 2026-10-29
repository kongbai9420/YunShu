import { useCallback, useEffect, useState } from "react"
import { RotateCcw, Trash2 } from "lucide-react"

import { Button } from "@/components/ui/button"
import { InlineNotice, panelClass } from "@/components/ui/controls"
import { LabeledField } from "@/components/ui/form"
import { api } from "@/lib/api"
import { dateText, utcDate } from "@/lib/monitor"
import { FORM_LABEL, LIFECYCLE_LABEL, ROLE_LABEL, labelOf } from "@/lib/vocab"
import { cn } from "@/lib/utils"

interface BinItem {
  id: string
  name: string
  asset_no: string | null
  form: string
  role: string
  deleted_at: string
  deleted_by: string
  deleted_lifecycle: string
  purge_at: string
}

const DAY = 86_400_000

/** 剩余天数：不足一天显示“不到 1 天”，到期后 Worker 一小时内清理 */
function daysLeft(purgeAt: string, now: number): string {
  const ms = utcDate(purgeAt).getTime() - now
  if (ms <= 0) return "即将删除"
  return ms < DAY ? "不到 1 天" : `${Math.ceil(ms / DAY)} 天`
}

/** 资产回收站（P22）：运维和管理员可恢复；只有管理员能彻底删除和修改保留天数 */
export function RecycleBinPanel({ admin, onRestored }: { admin: boolean; onRestored: () => void }) {
  const [items, setItems] = useState<BinItem[] | null>(null)
  const [days, setDays] = useState(30)
  const [draftDays, setDraftDays] = useState("30")
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [purging, setPurging] = useState<string | null>(null)

  const load = useCallback(() => {
    api
      .get<{ retention_days: number; items: BinItem[] }>("/api/assets/recycle-bin")
      .then((r) => {
        setItems(r.items)
        setDays(r.retention_days)
        setDraftDays(String(r.retention_days))
      })
      .catch((e: Error) => setError(e.message))
  }, [])
  useEffect(load, [load])

  async function act(work: () => Promise<string>) {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      setNotice(await work())
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
      setPurging(null)
      load()
    }
  }

  const restore = (a: BinItem) =>
    act(async () => {
      const r = await api.post<{ notice: string }>(`/api/assets/${a.id}/restore`, {})
      onRestored()
      return `已恢复“${a.name}”${r.notice ? `。${r.notice}` : ""}`
    })
  const purge = (a: BinItem) =>
    act(async () => {
      await api.del(`/api/assets/${a.id}/purge`)
      return `已彻底删除“${a.name}”`
    })
  const parsedDays = Number(draftDays)
  const daysValid = Number.isInteger(parsedDays) && parsedDays >= 1 && parsedDays <= 365
  const saveDays = () =>
    act(async () => {
      const r = await api.put<{ retention_days: number }>("/api/assets/recycle-bin/settings", { retention_days: parsedDays })
      return `保留期已改为 ${r.retention_days} 天`
    })
  const now = Date.now()

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <p className="flex-1 text-[0.8125rem] text-muted-foreground">
          删除的资产在这里保留 {days} 天，期间不采集、不告警、不出现在任何列表里；恢复后回到删除前的状态。到期后自动彻底删除。
        </p>
        {admin ? (
          <form
            className="flex items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              if (daysValid && parsedDays !== days) void saveDays()
            }}
          >
            <LabeledField label="保留天数" help="asset.recycle.days" hint="1–365 天">
              <input
                data-help-wrapped=""
                type="number"
                min={1}
                max={365}
                step={1}
                required
                value={draftDays}
                onChange={(e) => setDraftDays(e.target.value)}
                className="h-8 w-24 rounded-md border border-input bg-card px-2 text-[0.8125rem]"
              />
            </LabeledField>
            <Button type="submit" size="sm" variant="outline" disabled={busy || !daysValid || parsedDays === days}>
              保存
            </Button>
          </form>
        ) : null}
      </div>

      {error ? (
        <InlineNotice tone="bad" role="alert">
          {error}
        </InlineNotice>
      ) : notice ? (
        <InlineNotice tone="ok">{notice}</InlineNotice>
      ) : null}

      <div className={cn(panelClass, "overflow-x-auto")}>
        <table className="data-table min-w-full whitespace-nowrap">
          <caption className="sr-only">回收站里的资产，按删除时间从新到旧排列</caption>
          <thead>
            <tr>
              <th scope="col">名称</th>
              <th scope="col">资产编号</th>
              <th scope="col">形态 / 角色</th>
              <th scope="col">删除时间</th>
              <th scope="col">删除人</th>
              <th scope="col">剩余</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            {items?.map((a) => (
              <tr key={a.id}>
                <th scope="row" className="font-medium">
                  {a.name}
                  {a.deleted_lifecycle && a.deleted_lifecycle !== "active" ? (
                    <span className="ml-1.5 text-xs font-normal text-muted-foreground">删除前：{labelOf(LIFECYCLE_LABEL, a.deleted_lifecycle)}</span>
                  ) : null}
                </th>
                <td>{a.asset_no ?? <span className="text-muted-foreground">无</span>}</td>
                <td>
                  {labelOf(FORM_LABEL, a.form)} / {labelOf(ROLE_LABEL, a.role)}
                </td>
                <td>{dateText(a.deleted_at)}</td>
                <td>{a.deleted_by || <span className="text-muted-foreground">未知</span>}</td>
                <td title={`到期时间：${dateText(a.purge_at)}`}>{daysLeft(a.purge_at, now)}</td>
                <td>
                  {purging === a.id ? (
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="text-xs">彻底删除后不能恢复，管理端点和关联一并删除。</span>
                      <Button type="button" size="xs" variant="destructive" disabled={busy} onClick={() => void purge(a)}>
                        确认彻底删除
                      </Button>
                      <Button type="button" size="xs" variant="outline" disabled={busy} onClick={() => setPurging(null)}>
                        取消
                      </Button>
                    </span>
                  ) : (
                    <span className="flex gap-2">
                      <Button type="button" size="xs" variant="outline" disabled={busy} onClick={() => void restore(a)}>
                        <RotateCcw aria-hidden />
                        恢复
                      </Button>
                      {admin ? (
                        <Button type="button" size="xs" variant="ghost" disabled={busy} onClick={() => setPurging(a.id)}>
                          <Trash2 aria-hidden />
                          彻底删除
                        </Button>
                      ) : null}
                    </span>
                  )}
                </td>
              </tr>
            ))}
            {items && items.length === 0 ? (
              <tr>
                <td colSpan={7} className="py-10 text-center text-muted-foreground">
                  回收站是空的。
                </td>
              </tr>
            ) : null}
            {!items && !error ? (
              <tr>
                <td colSpan={7} className="py-10 text-center text-muted-foreground">
                  正在加载…
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  )
}
