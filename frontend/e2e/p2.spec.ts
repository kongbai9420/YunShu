import { expect, test, type Page } from "@playwright/test"

const password = "fake-e2e-password-123"
test.describe.configure({ mode: "serial" })

async function login(page: Page, username = "admin", secret = password) {
  await page.goto("/#/monitoring")
  await page.getByRole("textbox", { name: "用户名", exact: true }).fill(username)
  await page.getByLabel(/^密码/).fill(secret)
  await page.getByRole("button", { name: "登录", exact: true }).click()
  await expect(page.getByRole("heading", { name: "登录云枢运维平台", exact: true })).toHaveCount(0)
}

async function post(page: Page, path: string, body: unknown) {
  const cookies = await page.context().cookies()
  const csrf = cookies.find((c) => c.name === "yunshu_csrf")!.value
  const response = await page.request.post(path, {
    data: body,
    headers: { "X-CSRF-Token": csrf },
  })
  expect(response.ok(), await response.text()).toBeTruthy()
  return response.json()
}

test("首次登录改密、六种指标质量与历史、采集任务和模板", async ({ page }) => {
  await login(page, "admin", "admin123")
  await expect(page.getByRole("heading", { name: "修改密码" })).toBeVisible()
  await page.getByLabel("当前密码").fill("admin123")
  await page.getByLabel(/^新密码/).fill(password)
  await page.getByLabel("再次输入新密码").fill(password)
  await page.getByRole("button", { name: "保存新密码" }).click()
  await expect(page.getByRole("heading", { name: "监控中心", exact: true })).toBeVisible()
  const table = page.getByRole("table", {
    name: "最新指标，空值用横线和质量状态表示",
  })
  await expect(table.getByText("23.5 %", { exact: true })).toBeVisible()
  for (const state of ["有效", "已过期", "不支持", "未配置", "采集失败", "估算值"])
    await expect(table.getByText(state, { exact: true })).toBeVisible()
  const failed = table.getByRole("row").filter({ hasText: "sensor.temp" })
  await expect(failed).toContainText("—")
  await expect(failed).not.toContainText("0 °C")
  await expect(page.getByRole("table", { name: "指标历史样本；最新的在前" })).toBeVisible()
  await page.screenshot({
    path: "test-results/p2-metrics-desktop.png",
    fullPage: true,
  })
  await page.getByRole("button", { name: "立即采集", exact: true }).click()
  await expect(page.getByText("已加入采集队列", { exact: false })).toBeVisible()
  await expect(page.getByText("采集成功", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "采集任务", exact: true }).click()
  await expect(page.getByRole("table", { name: "采集任务列表" })).toContainText("质量样本设备")
  await page.getByRole("button", { name: "设备模板", exact: true }).click()
  await expect(page.getByRole("heading", { name: "Debian / Ubuntu", exact: true })).toBeVisible()
  await expect(page.getByText("A 批 · 未实机验证", { exact: true })).toHaveCount(5)
})

test("网段预览不建资产、排除地址、发现确认防重复", async ({ page }) => {
  await login(page)
  await page.getByRole("button", { name: "网段发现", exact: true }).click()
  await page.getByText("新建发现任务", { exact: true }).click()
  await page.getByLabel(/^授权范围/).fill("192.0.2.0/29")
  await page.getByLabel("排除地址 / 网段").fill("192.0.2.2")
  await page.getByLabel(/^TCP 端口/).fill("443")
  await page.getByRole("button", { name: "预览范围（不联网）" }).click()
  await expect(page.getByText("将探测 5 个地址", { exact: false })).toBeVisible()
  await page.getByLabel("排除地址 / 网段").fill("192.0.2.2,192.0.2.3")
  await expect(page.getByRole("button", { name: "确认授权并开始发现" })).toHaveCount(0)
  await page.getByRole("button", { name: "预览范围（不联网）" }).click()
  await page.getByRole("button", { name: "确认授权并开始发现" }).click()
  await expect(page.getByText("发现完成", { exact: true })).toBeVisible()
  await expect(page.getByText("待确认", { exact: true })).toHaveCount(4)
  const candidate = page.locator("section").filter({
    has: page.getByRole("heading", { name: "192.0.2.1", exact: true }),
  })
  await candidate.getByRole("button", { name: "确认或关联资产" }).click()
  await candidate.getByLabel("设备名称").fill("离线发现测试资产")
  await candidate.getByRole("button", { name: "确认新建资产" }).click()
  await expect(candidate.getByText("已确认", { exact: true })).toBeVisible()
  await expect(candidate.getByRole("button", { name: "确认新建资产" })).toHaveCount(0)
  await page.reload()
  await page.getByRole("button", { name: "网段发现", exact: true }).click()
  await expect(page.getByText("已确认", { exact: true })).toBeVisible()
})

