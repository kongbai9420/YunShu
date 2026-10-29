import { useState, type FormEvent } from "react"
import { Badge } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Card, InlineNotice, Select } from "@/components/ui/controls"
import { api, type Page } from "@/lib/api"
import { dateText, usePoll } from "@/lib/monitor"
import {
  CHANNEL_KINDS,
  type Channel,
  type Delivery,
  Check,
  ConfirmButton,
  Field,
  Input,
  inputClass,
  severityLabels,
  split,
  useMutation,
} from "./shared"

const emptyChannel: Channel = {
  id: "",
  name: "",
  kind: "email",
  enabled: false,
  revision: 1,
  has_secret: false,
  config: {
    host: "",
    port: 465,
    tls: "ssl",
    from_address: "",
    recipients: [],
    group_seconds: 30,
    severities: [],
    tags: [],
  },
}
const deliveryLabels: Record<string, string> = {
  queued: "等待投递",
  sending: "投递中",
  retrying: "等待重试",
  accepted: "服务已接受",
  failed: "投递失败",
  uncertain: "结果不确定",
  cancelled: "已取消",
}
export function ChannelsPanel({ editable }: { editable: boolean }) {
  const channels = usePoll<Channel[]>("/api/alerts/channels", 15000),
    deliveries = usePoll<Page<Delivery>>("/api/alerts/deliveries?limit=100"),
    [draft, setDraft] = useState<Channel | null>(null),
    mut = useMutation()
  return (
    <div className="min-w-0 space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="flex-1 text-sm text-muted-foreground">
          通知渠道默认停用；保存并核对目标后再启用。测试通知会向已保存的目标实际发送。
        </p>
        {editable ? (
          <Button onClick={() => setDraft({ ...emptyChannel, config: { ...emptyChannel.config } })}>
            新增通知渠道
          </Button>
        ) : null}
      </div>
      <InlineNotice>
        “服务已接受”只表示 SMTP /
        推送服务已接受请求，不代表收件人已收到。结果不确定时请先核对收件记录，重试可能产生重复消息。
        渠道和路由配置从后续告警事件生效；持续告警可在规则中开启重复提醒。
      </InlineNotice>
      {channels.error ? (
        <InlineNotice role="alert" tone="bad">
          {channels.error}
        </InlineNotice>
      ) : null}
      {mut.feedback}
      {draft ? (
        <ChannelEditor
          key={draft.id || "new"}
          initial={draft}
          saved={() => {
            setDraft(null)
            channels.refresh()
          }}
          cancel={() => setDraft(null)}
        />
      ) : null}
      <div className="grid min-w-0 grid-cols-1 gap-3 xl:grid-cols-2">
        {channels.data?.map((c) => (
          <Card key={c.id} title={c.name}>
            <div className="space-y-2 text-sm">
              <p>
                {CHANNEL_KINDS[c.kind]} · {c.enabled ? "已启用" : "已停用"} ·{" "}
                {c.has_secret ? "已保存秘密信息" : "未配置秘密信息"}
              </p>
              {c.kind === "email" ? (
                <p className="break-words text-xs">
                  {c.config.host}:{c.config.port} · {c.config.tls === "ssl" ? "TLS" : "STARTTLS"}
                  <br />
                  收件人：{c.config.recipients?.join("、") || "未设置"}
                </p>
              ) : null}
              <p className="text-xs text-muted-foreground">
                级别：
                {c.config.severities?.length
                  ? c.config.severities.map((s) => severityLabels[s as keyof typeof severityLabels] ?? s).join("、")
                  : "全部"}{" "}
                · 标签：{c.config.tags?.join("、") || "不限"} · 合并 {c.config.group_seconds ?? 30} 秒
              </p>
              {editable ? (
                <div className="flex flex-wrap items-start gap-2">
                  <Button size="sm" variant="outline" onClick={() => setDraft({ ...c, config: { ...c.config } })}>
                    编辑 {c.name}
                  </Button>
                  <ConfirmButton
                    label="测试通知"
                    message={`将向“${c.name}”已保存的目标排队发送测试通知；尚未保存的表单内容不会使用。`}
                    disabled={mut.busy}
                    onConfirm={async () => {
                      await mut.run(async () => {
                        await api.post(`/api/alerts/channels/${c.id}/test`, {})
                        deliveries.refresh()
                      }, "测试通知已加入投递队列，请查看投递状态")
                    }}
                  />
                  <ConfirmButton
                    label="删除渠道"
                    message={`删除“${c.name}”后不再向该渠道发送新通知。`}
                    disabled={mut.busy}
                    onConfirm={async () => {
                      await mut.run(async () => {
                        await api.del(`/api/alerts/channels/${c.id}`)
                        channels.refresh()
                        deliveries.refresh()
                      }, "通知渠道已删除")
                    }}
                  />
                </div>
              ) : null}
            </div>
          </Card>
        ))}
      </div>
      {channels.data?.length === 0 ? <InlineNotice>尚未配置通知渠道。告警仍会记录在平台中。</InlineNotice> : null}
      <Card
        title="最近通知投递"
        actions={
          <Button size="sm" variant="outline" onClick={deliveries.refresh}>
            刷新投递
          </Button>
        }
      >
        {deliveries.error ? (
          <InlineNotice role="alert" tone="bad">
            {deliveries.error}
          </InlineNotice>
        ) : null}
        <p className="mb-3 text-xs text-muted-foreground">
          显示最近 100 项，共 {deliveries.data?.total ?? "—"} 项；每 5 秒刷新。
        </p>
        <div className="overflow-x-auto">
          <table className="data-table w-full min-w-[620px]">
            <caption className="sr-only">通知投递记录</caption>
            <thead>
              <tr>
                {["通知渠道", "状态", "时间 / 尝试", "结果说明", "操作"].map((t) => (
                  <th scope="col" key={t}>
                    {t}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {deliveries.data?.items.map((d) => (
                <tr key={d.id}>
                  <th scope="row" className="font-normal">
                    {d.channel_name || "渠道已删除"}
                    {d.is_test ? <p className="text-xs text-muted-foreground">测试通知</p> : null}
                  </th>
                  <td>
                    <Badge
                      tone={
                        d.status === "accepted" ? "ok" : ["failed", "uncertain"].includes(d.status) ? "bad" : "neutral"
                      }
                    >
                      {deliveryLabels[d.status] ?? d.status}
                    </Badge>
                  </td>
                  <td className="text-xs">
                    {dateText(d.created_at)}
                    <p>尝试 {d.attempts} 次</p>
                  </td>
                  <td className="max-w-72 break-words text-xs">
                    {d.error || (d.status === "accepted" ? "服务已接受请求，实际送达请核对收件端" : "—")}
                  </td>
                  <td>
                    {editable && ["failed", "uncertain"].includes(d.status) ? (
                      <ConfirmButton
                        label="重试投递"
                        message={
                          d.status === "uncertain"
                            ? "请先核对收件端。确认重试可能再次发送同一消息。"
                            : "按已保存目标重新排队这条通知？"
                        }
                        disabled={mut.busy}
                        onConfirm={async () => {
                          await mut.run(async () => {
                            await api.post(
                              `/api/alerts/deliveries/${d.id}/retry`,
                              d.status === "uncertain" ? { confirm_uncertain: true } : {},
                            )
                            deliveries.refresh()
                          }, "已重新加入投递队列")
                        }}
                      />
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {deliveries.data?.items.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">暂无投递记录。</p>
          ) : null}
        </div>
      </Card>
    </div>
  )
}
function ChannelEditor({ initial, saved, cancel }: { initial: Channel; saved: () => void; cancel: () => void }) {
  const [draft, setDraft] = useState(initial),
    [recipients, setRecipients] = useState(initial.config.recipients?.join(", ") ?? ""),
    [tags, setTags] = useState(initial.config.tags?.join(", ") ?? ""),
    [url, setUrl] = useState(""),
    [secret, setSecret] = useState(""),
    [username, setUsername] = useState(""),
    [password, setPassword] = useState(""),
    mut = useMutation()
  function config(key: keyof Channel["config"], value: unknown) {
    setDraft((d) => ({ ...d, config: { ...d.config, [key]: value } }))
  }
  async function save(e: FormEvent) {
    e.preventDefault()
    const secrets = Object.fromEntries(
      Object.entries({ url, secret, username, password }).filter(([, value]) => value !== ""),
    )
    const payload = {
      name: draft.name.trim(),
      ...(!draft.id ? { kind: draft.kind } : {}),
      enabled: draft.enabled,
      config: {
        ...draft.config,
        recipients: split(recipients),
        tags: split(tags),
      },
      ...(draft.id ? { revision: draft.revision } : {}),
      ...(Object.keys(secrets).length ? { secret: secrets } : {}),
    }
    if (
      await mut.run(() =>
        draft.id ? api.patch(`/api/alerts/channels/${draft.id}`, payload) : api.post("/api/alerts/channels", payload),
      )
    )
      saved()
  }
  const tokenKind = ["serverchan", "pushplus"].includes(draft.kind)
  return (
    <Card title={draft.id ? "编辑通知渠道" : "新建通知渠道"}>
      <form onSubmit={(e) => void save(e)} className="space-y-4">
        <fieldset disabled={mut.busy} className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="渠道名称" plain>
            <Input
              required
              maxLength={100}
              value={draft.name}
              onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
            />
          </Field>
          <Select
            id="channel-kind"
            label="通知方式"
            help="alert.channel.kind"
            value={draft.kind}
            disabled={Boolean(draft.id)}
            options={Object.entries(CHANNEL_KINDS).map(([value, label]) => ({
              value,
              label,
            }))}
            onChange={(v) => {
              setDraft((d) => ({ ...d, kind: v as Channel["kind"] }))
              setUrl("")
              setSecret("")
              setUsername("")
              setPassword("")
            }}
          />
          {draft.kind === "email" ? (
            <>
              <Field label="SMTP 主机" hint="邮件服务商提供的发信服务器，如 smtp.example.com">
                <Input
                  required
                  maxLength={253}
                  placeholder="smtp.example.invalid"
                  value={draft.config.host ?? ""}
                  onChange={(e) => config("host", e.target.value)}
                />
              </Field>
              <Field label="SMTP 端口" hint="SSL 通常 465，STARTTLS 通常 587">
                <Input
                  required
                  type="number"
                  min={1}
                  max={65535}
                  value={draft.config.port ?? 465}
                  onChange={(e) => config("port", Number(e.target.value))}
                />
              </Field>
              <Select
                id="smtp-tls"
                label="安全连接"
                help="alert.channel.security"
                value={draft.config.tls ?? "ssl"}
                onChange={(v) => config("tls", v)}
                options={[
                  { value: "ssl", label: "TLS（通常 465）" },
                  { value: "starttls", label: "STARTTLS（通常 587）" },
                ]}
              />
              <Field label="发件人地址" hint="多数服务商要求与 SMTP 用户名一致">
                <Input
                  required
                  type="email"
                  maxLength={254}
                  value={draft.config.from_address ?? ""}
                  onChange={(e) => config("from_address", e.target.value)}
                />
              </Field>
              <Field label="收件人（逗号分隔）" plain>
                <Input required maxLength={4000} value={recipients} onChange={(e) => setRecipients(e.target.value)} />
              </Field>
              <Field label="SMTP 用户名（留空保留）" hint="编辑时留空表示沿用已保存的值">
                <Input
                  autoComplete="off"
                  maxLength={254}
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                />
              </Field>
              <Field label="SMTP 密码 / 授权码（留空保留）" hint="QQ、163 等邮箱要填授权码，不是登录密码">
                <Input
                  type="password"
                  autoComplete="new-password"
                  maxLength={4096}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </Field>
            </>
          ) : (
            <>
              <Field
                label={
                  tokenKind
                    ? `${draft.kind === "serverchan" ? "SendKey" : "Token"}（留空保留）`
                    : "通知 URL（留空保留）"
                }
                hint="机器人设置页提供的 Webhook 地址或令牌；编辑时留空沿用已保存的值"
              >
                <Input
                  required={!draft.has_secret}
                  type="password"
                  autoComplete="new-password"
                  maxLength={8192}
                  value={tokenKind ? secret : url}
                  onChange={(e) => (tokenKind ? setSecret(e.target.value) : setUrl(e.target.value))}
                />
              </Field>
              {["dingtalk", "feishu"].includes(draft.kind) ? (
                <Field label="机器人签名密钥（可选，留空保留）" help="alert.channel.secret">
                  <Input
                    type="password"
                    autoComplete="new-password"
                    maxLength={4096}
                    value={secret}
                    onChange={(e) => setSecret(e.target.value)}
                  />
                </Field>
              ) : null}
            </>
          )}
          <Field label="路由标签（逗号分隔，留空不限）" help="alert.channel.routes">
            <Input maxLength={2000} value={tags} onChange={(e) => setTags(e.target.value)} />
          </Field>
          <Field label="合并窗口（秒）" help="alert.channel.window">
            <Input
              required
              type="number"
              min={10}
              max={3600}
              value={draft.config.group_seconds ?? 30}
              onChange={(e) => config("group_seconds", Number(e.target.value))}
            />
          </Field>
          <Check
            label="启用自动通知"
            hint="关闭后告警照常记录，只是不再通过这个渠道发送"
            checked={draft.enabled}
            onChange={(v) => setDraft((d) => ({ ...d, enabled: v }))}
          />
        </fieldset>
        <fieldset disabled={mut.busy}>
          <legend className="text-xs text-muted-foreground">通知级别（不选则全部）</legend>
          <div className="flex flex-wrap gap-4">
            {Object.entries(severityLabels).map(([key, label]) => (
              <Check
                key={key}
                plain
                label={label}
                checked={draft.config.severities?.includes(key) ?? false}
                onChange={(v) =>
                  config(
                    "severities",
                    v
                      ? [...(draft.config.severities ?? []), key]
                      : (draft.config.severities ?? []).filter((s) => s !== key),
                  )
                }
              />
            ))}
          </div>
        </fieldset>
        <details>
          <summary className="cursor-pointer text-sm text-link">自定义消息模板（可选）</summary>
          <p className="my-2 text-xs text-muted-foreground">
            支持占位符 {"{title}"}、{"{message}"}。HTML 模板仅用于邮件，不在页面执行。
          </p>
          <div className="grid min-w-0 gap-3">
            <Field label="主题模板" help="alert.channel.template">
              <Input
                maxLength={500}
                value={draft.config.subject_template ?? ""}
                onChange={(e) => config("subject_template", e.target.value)}
              />
            </Field>
            <Field label="纯文本模板" help="alert.channel.template">
              <textarea data-help-wrapped=""
                className={inputClass}
                rows={3}
                maxLength={20000}
                value={draft.config.text_template ?? ""}
                onChange={(e) => config("text_template", e.target.value)}
              />
            </Field>
            {draft.kind === "email" ? (
              <Field label="HTML 模板" help="alert.channel.template">
                <textarea data-help-wrapped=""
                  className={inputClass}
                  rows={3}
                  maxLength={50000}
                  value={draft.config.html_template ?? ""}
                  onChange={(e) => config("html_template", e.target.value)}
                />
              </Field>
            ) : null}
          </div>
        </details>
        <p className="text-xs text-muted-foreground">
          秘密字段加密保存且不回显。编辑时留空保留原值；修改通知方式请新增渠道。
        </p>
        {mut.feedback}
        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={mut.busy}>
            {mut.busy ? "正在保存…" : "保存渠道"}
          </Button>
          <Button type="button" variant="outline" disabled={mut.busy} onClick={cancel}>
            取消编辑
          </Button>
        </div>
      </form>
    </Card>
  )
}
