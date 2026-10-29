import { useCallback, useEffect, useState } from "react"
import { Tabs } from "radix-ui"
import { Download, KeyRound, Plus, Trash2 } from "lucide-react"

import { PageHeader } from "@/components/AppShell"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { InlineNotice, Select, panelClass } from "@/components/ui/controls"
import { TextField } from "@/components/ui/form"
import { api, type Credential, type Site, type User, type UserRole } from "@/lib/api"
import { ROLE_LABEL, canEdit, seesAll, useAuth } from "@/lib/auth"
import { cn } from "@/lib/utils"
import { WorkersTab } from "./WorkersTab"
import { TokensTab } from "./TokensTab"
import { AiTab } from "./AiTab"
import { AppearanceTab } from "./AppearanceTab"
import { ScopeNotice, ScopeReason } from "@/components/ScopeNote"
import { ScopeDialog } from "./ScopeDialog"

const CRED_KIND: Record<string, string> = {
  password: "用户名 + 密码",
  snmp_community: "SNMP 团体名",
  snmpv3: "SNMPv3",
  ssh_key: "SSH 私钥",
  token: "API 令牌",
}
const SITE_KIND: Record<string, string> = { site: "场所", home: "家庭", datacenter: "机房", room: "房间" }

function when(iso: string | null): string {
  if (!iso) return "从未"
  return new Date(iso + (iso.endsWith("Z") || iso.includes("+") ? "" : "Z")).toLocaleString()
}

