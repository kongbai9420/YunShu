import { useRef, useState } from "react"
import { Download, Trash2 } from "lucide-react"

import { AppearancePicker, ThemeSwatch } from "@/components/AppearanceMenu"
import { Button } from "@/components/ui/button"
import { InlineNotice, Select, panelClass } from "@/components/ui/controls"
import { LabeledField } from "@/components/ui/form"
import { api } from "@/lib/api"
import { MODES, MODE_LABEL, useAppearance, type AppearanceState, type Mode } from "@/lib/theme"
import { cn } from "@/lib/utils"
// 主题包示例：管理员下载后照着改，再导入。变量说明见 src/themes/README.md
import EXAMPLE from "@/themes/example-theme.json"

function downloadExample() {
  const blob = new Blob([JSON.stringify(EXAMPLE, null, 2)], { type: "application/json" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = "yunshu-theme-example.json"
  a.click()
  URL.revokeObjectURL(url)
}

/** 平台设置 → 外观：所有人改自己的；管理员另外设平台默认、换玻璃壁纸、导入主题包 */
export function AppearanceTab({ admin }: { admin: boolean }) {
  return (
    <div className="grid max-w-3xl gap-4">
      <section className={cn(panelClass, "p-4")} aria-labelledby="appearance-mine">
        <h2 id="appearance-mine" className="text-[0.9375rem] font-semibold">
          我的外观
        </h2>
        <p className="mt-1 mb-3 text-xs text-muted-foreground">
          只影响你自己，跟账号保存，换电脑登录也一样。选“平台默认”时跟随管理员的设置。顶栏的调色盘按钮也能改。
        </p>
        <AppearancePicker />
      </section>
      {admin ? <AdminAppearance /> : null}
    </div>
  )
}

function AdminAppearance() {
  const { state, themes, replace, reload } = useAppearance()
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const themeFile = useRef<HTMLInputElement>(null)
  const imageFile = useRef<HTMLInputElement>(null)
  if (!state) return <p className="text-[0.8125rem] text-muted-foreground">正在加载…</p>

  async function run(work: () => Promise<string>) {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      setNotice(await work())
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const setDefault = (style: string, mode: Mode) =>
    run(async () => {
      replace(await api.put<AppearanceState>("/api/appearance/default", { style, mode }))
      return "平台默认已保存。选了“平台默认”的账号刷新后即生效。"
    })

  const upload = (path: string, input: HTMLInputElement | null, done: (r: unknown) => string) => {
    const file = input?.files?.[0]
    if (!file || !input) return
    const form = new FormData()
    form.append("file", file)
    void run(async () => {
      try {
        const r = await api.upload<unknown>(path, form)
        reload()
        return done(r)
      } finally {
        input.value = ""
      }
    })
  }

  const imported = themes.filter((t) => t.source === "imported")

  return (
    <>
      {error ? (
        <InlineNotice tone="bad" role="alert">
          {error}
        </InlineNotice>
      ) : null}
      {notice ? <InlineNotice tone="ok">{notice}</InlineNotice> : null}

      <section className={cn(panelClass, "p-4")} aria-labelledby="appearance-default">
        <h2 id="appearance-default" className="text-[0.9375rem] font-semibold">
          平台默认
        </h2>
        <p className="mt-1 mb-3 text-xs text-muted-foreground">新账号和没有自己选过的账号用这里的设置；登录页也按上次的外观显示。</p>
        <div className="flex flex-wrap gap-4">
          <Select
            id="appearance-default-style"
            label="默认风格"
            help="appearance.defaultStyle"
            value={state.default.style}
            disabled={busy}
            options={themes.map((t) => ({ value: t.id, label: t.name }))}
            onChange={(style) => void setDefault(style, state.default.mode)}
          />
          <Select
            id="appearance-default-mode"
            label="默认深浅"
            hint="跟随系统：按电脑或手机的深色模式自动切换。"
            value={state.default.mode}
            disabled={busy}
            options={MODES.map((m) => ({ value: m, label: MODE_LABEL[m] }))}
            onChange={(mode) => void setDefault(state.default.style, mode as Mode)}
          />
        </div>
      </section>

      <section className={cn(panelClass, "p-4")} aria-labelledby="appearance-wallpaper">
        <h2 id="appearance-wallpaper" className="text-[0.9375rem] font-semibold">
          玻璃主题壁纸
        </h2>
        <p className="mt-1 mb-3 text-xs text-muted-foreground">
          {state.wallpaper ? "正在使用自定义图片。" : "正在使用内置渐变。"}只在玻璃风格（以及基于玻璃的主题）下显示，深色时自动压暗。
        </p>
        <div className="flex flex-wrap items-end gap-3">
          {state.wallpaper ? (
            <img src={state.wallpaper.url} alt="当前壁纸预览" className="h-20 w-36 rounded-md border object-cover" />
          ) : null}
          <LabeledField label="上传图片" help="appearance.wallpaper" hint="PNG、JPEG 或 WebP，最大 4 MB。">
            <input
              data-help-wrapped=""
              ref={imageFile}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              disabled={busy}
              className="max-w-64 text-xs file:mr-2 file:rounded-md file:border file:border-input file:bg-card file:px-2 file:py-1 file:text-xs"
              onChange={() =>
                upload("/api/appearance/wallpaper", imageFile.current, () => "壁纸已更换。")
              }
            />
          </LabeledField>
          {state.wallpaper ? (
            <Button
              variant="secondary"
              size="sm"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  await api.del("/api/appearance/wallpaper")
                  reload()
                  return "已删除自定义壁纸，改回内置渐变。"
                })
              }
            >
              <Trash2 aria-hidden className="size-3.5" />
              改回内置渐变
            </Button>
          ) : null}
        </div>
      </section>

      <section className={cn(panelClass, "p-4")} aria-labelledby="appearance-themes">
        <h2 id="appearance-themes" className="text-[0.9375rem] font-semibold">
          导入的主题
        </h2>
        <p className="mt-1 mb-3 text-xs text-muted-foreground">
          主题包是一个 JSON 文件，只能改颜色、圆角、阴影、渐变这类变量，不能带脚本或外部图片。同一 id 再导入会覆盖。
          删除主题后，用它的账号和平台默认会退回“简洁”。
        </p>
        <div className="mb-3 flex flex-wrap items-start gap-3">
          <LabeledField label="导入主题包" help="appearance.import" hint="JSON 文件，最大 64 KB。">
            <input
              data-help-wrapped=""
              ref={themeFile}
              type="file"
              accept="application/json,.json"
              disabled={busy}
              className="max-w-64 text-xs file:mr-2 file:rounded-md file:border file:border-input file:bg-card file:px-2 file:py-1 file:text-xs"
              onChange={() =>
                upload("/api/appearance/themes", themeFile.current, (r) => {
                  const { theme, created } = r as { theme: { name: string }; created: boolean }
                  return `${created ? "已导入" : "已更新"}主题“${theme.name}”。可以在上方或顶栏选用。`
                })
              }
            />
          </LabeledField>
          <Button variant="secondary" size="sm" className="mt-5" onClick={downloadExample}>
            <Download aria-hidden className="size-3.5" />
            下载示例主题包
          </Button>
        </div>
        {imported.length === 0 ? (
          <p className="text-[0.8125rem] text-muted-foreground">还没有导入的主题。</p>
        ) : (
          <ul className="divide-y rounded-md border">
            {imported.map((t) => (
              <li key={t.id} className="flex items-center gap-3 px-3 py-2">
                <ThemeSwatch theme={t} />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[0.8125rem] font-medium">
                    {t.name} <span className="text-xs font-normal text-muted-foreground">· {t.id} · 基于{t.base === "glass" ? "玻璃" : "简洁"}</span>
                  </p>
                  {t.description || t.author ? (
                    <p className="truncate text-xs text-muted-foreground">
                      {[t.description, t.author && `作者：${t.author}`, t.theme_version && `版本 ${t.theme_version}`].filter(Boolean).join(" · ")}
                    </p>
                  ) : null}
                </div>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={busy}
                  aria-label={`删除主题 ${t.name}`}
                  onClick={() => {
                    if (!window.confirm(`删除主题“${t.name}”？用它的账号和平台默认会退回“简洁”。`)) return
                    void run(async () => {
                      await api.del(`/api/appearance/themes/${encodeURIComponent(t.id)}`)
                      reload()
                      return `已删除主题“${t.name}”。`
                    })
                  }}
                >
                  <Trash2 aria-hidden className="size-3.5" />
                  删除
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  )
}
