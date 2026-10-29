import { useEffect, useState } from "react"
import { LockKeyhole } from "lucide-react"

import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { TextField } from "@/components/ui/form"
import { ApiError, api } from "@/lib/api"
import { useAuth } from "@/lib/auth"

function Frame({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <main className="flex min-h-svh items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm rounded-2xl border bg-card p-6 shadow-card">
        <div className="mb-5 flex items-center gap-2">
          <LockKeyhole aria-hidden className="size-5 text-info" />
          <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
        </div>
        {children}
      </div>
    </main>
  )
}

export function LoginPage() {
  const { login, status } = useAuth()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // 单点登录（4.2 P16）：部署开启后显示按钮；失败原因由回调带在地址里
  const [sso, setSso] = useState<{ enabled: boolean; label: string } | null>(null)
  const [ssoError] = useState(() => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("sso_error"))
  useEffect(() => { void api.get<{ enabled: boolean; label: string }>("/api/auth/oidc/status", { quiet401: true }).then(setSso, () => setSso(null)) }, [])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(username, password)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "登录失败")
    } finally {
      setBusy(false)
    }
  }

  return (
    <Frame title="登录云枢运维平台">
      {status === "offline" ? (
        <InlineNotice tone="bad" role="alert" className="mb-4">
          无法连接后端服务，请确认后端已启动。正在每 5 秒自动重试，连上后会回到原来的页面。
        </InlineNotice>
      ) : null}
      {ssoError ? <InlineNotice tone="bad" role="alert" className="mb-4">单点登录失败：{ssoError}</InlineNotice> : null}
      {sso?.enabled ? (
        <>
          <a href="/api/auth/oidc/start" className="mb-3 flex h-9 w-full items-center justify-center rounded-md border text-sm font-medium hover:bg-secondary">{sso.label}</a>
          <p className="mb-3 text-center text-xs text-muted-foreground">或使用本地账号</p>
        </>
      ) : null}
      <form onSubmit={submit} className="space-y-3">
        <TextField id="login-user" plain label="用户名" value={username} onChange={setUsername} autoComplete="username" required autoFocus />
        <TextField
          id="login-pass" plain
          label="密码"
          type="password"
          value={password}
          onChange={setPassword}
          autoComplete="current-password"
          required
        />
        {error ? (
          <InlineNotice tone="bad" role="alert">
            {error}
          </InlineNotice>
        ) : null}
        <Button type="submit" className="w-full" disabled={busy || !username || !password}>
          {busy ? "正在登录…" : "登录"}
        </Button>
      </form>
    </Frame>
  )
}

export function ChangePasswordPage({ forced }: { forced: boolean }) {
  const { changePassword, logout } = useAuth()
  const [oldPw, setOldPw] = useState("")
  const [newPw, setNewPw] = useState("")
  const [again, setAgain] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const mismatch = again !== "" && newPw !== again
  const tooShort = newPw !== "" && newPw.length < 8

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (mismatch || tooShort) return
    setBusy(true)
    setError(null)
    try {
      await changePassword(oldPw, newPw)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "修改失败")
    } finally {
      setBusy(false)
    }
  }

  return (
    <Frame title="修改密码">
      {forced ? (
        <InlineNotice tone="warn" className="mb-4">
          这是初始密码或管理员重置的密码，修改后才能继续使用。
        </InlineNotice>
      ) : null}
      <form onSubmit={submit} className="space-y-3">
        <TextField id="pw-old" plain label="当前密码" type="password" value={oldPw} onChange={setOldPw} autoComplete="current-password" required autoFocus />
        <TextField
          id="pw-new"
          label="新密码"
          type="password"
          value={newPw}
          onChange={setNewPw}
          autoComplete="new-password"
          required
          hint="至少 8 位"
          error={tooShort ? "新密码至少 8 位" : undefined}
        />
        <TextField
          id="pw-again" plain
          label="再次输入新密码"
          type="password"
          value={again}
          onChange={setAgain}
          autoComplete="new-password"
          required
          error={mismatch ? "两次输入不一致" : undefined}
        />
        {error ? (
          <InlineNotice tone="bad" role="alert">
            {error}
          </InlineNotice>
        ) : null}
        <div className="flex gap-2">
          <Button type="submit" className="flex-1" disabled={busy || !oldPw || !newPw || mismatch || tooShort}>
            {busy ? "正在保存…" : "保存新密码"}
          </Button>
          <Button type="button" variant="outline" onClick={() => void logout()}>
            退出
          </Button>
        </div>
      </form>
    </Frame>
  )
}
