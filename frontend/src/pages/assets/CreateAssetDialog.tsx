import { useEffect, useState } from "react"
import { Dialog } from "radix-ui"

import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { ModalContent, ModalFooter, ModalHeader } from "@/components/ui/dialog"
import { api, type Asset, type Credential, type Site } from "@/lib/api"
import { AssetFields, EMPTY_DRAFT, payloadOf, type AssetDraft } from "./AssetFields"
import { ConnectionFields, EMPTY_CONNECTION, connectionPayload, type ConnectionDraft } from "./ConnectionFields"
import { InformationPreview, type InformationResult } from "./InformationPreview"

export function CreateAssetDialog({
  open,
  sites,
  onClose,
  onCreated,
}: {
  open: boolean
  sites: Site[]
  onClose: () => void
  onCreated: (a: Asset) => void
}) {
  const [draft, setDraft] = useState<AssetDraft>(EMPTY_DRAFT)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [connection, setConnection] = useState<ConnectionDraft>(EMPTY_CONNECTION)
  const [trust, setTrust] = useState<Pick<InformationResult, "tls_fingerprint" | "ssh_host_key">>({ tls_fingerprint: "", ssh_host_key: "" })
  const [credentials, setCredentials] = useState<Credential[]>([])
  const [loadingCredentials, setLoadingCredentials] = useState(false)
  const [credentialError, setCredentialError] = useState<string | null>(null)
  const [credentialRevision, setCredentialRevision] = useState(0)

  useEffect(() => {
    setDraft(EMPTY_DRAFT)
    setConnection(EMPTY_CONNECTION)
    setTrust({ tls_fingerprint: "", ssh_host_key: "" })
    setError(null)
  }, [open])

  useEffect(() => {
    if (!open) return
    let active = true
    setLoadingCredentials(true)
    setCredentialError(null)
    setCredentials([])
    api
      .get<Credential[]>("/api/credentials")
      .then((items) => {
        if (active) setCredentials(items)
      })
      .catch((err) => {
        if (active) setCredentialError((err as Error).message)
      })
      .finally(() => {
        if (active) setLoadingCredentials(false)
      })
    return () => {
      active = false
    }
  }, [open, credentialRevision])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (busy) return
    if (connection.protocol && connection.mode === "existing" && !connection.credentialId) {
      setError("请选择已有凭据，或切换为填写新凭据 / 暂不关联凭据。")
      return
    }
    if (
      connection.protocol &&
      connection.mode === "new" &&
      connection.kind === "snmpv3" &&
      connection.privKey &&
      !connection.authKey
    ) {
      setError("SNMPv3 配置加密密码时，还需要填写认证密码。")
      return
    }
    setBusy(true)
    setError(null)
    try {
      onCreated(
        await api.post<Asset>("/api/assets", {
          ...payloadOf(draft),
          endpoints: connectionPayload(connection).map((endpoint) => ({ ...endpoint, ...trust })),
        }),
      )
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={(o) => (o || busy ? null : onClose())}>
      <ModalContent width={640} aria-describedby={undefined}>
        <ModalHeader title="新增资产" closeDisabled={busy} />
        <form onSubmit={submit} className="space-y-4">
          <fieldset disabled={busy} className="min-w-0 space-y-4">
            <ConnectionFields
              draft={connection}
              onChange={(value) => {
                setConnection(value)
                setTrust({ tls_fingerprint: "", ssh_host_key: "" })
              }}
              credentials={credentials}
              loading={loadingCredentials}
              loadError={credentialError}
              onRetry={() => setCredentialRevision((v) => v + 1)}
            />
            {connection.protocol ? <InformationPreview
              source={connection.address.trim() ? { endpoint: connectionPayload(connection)[0] } : null}
              draft={draft}
              onApply={(values) => setDraft((previous) => ({ ...previous, ...values }))}
              onResult={(result) => setTrust({ tls_fingerprint: result.tls_fingerprint, ssh_host_key: result.ssh_host_key })}
              disabled={busy}
            /> : null}
            <AssetFields draft={draft} onChange={setDraft} sites={sites} idPrefix="new" compact />
          </fieldset>
          {error ? (
            <InlineNotice tone="bad" role="alert">
              {error}
            </InlineNotice>
          ) : null}
          <ModalFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
              取消
            </Button>
            <Button type="submit" disabled={busy || !draft.name.trim()}>
              {busy ? "正在保存…" : "创建"}
            </Button>
          </ModalFooter>
        </form>
      </ModalContent>
    </Dialog.Root>
  )
}
