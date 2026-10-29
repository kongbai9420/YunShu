import { useCallback, useEffect, useState } from "react"

import { InlineNotice } from "@/components/ui/controls"
import { api } from "@/lib/api"
import { MatchList, type Match } from "@/pages/vulns/MatchList"
import { VulnDialog } from "@/pages/vulns/VulnsPage"

/** 资产详情里的“漏洞”页签（4.3 P18d）：这台资产命中的漏洞与处置。 */
export function AssetVulns({ assetId, editable }: { assetId: string; editable: boolean }) {
  const [rows, setRows] = useState<Match[] | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [error, setError] = useState("")
  const load = useCallback(() => void api.get<Match[]>(`/api/vuln/matches?asset_id=${assetId}`).then(setRows, (e: Error) => setError(e.message)), [assetId])
  useEffect(load, [load])
  if (error) return <InlineNotice tone="bad" role="alert">{error}</InlineNotice>
  if (!rows) return <p className="text-[0.8125rem] text-muted-foreground">正在加载…</p>
  return (
    <div className="space-y-2 text-[0.8125rem]">
      <p className="text-xs text-muted-foreground">按“软件与版本”比对漏洞数据得出。没有采集到版本、或漏洞数据还没同步时，这里为空不代表没有漏洞。</p>
      <MatchList matches={rows} editable={editable} showRecord onChanged={load} onOpenRecord={setOpen} />
      {open ? <VulnDialog id={open} editable={editable} onClose={() => { setOpen(null); load() }} /> : null}
    </div>
  )
}
