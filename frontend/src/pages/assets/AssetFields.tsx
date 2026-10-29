import { Select } from "@/components/ui/controls"
import { TextField } from "@/components/ui/form"
import { useEffect, useState } from "react"

import { api, query, type Asset, type Site } from "@/lib/api"
import { FORM_LABEL, LIFECYCLE_LABEL, ROLE_LABEL, optionsFrom } from "@/lib/vocab"
import { localInput, type Profile, usePoll, utcDate } from "@/lib/monitor"

export interface AssetDraft {
  asset_no: string
  name: string
  form: string
  role: string
  platform: string
  vendor: string
  model: string
  serial_no: string
  lifecycle: string
  // 维护中的预计结束时间（本地时间，datetime-local 格式）
  maintenance_until: string
  site_id: string
  rack_id: string
  rack_unit: string
  rack_face: string
  device_type_id: string
  tags: string
  notes: string
  collection_profile: string
}

export const EMPTY_DRAFT: AssetDraft = {
  asset_no: "",
  name: "",
  form: "physical",
  role: "server",
  platform: "",
  vendor: "",
  model: "",
  serial_no: "",
  lifecycle: "active",
  maintenance_until: "",
  site_id: "",
  rack_id: "",
  rack_unit: "",
  rack_face: "front",
  device_type_id: "",
  tags: "",
  notes: "",
  collection_profile: "",
}

export function draftOf(a: Asset): AssetDraft {
  return {
    asset_no: a.asset_no ?? "",
    name: a.name,
    form: a.form,
    role: a.role,
    platform: a.platform,
    vendor: a.vendor,
    model: a.model,
    serial_no: a.serial_no,
    lifecycle: a.lifecycle,
    maintenance_until: a.maintenance_until ? localInput(utcDate(a.maintenance_until)) : "",
    site_id: a.site_id ?? "",
    rack_id: a.rack_id ?? "",
    rack_unit: a.rack_unit != null ? String(a.rack_unit) : "",
    rack_face: a.rack_face ?? "front",
    device_type_id: a.device_type_id ?? "",
    tags: a.tags.join("，"),
    notes: a.notes,
    collection_profile: a.collection_profile,
  }
}

/** 路径变化时取一次（不轮询）；path 为 null 时不请求 */
function useFetch<T>(path: string | null): { data: T | null } {
  const [data, setData] = useState<T | null>(null)
  useEffect(() => {
    let stopped = false
    setData(null)
    if (path) void api.get<T>(path).then((v) => { if (!stopped) setData(v) }, () => undefined)
    return () => { stopped = true }
  }, [path])
  return { data }
}

export function splitTags(text: string): string[] {
  return Array.from(
    new Set(
      text
        .split(/[,，;；、]/)
        .map((t) => t.trim())
        .filter(Boolean),
    ),
  )
}

/** 草稿转成接口字段；只返回与 base 不同的字段（base 为空表示新建，全部返回） */
export function payloadOf(d: AssetDraft, base?: AssetDraft): Record<string, unknown> {
  const full: Record<string, unknown> = {
    asset_no: d.asset_no.trim() || null,
    name: d.name.trim(),
    form: d.form,
    role: d.role,
    platform: d.platform.trim(),
    vendor: d.vendor.trim(),
    model: d.model.trim(),
    serial_no: d.serial_no.trim(),
    lifecycle: d.lifecycle,
    maintenance_until: d.lifecycle === "maintenance" && d.maintenance_until ? new Date(d.maintenance_until).toISOString() : null,
    site_id: d.site_id || null,
    rack_id: d.site_id && d.rack_id ? d.rack_id : null,
    rack_unit: d.site_id && d.rack_id && d.rack_unit.trim() ? Number(d.rack_unit) : null,
    rack_face: d.rack_face || "front",
    device_type_id: d.device_type_id || null,
    tags: splitTags(d.tags),
    notes: d.notes,
    collection_profile: d.collection_profile.trim(),
  }
  if (!base) return full
  const prev = payloadOf(base)
  return Object.fromEntries(Object.entries(full).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(prev[k])))
}

