import { useState } from "react"
import { Dialog } from "radix-ui"

import { PageHeader } from "@/components/AppShell"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Card, InlineNotice, Segmented, panelClass } from "@/components/ui/controls"
import { ModalContent, ModalFooter, ModalHeader } from "@/components/ui/dialog"
import { LabeledField } from "@/components/ui/form"
import { ApiError, api, query, type Asset, type Credential, type Page } from "@/lib/api"
import { canEdit, seesAll, useAuth } from "@/lib/auth"
import {
  type AssetCapability,
  type ControlAction,
  type ControlOperation,
  type ControlStatus,
  type PushSubscription,
  BMC_SNMP_ACTIONS,
  SNMP_NEEDS_CREDENTIAL,
  OP_STATUS,
  THERMAL_ACTIONS,
  THERMAL_MODE,
  confirmPhrase,
  newRequestKey,
} from "@/lib/control"
import { dateText, usePoll } from "@/lib/monitor"
import { cn } from "@/lib/utils"
import { CutoverPanel } from "./CutoverPanel"
import { ThermalCommands } from "./ThermalCommands"
import { ThermalPolicies } from "./ThermalPolicies"

type Tab = "power" | "thermal" | "bmc-snmp" | "policies" | "commands" | "history" | "owner"
// guest-power：虚拟机 / 容器经虚拟化平台控制电源（4.2 P17）；bmc-snmp：经 Redfish 开启 BMC 的 SNMP（4.4 P19）
type Kind = "power" | "thermal" | "guest-power" | "bmc-snmp"

export function OperationsPage() {
  const [tab, setTab] = useState<Tab>("power")
  const { user } = useAuth()
  const editable = canEdit(user)
  const status = usePoll<ControlStatus>("/api/control/status", 10000)
  return (
    <div className="min-w-0 pb-8 [&_p]:break-words">
      <PageHeader
        title="运维操作"
        description="电源、风扇温控与开启 BMC 的 SNMP：先校验权限与能力，高风险操作需输入确认文字；每个操作只执行一次，超时标为“结果不确定”且不会自动重试。“指令已接受”和“已查询确认”分开显示。"
      />
      <div className="min-w-0 space-y-4 px-6">
        {status.error ? (
          <InlineNotice tone="bad" role="alert">
            {status.error}
          </InlineNotice>
        ) : null}
        {status.data?.owner === "legacy" ? (
          <InlineNotice tone="warn">
            <p className="font-medium">控制仍由旧控制器负责</p>
            <p className="mt-0.5">
              为避免新旧两套系统同时向同一台 BMC 发命令，完成切换前新平台不会向设备发送任何控制命令，下面的操作都会被拒绝。可以先查看每台设备的控制能力与原因。
            </p>
          </InlineNotice>
        ) : null}
        {status.data?.indeterminate_targets.length ? (
          <IndeterminateTargets status={status.data} editable={editable} changed={status.refresh} />
        ) : null}
        {!editable ? <InlineNotice>当前账户为只读，只能查看控制能力与操作记录。</InlineNotice> : null}
        <Segmented
          label="运维操作内容"
          value={tab}
          onChange={setTab}
          options={[
            { value: "power", label: "电源控制" },
            { value: "thermal", label: "风扇与温控" },
            { value: "bmc-snmp", label: "开启 SNMP" },
            { value: "policies", label: "温控策略" },
            { value: "commands", label: "风扇命令" },
            { value: "history", label: "操作记录" },
            { value: "owner", label: "控制归属" },
          ]}
        />
        {tab === "power" || tab === "thermal" || tab === "bmc-snmp" ? (
          <ControlPanel
            key={tab}
            kind={tab}
            status={status.data}
            editable={tab === "bmc-snmp" ? user?.role === "admin" : editable}
          />
        ) : tab === "policies" ? (
          <ThermalPolicies editable={editable} curveEditable={editable && seesAll(user)} legacy={status.data?.owner !== "new"} />
        ) : tab === "commands" ? (
          <ThermalCommands />
        ) : tab === "owner" ? (
          <CutoverPanel isAdmin={user?.role === "admin"} changed={status.refresh} />
        ) : (
          <OperationHistory path={`/api/control/operations${query({ limit: 100 })}`} />
        )}
      </div>
    </div>
  )
}