test("关闭 SNMP 后降级到 SSH、只读用户不能触发采集或发现", async ({ page, browser }) => {
  await login(page)
  const cred = await post(page, "/api/credentials", {
    alias: "fake-off",
    kind: "snmp_community",
    secret: "fake-snmp-off",
  })
  const asset = await post(page, "/api/assets", {
    name: "降级测试设备",
    form: "physical",
    role: "server",
    platform: "ubuntu",
    lifecycle: "planned",
    endpoints: [
      { address: "192.0.2.20", protocol: "snmp", credential_id: cred.id },
      { address: "192.0.2.20", protocol: "ssh", port: 22 },
    ],
  })
  await page.goto("/#/assets")
  await page.getByRole("button", { name: "降级测试设备", exact: true }).click()
  await page.getByRole("tab", { name: /^管理端点/ }).click()
  const snmpEndpointId = asset.endpoints.find((ep: { protocol: string }) => ep.protocol === "snmp").id
  const credentialPicker = page.locator(`#credential-${snmpEndpointId}`)
  const endpointCard = page.locator("li").filter({ has: credentialPicker })
  await credentialPicker.selectOption("")
  await endpointCard.getByRole("button", { name: "保存凭据关联", exact: true }).click()
  await expect(credentialPicker).toBeEnabled()
  await expect(endpointCard.getByRole("button", { name: "保存凭据关联", exact: true })).toBeDisabled()
  await credentialPicker.selectOption(cred.id)
  await endpointCard.getByRole("button", { name: "保存凭据关联", exact: true }).click()
  await expect(credentialPicker).toBeEnabled()
  await expect(endpointCard.getByRole("button", { name: "保存凭据关联", exact: true })).toBeDisabled()
  await page.getByRole("tab", { name: "指标与采集", exact: true }).click()
  await page.getByRole("button", { name: "立即采集", exact: true }).click()
  await expect(page.getByText("降级：SNMP 不可用，改用 SSH", { exact: false }).first()).toBeVisible()
  await post(page, "/api/users", {
    username: "fake-viewer",
    password,
    role: "viewer",
  })
  const context = await browser.newContext()
  const viewer = await context.newPage()
  await login(viewer, "fake-viewer")
  await viewer.getByLabel("当前密码").fill(password)
  await viewer.getByLabel(/^新密码/).fill("fake-viewer-changed-123")
  await viewer.getByLabel("再次输入新密码").fill("fake-viewer-changed-123")
  await viewer.getByRole("button", { name: "保存新密码" }).click()
  await expect(viewer.getByRole("heading", { name: "监控中心", exact: true })).toBeVisible()
  await expect(viewer.getByRole("button", { name: "立即采集", exact: true })).toHaveCount(0)
  await viewer.getByRole("button", { name: "网段发现", exact: true }).click()
  await expect(viewer.getByText("只读账号可以查看任务与候选", { exact: false })).toBeVisible()
  const cookies = await context.cookies()
  const csrf = cookies.find((c) => c.name === "yunshu_csrf")!.value
  expect(
    (
      await viewer.request.post(`/api/assets/${asset.id}/collect`, {
        headers: { "X-CSRF-Token": csrf },
      })
    ).status(),
  ).toBe(403)
  await context.close()
})

