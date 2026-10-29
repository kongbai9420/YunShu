import { expect, test, type Page } from "@playwright/test"

const password = "fake-e2e-password-123"
const initial = "fake-shell-initial-123"
const memberPassword = "fake-shell-changed-123"

async function csrf(page: Page) {
  return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value
}
async function login(page: Page, role = "admin") {
  let res = await page.request.post("/api/auth/login", { data: {
    username: role === "admin" ? "admin" : `fake-shell-${role}`, password: role === "admin" ? password : memberPassword,
  } })
  if (!res.ok() && role === "admin") {
    expect((await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })).ok()).toBeTruthy()
    res = await page.request.post("/api/auth/password", { data: { old_password: "admin123", new_password: password }, headers: { "X-CSRF-Token": await csrf(page) } })
  }
  expect(res.ok(), await res.text()).toBeTruthy()
}
async function create(page: Page, path: string, data: unknown) {
  const res = await page.request.post(path, { data, headers: { "X-CSRF-Token": await csrf(page) } })
  expect(res.ok(), await res.text()).toBeTruthy()
  return res.json()
}
const tabs = (page: Page) => page.getByRole("group", { name: "总览类型", exact: true })

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext()
  const page = await context.newPage()
  await login(page)
  for (const role of ["operator", "viewer"]) {
    const res = await page.request.post("/api/users", { data: { username: `fake-shell-${role}`, role, password: initial }, headers: { "X-CSRF-Token": await csrf(page) } })
    if (res.status() === 409) continue
    expect(res.status()).toBe(201)
    const member = await browser.newContext()
    const p = await member.newPage()
    expect((await p.request.post("/api/auth/login", { data: { username: `fake-shell-${role}`, password: initial } })).ok()).toBeTruthy()
    expect((await p.request.post("/api/auth/password", { data: { old_password: initial, new_password: memberPassword }, headers: { "X-CSRF-Token": await csrf(p) } })).ok()).toBeTruthy()
    await member.close()
  }
  await context.close()
})

test("正式总览复用真实布局，旧拓扑链接保留视图，切换零业务写入", async ({ page }) => {
  await login(page)
  const asset = await create(page, "/api/assets", { name: "假外壳验收设备", form: "physical", role: "server", lifecycle: "planned" })
  const views = await (await page.request.get("/api/topology/views")).json()
  const home = views[0]
  const other = views[1]
  const patched = await page.request.patch(`/api/topology/views/${home.id}`, { data: {
    revision: home.revision, layout: { ...home.layout, [asset.id]: { x: 120, y: 180, locked: true } },
  }, headers: { "X-CSRF-Token": await csrf(page) } })
  expect(patched.ok(), await patched.text()).toBeTruthy()
  const before = await (await page.request.get(`/api/topology/views/${home.id}`)).json()
  const dashboard = await (await page.request.get("/api/dashboard/layout")).json()
  const writes: string[] = []
  page.on("request", (r) => { if (r.url().includes("/api/") && !["GET", "HEAD", "OPTIONS"].includes(r.method())) writes.push(r.url()) })
  await page.goto(`/#/topology?view=${home.id}`)
  await expect(page).toHaveURL(new RegExp(`#/overview\\?view=topology&topology_view=${home.id}$`))
  await expect(page.locator(".react-flow__node").filter({ hasText: "假外壳验收设备" })).toContainText("已锁定")
  await expect(page.getByRole("navigation", { name: "主导航" }).getByRole("link", { name: "总览", exact: true })).toHaveAttribute("aria-current", "page")
  await expect(page.getByRole("navigation", { name: "主导航" }).getByRole("link", { name: "拓扑", exact: true })).toHaveCount(0)
  await page.getByLabel("视图", { exact: true }).selectOption(other.id)
  await expect(page).toHaveURL(new RegExp(`topology_view=${other.id}$`))
  await page.goBack()
  await expect(page.getByLabel("视图", { exact: true })).toHaveValue(home.id)
  await page.goForward()
  await expect(page.getByLabel("视图", { exact: true })).toHaveValue(other.id)
  await page.goBack()
  await expect(page.getByLabel("视图", { exact: true })).toHaveValue(home.id)
  await page.reload()
  await expect(page.getByLabel("视图", { exact: true })).toHaveValue(home.id)
  await tabs(page).getByRole("button", { name: "仪表盘", exact: true }).click()
  await expect(page.getByRole("heading", { name: "总览", exact: true })).toBeVisible()
  await tabs(page).getByRole("button", { name: "资源全景", exact: true }).click()
  await expect(page.getByRole("heading", { name: "资源全景", exact: true })).toBeVisible()
  await expect(page.getByRole("note")).toHaveCount(0)
  await expect(page.locator('[data-resource-id="host-a"]')).toHaveCount(0)
  expect(await (await page.request.get(`/api/topology/views/${home.id}`)).json()).toEqual(before)
  expect(await (await page.request.get("/api/dashboard/layout")).json()).toEqual(dashboard)
  expect(writes).toEqual([])
  await page.getByRole("link", { name: "打开资产管理", exact: true }).click()
  await expect(page.getByRole("heading", { name: "资产管理", exact: true })).toBeVisible()
  await page.goto("/#/topology?view=missing-fake-view")
  await expect(page.getByRole("alert")).toContainText("链接指定的拓扑视图不存在")
  await page.getByLabel("视图", { exact: true }).selectOption(home.id)
  await expect(page.locator(".react-flow__node").filter({ hasText: "假外壳验收设备" })).toBeVisible()
})

