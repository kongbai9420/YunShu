import { useCallback, useEffect, useMemo, useState } from "react"
import { Dialog, Tabs } from "radix-ui"
import { KeyRound, Plus, ShieldAlert, ShieldCheck, Trash2, X } from "lucide-react"

import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice, Select } from "@/components/ui/controls"
import { ModalOverlay } from "@/components/ui/dialog"
import { CheckField, TextField } from "@/components/ui/form"
import {
  ApiError,
  api,
  query,
  type Asset,
  type AssetDetail,
  type Credential,
  type Endpoint,
  type Page,
  type Site,
} from "@/lib/api"
import {
  DEFAULT_PORT,
  FORM_LABEL,
  LIFECYCLE_LABEL,
  PROTOCOL_LABEL,
  PURPOSE_LABEL,
  RELATION_LABEL,
  ROLE_LABEL,
  labelOf,
  optionsFrom,
} from "@/lib/vocab"
import { AssetFields, draftOf, payloadOf, type AssetDraft } from "./AssetFields"
import { AssetMonitor } from "@/pages/monitor/AssetMonitor"
import { dateText, utcDate } from "@/lib/monitor"
import { useAuth } from "@/lib/auth"
import { AssetAlerts } from "@/pages/alerts/AssetAlerts"
import { InformationPreview } from "./InformationPreview"
import { SoftwarePanel } from "./SoftwarePanel"
import { AssetVulns } from "./AssetVulns"

/** 有证书或主机密钥校验的协议；SNMP、IPMI 等不适用，不显示该标记 */
const TLS_PROTOCOLS = new Set(["ssh", "https", "redfish", "pve-api", "esxi-api", "winrm"])