function IndeterminateTargets({ status, editable, changed }: { status: ControlStatus; editable: boolean; changed: () => void }) {
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [error, setError] = useState("")
  async function ack(key: string) {
    setError("")
    try {
      await api.post("/api/control/targets/acknowledge", { key, note: notes[key] ?? "" })
      changed()
    } catch (e) {
      setError((e as Error).message)
    }
  }
  return (
    <InlineNotice tone="bad" role="alert">
      <p className="font-medium">有设备的控制结果不确定，已暂停对它们的控制</p>
      <p className="mt-0.5">命令可能已送达也可能没有。请登录 BMC 或到现场核对实际状态，确认后填写核对结果并解除。系统不会自动重试。</p>
      <ul className="mt-2 space-y-2">
        {status.indeterminate_targets.map((t) => (
          <li key={t.key} className="space-y-1">
            <p>
              <span className="font-medium">{t.key.replace(/^bmc:/, "")}</span> · {dateText(t.updated_at)} · {t.message}
            </p>
            {editable ? (
              <div className="flex flex-wrap items-end gap-2">
                <LabeledField label="核对结果" help="ops.ack" className="flex-1">
                  <input
                    data-help-wrapped=""
                    className="h-8 min-w-0 rounded-md border bg-card px-2 text-[0.8125rem] text-foreground"
                    value={notes[t.key] ?? ""}
                    onChange={(e) => setNotes({ ...notes, [t.key]: e.target.value })}
                    placeholder="例如：已登录 BMC 确认电源已关闭"
                  />
                </LabeledField>
                <Button size="sm" variant="outline" disabled={!(notes[t.key] ?? "").trim()} onClick={() => void ack(t.key)}>
                  确认解除
                </Button>
              </div>
            ) : null}
          </li>
        ))}
      </ul>
      {error ? <p className="mt-2">{error}</p> : null}
    </InlineNotice>
  )
}

