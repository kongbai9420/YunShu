import { useCallback, useEffect, useRef, useState } from "react"
import { HelpPill } from "@/components/HelpPill"
import { Pencil, RotateCcw, Save, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import { InlineNotice } from "@/components/ui/controls"
import { ApiError, api } from "@/lib/api"

export interface LayoutRecord<T> {
  revision: number
  data: T | null
  updated_by: string
  updated_at: string | null
}

const POLL_MS = 20_000
const DRAFT_KEY = "yunshu-dashboard-draft"
const EDIT_HINT = "编辑模式：拖动卡片标题栏移动，拖动右下角调整大小，或用卡片上的按钮调整。未保存的修改会暂存在本浏览器。"

function timeOf(iso: string | null) {
  return iso ? new Date(iso).toLocaleString("zh-CN", { hour12: false }) : "—"
}

function readDraft<T>(key: string): { base: number; data: T } | null {
  try {
    const raw = sessionStorage.getItem(key)
    return raw ? (JSON.parse(raw) as { base: number; data: T }) : null
  } catch {
    return null
  }
}

function writeDraft<T>(key: string, draft: { base: number; data: T } | null) {
  try {
    if (draft) sessionStorage.setItem(key, JSON.stringify(draft))
    else sessionStorage.removeItem(key)
  } catch {
    // 浏览器不允许存储时只是不能恢复草稿，不影响编辑
  }
}

/**
 * 带修订号的布局状态：布局存数据库；保存带读取时的修订号，冲突时让用户选择，绝不静默覆盖。
 * 浏览器只用来暂存未保存的编辑：离开页面后回来可以恢复。每种布局用自己的草稿键，避免不同视角串写。
 */
export function useRevisionedLayout<T>(path: string, defaults: T, options: { draftKey?: string; editHint?: string } = {}) {
  const draftKey = options.draftKey ?? DRAFT_KEY
  const [record, setRecord] = useState<LayoutRecord<T> | null>(null)
  const [editing, setEditing] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [baseRevision, setBaseRevision] = useState(0)
  const [conflict, setConflict] = useState<LayoutRecord<T> | null>(null)
  const [remoteNotice, setRemoteNotice] = useState<LayoutRecord<T> | null>(null)
  const [recoverable, setRecoverable] = useState<{ base: number; data: T } | null>(() => readDraft<T>(draftKey))
  const [status, setStatus] = useState("")
  const [error, setError] = useState<string | null>(null)
  const editingRef = useRef(false)
  editingRef.current = editing

  const fetchLatest = useCallback(() => api.get<LayoutRecord<T>>(path), [path])

  useEffect(() => {
    fetchLatest()
      .then((rec) => {
        setRecord(rec)
        setBaseRevision(rec.revision)
      })
      .catch((e: Error) => setError(e.message))
  }, [fetchLatest])

  // 定时检查他人保存：查看态直接载入；编辑态只提示，不打断编辑
  useEffect(() => {
    const t = window.setInterval(() => {
      if (document.hidden) return
      fetchLatest()
        .then((rec) => {
          setRecord((cur) => {
            if (cur && rec.revision === cur.revision) return cur
            if (editingRef.current) {
              setRemoteNotice(rec)
              return cur
            }
            setBaseRevision(rec.revision)
            setStatus(`已载入「${rec.updated_by}」保存的修订 ${rec.revision}`)
            return rec
          })
        })
        .catch(() => undefined)
    }, POLL_MS)
    return () => window.clearInterval(t)
  }, [fetchLatest])

  // 编辑中有未保存修改：关闭或刷新页面前由浏览器提示
  useEffect(() => {
    if (!editing || !dirty) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
    }
    window.addEventListener("beforeunload", onBeforeUnload)
    return () => window.removeEventListener("beforeunload", onBeforeUnload)
  }, [editing, dirty])

  const dataOf = (rec: LayoutRecord<T> | null): T => (rec?.data as T | null) ?? defaults

  const startEdit = async (): Promise<T> => {
    let latest = record
    try {
      latest = await fetchLatest()
      setRecord(latest)
    } catch (e) {
      setError((e as Error).message)
    }
    setBaseRevision(latest?.revision ?? 0)
    setConflict(null)
    setRemoteNotice(null)
    setEditing(true)
    setDirty(false)
    setStatus(options.editHint ?? EDIT_HINT)
    return dataOf(latest)
  }

  /** 编辑中的每次修改：记下草稿，离开页面后回来可以恢复 */
  const noteDraft = (data: T) => {
    setDirty(true)
    writeDraft(draftKey, { base: baseRevision, data })
  }

  const finish = (rec: LayoutRecord<T>, msg: string) => {
    setRecord(rec)
    setBaseRevision(rec.revision)
    setConflict(null)
    setRemoteNotice(null)
    setEditing(false)
    setDirty(false)
    writeDraft(draftKey, null)
    setRecoverable(null)
    setStatus(msg)
  }

  const put = async (revision: number, data: T): Promise<boolean> => {
    setError(null)
    try {
      const rec = await api.put<LayoutRecord<T>>(path, { revision, data })
      finish(rec, `已保存为修订 ${rec.revision}`)
      return true
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const cur = (e.detail as { current?: LayoutRecord<T> } | undefined)?.current ?? null
        setConflict(cur)
        setStatus("保存未完成：已被其他人更新")
      } else {
        setError((e as Error).message)
      }
      return false
    }
  }

  const save = (data: T) => put(baseRevision, data)
  const overwrite = (data: T) => (conflict ? put(conflict.revision, data) : Promise.resolve(false))

  const loadLatest = async () => {
    const latest = await fetchLatest()
    finish(latest, `已载入最新的修订 ${latest.revision}，你的修改已放弃`)
  }

  const cancel = async () => {
    const latest = await fetchLatest().catch(() => record)
    if (latest) finish(latest, "已取消编辑，布局未改变")
  }

  /** 恢复上次离开时未保存的编辑：进入编辑态并以草稿为内容；保存时按草稿基于的修订号检查冲突 */
  const recover = (): T | null => {
    if (!recoverable) return null
    setBaseRevision(recoverable.base)
    setEditing(true)
    setDirty(true)
    setRecoverable(null)
    setStatus(`已恢复未保存的编辑（基于修订 ${recoverable.base}）`)
    return recoverable.data
  }

  const discardRecovery = () => {
    writeDraft(draftKey, null)
    setRecoverable(null)
  }

  return {
    record,
    data: dataOf(record),
    loaded: record !== null,
    editing,
    dirty,
    baseRevision,
    conflict,
    remoteNotice,
    recoverable,
    status,
    error,
    startEdit,
    noteDraft,
    save,
    overwrite,
    loadLatest,
    cancel,
    recover,
    discardRecovery,
  }
}

