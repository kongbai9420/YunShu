import { useEffect, useState } from "react"
import {
  Activity,
  Bell,
  ClipboardCheck,
  Cloud,
  FileUp,
  Rows3,
  FlaskConical,
  HardDriveDownload,
  LayoutDashboard,
  LogOut,
  PanelLeft,
  PanelLeftClose,
  PanelLeftOpen,
  Server,
  Settings,
  ShieldAlert,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react"

import { AppearanceMenu } from "@/components/AppearanceMenu"
import { AssistantButton } from "@/components/AssistantPanel"
import { ROLE_LABEL, useAuth } from "@/lib/auth"
import { LIVE_STATUS_TEXT, useLiveEvents, type LiveStatus } from "@/lib/live"
import { hrefFor, type RouteId } from "@/lib/router"
import { cn } from "@/lib/utils"

interface NavItem {
  label: string
  icon: LucideIcon
  route: RouteId
}

/** 侧栏按旧版分组：组标题 + 一级入口，不再展开二级菜单 */
const NAV_GROUPS: { title: string; items: NavItem[] }[] = [
  {
    title: "状态监控",
    items: [
      { label: "总览", icon: LayoutDashboard, route: "overview" },
      { label: "监控中心", icon: Activity, route: "monitoring" },
      { label: "告警中心", icon: Bell, route: "alerts" },
      { label: "服务器告警", icon: HardDriveDownload, route: "server-events" },
      { label: "巡检", icon: ClipboardCheck, route: "inspection" },
      { label: "漏洞情报", icon: ShieldAlert, route: "vulns" },
    ],
  },
  {
    title: "资产管理",
    items: [
      { label: "资产列表", icon: Server, route: "assets" },
      { label: "批量导入", icon: FileUp, route: "import" },
      { label: "机柜与定位", icon: Rows3, route: "racks" },
    ],
  },
  {
    title: "运维与系统",
    items: [
      { label: "运维操作", icon: Wrench, route: "operations" },
      { label: "平台设置", icon: Settings, route: "settings" },
    ],
  },
]

/** 隔离的原型只用本地演示资源，其余页面已接真实接口。侧栏不显示，直接打开 #/overview-prototype 仍可用于开发核对。 */
const DEMO_ROUTES: RouteId[] = ["overview-prototype"]

const COLLAPSED_KEY = "yunshu-sidebar-collapsed"

/** 桌面侧栏折叠状态按浏览器记住；存储不可用时退回展开 */
function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1"
  } catch {
    return false
  }
}

const LIVE_SHORT: Record<LiveStatus, string> = {
  connecting: "连接中",
  live: "已连接",
  reconnecting: "重连中",
  paused: "已暂停",
}
const LIVE_DOT: Record<LiveStatus, string> = {
  connecting: "bg-neutral",
  live: "bg-ok",
  reconnecting: "bg-warn",
  paused: "bg-neutral",
}

/** 顶栏连接状态：圆点 + 文字（窄屏只留圆点，完整说明在 title 与读屏文字里） */
function ConnectionPill() {
  const { status } = useLiveEvents()
  return (
    <span
      title={`实时推送：${LIVE_STATUS_TEXT[status]}`}
      className="inline-flex h-7 items-center gap-1.5 rounded-full bg-accent px-2.5 text-xs text-muted-foreground"
    >
      <span aria-hidden className={cn("size-[7px] shrink-0 rounded-full", LIVE_DOT[status])} />
      <span aria-hidden className="hidden sm:inline">
        {LIVE_SHORT[status]}
      </span>
      <span className="sr-only">实时推送：{LIVE_STATUS_TEXT[status]}</span>
    </span>
  )
}

const iconButton =
  "inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground"