function ControlPanel({ kind, status, editable }: { kind: Kind; status: ControlStatus | null; editable: boolean }) {
  const [search, setSearch] = useState("")
  const [q, setQ] = useState("")
  const [selected, setSelected] = useState<string[]>([])
  const [focus, setFocus] = useState("")
  const [pending, setPending] = useState<ControlAction | null>(null)
  const actions =
    kind === "power" ? (status?.power_actions ?? []) : kind === "bmc-snmp" ? BMC_SNMP_ACTIONS : THERMAL_ACTIONS
  const [credentialId, setCredentialId] = useState("")
  // 旧控制器负责时所有控制都会被拒绝：不让用户走完确认流程再看到“已拒绝”
  const legacy = status?.owner !== "new"
  const [batch, setBatch] = useState("")
  const assets = usePoll<Page<Asset>>(`/api/assets${query({ q, limit: 100, role: "server" })}`, 30000)
  const max = status?.max_batch ?? 20
  const items = assets.data?.items ?? []
  const chosen = items.filter((a) => selected.includes(a.id))
  const toggle = (id: string) =>
    setSelected((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : cur.length >= max ? cur : [...cur, id]))
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <Card title="选择服务器">
        <form
          className="mb-2 flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            setQ(search)
          }}
        >
          <LabeledField label="搜索资产" plain className="flex-1">
            <input
              data-help-wrapped=""
              className="h-8 min-w-0 rounded-md border bg-card px-2 text-[0.8125rem] text-foreground"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="名称、编号或管理地址"
            />
          </LabeledField>
          <Button type="submit" size="sm" variant="outline">
            搜索
          </Button>
        </form>
        {assets.error ? (
          <InlineNotice tone="bad" role="alert">
            {assets.error}
          </InlineNotice>
        ) : null}
        {!assets.data && !assets.error ? <p className="text-muted-foreground">正在加载资产…</p> : null}
        {assets.data && !items.length ? <p className="text-muted-foreground">没有符合条件的服务器。</p> : null}
        <ul className="max-h-96 space-y-1 overflow-y-auto" aria-label="服务器列表">
          {items.map((a) => (
            <li key={a.id} className="flex items-center gap-2 rounded-md px-1 py-1 hover:bg-muted">
              <input
                type="checkbox"
                data-help-plain=""
                id={`pw-${a.id}`}
                checked={selected.includes(a.id)}
                onChange={() => toggle(a.id)}
                disabled={!selected.includes(a.id) && selected.length >= max}
              />
              <label htmlFor={`pw-${a.id}`} className="min-w-0 flex-1 truncate">
                {a.name}
              </label>
              <Button size="xs" variant="ghost" onClick={() => setFocus(a.id)}>
                查看能力
              </Button>
            </li>
          ))}
        </ul>
        <p className="mt-2 text-xs text-muted-foreground">
          已选 {selected.length} 台（一次最多 {max} 台；Worker 逐台执行，同一管理地址同一时间只执行一个操作）
        </p>
      </Card>
      <div className="min-w-0 space-y-4">
        <Card title={`对已选的 ${selected.length} 台执行`}>
          <div className="flex flex-wrap gap-2">
            {actions.map((a) => (
              <Button
                key={a.key}
                size="sm"
                variant={a.high_risk ? "destructive" : "outline"}
                disabled={
                  !editable ||
                  !selected.length ||
                  legacy ||
                  (kind === "bmc-snmp" && SNMP_NEEDS_CREDENTIAL.includes(a.key) && !credentialId)
                }
                onClick={() => setPending(a)}
              >
                {a.label}
              </Button>
            ))}
          </div>
          {kind === "bmc-snmp" ? <SnmpCredentialPicker value={credentialId} onChange={setCredentialId} /> : null}
          {legacy ? <p className="mt-2 text-xs text-muted-foreground">控制仍由旧控制器负责，操作按钮暂不可用。</p> : null}
          <p className="mt-2 text-xs text-muted-foreground">
            每台设备在执行前都会再检查一次：控制归属、资产状态、{kind === "bmc-snmp" ? "Redfish" : "IPMI"}
            端点与凭据、是否有未完成或结果不确定的操作。不满足的会被拒绝且不发送任何命令。
          </p>
          {kind === "bmc-snmp" ? (
            <p className="mt-1 text-xs text-muted-foreground">
              只有管理员可以执行。通过 Redfish 修改 BMC 设置：Dell iDRAC、HPE iLO 及按 Redfish 标准提供 SNMP
              设置的 BMC 可以自动开启；联想、新华三、浪潮等读不到设置的会被拒绝并提示手动开启。开启后用所选凭据读一次设备名称，读到才算“已查询确认”，并自动在资产上添加
              SNMP 监控端点。iLO 修改后要重启管理芯片才生效，平台不会自动重启。
            </p>
          ) : null}
          {kind === "bmc-snmp" ? (
            <p className="mt-1 text-xs text-muted-foreground">
              配置 Trap 目标、订阅 Redfish 事件后，BMC 出现硬件告警时会立即推送到“服务器告警”，不用等定时读取。
              两者都只增删平台自己的推送目标，不动 BMC 上别人的设置；需要部署时先打开平台这端的接收（查看能力里会说明）。
            </p>
          ) : null}
          {kind === "thermal" ? (
            <p className="mt-1 text-xs text-muted-foreground">
              执行时先读取设备的厂商、型号与 BMC 固件，再到“风扇命令”里匹配要用的命令；资产上填写的品牌型号不作为依据，匹配不到的设备会被拒绝。
              内置命令大多整理自厂商文档与社区资料，未经本项目实机验证，请先在非生产设备上试用。
              转速低于机型安全下限时直接拒绝，不会自动改成下限。
            </p>
          ) : null}
        </Card>
        {focus ? <Capability kind={kind} assetId={focus} /> : null}
        {batch ? (
          <Card title="本次操作结果">
            <OperationHistory path={`/api/control/operations${query({ batch_id: batch, limit: 50 })}`} compact />
          </Card>
        ) : null}
      </div>
      {pending ? (
        <ConfirmDialog
          kind={kind}
          action={pending}
          assets={chosen}
          extra={
            kind === "bmc-snmp" && SNMP_NEEDS_CREDENTIAL.includes(pending.key) ? { credential_id: credentialId } : undefined
          }
          onClose={() => setPending(null)}
          onDone={(id) => {
            setBatch(id)
            setPending(null)
          }}
        />
      ) : null}
    </div>
  )
}

