import { useEffect, useMemo, useState } from "react"

import { PageHeader } from "@/components/AppShell"
import { Button } from "@/components/ui/button"
import { Card, InlineNotice, Segmented, Select } from "@/components/ui/controls"
import { LabeledField } from "@/components/ui/form"
import { ApiError, api, query, type Site } from "@/lib/api"
import { canEdit, seesAll, useAuth } from "@/lib/auth"
import { dateText } from "@/lib/monitor"

type Tab = "racks" | "locate" | "types"
type Face = "front" | "rear"

interface RackSummary { id: string; name: string; site_id: string; height_u: number; free_u: Record<Face, number>; power_watts: number; placed: number }
interface RackItem { id: string | null; name: string; unit: number; height: number; faces: Face[]; full_depth: boolean; model: string; power_watts: number | null }
interface Elevation { rack: { id: string; name: string; height_u: number }; items: RackItem[]; unplaced: { id: string; name: string }[]; free_u: Record<Face, number>; power_watts: number }
interface Location { switch_id: string; switch_name: string; port: string; vlan: number | null; mac: string; macs_on_port: number; uplink: boolean; last_seen_at: string }
interface LocateResult { kind: string; macs: string[]; ips?: string[]; assets?: { id: string; name: string }[]; locations: Location[]; message: string }
interface DeviceType { id: string; manufacturer: string; model: string; part_number: string; u_height: number; is_full_depth: boolean; interfaces: number; power_ports: number; source: string }

/** 机柜 U 位、按 MAC / IP 找端口、设备型号库（4.2 P14） */
export function RacksPage() {
  const [tab, setTab] = useState<Tab>("racks")
  return (
    <div className="min-w-0 pb-8">
      <PageHeader title="机柜与定位" description="查看机柜 U 位与功耗，按 MAC 或 IP 找到设备接在哪台交换机的哪个口，维护设备型号库。" />
      <div className="min-w-0 space-y-4 px-6">
        <Segmented label="机柜与定位内容" value={tab} onChange={setTab}
          options={[{ value: "racks", label: "机柜" }, { value: "locate", label: "查找 MAC / IP" }, { value: "types", label: "设备型号" }]} />
        {tab === "racks" ? <RackPanel /> : tab === "locate" ? <LocatePanel /> : <TypesPanel />}
      </div>
    </div>
  )
}

