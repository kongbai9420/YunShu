import { expect, test, type Page } from "@playwright/test"

const password = "fake-e2e-password-123"
const memberPassword = "fake-order-member-123"
const prefix = "fake-ro-"
test.describe.configure({ mode: "serial" })
const ids: Record<string, string> = {}

async function csrf(page: Page) { return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value }
async function call(page: Page, method: "post" | "put", path: string, data: unknown) {
  const r = await page.request[method](path, { data, headers: { "X-CSRF-Token": await csrf(page) } })
  expect(r.ok(), await r.text()).toBeTruthy()
  const body = await r.text()
  return body ? JSON.parse(body) : null
}
async function login(page: Page, user = "admin") {
  const r = await page.request.post("/api/auth/login", { data: { username: user, password: user === "admin" ? password : memberPassword } })
  if (!r.ok() && user === "admin") {
    expect((await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })).ok()).toBeTruthy()
    await call(page, "post", "/api/auth/password", { old_password: "admin123", new_password: password })
  } else expect(r.ok(), await r.text()).toBeTruthy()
}
async function open(page: Page) {
  await page.goto("/#/overview?view=resources")
  await page.getByLabel("搜索名称、编号、平台或地址", { exact: true }).fill(prefix)
  await page.getByRole("button", { name: "应用筛选", exact: true }).click()
  await expect(page.getByRole("status", { name: "资源计数" })).toContainText("共 6 项")
}
async function cardOrder(page: Page, group: string) {
  return page.getByRole("region", { name: `资源分类 ${group}` }).locator("article[data-resource-id]").evaluateAll(
    (els) => els.map((e) => e.getAttribute("aria-label")!.replace(/^资源 /, "")))
}

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext()
  const page = await context.newPage()
  await login(page)
  for (const role of ["operator", "viewer"]) {
    await call(page, "post", "/api/users", { username: `${prefix}${role}`, role, password: "fake-order-initial-123" })
    const member = await browser.newContext()
    const p = await member.newPage()
    expect((await p.request.post("/api/auth/login", { data: { username: `${prefix}${role}`, password: "fake-order-initial-123" } })).ok()).toBeTruthy()
    await call(p, "post", "/api/auth/password", { old_password: "fake-order-initial-123", new_password: memberPassword })
    await member.close()
  }
  for (const name of ["a-host", "b-server", "c-server", "d-server"]) {
    ids[name] = (await call(page, "post", "/api/assets", { name: `${prefix}${name}`, form: "physical", role: "server", lifecycle: "planned" })).id
  }
  for (const name of ["vm-1", "vm-2"]) {
    ids[name] = (await call(page, "post", "/api/assets", { name: `${prefix}${name}`, form: "vm", role: "server", lifecycle: "planned" })).id
    await call(page, "post", "/api/relations", { source_asset_id: ids["a-host"], target_asset_id: ids[name], kind: "hosts" })
  }
  await context.close()
})

test.afterAll(async ({ browser }) => {
  // 共享布局恢复默认，避免影响同一测试库里的其他用例
  const page = await (await browser.newContext()).newPage()
  await login(page)
  const current = await (await page.request.get("/api/overview/resources/layout")).json()
  await call(page, "put", "/api/overview/resources/layout", { revision: current.revision, data: { groups: [], order: [], children: {} } })
})

