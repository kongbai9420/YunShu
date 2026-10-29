import { Select } from "@/components/ui/controls"
import { TextField } from "@/components/ui/form"
import type { Credential } from "@/lib/api"
import { DEFAULT_PORT, PROTOCOL_LABEL, optionsFrom } from "@/lib/vocab"

const KINDS: Record<string, string> = {
  password: "用户名 + 密码",
  ssh_key: "SSH 私钥",
  snmp_community: "SNMP v2c 团体名",
  snmpv3: "SNMPv3",
  token: "API 令牌",
}
const ALLOWED: Record<string, string[]> = {
  ssh: ["password", "ssh_key"],
  snmp: ["snmp_community", "snmpv3"],
  "pve-api": ["password", "token"],
}
export interface ConnectionDraft {
  protocol: string
  address: string
  port: string
  mode: string
  credentialId: string
  alias: string
  kind: string
  username: string
  secret: string
  authProtocol: string
  authKey: string
  privProtocol: string
  privKey: string
}
export const EMPTY_CONNECTION: ConnectionDraft = {
  protocol: "ssh",
  address: "",
  port: "",
  mode: "new",
  credentialId: "",
  alias: "",
  kind: "password",
  username: "",
  secret: "",
  authProtocol: "SHA",
  authKey: "",
  privProtocol: "AES",
  privKey: "",
}

export function connectionPayload(d: ConnectionDraft) {
  if (!d.protocol) return []
  return [
    {
      protocol: d.protocol,
      address: d.address.trim(),
      port: d.port ? Number(d.port) : null,
      purpose: "monitoring",
      ...(d.mode === "existing" ? { credential_id: d.credentialId } : {}),
      ...(d.mode === "new"
        ? {
            new_credential: {
              alias: d.alias.trim() || `${PROTOCOL_LABEL[d.protocol] ?? d.protocol} · ${d.address.trim()}`.slice(0, 200),
              kind: d.kind,
              username: d.username.trim(),
              secret:
                d.kind === "snmpv3"
                  ? JSON.stringify({
                      auth_protocol: d.authProtocol,
                      auth_key: d.authKey,
                      priv_protocol: d.privProtocol,
                      priv_key: d.privKey,
                    })
                  : d.secret,
            },
          }
        : {}),
    },
  ]
}

