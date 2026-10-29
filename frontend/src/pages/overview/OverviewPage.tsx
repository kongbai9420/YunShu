import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react"

import { Segmented } from "@/components/ui/controls"

const Dashboard = lazy(() => import("@/pages/dashboard/DashboardRglPage").then((m) => ({ default: m.DashboardRglPage })))
const Topology = lazy(() => import("@/pages/topology/TopologyPage").then((m) => ({ default: m.TopologyPage })))
const Resources = lazy(() => import("./ResourcesPage").then((m) => ({ default: m.ResourcesPage })))

type View = "dashboard" | "resources" | "topology"
interface Location { view: View; topologyViewId?: string }
const OPTIONS: { value: View; label: string }[] = [
  { value: "dashboard", label: "仪表盘" }, { value: "resources", label: "资源全景" }, { value: "topology", label: "拓扑视图" },
]

function readLocation(): Location {
  const [path, query] = window.location.hash.slice(1).split("?")
  const params = new URLSearchParams(query)
  const selected = params.get("view")
  const legacy = path.replace(/^\//, "") === "topology"
  const view = legacy ? "topology" : OPTIONS.some((o) => o.value === selected) ? selected as View : "dashboard"
  return { view, topologyViewId: params.get("topology_view") || (legacy && selected && selected !== "topology" ? selected : undefined) }
}

function href(location: Location) {
  const params = new URLSearchParams()
  if (location.view !== "dashboard") params.set("view", location.view)
  if (location.topologyViewId) params.set("topology_view", location.topologyViewId)
  return `#/overview${params.size ? `?${params}` : ""}`
}

/** 统一导航外壳；只复用已存在的正式数据与布局，不加载演示夹具。 */
export function OverviewPage() {
  const [location, setLocation] = useState<Location>(readLocation)
  const current = useRef(location)
  const dirty = useRef(false)
  const onDirtyChange = useCallback((value: boolean) => { dirty.current = value }, [])

  useEffect(() => {
    const sync = () => {
      const path = window.location.hash.slice(1).split("?")[0].replace(/^\//, "")
      if (path !== "overview" && path !== "topology") return
      const next = readLocation()
      const changed = next.view !== current.current.view || next.topologyViewId !== current.current.topologyViewId
      if (changed && dirty.current && !window.confirm("当前视角有未保存的修改。切换后将离开编辑，是否继续？仪表盘和资源排序的草稿可在返回后恢复；拓扑未保存的布局会丢失。")) {
        window.history.replaceState(null, "", href(current.current))
        return
      }
      if (changed) dirty.current = false
      current.current = next
      setLocation(next)
      const canonical = href(next)
      if (window.location.hash !== canonical) {
        window.history.replaceState(null, "", canonical)
        // replaceState 不派发 hashchange，让顶层导航也识别旧拓扑入口的归并。
        window.dispatchEvent(new HashChangeEvent("hashchange"))
      }
    }
    sync()
    window.addEventListener("hashchange", sync)
    return () => window.removeEventListener("hashchange", sync)
  }, [])

  return <div className="flex h-full min-h-0 flex-col">
    <div className="shrink-0 border-b px-4 py-3 sm:px-6">
      <Segmented label="总览类型" value={location.view} options={OPTIONS}
        onChange={(view) => { window.location.hash = href({ ...location, view }).slice(1) }} />
    </div>
    <div role="region" aria-label="总览内容" className="min-h-0 flex-1 overflow-y-auto">
      <Suspense fallback={<p className="p-6 text-[0.8125rem] text-muted-foreground">正在加载视角…</p>}>
        {location.view === "dashboard" ? <Dashboard onDirtyChange={onDirtyChange} /> : null}
        {location.view === "topology" ? <Topology onDirtyChange={onDirtyChange} requestedViewId={location.topologyViewId}
          onViewChange={(id) => { window.location.hash = href({ view: "topology", topologyViewId: id }).slice(1) }} /> : null}
        {location.view === "resources" ? <Resources onDirtyChange={onDirtyChange} /> : null}
      </Suspense>
    </div>
  </div>
}
