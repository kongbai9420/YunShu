// 已经在资产页时，只改链接里的 #/assets?asset=<id> 也要打开对应资产的详情（以前要刷新页面才行）。不连接任何设备。
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
async function asset(page: Page, name: string) {
  const r = await page.request.post("/api/assets", { data: { name, form: "physical", role: "server", lifecycle: "active" }, headers: { "X-CSRF-Token": await csrf(page) } })
  expect(r.ok()).toBeTruthy()
  return r.json()
}

test("资产页内只改 ?asset= 链接也会打开对应资产", async ({ page }) => {
  await login(page)
  const a = await asset(page, "fake-link-alpha")
  const b = await asset(page, "fake-link-beta")
  await page.goto(`/#/assets?asset=${a.id}`)
  await expect(page.getByRole("dialog")).toContainText("fake-link-alpha")
  await page.evaluate((id) => { window.location.hash = `#/assets?asset=${id}` }, b.id)
  await expect(page.getByRole("dialog")).toContainText("fake-link-beta")
})
