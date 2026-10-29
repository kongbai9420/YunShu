// 文件名以 pz- 开头：Playwright 按文件名顺序执行，p11 会排在 p2 之前并提前改掉管理员初始密码，破坏 p2 的首次登录用例
import { expect, test, type Page } from "@playwright/test"

const password = "fake-e2e-password-123"
const memberPassword = "fake-scope-member-123"
const prefix = "fake-sc-"
test.describe.configure({ mode: "serial" })
const ids: Record<string, string> = {}

async function csrf(page: Page) { return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value }
async function call(page: Page, method: "post" | "put", path: string, data: unknown) {
  const r = await page.request[method](path, { data, headers: { "X-CSRF-Token": await csrf(page) } })
  expect(r.ok(), await r.text()).toBeTruthy()
  const body = await r.text()
  return body ? JSON.parse(body) : null
}
async function login(page: Page, user = "admin") {
  const r = await page.request.post("/api/auth/login", { data: { username: user, password: user === "admin" ? password : memberPassword } })
  if (!r.ok() && user === "admin") {
    expect((await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })).ok()).toBeTruthy()
    await call(page, "post", "/api/auth/password", { old_password: "admin123", new_password: password })
  } else expect(r.ok(), await r.text()).toBeTruthy()
}

test.beforeAll(async ({ browser }) => {
  const page = await (await browser.newContext()).newPage()
  await login(page)
  ids.siteA = (await call(page, "post", "/api/sites", { name: `${prefix}场所A`, kind: "datacenter" })).id
  ids.siteB = (await call(page, "post", "/api/sites", { name: `${prefix}场所B`, kind: "datacenter" })).id
  ids.a = (await call(page, "post", "/api/assets", { name: `${prefix}设备A`, form: "physical", role: "server", lifecycle: "planned", site_id: ids.siteA })).id
  ids.b = (await call(page, "post", "/api/assets", { name: `${prefix}设备B`, form: "physical", role: "server", lifecycle: "planned", site_id: ids.siteB })).id
  const user = await call(page, "post", "/api/users", { username: `${prefix}op`, role: "operator", password: "fake-scope-initial-123" })
  ids.user = user.id
  const member = await (await browser.newContext()).newPage()
  expect((await member.request.post("/api/auth/login", { data: { username: `${prefix}op`, password: "fake-scope-initial-123" } })).ok()).toBeTruthy()
  await call(member, "post", "/api/auth/password", { old_password: "fake-scope-initial-123", new_password: memberPassword })
})

test("管理员在用户页设置资产范围并预览实际可见资产", async ({ page }) => {
  await login(page)
  await page.goto("/#/settings")
  await page.getByRole("tab", { name: "用户与角色", exact: true }).click()
  await page.getByRole("button", { name: `资产范围 ${prefix}op`, exact: true }).click()
  const dialog = page.getByRole("dialog", { name: `资产范围：${prefix}op` })
  await dialog.getByLabel("可见全部资产（升级前的账号默认如此）").uncheck()
  await dialog.getByLabel(`${prefix}场所A`, { exact: true }).check()
  await dialog.getByRole("button", { name: "保存并预览", exact: true }).click()
  await expect(dialog.getByRole("region", { name: "实际可见资产" })).toContainText("实际可见 1 项资产")
  await expect(dialog.getByRole("region", { name: "实际可见资产" })).toContainText(`${prefix}设备A`)
  await page.keyboard.press("Escape")
  await expect(page.getByRole("button", { name: `资产范围 ${prefix}op`, exact: true })).toContainText("限定范围")
})

test("范围受限的运维：只看到授权资产，无权功能灰键或提示，直链范围外资产提示无权", async ({ page }) => {
  await login(page, `${prefix}op`)
  await page.goto("/#/assets")
  await page.getByLabel("搜索", { exact: true }).fill(prefix)
  await expect(page.getByRole("row").filter({ hasText: `${prefix}设备A` })).toBeVisible()
  await expect(page.getByRole("row").filter({ hasText: `${prefix}设备B` })).toHaveCount(0)
  // 场所：看得到授权场所，管理按钮灰键并说明原因
  await page.goto("/#/settings")
  await page.getByRole("tab", { name: "场所", exact: true }).click()
  await expect(page.getByText(`${prefix}场所A`, { exact: true })).toBeVisible()
  await expect(page.getByText(`${prefix}场所B`, { exact: true })).toHaveCount(0)
  await expect(page.getByRole("button", { name: "添加场所", exact: true })).toBeDisabled()
  await expect(page.getByText("场所管理只对可见全部资产的账号开放", { exact: false })).toBeVisible()
  // Worker 状态与告警规则：页签保留并提示
  await page.getByRole("tab", { name: "Worker 状态", exact: true }).click()
  await expect(page.getByRole("region", { name: "资产范围限制" })).toBeVisible()
  await page.goto("/#/alerts")
  await page.getByRole("button", { name: "告警规则", exact: true }).click()
  await expect(page.getByRole("region", { name: "资产范围限制" })).toContainText("全局配置")
  // 直接打开范围外资产的接口：当作不存在
  const r = await page.request.get(`/api/assets/${ids.b}`)
  expect(r.status()).toBe(404)
  expect(await r.text()).toContain("资产不存在或你无权查看")
})
