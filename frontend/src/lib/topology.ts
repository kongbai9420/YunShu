/** 拓扑接口（backend/app/api/routes_topology.py）。三层分存：链路、视图布局、资产各自独立。 */

import { api, query } from "@/lib/api"

export interface LayoutPos {
  x: number
  y: number
  locked: boolean
}

export interface TopoView {
  id: string
  name: string
  kind: "home" | "datacenter"
  site_id: string | null
  layout: Record<string, LayoutPos>
  hidden: string[]
  revision: number
  updated_by: string
  updated_at: string
}

export interface TopoNode {
  id: string
  name: string
  form: string
  role: string
  platform: string
  lifecycle: string
  site_id: string | null
  site_name: string | null
  rack_id: string | null
  rack_name: string | null
  rack_unit: number | null
}

export interface LldpObservation {
  reporter_asset_id: string
  neighbor_asset_id: string
  local_port: string
  remote_name: string
  remote_port: string
  remote_chassis: string
}

export interface LinkEvidence {
  source?: string
  observations?: LldpObservation[]
  observed_at?: string | null
}

export interface TopoLink {
  id: string
  source_asset_id: string
  target_asset_id: string
  source_port: string
  target_port: string
  kind: string
  origin: "discovered" | "confirmed" | "manual"
  state: "candidate" | "confirmed" | "rejected"
  evidence: LinkEvidence
  locked: boolean
  notes: string
  last_seen_at: string | null
  created_at: string
  updated_at: string
}

export interface TopoRelation {
  id: string
  source_asset_id: string
  target_asset_id: string
  kind: string
  origin: string
}

export interface TopoGraph {
  nodes: TopoNode[]
  links: TopoLink[]
  relations: TopoRelation[]
}

interface DiffItem {
  source_asset_id: string
  target_asset_id: string
  source_name: string
  target_name: string
  source_port: string
  target_port: string
  evidence: LinkEvidence
}

export interface RescanPreview {
  token: string
  added: (DiffItem & { fingerprint: string })[]
  refreshed: { link: DiffItem & { id: string }; proposal: DiffItem & { fingerprint: string } }[]
  removed: (DiffItem & { id: string })[]
  unchanged: number
  kept_unreadable: number
  skipped_rejected: number
  skipped_covered: number
  protected: number
  readable_reporters: number
  unmatched: { reporter_asset_id: string; reporter_name: string; local_port: string; remote_name: string; reason: string }[]
}

export interface ManualLinkBody {
  source_asset_id: string
  target_asset_id: string
  source_port?: string
  target_port?: string
  kind?: string
  notes?: string
}

export const LINK_KIND_LABEL: Record<string, string> = {
  ethernet: "以太网",
  fiber: "光纤",
  wifi: "无线",
  virtual: "虚拟",
  other: "其他",
}

export const VIEW_KIND_LABEL: Record<string, string> = { home: "家用", datacenter: "机房" }

export const topologyApi = {
  views: () => api.get<TopoView[]>("/api/topology/views"),
  view: (id: string) => api.get<TopoView>(`/api/topology/views/${id}`),
  saveView: (id: string, body: { revision: number; layout?: Record<string, LayoutPos>; hidden?: string[] }) =>
    api.patch<TopoView>(`/api/topology/views/${id}`, body),
  graph: (viewId: string) => api.get<TopoGraph>(`/api/topology/graph${query({ view_id: viewId })}`),
  createLink: (body: ManualLinkBody) => api.post<TopoLink>("/api/topology/links", body),
  updateLink: (id: string, body: Partial<ManualLinkBody>) => api.patch<TopoLink>(`/api/topology/links/${id}`, body),
  confirmLink: (id: string) => api.post<TopoLink>(`/api/topology/links/${id}/confirm`),
  rejectLink: (id: string) => api.post<TopoLink>(`/api/topology/links/${id}/reject`),
  deleteLink: (id: string) => api.del(`/api/topology/links/${id}`),
  preview: () => api.post<RescanPreview>("/api/topology/rescan/preview"),
  apply: (token: string) =>
    api.post<{ added: number; refreshed: number; unchanged: number; removed: number; protected: number }>(
      "/api/topology/rescan/apply",
      { token },
    ),
}
