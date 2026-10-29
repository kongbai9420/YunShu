/**
 * 后端接口客户端。会话用 HttpOnly Cookie；改数据的请求带 X-CSRF-Token（值来自 yunshu_csrf Cookie）。
 * 错误统一抛 ApiError，detail 是后端返回的中文说明。
 */

export class ApiError extends Error {
  status: number
  /** 后端 detail 原样保留，如 409 冲突时带回的当前版本 */
  detail: unknown
  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)yunshu_csrf=([^;]+)/)
  return m ? decodeURIComponent(m[1]) : ""
}

function detailOf(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail
    if (typeof d === "string") return d
    if (d && typeof d === "object" && "message" in d && typeof (d as { message: unknown }).message === "string") {
      return (d as { message: string }).message
    }
    // FastAPI 校验错误：取第一条
    if (Array.isArray(d) && d[0] && typeof d[0] === "object" && "msg" in d[0]) {
      const loc = Array.isArray(d[0].loc) ? d[0].loc.slice(1).join(".") : ""
      return `输入不正确${loc ? `（${loc}）` : ""}：${String(d[0].msg)}`
    }
  }
  if (status === 0) return "无法连接后端，请确认服务已启动"
  return `请求失败（${status}）`
}

type Listener = () => void
const unauthorizedListeners = new Set<Listener>()

/** 会话过期时通知登录状态，回到登录页 */
export function onUnauthorized(fn: Listener): () => void {
  unauthorizedListeners.add(fn)
  return () => unauthorizedListeners.delete(fn)
}

export async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  opts: { form?: FormData; quiet401?: boolean } = {},
): Promise<T> {
  const headers: Record<string, string> = {}
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken()
  let payload: BodyInit | undefined
  if (opts.form) payload = opts.form
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json"
    payload = JSON.stringify(body)
  }
  let res: Response
  try {
    res = await fetch(path, { method, headers, body: payload, credentials: "same-origin" })
  } catch {
    throw new ApiError(0, detailOf(null, 0))
  }
  if (res.status === 204) return undefined as T
  const text = await res.text()
  let data: unknown = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = null
  }
  if (!res.ok) {
    if (res.status === 401 && !opts.quiet401) unauthorizedListeners.forEach((fn) => fn())
    throw new ApiError(res.status, detailOf(data, res.status), data && typeof data === "object" ? (data as { detail?: unknown }).detail : undefined)
  }
  return data as T
}

export const api = {
  get: <T>(path: string, opts?: { quiet401?: boolean }) => request<T>("GET", path, undefined, opts),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  put: <T>(path: string, body: unknown) => request<T>("PUT", path, body),
  del: (path: string) => request<void>("DELETE", path),
  upload: <T>(path: string, form: FormData) => request<T>("POST", path, undefined, { form }),
}

export function query(params: Record<string, string | number | undefined | null>): string {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v))
  const s = q.toString()
  return s ? `?${s}` : ""
}

// ---- 接口类型（与 backend/app/schemas.py 对应） ----

export type UserRole = "admin" | "operator" | "viewer"

export interface User {
  id: string
  username: string
  display_name: string
  role: UserRole
  is_active: boolean
  require_password_change: boolean
  last_login_at: string | null
  /** 资产范围（4.1 P11）：false 表示只能看到授权的场所与资产；管理员始终全部可见 */
  scope_all?: boolean
}

export interface Endpoint {
  id: string
  asset_id: string
  address: string
  port: number | null
  protocol: string
  purpose: string
  credential_id: string | null
  tls_verify: boolean
  notes: string
  /** 4.3 P18b：每天读取已安装软件包清单（仅 SSH） */
  collect_packages: boolean
  tls_fingerprint: string
  ssh_host_key: string
  trust_pinned_at: string | null
}

export interface Asset {
  id: string
  asset_no: string | null
  name: string
  form: string
  role: string
  platform: string
  vendor: string
  model: string
  serial_no: string
  collection_profile: string
  lifecycle: string
  maintenance_until?: string | null
  site_id: string | null
  rack_id: string | null
  rack_unit: number | null
  device_type_id?: string | null
  rack_face?: "front" | "rear"
  tags: string[]
  notes: string
  revision: number
  legacy_source: string | null
  legacy_id: string | null
  created_at: string
  updated_at: string
  endpoints: Endpoint[]
}

export interface Relation {
  id: string
  source_asset_id: string
  target_asset_id: string
  kind: string
  origin: string
}

export interface AssetDetail extends Asset {
  relations: Relation[]
}

export interface Page<T> {
  items: T[]
  total: number
}

export interface Site {
  id: string
  name: string
  kind: string
  parent_id: string | null
  notes: string
}

export interface Credential {
  id: string
  alias: string
  kind: string
  username: string
  notes: string
  key_id: string
  last_used_at: string | null
  updated_at: string
}
