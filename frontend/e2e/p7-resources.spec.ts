import { expect, test, type Page } from "@playwright/test"

const password = "fake-e2e-password-123"
const memberPassword = "fake-resource-member-123"
const prefix = "fake-rp-"
test.describe.configure({ mode: "serial" })
const hostName = `${prefix}00-host`
const longName = `${prefix}01-名称很长的虚拟资源用于检查窄屏换行而不挤压状态标签`
let hostId = ""
let guestId = ""
let siteId = ""
let total = 0

async function csrf(page: Page) { return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value }
async function post(page: Page, path: string, data: unknown) {
  const r = await page.request.post(path, { data, headers: { "X-CSRF-Token": await csrf(page) } })
  expect(r.ok(), await r.text()).toBeTruthy()
  const body = await r.text()
  return body ? JSON.parse(body) : null
}
async function login(page: Page, role = "admin") {
  const r = await page.request.post("/api/auth/login", { data: { username: role === "admin" ? "admin" : `fake-rp-${role}`, password: role === "admin" ? password : memberPassword } })
  if (!r.ok() && role === "admin") {
    expect((await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })).ok()).toBeTruthy()
    await post(page, "/api/auth/password", { old_password: "admin123", new_password: password })
  } else expect(r.ok(), await r.text()).toBeTruthy()
}
async function search(page: Page, q: string) {
  await page.getByLabel("搜索名称、编号、平台或地址", { exact: true }).fill(q)
  await page.getByRole("button", { name: "应用筛选", exact: true }).click()
}
const card = (page: Page, name: string) => page.getByRole("article", { name: `资源 ${name}`, exact: true })

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext()
  const page = await context.newPage()
  await login(page)
  for (const role of ["operator", "viewer"]) {
    await post(page, "/api/users", { username: `fake-rp-${role}`, role, password: "fake-resource-initial-123" })
    const member = await browser.newContext()
    const p = await member.newPage()
    expect((await p.request.post("/api/auth/login", { data: { username: `fake-rp-${role}`, password: "fake-resource-initial-123" } })).ok()).toBeTruthy()
    await post(p, "/api/auth/password", { old_password: "fake-resource-initial-123", new_password: memberPassword })
    await member.close()
  }
  const site = await post(page, "/api/sites", { name: `${prefix}site`, kind: "home" })
  siteId = site.id
  const host = await post(page, "/api/assets", { name: hostName, form: "physical", role: "server", platform: "pve", lifecycle: "planned", site_id: site.id, tags: ["fake-rp-tag"] })
  hostId = host.id
  const other = await post(page, "/api/assets", { name: `${prefix}02-other-host`, form: "physical", role: "server", lifecycle: "planned" })
  for (let i = 0; i < 27; i++) {
    const child = await post(page, "/api/assets", { name: i === 0 ? longName : `${prefix}child-${String(i).padStart(2, "0")}`, form: i % 2 ? "container" : "vm", role: "server", lifecycle: "planned" })
    await post(page, "/api/relations", { source_asset_id: host.id, target_asset_id: child.id, kind: "hosts" })
    if (i === 0) {
      guestId = child.id
      await post(page, "/api/relations", { source_asset_id: child.id, target_asset_id: host.id, kind: "hosts" })
      await post(page, "/api/relations", { source_asset_id: other.id, target_asset_id: child.id, kind: "hosts" })
    }
  }
  for (const role of ["switch", "router", "ap", "firewall", "nas", "storage", "ups", "other"]) {
    await post(page, "/api/assets", { name: `${prefix}z-${role}`, form: "physical", role, lifecycle: "planned" })
  }
  await post(page, "/api/assets", { name: `${prefix}z-orphan`, form: "vm", role: "server", lifecycle: "planned" })
  total = 38
  await context.close()
})

