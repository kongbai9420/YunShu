import { expect, test, type Page } from "@playwright/test"

test.describe.configure({ mode: "serial" })
const password = "fake-e2e-password-123"
async function csrf(page: Page) {
  return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value
}
async function login(page: Page) {
  let response = await page.request.post("/api/auth/login", {
    data: { username: "admin", password },
  })
  if (!response.ok()) {
    response = await page.request.post("/api/auth/login", {
      data: { username: "admin", password: "admin123" },
    })
    expect(response.ok()).toBeTruthy()
    response = await page.request.post("/api/auth/password", {
      data: { old_password: "admin123", new_password: password },
      headers: { "X-CSRF-Token": await csrf(page) },
    })
    expect(response.ok()).toBeTruthy()
  }
  await page.goto("/#/alerts")
  await expect(page.getByRole("heading", { name: "告警中心", exact: true })).toBeVisible()
}

test("告警规则与通知渠道保存、冲突保留输入、测试入队及维护窗口", async ({ page }) => {
  await login(page)
  await page.getByRole("button", { name: "通知与投递", exact: true }).click()
  await page.getByRole("button", { name: "新增通知渠道", exact: true }).click()
  await page.getByLabel("渠道名称", { exact: true }).fill("离线验收邮件")
  await page.getByLabel("SMTP 主机", { exact: true }).fill("smtp.example.invalid")
  await page.getByLabel("发件人地址", { exact: true }).fill("sender@example.invalid")
  await page.getByLabel("收件人（逗号分隔）", { exact: true }).fill("receiver@example.invalid")
  await page.getByLabel("SMTP 用户名（留空保留）", { exact: true }).fill("fake-smtp-user")
  await page.getByLabel("SMTP 密码 / 授权码（留空保留）", { exact: true }).fill("fake-smtp-password")
  await expect(page.getByLabel("启用自动通知", { exact: true })).not.toBeChecked()
  await page.getByRole("button", { name: "保存渠道", exact: true }).click()
  await expect(page.getByRole("heading", { name: "离线验收邮件", exact: true })).toBeVisible()
  await page.getByRole("button", { name: "编辑 离线验收邮件", exact: true }).click()
  await expect(page.getByLabel("SMTP 密码 / 授权码（留空保留）", { exact: true })).toHaveValue("")
  await page.getByRole("button", { name: "取消编辑", exact: true }).click()

  // e2e/server.py 注入离线发送器，真实接口 / 队列 / Worker 链路不会外发。
  await page.getByRole("button", { name: "测试通知", exact: true }).click()
  await expect(page.getByText(/尚未保存的表单内容不会使用/)).toBeVisible()
  await page.getByRole("button", { name: "确认测试通知", exact: true }).click()
  await expect(page.getByText("测试通知已加入投递队列，请查看投递状态", { exact: true })).toBeVisible()
  await expect(page.getByRole("table", { name: "通知投递记录" })).toContainText("服务已接受")

  await page.getByRole("button", { name: "告警规则", exact: true }).click()
  await page.getByRole("button", { name: "新增告警规则", exact: true }).click()
  await page.getByLabel("规则名称", { exact: true }).fill("离线 CPU 规则")
  await page.getByLabel("触发阈值", { exact: true }).fill("90")
  await page.getByLabel("恢复阈值", { exact: true }).fill("80")
  await page.getByRole("button", { name: "保存规则", exact: true }).click()
  await expect(page.getByRole("heading", { name: "离线 CPU 规则", exact: true })).toBeVisible()
  await page.getByRole("button", { name: "编辑 离线 CPU 规则", exact: true }).click()
  await page.getByLabel("规则名称", { exact: true }).fill("保留未保存的输入")
  await page.route("**/api/alerts/rules/*", async (route) => {
    if (route.request().method() === "PATCH")
      await route.fulfill({
        status: 409,
        json: { detail: "规则已被其他用户修改，请刷新后重试" },
      })
    else await route.continue()
  })
  await page.getByRole("button", { name: "保存规则", exact: true }).click()
  await expect(page.getByRole("alert")).toContainText("输入内容已保留")
  await expect(page.getByLabel("规则名称", { exact: true })).toHaveValue("保留未保存的输入")
  await page.getByRole("button", { name: "取消编辑", exact: true }).click()

  await page.getByRole("button", { name: "维护窗口", exact: true }).click()
  await page.getByRole("button", { name: "新增维护窗口", exact: true }).click()
  await page.getByLabel("维护名称", { exact: true }).fill("离线维护演练")
  // 不选资产也不填标签：不勾选“对全部资产生效”就不保存
  await page.getByRole("button", { name: "保存维护窗口", exact: true }).click()
  await expect(page.getByText("请选择维护资产或填写标签")).toBeVisible()
  await page.getByLabel("对全部资产生效", { exact: true }).check()
  await page.getByRole("button", { name: "保存维护窗口", exact: true }).click()
  await expect(page.getByRole("heading", { name: "离线维护演练", exact: true })).toBeVisible()
  await expect(page.getByText("全部资产", { exact: true })).toBeVisible()
  // 修改：改名并限定标签；没动时间时，保存后时间不变（以前按本地时间解析接口时间，会挪 8 小时）
  const period = await page.getByText("→").innerText()
  await page.getByRole("button", { name: "修改", exact: true }).click()
  await expect(page.getByLabel("对全部资产生效", { exact: true })).toBeChecked()
  await page.getByLabel("维护名称", { exact: true }).fill("离线维护演练（已改）")
  await page.getByLabel("限定标签（逗号分隔）", { exact: true }).fill("fake-rack")
  await expect(page.getByLabel("对全部资产生效", { exact: true })).toHaveCount(0)
  await page.getByRole("button", { name: "保存维护窗口", exact: true }).click()
  await expect(page.getByRole("heading", { name: "离线维护演练（已改）", exact: true })).toBeVisible()
  await expect(page.getByText("资产不限 · 标签 fake-rack")).toBeVisible()
  await expect(page.getByText("→")).toHaveText(period)
  await page.getByRole("button", { name: "删除维护窗口", exact: true }).click()
  await page.getByRole("button", { name: "确认删除维护窗口", exact: true }).click()
  await expect(page.getByRole("heading", { name: "离线维护演练（已改）", exact: true })).toHaveCount(0)
})