function useList<T>(path: string, enabled = true) {
  const [items, setItems] = useState<T[]>([])
  const [error, setError] = useState<string | null>(null)
  const reload = useCallback(() => {
    if (!enabled) return
    api
      .get<T[]>(path)
      .then((r) => {
        setItems(r)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [path, enabled])
  useEffect(reload, [reload])
  return { items, error, reload, setError }
}

/** 链接里的 ?tab=（例如顶栏外观菜单的 #/settings?tab=appearance） */
function tabFromHash(): string | null {
  return new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("tab")
}

function ErrorNote({ error }: { error: string | null }) {
  return error ? (
    <InlineNotice tone="bad" role="alert">
      {error}
    </InlineNotice>
  ) : null
}

export function SettingsPage() {
  const { user } = useAuth()
  const isAdmin = user?.role === "admin"
  const tabs = [
    { value: "credentials", label: "凭据" },
    { value: "sites", label: "场所" },
    ...(canEdit(user) ? [{ value: "workers", label: "Worker 状态" }] : []),
    ...(isAdmin
      ? [
          { value: "users", label: "用户与角色" },
          { value: "backups", label: "备份" },
          { value: "audit", label: "审计日志" },
          { value: "ai", label: "AI" },
        ]
      : []),
    { value: "tokens", label: "API 令牌" },
    { value: "appearance", label: "外观" },
    { value: "account", label: "我的账户" },
  ]
  const [tab, setTab] = useState(() => {
    const wanted = tabFromHash()
    return wanted && tabs.some((t) => t.value === wanted) ? wanted : "credentials"
  })
  // 已在设置页时点外观菜单里的链接，只有 hash 变化，页面不会重新挂载
  useEffect(() => {
    const onChange = () => {
      const wanted = tabFromHash()
      if (wanted) setTab(wanted)
    }
    window.addEventListener("hashchange", onChange)
    return () => window.removeEventListener("hashchange", onChange)
  }, [])
  return (
    <div className="pb-8">
      <PageHeader title="平台设置" description="凭据、场所、用户、备份、外观与 API 令牌。" />
      <Tabs.Root value={tab} onValueChange={setTab} className="px-6">
        <Tabs.List aria-label="平台设置" className="mb-4 flex gap-1 overflow-x-auto overflow-y-hidden border-b">
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
        <Tabs.Content value="credentials">
          <CredentialsTab editable={canEdit(user)} canCreate={seesAll(user)} />
        </Tabs.Content>
        <Tabs.Content value="sites">
          <SitesTab editable={canEdit(user)} scopeLimited={!seesAll(user)} />
        </Tabs.Content>
        {canEdit(user) ? (
          <Tabs.Content value="workers">
            {seesAll(user) ? <WorkersTab /> : <ScopeNotice what="Worker 状态" />}
          </Tabs.Content>
        ) : null}
        {isAdmin ? (
          <>
            <Tabs.Content value="users">
              <UsersTab me={user} />
            </Tabs.Content>
            <Tabs.Content value="backups">
              <BackupsTab />
            </Tabs.Content>
            <Tabs.Content value="audit">
              <AuditTab />
            </Tabs.Content>
            <Tabs.Content value="ai">
              <AiTab />
            </Tabs.Content>
          </>
        ) : null}
        <Tabs.Content value="tokens">
          <TokensTab />
        </Tabs.Content>
        <Tabs.Content value="appearance">
          <AppearanceTab admin={isAdmin} />
        </Tabs.Content>
        <Tabs.Content value="account">
          <AccountTab />
        </Tabs.Content>
      </Tabs.Root>
    </div>
  )
}

function CredentialsTab({ editable, canCreate }: { editable: boolean; canCreate: boolean }) {
  const { items, error, reload, setError } = useList<Credential>("/api/credentials")
  const [alias, setAlias] = useState("")
  const [kind, setKind] = useState("password")
  const [username, setUsername] = useState("")
  const [secret, setSecret] = useState("")
  const [rotating, setRotating] = useState<string | null>(null)
  const [newSecret, setNewSecret] = useState("")
  const [notice, setNotice] = useState<string | null>(null)

  async function add(e: React.FormEvent) {
    e.preventDefault()
    try {
      await api.post("/api/credentials", { alias: alias.trim(), kind, username: username.trim(), secret })
      setAlias("")
      setUsername("")
      setSecret("")
      setNotice("凭据已加密保存。")
      reload()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  async function rotate(id: string) {
    try {
      await api.patch(`/api/credentials/${id}`, { secret: newSecret })
      setRotating(null)
      setNewSecret("")
      setNotice("已更新，旧密码不再保存。")
      reload()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  async function remove(id: string) {
    try {
      await api.del(`/api/credentials/${id}`)
      reload()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return (
    <div className="max-w-3xl space-y-4">
      <InlineNotice>
        密码、团体名、私钥和令牌只以加密形式保存，任何人在界面和接口里都看不到原文，只能整体替换。管理端点和导入模板用“别名”引用凭据。
      </InlineNotice>
      <ErrorNote error={error} />
      {notice ? <InlineNotice tone="ok">{notice}</InlineNotice> : null}
      <ul className="space-y-2">
        {items.map((c) => (
          <li key={c.id} className={cn(panelClass, "p-3 text-[0.8125rem]")}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 font-medium">
                  <KeyRound aria-hidden className="size-4 text-info" />
                  {c.alias}
                </div>
                <div className="mt-0.5 text-xs text-muted-foreground">
                  {CRED_KIND[c.kind] ?? c.kind}
                  {c.username ? ` · 用户名 ${c.username}` : ""} · 更新于 {when(c.updated_at)} · 密钥编号 {c.key_id}
                </div>
              </div>
              {editable ? (
                <div className="flex gap-1">
                  <Button size="xs" variant="outline" onClick={() => setRotating(rotating === c.id ? null : c.id)}>
                    替换密码
                  </Button>
                  <Button size="icon-xs" variant="ghost" aria-label={`删除凭据 ${c.alias}`} onClick={() => void remove(c.id)}>
                    <Trash2 aria-hidden />
                  </Button>
                </div>
              ) : null}
            </div>
            {rotating === c.id ? (
              <form
                className="mt-3 flex flex-wrap items-end gap-2"
                onSubmit={(e) => {
                  e.preventDefault()
                  void rotate(c.id)
                }}
              >
                <TextField id={`rot-${c.id}`} help="settings.credential.rotate" label="新的密码 / 密钥" type="password" value={newSecret} onChange={setNewSecret} autoComplete="new-password" className="min-w-56 flex-1" />
                <Button type="submit" size="sm" disabled={!newSecret}>
                  保存
                </Button>
              </form>
            ) : null}
          </li>
        ))}
        {items.length === 0 ? <li className="text-[0.8125rem] text-muted-foreground">还没有凭据。</li> : null}
      </ul>
      {editable && !canCreate ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" disabled title="单独新建凭据只对可见全部资产的账号开放">
            <Plus aria-hidden />
            添加凭据
          </Button>
          <ScopeReason what="单独新建凭据" />
          <span className="text-xs text-muted-foreground">可以在资产的管理端点里同时新建凭据。</span>
        </div>
      ) : null}
      {editable && canCreate ? (
        <form onSubmit={add} className={cn(panelClass, "grid grid-cols-1 gap-3 p-3 sm:grid-cols-2")}>
          <TextField id="cred-alias" label="别名" value={alias} onChange={setAlias} required hint="如 idrac-默认、交换机-只读" />
          <Select id="cred-kind" help="settings.credential.kind" label="类型" value={kind} onChange={setKind} options={Object.entries(CRED_KIND).map(([value, label]) => ({ value, label }))} />
          <TextField id="cred-user" hint="SNMP 团体名、API 令牌可留空" label="用户名" value={username} onChange={setUsername} autoComplete="off" />
          <TextField id="cred-secret" hint="加密保存，保存后不再显示明文" label="密码 / 密钥" type="password" value={secret} onChange={setSecret} required autoComplete="new-password" />
          <div className="sm:col-span-2">
            <Button type="submit" size="sm" disabled={!alias.trim() || !secret}>
              <Plus aria-hidden />
              添加凭据
            </Button>
          </div>
        </form>
      ) : null}
    </div>
  )
}

function SitesTab({ editable, scopeLimited }: { editable: boolean; scopeLimited: boolean }) {
  const { items, error, reload, setError } = useList<Site>("/api/sites")
  const [name, setName] = useState("")
  const [kind, setKind] = useState("site")
  const [parent, setParent] = useState("")
  const nameOf = new Map(items.map((s) => [s.id, s.name]))

  async function add(e: React.FormEvent) {
    e.preventDefault()
    try {
      await api.post("/api/sites", { name: name.trim(), kind, parent_id: parent || null })
      setName("")
      reload()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  async function remove(id: string) {
    try {
      await api.del(`/api/sites/${id}`)
      reload()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return (
    <div className="max-w-3xl space-y-4">
      <ErrorNote error={error} />
      <ul className="space-y-2">
        {items.map((s) => (
          <li key={s.id} className={cn(panelClass, "flex items-center justify-between gap-2 p-3 text-[0.8125rem]")}>
            <span>
              <span className="font-medium">{s.name}</span>
              <span className="ml-2 text-xs text-muted-foreground">
                {SITE_KIND[s.kind] ?? s.kind}
                {s.parent_id ? ` · 属于 ${nameOf.get(s.parent_id) ?? "—"}` : ""}
              </span>
            </span>
            {editable ? (
              <Button size="icon-xs" variant="ghost" aria-label={`删除场所 ${s.name}`} onClick={() => void remove(s.id)}
                disabled={scopeLimited} title={scopeLimited ? "场所管理只对可见全部资产的账号开放" : undefined}>
                <Trash2 aria-hidden />
              </Button>
            ) : null}
          </li>
        ))}
        {items.length === 0 ? <li className="text-[0.8125rem] text-muted-foreground">还没有场所。模板导入时填写的场所会自动新建。</li> : null}
      </ul>
      {editable && scopeLimited ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" disabled title="场所管理只对可见全部资产的账号开放">
            <Plus aria-hidden />
            添加场所
          </Button>
          <ScopeReason what="场所管理" />
        </div>
      ) : null}
      {editable && !scopeLimited ? (
        <form onSubmit={add} className={cn(panelClass, "flex flex-wrap items-end gap-2 p-3")}>
          <TextField id="site-name" plain label="名称" value={name} onChange={setName} required className="min-w-48 flex-1" />
          <Select id="site-kind" help="settings.site.kind" label="类型" value={kind} onChange={setKind} options={Object.entries(SITE_KIND).map(([value, label]) => ({ value, label }))} />
          <Select id="site-parent" help="settings.site.parent" label="上级" value={parent} onChange={setParent} options={[{ value: "", label: "无" }, ...items.map((s) => ({ value: s.id, label: s.name }))]} />
          <Button type="submit" size="sm" disabled={!name.trim()}>
            <Plus aria-hidden />
            添加场所
          </Button>
        </form>
      ) : null}
    </div>
  )
}

function UsersTab({ me }: { me: User | null }) {
  const { items, error, reload, setError } = useList<User>("/api/users")
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [role, setRole] = useState<UserRole>("viewer")
  const [resetting, setResetting] = useState<string | null>(null)
  const [resetPw, setResetPw] = useState("")
  const [scoping, setScoping] = useState<User | null>(null)
  const roleOptions = (Object.keys(ROLE_LABEL) as UserRole[]).map((r) => ({ value: r, label: ROLE_LABEL[r] }))

  async function act(fn: () => Promise<unknown>) {
    try {
      await fn()
      reload()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return (
    <div className="max-w-3xl space-y-4">
      <InlineNotice>三个角色：管理员可以管理用户、备份和查看审计；运维可以增删改资产、凭据和导入；只读只能查看。新建或重置密码的用户首次登录必须改密码。</InlineNotice>
      <ErrorNote error={error} />
      <ul className="space-y-2">
        {items.map((u) => (
          <li key={u.id} className={cn(panelClass, "p-3 text-[0.8125rem]")}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="min-w-0">
                <span className="font-medium">{u.display_name || u.username}</span>
                <span className="ml-2 text-xs text-muted-foreground">
                  {u.username} · 最近登录 {when(u.last_login_at)}
                </span>
                {!u.is_active ? (
                  <Badge tone="neutral" className="ml-2">
                    已停用
                  </Badge>
                ) : null}
                {u.require_password_change ? (
                  <Badge tone="warn" className="ml-2">
                    待改密码
                  </Badge>
                ) : null}
              </span>
              <span className="flex flex-wrap items-center gap-1">
                <label className="sr-only" htmlFor={`role-${u.id}`}>
                  {u.username} 的角色
                </label>
                <select
                  id={`role-${u.id}`} data-help-plain="" title="管理员：全部功能；运维：查看并操作；只读：只能查看"
                  value={u.role}
                  onChange={(e) => void act(() => api.patch(`/api/users/${u.id}`, { role: e.target.value }))}
                  className="h-7 rounded-md border border-input bg-card px-1.5 text-xs"
                >
                  {roleOptions.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
                {u.role !== "admin" ? (
                  <Button size="xs" variant="outline" aria-label={`资产范围 ${u.username}`} onClick={() => setScoping(u)}>
                    资产范围：{u.scope_all === false ? "限定范围" : "可见全部"}
                  </Button>
                ) : null}
                <Button size="xs" variant="outline" onClick={() => setResetting(resetting === u.id ? null : u.id)}>
                  重置密码
                </Button>
                {u.id !== me?.id ? (
                  <Button size="xs" variant="ghost" onClick={() => void act(() => api.patch(`/api/users/${u.id}`, { is_active: !u.is_active }))}>
                    {u.is_active ? "停用" : "启用"}
                  </Button>
                ) : null}
              </span>
            </div>
            {resetting === u.id ? (
              <form
                className="mt-3 flex flex-wrap items-end gap-2"
                onSubmit={(e) => {
                  e.preventDefault()
                  void act(async () => {
                    await api.patch(`/api/users/${u.id}`, { password: resetPw })
                    setResetting(null)
                    setResetPw("")
                  })
                }}
              >
                <TextField id={`reset-${u.id}`} help="settings.user.tempPassword" label="临时密码（至少 8 位）" type="password" value={resetPw} onChange={setResetPw} autoComplete="new-password" className="min-w-56 flex-1" />
                <Button type="submit" size="sm" disabled={resetPw.length < 8}>
                  保存
                </Button>
              </form>
            ) : null}
          </li>
        ))}
      </ul>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          void act(async () => {
            await api.post("/api/users", { username: username.trim(), password, role })
            setUsername("")
            setPassword("")
          })
        }}
        className={cn(panelClass, "flex flex-wrap items-end gap-2 p-3")}
      >
        <TextField id="nu-name" label="用户名" value={username} onChange={setUsername} required autoComplete="off" hint="字母、数字和 _ . @ -" />
        <TextField id="nu-pass" label="初始密码" type="password" value={password} onChange={setPassword} required autoComplete="new-password" hint="至少 8 位" />
        <Select id="nu-role" help="settings.user.role" label="角色" value={role} onChange={(v) => setRole(v as UserRole)} options={roleOptions} />
        <Button type="submit" size="sm" disabled={!username.trim() || password.length < 8}>
          <Plus aria-hidden />
          新建用户
        </Button>
      </form>
      {scoping ? <ScopeDialog user={scoping} onClose={() => setScoping(null)} onSaved={reload} /> : null}
    </div>
  )
}

interface BackupInfo {
  name: string
  created_at: string
  reason: string
  size: number
  asset_count: number
  key_ids: string[]
}
const REASON: Record<string, string> = { scheduled: "定时", manual: "手动", "pre-migrate": "升级前", "pre-restore": "恢复前" }

function BackupsTab() {
  const [data, setData] = useState<{ items: BackupInfo[]; interval_hours: number; restore_hint: string; key_hint: string; database?: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => {
    api
      .get<typeof data>("/api/backups")
      .then(setData)
      .catch((e: Error) => setError(e.message))
  }, [])
  useEffect(load, [load])

  async function backupNow() {
    setBusy(true)
    try {
      await api.post("/api/backups")
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-3xl space-y-4">
      <InlineNotice tone="warn">
        <strong className="font-semibold">备份不含主密钥。</strong>
        {data?.key_hint.replace("备份不含主密钥。", "")} 主密钥丢失后，已保存的设备密码无法解密，需要重新录入；资产数据不受影响。
      </InlineNotice>
      <ErrorNote error={error} />
      <div className="flex flex-wrap items-center gap-3 text-[0.8125rem]">
        <Button size="sm" onClick={() => void backupNow()} disabled={busy}>
          {busy ? "正在备份…" : "立即备份"}
        </Button>
        <span className="text-muted-foreground">
          {data ? (data.interval_hours > 0 ? `定时备份：每 ${data.interval_hours} 小时一次` : "定时备份已关闭") : ""}
        </span>
      </div>
      {data?.database ? (
        <p className="text-xs text-muted-foreground" aria-label="数据库类型">
          {data.database === "postgresql"
            ? "当前数据库：外部 PostgreSQL（标准版）。备份用 pg_dump 生成可校验的 .dump 文件，客户端工具版本需不低于数据库服务器。"
            : "当前数据库：本地 SQLite 文件库（轻量版）。备份为数据库文件的一致快照。"}
        </p>
      ) : null}
      <ul className="space-y-2">
        {data?.items.map((b) => (
          <li key={b.name} className={cn(panelClass, "flex items-center justify-between gap-2 p-3 text-[0.8125rem]")}>
            <span className="min-w-0">
              <span className="block truncate font-medium">{b.name}</span>
              <span className="text-xs text-muted-foreground">
                {REASON[b.reason] ?? b.reason} · {when(b.created_at.replace(/\+00:00$/, "Z"))} · 资产 {b.asset_count} 台 · {(b.size / 1024).toFixed(0)} KB
              </span>
            </span>
            <Button asChild size="xs" variant="outline">
              <a href={`/api/backups/${encodeURIComponent(b.name)}`} download>
                <Download aria-hidden />
                下载
              </a>
            </Button>
          </li>
        ))}
        {data && data.items.length === 0 ? <li className="text-[0.8125rem] text-muted-foreground">还没有备份。</li> : null}
      </ul>
      {data ? <p className="text-xs text-muted-foreground">{data.restore_hint}</p> : null}
    </div>
  )
}

interface AuditEvent {
  id: string
  occurred_at: string
  actor_name: string
  action: string
  target_type: string
  target_id: string
  summary: string
}

function AuditTab() {
  const [items, setItems] = useState<AuditEvent[]>([])
  const [total, setTotal] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(() => {
    api
      .get<{ items: AuditEvent[]; total: number }>("/api/audit?limit=200")
      .then((r) => {
        setItems(r.items)
        setTotal(r.total)
      })
      .catch((e: Error) => setError(e.message))
  }, [])
  useEffect(load, [load])
  return (
    <div className="max-w-4xl space-y-3">
      <p className="text-xs text-muted-foreground">
        所有写操作和登录都会记录，只能查看、不能修改；详情里的密码、令牌已替换为 ***。显示最近 {items.length} 条，共 {total} 条。
      </p>
      <ErrorNote error={error} />
      <div className={cn(panelClass, "overflow-x-auto")}>
        <table className="data-table w-full min-w-[640px]">
          <caption className="sr-only">审计日志</caption>
          <thead>
            <tr>
              <th scope="col">时间</th>
              <th scope="col">操作人</th>
              <th scope="col">操作</th>
              <th scope="col">说明</th>
            </tr>
          </thead>
          <tbody>
            {items.map((e) => (
              <tr key={e.id}>
                <td className="whitespace-nowrap tabular">{when(e.occurred_at)}</td>
                <td>{e.actor_name || <span className="text-muted-foreground">系统</span>}</td>
                <td className="text-xs text-muted-foreground">{e.action}</td>
                <td>{e.summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function AccountTab() {
  const { user, changePassword } = useAuth()
  const [oldPw, setOldPw] = useState("")
  const [newPw, setNewPw] = useState("")
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null)
  return (
    <form
      className="max-w-sm space-y-3"
      onSubmit={async (e) => {
        e.preventDefault()
        try {
          await changePassword(oldPw, newPw)
          setOldPw("")
          setNewPw("")
          setMsg({ tone: "ok", text: "密码已修改，其他设备上的登录已退出。" })
        } catch (err) {
          setMsg({ tone: "bad", text: (err as Error).message })
        }
      }}
    >
      <p className="text-[0.8125rem]">
        当前用户：<span className="font-medium">{user?.display_name || user?.username}</span>（{user ? ROLE_LABEL[user.role] : ""}）
      </p>
      <TextField id="acc-old" plain label="当前密码" type="password" value={oldPw} onChange={setOldPw} autoComplete="current-password" required />
      <TextField id="acc-new" label="新密码" type="password" value={newPw} onChange={setNewPw} autoComplete="new-password" required hint="至少 8 位" />
      {msg ? (
        <InlineNotice tone={msg.tone} role={msg.tone === "bad" ? "alert" : "status"}>
          {msg.text}
        </InlineNotice>
      ) : null}
      <Button type="submit" size="sm" disabled={!oldPw || newPw.length < 8}>
        修改密码
      </Button>
    </form>
  )
}