test("真实资源批量读取、分类筛选、分页与一层承载；卡片长条切换零业务写", async ({ page }) => {
  await login(page)
  const writes: string[] = []
  const reads: string[] = []
  page.on("request", (r) => {
    if (!r.url().includes("/api/")) return
    if (r.method() === "GET") reads.push(new URL(r.url()).pathname)
    else if (!["HEAD", "OPTIONS"].includes(r.method())) writes.push(r.url())
  })
  await page.goto("/#/overview?view=resources")
  await expect(page.getByRole("heading", { name: "资源全景", exact: true })).toBeVisible()
  await expect(page.locator("[data-resource-id]").first()).toBeVisible()
  // 开发服务器的 React StrictMode 会执行两次挂载；请求数仍与卡片数无关。
  expect(reads.filter((p) => p === "/api/overview/resources").length).toBeLessThanOrEqual(2)
  expect(reads.filter((p) => /^\/api\/assets\/.+/.test(p))).toHaveLength(0)
  await search(page, prefix)
  await expect(page.getByRole("status", { name: "资源计数" })).toContainText(`共 ${total} 项匹配资产`)
  await expect(page.locator("[data-resource-id]")).toHaveCount(25)
  await expect(card(page, hostName)).toContainText("电源：未知")
  await card(page, hostName).getByRole("button", { name: "已确认承载 · 27 项", exact: true }).click()
  const children = card(page, hostName).getByLabel("已确认下挂资源", { exact: true })
  await expect(children.getByRole("article")).toHaveCount(25)
  await expect(children).toContainText("不受上方筛选限制")
  await expect(children.getByRole("button", { name: /已确认承载/ })).toHaveCount(0)
  const childPages = children.getByRole("group", { name: "下挂资源分页" })
  await childPages.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(children.getByRole("article")).toHaveCount(2)
  const readCount = reads.length
  await page.getByRole("group", { name: "资源展示方式" }).getByRole("button", { name: "长条", exact: true }).click()
  await expect(children.getByRole("article")).toHaveCount(2)
  expect(reads).toHaveLength(readCount)
  await page.getByRole("group", { name: "资源分页", exact: true }).getByRole("button", { name: "下一页", exact: true }).click()
  await expect(page.locator("[data-resource-id]")).toHaveCount(13)
  await expect(page.getByRole("region", { name: "资源分类 未关联虚拟资源" })).toBeVisible()
  await page.getByLabel("类别", { exact: true }).selectOption("ups")
  await expect(page.getByRole("status", { name: "资源计数" })).toContainText("共 1 项匹配资产")
  await expect(card(page, `${prefix}z-ups`)).toBeVisible()
  await page.getByLabel("类别", { exact: true }).selectOption("")
  await page.getByLabel("场所", { exact: true }).selectOption(siteId)
  await expect(page.locator("[data-resource-id]")).toHaveCount(1)
  await page.getByLabel("标签（精确匹配）", { exact: true }).fill("fake-rp-tag")
  await search(page, prefix)
  await expect(card(page, hostName)).toBeVisible()
  await page.getByRole("button", { name: "清空筛选", exact: true }).click()
  await search(page, longName)
  await expect(page.locator("[data-resource-id]")).toHaveCount(1)
  await expect(card(page, longName)).toContainText("已确认宿主")
  await expect(card(page, longName).getByRole("button", { name: hostName, exact: true })).toBeVisible()
  await search(page, "fake-rp-no-such-asset")
  await expect(page.getByRole("heading", { name: "没有匹配的资产" })).toBeVisible()
  expect(writes).toEqual([])
})

for (const role of ["operator", "viewer"]) {
  test(`${role} 真实资源详情与窄屏深色 200% 字号`, async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 900 })
    await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" })
    await login(page, role)
    await page.goto("/#/overview?view=resources")
    await expect(page.getByRole("heading", { name: "资源全景", exact: true })).toBeVisible()
    await page.addStyleTag({ content: "html { font-size:200% !important }" })
    await search(page, longName)
    const guest = page.locator(`[data-resource-id="${guestId}"]`)
    await expect(guest).toBeVisible()
    await expect(guest).toContainText("电源：未知")
    // 电源快捷入口尚未接入；排序编辑只对 admin / operator 显示
    await expect(page.getByRole("button", { name: /开机|关机|保存排序/ })).toHaveCount(0)
    await expect(page.getByRole("button", { name: "编辑排序", exact: true })).toHaveCount(role === "viewer" ? 0 : 1)
    for (const mode of ["长条", "卡片"]) {
      await page.getByRole("group", { name: "资源展示方式" }).getByRole("button", { name: mode, exact: true }).click()
      expect(await page.getByRole("region", { name: "总览内容" }).evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBeTruthy()
      expect(await guest.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBeTruthy()
    }
    await guest.getByRole("button", { name: hostName, exact: true }).click()
    const drawer = page.getByRole("dialog")
    await expect(drawer).toContainText(hostName)
    await expect(drawer.getByRole("tab", { name: "编辑", exact: true })).toHaveCount(role === "operator" ? 1 : 0)
    expect(hostId).toBeTruthy()
  })
}