export function AssetFields({
  draft,
  onChange,
  sites,
  idPrefix,
  compact = false,
}: {
  draft: AssetDraft
  onChange: (d: AssetDraft) => void
  sites: Site[]
  idPrefix: string
  compact?: boolean
}) {
  const set = (k: keyof AssetDraft) => (v: string) => onChange({ ...draft, [k]: v })
  const profiles = usePoll<Profile[]>("/api/collection/profiles", 60000)
  // 机柜按所选场所过滤；型号按厂商筛选，最多列 200 个（4.2 P14）
  const racks = useFetch<{ id: string; name: string; height_u: number }[]>(draft.site_id ? `/api/racks${query({ site_id: draft.site_id })}` : null)
  const types = useFetch<{ items: { id: string; manufacturer: string; model: string; u_height: number }[] }>(`/api/device-types${query({ q: draft.vendor.trim() || undefined, limit: 200 })}`)
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <TextField
        id={`${idPrefix}-name`}
        label="名称"
        plain
        value={draft.name}
        onChange={set("name")}
        required
        maxLength={200}
      />
      <Select
        id={`${idPrefix}-form`}
        label="形态"
        help="asset.form"
        value={draft.form}
        onChange={set("form")}
        options={optionsFrom(FORM_LABEL)}
      />
      <Select
        id={`${idPrefix}-role`}
        label="角色"
        help="asset.role"
        value={draft.role}
        onChange={set("role")}
        options={optionsFrom(ROLE_LABEL)}
      />
      <TextField
        id={`${idPrefix}-platform`}
        label="平台"
        value={draft.platform}
        onChange={set("platform")}
        maxLength={64}
        hint="如 debian、proxmox、openwrt、dell-idrac"
      />
      <Select
        id={`${idPrefix}-life`}
        label="生命周期"
        help="asset.lifecycle"
        value={draft.lifecycle}
        onChange={(v) =>
          // 改为维护中时预计结束默认 7 天后
          onChange({ ...draft, lifecycle: v, maintenance_until: v === "maintenance" ? draft.maintenance_until || localInput(new Date(Date.now() + 7 * 86400000)) : "" })
        }
        options={optionsFrom(LIFECYCLE_LABEL)}
      />
      {draft.lifecycle === "maintenance" ? (
        <TextField
          id={`${idPrefix}-mu`}
          label="预计结束时间"
          help="asset.maintenanceUntil"
          type="datetime-local"
          value={draft.maintenance_until}
          onChange={set("maintenance_until")}
          required
          hint="过了这个时间还没改回“在用”会提醒；修好后请改回“在用”"
        />
      ) : null}
      <div className="space-y-1">
        <Select
          id={`${idPrefix}-cp`}
          label="采集模板"
          help="asset.profile"
          value={draft.collection_profile}
          onChange={set("collection_profile")}
          options={[
            { value: "", label: "按平台和端点自动选择" },
            ...(profiles.data ?? []).map((p) => ({
              value: p.name,
              label: `${p.label}（${p.batch} 批）`,
            })),
            ...(!profiles.data?.some((p) => p.name === draft.collection_profile) && draft.collection_profile
              ? [
                  {
                    value: draft.collection_profile,
                    label: `${draft.collection_profile}（原设置）`,
                  },
                ]
              : []),
          ]}
        />
        {profiles.error ? (
          <p role="alert" className="text-xs text-bad">
            模板列表加载失败，请刷新后重试。
          </p>
        ) : null}
      </div>
      <details open={compact ? undefined : true} className="min-w-0 sm:col-span-2" key={String(compact)}>
        <summary className={compact ? "cursor-pointer text-xs font-medium text-muted-foreground" : "hidden"}>
          更多资产信息（可选）
        </summary>
        <div className="grid min-w-0 grid-cols-1 gap-3 pt-3 sm:grid-cols-2">
          <TextField
            id={`${idPrefix}-no`}
            label="资产编号"
            help="asset.assetNo"
            value={draft.asset_no}
            onChange={set("asset_no")}
            maxLength={100}
            hint="模板导入以它去重；可留空"
          />
          <TextField
            id={`${idPrefix}-vendor`}
            label="品牌"
            hint="也用于筛选下方的设备型号"
            value={draft.vendor}
            onChange={set("vendor")}
            maxLength={100}
          />
          <TextField
            id={`${idPrefix}-model`}
            label="型号"
            plain
            value={draft.model}
            onChange={set("model")}
            maxLength={100}
          />
          <TextField
            id={`${idPrefix}-sn`}
            label="序列号"
            hint="厂商服务标签，报修时使用"
            value={draft.serial_no}
            onChange={set("serial_no")}
            maxLength={100}
          />
          <Select
            id={`${idPrefix}-site`}
            label="场所"
            hint="资产范围授权按场所划分"
            value={draft.site_id}
            onChange={set("site_id")}
            options={[{ value: "", label: "未指定" }, ...sites.map((s) => ({ value: s.id, label: s.name }))]}
          />
          <Select
            id={`${idPrefix}-rack`}
            label="机柜"
            hint="只列出所选场所下的机柜"
            value={draft.rack_id}
            onChange={set("rack_id")}
            disabled={!draft.site_id}
            options={[{ value: "", label: draft.site_id ? "不放入机柜" : "先选场所" }, ...(racks.data ?? []).map((r) => ({ value: r.id, label: `${r.name}（${r.height_u}U）` }))]}
          />
          <TextField
            id={`${idPrefix}-unit`}
            label="起始 U 位"
            value={draft.rack_unit}
            onChange={set("rack_unit")}
            inputMode="numeric"
            disabled={!draft.rack_id}
            hint="设备最下面一个 U 的编号"
            help="asset.rackUnit"
          />
          <Select
            id={`${idPrefix}-type`}
            label="设备型号"
            help="asset.deviceType"
            value={draft.device_type_id}
            onChange={set("device_type_id")}
            options={[{ value: "", label: "未指定（按 1U 全深）" }, ...(types.data?.items ?? []).map((t) => ({ value: t.id, label: `${t.manufacturer} ${t.model}（${t.u_height}U）` }))]}
          />
          <Select
            id={`${idPrefix}-face`}
            label="安装面"
            help="asset.rackFace"
            value={draft.rack_face}
            onChange={set("rack_face")}
            disabled={!draft.rack_id}
            options={[{ value: "front", label: "正面" }, { value: "rear", label: "背面" }]}
          />
          <TextField
            id={`${idPrefix}-tags`}
            label="标签"
            value={draft.tags}
            onChange={set("tags")}
            hint="多个标签用逗号分隔"
          />
          <TextField
            id={`${idPrefix}-notes`}
            label="备注"
            plain
            value={draft.notes}
            onChange={set("notes")}
            multiline
            className="sm:col-span-2"
          />
        </div>
      </details>
    </div>
  )
}
