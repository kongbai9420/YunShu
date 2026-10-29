import { Dialog } from "radix-ui"

import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { ModalContent, ModalFooter, ModalHeader } from "@/components/ui/dialog"
import type { RescanPreview } from "@/lib/topology"

function ports(a: string, b: string) {
  const p = [a, b].filter(Boolean)
  return p.length ? `（${a || "?"} ↔ ${b || "?"}）` : ""
}

/** 重新扫描的差异预览：先看清楚再合并；只动候选，已确认 / 人工 / 已忽略与布局都写明不受影响 */
export function RescanDialog({
  open,
  onOpenChange,
  loading,
  preview,
  error,
  applying,
  onApply,
  onRetry,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  loading: boolean
  preview: RescanPreview | null
  error: string
  applying: boolean
  onApply: () => void
  onRetry: () => void
}) {
  const empty = preview ? preview.added.length + preview.removed.length + preview.refreshed.length === 0 : true
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <ModalContent width={600}>
        <ModalHeader
          title="重新扫描：差异预览"
          showClose={false}
          description={
            <>
              根据最近一次采集到的 LLDP 邻居表生成候选链路，不会连接或改动任何设备。合并只改动候选；
              {preview
                ? ` ${preview.protected} 条已确认或人工链路、已忽略的记录和所有视图布局（含锁定位置）都不受影响。`
                : " 已确认、人工链路和视图布局都不受影响。"}
            </>
          }
        />

        {loading ? <p className="mt-4 text-muted-foreground">正在计算差异…</p> : null}
        {error ? (
          <InlineNotice tone="bad" role="alert" className="mt-4">
            {error}
            <Button size="xs" variant="outline" className="ml-2" onClick={onRetry}>
              重新预览
            </Button>
          </InlineNotice>
        ) : null}

        {preview && !loading ? (
          <div className="mt-4 space-y-3">
            {empty && preview.readable_reporters === 0 && preview.unchanged === 0 ? (
              <InlineNotice>
                还没有设备上报 LLDP 邻居，所以没有可生成的候选链路。交换机等设备通过 SNMP 采集到 LLDP 邻居表后再来扫描；也可以先用“新增人工连线”手动连线。
              </InlineNotice>
            ) : empty ? (
              <InlineNotice>没有差异：当前候选已包含这次扫描的全部结果。</InlineNotice>
            ) : null}
            {preview.added.length ? (
              <section>
                <h3 className="mb-1 text-xs font-semibold text-muted-foreground">新增候选 {preview.added.length}</h3>
                <ul className="space-y-1">
                  {preview.added.map((r) => (
                    <li key={r.fingerprint} className="rounded-md bg-ok-bg px-2 py-1">
                      新增：{r.source_name} → {r.target_name}
                      <span className="ml-1 text-xs text-muted-foreground">{ports(r.source_port, r.target_port)}</span>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
            {preview.refreshed.length ? (
              <section>
                <h3 className="mb-1 text-xs font-semibold text-muted-foreground">证据更新 {preview.refreshed.length}</h3>
                <ul className="space-y-1">
                  {preview.refreshed.map((r) => (
                    <li key={r.link.id} className="rounded-md bg-info-bg px-2 py-1">
                      更新：{r.proposal.source_name} → {r.proposal.target_name}
                      <span className="block text-xs text-muted-foreground">
                        端口 {ports(r.link.source_port, r.link.target_port) || "（无）"} 改为{" "}
                        {ports(r.proposal.source_port, r.proposal.target_port) || "（无）"}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
            {preview.removed.length ? (
              <section>
                <h3 className="mb-1 text-xs font-semibold text-muted-foreground">
                  不再发现、将移除的候选 {preview.removed.length}
                </h3>
                <ul className="space-y-1">
                  {preview.removed.map((r) => (
                    <li key={r.id} className="rounded-md bg-bad-bg px-2 py-1">
                      移除：{r.source_name} → {r.target_name}
                      <span className="ml-1 text-xs text-muted-foreground">{ports(r.source_port, r.target_port)}</span>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
            <ul className="space-y-0.5 text-xs text-muted-foreground">
              <li>未变化的候选 {preview.unchanged} 条；读到邻居表的设备 {preview.readable_reporters} 台。</li>
              {preview.kept_unreadable ? (
                <li>{preview.kept_unreadable} 条候选的上报设备这次没有读到邻居表，保留不动。</li>
              ) : null}
              {preview.skipped_rejected ? <li>{preview.skipped_rejected} 条之前忽略过，不再提出。</li> : null}
              {preview.skipped_covered ? <li>{preview.skipped_covered} 条两端之间已有已确认或人工链路，不再提出。</li> : null}
            </ul>
            {preview.unmatched.length ? (
              <details className="rounded-md border px-2 py-1">
                <summary className="cursor-pointer text-xs font-medium">
                  {preview.unmatched.length} 个邻居没有对应到资产（不按 IP 匹配）
                </summary>
                <ul className="mt-1 space-y-0.5 text-xs text-muted-foreground">
                  {preview.unmatched.map((u, i) => (
                    <li key={`${u.reporter_asset_id}-${u.local_port}-${i}`}>
                      {u.reporter_name} 端口 {u.local_port || "?"}：{u.reason}
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </div>
        ) : null}

        <ModalFooter>
          <Dialog.Close asChild>
            <Button variant="outline">
              {empty ? "关闭" : "暂不合并"}
            </Button>
          </Dialog.Close>
          {preview && !empty && !loading ? (
            <Button onClick={onApply} disabled={applying}>
              {applying ? "正在合并…" : "合并到候选"}
            </Button>
          ) : null}
        </ModalFooter>
      </ModalContent>
    </Dialog.Root>
  )
}
