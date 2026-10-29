// 首次启动向导（4.1 P12）界面：浏览器测试后端已显式指定数据库、不会进入设置模式，这里拦截 /api/setup 接口验证页面流程；
// 设置模式的真实后端行为（门禁、口令、建表、PostgreSQL）由 backend/tests/test_setup_wizard.py 覆盖。
import { expect, test } from "@playwright/test"

test("首次启动向导：选择轻量版或测试 PostgreSQL 连接后保存", async ({ page }) => {
  const posted: { path: string; body: Record<string, unknown> }[] = []
  let applied = false
  await page.route("**/api/setup/status", (r) => r.fulfill({ json: applied ? { pending: false } : { pending: true, token_required: true } }))
  await page.route("**/api/setup/test", async (r) => {
    const body = r.request().postDataJSON()
    posted.push({ path: "test", body })
    await r.fulfill({ json: body.database === "fake-foreign"
      ? { ok: false, message: "连接成功（PostgreSQL 17.11），但库里已有其他表。请换一个空库，或先清空。", version: "17.11", state: "foreign" }
      : { ok: true, message: "连接成功（PostgreSQL 17.11），这是一个空库，保存后会创建数据表。", version: "17.11", state: "empty" } })
  })
  await page.route("**/api/setup/apply", async (r) => {
    posted.push({ path: "apply", body: r.request().postDataJSON() })
    applied = true
    await r.fulfill({ json: { ok: true, kind: "postgresql", message: "已保存，平台已就绪。首次登录 admin / admin123，按提示修改密码。" } })
  })

  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto("/")
  await expect(page.getByRole("heading", { name: "首次设置 · 选择数据库" })).toBeVisible()
  await expect(page.getByRole("radio", { name: /轻量版/ })).toBeChecked()
  await expect(page.getByRole("button", { name: "使用轻量版" })).toBeDisabled() // 远程访问需先填口令

  await page.getByRole("radio", { name: /标准版/ }).check()
  await page.getByLabel("主机").fill("db.example.invalid")
  await page.getByLabel("设置口令").fill("fake-setup-token")
  const save = page.getByRole("button", { name: "保存并使用" })
  await expect(save).toBeDisabled() // 测试通过前不能保存

  await page.getByLabel("数据库名").fill("fake-foreign")
  await page.getByRole("button", { name: "测试连接" }).click()
  await expect(page.getByText("库里已有其他表")).toBeVisible()
  await expect(save).toBeDisabled()

  await page.getByLabel("数据库名").fill("yunshu")
  await page.getByLabel("密码").fill("fake-db-pass")
  await page.getByRole("button", { name: "测试连接" }).click()
  await expect(page.getByText("这是一个空库")).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await save.click()
  await expect(page.getByText("已保存，平台已就绪")).toBeVisible()
  const sent = posted.find((p) => p.path === "apply")!.body
  expect(sent).toMatchObject({ kind: "postgresql", host: "db.example.invalid", database: "yunshu", port: 5432, token: "fake-setup-token" })

  await page.getByRole("button", { name: "去登录" }).click()
  await expect(page.getByRole("heading", { name: "登录云枢运维平台" })).toBeVisible()
})
