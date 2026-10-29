import { expect, test, type Page } from "@playwright/test"

const password = "fake-e2e-password-123"
const memberPassword = "fake-worker-member-123"
test.describe.configure({ mode: "serial" })

async function csrf(page: Page) { return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value }
async function post(page: Page, path: string, data: unknown) {
  const r = await page.request.post(path, { data, headers: { "X-CSRF-Token": await csrf(page) } })
  expect(r.ok(), await r.text()).toBeTruthy()
}
async function login(page: Page, user = "admin") {
  const r = await page.request.post("/api/auth/login", { data: { username: user, password: user === "admin" ? password : memberPassword } })
  if (!r.ok() && user === "admin") {
    expect((await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })).ok()).toBeTruthy()
    await post(page, "/api/auth/password", { old_password: "admin123", new_password: password })
  } else expect(r.ok(), await r.text()).toBeTruthy()
}

test("Worker 状态：内置 Worker 在线且为调度者，显示队列与失败概况；只读用户没有该页签", async ({ page, browser }) => {
  await login(page)
  await post(page, "/api/users", { username: "fake-worker-viewer", role: "viewer", password: "fake-worker-initial-123" })
  const member = await (await browser.newContext()).newPage()
  expect((await member.request.post("/api/auth/login", { data: { username: "fake-worker-viewer", password: "fake-worker-initial-123" } })).ok()).toBeTruthy()
  await post(member, "/api/auth/password", { old_password: "fake-worker-initial-123", new_password: memberPassword })

  await page.goto("/#/settings")
  await page.getByRole("tab", { name: "Worker 状态", exact: true }).click()
  await expect(page.getByRole("status", { name: "队列概况" })).toContainText("在线 Worker 1 个")
  const list = page.getByRole("list", { name: "Worker 列表" })
  await expect(list.getByText("在线", { exact: true })).toBeVisible()
  await expect(list.getByText("调度者", { exact: true })).toBeVisible()
  await expect(page.getByText("轻量版（SQLite）：只运行一个 Worker")).toBeVisible()
  await expect(page.getByRole("region", { name: "最近 1 小时失败原因" })).toBeVisible()

  await member.goto("/#/settings")
  await expect(member.getByRole("tab", { name: "凭据", exact: true })).toBeVisible()
  await expect(member.getByRole("tab", { name: "Worker 状态", exact: true })).toHaveCount(0)
  expect((await member.request.get("/api/workers")).status()).toBe(403)
})
