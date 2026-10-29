import { useState } from "react"
import { Dialog } from "radix-ui"
import { ChevronDown, ChevronRight, Power } from "lucide-react"

import { HelpPill } from "@/components/HelpPill"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { ModalContent, ModalFooter, ModalHeader } from "@/components/ui/dialog"
import { CheckField, LabeledField } from "@/components/ui/form"
import type { AssetCapability, ControlAction } from "@/lib/control"
import { usePoll } from "@/lib/monitor"
import type { Resource } from "@/lib/resources"
import { ConfirmDialog, OperationHistory } from "@/pages/operations/OperationsPage"

/** 卡片上直接给出的操作；重启与强制断电放进带名称的“更多操作” */
const QUICK = ["on", "soft", "shutdown"]
/** 会让宿主停机、可以先联动关闭其上虚拟机的操作 */
const HOST_DOWN = ["soft", "off", "reset"]

type Actions = AssetCapability["actions"]
interface GuestCapability { enabled: boolean; source: { platform: string; target: string; power: string } | null; actions: Actions }
interface HostedGuests { enabled: boolean; guests: { id: string; name: string; power: string; lifecycle: string }[] }

/**
 * 资源全景的电源快捷入口（p7-overview-actions；4.2 P17 增加虚拟机与宿主联动）。
 * 物理机：能力来自 /api/control/assets/{id}/power，提交 /api/control/power（IPMI）。
 * 虚拟机 / 容器：能力来自 /guest-power，提交 /api/control/guest-power（经 PVE / vSphere，不借用宿主 IPMI）。
 * 宿主关机类操作可勾选“先正常关闭其上的虚拟机”，全部确认关闭后才执行宿主操作。
 * 打开对话框只读取能力；只有在确认对话框里明确确认后才会提交。
 */
export function PowerEntry({ resource, editable }: { resource: Resource; editable: boolean }) {
  const [open, setOpen] = useState(false)
  return <>
    <Button size="xs" variant="outline" onClick={() => setOpen(true)} aria-label={`电源操作 ${resource.name}`}>
      <Power aria-hidden />电源操作
    </Button>
    {open ? <PowerDialog resource={resource} editable={editable} onClose={() => setOpen(false)} /> : null}
  </>
}