test("切换视角前确认未保存修改，取消保留草稿和拓扑编辑", async ({ page }) => {
  // 统一处理确认框：每次切换前设定本次回答，并检查恰好弹出一次（避免未用掉的处理器串到下一次）
  let answer: "accept" | "dismiss" = "dismiss"
  let dialogs = 0
  page.on("dialog", (dialog) => { dialogs += 1; void (answer === "accept" ? dialog.accept() : dialog.dismiss()) })
  const switchTo = async (label: string, reply: "accept" | "dismiss") => {
    answer = reply
    const before = dialogs
    await tabs(page).getByRole("button", { name: label, exact: true }).click()
    await expect.poll(() => dialogs - before).toBe(1)
  }
  await login(page)
  await page.goto("/#/overview")
  await page.getByRole("button", { name: "编辑布局", exact: true }).click()
  const down = page.getByRole("button", { name: "下移「资产采集状态」", exact: true })
  await (await down.isEnabled() ? down : page.getByRole("button", { name: "上移「资产采集状态」", exact: true })).click()
  await switchTo("拓扑视图", "dismiss")
  await expect(page).toHaveURL(/#\/overview$/)
  await expect(page.getByRole("button", { name: "保存布局", exact: true })).toBeVisible()
  await switchTo("拓扑视图", "accept")
  await expect(page.getByRole("heading", { name: "拓扑", exact: true })).toBeVisible()
  const asset = await create(page, "/api/assets", { name: "假外壳编辑设备", form: "physical", role: "server", lifecycle: "planned" })
  await page.reload()
  const node = page.locator(".react-flow__node").filter({ hasText: "假外壳编辑设备" })
  // 前序用例会保存重叠节点；使用正式支持的键盘选择，不依赖画布空白位置。
  await node.focus()
  await node.press("Enter")
  await page.getByRole("button", { name: "锁定位置", exact: true }).click()
  // 先确认拓扑已处于“有未保存修改”，再切换
  await expect(page.getByRole("button", { name: "保存布局", exact: true })).toBeEnabled()
  await switchTo("资源全景", "dismiss")
  await expect(page).toHaveURL(/view=topology/)
  await expect(node).toContainText("已锁定")
  await switchTo("仪表盘", "accept")
  await expect(page.getByText("上次编辑布局时有未保存的修改", { exact: false })).toBeVisible()
  const views = await (await page.request.get("/api/topology/views")).json()
  expect(views.every((v: { layout: Record<string, unknown> }) => !v.layout[asset.id])).toBeTruthy()
})

for (const role of ["operator", "viewer"]) {
  test(`${role} 正式三视角窄屏深色 200% 字号与权限`, async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 900 })
    await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" })
    await login(page, role)
    await page.goto("/#/overview")
    await page.addStyleTag({ content: "html { font-size:200% !important }" })
    await expect(page.getByRole("button", { name: "编辑布局", exact: true })).toHaveCount(role === "operator" ? 1 : 0)
    for (const label of ["资源全景", "拓扑视图", "仪表盘"]) {
      await tabs(page).getByRole("button", { name: label, exact: true }).click()
      await expect(tabs(page).getByRole("button", { name: label, exact: true })).toHaveAttribute("aria-pressed", "true")
      if (label === "拓扑视图") await expect(page.getByRole("button", { name: "重新扫描", exact: true })).toHaveCount(role === "operator" ? 1 : 0)
      expect(await page.locator("main").evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBeTruthy()
      expect(await page.getByRole("region", { name: "总览内容", exact: true }).evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBeTruthy()
    }
  })
}
