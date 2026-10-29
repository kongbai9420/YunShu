import { Badge } from "@/components/status"

/** 修复与缓解（4.3 P18 补充）：本地比对得出的修复版本、各来源的修复建议原文、补丁与公告链接。只整理来源原文，不由 AI 生成。 */

export interface FixText { kind: string; kind_text: string; text: string; sources: string[] }
export interface Remediation {
  fixed: { product: string; fixed_version: string; assets: string[]; note: string }[]
  texts: FixText[]
  links: { url: string; source: string; tags: string[]; tags_text: string[] }[]
  kev_due: string
}

const KIND_TONE: Record<string, "ok" | "warn" | "info"> = { solution: "ok", workaround: "warn", action: "info" }

export function RemediationSection({ r, kev }: { r: Remediation; kev: boolean }) {
  const empty = !r.fixed.length && !r.texts.length && !r.links.length
  return (
    <section aria-label="修复与缓解" className="space-y-2">
      <h4 className="font-medium">修复与缓解</h4>
      {kev && r.kev_due ? <p className="text-xs"><Badge tone="bad">CISA 要求处置期限 {r.kev_due}</Badge></p> : null}
      {empty ? <p className="text-xs text-muted-foreground">各来源都没有给出修复版本或缓解措施，请查看发布来源的原文或厂商公告。</p> : null}
      {r.fixed.length ? (
        <ul className="space-y-1 text-xs" aria-label="修复版本">
          {r.fixed.map((f) => (
            <li key={`${f.product}|${f.fixed_version}`}>
              <span className="font-medium">{f.product}</span>：
              {f.fixed_version ? <>升级到 <span className="font-medium">{f.fixed_version}</span> 或更高版本</> : <span className="text-bad">{f.note || "尚无修复版本"}</span>}
              <span className="text-muted-foreground">（{f.assets.slice(0, 6).join("、")}{f.assets.length > 6 ? ` 等 ${f.assets.length} 台` : ""}）</span>
            </li>
          ))}
        </ul>
      ) : null}
      {r.texts.length ? (
        <ul className="space-y-1.5 text-xs" aria-label="来源给出的建议">
          {r.texts.map((t, i) => (
            <li key={i} className="rounded-md bg-secondary p-2">
              <p className="mb-0.5"><Badge tone={KIND_TONE[t.kind] ?? "neutral"}>{t.kind_text}</Badge> <span className="text-muted-foreground">来源：{t.sources.join("、")}</span></p>
              <p className="whitespace-pre-wrap">{t.text}</p>
            </li>
          ))}
        </ul>
      ) : null}
      {r.links.length ? (
        <ul className="space-y-0.5 break-all text-xs" aria-label="补丁与公告">
          {r.links.map((l) => (
            <li key={l.url}>
              <span className="text-muted-foreground">[{l.tags_text.join("、")}]</span>{" "}
              <a className="text-primary underline" href={l.url} target="_blank" rel="noreferrer noopener">{l.url}</a>
            </li>
          ))}
        </ul>
      ) : null}
      <p className="text-xs text-muted-foreground">以上为各来源原文与版本比对结果，执行前请以厂商公告为准，并先在测试环境验证。</p>
    </section>
  )
}