/** 开启 SNMP 后平台用来采集的凭据（只列团体名与 SNMPv3 凭据） */
function SnmpCredentialPicker({ value, onChange }: { value: string; onChange: (id: string) => void }) {
  const credentials = usePoll<Credential[]>("/api/credentials", 60000)
  const items = (credentials.data ?? []).filter((c) => c.kind === "snmp_community" || c.kind === "snmpv3")
  return (
    <>
      <LabeledField label="SNMP 凭据（开启时用于采集，配置 Trap 目标时用其团体名）" help="ops.snmpCredential" className="mt-3">
        <select
          data-help-wrapped=""
          className="h-8 min-w-0 rounded-md border bg-card px-2 text-[0.8125rem] text-foreground"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">选择 SNMP 凭据</option>
          {items.map((c) => (
            <option key={c.id} value={c.id}>
              {c.alias}（{c.kind === "snmpv3" ? `SNMPv3 · ${c.username}` : "团体名 v2c"}）
            </option>
          ))}
        </select>
      </LabeledField>
      {credentials.data && !items.length ? (
        <p className="mt-1 text-xs text-muted-foreground">还没有 SNMP 凭据：请先到“设置 → 凭据”添加（推荐 SNMPv3）。</p>
      ) : null}
    </>
  )
}

/** BMC 主动推送：平台这端能否接收、这台 BMC 上平台配置过什么、最近什么时候收到（4.4 P20c） */
function PushStatus({ push }: { push: NonNullable<AssetCapability["push"]> }) {
  const line = (sub: PushSubscription | null) =>
    sub
      ? `已由 ${sub.created_by || "平台"} 于 ${dateText(sub.created_at)} 配置；` +
        (sub.last_event_at ? `最近收到 ${dateText(sub.last_event_at)}，共 ${sub.events_received} 次` : "还没收到过推送")
      : "这台 BMC 上还没有配置"
  return (
    <div className="mt-2 space-y-1 text-xs text-muted-foreground" aria-label="告警推送">
      <p>
        <span className="font-medium text-foreground">SNMP Trap：</span>
        {push.trap.enabled && push.trap.target
          ? `平台在 ${push.trap.target} 接收（BMC 发往 UDP 162，部署时映射到平台的 ${push.trap.port}）。`
          : "平台还没打开 Trap 接收（部署设置 YUNSHU_SNMP_TRAP=1 与 YUNSHU_SNMP_TRAP_TARGET）。"}
        {line(push.trap.subscription)}
      </p>
      <p>
        <span className="font-medium text-foreground">Redfish 事件：</span>
        {push.events.url
          ? `BMC 推送到 ${push.events.url}/api/bmc-events/…（地址带随机令牌）。`
          : "没有设置事件接收地址（部署设置 YUNSHU_BMC_EVENTS_URL，BMC 能访问到的平台 HTTPS 地址）。"}
        {line(push.events.subscription)}
      </p>
    </div>
  )
}