export function ConnectionFields({
  draft: d,
  onChange,
  credentials,
  loading,
  loadError,
  onRetry,
}: {
  draft: ConnectionDraft
  onChange: (value: ConnectionDraft) => void
  credentials: Credential[]
  loading: boolean
  loadError: string | null
  onRetry: () => void
}) {
  const set = (key: keyof ConnectionDraft) => (value: string) => onChange({ ...d, [key]: value })
  const kinds = ALLOWED[d.protocol] ?? ["password"]
  const matching = credentials.filter((c) => kinds.includes(c.kind))
  return (
    <fieldset className="min-w-0 space-y-3 rounded-lg border p-3">
      <legend className="px-1 text-sm font-semibold">连接与登录</legend>
      <p className="text-xs text-muted-foreground">
        选择协议并填写设备地址和登录凭据后，可在创建前获取设备信息。还可在详情里添加其他端点。
      </p>
      <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
        <Select
          id="new-protocol"
          label="协议"
          help="conn.protocol"
          value={d.protocol}
          onChange={(protocol) =>
            onChange({
              ...d,
              protocol,
              port: "",
              credentialId: "",
              kind: (ALLOWED[protocol] ?? ["password"])[0],
              username: "",
              secret: "",
              authKey: "",
              privKey: "",
            })
          }
          options={[{ value: "", label: "暂不配置，仅建档" }, ...optionsFrom(PROTOCOL_LABEL)]}
        />
        {d.protocol ? (
          <>
            <TextField
              id="new-address"
              label="设备地址"
              value={d.address}
              onChange={set("address")}
              required
              maxLength={255}
              placeholder="192.0.2.10 或主机名"
              hint="只填 IP 或主机名，不含协议前缀和路径。"
            />
            <TextField
              id="new-port"
              label="端口"
              type="number"
              value={d.port}
              onChange={set("port")}
              min={1}
              max={65535}
              hint={DEFAULT_PORT[d.protocol] ? `留空使用 ${DEFAULT_PORT[d.protocol]}` : "填写设备实际服务端口。"}
            />
            <Select
              id="new-credential-mode"
              label="登录凭据"
              help="conn.credential"
              value={d.mode}
              onChange={(mode) => onChange({ ...d, mode, secret: "", authKey: "", privKey: "" })}
              options={[
                { value: "new", label: "填写新凭据" },
                { value: "existing", label: "选择已有凭据" },
                { value: "none", label: "暂不关联凭据" },
              ]}
            />
            {d.mode === "existing" ? (
              <div className="min-w-0 space-y-2 sm:col-span-2">
                <Select
                  id="new-credential-id"
                  label="已有凭据"
                  hint="只列出与所选协议匹配的凭据"
                  value={d.credentialId}
                  onChange={set("credentialId")}
                  disabled={loading}
                  options={[
                    { value: "", label: loading ? "正在加载…" : "请选择凭据" },
                    ...matching.map((c) => ({
                      value: c.id,
                      label: `${c.alias}${c.username ? ` · ${c.username}` : ""}`,
                    })),
                  ]}
                />
                {loadError ? (
                  <p role="alert" className="text-xs text-bad">
                    {loadError}{" "}
                    <button type="button" className="underline" onClick={onRetry}>
                      重试加载凭据
                    </button>
                  </p>
                ) : !loading && matching.length === 0 ? (
                  <p className="text-xs text-muted-foreground">没有适用于此协议的凭据，可切换为“填写新凭据”。</p>
                ) : null}
              </div>
            ) : null}
            {d.mode === "new" ? (
              <>
                <TextField
                  id="new-credential-alias"
                  label="凭据别名"
                  value={d.alias}
                  onChange={set("alias")}
                  maxLength={200}
                  hint="可留空，保存时按协议和地址命名；之后可供其他资产选择。"
                  placeholder="例如 Linux-SSH"
                />
                <Select
                  id="new-credential-kind"
                  label="认证方式"
                  help="conn.authMode"
                  value={d.kind}
                  onChange={(kind) =>
                    onChange({
                      ...d,
                      kind,
                      secret: "",
                      authKey: "",
                      privKey: "",
                    })
                  }
                  options={kinds.map((value) => ({
                    value,
                    label: KINDS[value],
                  }))}
                />
                {d.kind !== "snmp_community" ? (
                  <TextField
                    id="new-username"
                    label="设备用户名"
                    value={d.username}
                    onChange={set("username")}
                    required
                    maxLength={100}
                    autoComplete="off"
                    placeholder={
                      d.protocol === "ssh"
                        ? "root 或其他 SSH 用户名"
                        : d.protocol === "pve-api"
                          ? d.kind === "token"
                            ? "user@realm!tokenid"
                            : "root@pam"
                          : "设备登录用户名"
                    }
                    hint="填写设备账户，与平台登录账户无关。"
                  />
                ) : null}
                {d.kind === "ssh_key" ? (
                  <div className="min-w-0 space-y-1 sm:col-span-2">
                    <label className="text-xs font-medium text-muted-foreground" htmlFor="new-private-key">
                      SSH 私钥 *
                    </label>
                    <textarea
                      id="new-private-key"
                      data-help-wrapped=""
                      aria-describedby="new-private-key-hint"
                      required
                      maxLength={8192}
                      rows={4}
                      value={d.secret}
                      onChange={(e) => set("secret")(e.target.value)}
                      autoComplete="off"
                      spellCheck={false}
                      className="w-full min-w-0 rounded-md border bg-card p-2 text-sm [-webkit-text-security:disc]"
                    />
                    <p id="new-private-key-hint" className="text-xs text-muted-foreground">
                      粘贴完整私钥，保留换行；当前支持未加密私钥。
                    </p>
                  </div>
                ) : d.kind === "snmpv3" ? (
                  <>
                    <Select
                      id="new-auth-protocol"
                      label="认证算法"
                      help="conn.authAlgo"
                      value={d.authProtocol}
                      onChange={set("authProtocol")}
                      options={["SHA", "SHA256", "SHA512", "MD5"].map((value) => ({ value, label: value }))}
                    />
                    <TextField
                      id="new-auth-key"
                      label="认证密码"
                      type="password"
                      value={d.authKey}
                      onChange={set("authKey")}
                      autoComplete="new-password"
                      maxLength={1000}
                      hint="留空表示不认证；加密必须同时配置认证。"
                    />
                    <Select
                      id="new-priv-protocol"
                      label="加密算法"
                      help="conn.privAlgo"
                      value={d.privProtocol}
                      onChange={set("privProtocol")}
                      options={["AES", "AES256", "DES"].map((value) => ({
                        value,
                        label: value,
                      }))}
                    />
                    <TextField
                      id="new-priv-key"
                      label="加密密码"
                      hint="至少 8 位；留空表示只认证不加密"
                      type="password"
                      value={d.privKey}
                      onChange={set("privKey")}
                      autoComplete="new-password"
                      maxLength={1000}
                    />
                  </>
                ) : (
                  <TextField
                    id="new-secret"
                    label={d.kind === "snmp_community" ? "只读团体名" : d.kind === "token" ? "API 令牌值" : "设备密码"}
                    hint="加密保存，保存后页面不再显示明文"
                    type="password"
                    value={d.secret}
                    onChange={set("secret")}
                    required
                    maxLength={8192}
                    autoComplete="new-password"
                  />
                )}
                <p className="text-xs text-muted-foreground sm:col-span-2">
                  凭据加密保存，后续只显示别名。保存失败会保留当前输入，关闭窗口后清除。
                </p>
              </>
            ) : null}
            {d.mode === "none" ? (
              <p className="text-xs text-muted-foreground sm:col-span-2">
                先保存端点，稍后在详情里关联凭据；需要认证的采集暂时不可用。
              </p>
            ) : null}
            {["https", "http", "winrm", "other"].includes(d.protocol) ? (
              <p className="text-xs text-muted-foreground sm:col-span-2">
                此协议当前没有专用指标采集器；可选择“仅 TCP 可达性”模板检测端口。
              </p>
            ) : null}
          </>
        ) : null}
      </div>
    </fieldset>
  )
}