function RackPanel() {
  const [sites, setSites] = useState<Site[]>([])
  const [siteId, setSiteId] = useState("")
  const [racks, setRacks] = useState<RackSummary[] | null>(null)
  const [rackId, setRackId] = useState("")
  const [view, setView] = useState<Elevation | null>(null)
  const [error, setError] = useState("")

  useEffect(() => { void api.get<Site[]>("/api/sites").then(setSites, (e: Error) => setError(e.message)) }, [])
  useEffect(() => {
    setRacks(null)
    void api.get<RackSummary[]>(`/api/racks/summary${query({ site_id: siteId || undefined })}`).then((r) => {
      setRacks(r)
      setRackId((cur) => (r.some((x) => x.id === cur) ? cur : r[0]?.id ?? ""))
    }, (e: Error) => setError(e.message))
  }, [siteId])
  useEffect(() => {
    setView(null)
    if (rackId) void api.get<Elevation>(`/api/racks/${rackId}/elevation`).then(setView, (e: Error) => setError(e.message))
  }, [rackId])

  return (
    <div className="space-y-4">
      {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
      <div className="flex flex-wrap items-end gap-3">
        <Select id="rack-site" plain label="场所" value={siteId} onChange={setSiteId}
          options={[{ value: "", label: "全部场所" }, ...sites.map((s) => ({ value: s.id, label: s.name }))]} />
      </div>
      {racks === null ? <p className="text-[0.8125rem] text-muted-foreground">正在读取…</p> : racks.length === 0 ? (
        <InlineNotice>还没有机柜。在“平台设置 → 场所与机柜”里添加机柜，再在资产里填写机柜与 U 位。</InlineNotice>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[32rem] text-[0.8125rem]" aria-label="机柜概况">
            <thead className="text-left text-xs text-muted-foreground">
              <tr><th className="py-1.5 pr-3 font-medium">机柜</th><th className="pr-3 font-medium">高度</th><th className="pr-3 font-medium">已放置</th><th className="pr-3 font-medium">空余（正 / 背）</th><th className="font-medium">当前功耗</th></tr>
            </thead>
            <tbody>
              {racks.map((r) => (
                <tr key={r.id} className={`cursor-pointer border-t ${r.id === rackId ? "bg-secondary" : ""}`} onClick={() => setRackId(r.id)}>
                  <td className="py-1.5 pr-3"><button type="button" className="font-medium underline-offset-2 hover:underline" onClick={() => setRackId(r.id)}>{r.name}</button></td>
                  <td className="pr-3 tabular">{r.height_u}U</td>
                  <td className="pr-3 tabular">{r.placed}</td>
                  <td className="pr-3 tabular">{r.free_u.front}U / {r.free_u.rear}U</td>
                  <td className="tabular">{r.power_watts ? `${r.power_watts} W` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {view ? <RackElevation view={view} /> : null}
    </div>
  )
}

function RackElevation({ view }: { view: Elevation }) {
  const units = useMemo(() => Array.from({ length: view.rack.height_u }, (_, i) => view.rack.height_u - i), [view.rack.height_u])
  const at = (face: Face, u: number) => view.items.find((i) => i.faces.includes(face) && i.height > 0 && u >= i.unit && u < i.unit + i.height)
  return (
    <Card title={`${view.rack.name} · ${view.rack.height_u}U`} actions={<span className="text-xs text-muted-foreground">合计 {view.power_watts || 0} W</span>}>
      <div className="grid gap-4 sm:grid-cols-2">
        {(["front", "rear"] as Face[]).map((face) => (
          <section key={face} aria-label={face === "front" ? "正面" : "背面"}>
            <h3 className="mb-1 text-xs font-medium text-muted-foreground">{face === "front" ? "正面" : "背面"} · 空余 {view.free_u[face]}U</h3>
            <ol className="rounded-md border text-xs">
              {units.map((u) => {
                const item = at(face, u)
                if (item && u !== item.unit + item.height - 1) return null // 多 U 设备只在顶部画一次
                return (
                  <li key={u} className="flex border-t first:border-t-0" style={{ minHeight: `${(item?.height ?? 1) * 1.5}rem` }}>
                    <span className="w-9 shrink-0 border-r px-1 py-0.5 text-right tabular text-muted-foreground">{item && item.height > 1 ? `${item.unit}-${item.unit + item.height - 1}` : u}</span>
                    {item ? (
                      <span className={`flex min-w-0 flex-1 flex-col justify-center px-2 ${item.id ? "bg-info/10" : "bg-muted text-muted-foreground"}`}>
                        {item.id ? <a className="truncate font-medium hover:underline" href={`#/assets?asset=${item.id}`}>{item.name}</a> : <span>{item.name}</span>}
                        {item.model ? <span className="truncate text-muted-foreground">{item.model}{item.full_depth ? "" : " · 半深"}</span> : null}
                      </span>
                    ) : <span className="flex-1" />}
                  </li>
                )
              })}
            </ol>
          </section>
        ))}
      </div>
      {view.unplaced.length ? <p className="mt-3 text-xs text-muted-foreground">在此机柜但未填 U 位：{view.unplaced.map((a) => a.name).join("、")}</p> : null}
      {view.items.some((i) => i.height === 0) ? <p className="mt-1 text-xs text-muted-foreground">0U 设备（如竖装 PDU）不占 U 位：{view.items.filter((i) => i.height === 0).map((i) => i.name).join("、")}</p> : null}
    </Card>
  )
}

function LocatePanel() {
  const [q, setQ] = useState("")
  const [result, setResult] = useState<LocateResult | null>(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  async function search(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError("")
    try { setResult(await api.get<LocateResult>(`/api/locate${query({ q: q.trim() })}`)) } catch (err) { setError(err instanceof ApiError ? err.message : "查询失败") } finally { setBusy(false) }
  }
  return (
    <div className="space-y-3">
      <form onSubmit={search} className="flex flex-wrap items-end gap-2">
        <LabeledField label="MAC 或 IP 地址" help="racks.locate" className="flex-1 sm:max-w-sm">
          <input data-help-wrapped="" className="h-8 rounded-md border bg-card px-2 text-[0.8125rem]" value={q} onChange={(e) => setQ(e.target.value)} placeholder="00:11:22:33:44:55 或 192.0.2.10" />
        </LabeledField>
        <Button type="submit" disabled={!q.trim() || busy}>{busy ? "正在查找…" : "查找"}</Button>
      </form>
      <p className="text-xs text-muted-foreground">数据来自交换机的 MAC 地址表和 ARP 表，每台交换机约 10 分钟更新一次，离线后保留最后位置 7 天。只作参考，不会据此合并资产或认定连线。</p>
      {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
      {result ? (
        <section aria-label="查找结果" className="space-y-2 text-[0.8125rem]">
          {result.macs.length ? <p>MAC：{result.macs.join("、")}{result.ips?.length ? ` · IP：${result.ips.join("、")}` : ""}</p> : null}
          {result.assets?.length ? <p>管理地址相同的资产：{result.assets.map((a) => <a key={a.id} className="mr-2 underline" href={`#/assets?asset=${a.id}`}>{a.name}</a>)}</p> : null}
          {result.locations.length ? (
            <ul className="space-y-1">
              {result.locations.map((l) => (
                <li key={`${l.switch_id}-${l.port}-${l.mac}`} className="rounded-md border px-3 py-2">
                  <strong>{l.switch_name}</strong> · {l.port}{l.vlan != null ? ` · VLAN ${l.vlan}` : ""}
                  <span className={`ml-2 rounded px-1.5 py-0.5 text-xs ${l.uplink ? "bg-muted text-muted-foreground" : "bg-info/10"}`}>{l.uplink ? "上联口" : "可能的接入口"}</span>
                  <span className="block text-xs text-muted-foreground">该口学到 {l.macs_on_port} 个 MAC · 最近看到 {dateText(l.last_seen_at)}</span>
                </li>
              ))}
            </ul>
          ) : <InlineNotice>{result.message}</InlineNotice>}
        </section>
      ) : null}
    </div>
  )
}

function TypesPanel() {
  const { user } = useAuth()
  const [q, setQ] = useState("")
  const [rows, setRows] = useState<{ items: DeviceType[]; total: number } | null>(null)
  const [notice, setNotice] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const isAdmin = user?.role === "admin"
  const load = (text = q) => void api.get<{ items: DeviceType[]; total: number }>(`/api/device-types${query({ q: text.trim() || undefined })}`).then(setRows, (e: Error) => setError(e.message))
  useEffect(() => load(""), []) // eslint-disable-line react-hooks/exhaustive-deps

  async function upload(file: File | undefined, manufacturers: string) {
    if (!file) return
    setBusy(true)
    setError("")
    const form = new FormData()
    form.append("file", file)
    form.append("manufacturers", manufacturers)
    try {
      const r = await api.upload<{ created: number; updated: number; unchanged: number; kept_manual: number; error_count: number }>("/api/device-types/import", form)
      setNotice(`导入完成：新增 ${r.created}、更新 ${r.updated}、未变 ${r.unchanged}、保留手工录入 ${r.kept_manual}${r.error_count ? `、跳过 ${r.error_count} 个无法读取的文件` : ""}。`)
      load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "导入失败")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-3 text-[0.8125rem]">
      <p className="text-xs text-muted-foreground">型号决定设备在机柜里占几个 U、是否前后两面都占。可以从社区维护的 devicetype-library（CC0 公有领域）导入：在 GitHub 下载该仓库的 ZIP，再在这里上传；平台不会自己联网下载。</p>
      {isAdmin ? <ImportForm busy={busy} onUpload={upload} /> : <InlineNotice>导入型号库需要管理员账号{canEdit(user) && seesAll(user) ? "；运维可以在资产里选择已有型号" : ""}。</InlineNotice>}
      {notice ? <InlineNotice tone="ok" role="status">{notice}</InlineNotice> : null}
      {error ? <InlineNotice tone="bad" role="alert">{error}</InlineNotice> : null}
      <form className="flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); load() }}>
        <input aria-label="搜索型号" data-help-plain="" className="h-8 min-w-0 flex-1 rounded-md border bg-card px-2 sm:max-w-xs" value={q} onChange={(e) => setQ(e.target.value)} placeholder="厂商、型号或部件号" />
        <Button type="submit" variant="outline">搜索</Button>
      </form>
      {rows ? (
        <>
          <p className="text-xs text-muted-foreground">共 {rows.total} 个型号{rows.items.length < rows.total ? `，显示前 ${rows.items.length} 个` : ""}</p>
          <ul className="divide-y rounded-md border" aria-label="设备型号">
            {rows.items.map((t) => (
              <li key={t.id} className="flex flex-wrap justify-between gap-2 px-3 py-2">
                <span className="min-w-0 break-words"><strong>{t.manufacturer}</strong> {t.model}{t.part_number ? <span className="text-muted-foreground">（{t.part_number}）</span> : null}</span>
                <span className="text-xs text-muted-foreground">{t.u_height}U · {t.is_full_depth ? "全深" : "半深"} · {t.interfaces} 个接口 · {t.power_ports} 个电源口 · {t.source === "manual" ? "手工录入" : "型号库"}</span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  )
}

function ImportForm({ busy, onUpload }: { busy: boolean; onUpload: (file: File | undefined, manufacturers: string) => void }) {
  const [file, setFile] = useState<File | undefined>()
  const [manufacturers, setManufacturers] = useState("Dell, HPE, Lenovo, Supermicro, Huawei, H3C, Cisco, MikroTik, Ubiquiti, Synology")
  return (
    <div className="flex flex-wrap items-end gap-2 rounded-md border p-3">
      <LabeledField label="型号库 ZIP" help="racks.typeImport">
        <input data-help-wrapped="" type="file" accept=".zip" onChange={(e) => setFile(e.target.files?.[0])} />
      </LabeledField>
      <LabeledField label="只导入这些厂商" hint="逗号分隔，按厂商名匹配，不区分大小写；留空导入全部" className="flex-1">
        <input data-help-wrapped="" className="h-8 rounded-md border bg-card px-2" value={manufacturers} onChange={(e) => setManufacturers(e.target.value)} />
      </LabeledField>
      <Button disabled={!file || busy} onClick={() => onUpload(file, manufacturers)}>{busy ? "正在导入…" : "导入"}</Button>
    </div>
  )
}