/** 单台资产的控制能力与不可执行原因（运维操作页与资源全景共用） */
export function Capability({ kind, assetId }: { kind: Kind; assetId: string }) {
  const info = usePoll<AssetCapability>(`/api/control/assets/${assetId}/${kind}`, 15000)
  if (info.error)
    return (
      <InlineNotice tone="bad" role="alert">
        {info.error}
      </InlineNotice>
    )
  if (!info.data) return <p className="text-muted-foreground">正在读取控制能力…</p>
  const d = info.data
  return (
    <Card
      title={`${d.asset_name} 的${kind === "power" ? "电源控制" : kind === "bmc-snmp" ? "SNMP 开启" : "风扇温控"}能力`}
    >
      <p className="text-xs text-muted-foreground">
        {d.endpoint
          ? `${kind === "bmc-snmp" ? "Redfish " : "控制"}端点：${d.endpoint.address}（${d.endpoint.purpose === "control" ? "控制" : "管理"}）`
          : `没有可用于控制的 ${kind === "bmc-snmp" ? "Redfish" : "IPMI"} 端点`}
        {d.target?.state === "indeterminate" ? " · 上次结果不确定，已暂停控制" : ""}
      </p>
      {kind === "bmc-snmp" ? (
        <p className="text-xs text-muted-foreground">
          {d.snmp_endpoints?.length
            ? `资产上已有 SNMP 端点：${d.snmp_endpoints.map((e) => e.address).join("、")}`
            : "资产上还没有 SNMP 端点"}
          {d.note ? ` · ${d.note}` : ""}
        </p>
      ) : null}
      {kind === "bmc-snmp" && d.push ? <PushStatus push={d.push} /> : null}
      {kind === "thermal" && d.target ? (
        <p className="text-xs text-muted-foreground">
          温控模式：{THERMAL_MODE[d.target.thermal_mode] ?? d.target.thermal_mode}
          {d.target.applied_pwm != null ? ` · ${d.target.applied_pwm}%` : ""}（“已下发”表示 BMC 接受了命令，不代表已确认生效）
        </p>
      ) : null}
      {/* 所有操作因同一原因不可执行时只说一次 */}
      {d.actions.length && d.actions.every((a) => !a.allowed && a.reason === d.actions[0].reason) ? (
        <p className="mt-2 text-[0.8125rem]">
          <Badge tone="neutral">全部不可执行</Badge> <span className="text-xs text-muted-foreground">{d.actions[0].reason}</span>
        </p>
      ) : (
        <ul className="mt-2 space-y-1">
          {d.actions.map((a) => (
            <li key={a.key} className="flex flex-wrap items-center gap-2">
              <span className="w-24 shrink-0">{a.label}</span>
              <Badge tone={a.allowed ? "ok" : "neutral"}>{a.allowed ? "可以执行" : "不可执行"}</Badge>
              {a.reason ? <span className="text-xs text-muted-foreground">{a.reason}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

/** 控制确认：高风险需输入名称，请求标识防重复提交（运维操作页与资源全景共用） */
export function ConfirmDialog({
  kind,
  action,
  assets,
  onClose,
  onDone,
  extra,
  children,
}: {
  kind: Kind
  action: ControlAction
  assets: { id: string; name: string }[]
  onClose: () => void
  onDone: (batchId: string, guestBatchId?: string | null) => void
  /** 附加到请求里的参数，如宿主联动的 guests_first */
  extra?: Record<string, unknown>
  /** 确认框里额外显示的内容（如联动选项） */
  children?: React.ReactNode
}) {
  // 打开对话框时生成一次：双击或网络重试提交同一个标识，后端只执行一次
  const [requestKey] = useState(newRequestKey)
  const [confirm, setConfirm] = useState("")
  const [reason, setReason] = useState("")
  const [pwm, setPwm] = useState("")
  const needsPwm = action.key === "set_speed_all"
  const pwmValue = /^\d{1,3}$/.test(pwm) ? Number(pwm) : null
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const phrase = confirmPhrase(assets.map((a) => a.name))
  const ready = (!action.high_risk || confirm.trim() === phrase) && (!needsPwm || (pwmValue !== null && pwmValue <= 100))
  async function submit() {
    if (busy || !ready) return
    setBusy(true)
    setError("")
    try {
      const res = await api.post<{ batch_id: string; guest_batch_id?: string | null }>(`/api/control/${kind}`, {
        asset_ids: assets.map((a) => a.id),
        action: action.key,
        ...(needsPwm ? { pwm: pwmValue } : {}),
        ...(extra ?? {}),
        confirm,
        reason,
        request_key: requestKey,
      })
      onDone(res.batch_id, res.guest_batch_id)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "提交失败")
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog.Root open onOpenChange={(o) => (!o ? onClose() : undefined)}>
      <ModalContent width={520}>
        <ModalHeader
          title={`${action.label}：${assets.length} 台`}
          description={
            <>
              {kind === "thermal"
                ? action.high_risk
                  ? "固定转速会让 BMC 停止按温度自动调速，转速过低可能导致过热。执行前会读取设备型号匹配风扇命令；超时标为“结果不确定”，不会自动重试。"
                  : "恢复由 BMC 按温度自动调节风扇。通过 IPMI 下发的无法回读控制模式，结果会显示为“已接受，无法确认”；通过 Redfish 的会再读一次确认。"
                : kind === "bmc-snmp"
                  ? action.high_risk
                    ? "关闭后，依赖这台 BMC 的 SNMP 的其他监控系统也会读不到数据。修改发出后超时会标为“结果不确定”，不会自动重试。"
                    : "通过 Redfish 修改 BMC 设置并写入所选凭据（团体名只追加、不覆盖已在用的）。先只读识别 BMC，不支持的机型直接拒绝，不做任何修改。"
                : kind === "guest-power"
                  ? "通过发现这台虚拟机的 PVE / vSphere 平台执行，不使用宿主的 IPMI。正常关机需要虚拟机里有对应的客户机工具，失败时不会自动改成强制断电；平台任务超时会标为“结果不确定”。"
                  : action.high_risk
                    ? "这是高风险操作，可能中断设备上运行的业务。指令发出后无法撤回；超时会标为“结果不确定”，不会自动重试。"
                    : "指令发出后会再查询一次电源状态确认结果。"}
            </>
          }
        />
        <ul className="mt-3 max-h-40 list-disc overflow-y-auto pl-5">
          {assets.map((a) => (
            <li key={a.id}>{a.name}</li>
          ))}
        </ul>
        {children}
        <LabeledField label="原因（写入审计）" help="ops.reason" className="mt-3">
          <input
            data-help-wrapped=""
            className="h-8 rounded-md border bg-card px-2 text-[0.8125rem] text-foreground"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={500}
          />
        </LabeledField>
        {needsPwm ? (
          <LabeledField label="目标转速 %" hint="0–100 的整数；低于机型安全下限会被拒绝，不会自动调整" help="ops.pwm" className="mt-3">
            <input
              data-help-wrapped=""
              className="h-8 rounded-md border bg-card px-2 text-[0.8125rem] text-foreground"
              inputMode="numeric"
              value={pwm}
              onChange={(e) => setPwm(e.target.value.trim())}
            />
          </LabeledField>
        ) : null}
        {action.high_risk ? (
          <LabeledField label={`请输入“${phrase}”确认`} help="ops.confirmPhrase" className="mt-3">
            <input
              data-help-wrapped=""
              className="h-8 rounded-md border bg-card px-2 text-[0.8125rem] text-foreground"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="off"
            />
          </LabeledField>
        ) : null}
        {error ? (
          <InlineNotice tone="bad" role="alert" className="mt-3">
            {error}
          </InlineNotice>
        ) : null}
        <ModalFooter>
          <Dialog.Close asChild>
            <Button variant="outline">
              取消
            </Button>
          </Dialog.Close>
          <Button variant={action.high_risk ? "destructive" : "default"} disabled={!ready || busy} onClick={() => void submit()}>
            {busy ? "正在提交…" : `确认${action.label}`}
          </Button>
        </ModalFooter>
      </ModalContent>
    </Dialog.Root>
  )
}

export function OperationHistory({ path, compact = false }: { path: string; compact?: boolean }) {
  const ops = usePoll<ControlOperation[]>(path, 3000)
  if (ops.error)
    return (
      <InlineNotice tone="bad" role="alert">
        {ops.error}
      </InlineNotice>
    )
  if (!ops.data) return <p className="text-muted-foreground">正在读取操作记录…</p>
  if (!ops.data.length) return <p className="text-muted-foreground">还没有控制操作记录。</p>
  return (
    <div className={cn(panelClass, "min-w-0 overflow-x-auto")}>
      <table className="data-table w-full min-w-[36rem]">
        <caption className="sr-only">控制操作记录</caption>
        <thead>
          <tr>
            <th>资产 / 操作</th>
            <th>结果</th>
            <th>说明</th>
            {!compact ? <th>请求人 / 时间</th> : null}
          </tr>
        </thead>
        <tbody>
          {ops.data.map((o) => {
            const st = OP_STATUS[o.status] ?? { label: o.status, tone: "neutral" as const }
            return (
              <tr key={o.id} className="border-t align-top">
                <td>
                  {o.asset_name}
                  <br />
                  <span className="text-muted-foreground">{o.action_label}</span>
                </td>
                <td>
                  <Badge tone={st.tone}>{st.label}</Badge>
                </td>
                <td className="max-w-80 break-words text-xs">{o.message || "—"}</td>
                {!compact ? (
                  <td className="text-xs">
                    {o.requested_by}
                    <br />
                    {dateText(o.created_at)}
                  </td>
                ) : null}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
