// 检查每个可操作 / 可设置的控件都有说明（一句话说得清的写在控件下方，说不清的加「!」胶囊）。
// 封装控件（TextField / Select / Field / Check / LabeledField / CheckField）必须带 hint、help 或 plain（标签已足够说明）之一；
// 原生 input / select / textarea 必须放进封装控件（data-help-wrapped）、或用 aria-describedby 关联说明、或标 data-help-plain。
// 用法：node scripts/check-help.mjs [--list]
import { readdirSync, readFileSync, statSync } from "node:fs"
import { join, relative } from "node:path"

const root = new URL("../src/", import.meta.url).pathname.replace(/^\/(\w:)/, "$1")
const COMPONENTS = ["TextField", "Select", "Field", "Check", "LabeledField", "CheckField"]
const files = []
const walk = (dir) => {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p)
    else if (p.endsWith(".tsx") && !p.includes(join("components", "ui"))) files.push(p)
  }
}
walk(join(root, "pages"))
walk(join(root, "components"))

/** 从 <Tag 开始找到这个开始标签的结尾（跳过 {…} 表达式与字符串） */
function tagText(src, start) {
  let depth = 0
  let quote = ""
  for (let i = start + 1; i < src.length; i++) {
    const c = src[i]
    if (quote) {
      if (c === quote && src[i - 1] !== "\\") quote = ""
      continue
    }
    if (c === '"' || c === "'" || c === "`") quote = c
    else if (c === "{") depth++
    else if (c === "}") depth--
    else if (c === ">" && depth === 0) return src.slice(start, i + 1)
  }
  return src.slice(start)
}

const problems = []
let total = 0
for (const file of files) {
  const src = readFileSync(file, "utf8")
  // 前面紧跟标识符的是 TypeScript 泛型（如 Partial<Check>），不是控件
  const re = /(?<![\w.])<(TextField|Select|Field|Check|LabeledField|CheckField|input|select|textarea)\b/g
  let m
  while ((m = re.exec(src))) {
    const tag = tagText(src, m.index)
    const name = m[1]
    if (name === "Check" && /aria-hidden/.test(tag)) continue // lucide 的对勾图标，不是勾选框
    if (name === "input" && /type="(hidden|file)"/.test(tag) && !/data-help/.test(tag)) {
      // 文件选择框的说明写在它所在的封装控件里；隐藏字段不是用户可操作项
      if (/type="hidden"/.test(tag)) continue
    }
    total++
    const ok = COMPONENTS.includes(name)
      ? /\b(hint|help)=/.test(tag) || /\splain(\s|\/|>|=)/.test(tag)
      : /data-help-(plain|wrapped)/.test(tag) || /aria-describedby=/.test(tag)
    if (!ok) {
      const line = src.slice(0, m.index).split("\n").length
      const label = (tag.match(/(?:label|aria-label)=(?:"([^"]*)"|\{`?([^}`]*)`?\})/) || [])
      problems.push(`${relative(root, file)}:${line}  <${name}> ${label[1] ?? label[2] ?? ""}`.trimEnd())
    }
  }
}
if (process.argv.includes("--list") || problems.length) console.log(problems.join("\n"))
console.log(`控件 ${total} 个，缺少说明 ${problems.length} 个`)
process.exit(problems.length ? 1 : 0)
