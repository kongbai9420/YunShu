// 4.3 P18d 比对与处置：导入离线包 → 伪造 iDRAC9 固件命中 CVE-2018-1243 → 漏洞列表显示影响本地 → 修复与缓解 → 处置为不受影响 → 资产详情的漏洞页签同步。
import { expect, test, type Page } from "@playwright/test"
import { fileURLToPath } from "node:url"

const password = "fake-e2e-password-123"

async function csrf(page: Page) { return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value }
async function login(page: Page) {
  const r = await page.request.post("/api/auth/login", { data: { username: "admin", password } })
  if (!r.ok()) {
    expect((await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })).ok()).toBeTruthy()
    expect((await page.request.post("/api/auth/password", { data: { old_password: "admin123", new_password: password }, headers: { "X-CSRF-Token": await csrf(page) } })).ok()).toBeTruthy()
  }
}

test("比对与处置：离线包、本地影响、处置", async ({ page }) => {
  await login(page)
  const headers = { "X-CSRF-Token": await csrf(page) }
  const asset = await (await page.request.post("/api/assets", { headers, data: { name: "fake-idrac-p18d", form: "physical", role: "server", vendor: "Dell", model: "PowerEdge R740",
    lifecycle: "active", endpoints: [{ address: "192.0.2.43", port: 161, protocol: "snmp" }] } })).json()
  expect((await page.request.post(`/api/assets/${asset.id}/collect`, { headers })).ok()).toBeTruthy()
  await expect.poll(async () => (await (await page.request.get(`/api/assets/${asset.id}/software`)).json()).items.length, { timeout: 30000 }).toBe(1)

  await page.goto("/#/vulns")
  await page.getByRole("tab", { name: "离线包与推送" }).click()
  await page.locator('input[type="file"]').setInputFiles(fileURLToPath(new URL("./fixtures/vulndb-mini.zip", import.meta.url)))
  await page.getByRole("button", { name: "导入", exact: true }).click()
  await expect(page.getByText(/导入完成：新漏洞/)).toBeVisible({ timeout: 30000 })
  await expect.poll(async () => (await (await page.request.get(`/api/vuln/matches?asset_id=${asset.id}`)).json()).length, { timeout: 30000 }).toBe(1)

  await page.getByRole("tab", { name: "漏洞", exact: true }).click()
  await page.getByRole("checkbox", { name: "只看影响本地资产的" }).check()
  const list = page.getByRole("list", { name: "漏洞列表" })
  await expect(list).toContainText("影响本地 1 台")
  await list.getByRole("button", { name: /CVE-2018-1243/ }).click()
  const impact = page.getByRole("region", { name: "本地影响范围" })
  await expect(impact).toContainText("iDRAC9 固件：3.00.00.00 → 修复于 3.21.21.21")
  const fix = page.getByRole("region", { name: "修复与缓解" })
  await expect(fix.getByRole("list", { name: "修复版本" })).toContainText("iDRAC9 固件：升级到 3.21.21.21 或更高版本（fake-idrac-p18d）")
  await expect(fix.getByRole("list", { name: "补丁与公告" })).toContainText("[厂商公告]")
  await impact.getByRole("button", { name: "处置" }).click()
  await impact.getByLabel("处置状态").selectOption("not_affected")
  await impact.getByLabel("理由").fill("fake：iDRAC 只在隔离管理网")
  await impact.getByRole("button", { name: "保存" }).click()
  await expect(impact).toContainText("不受影响")
  await expect(impact).toContainText("处置说明：fake：iDRAC 只在隔离管理网")

  await page.goto(`/#/assets?asset=${asset.id}`)
  const drawer = page.getByRole("dialog")
  await drawer.getByRole("tab", { name: "漏洞", exact: true }).click()
  await expect(drawer.getByRole("list", { name: "本地影响" })).toContainText("CVE-2018-1243")
  await expect(drawer.getByRole("list", { name: "本地影响" })).toContainText("不受影响")
})
