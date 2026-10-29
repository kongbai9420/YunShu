// 4.4 P21 命令巡检板块：内置模板只读可查看、自定义模板拦截危险命令、新建巡检任务、执行记录与输出对比、窄屏。
// 浏览器测试环境没有可登录的设备：模板与任务走真实接口；执行记录与结果拦截接口给出伪造数据，只验证界面。
// 判定、执行与告警由 backend/tests/test_command_inspection.py 覆盖。不连接任何设备。
import { expect, test, type Page } from "@playwright/test"

const password = "fake-e2e-password-123"

async function csrf(page: Page) { return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value }
async function login(page: Page) {
  const r = await page.request.post("/api/auth/login", { data: { username: "admin", password } })
  if (!r.ok()) {
    expect((await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })).ok()).toBeTruthy()
    expect((await page.request.post("/api/auth/password", { data: { old_password: "admin123", new_password: password }, headers: { "X-CSRF-Token": await csrf(page) } })).ok()).toBeTruthy()
  }
}
async function post(page: Page, path: string, data: unknown) {
  const r = await page.request.post(path, { data, headers: { "X-CSRF-Token": await csrf(page) } })
  expect(r.ok(), await r.text()).toBeTruthy()
  return r.json()
}

test("巡检：模板、危险命令拦截、新建每周任务", async ({ page }) => {
  await login(page)
  await post(page, "/api/assets", { name: "fake-inspect-web", form: "physical", role: "server", lifecycle: "active" })
  await page.goto("/#/inspection")
  await expect(page.getByRole("heading", { name: "巡检", exact: true })).toBeVisible()

  // 内置模板：只读，可查看检查项
  await page.getByRole("group", { name: "巡检内容" }).getByRole("button", { name: "巡检模板" }).click()
  const linux = page.getByRole("region", { name: "模板 Linux 通用巡检" })
  await linux.getByRole("button", { name: "查看检查项" }).click()
  const view = page.getByRole("dialog", { name: "Linux 通用巡检" })
  await expect(view).toContainText("磁盘使用率")
  await expect(view).toContainText("数值≥85 警告，≥95 严重")
  await page.keyboard.press("Escape")
  await expect(view).toBeHidden()
  await expect(linux.getByRole("button", { name: "编辑" })).toHaveCount(0)

  // 自定义模板：危险命令拒绝保存
  await page.getByRole("button", { name: "新建自定义模板" }).click()
  const editor = page.getByRole("dialog", { name: "新建自定义模板" })
  const name = `fake-tpl-${Date.now()}`
  await editor.getByLabel("模板名称", { exact: true }).fill(name)
  await editor.getByLabel("名称", { exact: true }).fill("重启 nginx")
  await editor.getByLabel("命令", { exact: true }).fill("systemctl restart nginx")
  const save = editor.getByRole("button", { name: "保存模板" })
  await expect(save).toBeDisabled()
  await editor.getByLabel(/确认保存/).fill(name)
  await save.click()
  await expect(editor.getByRole("alert")).toContainText("危险操作")
  await editor.getByLabel("名称", { exact: true }).fill("nginx 状态")
  await editor.getByLabel("命令", { exact: true }).fill("systemctl is-active nginx")
  await editor.getByLabel("判定方式").selectOption("regex")
  await editor.getByLabel("正则表达式").fill("^active")
  await save.click()
  await expect(editor).toBeHidden()
  await expect(page.getByRole("heading", { name })).toBeVisible()

  // 新建每周任务
  await page.getByRole("group", { name: "巡检内容" }).getByRole("button", { name: "巡检任务" }).click()
  await page.getByRole("button", { name: "新建巡检任务" }).click()
  const job = page.getByRole("dialog", { name: "新建巡检任务" })
  await job.getByLabel("名称", { exact: true }).fill("fake-weekly-job")
  await job.getByLabel("执行方式").selectOption("weekly")
  await job.getByLabel("周四").check()
  await job.getByLabel("搜索设备").fill("fake-inspect-web")
  await job.getByRole("button", { name: "搜索", exact: true }).click()
  await job.getByLabel("fake-inspect-web", { exact: true }).check()
  await job.getByRole("button", { name: "保存", exact: true }).click()
  await expect(job).toBeHidden()
  const table = page.getByRole("table", { name: "巡检任务列表" })
  await expect(table).toContainText("fake-weekly-job")
  await expect(table).toContainText("每周一、四 08:00")

  // 旧的巡检报告链接打开每日巡检报告
  await page.goto("/#/reports")
  await expect(page.getByRole("heading", { name: "巡检报告", exact: true })).toBeVisible()
})