type Revisioned<T> = ReturnType<typeof useRevisionedLayout<T>>

/** 页面顶部的编辑工具栏 + 冲突 / 恢复提示 */
export function LayoutToolbar<T>({
  state,
  canEdit,
  onStartEdit,
  onSave,
  onOverwrite,
  onResetDefault,
  onRecover,
  labels = { edit: "编辑布局", save: "保存布局", reset: "恢复默认布局", thing: "布局" },
}: {
  labels?: { edit: string; save: string; reset: string; thing: string }
  state: Revisioned<T>
  canEdit: boolean
  onStartEdit: () => void
  onSave: () => void
  onOverwrite: () => void
  onResetDefault: () => void
  onRecover: () => void
}) {
  const { record, editing, conflict, remoteNotice, baseRevision, status, error, recoverable } = state
  return (
    <div className="space-y-2 px-6 pb-3">
      <div className="flex flex-wrap items-center gap-2">
        {editing ? (
          <>
            <Button size="sm" onClick={onSave}>
              <Save aria-hidden />
              {labels.save}
            </Button>
            <Button size="sm" variant="outline" onClick={() => void state.cancel()}>
              <X aria-hidden />
              取消
            </Button>
            <Button size="sm" variant="ghost" onClick={onResetDefault}>
              <RotateCcw aria-hidden />
              {labels.reset}
            </Button>
          </>
        ) : canEdit ? (
          <Button size="sm" variant="outline" onClick={onStartEdit} disabled={!state.loaded}>
            <Pencil aria-hidden />
            {labels.edit}
          </Button>
        ) : null}
        <span className="text-xs text-muted-foreground">
          {record && record.revision > 0
            ? `修订 ${editing ? baseRevision : record.revision} · ${record.updated_by} · ${timeOf(record.updated_at)}`
            : `使用默认${labels.thing}（还没有人保存过）`}
        </span>
      </div>
      <p role="status" className="min-h-4 text-xs text-muted-foreground">
        {status}
      </p>
      {error ? (
        <InlineNotice tone="bad" role="alert">
          {error}
        </InlineNotice>
      ) : null}
      {recoverable && !editing && canEdit ? (
        <InlineNotice tone="warn">
          <p>上次编辑{labels.thing}时有未保存的修改（基于修订 {recoverable.base}），已暂存在本浏览器。</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button size="xs" onClick={onRecover}>
              恢复并继续编辑
            </Button>
            <Button size="xs" variant="outline" onClick={state.discardRecovery}>
              丢弃
            </Button>
          </div>
        </InlineNotice>
      ) : null}
      {conflict ? (
        <InlineNotice tone="bad" role="alert">
          <p className="font-medium">保存未完成：{labels.thing}在你编辑期间已被更新。</p>
          <p className="mt-0.5">
            「{conflict.updated_by}」在 {timeOf(conflict.updated_at)} 保存了修订 {conflict.revision}，你的编辑基于修订{" "}
            {baseRevision}。请选择保留哪一份，系统不会自动覆盖。
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button size="xs" onClick={() => void state.loadLatest()}>
              载入最新版本（放弃我的修改）
            </Button>
            <Button size="xs" variant="outline" onClick={onOverwrite}>
              用我的版本覆盖
            </Button>
            <HelpPill topic="layout.overwrite" className="self-center" />
          </div>
        </InlineNotice>
      ) : remoteNotice ? (
        <InlineNotice tone="warn">
          「{remoteNotice.updated_by}」刚保存了修订 {remoteNotice.revision}。你可以继续编辑，保存时会请你选择保留哪一份。
        </InlineNotice>
      ) : null}
    </div>
  )
}
