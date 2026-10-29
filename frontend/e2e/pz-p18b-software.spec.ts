// 4.3 P18b 软件与版本：SSH 端点开启软件包清单 → 立即读取 → 资产详情显示系统版本与软件包。设备是伪造的（e2e/server.py）。
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

test("软件与版本：读取软件包清单并显示系统版本", async ({ page }) => {
  await login(page)
  const headers = { "X-CSRF-Token": await csrf(page) }
  const cred = await (await page.request.post("/api/credentials", { data: { alias: "fake-ssh-p18b", kind: "password", username: "fake", secret: "fake-ssh-pass" }, headers })).json()
  const asset = await (await page.request.post("/api/assets", { headers, data: { name: "fake-debian-p18b", form: "physical", role: "server", platform: "debian", lifecycle: "active",
    endpoints: [{ address: "192.0.2.42", port: 22, protocol: "ssh", credential_id: cred.id }] } })).json()
  expect((await page.request.post(`/api/assets/${asset.id}/collect`, { headers })).ok()).toBeTruthy()

  await page.goto(`/#/assets?asset=${asset.id}`)
  const drawer = page.getByRole("dialog")
  await drawer.getByRole("tab", { name: /管理端点/ }).click()
  await drawer.getByRole("checkbox", { name: "每天读取软件包清单" }).click()
  await expect(drawer.getByRole("checkbox", { name: "每天读取软件包清单" })).toBeChecked()
  await drawer.getByRole("tab", { name: "软件与版本" }).click()
  await expect(drawer.getByRole("cell", { name: /Debian GNU\/Linux 12/ })).toBeVisible({ timeout: 30000 })
  await expect(drawer.getByRole("cell", { name: "Debian:12" })).toBeVisible()
  await drawer.getByRole("button", { name: "立即读取" }).click()
  await expect(drawer.getByText(/已安排读取/)).toBeVisible()
  await expect.poll(async () => (await (await page.request.get(`/api/assets/${asset.id}/software`)).json()).package_scans[0]?.count ?? 0, { timeout: 30000 }).toBe(2)
  await drawer.getByRole("tab", { name: "关联 0" }).click()
  await drawer.getByRole("tab", { name: "软件与版本" }).click()
  await expect(drawer.getByRole("heading", { name: "软件包清单（2 个）" })).toBeVisible()
  await drawer.getByLabel("查找软件包").fill("ssl")
  await drawer.getByRole("button", { name: "查找" }).click()
  await expect(drawer.getByRole("list", { name: "软件包" })).toContainText("libssl3（源码包 openssl）")
})
