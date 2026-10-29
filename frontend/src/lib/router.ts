import { useEffect, useState } from "react"

/** 顶层页面不多，用 hash 路由，不引入路由库 */
export type RouteId =
  | "overview"
  | "overview-prototype"
  | "assets"
  | "import"
  | "racks"
  | "topology"
  | "monitoring"
  | "alerts"
  | "server-events"
  | "reports"
  | "inspection"
  | "vulns"
  | "operations"
  | "settings"

const ROUTES: RouteId[] = [
  "overview",
  "overview-prototype",
  "assets",
  "import",
  "racks",
  "topology",
  "monitoring",
  "alerts",
  "server-events",
  "reports",
  "inspection",
  "vulns",
  "operations",
  "settings",
]

function parse(hash: string): RouteId {
  const id = hash.replace(/^#\/?/, "").split("?")[0] as RouteId
  // 总览在 P3 前仍是模拟数据的原型，默认先打开真实的资产管理
  return ROUTES.includes(id) ? id : "assets"
}

export function useHashRoute(): RouteId {
  const [route, setRoute] = useState<RouteId>(() => parse(window.location.hash))
  useEffect(() => {
    const onChange = () => setRoute(parse(window.location.hash))
    window.addEventListener("hashchange", onChange)
    return () => window.removeEventListener("hashchange", onChange)
  }, [])
  return route
}

export function hrefFor(route: RouteId): string {
  return `#/${route}`
}
