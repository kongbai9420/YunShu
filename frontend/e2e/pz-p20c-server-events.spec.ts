// 4.4 P20c“服务器告警”板块：筛选、批量标记已处理（必须写处理结果）、窄屏。浏览器测试环境没有 BMC 日志，
// 这里拦截板块接口给出伪造事件，只验证界面；入库、去重与接口由 backend/tests/test_server_events.py 覆盖。不连接任何设备。
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

const event = (id: string, severity: string, category: string, label: string, message: string) => ({
  id, asset_id: "00000000-0000-4000-8000-000000000001", asset_name: "fake-r740", source: "redfish", occurred_at: "2026-10-27T03:00:00+00:00",
  severity, category, category_label: label, message, first_seen_at: "2026-10-27T03:01:00+00:00", state: "open", handled_by: "", handled_at: null, note: "",
})

test("服务器告警：筛选、标记已处理要写结果、窄屏不横向滚动", async ({ page }) => {
  await login(page)
  const queries: string[] = []
  const posts: Record<string, unknown>[] = []
  await page.route("**/api/server-events/summary", (r) => r.fulfill({ json: { open: { critical: { events: 1, servers: 1 }, warning: { events: 1, servers: 1 } } } }))
  await page.route("**/api/server-events?*", (r) => {
    queries.push(r.request().url())
    const url = new URL(r.request().url())
    const items = [event("e1", "critical", "temperature", "温度", "fake: Inlet temperature is above the critical threshold"),
                   event("e2", "warning", "power", "电源", "fake: Power supply redundancy is degraded")]
      .filter((e) => !url.searchParams.get("category") || e.category === url.searchParams.get("category"))
    return r.fulfill({ json: { total: items.length, items, categories: { temperature: "温度", power: "电源", other: "其他" } } })
  })
  await page.route("**/api/server-events/state", async (r) => { posts.push(r.request().postDataJSON()); await r.fulfill({ json: { updated: 2 } }) })

  await page.goto("/#/server-events")
  await expect(page.getByRole("heading", { name: "服务器告警", exact: true })).toBeVisible()
  await expect(page.getByText("待处理的严重事件", { exact: true })).toBeVisible()
  const table = page.getByRole("table", { name: "服务器告警列表" })
  await expect(table.getByRole("row")).toHaveCount(3)
  await expect(table).toContainText("Inlet temperature")

  await page.getByLabel("部件").selectOption("power")
  await expect(table.getByRole("row")).toHaveCount(2)
  expect(queries.at(-1)).toContain("category=power")
  await page.getByLabel("部件").selectOption("")

  await page.getByLabel("全选本页").check()
  await page.getByRole("button", { name: "标记已处理" }).click()
  const dialog = page.getByRole("dialog", { name: "标记已处理：2 条" })
  await expect(dialog.getByRole("button", { name: "确认已处理" })).toBeDisabled()
  await dialog.getByLabel("处理结果").fill("已更换 2 号电源")
  await dialog.getByRole("button", { name: "确认已处理" }).click()
  await expect.poll(() => posts.length).toBe(1)
  expect(posts[0]).toEqual({ ids: ["e1", "e2"], state: "handled", note: "已更换 2 号电源" })

  await page.setViewportSize({ width: 375, height: 812 })
  await expect(table).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375)
})
