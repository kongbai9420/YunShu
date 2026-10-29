import { expect, test, type Page } from "@playwright/test"

const adminPassword = "fake-e2e-password-123"
const initialPassword = "fake-p6-role-initial-123"
const rolePassword = "fake-p6-role-changed-123"
const roles = ["admin", "operator", "viewer"] as const
const routes = [
  ["overview", "总览"], ["assets", "资产管理"], ["import", "批量导入"],
  ["topology", "拓扑"], ["monitoring", "监控中心"], ["alerts", "告警中心"],
  ["operations", "运维操作"], ["settings", "平台设置"],
] as const

async function csrf(page: Page) {
  return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value
}

async function signIn(page: Page, username: string, password: string) {
  const response = await page.request.post("/api/auth/login", { data: { username, password } })
  expect(response.ok(), await response.text()).toBeTruthy()
  return response.json()
}

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext()
  const page = await context.newPage()
  let response = await page.request.post("/api/auth/login", { data: { username: "admin", password: adminPassword } })
  if (!response.ok()) {
    await signIn(page, "admin", "admin123")
    response = await page.request.post("/api/auth/password", {
      data: { old_password: "admin123", new_password: adminPassword },
      headers: { "X-CSRF-Token": await csrf(page) },
    })
    expect(response.ok()).toBeTruthy()
  }
  for (const role of ["operator", "viewer"]) {
    response = await page.request.post("/api/users", {
      data: { username: `fake-p6-${role}`, password: initialPassword, role },
      headers: { "X-CSRF-Token": await csrf(page) },
    })
    // Playwright 在测试失败后会重启 worker，但隔离服务仍在；保留已完成改密的假账号。
    if (response.status() === 409) continue
    expect(response.status(), await response.text()).toBe(201)
    const account = await browser.newContext()
    const accountPage = await account.newPage()
    const login = await signIn(accountPage, `fake-p6-${role}`, initialPassword)
    expect(login.user.require_password_change).toBe(true)
    response = await accountPage.request.post("/api/auth/password", {
      data: { old_password: initialPassword, new_password: rolePassword },
      headers: { "X-CSRF-Token": await csrf(accountPage) },
    })
    expect(response.ok()).toBeTruthy()
    await account.close()
  }
  await context.close()
})

for (const role of roles) {
  for (const scenario of ["desktop", "narrow-dark", "narrow-dark-200"] as const) {
    test(`${role} 八个页面权限与布局：${scenario}`, async ({ page }) => {
      const errors: string[] = []
      page.on("pageerror", (error) => errors.push(error.message))
      const editable = role !== "viewer"
      await page.setViewportSize({ width: scenario === "desktop" ? 1440 : 375, height: 900 })
      await page.emulateMedia({ colorScheme: scenario === "desktop" ? "light" : "dark" })
      await signIn(page, role === "admin" ? "admin" : `fake-p6-${role}`, role === "admin" ? adminPassword : rolePassword)
      const headers = { "X-CSRF-Token": await csrf(page) }
      // 真实账号向后端请求，验证管理权限与查看/写入边界；不模拟角色响应。
      expect((await page.request.get("/api/users")).status()).toBe(role === "admin" ? 200 : 403)
      expect((await page.request.get("/api/backups")).status()).toBe(role === "admin" ? 200 : 403)
      const create = await page.request.post("/api/assets", {
        data: { name: `fake-p6-${role}-${scenario}`, form: "physical", role: "server" }, headers,
      })
      expect(create.status(), await create.text()).toBe(editable ? 201 : 403)
      if (role !== "admin") {
        expect((await page.request.post("/api/control/owner", {
          data: { owner: "legacy", revision: 0, confirm: "切回旧控制器", checklist: [], note: "fake permission check" }, headers,
        })).status()).toBe(403)
      }
      for (const [route, title] of routes) {
        await page.goto(`/#/${route}`)
        await expect(page.getByRole("heading", { name: title, level: 1, exact: true })).toBeVisible()
        if (scenario === "narrow-dark-200")
          await page.addStyleTag({ content: "html { font-size:200% !important }" })
        // 横向不溢出仍可能发生绝对定位品牌覆盖按钮，单独核对顶栏点击区域。
        expect(await page.locator("header").first().evaluate((header) => {
          const rects = Array.from(header.querySelectorAll("a,button"))
            .map((el) => el.getBoundingClientRect()).filter((r) => r.width > 0 && r.height > 0)
          return rects.every((a, i) => rects.slice(i + 1).every((b) =>
            Math.min(a.right, b.right) - Math.max(a.left, b.left) <= 1 ||
            Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) <= 1))
        }), `${role}/${scenario}/${route} 顶栏控件不能重叠`).toBeTruthy()
        if (route === "overview")
          await expect(page.getByRole("button", { name: "编辑布局", exact: true })).toHaveCount(editable ? 1 : 0)
        if (route === "assets")
          await expect(page.getByRole("button", { name: "新增资产", exact: true })).toHaveCount(editable ? 1 : 0)
        if (route === "import")
          await expect(page.locator('input[type="file"]')).toHaveCount(editable ? 1 : 0)
        if (route === "topology")
          await expect(page.getByRole("button", { name: "重新扫描", exact: true })).toHaveCount(editable ? 1 : 0)
        if (route === "monitoring" && role === "viewer") {
          await expect(page.getByRole("button", { name: "立即采集", exact: true })).toHaveCount(0)
          await page.getByRole("button", { name: "网段发现", exact: true }).click()
          await expect(page.getByText("只读账号可以查看任务与候选", { exact: false })).toBeVisible()
        }
        if (route === "operations" && role === "viewer")
          await expect(page.getByRole("button", { name: "强制断电", exact: true })).toBeDisabled()
        if (route === "alerts") {
          await page.getByRole("button", { name: "告警规则", exact: true }).click()
          await expect(page.getByRole("button", { name: "新增告警规则", exact: true })).toHaveCount(editable ? 1 : 0)
        }
        if (route === "settings") {
          await expect(page.getByRole("tab", { name: "用户与角色", exact: true })).toHaveCount(role === "admin" ? 1 : 0)
          await expect(page.getByRole("tab", { name: "备份", exact: true })).toHaveCount(role === "admin" ? 1 : 0)
          await expect(page.getByRole("button", { name: "添加凭据", exact: true })).toHaveCount(editable ? 1 : 0)
        }
        await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), { message: `${role}/${scenario}/${route} 页面不能溢出` }).toBeTruthy()
        await expect.poll(() => page.locator("main").evaluate((el) => el.scrollWidth <= el.clientWidth + 1), { message: `${role}/${scenario}/${route} 主内容不能溢出` }).toBeTruthy()
      }
      expect(errors).toEqual([])
    })
  }
}