function PowerDialog({ resource, editable, onClose }: { resource: Resource; editable: boolean; onClose: () => void }) {
  const guest = resource.form === "vm" || resource.form === "container"
  const hostCap = usePoll<AssetCapability>(guest ? `/api/control/assets/${resource.id}/power?skip=1` : `/api/control/assets/${resource.id}/power`, 15000)
  const guestCap = usePoll<GuestCapability>(`/api/control/assets/${resource.id}/guest-power`, 15000)
  const hosted = usePoll<HostedGuests>(`/api/control/assets/${resource.id}/hosted-guests`, 30000)
  const [pending, setPending] = useState<ControlAction | null>(null)
  const [batch, setBatch] = useState<{ host: string; guests?: string | null } | null>(null)
  const [more, setMore] = useState(false)
  const [linked, setLinked] = useState(true)
  const [wait, setWait] = useState("300")
  const cap = guest ? guestCap : hostCap
  const actions: Actions = cap.data?.actions ?? []
  const guests = guest ? [] : hosted.data?.guests ?? []
  const canLink = !guest && guests.length > 0 && !!hosted.data?.enabled
  const waitValue = /^\d{2,4}$/.test(wait) ? Number(wait) : null

  const button = (a: Actions[number]) => <div key={a.key} className="flex flex-wrap items-center gap-2">
    <Button size="sm" variant={a.high_risk ? "destructive" : "default"} disabled={!editable || !a.allowed || pending !== null}
      onClick={() => setPending(a)}>{a.label}</Button>
    {!a.allowed && a.reason ? <span className="text-xs text-muted-foreground">{a.reason}</span> : null}
  </div>
  const address = guest
    ? guestCap.data ? guestCap.data.source ? `${guestCap.data.source.platform === "pve" ? "PVE" : "vSphere"} · ${guestCap.data.source.target}` : "没有找到发现它的虚拟化平台" : "读取中…"
    : hostCap.data ? hostCap.data.endpoint?.address ?? "没有可用于控制的 IPMI 端点" : "读取中…"
  const linking = canLink && linked && pending !== null && HOST_DOWN.includes(pending.key)

  return <Dialog.Root open onOpenChange={(o) => (!o ? onClose() : undefined)}>
    <ModalContent width={560}>
      <ModalHeader title={`电源操作：${resource.name}`}
        description={guest
          ? "虚拟机与容器通过发现它的 PVE / vSphere 平台控制，不使用宿主的 IPMI。指令经任务队列执行并写入审计；超时会标为“结果不确定”，不会自动重发。"
          : "请先核对对象。指令经任务队列执行并写入审计；超时会标为“结果不确定”，不会自动重发。"} />
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[0.8125rem]" aria-label="操作对象核对">
        <dt className="text-muted-foreground">名称</dt><dd className="break-words">{resource.name}</dd>
        <dt className="text-muted-foreground">UUID</dt><dd className="break-all font-mono text-xs">{resource.id}</dd>
        <dt className="text-muted-foreground">{guest ? "控制途径" : "控制地址"}</dt><dd className="break-words">{address}</dd>
      </dl>
      {guest && guestCap.data && !guestCap.data.enabled ? <InlineNotice className="mt-3">
        虚拟机电源控制默认关闭：部署设置 YUNSHU_GUEST_POWER=1 后才可用。<HelpPill topic="guest.power" />
      </InlineNotice> : null}
      {!guest && resource.children_count ? <InlineNotice tone="warn" className="mt-3">
        该资源已确认承载 {resource.children_count} 项资源。关机、重启或强制断电会影响它们；关系或运行状态可能不完整，请以实际情况为准。
      </InlineNotice> : null}
      {cap.error ? <InlineNotice tone="bad" role="alert" className="mt-3">{cap.error}</InlineNotice> : null}
      {!editable ? <p className="mt-3 text-xs text-muted-foreground">只读用户可以查看控制能力和原因，不能执行控制。</p> : null}
      {cap.data ? <div className="mt-3 space-y-2" role="group" aria-label="电源操作">
        {!guest && hostCap.data?.target?.state === "indeterminate" ? <p><Badge tone="bad">上次结果不确定</Badge> <span className="text-xs text-muted-foreground">已暂停控制，请先在运维操作中核对并解除。</span></p> : null}
        {actions.filter((a) => QUICK.includes(a.key)).map(button)}
        {actions.some((a) => !QUICK.includes(a.key)) ? <>
          <button type="button" className="flex items-center gap-1 text-xs font-medium" aria-expanded={more} onClick={() => setMore((v) => !v)}>
            {more ? <ChevronDown aria-hidden className="size-4" /> : <ChevronRight aria-hidden className="size-4" />}
            {guest ? "更多操作（重启、强制断电）" : "更多操作（硬件重启、强制断电）"}
          </button>
          {more ? <div className="space-y-2 border-l-2 pl-3">{actions.filter((a) => !QUICK.includes(a.key)).map(button)}</div> : null}
        </> : null}
      </div> : null}
      {batch ? <div className="mt-4 space-y-3" aria-label="本次操作结果">
        {batch.guests ? <div><p className="mb-2 text-xs font-medium">其上虚拟机（先执行）</p><OperationHistory path={`/api/control/operations?batch_id=${batch.guests}`} compact /></div> : null}
        <div><p className="mb-2 text-xs font-medium">本次操作</p><OperationHistory path={`/api/control/operations?batch_id=${batch.host}`} compact /></div>
      </div> : null}
      <ModalFooter>
        <a className="mr-auto self-center text-xs text-link underline" href="#/operations">打开运维操作查看全部记录</a>
        <Dialog.Close asChild><Button variant="outline">关闭</Button></Dialog.Close>
      </ModalFooter>
    </ModalContent>
    {pending ? <ConfirmDialog kind={guest ? "guest-power" : "power"} action={pending} assets={[{ id: resource.id, name: resource.name }]}
      extra={linking ? { guests_first: true, guest_timeout: waitValue ?? 300 } : guest ? { timeout: waitValue ?? 300 } : undefined}
      onClose={() => setPending(null)} onDone={(id, guestBatch) => { setBatch({ host: id, guests: guestBatch }); setPending(null) }}>
      {canLink && HOST_DOWN.includes(pending.key) ? <section aria-label="宿主联动" className="mt-3 space-y-2 rounded-md border p-3">
        <CheckField label={`先正常关闭其上 ${guests.length} 台虚拟机，全部确认关闭后再执行`} help="guest.linked" checked={linked} onChange={setLinked} />
        <ul className="max-h-28 list-disc overflow-y-auto pl-9 text-xs text-muted-foreground">
          {guests.map((g) => <li key={g.id}>{g.name}{g.power === "on" ? "（运行中）" : g.power === "off" ? "（已关机）" : ""}</li>)}
        </ul>
        {linked ? <LabeledField label="最长等待（秒）" help="guest.timeout" hint="30–1800；超时则取消宿主操作">
          <input data-help-wrapped="" inputMode="numeric" className="h-8 w-28 rounded-md border bg-card px-2 text-[0.8125rem]" value={wait} onChange={(e) => setWait(e.target.value.trim())} />
        </LabeledField> : null}
      </section> : guest && pending.key === "shutdown" ? <LabeledField className="mt-3" label="最长等待（秒）" help="guest.timeout" hint="30–1800">
        <input data-help-wrapped="" inputMode="numeric" className="h-8 w-28 rounded-md border bg-card px-2 text-[0.8125rem]" value={wait} onChange={(e) => setWait(e.target.value.trim())} />
      </LabeledField> : null}
    </ConfirmDialog> : null}
  </Dialog.Root>
}
