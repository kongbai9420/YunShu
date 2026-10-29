// 4.2 P14 机柜与定位。文件名以 pz- 开头，排在 p2 首次登录用例之后（见 pz-p11-scope.spec.ts 说明）
import { expect, test, type Page } from "@playwright/test"

const password = "fake-e2e-password-123"

async function csrf(page: Page) { return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value }
async function call(page: Page, method: "post" | "patch", path: string, data: unknown) {
  return page.request[method](path, { data, headers: { "X-CSRF-Token": await csrf(page) } })
}
async function login(page: Page) {
  const r = await page.request.post("/api/auth/login", { data: { username: "admin", password } })
  if (!r.ok()) {
    expect((await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })).ok()).toBeTruthy()
    expect((await call(page, "post", "/api/auth/password", { old_password: "admin123", new_password: password })).ok()).toBeTruthy()
  }
}

test("机柜 U 位视图、冲突提示与按地址查找", async ({ page }) => {
  await login(page)
  const site = await (await call(page, "post", "/api/sites", { name: "fake-p14-机房" })).json()
  const rack = await (await call(page, "post", "/api/racks", { site_id: site.id, name: "fake-R01", height_u: 12 })).json()
  const dtype = await (await call(page, "post", "/api/device-types", { manufacturer: "Fake", model: "fake-2U-server", u_height: 2 })).json()
  const make = (name: string, unit: number, extra: Record<string, unknown> = {}) =>
    call(page, "post", "/api/assets", { name, form: "physical", role: "server", site_id: site.id, rack_id: rack.id, rack_unit: unit, ...extra })
  expect((await make("fake-p14-db", 1, { device_type_id: dtype.id })).status()).toBe(201)
  const clash = await make("fake-p14-web", 2)
  expect(clash.status()).toBe(409)
  expect((await clash.json()).detail).toContain("fake-p14-db")
  expect((await make("fake-p14-web", 5)).status()).toBe(201)

  await page.goto("/#/racks")
  await expect(page.getByRole("heading", { name: "机柜与定位" })).toBeVisible()
  await page.getByLabel("场所").selectOption({ label: "fake-p14-机房" })
  await expect(page.getByRole("table", { name: "机柜概况" }).getByText("fake-R01")).toBeVisible()
  const front = page.getByRole("region", { name: "正面" })
  await expect(front.getByText("fake-p14-db")).toBeVisible()
  await expect(front.getByText("1-2")).toBeVisible()
  await expect(front.getByText("空余 9U")).toBeVisible()

  await page.getByRole("button", { name: "查找 MAC / IP" }).click()
  await page.getByLabel("MAC 或 IP 地址").fill("not-an-address")
  await page.getByRole("button", { name: "查找", exact: true }).click()
  await expect(page.getByText("请输入 MAC 地址")).toBeVisible()
  await page.getByLabel("MAC 或 IP 地址").fill("192.0.2.231")
  await page.getByRole("button", { name: "查找", exact: true }).click()
  await expect(page.getByText("ARP 表里没有这个 IP")).toBeVisible()

  await page.getByRole("button", { name: "设备型号" }).click()
  await page.getByLabel("搜索型号").fill("fake-2U")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(page.getByRole("list", { name: "设备型号" }).getByText("fake-2U-server")).toBeVisible()

  await page.setViewportSize({ width: 375, height: 812 })
  await page.getByRole("button", { name: "机柜", exact: true }).click()
  await expect(page.getByRole("region", { name: "正面" })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)

  // 机柜里的资产名可以直接打开资产详情
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.getByRole("region", { name: "正面" }).getByRole("link", { name: "fake-p14-db" }).click()
  await expect(page).toHaveURL(/#\/assets\?asset=/)
  await expect(page.getByRole("dialog").getByText("fake-p14-db").first()).toBeVisible()
})
