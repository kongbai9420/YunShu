import { expect, test, type Page } from "@playwright/test"

const password = "fake-e2e-password-123"
const initial = "fake-p7-initial-123"
const rolePassword = "fake-p7-changed-123"
const proto = "/#/overview-prototype?view=resources"

async function csrf(page: Page) {
  return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value
}

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext()
  const page = await context.newPage()
  let response = await page.request.post("/api/auth/login", { data: { username: "admin", password } })
  if (!response.ok()) {
    expect((await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })).ok()).toBeTruthy()
    expect((await page.request.post("/api/auth/password", { data: { old_password: "admin123", new_password: password },
      headers: { "X-CSRF-Token": await csrf(page) } })).ok()).toBeTruthy()
  }
  for (const role of ["operator", "viewer"]) {
    response = await page.request.post("/api/users", { data: { username: `fake-p7-${role}`, password: initial, role },
      headers: { "X-CSRF-Token": await csrf(page) } })
    if (response.status() === 409) continue
    expect(response.status()).toBe(201)
    const account = await browser.newContext()
    const member = await account.newPage()
    expect((await member.request.post("/api/auth/login", { data: { username: `fake-p7-${role}`, password: initial } })).ok()).toBeTruthy()
    expect((await member.request.post("/api/auth/password", { data: { old_password: initial, new_password: rolePassword },
      headers: { "X-CSRF-Token": await csrf(member) } })).ok()).toBeTruthy()
    await account.close()
  }
  await context.close()
})

async function login(page: Page, role = "admin") {
  expect((await page.request.post("/api/auth/login", { data: {
    username: role === "admin" ? "admin" : `fake-p7-${role}`, password: role === "admin" ? password : rolePassword,
  } })).ok()).toBeTruthy()
  await page.goto(proto)
  await expect(page.getByRole("heading", { name: "总览原型", exact: true })).toBeVisible()
}

test("原型分类、三层排序、键盘与拖动、演练均不写真实业务", async ({ page }) => {
  await login(page)
  // 侧栏不再显示原型入口，直接打开地址仍可用（桌面宽度下侧栏可见）
  await expect(page.getByRole("navigation").getByRole("link", { name: "总览", exact: true })).toHaveCount(1)
  await expect(page.getByRole("navigation").getByRole("link", { name: "总览原型" })).toHaveCount(0)
  const errors: string[] = []
  const writes: string[] = []
  page.on("pageerror", (error) => errors.push(error.message))
  page.on("request", (request) => {
    if (request.url().includes("/api/") && !["GET", "HEAD", "OPTIONS"].includes(request.method())) writes.push(request.url())
  })
  const before = await (await page.request.get("/api/dashboard/layout")).json()
  const relations = await (await page.request.get("/api/relations")).json()
  await expect(page.getByRole("note")).toContainText("本页为总览原型")
  await expect(page.getByRole("article", { name: "演示待关联 VM", exact: true })).toBeVisible()
  await page.getByLabel("类别 / 形态").selectOption("UPS")
  await expect(page.locator("[data-resource-id]")).toHaveCount(1)
  await expect(page.getByRole("article", { name: "演示 UPS", exact: true })).toBeVisible()
  await page.getByLabel("类别 / 形态").selectOption("all")
  await page.getByRole("button", { name: "编辑排序", exact: true }).click()
  await page.getByRole("button", { name: "上移网络设备", exact: true }).click()
  await page.getByRole("button", { name: "上移演示主机 B", exact: true }).click()
  const compute = page.getByRole("region", { name: "物理服务器 / 宿主", exact: true })
  // section 带 aria-label 是 region；只核对该层，不把下挂子卡算成顶层卡。
  await expect(compute.locator(":scope > div > [data-sort-id]").first()).toHaveAttribute("data-sort-id", "host-b")
  const handle = page.getByRole("button", { name: "拖动演示 Web VM", exact: true })
  await handle.focus()
  await page.keyboard.press("Space")
  await expect(handle).toHaveAttribute("aria-pressed", "true")
  await page.keyboard.press("ArrowDown")
  await expect(page.getByText("移到演示 DNS VM的位置", { exact: true })).toHaveCount(1)
  await page.keyboard.press("Space")
  await expect(page.locator('[data-resource-id="host-a"] [data-sort-id]').first()).toHaveAttribute("data-sort-id", "vm-dns")
  await page.getByRole("button", { name: "取消排序", exact: true }).click()
  await expect(compute.locator(":scope > div > [data-sort-id]").first()).toHaveAttribute("data-sort-id", "host-a")
  await page.getByRole("button", { name: "编辑排序", exact: true }).click()
  await page.getByLabel("搜索演示资源").fill("演示主机")
  await page.getByRole("button", { name: "上移演示主机 B", exact: true }).click()
  await page.getByLabel("搜索演示资源").fill("")
  await expect(page.getByRole("article", { name: "演示备用主机", exact: true })).toBeVisible()
  const source = page.getByRole("button", { name: "拖动演示核心交换机", exact: true })
  const target = page.getByRole("button", { name: "拖动演示家庭路由器", exact: true })
  await target.scrollIntoViewIfNeeded()
  const a = (await source.boundingBox())!
  const b = (await target.boundingBox())!
  await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2)
  await page.mouse.down()
  await page.mouse.move(a.x + a.width / 2 + 8, a.y + a.height / 2, { steps: 3 })
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 16 })
  await page.mouse.up()
  await expect(page.getByRole("region", { name: "网络设备", exact: true }).locator(":scope > div > [data-sort-id]").first()).toHaveAttribute("data-sort-id", "router")
  await page.getByRole("button", { name: "保存演示排序", exact: true }).click()
  await expect(page.getByRole("status", { name: "原型反馈" })).toContainText("未写入数据库")
  await page.getByRole("button", { name: "长条", exact: true }).click()
  await expect(compute.locator(":scope > div > [data-sort-id]").first()).toHaveAttribute("data-sort-id", "host-b")
  await page.getByRole("article", { name: "演示主机 A", exact: true }).getByRole("button", { name: "电源操作（演练）", exact: true }).click()
  const dialog = page.getByRole("dialog")
  await expect(dialog).toContainText("演示 Web VM")
  await dialog.getByLabel("操作类型").selectOption("off")
  await expect(dialog.getByRole("button", { name: "完成确认演练" })).toBeDisabled()
  await dialog.getByLabel(/输入“演示主机 A”/).fill("演示主机 A")
  await dialog.getByRole("button", { name: "完成确认演练" }).click()
  await expect(page.getByRole("status", { name: "原型反馈" })).toContainText("设备状态未改变")
  expect(await (await page.request.get("/api/dashboard/layout")).json()).toEqual(before)
  expect(await (await page.request.get("/api/relations")).json()).toEqual(relations)
  expect(writes).toEqual([])
  await page.reload()
  await expect(compute.locator(":scope > div > [data-sort-id]").first()).toHaveAttribute("data-sort-id", "host-a")
  expect(errors).toEqual([])
})

