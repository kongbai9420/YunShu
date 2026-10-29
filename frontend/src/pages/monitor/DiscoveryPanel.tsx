import { useEffect, useState } from "react"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Card, InlineNotice, Select, panelClass } from "@/components/ui/controls"
import { TextField } from "@/components/ui/form"
import { api, type Credential, type Site } from "@/lib/api"
import { HelpPill } from "@/components/HelpPill"
import { dateText, STATE, usePoll } from "@/lib/monitor"
import { FORM_LABEL, PROTOCOL_LABEL, ROLE_LABEL, optionsFrom } from "@/lib/vocab"

interface Observation {
  id: string
  address: string
  ports: number[]
  facts: Record<string, string>
  status: string
  asset_id: string | null
  observed_at: string
  matches: { id: string; name: string; site_id: string | null }[]
}
interface Run {
  id: string
  created_at: string
  scope: string[]
  protocol: string
  ports: number[]
  state: string
  generation: number
  total: number
  processed: number
  responded: number
  error: string
  items?: Observation[]
}
const tokens = (s: string) => s.split(/[\s,，;；]+/).filter(Boolean)

export function DiscoveryPanel({ editable }: { editable: boolean }) {
  const [selected, setSelected] = useState("")
  const runs = usePoll<{ items: Run[]; total: number }>("/api/discovery/runs", 5000)
  useEffect(() => {
    if (!selected && runs.data?.items.length) setSelected(runs.data.items[0].id)
  }, [selected, runs.data])
  return (
    <div className="space-y-4 [&_button]:h-auto [&_button]:min-h-8 [&_button]:max-w-full [&_button]:whitespace-normal">
      <InlineNotice>
        只探测你明确指定的 IPv4 范围，每次最多 256 个地址。TCP 仅验证端口可达；SNMP
        使用选定的只读凭据。不响应不能直接判定设备离线，发现结果须逐项确认。
      </InlineNotice>
      {editable ? (
        <details className={cn(panelClass, "p-4")}>
          <summary className="cursor-pointer font-medium">新建发现任务</summary>
          <div className="mt-4">
            <NewRun
              onCreated={(run) => {
                setSelected(run.id)
                runs.refresh()
              }}
            />
          </div>
        </details>
      ) : (
        <p className="text-[0.8125rem] text-muted-foreground">
          只读账号可以查看任务与候选；开始发现和确认资产需要管理员或运维权限。
        </p>
      )}
      {runs.error ? (
        <InlineNotice role="alert" tone="bad">
          {runs.error}
        </InlineNotice>
      ) : null}
      {runs.data?.items.length ? (
        <Select
          id="discovery-run"
          label="发现记录（最近 20 项）"
          plain
          value={selected}
          onChange={setSelected}
          options={runs.data.items.map((r) => ({
            value: r.id,
            label: `${dateText(r.created_at)} · ${r.scope.join(", ")} · ${STATE[r.state] ?? r.state}`,
          }))}
          className="min-w-0 max-w-full [&_select]:max-w-full"
        />
      ) : (
        <p className="text-muted-foreground">尚无发现记录。填写范围并预览后，即可开始发现。</p>
      )}
      {selected ? <RunDetail key={selected} id={selected} editable={editable} onChanged={runs.refresh} /> : null}
    </div>
  )
}