test("告警状态与确认、静默操作，以及只读权限和窄屏放大", async ({ page }) => {
  await login(page)
  const now = new Date().toISOString()
  const alert = {
    id: "fake-alert-ui",
    rule_id: "fake-rule",
    asset_id: "fake-asset",
    asset_name: "离线测试服务器",
    metric_key: "cpu.load",
    state: "active",
    severity: "warning",
    title: "CPU 使用率过高",
    value: 95,
    unit: "%",
    threshold: 90,
    first_seen_at: now,
    last_seen_at: now,
    activated_at: now,
    resolved_at: null,
    acknowledged_by: null as string | null,
    silenced_until: null as string | null,
    suppression_reason: null,
  }
  await page.route("**/api/alerts?*", (route) => route.fulfill({ json: { items: [alert], total: 1 } }))
  await page.route("**/api/alerts/fake-alert-ui/action", async (route) => {
    const body = route.request().postDataJSON()
    if (body.action === "acknowledge") {
      alert.acknowledged_by = "fake-user"
      alert.state = "acknowledged"
    } else if (body.action === "silence") {
      expect(body.minutes).toBe(60)
      alert.silenced_until = now
      alert.state = "silenced"
    } else {
      alert.silenced_until = null
      alert.state = "acknowledged"
    }
    await route.fulfill({ json: alert })
  })
  await page.getByRole("button", { name: "刷新告警", exact: true }).click()
  await expect(page.getByRole("table", { name: "当前告警列表" })).toContainText("CPU 使用率过高")
  await page.getByRole("button", { name: "确认告警", exact: true }).click()
  await expect(page.getByRole("table", { name: "当前告警列表" })).toContainText("已确认")
  await page.getByRole("button", { name: "静默 1 小时", exact: true }).click()
  await expect(page.getByRole("button", { name: "解除静默", exact: true })).toBeVisible()
  await page.getByRole("button", { name: "解除静默", exact: true }).click()
  await expect(page.getByRole("button", { name: "静默 1 小时", exact: true })).toBeVisible()

  await page.setViewportSize({ width: 375, height: 820 })
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" })
  await page.addStyleTag({ content: "html {font-size:200% !important}" })
  for (const name of ["告警规则", "通知与投递", "维护窗口", "当前告警"]) {
    await page.getByRole("button", { name, exact: true }).click()
    if (name === "告警规则") await page.getByRole("button", { name: "新增告警规则", exact: true }).click()
    if (name === "通知与投递") await page.getByRole("button", { name: "新增通知渠道", exact: true }).click()
    if (name === "维护窗口") await page.getByRole("button", { name: "新增维护窗口", exact: true }).click()
    await expect.poll(() => page.locator("main").evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBeTruthy()
  }
  await page.screenshot({
    path: "test-results/p4-alerts-narrow-dark.png",
    fullPage: true,
  })
  // 权限来自真实 auth 状态；仅这次浏览器响应模拟 viewer，不修改数据库账户。
  await page.route("**/api/auth/me", async (route) => {
    const response = await route.fetch(),
      body = await response.json()
    body.user.role = "viewer"
    await route.fulfill({ response, json: body })
  })
  await page.reload()
  await expect(page.getByText("当前账户为只读，可查看告警与配置。", { exact: true })).toBeVisible()
  await expect(page.getByRole("button", { name: "确认告警", exact: true })).toHaveCount(0)
  await page.getByRole("button", { name: "告警规则", exact: true }).click()
  await expect(page.getByRole("button", { name: "新增告警规则", exact: true })).toHaveCount(0)
})