for (const role of ["operator", "viewer"]) {
  test(`${role} 三视角、URL 恢复和窄屏大字号`, async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 900 })
    await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" })
    await login(page, role)
    await page.addStyleTag({ content: "html { font-size:200% !important }" })
    await expect(page.getByRole("button", { name: "编辑排序", exact: true })).toHaveCount(role === "viewer" ? 0 : 1)
    for (const [label, view] of [["仪表盘", "dashboard"], ["资源全景", "resources"], ["拓扑视图", "topology"]]) {
      await page.getByRole("group", { name: "总览类型", exact: true }).getByRole("button", { name: label, exact: true }).click()
      await expect(page).toHaveURL(new RegExp(`view=${view}$`))
      await expect(page.getByRole("button", { name: label, exact: true }).first()).toHaveAttribute("aria-pressed", "true")
      expect(await page.locator("main").evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBeTruthy()
    }
    await page.reload()
    await expect(page.getByRole("region", { name: "演示拓扑", exact: true })).toBeVisible()
    await page.goBack()
    await expect(page.getByRole("region", { name: "资源全景", exact: true })).toBeVisible()
    // reload 会清除临时 style；正文可读性也必须在 200% 字号下核对。
    await page.addStyleTag({ content: "html { font-size:200% !important }" })
    if (role === "viewer") {
      await expect(page.getByRole("article", { name: "演示主机 A", exact: true }).getByRole("button", { name: "电源操作（演练）", exact: true })).toBeDisabled()
    }
    const resourceTitle = page.getByRole("heading", { name: "演示数据库 VM", exact: true })
    await resourceTitle.scrollIntoViewIfNeeded()
    expect((await resourceTitle.boundingBox())!.width).toBeGreaterThan(140)
    await page.screenshot({ path: `test-results/p7-${role}-375-dark-200.png`, fullPage: true })
    await page.goto("/#/topology")
    await expect(page.getByRole("heading", { name: "拓扑", exact: true })).toBeVisible()
    await expect(page.getByRole("note")).toHaveCount(0)
    await page.goto("/#/overview")
    await expect(page.getByRole("heading", { name: "总览", exact: true })).toBeVisible()
    await expect(page.getByRole("note")).toHaveCount(0)
  })
}