export function AppShell({ route, children }: { route: RouteId; children: React.ReactNode }) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(readCollapsed)
  const { user, logout } = useAuth()

  useEffect(() => {
    setMenuOpen(false)
  }, [route])

  function toggleCollapsed() {
    setCollapsed((c) => {
      try {
        localStorage.setItem(COLLAPSED_KEY, c ? "0" : "1")
      } catch {
        // 存储不可用时只在本次会话内生效
      }
      return !c
    })
  }

  // 折叠只作用于桌面宽度；窄屏抽屉始终显示完整菜单
  const rail = collapsed && !menuOpen
  const userName = user ? user.display_name || user.username : ""

  return (
    <div className="flex h-svh flex-col overflow-hidden">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-card px-3 py-2 focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
      >
        跳到主要内容
      </a>
      {/* 顶栏：48px 毛玻璃，品牌居中，右侧连接状态 / 外观 / 用户（对照旧版 .mac-titlebar，不做窗口按钮）。玻璃主题下悬浮成圆角片（themes/glass.css 按 data-shell 调整） */}
      <header data-shell="titlebar" className="relative z-20 flex h-12 shrink-0 items-center gap-1 border-b bg-titlebar px-2 backdrop-blur-xl backdrop-saturate-150 sm:px-3">
        <button
          type="button"
          className={cn(iconButton, "md:hidden")}
          onClick={() => setMenuOpen(true)}
          aria-label="打开导航"
          aria-expanded={menuOpen}
        >
          <PanelLeft aria-hidden className="size-4" />
        </button>
        <button
          type="button"
          className={cn(iconButton, "hidden md:inline-flex")}
          onClick={toggleCollapsed}
          aria-label={collapsed ? "展开侧栏" : "收起侧栏"}
          aria-expanded={!collapsed}
          title={collapsed ? "展开侧栏" : "收起侧栏"}
        >
          {collapsed ? <PanelLeftOpen aria-hidden className="size-4" /> : <PanelLeftClose aria-hidden className="size-4" />}
        </button>
        <div className="flex min-w-0 justify-center sm:pointer-events-none sm:absolute sm:inset-x-0">
          <a
            href={hrefFor("overview")}
            className="pointer-events-auto flex items-center gap-2 rounded-md px-1"
            aria-label="云枢运维平台，回到总览"
          >
            <span
              aria-hidden
              className="inline-flex size-6 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground"
            >
              <Cloud className="size-3.5" />
            </span>
            <span aria-hidden className="hidden text-[0.84375rem] font-semibold tracking-tight sm:inline">
              云枢
            </span>
            <span aria-hidden className="hidden text-xs text-muted-foreground sm:inline">
              · 运维平台
            </span>
          </a>
        </div>
        <div className="relative ml-auto flex items-center gap-1">
          <ConnectionPill />
          <AssistantButton className={iconButton} />
          <AppearanceMenu className={iconButton} />
          {user ? (
            <>
              <div className="hidden min-w-0 border-l pl-2 text-right leading-tight lg:block">
                <div className="max-w-40 truncate text-xs font-medium">{userName}</div>
                <div className="text-[0.6875rem] text-muted-foreground">{ROLE_LABEL[user.role]}</div>
              </div>
              <button
                type="button"
                onClick={() => void logout()}
                className={iconButton}
                aria-label="退出登录"
                title={`退出登录（${userName}，${ROLE_LABEL[user.role]}）`}
              >
                <LogOut aria-hidden className="size-4" />
              </button>
            </>
          ) : null}
        </div>
      </header>
      {DEMO_ROUTES.includes(route) ? (
        <div
          role="note"
          className="flex items-center justify-center gap-2 border-b bg-warn-bg px-4 py-1.5 text-center text-xs font-medium text-foreground"
        >
          <FlaskConical aria-hidden className="size-3.5 shrink-0 text-warn" />
          演示数据：本页为总览原型，排序仅在本次页面内保留，不创建任务，也不会对设备执行操作。
        </div>
      ) : null}
      <div className="flex min-h-0 flex-1">
        <nav
          data-shell="sidebar"
          aria-label="主导航"
          className={cn(
            // 桌面：侧栏 200px 半透明毛玻璃，不随内容滚动，只有右侧内容区滚动；菜单比可用高度高时侧栏自身滚动。窄屏：抽屉
            "fixed inset-y-0 left-0 z-40 w-[200px] shrink-0 flex-col overflow-y-auto overscroll-contain border-r py-4 md:static md:flex md:h-full md:bg-sidebar md:backdrop-blur-xl md:backdrop-saturate-150 motion-safe:transition-[width] motion-safe:duration-200",
            rail ? "px-2 md:w-14" : "px-3",
            // 抽屉浮在页面内容上，半透明底色会透出下面的文字，改用不透明背景
            menuOpen ? "flex bg-background" : "hidden",
          )}
        >
          <div className="mb-3 flex items-center justify-between pl-2 md:hidden">
            <span className="truncate text-[0.9375rem] font-semibold tracking-tight">云枢运维平台</span>
            <button type="button" className={iconButton} onClick={() => setMenuOpen(false)} aria-label="关闭导航">
              <X aria-hidden className="size-4" />
            </button>
          </div>
          {NAV_GROUPS.map((group, gi) => (
            <div key={group.title} className={cn(gi > 0 && (rail ? "mt-3 border-t pt-3" : "mt-5"))}>
              {/* 组标题不用标题元素，避免与页面 h1（如“资产管理”）重名；分组名作为列表的无障碍名称 */}
              {rail ? null : (
                <div
                  aria-hidden
                  className="px-2.5 pb-1.5 text-[0.6875rem] font-semibold tracking-[0.05em] text-muted-foreground uppercase"
                >
                  {group.title}
                </div>
              )}
              <ul className="space-y-[3px]" aria-label={group.title}>
                {group.items.map((item) => {
                  const active = item.route === route
                  return (
                    <li key={item.route}>
                      <a
                        href={hrefFor(item.route)}
                        aria-current={active ? "page" : undefined}
                        aria-label={rail ? item.label : undefined}
                        title={rail ? item.label : undefined}
                        className={cn(
                          "flex h-8 items-center gap-2.5 rounded-lg px-2.5 text-[0.8125rem] font-medium",
                          rail && "justify-center px-0",
                          active
                            ? "bg-primary text-primary-foreground"
                            : "text-muted-foreground hover:bg-accent hover:text-foreground",
                        )}
                      >
                        <item.icon aria-hidden className="size-4 shrink-0" />
                        {rail ? null : <span className="truncate">{item.label}</span>}
                      </a>
                    </li>
                  )
                })}
              </ul>
            </div>
          ))}
        </nav>
        {menuOpen ? (
          <div aria-hidden className="fixed inset-0 z-30 bg-black/30 md:hidden" onClick={() => setMenuOpen(false)} />
        ) : null}
        <main id="main" tabIndex={-1} className="min-h-0 min-w-0 flex-1 overflow-y-auto outline-none">
          {children}
        </main>
      </div>
    </div>
  )
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: React.ReactNode
  actions?: React.ReactNode
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-3 px-6 pt-6 pb-4">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description ? <p className="mt-1 max-w-3xl text-[0.8125rem] text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  )
}
