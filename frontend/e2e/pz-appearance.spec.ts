// 外观主题（P23）：顶栏选玻璃 / 深浅并跟账号保存；管理员导入主题包、换壁纸；切回默认后不残留主题变量。不连接任何设备。
import { readFileSync } from "node:fs"
import { expect, test, type Page } from "@playwright/test"

const password = "fake-e2e-password-123"
const example = readFileSync(new URL("../src/themes/example-theme.json", import.meta.url))
// 1×1 PNG（只用于上传校验和显示）
const png = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64",
)

async function csrf(page: Page) { return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value }
async function login(page: Page) {
  const r = await page.request.post("/api/auth/login", { data: { username: "admin", password } })
  if (!r.ok()) {
    expect((await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })).ok()).toBeTruthy()
    expect((await page.request.post("/api/auth/password", { data: { old_password: "admin123", new_password: password }, headers: { "X-CSRF-Token": await csrf(page) } })).ok()).toBeTruthy()
    expect((await page.request.post("/api/auth/login", { data: { username: "admin", password } })).ok()).toBeTruthy()
  }
}
const html = (page: Page) => page.locator("html")

test.afterEach(async ({ page }) => {
  // 其他用例按简洁、跟随系统截图和断言：恢复默认，删掉导入的主题和壁纸
  const headers = { "X-CSRF-Token": await csrf(page) }
  await page.request.put("/api/appearance/mine", { data: {}, headers })
  await page.request.delete("/api/appearance/themes/ocean", { headers })
  await page.request.delete("/api/appearance/wallpaper", { headers })
})

test("顶栏选玻璃和深色，刷新后仍是账号里保存的选择", async ({ page }) => {
  await login(page)
  await page.goto("/#/overview")
  await expect(html(page)).toHaveAttribute("data-style", "simple")
  await page.getByRole("button", { name: /^外观：/ }).click()
  const menu = page.getByRole("dialog", { name: "外观" })
  await menu.getByRole("radiogroup", { name: "风格" }).getByRole("radio", { name: /^玻璃/ }).click()
  await menu.getByRole("radiogroup", { name: "深浅" }).getByRole("radio", { name: "深色" }).click()
  await expect(html(page)).toHaveAttribute("data-style", "glass")
  await expect(html(page)).toHaveAttribute("data-theme", "dark")
  await expect.poll(async () => (await (await page.request.get("/api/appearance")).json()).mine).toEqual({ style: "glass", mode: "dark" })

  // 换一个浏览器存储（模拟换电脑）：按账号恢复
  await page.evaluate(() => localStorage.clear())
  await page.reload()
  await expect(html(page)).toHaveAttribute("data-style", "glass")
  await expect(html(page)).toHaveAttribute("data-theme", "dark")
  await expect(page.getByRole("button", { name: "外观：玻璃 · 深色" })).toBeVisible()
  // 玻璃的卡片是半透明的
  const card = await page.locator(".shadow-card").first().evaluate((el) => getComputedStyle(el).backgroundColor)
  expect(card).toMatch(/rgba\(.*0\.5\d*\)/)
})

test("管理员导入主题包和壁纸，选用后再切回默认不残留", async ({ page }) => {
  await login(page)
  await page.goto("/#/settings?tab=appearance")
  await expect(page.getByRole("tab", { name: "外观" })).toHaveAttribute("data-state", "active")

  await page.getByLabel("导入主题包").setInputFiles({ name: "ocean.json", mimeType: "application/json", buffer: example })
  await expect(page.getByText("已导入主题“海洋”")).toBeVisible()
  await page.getByLabel("上传图片").setInputFiles({ name: "wall.png", mimeType: "image/png", buffer: png })
  await expect(page.getByText("壁纸已更换。")).toBeVisible()
  await expect(page.getByRole("img", { name: "当前壁纸预览" })).toBeVisible()

  const mine = page.getByRole("region", { name: "我的外观" })
  await mine.getByRole("radiogroup", { name: "风格" }).getByRole("radio", { name: /^海洋/ }).click()
  await mine.getByRole("radiogroup", { name: "深浅" }).getByRole("radio", { name: "浅色" }).click()
  await expect(html(page)).toHaveAttribute("data-skin", "ocean")
  await expect(html(page)).toHaveAttribute("data-style", "glass") // 主题包基于玻璃
  await expect(html(page)).toHaveAttribute("data-wallpaper", "")
  await expect.poll(() => page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--primary").trim())).toBe("#00668a")

  // 刷新（首帧前按缓存写入变量）后切回平台默认：变量全部清掉
  await page.reload()
  await expect(html(page)).toHaveAttribute("data-skin", "ocean")
  await page.getByRole("region", { name: "我的外观" }).getByRole("radiogroup", { name: "风格" }).getByRole("radio", { name: /^平台默认/ }).click()
  await expect(html(page)).toHaveAttribute("data-style", "simple")
  await expect(html(page)).not.toHaveAttribute("data-wallpaper", "")
  expect(await page.evaluate(() => document.documentElement.style.cssText)).toBe("")
  await expect.poll(() => page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--primary").trim())).toBe("#0062cc")

  // 删除主题
  page.once("dialog", (d) => void d.accept())
  await page.getByRole("button", { name: "删除主题 海洋" }).click()
  await expect(page.getByText("已删除主题“海洋”。")).toBeVisible()
  await expect(page.getByText("还没有导入的主题。")).toBeVisible()
})