test("巡检：执行记录、结果明细与上一次输出对比，窄屏不横向滚动", async ({ page }) => {
  await login(page)
  const run = { id: "fake-run-1", name: "fake-daily", template_name: "Linux 通用巡检", triggered_by: "定时巡检", created_at: "2026-10-27T00:00:00+00:00",
    finished_at: "2026-10-27T00:01:00+00:00", status: "done", asset_total: 1, assets_done: 1, counts: { ok: 9, abnormal: 1, critical: 1, error: 1 } }
  await page.route("**/api/command-inspection/runs?*", (r) => r.fulfill({ json: { total: 1, items: [run] } }))
  await page.route("**/api/command-inspection/runs/fake-run-1*", (r) => r.fulfill({ json: { ...run, results: [
    { id: "res-1", asset_name: "fake-web-1", item_key: "disk", item_name: "磁盘使用率", command: "df -P", verdict: "abnormal", severity: "critical",
      detail: "1 项≥95：97% /var", exit_code: 0, observed_at: "2026-10-27T00:00:30+00:00", output_size: 20 },
    { id: "res-2", asset_name: "fake-web-1", item_key: "", item_name: "连接设备", command: "", verdict: "error", severity: "",
      detail: "连接失败：SSH 连接超时", exit_code: null, observed_at: "2026-10-27T00:00:30+00:00", output_size: 0 }] } }))
  await page.route("**/api/command-inspection/results/res-1", (r) => r.fulfill({ json: {
    asset_name: "fake-web-1", item_name: "磁盘使用率", command: "df -P", output: "40% /\n97% /var\n", verdict: "abnormal", detail: "", exit_code: 0,
    observed_at: "2026-10-27T00:00:30+00:00", previous: { output: "40% /\n61% /var\n", verdict: "ok", observed_at: "2026-10-26T00:00:30+00:00" } } }))

  await page.goto("/#/inspection")
  await page.getByRole("group", { name: "巡检内容" }).getByRole("button", { name: "执行记录" }).click()
  const detail = page.getByRole("table", { name: "巡检结果明细" })
  await expect(detail).toContainText("97% /var")
  await expect(detail).toContainText("异常 · 严重")
  await expect(detail).toContainText("连接失败")
  await detail.getByRole("button", { name: "查看" }).click()
  const out = page.getByRole("dialog", { name: "fake-web-1 · 磁盘使用率" })
  await expect(out.getByRole("region", { name: "本次输出" })).toContainText("97% /var")
  await expect(out.getByRole("region", { name: "上一次输出" })).toContainText("61% /var")
  await page.keyboard.press("Escape")
  await expect(out).toBeHidden()

  await page.setViewportSize({ width: 375, height: 812 })
  await expect(detail).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375)
})

test("巡检：cron 表达式任务，写错时提示", async ({ page }) => {
  await login(page)
  await post(page, "/api/assets", { name: "fake-inspect-cron", form: "physical", role: "server", lifecycle: "active" })
  await page.goto("/#/inspection")
  await page.getByRole("button", { name: "新建巡检任务" }).click()
  const job = page.getByRole("dialog", { name: "新建巡检任务" })
  await job.getByLabel("名称", { exact: true }).fill("fake-cron-job")
  await job.getByLabel("执行方式").selectOption("cron")
  await job.getByLabel("搜索设备").fill("fake-inspect-cron")
  await job.getByRole("button", { name: "搜索", exact: true }).click()
  await job.getByLabel("fake-inspect-cron", { exact: true }).check()
  const cron = job.getByLabel("cron 表达式（平台时区）")
  await cron.fill("0 8 * *")
  await job.getByRole("button", { name: "保存", exact: true }).click()
  await expect(job.getByRole("alert")).toContainText("5 段")
  await cron.fill("0 */6 * * *")
  await job.getByRole("button", { name: "保存", exact: true }).click()
  await expect(job).toBeHidden()
  await expect(page.getByRole("table", { name: "巡检任务列表" })).toContainText("cron · 0 */6 * * *")
})