test("编辑排序：按钮与拖动调整主卡、分组和子资源顺序，保存后刷新仍在，不改宿主关系且零控制写", async ({ page }) => {
  const writes: string[] = []
  page.on("request", (r) => { if (r.method() !== "GET" && /\/api\/(control|relations|assets)/.test(r.url())) writes.push(`${r.method()} ${r.url()}`) })
  await login(page)
  await open(page)
  expect(await cardOrder(page, "物理主机与独立系统")).toEqual(["a-host", "b-server", "c-server", "d-server"].map((n) => prefix + n))
  await page.getByRole("button", { name: "编辑排序", exact: true }).click()
  await expect(page.getByRole("group", { name: `调整 ${prefix}a-host 的位置` })).toBeVisible()

  await page.getByRole("button", { name: `下移 ${prefix}a-host`, exact: true }).click()
  expect(await cardOrder(page, "物理主机与独立系统")).toEqual(["b-server", "a-host", "c-server", "d-server"].map((n) => prefix + n))
  // 拖动 d 到 b 之前
  // 用标准拖放事件模拟（真实鼠标拖动在页面滚动时可能拾取到别的卡片）
  const dt = await page.evaluateHandle(() => new DataTransfer())
  const src = page.getByRole("article", { name: `资源 ${prefix}d-server`, exact: true })
  const dst = page.getByRole("article", { name: `资源 ${prefix}b-server`, exact: true })
  await src.dispatchEvent("dragstart", { dataTransfer: dt })
  await dst.dispatchEvent("dragover", { dataTransfer: dt })
  await dst.dispatchEvent("drop", { dataTransfer: dt })
  await src.dispatchEvent("dragend", { dataTransfer: dt })
  expect(await cardOrder(page, "物理主机与独立系统")).toEqual(["d-server", "b-server", "a-host", "c-server"].map((n) => prefix + n))
  // 分组顺序：虚拟资源分组上移到最前
  await page.getByRole("button", { name: "分组上移 已关联虚拟资源", exact: true }).click()
  await expect(page.locator("section[aria-label^='资源分类']").first()).toHaveAttribute("aria-label", "资源分类 已关联虚拟资源")
  // 子资源顺序
  const host = page.getByRole("article", { name: `资源 ${prefix}a-host`, exact: true })
  await host.getByRole("button", { name: /已确认承载/ }).click()
  await expect(host.getByRole("article", { name: `下挂资源 ${prefix}vm-1`, exact: true })).toBeVisible()
  await host.getByRole("button", { name: `上移 ${prefix}vm-2`, exact: true }).click()
  await expect(host.locator("article[aria-label^='下挂资源']").first()).toHaveAttribute("aria-label", `下挂资源 ${prefix}vm-2`)

  await page.getByRole("button", { name: "保存排序", exact: true }).click()
  await expect(page.getByText(/已保存为修订 \d+/)).toBeVisible()
  await page.reload()
  await open(page)
  await expect.poll(() => cardOrder(page, "物理主机与独立系统"))
    .toEqual(["d-server", "b-server", "a-host", "c-server"].map((n) => prefix + n))
  await expect(page.locator("section[aria-label^='资源分类']").first()).toHaveAttribute("aria-label", "资源分类 已关联虚拟资源")
  const children = await (await page.request.get(`/api/overview/resources/${ids["a-host"]}/children`)).json()
  expect(children.items.map((r: { name: string }) => r.name)).toEqual([`${prefix}vm-2`, `${prefix}vm-1`])
  // 筛选下保存仍保留全部资产（含筛选外）的完整顺序
  const order = await (await page.request.get("/api/overview/resources/order")).json()
  const layout = await (await page.request.get("/api/overview/resources/layout")).json()
  expect(layout.data.order.length).toBe(order.items.length)
  expect(writes).toEqual([])
})

test("修订冲突时选择载入最新或覆盖，取消不保存", async ({ page, browser }) => {
  await login(page)
  await open(page)
  await page.getByRole("button", { name: "编辑排序", exact: true }).click()
  await page.getByRole("button", { name: `下移 ${prefix}d-server`, exact: true }).click()
  // 另一位运维先保存
  const other = await (await browser.newContext()).newPage()
  await login(other, `${prefix}operator`)
  const current = await (await other.request.get("/api/overview/resources/layout")).json()
  await call(other, "put", "/api/overview/resources/layout", { revision: current.revision, data: { ...current.data } })
  await page.getByRole("button", { name: "保存排序", exact: true }).click()
  await expect(page.getByText("保存未完成：排序在你编辑期间已被更新。")).toBeVisible()
  await page.getByRole("button", { name: "用我的版本覆盖", exact: true }).click()
  await expect(page.getByText(/已保存为修订 \d+/)).toBeVisible()
  await expect.poll(() => cardOrder(page, "物理主机与独立系统")).toEqual(["b-server", "d-server", "a-host", "c-server"].map((n) => prefix + n))
  // 取消：草稿丢弃，顺序不变
  await page.getByRole("button", { name: "编辑排序", exact: true }).click()
  await page.getByRole("button", { name: `下移 ${prefix}b-server`, exact: true }).click()
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await expect(page.getByText("已取消编辑，布局未改变")).toBeVisible()
  await expect.poll(() => cardOrder(page, "物理主机与独立系统")).toEqual(["b-server", "d-server", "a-host", "c-server"].map((n) => prefix + n))
})

for (const role of ["operator", "viewer"] as const) {
  test(`${role} 排序权限与窄屏`, async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 })
    await login(page, `${prefix}${role}`)
    await open(page)
    const edit = page.getByRole("button", { name: "编辑排序", exact: true })
    if (role === "viewer") {
      await expect(edit).toHaveCount(0)
      const r = await page.request.put("/api/overview/resources/layout", { data: { revision: 0, data: {} }, headers: { "X-CSRF-Token": await csrf(page) } })
      expect(r.status()).toBe(403)
      // 只读用户可以查看电源能力，但不能执行
      await page.getByRole("button", { name: `电源操作 ${prefix}a-host`, exact: true }).click()
      const dialog = page.getByRole("dialog", { name: `电源操作：${prefix}a-host` })
      await expect(dialog).toContainText("只读用户可以查看控制能力和原因，不能执行控制。")
      await expect(dialog.getByRole("button", { name: "开机", exact: true })).toBeDisabled()
      await page.keyboard.press("Escape")
      await expect(dialog).toHaveCount(0)
    } else {
      await edit.click()
      await expect(page.getByRole("button", { name: `上移 ${prefix}d-server`, exact: true })).toBeVisible()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
      await page.getByRole("button", { name: "取消", exact: true }).click()
    }
  })
}