function NewRun({ onCreated }: { onCreated: (run: Run) => void }) {
  const [scope, setScope] = useState("")
  const [excludes, setExcludes] = useState("")
  const [protocol, setProtocol] = useState("tcp")
  const [ports, setPorts] = useState("22,443,8006")
  const [credential, setCredential] = useState("")
  const [site, setSite] = useState("")
  const [concurrency, setConcurrency] = useState("8")
  const [timeout, setTimeoutValue] = useState("1")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<{ signature: string; total: number; addresses: string[] } | null>(null)
  const [started, setStarted] = useState(false)
  const credentials = usePoll<Credential[]>("/api/credentials", 60000)
  const sites = usePoll<Site[]>("/api/sites", 60000)
  const body = {
    scope: tokens(scope),
    excludes: tokens(excludes),
    protocol,
    ports: tokens(ports).map(Number),
    credential_id: protocol === "snmp" ? credential || null : null,
    site_id: site || null,
    concurrency: Number(concurrency),
    timeout: Number(timeout),
  }
  const signature = JSON.stringify(body)
  const current = preview?.signature === signature && !started
  async function submit(start: boolean) {
    setBusy(true)
    setError("")
    try {
      if (start) {
        if (!current) return
        const run = await api.post<Run>("/api/discovery/runs", body)
        setStarted(true)
        setPreview(null)
        onCreated(run)
      } else {
        const result = await api.post<{ total: number; addresses: string[] }>("/api/discovery/preview", body)
        setStarted(false)
        setPreview({ ...result, signature })
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault()
        void submit(false)
      }}
    >
      <div className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <TextField
          id="discovery-scope"
          label="授权范围"
          help="discovery.scope"
          value={scope}
          onChange={setScope}
          required
          placeholder="192.0.2.0/28 或 192.0.2.10"
          hint="IPv4 地址或 CIDR，多个用逗号或空格分隔"
        />
        <TextField
          id="discovery-excludes"
          label="排除地址 / 网段"
          help="discovery.exclude"
          value={excludes}
          onChange={setExcludes}
          placeholder="192.0.2.1,192.0.2.8/30"
        />
        <Select
          id="discovery-protocol"
          label="只读探测方式"
          help="discovery.method"
          value={protocol}
          onChange={(v) => {
            setProtocol(v)
            setPorts(v === "snmp" ? "161" : "22,443,8006")
          }}
          options={[
            { value: "tcp", label: "TCP 端口可达性（不发送凭据）" },
            { value: "snmp", label: "SNMP v2c / v3 系统身份" },
          ]}
        />
        <TextField
          id="discovery-ports"
          label={protocol === "snmp" ? "UDP 端口" : "TCP 端口"}
          value={ports}
          onChange={setPorts}
          required
          hint={protocol === "snmp" ? "一个端口，默认 161" : "最多 4 个端口，用逗号分隔；不扫描其他端口"}
        />
        {protocol === "snmp" ? (
          <div>
            <Select
              id="discovery-credential"
              hint="只用来读取系统信息，不会修改设备"
              label="SNMP 只读凭据"
              value={credential}
              onChange={setCredential}
              options={[
                { value: "", label: "请选择凭据" },
                ...(credentials.data ?? [])
                  .filter((c) => ["snmp_community", "snmpv3"].includes(c.kind))
                  .map((c) => ({ value: c.id, label: c.alias })),
              ]}
            />
            <p className="mt-1 text-xs text-muted-foreground">没有可选项时先在平台设置添加 SNMP 凭据。</p>
          </div>
        ) : null}
        <Select
          id="discovery-site"
          label="新资产所属场所"
          hint="从发现结果新建的资产放到这个场所"
          value={site}
          onChange={setSite}
          options={[{ value: "", label: "未指定" }, ...(sites.data ?? []).map((s) => ({ value: s.id, label: s.name }))]}
        />
        <TextField
          id="discovery-concurrency"
          label="并发上限"
          help="discovery.concurrency"
          type="number"
          min={1}
          max={16}
          value={concurrency}
          onChange={setConcurrency}
          required
        />
        <TextField
          id="discovery-timeout"
          label="每次连接超时（秒）"
          help="discovery.timeout"
          type="number"
          min={0.2}
          max={2}
          step={0.1}
          value={timeout}
          onChange={setTimeoutValue}
          required
        />
      </div>
      {error || credentials.error || sites.error ? (
        <InlineNotice role="alert" tone="bad">
          {error || credentials.error || sites.error}
        </InlineNotice>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" variant="outline" disabled={busy || !scope.trim()}>
          预览范围（不联网）
        </Button>
        <HelpPill topic="discovery.preview" className="self-center" />
        {current ? (
          <Button type="button" size="sm" disabled={busy} onClick={() => void submit(true)}>
            确认授权并开始发现
          </Button>
        ) : null}
      </div>
      {current && preview ? (
        <InlineNotice>
          <p>
            将探测 {preview.total} 个地址，方式：{protocol === "snmp" ? "SNMP 只读查询" : "TCP 连接"}，端口：{ports}
            。仅在点击“确认授权并开始发现”后联网。
          </p>
          <details className="mt-1">
            <summary className="cursor-pointer text-link">核对完整地址列表</summary>
            <p className="mt-1 break-all">{preview.addresses.join("、")}</p>
          </details>
        </InlineNotice>
      ) : preview ? (
        <p className="text-xs text-muted-foreground">范围或配置已变化，请重新预览。</p>
      ) : null}
    </form>
  )
}

function RunDetail({ id, editable, onChanged }: { id: string; editable: boolean; onChanged: () => void }) {
  const run = usePoll<Run>(`/api/discovery/runs/${id}`, 3000)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  async function action(value: string) {
    if (!run.data) return
    setBusy(true)
    setError("")
    try {
      await api.post(`/api/discovery/runs/${id}/state`, { action: value, generation: run.data.generation })
      run.refresh()
      onChanged()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  if (!run.data)
    return <InlineNotice tone={run.error ? "bad" : "info"}>{run.error ?? "正在加载发现结果…"}</InlineNotice>
  const r = run.data
  return (
    <div className="space-y-3">
      <Card
        title="发现进度"
        actions={
          <Badge tone={r.state === "completed" ? "ok" : r.state === "failed" ? "bad" : "info"}>
            {STATE[r.state] ?? r.state}
          </Badge>
        }
      >
        <div className="space-y-2 text-[0.8125rem]">
          <p aria-live="polite">
            已检查 {r.processed} / {r.total} 个地址 · 有响应 {r.responded} 个 · 未响应 {r.processed - r.responded} 个
          </p>
          <progress
            aria-label="发现进度"
            value={r.processed}
            max={r.total}
            className="h-2 w-full accent-[var(--primary)]"
          />
          <p className="text-xs text-muted-foreground">
            暂停和取消后，已开始的连接会在超时内结束；未处理的地址不会继续探测。继续任务会重试尚未保存的批次。
          </p>
          {r.error ? <InlineNotice tone="warn">{r.error}</InlineNotice> : null}
          {editable ? (
            <div className="flex flex-wrap gap-2">
              {["queued", "running"].includes(r.state) ? (
                <Button size="sm" variant="outline" disabled={busy} onClick={() => void action("pause")}>
                  暂停发现
                </Button>
              ) : null}
              {["paused", "failed"].includes(r.state) ? (
                <Button size="sm" variant="outline" disabled={busy} onClick={() => void action("resume")}>
                  继续发现
                </Button>
              ) : null}
              {["queued", "running", "paused", "failed"].includes(r.state) ? (
                <Button size="sm" variant="outline" disabled={busy} onClick={() => void action("cancel")}>
                  取消发现
                </Button>
              ) : null}
              <Button size="sm" variant="ghost" onClick={run.refresh}>
                刷新发现结果
              </Button>
            </div>
          ) : null}
        </div>
      </Card>
      {error || run.error ? (
        <InlineNotice tone="bad" role="alert">
          {error || run.error}
        </InlineNotice>
      ) : null}
      <h3 className="text-sm font-semibold">待确认设备与处理记录</h3>
      {(r.items ?? []).length === 0 ? (
        <InlineNotice>
          {r.state === "completed"
            ? "没有发现有响应的设备；可核对范围、端口、SNMP 凭据及防火墙。"
            : "暂时没有发现结果。发现结果不会自动写入资产。"}
        </InlineNotice>
      ) : null}
      <div className="space-y-3">
        {r.items?.map((o) => (
          <Candidate key={o.id} observation={o} protocol={r.protocol} editable={editable} onHandled={run.refresh} />
        ))}
      </div>
    </div>
  )
}

function Candidate({
  observation: o,
  protocol,
  editable,
  onHandled,
}: {
  observation: Observation
  protocol: string
  editable: boolean
  onHandled: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [name, setName] = useState(o.facts.sys_name || `发现设备 ${o.address}`)
  const [assetNo, setAssetNo] = useState("")
  const [form, setForm] = useState("physical")
  const [role, setRole] = useState("other")
  const [platform, setPlatform] = useState("")
  const [endpointProtocol, setEndpointProtocol] = useState(protocol === "snmp" ? "snmp" : "other")
  const [port, setPort] = useState(String(o.ports[0]))
  const [existing, setExisting] = useState(o.matches[0]?.id ?? "")
  const [duplicate, setDuplicate] = useState(false)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  async function confirm(action: string) {
    setBusy(true)
    setError("")
    try {
      await api.post(`/api/discovery/observations/${o.id}/confirm`, {
        action,
        name,
        asset_no: assetNo || null,
        form,
        role,
        platform,
        endpoint_protocol: endpointProtocol,
        port: Number(port),
        asset_id: existing || null,
        allow_duplicate: duplicate,
      })
      onHandled()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Card
      title={o.address}
      actions={
        <Badge tone={o.status === "candidate" ? "warn" : "neutral"}>
          {o.status === "candidate" ? "待确认" : o.status === "ignored" ? "已忽略" : "已确认"}
        </Badge>
      }
    >
      <div className="space-y-3 text-[0.8125rem]">
        <p>
          {protocol === "snmp" ? "SNMP 身份响应" : "开放的 TCP 端口"}：{o.ports.join("、")} · {dateText(o.observed_at)}
        </p>
        {o.facts.sys_descr ? <p className="break-words text-muted-foreground">{o.facts.sys_descr}</p> : null}
        {o.matches.length && o.status === "candidate" ? (
          <InlineNotice tone="warn">
            已有 {o.matches.length} 台资产使用此地址，可能是同一设备，也可能属于不同场所。请人工确认，不会自动合并。
          </InlineNotice>
        ) : null}
        {editable && o.status === "candidate" ? (
          <>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="outline" onClick={() => setExpanded(!expanded)}>
                {expanded ? "收起确认表单" : "确认或关联资产"}
              </Button>
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => void confirm("ignore")}>
                忽略此候选
              </Button>
            </div>
            {expanded ? (
              <div className="space-y-3 rounded-lg border p-3">
                {o.matches.length ? (
                  <div className="space-y-2">
                    <Select
                      id={`${o.id}-existing`}
                      label="关联到已有资产（仅关联观察记录）"
          help="discovery.link"
                      value={existing}
                      onChange={setExisting}
                      options={o.matches.map((m) => ({ value: m.id, label: m.name }))}
                    />
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy || !existing}
                      onClick={() => void confirm("link")}
                    >
                      确认为已有资产
                    </Button>
                    <p className="text-xs text-muted-foreground">已有资产的字段、端点和人工关系保持原样。</p>
                  </div>
                ) : null}
                <form
                  className="space-y-3"
                  onSubmit={(e) => {
                    e.preventDefault()
                    void confirm("create")
                  }}
                >
                  <p className="font-medium">新建独立资产</p>
                  <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                    <TextField
                      id={`${o.id}-name`}
                      plain
                      label="设备名称"
                      value={name}
                      onChange={setName}
                      required
                      maxLength={200}
                    />
                    <TextField
                      id={`${o.id}-no`}
                      help="asset.assetNo"
                      label="资产编号"
                      value={assetNo}
                      onChange={setAssetNo}
                      maxLength={100}
                    />
                    <Select
                      id={`${o.id}-form`}
                      help="asset.form"
                      label="形态"
                      value={form}
                      onChange={setForm}
                      options={optionsFrom(FORM_LABEL)}
                    />
                    <Select
                      id={`${o.id}-role`}
                      help="asset.role"
                      label="角色"
                      value={role}
                      onChange={setRole}
                      options={optionsFrom(ROLE_LABEL)}
                    />
                    <TextField
                      id={`${o.id}-platform`}
                      hint="用于自动选择采集模板，如 debian、dell-idrac"
                      label="平台（可留空）"
                      value={platform}
                      onChange={setPlatform}
                      maxLength={64}
                    />
                    <Select
                      id={`${o.id}-port`}
                      help="discovery.confirm"
                      label="确认管理端口"
                      value={port}
                      onChange={setPort}
                      options={o.ports.map((p) => ({ value: String(p), label: String(p) }))}
                    />
                    <Select
                      id={`${o.id}-protocol`}
                      help="conn.protocol"
                      label="确认管理协议"
                      value={endpointProtocol}
                      onChange={setEndpointProtocol}
                      options={optionsFrom(PROTOCOL_LABEL).filter((p) =>
                        protocol === "snmp"
                          ? p.value === "snmp"
                          : ["other", "ssh", "http", "https", "redfish", "pve-api", "esxi-api"].includes(p.value),
                      )}
                    />
                  </div>
                  <p className="text-xs text-muted-foreground">
                    端口号不证明服务类型，请按实际设备确认。保留“其他”时只监测端口可达性；其他协议的凭据可创建后在资产端点中关联。
                  </p>
                  {o.matches.length ? (
                    <div className="flex items-start gap-1">
                    <label className="flex items-start gap-2">
                      <input
                        type="checkbox"
                        data-help-wrapped=""
                        className="mt-1"
                        checked={duplicate}
                        onChange={(e) => setDuplicate(e.target.checked)}
                      />
                      我确认同 IP 应新建独立资产
                    </label>
                    <HelpPill topic="discovery.duplicate" />
                    </div>
                  ) : null}
                  <Button
                    type="submit"
                    size="sm"
                    disabled={busy || !name.trim() || (o.matches.length > 0 && !duplicate)}
                  >
                    确认新建资产
                  </Button>
                </form>
              </div>
            ) : null}
          </>
        ) : o.status === "confirmed" ? (
          <p className="text-xs text-muted-foreground">
            {o.asset_id ? "已关联资产，可前往资产管理查看。" : "原关联资产已删除；保留本次观察记录。"}
          </p>
        ) : null}
        {error ? (
          <InlineNotice role="alert" tone="bad">
            {error}
          </InlineNotice>
        ) : null}
      </div>
    </Card>
  )
}
