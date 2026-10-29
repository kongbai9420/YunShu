// README 与文档站用的界面截图。默认跳过；README_SCREENS=1 时运行，只用伪造数据，截图写到项目目录外。
import { expect, test, type Page } from "@playwright/test"

const password = "fake-e2e-password-123"
const out = process.env.README_SCREENS_DIR ?? "test-results/screenshots/readme"

test.skip(!process.env.README_SCREENS, "只在 README_SCREENS=1 时截图")

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

test("README 截图", async ({ page }) => {
  test.setTimeout(120000)
  await page.setViewportSize({ width: 1440, height: 900 })
  await login(page)

  const site = await post(page, "/api/sites", { name: "演示机房 A" })
  const rack = await post(page, "/api/racks", { site_id: site.id, name: "A01", height_u: 24 })
  const r740 = await post(page, "/api/device-types", { manufacturer: "Dell", model: "PowerEdge R740xd", u_height: 2 })
  const r640 = await post(page, "/api/device-types", { manufacturer: "Dell", model: "PowerEdge R640", u_height: 1 })
  const sw = await post(page, "/api/device-types", { manufacturer: "Fake", model: "48 口千兆交换机", u_height: 1, is_full_depth: false })
  const ups = await post(page, "/api/device-types", { manufacturer: "Fake", model: "机架式 UPS 3kVA", u_height: 2 })
  const nas = await post(page, "/api/device-types", { manufacturer: "Synology", model: "RS1221+", u_height: 2 })
  const asset = (name: string, extra: Record<string, unknown>) =>
    post(page, "/api/assets", { name, form: "physical", role: "server", site_id: site.id, lifecycle: "active", ...extra })
  const pve1 = await asset("pve-01（虚拟化宿主）", { platform: "proxmox", vendor: "Dell", model: "PowerEdge R740xd", rack_id: rack.id, rack_unit: 10, device_type_id: r740.id, tags: ["虚拟化"] })
  const pve2 = await asset("pve-02（虚拟化宿主）", { platform: "proxmox", vendor: "Dell", model: "PowerEdge R740xd", rack_id: rack.id, rack_unit: 12, device_type_id: r740.id, tags: ["虚拟化"] })
  await asset("db-01（数据库）", { platform: "debian", vendor: "Dell", model: "PowerEdge R640", rack_id: rack.id, rack_unit: 8, device_type_id: r640.id, tags: ["生产"] })
  await asset("core-sw-01（核心交换）", { role: "switch", platform: "", vendor: "Fake", model: "48 口千兆交换机", rack_id: rack.id, rack_unit: 22, device_type_id: sw.id })
  await asset("access-sw-01（接入交换）", { role: "switch", vendor: "Fake", model: "48 口千兆交换机", rack_id: rack.id, rack_unit: 22, rack_face: "rear", device_type_id: sw.id })
  await asset("ups-01（机房 UPS）", { role: "ups", vendor: "Fake", rack_id: rack.id, rack_unit: 1, device_type_id: ups.id })
  await asset("nas-01（备份存储）", { role: "nas", platform: "synology dsm", vendor: "Synology", model: "RS1221+", rack_id: rack.id, rack_unit: 4, device_type_id: nas.id })
  for (const [name, host] of [["web-01", pve1], ["web-02", pve1], ["gitlab", pve1], ["monitor", pve2], ["k8s-node-1", pve2]] as const) {
    const vm = await post(page, "/api/assets", { name, form: "vm", role: "server", platform: "debian", site_id: site.id, lifecycle: "active" })
    await post(page, "/api/relations", { source_asset_id: host.id, target_asset_id: vm.id, kind: "hosts" })
  }

  await page.goto("/#/overview?view=resources")
  await expect(page.getByRole("article").first()).toBeVisible()
  await page.waitForTimeout(800)
  await page.screenshot({ path: `${out}/resources.png` })

  await page.goto("/#/racks")
  await page.getByLabel("场所").selectOption({ label: "演示机房 A" })
  await expect(page.getByRole("region", { name: "正面" })).toBeVisible()
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${out}/racks.png`, fullPage: true })

  await page.goto("/#/assets")
  await expect(page.getByText("pve-01（虚拟化宿主）").first()).toBeVisible()
  await page.screenshot({ path: `${out}/assets.png` })

  await page.goto("/#/alerts")
  await page.getByRole("button", { name: "告警规则" }).click()
  await page.getByRole("button", { name: "新增告警规则" }).click()
  const pill = page.locator('[data-help-pill="alert.rule.recovery"]').first()
  await pill.hover()
  await expect(page.getByRole("dialog").last()).toBeVisible()
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${out}/alert-rules-help.png` })

  await page.emulateMedia({ colorScheme: "dark" })
  await page.goto("/#/overview?view=resources")
  await expect(page.getByRole("article").first()).toBeVisible()
  await page.waitForTimeout(800)
  await page.screenshot({ path: `${out}/resources-dark.png` })
})
