// 「!」说明胶囊：悬停、键盘聚焦、点按都能看到详细说明，Esc 关闭；胶囊不混进控件名称。
// 文件名以 pz- 开头，排在 p2 首次登录用例之后（见 pz-p11-scope.spec.ts 说明）
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

test("说明胶囊：悬停、键盘、点按都能打开，Esc 关闭，不影响控件名称", async ({ page }) => {
  await login(page)
  await page.goto("/#/assets")
  const pill = page.locator('[data-help-pill="asset.lifecycle"]').first()
  await expect(pill).toHaveText("!")
  await expect(pill).toHaveAccessibleName("详细说明")

  // 悬停打开，移开关闭
  await pill.hover()
  const tip = page.getByRole("dialog").filter({ hasText: "只有“在用”的资产会被定时采集" })
  await expect(tip).toBeVisible()
  await expect(tip).toContainText("默认：在用")
  await page.mouse.move(5, 5)
  await expect(tip).toBeHidden()

  // 键盘聚焦打开，Esc 关闭
  await pill.focus()
  await expect(tip).toBeVisible()
  await page.keyboard.press("Escape")
  await expect(tip).toBeHidden()

  // 控件的可访问名称仍是原来的标签，胶囊没有混进去
  await expect(page.getByRole("combobox", { name: "生命周期", exact: true }).first()).toBeVisible()

  // 会影响设备或数据的选项用橙色胶囊
  await page.goto("/#/settings")
  await page.getByRole("tab", { name: "用户与角色" }).click()
  const scopeButton = page.getByRole("button", { name: /资产范围/ }).first()
  if (await scopeButton.count()) {
    await scopeButton.click()
    const risky = page.locator('[data-help-pill="settings.scope.all"]')
    await expect(risky).toHaveClass(/text-warn/)
    await page.keyboard.press("Escape")
  }

  // 窄屏点按打开，说明框不超出屏幕
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto("/#/assets")
  const narrow = page.locator('[data-help-pill="asset.form"]').first()
  await narrow.click()
  const box = await page.getByRole("dialog").filter({ hasText: "资产的存在形式" }).boundingBox()
  expect(box).not.toBeNull()
  expect(box!.x).toBeGreaterThanOrEqual(0)
  expect(box!.x + box!.width).toBeLessThanOrEqual(375)
})
