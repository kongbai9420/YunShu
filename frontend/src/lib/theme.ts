import { createContext, createElement, useCallback, useContext, useEffect, useMemo, useState } from "react"

import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { BUILTIN_THEMES, type Base, type ThemeInfo, type ThemePackage, type Tokens } from "@/themes"

/**
 * 外观（P23）：风格（简洁 / 玻璃 / 导入的主题）× 深浅（跟随系统 / 浅色 / 深色），两者分开选。
 * 平台默认由管理员设；每个账号可以覆盖，跟账号走（/api/appearance）。
 * 写在 <html> 上：data-style=基础风格（simple / glass，决定有没有玻璃表面）、data-skin=主题 id、
 * data-theme=固定的深浅（跟随系统时不写）、data-wallpaper=有自定义壁纸；主题包的变量用 style.setProperty 写入。
 * 最近一次的结果存在浏览器里（CACHE_KEY），index.html 在首帧前按它恢复，避免闪烁；两处要保持一致。
 */
export type Mode = "system" | "light" | "dark"

export const MODE_LABEL: Record<Mode, string> = { system: "跟随系统", light: "浅色", dark: "深色" }
export const MODES: Mode[] = ["system", "light", "dark"]

export interface AppearanceState {
  default: { style: string; mode: Mode }
  mine: { style: string | null; mode: Mode | null }
  themes: ThemePackage[]
  wallpaper: { url: string } | null
}

interface Resolved {
  id: string
  base: Base
  mode: Mode
  tokens: { light: Tokens; dark: Tokens } | null
  wallpaper: string | null
}

const CACHE_KEY = "yunshu-appearance"
const LEGACY_KEY = "yunshu-theme" // P23 之前只记深浅

const darkQuery = typeof window !== "undefined" ? window.matchMedia("(prefers-color-scheme: dark)") : null
let current: Resolved | null = null

export function allThemes(state: AppearanceState | null): ThemeInfo[] {
  const imported = (state?.themes ?? []).map((t) => ({ ...t, source: "imported" as const }))
  return [...BUILTIN_THEMES, ...imported]
}

export function themeName(themes: ThemeInfo[], id: string): string {
  return themes.find((t) => t.id === id)?.name ?? id
}

function resolve(state: AppearanceState): Resolved {
  const style = state.mine.style ?? state.default.style
  const mode = state.mine.mode ?? state.default.mode
  // 主题被删掉或找不到时退回简洁
  const theme = allThemes(state).find((t) => t.id === style) ?? BUILTIN_THEMES[0]
  const hasTokens = Object.keys(theme.light).length > 0 || Object.keys(theme.dark).length > 0
  return {
    id: theme.id,
    base: theme.base,
    mode,
    tokens: hasTokens ? { light: theme.light, dark: theme.dark } : null,
    wallpaper: theme.base === "glass" && state.wallpaper ? state.wallpaper.url : null,
  }
}

function isDark(mode: Mode): boolean {
  return mode === "dark" || (mode === "system" && !!darkQuery?.matches)
}

function apply(r: Resolved) {
  current = r
  const root = document.documentElement
  root.setAttribute("data-style", r.base)
  root.setAttribute("data-skin", r.id)
  if (r.mode === "system") root.removeAttribute("data-theme")
  else root.setAttribute("data-theme", r.mode)
  // 清掉上一个主题写的变量。index.html 首帧前按缓存写的也在 <html> 的行内样式里，所以按前缀全部清掉
  const old = Array.from({ length: root.style.length }, (_, i) => root.style.item(i)).filter((n) => n.startsWith("--"))
  for (const name of old) root.style.removeProperty(name)
  const tokens = r.tokens ? (isDark(r.mode) ? r.tokens.dark : r.tokens.light) : {}
  for (const [name, value] of Object.entries(tokens)) root.style.setProperty(`--${name}`, value)
  if (r.wallpaper) {
    root.style.setProperty("--app-wallpaper", `url("${r.wallpaper}")`)
    root.setAttribute("data-wallpaper", "")
  } else root.removeAttribute("data-wallpaper")
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(r))
    localStorage.removeItem(LEGACY_KEY)
  } catch {
    // 存储不可用时只是下次首帧会闪一下
  }
}

// 跟随系统时，系统切换深浅要换一套主题包变量（基础风格由 CSS 媒体查询处理）
darkQuery?.addEventListener("change", () => {
  if (current && current.mode === "system" && current.tokens) apply(current)
})

/** 登录前（或接口不可用时）按浏览器里记住的外观显示 */
function cached(): { style: string; mode: Mode } {
  try {
    const c = JSON.parse(localStorage.getItem(CACHE_KEY) ?? "null") as Resolved | null
    if (c && MODES.includes(c.mode)) return { style: c.id, mode: c.mode }
    const legacy = localStorage.getItem(LEGACY_KEY)
    if (legacy === "light" || legacy === "dark") return { style: "simple", mode: legacy }
  } catch {
    // 忽略
  }
  return { style: "simple", mode: "system" }
}

interface AppearanceContext {
  state: AppearanceState | null
  themes: ThemeInfo[]
  /** 当前生效的主题 id 与深浅 */
  style: string
  mode: Mode
  error: string | null
  /** 改自己的选择；null 表示用平台默认 */
  setMine: (choice: { style: string | null; mode: Mode | null }) => Promise<void>
  /** 管理员改了默认、导入或删除主题、换壁纸之后，用接口返回的新状态刷新 */
  replace: (state: AppearanceState) => void
  reload: () => void
}

const Ctx = createContext<AppearanceContext | null>(null)

export function AppearanceProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAuth()
  const [state, setState] = useState<AppearanceState | null>(null)
  const [error, setError] = useState<string | null>(null)

  const replace = useCallback((next: AppearanceState) => {
    setState(next)
    apply(resolve(next))
  }, [])

  const reload = useCallback(() => {
    api
      .get<AppearanceState>("/api/appearance", { quiet401: true })
      .then((s) => {
        replace(s)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [replace])

  useEffect(() => {
    if (user) reload()
  }, [user, reload])

  const setMine = useCallback(
    async (choice: { style: string | null; mode: Mode | null }) => {
      if (state) replace({ ...state, mine: choice }) // 先在本机生效，再保存到账号
      try {
        replace(await api.put<AppearanceState>("/api/appearance/mine", choice))
        setError(null)
      } catch (e) {
        setError(`外观没有保存到账号：${(e as Error).message}`)
        reload()
      }
    },
    [state, replace, reload],
  )

  const value = useMemo<AppearanceContext>(() => {
    const now = state ? resolve(state) : cached()
    return {
      state,
      themes: allThemes(state),
      style: "id" in now ? now.id : now.style,
      mode: now.mode,
      error,
      setMine,
      replace,
      reload,
    }
  }, [state, error, setMine, replace, reload])

  return createElement(Ctx.Provider, { value }, children)
}

export function useAppearance(): AppearanceContext {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error("useAppearance 必须在 AppearanceProvider 里使用")
  return ctx
}
