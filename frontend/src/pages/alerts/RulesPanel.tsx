import { useState, type FormEvent } from "react"
import { Button } from "@/components/ui/button"
import { Card, InlineNotice, Select } from "@/components/ui/controls"
import { api } from "@/lib/api"
import { usePoll } from "@/lib/monitor"
import {
  AssetChoices,
  type Rule,
  type Channel,
  Check,
  ConfirmButton,
  Field,
  Input,
  severityLabels,
  split,
  useMutation,
} from "./shared"

const emptyRule: Rule = {
  id: "",
  name: "",
  enabled: true,
  kind: "threshold",
  metric_key: "cpu.load",
  comparison: "gte",
  threshold: 90,
  recovery_threshold: 80,
  consecutive_samples: 3,
  duration_seconds: 0,
  cooldown_seconds: 300,
  repeat_seconds: 0,
  severity: "warning",
  asset_ids: [],
  tags: [],
  channel_ids: [],
  suppress_by_dependency: true,
  revision: 1,
}
export function RulesPanel({ editable }: { editable: boolean }) {
  const rules = usePoll<Rule[]>("/api/alerts/rules", 15000)
  const [draft, setDraft] = useState<Rule | null>(null),
    mut = useMutation()
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="flex-1 text-sm text-muted-foreground">
          阈值规则使用新鲜有效样本；可达性规则使用明确探测结果。过期、未配置与估算值不会直接触发阈值告警。
        </p>
        {editable ? <Button onClick={() => setDraft({ ...emptyRule })}>新增告警规则</Button> : null}
      </div>
      {rules.error ? (
        <InlineNotice tone="bad" role="alert">
          {rules.error}
        </InlineNotice>
      ) : null}
      {mut.feedback}
      {draft ? (
        <RuleEditor
          key={draft.id || "new"}
          initial={draft}
          saved={() => {
            setDraft(null)
            rules.refresh()
          }}
          cancel={() => setDraft(null)}
        />
      ) : null}
      {!rules.data ? (
        <p className="text-sm">正在加载规则…</p>
      ) : rules.data.length === 0 ? (
        <InlineNotice>暂无告警规则。新增规则后，后台会按条件判断后续采集样本。</InlineNotice>
      ) : (
        <div className="grid min-w-0 grid-cols-1 gap-3 xl:grid-cols-2">
          {rules.data.map((r) => (
            <Card key={r.id} title={r.name}>
              <div className="space-y-2 text-sm">
                <p>
                  {r.enabled ? "已启用" : "已停用"} · {severityLabels[r.severity]} ·{" "}
                  {r.kind === "availability"
                    ? "明确可达性失败"
                    : `${r.metric_key} ${r.comparison === "gte" ? "≥" : "≤"} ${r.threshold}；恢复 ${r.recovery_threshold}`}
                </p>
                <p className="text-xs text-muted-foreground">
                  连续 {r.consecutive_samples} 个样本 · 持续 {r.duration_seconds} 秒 · 冷却 {r.cooldown_seconds} 秒 ·{" "}
                  {r.repeat_seconds ? `每 ${r.repeat_seconds} 秒重复` : "不重复提醒"}
                </p>
                <p className="text-xs">
                  {r.asset_ids.length ? `${r.asset_ids.length} 台指定资产` : "全部在用资产"}
                  {r.tags.length ? `；标签 ${r.tags.join("、")}` : ""} ·{" "}
                  {r.channel_ids.length ? `${r.channel_ids.length} 个指定渠道` : "匹配全部已启用渠道"}
                  {r.suppress_by_dependency ? " · 依赖故障抑制" : ""}
                </p>
                {editable ? (
                  <div className="flex flex-wrap gap-2">
                    <Button size="sm" variant="outline" onClick={() => setDraft({ ...r })}>
                      编辑 {r.name}
                    </Button>
                    <ConfirmButton
                      label="删除规则"
                      message={`删除“${r.name}”后不再判断新样本，历史事件保留。`}
                      disabled={mut.busy}
                      onConfirm={async () => {
                        await mut.run(async () => {
                          await api.del(`/api/alerts/rules/${r.id}`)
                          rules.refresh()
                        }, "规则已删除")
                      }}
                    />
                  </div>
                ) : null}
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
function RuleEditor({ initial, saved, cancel }: { initial: Rule; saved: () => void; cancel: () => void }) {
  const [draft, setDraft] = useState(initial),
    [tags, setTags] = useState(initial.tags.join(", ")),
    channels = usePoll<Channel[]>("/api/alerts/channels", 60000),
    mut = useMutation()
  function patch<K extends keyof Rule>(key: K, value: Rule[K]) {
    setDraft((d) => ({ ...d, [key]: value }))
  }
  async function save(e: FormEvent) {
    e.preventDefault()
    const { id, revision, ...fields } = draft
    const payload = {
      ...fields,
      ...(id ? { revision } : {}),
      tags: split(tags),
      metric_key: draft.kind === "availability" ? "availability" : draft.metric_key.trim(),
    }
    if (
      await mut.run(() =>
        draft.id ? api.patch(`/api/alerts/rules/${draft.id}`, payload) : api.post("/api/alerts/rules", payload),
      )
    )
      saved()
  }
  return (
    <Card title={draft.id ? "编辑告警规则" : "新建告警规则"}>
      <form onSubmit={(e) => void save(e)} className="space-y-4">
        <fieldset disabled={mut.busy} className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <Field label="规则名称" plain>
            <Input required maxLength={100} value={draft.name} onChange={(e) => patch("name", e.target.value)} />
          </Field>
          <Select
            id="rule-kind"
            label="规则类型"
            help="alert.rule.kind"
            value={draft.kind}
            onChange={(v) =>
              setDraft((d) => ({
                ...d,
                kind: v as Rule["kind"],
                ...(v === "availability"
                  ? { metric_key: "availability", threshold: 1, recovery_threshold: 0 }
                  : { metric_key: "cpu.load", threshold: 90, recovery_threshold: 80 }),
              }))
            }
            options={[
              { value: "threshold", label: "指标阈值" },
              { value: "availability", label: "可达性失败" },
            ]}
          />
          <Select
            id="rule-severity"
            label="告警级别"
            help="alert.rule.severity"
            value={draft.severity}
            onChange={(v) => patch("severity", v as Rule["severity"])}
            options={Object.entries(severityLabels).map(([value, label]) => ({
              value,
              label,
            }))}
          />
          {draft.kind === "threshold" ? (
            <>
              <Field label="指标键（支持 * 通配）" help="alert.rule.metric">
                <Input
                  required
                  maxLength={100}
                  value={draft.metric_key}
                  onChange={(e) => patch("metric_key", e.target.value)}
                  list="alert-metric-keys"
                />
                <datalist id="alert-metric-keys">
                  {["cpu.load", "mem.used_pct", "disk.used_pct", "sensor.temp*", "power.watts"].map((k) => (
                    <option key={k} value={k} />
                  ))}
                </datalist>
              </Field>
              <Select
                id="rule-comparison"
                label="触发方向"
                help="alert.rule.direction"
                value={draft.comparison}
                onChange={(v) => patch("comparison", v as Rule["comparison"])}
                options={[
                  { value: "gte", label: "大于等于阈值" },
                  { value: "lte", label: "小于等于阈值" },
                ]}
              />
              <Field label="触发阈值" help="alert.rule.threshold">
                <Input
                  required
                  type="number"
                  step="any"
                  value={draft.threshold}
                  onChange={(e) => patch("threshold", Number(e.target.value))}
                />
              </Field>
              <Field label="恢复阈值" help="alert.rule.recovery">
                <Input
                  required
                  type="number"
                  step="any"
                  value={draft.recovery_threshold}
                  onChange={(e) => patch("recovery_threshold", Number(e.target.value))}
                />
              </Field>
            </>
          ) : null}
          {(
            [
              ["consecutive_samples", "连续样本数", 1, 100, "alert.rule.samples"],
              ["duration_seconds", "持续时间（秒）", 0, 86400, "alert.rule.duration"],
              ["cooldown_seconds", "冷却时间（秒）", 0, 86400, "alert.rule.cooldown"],
              ["repeat_seconds", "重复提醒（秒，0 为关闭）", 0, 86400, "alert.rule.repeat"],
            ] as const
          ).map(([key, label, min, max, help]) => (
            <Field key={key} label={label} help={help}>
              <Input
                required
                type="number"
                min={min}
                max={max}
                step={1}
                value={draft[key]}
                onChange={(e) => patch(key, Number(e.target.value))}
              />
            </Field>
          ))}
          <Field label="资产标签（逗号分隔）" help="alert.rule.tags">
            <Input maxLength={2000} value={tags} onChange={(e) => setTags(e.target.value)} />
          </Field>
          <Check label="启用规则" hint="停用后不再产生新告警，已有告警按规则变更关闭" checked={draft.enabled} onChange={(v) => patch("enabled", v)} />
          <Check
            label="上游依赖故障时抑制下游通知"
            help="alert.rule.suppress"
            checked={draft.suppress_by_dependency}
            onChange={(v) => patch("suppress_by_dependency", v)}
          />
        </fieldset>
        <details>
          <summary className="cursor-pointer text-sm text-link">选择资产与通知渠道（可选）</summary>
          <div className="mt-3 grid min-w-0 gap-4 sm:grid-cols-2">
            <fieldset disabled={mut.busy} className="min-w-0">
              <legend className="mb-1 text-sm font-medium">限定资产</legend>
              <AssetChoices value={draft.asset_ids} onChange={(ids) => patch("asset_ids", ids)} disabled={mut.busy} />
            </fieldset>
            <fieldset disabled={mut.busy} className="min-w-0">
              <legend className="mb-1 text-sm font-medium">通知渠道</legend>
              <p className="text-xs text-muted-foreground">不选则匹配所有已启用且满足级别、标签条件的渠道。</p>
              {channels.error ? <InlineNotice tone="bad">{channels.error}</InlineNotice> : null}
              {channels.data?.map((c) => (
                <Check
                  key={c.id}
                  plain
                  label={`${c.name}${c.enabled ? "" : "（已停用）"}`}
                  checked={draft.channel_ids.includes(c.id)}
                  onChange={(v) =>
                    patch(
                      "channel_ids",
                      v ? [...draft.channel_ids, c.id] : draft.channel_ids.filter((id) => id !== c.id),
                    )
                  }
                />
              ))}
            </fieldset>
          </div>
        </details>
        <p className="text-xs text-muted-foreground">
          高值告警的恢复阈值应低于触发阈值；低值告警相反，以避免边界值反复触发。
        </p>
        {mut.feedback}
        <div className="flex flex-wrap gap-2">
          <Button disabled={mut.busy} type="submit">
            {mut.busy ? "正在保存…" : "保存规则"}
          </Button>
          <Button type="button" variant="outline" disabled={mut.busy} onClick={cancel}>
            取消编辑
          </Button>
        </div>
      </form>
    </Card>
  )
}