export function AssetDetailDrawer({
  assetId,
  sites,
  editable,
  onClose,
  onChanged,
}: {
  assetId: string | null
  sites: Site[]
  editable: boolean
  onClose: () => void
  onChanged: () => void
}) {
  const [dirty, setDirty] = useState(false)
  const [confirming, setConfirming] = useState(false)

  useEffect(() => {
    setDirty(false)
    setConfirming(false)
  }, [assetId])

  // 编辑页有未保存修改时，关闭前先确认
  const requestClose = () => (dirty ? setConfirming(true) : onClose())

  return (
    <Dialog.Root open={assetId !== null} onOpenChange={(o) => (o ? null : requestClose())}>
      <Dialog.Portal>
        <ModalOverlay />
        <Dialog.Content
          aria-describedby={undefined}
          className="fixed inset-y-0 right-0 z-50 flex w-full max-w-[640px] flex-col border-l border-input bg-popover text-popover-foreground shadow-modal outline-none data-[state=open]:animate-in data-[state=open]:slide-in-from-right-8 data-[state=open]:fade-in-0"
        >
          {confirming ? (
            <div
              role="alert"
              className="flex flex-wrap items-center gap-2 border-b bg-warn-bg px-5 py-3 text-[0.8125rem]"
            >
              <span className="flex-1">编辑页有未保存的修改，关闭后这些修改会丢失。</span>
              <Button size="xs" variant="outline" onClick={() => setConfirming(false)} autoFocus>
                继续编辑
              </Button>
              <Button
                size="xs"
                variant="destructive"
                onClick={() => {
                  setConfirming(false)
                  setDirty(false)
                  onClose()
                }}
              >
                放弃修改并关闭
              </Button>
            </div>
          ) : null}
          {assetId ? (
            <DrawerBody
              key={assetId}
              assetId={assetId}
              sites={sites}
              editable={editable}
              onDirtyChange={setDirty}
              onChanged={onChanged}
              onDeleted={() => {
                setDirty(false)
                onChanged()
                onClose()
              }}
            />
          ) : (
            <Dialog.Title className="sr-only">资产详情</Dialog.Title>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}

function DrawerBody({
  assetId,
  sites,
  editable,
  onDirtyChange,
  onChanged,
  onDeleted,
}: {
  assetId: string
  sites: Site[]
  editable: boolean
  onDirtyChange: (d: boolean) => void
  onChanged: () => void
  onDeleted: () => void
}) {
  const [asset, setAsset] = useState<AssetDetail | null>(null)
  const isAdmin = useAuth().user?.role === "admin"
  const [error, setError] = useState<string | null>(null)
  const [credentials, setCredentials] = useState<Credential[]>([])
  const [names, setNames] = useState<Record<string, string>>({})

  const load = useCallback(async () => {
    try {
      const a = await api.get<AssetDetail>(`/api/assets/${assetId}`)
      setAsset(a)
      setError(null)
      const others = Array.from(new Set(a.relations.flatMap((r) => [r.source_asset_id, r.target_asset_id]))).filter(
        (id) => id !== a.id,
      )
      const pairs = await Promise.all(
        others.map((id) =>
          api
            .get<Asset>(`/api/assets/${id}`)
            .then((o) => [id, o.name] as const)
            .catch(() => [id, "（已删除）"] as const),
        ),
      )
      setNames(Object.fromEntries(pairs))
    } catch (e) {
      setError((e as Error).message)
    }
  }, [assetId])

  useEffect(() => {
    void load()
    api
      .get<Credential[]>("/api/credentials")
      .then(setCredentials)
      .catch(() => setCredentials([]))
  }, [load])

  const afterChange = useCallback(async () => {
    await load()
    onChanged()
  }, [load, onChanged])

  if (error && !asset)
    return (
      <div className="p-5">
        <Dialog.Title className="mb-3 text-lg font-semibold">资产详情</Dialog.Title>
        <InlineNotice tone="bad" role="alert">
          {error}
        </InlineNotice>
      </div>
    )
  if (!asset)
    return (
      <div className="p-5 text-[0.8125rem] text-muted-foreground">
        <Dialog.Title className="sr-only">资产详情</Dialog.Title>
        正在加载…
      </div>
    )

  const siteName = sites.find((s) => s.id === asset.site_id)?.name
  const tabs = [
    { value: "overview", label: "概览" },
    { value: "monitor", label: "指标与采集" },
    { value: "alerts", label: "告警" },
    { value: "endpoints", label: `管理端点 ${asset.endpoints.length}` },
    { value: "relations", label: `关联 ${asset.relations.length}` },
    { value: "software", label: "软件与版本" },
    { value: "vulns", label: "漏洞" },
    ...(editable ? [{ value: "edit", label: "编辑" }] : []),
  ]

  return (
    <>
      <header className="flex items-start justify-between gap-3 border-b px-5 pt-5 pb-4">
        <div className="min-w-0">
          <Dialog.Title className="truncate text-lg font-semibold">{asset.name}</Dialog.Title>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs">
            <Badge tone="neutral">形态：{labelOf(FORM_LABEL, asset.form)}</Badge>
            <Badge tone="neutral">角色：{labelOf(ROLE_LABEL, asset.role)}</Badge>
            <Badge tone="neutral">平台：{asset.platform || "未填写"}</Badge>
            <Badge tone={asset.lifecycle === "active" ? "ok" : "info"}>
              {labelOf(LIFECYCLE_LABEL, asset.lifecycle)}
            </Badge>
            {asset.lifecycle === "maintenance" && asset.maintenance_until ? (
              // 维护超期时用警告色
              <Badge tone={utcDate(asset.maintenance_until).getTime() <= Date.now() ? "warn" : "neutral"}>
                {utcDate(asset.maintenance_until).getTime() <= Date.now() ? "维护已超期：" : "预计结束："}
                {dateText(asset.maintenance_until)}
              </Badge>
            ) : null}
          </div>
        </div>
        <Dialog.Close asChild>
          <Button variant="ghost" size="icon-sm" aria-label="关闭详情">
            <X aria-hidden />
          </Button>
        </Dialog.Close>
      </header>
      <Tabs.Root defaultValue="overview" className="flex min-h-0 flex-1 flex-col">
        <Tabs.List aria-label="资产详情" className="flex gap-1 overflow-x-auto border-b px-4">
          {tabs.map((t) => (
            <Tabs.Trigger
              key={t.value}
              value={t.value}
              className="-mb-px shrink-0 border-b-2 border-transparent px-2.5 py-2 text-[0.8125rem] font-medium text-muted-foreground hover:text-foreground data-[state=active]:border-primary data-[state=active]:text-foreground"
            >
              {t.label}
            </Tabs.Trigger>
          ))}
        </Tabs.List>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <Tabs.Content value="overview">
            <dl className="grid grid-cols-[7rem_1fr] gap-x-3 gap-y-2 text-[0.8125rem]">
              {(
                [
                  ["资产编号", asset.asset_no ?? "无"],
                  ["品牌", asset.vendor],
                  ["型号", asset.model],
                  ["序列号", asset.serial_no],
                  ["场所", siteName ?? (asset.site_id ? "—" : "")],
                  ["标签", asset.tags.join("、")],
                  ["采集模板", asset.collection_profile],
                  ["备注", asset.notes],
                  ["来源", asset.legacy_source ? `旧版导入（旧 ID ${asset.legacy_id}）` : "新平台创建"],
                  ["修订号", String(asset.revision)],
                  [
                    "更新时间",
                    dateText(asset.updated_at), // PostgreSQL 返回带时区的时间，不能一律补 Z（4.1 P12 走查发现）
                  ],
                ] as const
              ).map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-muted-foreground">{k}</dt>
                  <dd className="break-words whitespace-pre-wrap">
                    {v || <span className="text-muted-foreground">未填写</span>}
                  </dd>
                </div>
              ))}
            </dl>
          </Tabs.Content>

          <Tabs.Content value="monitor">
            <AssetMonitor asset={asset} editable={editable} />
          </Tabs.Content>

          <Tabs.Content value="alerts">
            <AssetAlerts assetId={asset.id} />
          </Tabs.Content>

          <Tabs.Content value="endpoints">
            <EndpointsTab asset={asset} credentials={credentials} editable={editable} onChanged={afterChange} />
          </Tabs.Content>

          <Tabs.Content value="vulns">
            <AssetVulns assetId={asset.id} editable={isAdmin} />
          </Tabs.Content>

          <Tabs.Content value="software">
            <SoftwarePanel assetId={asset.id} editable={editable} />
          </Tabs.Content>

          <Tabs.Content value="relations">
            <RelationsTab asset={asset} names={names} editable={editable} onChanged={afterChange} />
          </Tabs.Content>

          {editable ? (
            <Tabs.Content value="edit">
              <EditTab
                asset={asset}
                sites={sites}
                onDirtyChange={onDirtyChange}
                onSaved={afterChange}
                onReload={load}
                onDeleted={onDeleted}
              />
            </Tabs.Content>
          ) : null}
        </div>
      </Tabs.Root>
    </>
  )
}

function EndpointsTab({
  asset,
  credentials,
  editable,
  onChanged,
}: {
  asset: AssetDetail
  credentials: Credential[]
  editable: boolean
  onChanged: () => Promise<void>
}) {
  const [adding, setAdding] = useState(false)
  const [address, setAddress] = useState("")
  const [protocol, setProtocol] = useState("ipmi")
  const [port, setPort] = useState("")
  const [purpose, setPurpose] = useState("management")
  const [credentialId, setCredentialId] = useState("")
  const [error, setError] = useState<string | null>(null)
  const credName = useMemo(() => new Map(credentials.map((c) => [c.id, c.alias])), [credentials])

  async function add(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    try {
      await api.post(`/api/assets/${asset.id}/endpoints`, {
        address: address.trim(),
        protocol,
        purpose,
        port: port ? Number(port) : (DEFAULT_PORT[protocol] ?? null),
        credential_id: credentialId || null,
      })
      setAdding(false)
      setAddress("")
      setPort("")
      setCredentialId("")
      await onChanged()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  async function remove(id: string) {
    setError(null)
    try {
      await api.del(`/api/endpoints/${id}`)
      await onChanged()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  async function togglePackages(id: string, value: boolean) {
    setError("")
    try {
      await api.patch(`/api/endpoints/${id}`, { collect_packages: value })
      await onChanged()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        一台资产可以有多个管理端点（如 iDRAC 的 IPMI 与 Redfish、系统的 SSH）。界面只显示凭据别名，不显示密码。
      </p>
      {asset.endpoints.length === 0 ? <p className="text-[0.8125rem] text-muted-foreground">还没有管理端点。</p> : null}
      <ul className="space-y-2">
        {asset.endpoints.map((ep) => (
          <li key={ep.id} className="flex items-start justify-between gap-3 rounded-lg border p-3 text-[0.8125rem]">
            <div className="min-w-0">
              <div className="font-medium">
                {labelOf(PROTOCOL_LABEL, ep.protocol)} ·{" "}
                <span className="tabular">
                  {ep.address}
                  {ep.port ? `:${ep.port}` : ""}
                </span>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs">
                <Badge tone="neutral">用途：{labelOf(PURPOSE_LABEL, ep.purpose)}</Badge>
                {ep.credential_id ? (
                  <Badge tone="info" icon={KeyRound}>
                    凭据：{credName.get(ep.credential_id) ?? "已引用"}
                  </Badge>
                ) : (
                  <Badge tone="warn" icon={KeyRound}>
                    未关联凭据
                  </Badge>
                )}
                {!TLS_PROTOCOLS.has(ep.protocol) ? null : ep.tls_verify ? (
                  <Badge tone="ok" icon={ShieldCheck}>
                    校验证书 / 主机密钥
                  </Badge>
                ) : (
                  <Badge tone="warn" icon={ShieldAlert}>
                    不校验
                  </Badge>
                )}
              </div>
              {editable ? <EndpointCredential endpoint={ep} credentials={credentials} onChanged={onChanged} /> : null}
              {editable && ep.protocol === "ssh" && ep.purpose !== "control" ? (
                <CheckField
                  className="mt-2"
                  label="每天读取软件包清单"
                  help="asset.endpoint.packages"
                  checked={ep.collect_packages}
                  onChange={(v) => void togglePackages(ep.id, v)}
                />
              ) : null}
            </div>
            {editable ? (
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={`删除端点 ${ep.address}`}
                onClick={() => void remove(ep.id)}
              >
                <Trash2 aria-hidden />
              </Button>
            ) : null}
          </li>
        ))}
      </ul>
      {error ? (
        <InlineNotice tone="bad" role="alert">
          {error}
        </InlineNotice>
      ) : null}
      {editable && !adding ? (
        <Button variant="outline" size="sm" onClick={() => setAdding(true)}>
          <Plus aria-hidden />
          添加管理端点
        </Button>
      ) : null}
      {editable && adding ? (
        <form onSubmit={add} className="grid grid-cols-1 gap-3 rounded-lg border p-3 sm:grid-cols-2">
          <TextField id="ep-addr" label="地址" hint="只填 IP 或主机名" value={address} onChange={setAddress} required autoFocus />
          <Select
            id="ep-proto"
            help="conn.protocol"
            label="协议"
            value={protocol}
            onChange={setProtocol}
            options={optionsFrom(PROTOCOL_LABEL)}
          />
          <TextField
            id="ep-port"
            label="端口"
            type="number"
            value={port}
            onChange={setPort}
            min={1}
            max={65535}
            hint={DEFAULT_PORT[protocol] ? `留空用默认 ${DEFAULT_PORT[protocol]}` : undefined}
          />
          <Select
            id="ep-purpose"
            label="用途"
            help="asset.endpoint.purpose"
            value={purpose}
            onChange={setPurpose}
            options={optionsFrom(PURPOSE_LABEL)}
          />
          <Select
            id="ep-cred"
            label="凭据"
            help="conn.credential"
            value={credentialId}
            onChange={setCredentialId}
            options={[{ value: "", label: "不关联" }, ...credentials.map((c) => ({ value: c.id, label: c.alias }))]}
            className="sm:col-span-2"
          />
          <div className="flex gap-2 sm:col-span-2">
            <Button type="submit" size="sm" disabled={!address.trim()}>
              保存
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={() => setAdding(false)}>
              取消
            </Button>
          </div>
        </form>
      ) : null}
    </div>
  )
}

function EndpointCredential({
  endpoint,
  credentials,
  onChanged,
}: {
  endpoint: Endpoint
  credentials: Credential[]
  onChanged: () => Promise<void>
}) {
  const [value, setValue] = useState(endpoint.credential_id ?? "")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  useEffect(() => setValue(endpoint.credential_id ?? ""), [endpoint.credential_id])
  async function save() {
    setBusy(true)
    setError("")
    try {
      await api.patch(`/api/endpoints/${endpoint.id}`, { credential_id: value || null })
      await onChanged()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="mt-3 space-y-2">
      <div className="flex flex-wrap items-end gap-2">
        <Select
          id={`credential-${endpoint.id}`}
          label="关联只读凭据"
          help="asset.endpoint.readCredential"
          disabled={busy}
          value={value}
          onChange={setValue}
          options={[{ value: "", label: "不关联凭据" }, ...credentials.map((c) => ({ value: c.id, label: c.alias }))]}
        />
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={busy || value === (endpoint.credential_id ?? "")}
          onClick={() => void save()}
        >
          保存凭据关联
        </Button>
      </div>
      {error ? (
        <InlineNotice role="alert" tone="bad">
          {error}
        </InlineNotice>
      ) : null}
    </div>
  )
}

function RelationsTab({
  asset,
  names,
  editable,
  onChanged,
}: {
  asset: AssetDetail
  names: Record<string, string>
  editable: boolean
  onChanged: () => Promise<void>
}) {
  const [hostNo, setHostNo] = useState("")
  const [kind, setKind] = useState("hosts")
  const [direction, setDirection] = useState<"in" | "out">("in")
  const [error, setError] = useState<string | null>(null)

  async function add(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    try {
      const found = await api.get<Page<Asset>>(`/api/assets${query({ q: hostNo.trim(), limit: 20 })}`)
      const other = found.items.find((a) => a.asset_no === hostNo.trim() || a.name === hostNo.trim())
      if (!other) {
        setError("没有找到这个资产编号或名称")
        return
      }
      const [source, target] = direction === "in" ? [other.id, asset.id] : [asset.id, other.id]
      await api.post("/api/relations", { source_asset_id: source, target_asset_id: target, kind })
      setHostNo("")
      await onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "添加失败")
    }
  }

  async function remove(id: string) {
    try {
      await api.del(`/api/relations/${id}`)
      await onChanged()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        这里是已确认的资产关系，与拓扑画布分开存放；删除画布节点不会删除这里的关系。
      </p>
      {asset.relations.length === 0 ? <p className="text-[0.8125rem] text-muted-foreground">还没有关联。</p> : null}
      <ul className="space-y-2">
        {asset.relations.map((r) => {
          const incoming = r.target_asset_id === asset.id
          const other = names[incoming ? r.source_asset_id : r.target_asset_id] ?? "…"
          return (
            <li key={r.id} className="flex items-center justify-between gap-3 rounded-lg border p-3 text-[0.8125rem]">
              <span>
                {incoming ? (
                  <>
                    <strong className="font-medium">{other}</strong> {labelOf(RELATION_LABEL, r.kind)} 本资产
                  </>
                ) : (
                  <>
                    本资产 {labelOf(RELATION_LABEL, r.kind)} <strong className="font-medium">{other}</strong>
                  </>
                )}
                <span className="ml-2 text-xs text-muted-foreground">
                  {r.origin === "manual" ? "手动" : r.origin === "legacy" ? "旧版导入" : "模板导入"}
                </span>
              </span>
              {editable ? (
                <Button variant="ghost" size="icon-sm" aria-label="删除关联" onClick={() => void remove(r.id)}>
                  <Trash2 aria-hidden />
                </Button>
              ) : null}
            </li>
          )
        })}
      </ul>
      {error ? (
        <InlineNotice tone="bad" role="alert">
          {error}
        </InlineNotice>
      ) : null}
      {editable ? (
        <form onSubmit={add} className="flex flex-wrap items-end gap-2 rounded-lg border p-3">
          <Select
            id="rel-dir"
            label="方向"
            hint="箭头指向被承载 / 下游的一方"
            value={direction}
            onChange={(v) => setDirection(v as "in" | "out")}
            options={[
              { value: "in", label: "对方 → 本资产" },
              { value: "out", label: "本资产 → 对方" },
            ]}
          />
          <Select id="rel-kind" label="关系" help="asset.relation" value={kind} onChange={setKind} options={optionsFrom(RELATION_LABEL)} />
          <TextField
            id="rel-other"
            hint="填对方的资产编号或完整名称"
            label="对方资产编号或名称"
            value={hostNo}
            onChange={setHostNo}
            className="min-w-48 flex-1"
          />
          <Button type="submit" size="sm" disabled={!hostNo.trim()}>
            添加关联
          </Button>
        </form>
      ) : null}
    </div>
  )
}

function EditTab({
  asset,
  sites,
  onDirtyChange,
  onSaved,
  onReload,
  onDeleted,
}: {
  asset: AssetDetail
  sites: Site[]
  onDirtyChange: (d: boolean) => void
  onSaved: () => Promise<void>
  onReload: () => Promise<void>
  onDeleted: () => void
}) {
  // 编辑基于打开时的快照；后台数据刷新不会覆盖正在输入的内容
  const [base, setBase] = useState<{ draft: AssetDraft; revision: number }>(() => ({
    draft: draftOf(asset),
    revision: asset.revision,
  }))
  const [draft, setDraft] = useState<AssetDraft>(base.draft)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  // 回收站保留天数，点“删除资产”时再读，确认文字里告诉用户能恢复多久
  const [keepDays, setKeepDays] = useState<number | null>(null)
  const [informationEndpoint, setInformationEndpoint] = useState(asset.endpoints.find((ep) => ep.purpose !== "control")?.id ?? "")
  const changes = payloadOf(draft, base.draft)
  const dirty = Object.keys(changes).length > 0

  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange])
  useEffect(() => () => onDirtyChange(false), [onDirtyChange])

  async function save(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      const updated = await api.patch<Asset>(`/api/assets/${asset.id}`, { ...changes, revision: base.revision })
      setBase({ draft: draftOf(updated), revision: updated.revision })
      setDraft(draftOf(updated))
      setSaved(true)
      await onSaved()
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && err.message.includes("修订号")) setConflict(true)
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function reloadLatest() {
    const latest = await api.get<AssetDetail>(`/api/assets/${asset.id}`)
    setBase({ draft: draftOf(latest), revision: latest.revision })
    setDraft(draftOf(latest))
    setConflict(false)
    setError(null)
    await onReload()
  }

  async function remove() {
    try {
      await api.del(`/api/assets/${asset.id}`)
      onDeleted()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return (
    <form onSubmit={save} className="space-y-4">
      <Select id="edit-information-endpoint" label="获取信息使用的管理端点" help="asset.infoEndpoint" value={informationEndpoint}
        onChange={setInformationEndpoint} options={[{ value: "", label: "请选择管理端点" }, ...asset.endpoints.map((ep) => ({ value: ep.id, label: `${PROTOCOL_LABEL[ep.protocol] ?? ep.protocol} · ${ep.address}${ep.port ? `:${ep.port}` : ""}` }))]} />
      {!asset.endpoints.length ? <p className="text-xs text-muted-foreground">请先在“管理端点”添加连接地址和凭据。</p> : null}
      <InformationPreview source={informationEndpoint ? { endpoint_id: informationEndpoint } : null} draft={draft}
        onApply={(values) => setDraft((previous) => ({ ...previous, ...values }))} disabled={busy} />
      <AssetFields draft={draft} onChange={setDraft} sites={sites} idPrefix="edit" />
      {conflict ? (
        <InlineNotice tone="warn" role="alert">
          <p>{error}</p>
          <div className="mt-2 flex gap-2">
            <Button type="button" size="xs" variant="outline" onClick={() => void reloadLatest()}>
              载入最新内容（放弃我的修改）
            </Button>
          </div>
        </InlineNotice>
      ) : error ? (
        <InlineNotice tone="bad" role="alert">
          {error}
        </InlineNotice>
      ) : saved && !dirty ? (
        <InlineNotice tone="ok">已保存（修订号 {base.revision}）。</InlineNotice>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" disabled={!dirty || busy || !draft.name.trim()}>
          {busy ? "正在保存…" : "保存修改"}
        </Button>
        <Button type="button" variant="outline" disabled={!dirty} onClick={() => setDraft(base.draft)}>
          撤销修改
        </Button>
        <span className="flex-1" />
        {confirmDelete ? (
          <>
            <span className="text-[0.8125rem]">
              移入回收站{keepDays ? `，保留 ${keepDays} 天` : ""}，期间可在“资产管理 → 回收站”恢复；到期后连同管理端点和关联彻底删除。
            </span>
            <Button type="button" variant="destructive" size="sm" onClick={() => void remove()}>
              确认删除
            </Button>
            <Button type="button" variant="outline" size="sm" onClick={() => setConfirmDelete(false)}>
              取消
            </Button>
          </>
        ) : (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              setConfirmDelete(true)
              api
                .get<{ retention_days: number }>("/api/assets/recycle-bin/settings")
                .then((r) => setKeepDays(r.retention_days))
                .catch(() => setKeepDays(null))
            }}
          >
            <Trash2 aria-hidden />
            删除资产
          </Button>
        )}
      </div>
    </form>
  )
}
