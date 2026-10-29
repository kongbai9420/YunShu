// 维护中的预计结束时间：改为维护中时出现并默认 7 天后，保存后详情里显示；改回在用后消失。不连接任何设备。
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

test("改为维护中时填预计结束时间，详情里显示", async ({ page }) => {
  await login(page)
  const r = await page.request.post("/api/assets", { data: { name: "fake-maint-ui", form: "physical", role: "server" }, headers: { "X-CSRF-Token": await csrf(page) } })
  const asset = await r.json()
  await page.goto(`/#/assets?asset=${asset.id}`)
  const drawer = page.getByRole("dialog")
  await drawer.getByRole("tab", { name: "编辑", exact: true }).click()
  await expect(drawer.getByRole("textbox", { name: "预计结束时间", exact: true })).toHaveCount(0)
  await drawer.getByLabel("生命周期", { exact: true }).selectOption("maintenance")
  const until = drawer.getByRole("textbox", { name: "预计结束时间", exact: true })
  const value = await until.inputValue()
  const days = (new Date(value).getTime() - Date.now()) / 86400000
  expect(days).toBeGreaterThan(6.9)
  expect(days).toBeLessThanOrEqual(7)
  await until.fill("2099-01-02T03:04")
  await drawer.getByRole("button", { name: "保存修改", exact: true }).click()
  await expect(drawer).toContainText("预计结束：")
  const saved = await (await page.request.get(`/api/assets/${asset.id}`)).json()
  expect(new Date(saved.maintenance_until.endsWith("Z") || saved.maintenance_until.includes("+") ? saved.maintenance_until : `${saved.maintenance_until}Z`).getTime())
    .toBe(new Date("2099-01-02T03:04").getTime())
  // 再打开编辑页，显示的还是这个时间（没有按时区挪动）
  await expect(drawer.getByRole("textbox", { name: "预计结束时间", exact: true })).toHaveValue("2099-01-02T03:04")
  await drawer.getByLabel("生命周期", { exact: true }).selectOption("active")
  await expect(drawer.getByRole("textbox", { name: "预计结束时间", exact: true })).toHaveCount(0)
  await drawer.getByRole("button", { name: "保存修改", exact: true }).click()
  await expect(drawer).not.toContainText("预计结束：")
})