test("发现任务可暂停、继续和取消", async ({ page }) => {
  await login(page)
  await page.getByRole("button", { name: "网段发现", exact: true }).click()
  await page.getByText("新建发现任务", { exact: true }).click()
  await page.getByLabel(/^授权范围/).fill("192.0.2.0/24")
  await page.getByLabel("并发上限").fill("1")
  await page.getByLabel(/^TCP 端口/).fill("443")
  await page.getByRole("button", { name: "预览范围（不联网）" }).click()
  await page.getByRole("button", { name: "确认授权并开始发现" }).click()
  await page.getByRole("button", { name: "暂停发现", exact: true }).click()
  await expect(page.getByText("已暂停", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "继续发现", exact: true }).click()
  // 等页面拿到继续后的新代次再取消，否则会用旧代次提交而被拒绝（409）
  await expect(page.getByText("已暂停", { exact: true })).toHaveCount(0)
  await page.getByRole("button", { name: "取消发现", exact: true }).click()
  await expect(page.getByText("已取消", { exact: true })).toBeVisible()
})

test("375 宽深色、键盘焦点与 200% 字号下可操作", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" })
  await login(page)
  await expect(page.getByRole("heading", { name: "监控中心", exact: true })).toBeVisible()
  await page.getByRole("button", { name: "网段发现", exact: true }).click()
  await page.getByText("新建发现任务", { exact: true }).click()
  await page.getByLabel(/^授权范围/).fill("192.0.2.1")
  await page.getByLabel(/^授权范围/).press("Tab")
  // 「!」说明胶囊也在 Tab 顺序里，键盘用户聚焦即可看到详细说明
  await expect(page.locator('[data-help-pill="discovery.exclude"]')).toBeFocused()
  await page.keyboard.press("Tab")
  await expect(page.getByLabel("排除地址 / 网段")).toBeFocused()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
  expect(await page.locator("main").evaluate((main) => main.scrollWidth <= main.clientWidth)).toBeTruthy()
  await page.screenshot({
    path: "test-results/p2-narrow-dark-normal.png",
    fullPage: true,
  })
  await page.evaluate(() => {
    document.documentElement.style.fontSize = "200%"
  })
  await expect(page.getByRole("button", { name: "预览范围（不联网）" })).toBeVisible()
  await page.getByRole("button", { name: "预览范围（不联网）" }).click()
  await expect(page.getByText("将探测 1 个地址", { exact: false })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
  expect(await page.locator("main").evaluate((main) => main.scrollWidth <= main.clientWidth)).toBeTruthy()
  await page.screenshot({
    path: "test-results/p2-narrow-dark.png",
    fullPage: true,
  })
  await page.getByRole("button", { name: "指标与历史", exact: true }).click()
  await expect(page.getByRole("table", { name: "最新指标，空值用横线和质量状态表示" })).toBeVisible()
  expect(await page.locator("main").evaluate((main) => main.scrollWidth <= main.clientWidth)).toBeTruthy()
})

