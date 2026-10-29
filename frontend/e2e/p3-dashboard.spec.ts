import { expect, test, type Browser, type Page } from "@playwright/test"

// 在 p2.spec.ts 之后运行：初始管理员的密码已改成下面这个伪造值
const password = "fake-e2e-password-123"
test.describe.configure({ mode: "serial" })

async function login(page: Page) {
  await page.goto("/#/overview")
  await page.getByRole("textbox", { name: "用户名", exact: true }).fill("admin")
  await page.getByLabel(/^密码/).fill(password)
  await page.getByRole("button", { name: "登录", exact: true }).click()
  await expect(page.getByRole("heading", { name: "总览", exact: true })).toBeVisible()
}

async function newPage(browser: Browser) {
  const ctx = await browser.newContext({ baseURL: "http://127.0.0.1:5185" })
  return ctx.newPage()
}

test("总览小组件来自真实数据，编辑后保存并带修订号", async ({ page }) => {
  await login(page)
  await expect(page.getByRole("note")).toHaveCount(0) // 不再是演示数据
  const health = page.getByRole("region", { name: "资产采集状态" })
  await expect(health).toBeVisible()
  await expect(page.getByText("使用默认布局（还没有人保存过）")).toBeVisible()

  await page.getByRole("button", { name: "编辑布局", exact: true }).click()
  await page.getByRole("button", { name: "下移「资产采集状态」" }).click()
  await page.getByLabel("添加关注资产").fill("质量样本")
  await page.getByRole("button", { name: "加入", exact: true }).first().click()
  const watch = page.getByRole("region", { name: "关注列表" })
  await expect(watch).toContainText("质量样本设备")
  await page.getByRole("button", { name: "保存布局", exact: true }).click()
  await expect(page.getByText("已保存为修订 1")).toBeVisible()
  await page.reload()
  await expect(page.getByText(/修订 1 · /)).toBeVisible()
  await expect(page.getByRole("region", { name: "关注列表" })).toContainText("质量样本设备")
})

test("两个窗口同时改布局：后保存的看到冲突并自己选择", async ({ page, browser }) => {
  await login(page)
  const other = await newPage(browser)
  await login(other)

  await page.getByRole("button", { name: "编辑布局", exact: true }).click()
  await other.getByRole("button", { name: "编辑布局", exact: true }).click()
  await other.getByRole("button", { name: "上移「资产采集状态」" }).click()
  await other.getByRole("button", { name: "保存布局", exact: true }).click()
  await expect(other.getByText("已保存为修订 2")).toBeVisible()

  await page.getByRole("button", { name: "下移「告警与采集问题」" }).click()
  await page.getByRole("button", { name: "保存布局", exact: true }).click()
  const conflict = page.getByRole("alert").filter({ hasText: "保存未完成" })
  await expect(conflict).toContainText("修订 2")
  await expect(conflict).toContainText("系统不会自动覆盖")
  await page.getByRole("button", { name: "用我的版本覆盖" }).click()
  await expect(page.getByText("已保存为修订 3")).toBeVisible()
  await other.close()
})

test("编辑中离开页面，回来可以恢复未保存的修改", async ({ page }) => {
  await login(page)
  await page.getByRole("button", { name: "编辑布局", exact: true }).click()
  await page.getByRole("button", { name: "下移「资产采集状态」" }).click()
  await page.goto("/#/assets")
  await expect(page.getByRole("heading", { name: "资产管理", exact: true })).toBeVisible()
  await page.goto("/#/overview")
  await expect(page.getByText("上次编辑布局时有未保存的修改")).toBeVisible()
  await page.getByRole("button", { name: "恢复并继续编辑" }).click()
  await expect(page.getByRole("button", { name: "保存布局", exact: true })).toBeVisible()
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await expect(page.getByText("已取消编辑，布局未改变")).toBeVisible()
  await expect(page.getByText("上次编辑布局时有未保存的修改")).toHaveCount(0)
})
