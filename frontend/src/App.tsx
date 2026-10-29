import { Suspense, lazy, useEffect, useState } from "react"

import { AppShell } from "@/components/AppShell"
import { api } from "@/lib/api"
import { AuthProvider, useAuth } from "@/lib/auth"
import { AppearanceProvider } from "@/lib/theme"
import { useHashRoute, type RouteId } from "@/lib/router"
import { ChangePasswordPage, LoginPage } from "@/pages/LoginPage"
import { SetupPage } from "@/pages/SetupPage"

// 每个页面单独分包：网格库、React Flow、ELK 只在打开对应页面时加载
const OverviewPage = lazy(() =>
  import("@/pages/overview/OverviewPage").then((m) => ({ default: m.OverviewPage })),
)
const OverviewPrototypePage = lazy(() =>
  import("@/pages/overview-prototype/OverviewPrototypePage").then((m) => ({ default: m.OverviewPrototypePage })),
)
const AssetsPage = lazy(() => import("@/pages/assets/AssetsPage").then((m) => ({ default: m.AssetsPage })))
const ImportPage = lazy(() => import("@/pages/import/ImportPage").then((m) => ({ default: m.ImportPage })))
const InspectionPage = lazy(() =>
  import("@/pages/inspection/InspectionPage").then((m) => ({ default: m.InspectionPage })),
)
const VulnsPage = lazy(() => import("@/pages/vulns/VulnsPage").then((m) => ({ default: m.VulnsPage })))
const RacksPage = lazy(() => import("@/pages/racks/RacksPage").then((m) => ({ default: m.RacksPage })))
const SettingsPage = lazy(() => import("@/pages/settings/SettingsPage").then((m) => ({ default: m.SettingsPage })))
const MonitorPage = lazy(() => import("@/pages/monitor/MonitorPage").then((m) => ({ default: m.MonitorPage })))
const ServerEventsPage = lazy(() =>
  import("@/pages/server-events/ServerEventsPage").then((m) => ({ default: m.ServerEventsPage })),
)
const AlertsPage = lazy(() => import("@/pages/alerts/AlertsPage").then((m) => ({ default: m.AlertsPage })))
const OperationsPage = lazy(() =>
  import("@/pages/operations/OperationsPage").then((m) => ({ default: m.OperationsPage })),
)

const TITLES: Record<RouteId, string> = {
  overview: "总览",
  "overview-prototype": "总览原型",
  assets: "资产管理",
  import: "批量导入",
  racks: "机柜与定位",
  topology: "拓扑",
  monitoring: "监控中心",
  alerts: "告警中心",
  "server-events": "服务器告警",
  reports: "巡检",
  inspection: "巡检",
  vulns: "漏洞情报",
  operations: "运维操作",
  settings: "平台设置",
}

function Page({ route }: { route: RouteId }) {
  switch (route) {
    case "overview":
    case "topology":
      return <OverviewPage />
    case "overview-prototype":
      return <OverviewPrototypePage />
    case "assets":
      return <AssetsPage />
    case "import":
      return <ImportPage />
    case "racks":
      return <RacksPage />
    case "monitoring":
      return <MonitorPage />
    case "alerts":
      return <AlertsPage />
    case "server-events":
      return <ServerEventsPage />
    case "reports":
      // 旧链接：直接打开“巡检”板块里的每日巡检报告
      return <InspectionPage initialTab="reports" />
    case "inspection":
      return <InspectionPage />
    case "vulns":
      return <VulnsPage />
    case "operations":
      return <OperationsPage />
    case "settings":
      return <SettingsPage />
  }
}

function Shell() {
  const route = useHashRoute()
  const { status, user } = useAuth()

  useEffect(() => {
    document.title = `${TITLES[route]} · 云枢运维平台`
  }, [route])

  if (status === "loading") return <p className="px-6 pt-6 text-[0.8125rem] text-muted-foreground">正在加载…</p>
  if (status !== "ready" || !user) return <LoginPage />
  if (user.require_password_change) return <ChangePasswordPage forced />

  return (
    <AppShell route={route === "topology" ? "overview" : route}>
      <Suspense fallback={<p className="px-6 pt-6 text-[0.8125rem] text-muted-foreground">正在加载…</p>}>
        <Page key={route === "topology" ? "overview" : route} route={route} />
      </Suspense>
    </AppShell>
  )
}

function App() {
  // 全新安装先走首次启动向导（4.1 P12）；查询失败时按正常流程，由登录页提示连接问题
  const [setup, setSetup] = useState<{ pending: boolean; token_required?: boolean } | null>(null)
  useEffect(() => {
    api.get<{ pending: boolean; token_required?: boolean }>("/api/setup/status").then(setSetup, () => setSetup({ pending: false }))
  }, [])
  if (setup === null) return <p className="px-6 pt-6 text-[0.8125rem] text-muted-foreground">正在加载…</p>
  if (setup.pending) return <SetupPage tokenRequired={!!setup.token_required} onDone={() => setSetup({ pending: false })} />
  return (
    <AuthProvider>
      <AppearanceProvider>
        <Shell />
      </AppearanceProvider>
    </AuthProvider>
  )
}

export default App
