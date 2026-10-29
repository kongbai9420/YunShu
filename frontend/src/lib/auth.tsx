import { createContext, useCallback, useContext, useEffect, useState } from "react"

import { ApiError, api, onUnauthorized, type User } from "@/lib/api"

interface AuthState {
  status: "loading" | "anonymous" | "ready" | "offline"
  user: User | null
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  changePassword: (oldPassword: string, newPassword: string) => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<AuthState["status"]>("loading")
  const [user, setUser] = useState<User | null>(null)

  const refresh = useCallback(async () => {
    try {
      const me = await api.get<{ user: User }>("/api/auth/me", { quiet401: true })
      setUser(me.user)
      setStatus("ready")
    } catch (e) {
      setUser(null)
      // 连不上后端（或反向代理 / 开发代理返回 502–504）不等于未登录：显示“无法连接”并自动重试，恢复后无需重新登录
      setStatus(e instanceof ApiError && [0, 500, 502, 503, 504].includes(e.status) ? "offline" : "anonymous")
    }
  }, [])

  useEffect(() => {
    if (status !== "offline") return
    const t = window.setInterval(() => void refresh(), 5000)
    return () => window.clearInterval(t)
  }, [status, refresh])

  useEffect(() => {
    void refresh()
    return onUnauthorized(() => {
      setUser(null)
      setStatus("anonymous")
    })
  }, [refresh])

  const login = useCallback(async (username: string, password: string) => {
    const res = await api.post<{ user: User }>("/api/auth/login", { username, password })
    setUser(res.user)
    setStatus("ready")
  }, [])

  const logout = useCallback(async () => {
    try {
      await api.post("/api/auth/logout")
    } finally {
      setUser(null)
      setStatus("anonymous")
    }
  }, [])

  const changePassword = useCallback(
    async (oldPassword: string, newPassword: string) => {
      await api.post("/api/auth/password", { old_password: oldPassword, new_password: newPassword })
      await refresh()
    },
    [refresh],
  )

  return (
    <AuthContext.Provider value={{ status, user, login, logout, changePassword, refresh }}>{children}</AuthContext.Provider>
  )
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error("useAuth 必须在 AuthProvider 内使用")
  return ctx
}

/** 运维和管理员可以改资产；只读用户只看（后端同样校验，这里只是不显示按钮） */
export function canEdit(user: User | null): boolean {
  return user?.role === "admin" || user?.role === "operator"
}

/** 是否可见全部资产（4.1 P11）：管理员始终是；其他账号看 scope_all */
export function seesAll(user: User | null): boolean {
  return user?.role === "admin" || user?.scope_all !== false
}

/** 范围受限账号遇到“只对全部可见开放”的功能时的统一说明 */
export const SCOPE_ONLY = "只对可见全部资产的账号开放，如需请联系管理员"

export const ROLE_LABEL: Record<User["role"], string> = { admin: "管理员", operator: "运维", viewer: "只读" }
