// 巡检报告：页面刚打开就点“立即巡检”时，晚到的报告列表不能把新报告换回旧的（产品核查 P2 走查中发现）。不连接任何设备。
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

test("刚打开报告页就立即巡检，显示的是新报告", async ({ page }) => {
  await login(page)
  // 先有一份旧报告
  expect((await page.request.post("/api/inspections", { headers: { "X-CSRF-Token": await csrf(page) } })).ok()).toBeTruthy()
  // 报告列表晚到：先从后端取到旧的列表，再晚 1.5 秒交给页面（请求已发出、响应还在路上）
  await page.route(/\/api\/inspections$/, async (route) => {
    if (route.request().method() !== "GET") return route.continue()
    const response = await route.fetch()
    await new Promise((r) => setTimeout(r, 1500))
    await route.fulfill({ response })
  })
  await page.goto("/#/reports")
  const created = page.waitForResponse((r) => r.request().method() === "POST" && /\/api\/inspections$/.test(r.url()))
  await page.getByRole("button", { name: "立即巡检" }).click()
  const id = (await (await created).json()).id
  await page.waitForResponse((r) => r.request().method() === "GET" && /\/api\/inspections$/.test(r.url()))
  await page.waitForTimeout(800)
  await expect(page.getByLabel("历史报告")).toHaveValue(id)
})
