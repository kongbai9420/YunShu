import { useState } from "react"
import { Database } from "lucide-react"

import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { LabeledField, TextField } from "@/components/ui/form"
import { HelpPill } from "@/components/HelpPill"
import { ApiError, api } from "@/lib/api"

interface CheckResult { ok: boolean; message: string; version: string; state: string }

const TIERS = [
  { kind: "sqlite", title: "轻量版（SQLite）", fit: "个人、小机房", note: "数据存在本机文件里，不需要额外的数据库服务，开箱即用。以后可用升级工具迁到 PostgreSQL。" },
  { kind: "postgresql", title: "标准版（PostgreSQL）", fit: "中小企业、大型企业", note: "连接已有的 PostgreSQL 16 或更高版本。支持多个 Worker 同时采集，适合资产多、多人使用的场景。" },
] as const

/** 首次启动向导（4.1 P12）：全新安装且部署没有指定数据库时显示，规则见 backend/app/setup_wizard.py */
export function SetupPage({ tokenRequired, onDone }: { tokenRequired: boolean; onDone: () => void }) {
  const [kind, setKind] = useState<"sqlite" | "postgresql">("sqlite")
  const [form, setForm] = useState({ host: "", port: "5432", database: "yunshu", username: "yunshu", password: "", sslmode: "prefer" })
  const [token, setToken] = useState("")
  const [check, setCheck] = useState<CheckResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<"test" | "apply" | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const body = () => ({ kind, ...form, port: Number(form.port) || 5432, token })
  const set = (key: keyof typeof form) => (value: string) => { setForm({ ...form, [key]: value }); setCheck(null) }

  async function run(action: "test" | "apply") {
    setBusy(action)
    setError(null)
    try {
      if (action === "test") setCheck(await api.post<CheckResult>("/api/setup/test", body()))
      else setDone((await api.post<{ message: string }>("/api/setup/apply", body())).message)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "操作失败")
    } finally {
      setBusy(null)
    }
  }

  if (done) {
    return (
      <Frame>
        <InlineNotice tone="ok" role="status">{done}</InlineNotice>
        <Button className="mt-4 w-full" onClick={onDone}>去登录</Button>
      </Frame>
    )
  }

  const pgReady = kind === "sqlite" || (form.host.trim() && form.database.trim() && form.username.trim())
  return (
    <Frame>
      <p className="mb-4 text-[0.8125rem] text-muted-foreground">第一次启动，请选择数据存放方式。选好后平台会自动建表并创建初始管理员。</p>
      <fieldset className="space-y-2" aria-label="数据库档位">
        <legend className="mb-1 inline-flex items-center text-xs font-medium text-muted-foreground">数据库档位<HelpPill topic="setup.tier" /></legend>
        {TIERS.map((t) => (
          <label key={t.kind} className={`flex cursor-pointer gap-3 rounded-xl border p-3 ${kind === t.kind ? "border-info bg-info/5" : ""}`}>
            <input type="radio" name="tier" data-help-plain="" className="mt-1" checked={kind === t.kind} onChange={() => { setKind(t.kind); setCheck(null) }} />
            <span>
              <span className="block text-sm font-medium">{t.title}<span className="ml-2 text-xs font-normal text-muted-foreground">适合{t.fit}</span></span>
              <span className="mt-0.5 block text-xs text-muted-foreground">{t.note}</span>
            </span>
          </label>
        ))}
      </fieldset>

      {kind === "postgresql" ? (
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-[1fr_6rem]">
          <TextField id="setup-host" hint="PostgreSQL 服务器的地址或主机名" label="主机" value={form.host} onChange={set("host")} placeholder="数据库地址或主机名" required />
          <TextField id="setup-port" hint="PostgreSQL 默认 5432" label="端口" value={form.port} onChange={set("port")} inputMode="numeric" />
          <TextField id="setup-db" hint="需要一个空库，或本平台以前用过的库" label="数据库名" value={form.database} onChange={set("database")} required />
          <LabeledField label="加密连接" help="setup.sslmode">
            <select data-help-wrapped="" className="h-8 w-full rounded-md border bg-card px-2 text-[0.8125rem]" value={form.sslmode} onChange={(e) => set("sslmode")(e.target.value)}>
              <option value="prefer">优先</option>
              <option value="require">必须</option>
              <option value="verify-full">校验证书</option>
              <option value="disable">关闭</option>
            </select>
          </LabeledField>
          <TextField id="setup-user" hint="需要能在该库里建表的账号" label="用户名" value={form.username} onChange={set("username")} autoComplete="off" required />
          <TextField id="setup-pass" plain label="密码" type="password" value={form.password} onChange={set("password")} autoComplete="new-password" />
          <p className="text-xs text-muted-foreground sm:col-span-2">需要一个空库，或本平台以前用过的库。连接信息保存在主密钥旁边，不放进数据目录。</p>
        </div>
      ) : null}

      {tokenRequired ? (
        <div className="mt-4">
          <TextField id="setup-token" label="设置口令" help="setup.token" value={token} onChange={setToken} autoComplete="off" required
            hint="不是从服务器本机打开时需要。口令在服务启动日志里，形如“从本机以外访问时需要设置口令：…”。" />
        </div>
      ) : null}

      {check ? <InlineNotice tone={check.ok ? "ok" : "bad"} role="status" className="mt-4">{check.message}</InlineNotice> : null}
      {error ? <InlineNotice tone="bad" role="alert" className="mt-4">{error}</InlineNotice> : null}

      <div className="mt-4 flex flex-wrap justify-end gap-2">
        {kind === "postgresql" ? (
          <Button variant="outline" disabled={!pgReady || busy !== null || (tokenRequired && !token)} onClick={() => void run("test")}>
            {busy === "test" ? "正在测试…" : "测试连接"}
          </Button>
        ) : null}
        <Button disabled={!pgReady || busy !== null || (tokenRequired && !token) || (kind === "postgresql" && !check?.ok)} onClick={() => void run("apply")}>
          {busy === "apply" ? "正在保存…" : kind === "sqlite" ? "使用轻量版" : "保存并使用"}
        </Button>
      </div>
    </Frame>
  )
}

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex min-h-svh items-center justify-center bg-background px-4 py-8">
      <div className="w-full max-w-lg rounded-2xl border bg-card p-6 shadow-card">
        <div className="mb-3 flex items-center gap-2">
          <Database aria-hidden className="size-5 text-info" />
          <h1 className="text-lg font-semibold tracking-tight">首次设置 · 选择数据库</h1>
        </div>
        {children}
      </div>
    </main>
  )
}
