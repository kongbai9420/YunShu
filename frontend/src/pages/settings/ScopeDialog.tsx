import { useEffect, useMemo, useState } from "react"
import { Dialog } from "radix-ui"

import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { CheckField } from "@/components/ui/form"
import { HelpPill } from "@/components/HelpPill"
import { ModalContent, ModalFooter, ModalHeader } from "@/components/ui/dialog"
import { api, query, type Site, type User } from "@/lib/api"

interface ScopeData { scope_all: boolean; site_ids: string[]; asset_ids: string[] }
interface Preview { all: boolean; total: number; items: { id: string; name: string; site_name: string | null }[] }
interface AssetLite { id: string; name: string; asset_no: string | null }

/** 管理员编辑账号的资产范围并预览实际可见资产（4.1 P11） */
export function ScopeDialog({ user, onClose, onSaved }: { user: User; onClose: () => void; onSaved: () => void }) {
  const [scope, setScope] = useState<ScopeData | null>(null)
  const [sites, setSites] = useState<Site[]>([])
  const [names, setNames] = useState<Record<string, string>>({})
  const [q, setQ] = useState("")
  const [found, setFound] = useState<AssetLite[]>([])
  const [preview, setPreview] = useState<Preview | null>(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    void Promise.all([api.get<ScopeData>(`/api/users/${user.id}/scope`), api.get<Site[]>("/api/sites")])
      .then(async ([s, all]) => {
        setScope({ scope_all: s.scope_all, site_ids: s.site_ids, asset_ids: s.asset_ids })
        setSites(all)
        if (s.asset_ids.length) {
          const p = await api.get<Preview>(`/api/users/${user.id}/scope/preview`)
          setNames(Object.fromEntries(p.items.map((i) => [i.id, i.name])))
        }
      })
      .catch((e: Error) => setError(e.message))
  }, [user.id])

  // 按上级排成树形顺序，便于看出“授权上级即包含下级”
  const ordered = useMemo(() => {
    const kids = new Map<string | null, Site[]>()
    for (const s of sites) kids.set(s.parent_id ?? null, [...(kids.get(s.parent_id ?? null) ?? []), s])
    const out: { site: Site; depth: number }[] = []
    const walk = (parent: string | null, depth: number) => {
      for (const s of (kids.get(parent) ?? []).sort((a, b) => a.name.localeCompare(b.name, "zh-CN"))) {
        out.push({ site: s, depth })
        walk(s.id, depth + 1)
      }
    }
    walk(null, 0)
    return out
  }, [sites])

  const search = async () => {
    const page = await api.get<{ items: AssetLite[] }>(`/api/assets${query({ q: q.trim(), limit: 20 })}`)
    setFound(page.items)
  }
  const toggle = (key: "site_ids" | "asset_ids", id: string) => {
    if (!scope) return
    const list = scope[key]
    setScope({ ...scope, [key]: list.includes(id) ? list.filter((x) => x !== id) : [...list, id] })
    setPreview(null)
  }
  const save = async () => {
    if (!scope) return
    setBusy(true)
    setError("")
    try {
      await api.put(`/api/users/${user.id}/scope`, scope)
      setPreview(await api.get<Preview>(`/api/users/${user.id}/scope/preview`))
      onSaved()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog.Root open onOpenChange={(o) => (!o ? onClose() : undefined)}>
      <ModalContent width={620}>
        <ModalHeader title={`资产范围：${user.display_name || user.username}`}
          description="在角色之上限定这个账号能看到和操作的资产。授权场所包含它的全部下级场所；宿主可见不会带出它上面的虚拟机。取消“全部可见”且不选任何内容时，什么资产都看不到。" />
        {error ? <InlineNotice tone="bad" role="alert" className="mt-3">{error}</InlineNotice> : null}
        {!scope ? <p className="mt-3 text-[0.8125rem] text-muted-foreground">正在读取…</p> : (
          <div className="mt-3 max-h-[60vh] space-y-4 overflow-y-auto text-[0.8125rem]">
            <CheckField className="font-medium" label="可见全部资产（升级前的账号默认如此）" help="settings.scope.all" checked={scope.scope_all}
              onChange={(checked) => { setScope({ ...scope, scope_all: checked }); setPreview(null) }} />
            <fieldset disabled={scope.scope_all} className="space-y-1 disabled:opacity-50">
              <legend className="mb-1 inline-flex items-center text-xs font-medium text-muted-foreground">授权场所（含全部下级）<HelpPill topic="settings.site.parent" /></legend>
              {ordered.map(({ site, depth }) => (
                <label key={site.id} className="flex items-center gap-2" style={{ paddingLeft: depth * 16 }}>
                  <input type="checkbox" data-help-plain="" checked={scope.site_ids.includes(site.id)} onChange={() => toggle("site_ids", site.id)} />
                  {site.name}
                </label>
              ))}
              {ordered.length === 0 ? <p className="text-xs text-muted-foreground">还没有场所。</p> : null}
            </fieldset>
            <fieldset disabled={scope.scope_all} className="space-y-2 disabled:opacity-50">
              <legend className="mb-1 text-xs font-medium text-muted-foreground">单独授权的资产</legend>
              <ul className="space-y-1" aria-label="已授权资产">
                {scope.asset_ids.map((id) => (
                  <li key={id} className="flex items-center justify-between gap-2">
                    <span className="break-all">{names[id] ?? id}</span>
                    <Button size="xs" variant="ghost" onClick={() => toggle("asset_ids", id)}>移除</Button>
                  </li>
                ))}
              </ul>
              <div className="flex flex-wrap items-center gap-2">
                <input aria-label="搜索资产" data-help-plain="" className="h-8 min-w-0 flex-1 rounded-md border bg-card px-2" value={q} onChange={(e) => setQ(e.target.value)} placeholder="名称、编号或地址" />
                <Button size="xs" variant="outline" onClick={() => void search()} disabled={!q.trim()}>搜索</Button>
              </div>
              <ul className="space-y-1" aria-label="搜索结果">
                {found.filter((a) => !scope.asset_ids.includes(a.id)).map((a) => (
                  <li key={a.id} className="flex items-center justify-between gap-2">
                    <span className="break-all">{a.name}{a.asset_no ? `（${a.asset_no}）` : ""}</span>
                    <Button size="xs" variant="outline" onClick={() => { setNames((n) => ({ ...n, [a.id]: a.name })); toggle("asset_ids", a.id) }}>加入</Button>
                  </li>
                ))}
              </ul>
            </fieldset>
            {preview ? (
              <section aria-label="实际可见资产" className="rounded-md bg-secondary p-2">
                <p className="font-medium">{preview.all ? "可见全部资产" : `实际可见 ${preview.total} 项资产`}</p>
                {!preview.all ? <p className="mt-1 break-words text-xs text-muted-foreground">{preview.items.map((i) => `${i.name}${i.site_name ? `（${i.site_name}）` : ""}`).join("、") || "无"}{preview.total > preview.items.length ? " …" : ""}</p> : null}
              </section>
            ) : null}
          </div>
        )}
        <ModalFooter>
          <Dialog.Close asChild><Button variant="outline">关闭</Button></Dialog.Close>
          <Button disabled={!scope || busy} onClick={() => void save()}>{busy ? "正在保存…" : "保存并预览"}</Button>
        </ModalFooter>
      </ModalContent>
    </Dialog.Root>
  )
}
