import { useCallback, useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { TextField } from "@/components/ui/form"
import { ApiError, api } from "@/lib/api"
import { useLiveEvents } from "@/lib/live"
import { dateText } from "@/lib/monitor"

/** 软件与版本（4.3 P18b）：各协议采集到的系统、固件、虚拟化版本，以及 SSH 读取的软件包清单。漏洞比对以此为依据。 */

interface Item { kind: string; label: string; version: string; detail: string; cpe: string; ecosystem: string; source: string; observed_at: string | null }
interface Scan { endpoint_id: string; state: string; message: string; manager: string; ecosystem: string; count: number; last_success_at: string | null; last_attempt_at: string | null }
interface Software { items: Item[]; package_scans: Scan[]; packages_enabled: string[] }
interface Pkg { name: string; version: string; source: string; source_version: string }

const KIND: Record<string, string> = { os: "系统", firmware: "固件", bios: "BIOS", hypervisor: "虚拟化", appliance: "设备系统" }
const SCAN_STATE: Record<string, string> = { ok: "成功", failed: "失败", unsupported: "不支持", pending: "等待中" }

export function SoftwarePanel({ assetId, editable }: { assetId: string; editable: boolean }) {
  const [data, setData] = useState<Software | null>(null)
  const [pkgs, setPkgs] = useState<Pkg[] | null>(null)
  const [q, setQ] = useState("")
  const [error, setError] = useState("")
  const [note, setNote] = useState("")

  const load = useCallback(() => void api.get<Software>(`/api/assets/${assetId}/software`).then(setData, (e: Error) => setError(e.message)), [assetId])
  useEffect(load, [load])
  // 刚加的资产常常是先打开详情、采集随后才完成：这台资产有采集成功的推送时重新读取，不用关掉再开
  useLiveEvents({
    collection: (e) => {
      if (!e.snapshot && e.items.some((i) => i.asset_id === assetId && i.state === "ok")) load()
    },
    onRefetch: load,
  })

  async function search(e?: React.FormEvent) {
    e?.preventDefault()
    try {
      setPkgs(await api.get<Pkg[]>(`/api/assets/${assetId}/packages?q=${encodeURIComponent(q.trim())}&limit=200`))
    } catch (err) {
      setError((err as Error).message)
    }
  }

  async function scan() {
    setError("")
    try {
      const r = await api.post<{ queued: number }>(`/api/assets/${assetId}/software/scan`)
      setNote(`已安排读取（${r.queued} 个端点），稍后刷新查看`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "安排读取失败")
    }
  }

  if (!data) return error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : <p className="text-[0.8125rem] text-muted-foreground">正在加载…</p>
  const totalPkgs = data.package_scans.reduce((n, s) => n + s.count, 0)

  return (
    <div className="space-y-4 text-[0.8125rem]">
      <p className="text-xs text-muted-foreground">采集到的版本号，用于漏洞比对。没有出现的设备类型表示暂未覆盖，不代表没有漏洞。</p>
      {data.items.length === 0 ? (
        <p className="text-muted-foreground">还没有采集到版本信息。需要管理端点采集成功一次（SNMP、Redfish、SSH、PVE / ESXi 接口都会带回版本）。</p>
      ) : (
        <table className="w-full text-left text-xs">
          <caption className="sr-only">软件与版本</caption>
          <thead><tr className="text-muted-foreground"><th className="py-1">类别</th><th>名称</th><th>版本</th><th>比对依据</th><th>来源</th></tr></thead>
          <tbody>
            {data.items.map((i) => (
              <tr key={`${i.label}-${i.version}`} className="border-t align-top">
                <td className="py-1">{KIND[i.kind] ?? i.kind}</td>
                <td>{i.label}{i.detail ? <div className="text-muted-foreground">{i.detail}</div> : null}</td>
                <td className="tabular break-all">{i.version}</td>
                <td className="break-all">{i.cpe || i.ecosystem || <span className="text-muted-foreground">暂不比对</span>}</td>
                <td>{i.source}{i.observed_at ? <div className="text-muted-foreground">{dateText(i.observed_at)}</div> : null}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <section aria-labelledby="pkg-title" className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h4 id="pkg-title" className="font-medium">软件包清单{totalPkgs ? `（${totalPkgs} 个）` : ""}</h4>
          {editable && data.packages_enabled.length ? <Button size="sm" variant="outline" onClick={() => void scan()}>立即读取</Button> : null}
        </div>
        {data.packages_enabled.length === 0 ? (
          <p className="text-xs text-muted-foreground">未开启。在“管理端点”里给 SSH 端点勾选“每天读取软件包清单”后，平台每天用只读命令读取一次。</p>
        ) : null}
        {data.package_scans.map((s) => (
          <p key={s.endpoint_id} className="text-xs">
            最近读取：{SCAN_STATE[s.state] ?? s.state}
            {s.last_attempt_at ? `（${dateText(s.last_attempt_at)}）` : ""}
            {s.ecosystem ? ` · ${s.ecosystem}` : ""}{s.manager ? ` · ${s.manager}` : ""}
            {s.message ? <span className="text-muted-foreground"> · {s.message}</span> : null}
          </p>
        ))}
        {note ? <InlineNotice tone="ok">{note}</InlineNotice> : null}
        {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
        {totalPkgs ? (
          <form onSubmit={search} className="flex flex-wrap items-end gap-2">
            <TextField id="pkg-q" label="查找软件包" value={q} onChange={setQ} placeholder="如 openssl" plain />
            <Button type="submit" variant="outline" size="sm">查找</Button>
          </form>
        ) : null}
        {pkgs ? (
          pkgs.length ? (
            <ul className="max-h-72 divide-y overflow-y-auto rounded-md border text-xs" aria-label="软件包">
              {pkgs.map((p) => (
                <li key={p.name} className="flex flex-wrap justify-between gap-2 px-2 py-1">
                  <span>{p.name}{p.source !== p.name ? <span className="text-muted-foreground">（源码包 {p.source}）</span> : null}</span>
                  <span className="tabular">{p.version}</span>
                </li>
              ))}
            </ul>
          ) : <p className="text-xs text-muted-foreground">没有找到。</p>
        ) : null}
      </section>
    </div>
  )
}