test("新增资产一次保存 SSH 端点和凭据，冲突可恢复、复用及仅建档", async ({ page }) => {
  await login(page)
  await page.goto("/#/assets")
  const start = async (name: string) => {
    await page.getByRole("button", { name: "新增资产", exact: true }).first().click()
    const dialog = page.getByRole("dialog", { name: "新增资产", exact: true })
    await dialog.getByLabel(/^名称/).fill(name)
    await dialog.getByLabel("生命周期", { exact: true }).selectOption("planned")
    return dialog
  }
  let dialog = await start("一次创建 SSH 设备")
  await dialog.getByLabel("平台", { exact: true }).fill("ubuntu")
  await dialog.getByLabel("设备地址", { exact: false }).fill("192.0.2.72")
  await dialog.getByLabel("端口", { exact: true }).fill("2222")
  await dialog.getByLabel("凭据别名", { exact: false }).fill("fake-inline-browser")
  await dialog.getByLabel("设备用户名", { exact: false }).fill("fake-monitor")
  await dialog.getByLabel("设备密码", { exact: false }).fill("fake-inline-password-123")
  await dialog.getByRole("button", { name: "创建", exact: true }).click()
  await page.getByRole("tab", { name: /^管理端点/ }).click()
  await expect(page.getByText("fake-inline-browser", { exact: false }).first()).toBeVisible()
  await expect(page.getByText("192.0.2.72", { exact: false })).toBeVisible()
  await page.reload()
  await page.getByRole("button", { name: "一次创建 SSH 设备", exact: true }).click()
  await page.getByRole("tab", { name: /^管理端点/ }).click()
  await expect(page.getByText("fake-inline-browser", { exact: false }).first()).toBeVisible()
  await page.getByRole("button", { name: "关闭详情", exact: true }).click()

  dialog = await start("复用 SSH 凭据设备")
  await dialog.getByLabel("设备地址", { exact: false }).fill("192.0.2.73")
  await dialog.getByLabel("凭据别名", { exact: false }).fill("fake-inline-browser")
  await dialog.getByLabel("设备用户名", { exact: false }).fill("fake-monitor")
  await dialog.getByLabel("设备密码", { exact: false }).fill("fake-inline-retry-123")
  await dialog.getByRole("button", { name: "创建", exact: true }).click()
  await expect(dialog.getByRole("alert")).toContainText("凭据别名已存在")
  await expect(dialog.getByLabel("设备密码", { exact: false })).toHaveValue("fake-inline-retry-123")
  await dialog.getByLabel("登录凭据", { exact: true }).selectOption("existing")
  await dialog.getByLabel("已有凭据", { exact: true }).selectOption({ label: "fake-inline-browser · fake-monitor" })
  await dialog.getByRole("button", { name: "创建", exact: true }).click()
  await expect(page.getByRole("heading", { name: "复用 SSH 凭据设备", exact: true })).toBeVisible()
  await page.getByRole("button", { name: "关闭详情", exact: true }).click()
  const credentials = await (await page.request.get("/api/credentials")).json()
  expect(credentials.filter((c: { alias: string }) => c.alias === "fake-inline-browser")).toHaveLength(1)

  dialog = await start("仅建档设备")
  await dialog.getByLabel("协议", { exact: true }).selectOption("")
  await dialog.getByRole("button", { name: "创建", exact: true }).click()
  await page.getByRole("tab", { name: /^管理端点/ }).click()
  await expect(page.getByText("还没有管理端点。", { exact: true })).toBeVisible()
})

test("新增连接表单支持私钥、协议切换、取消清密与窄屏大字号", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" })
  await login(page)
  await page.goto("/#/assets")
  await page.getByRole("button", { name: "新增资产", exact: true }).first().click()
  const dialog = page.getByRole("dialog", { name: "新增资产", exact: true })
  await dialog.getByLabel(/^名称/).fill("私钥设备")
  await dialog.getByLabel("设备地址", { exact: false }).fill("192.0.2.74")
  await dialog.getByLabel("设备地址", { exact: false }).press("Tab")
  await expect(dialog.getByLabel("端口", { exact: true })).toBeFocused()
  await dialog.getByLabel("认证方式", { exact: true }).selectOption("ssh_key")
  const fakeKey = "-----BEGIN FAKE PRIVATE KEY-----\nfake-multiline\n-----END FAKE PRIVATE KEY-----"
  await dialog.getByLabel("SSH 私钥", { exact: false }).fill(fakeKey)
  await expect(dialog.getByLabel("SSH 私钥", { exact: false })).toHaveValue(fakeKey)
  await page.evaluate(() => {
    document.documentElement.style.fontSize = "200%"
  })
  expect(await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBeTruthy()
  await dialog.getByLabel("协议", { exact: true }).selectOption("snmp")
  await expect(dialog.getByLabel("只读团体名", { exact: false })).toBeVisible()
  await dialog.getByLabel("协议", { exact: true }).selectOption("pve-api")
  await expect(dialog.getByLabel("端口", { exact: true })).toHaveValue("")
  await expect(dialog.getByText("留空使用 8006", { exact: true })).toBeVisible()
  await dialog.getByLabel("协议", { exact: true }).selectOption("ssh")
  await dialog.getByLabel("设备密码", { exact: false }).fill("fake-discarded-password")
  await page.screenshot({
    path: "test-results/p2-inline-narrow-dark.png",
    fullPage: true,
  })
  await dialog.getByRole("button", { name: "取消", exact: true }).click()
  await page.getByRole("button", { name: "新增资产", exact: true }).first().click()
  await expect(dialog.getByLabel("设备密码", { exact: false })).toHaveValue("")
  await expect(dialog.getByLabel("设备地址", { exact: false })).toHaveValue("")
})
