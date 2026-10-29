// 4.4 P20a 采集计划的展示与“重新探测”。浏览器测试环境没有真实 BMC，这里拦截采集状态接口给出一份计划，
// 只验证界面展示与提交；探测与按计划执行由 backend/tests/test_collection_plan.py 覆盖。不连接任何设备。
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

test("资产详情 → 指标与采集：显示每项数据的主来源与兜底，可重新探测", async ({ page }) => {
  await login(page)
  const r = await page.request.post("/api/assets", { data: { name: "fake-plan-r750", form: "physical", role: "server", platform: "iDRAC9", lifecycle: "active" },
                                                    headers: { "X-CSRF-Token": await csrf(page) } })
  const asset = await r.json()
  let reprobed = 0
  const plan = (reprobe: boolean) => ({
    profile: { name: "dell-server", label: "Dell 服务器 / iDRAC9", verified: false, inferred: true }, endpoints: [], last_task: null,
    plan: { exists: true, probed_at: "2026-10-27T02:00:00+00:00", reprobe, slow_every: 10,
            probed: { snmp: ["fan", "health", "temp"], redfish: ["power", "sel", "storage", "temp"], ipmi: null },
            items: [
              { key: "temp", label: "温度", slow: false, primary: "snmp", fallbacks: ["redfish"] },
              { key: "storage", label: "硬盘与阵列", slow: true, primary: "redfish", fallbacks: [] },
              { key: "inventory", label: "内存容量等清单", slow: true, primary: null, fallbacks: [] },
            ],
            // 4.4 P20e 双来源核对
            checked_at: "2026-10-27T02:00:00+00:00", pinned: { temp: "redfish" },
            check: [{ key: "temp", label: "温度", ok: false, primary: "snmp", other: "redfish", at: "2026-10-27T02:00:00+00:00",
                      detail: "不一致：inlet SNMP 31℃ / Redfish 24℃。已改由 Redfish 提供，SNMP 的该品牌 MIB 对应可能有误" }] },
  })
  await page.route(`**/api/assets/${asset.id}/collection`, (route) => route.fulfill({ json: plan(reprobed > 0) }))
  await page.route(`**/api/assets/${asset.id}/collection-plan/reprobe`, async (route) => { reprobed += 1; await route.fulfill({ status: 202, json: { task_id: "fake" } }) })

  await page.goto(`/#/assets?asset=${asset.id}`)
  await page.getByRole("tab", { name: "指标与采集" }).click()
  const section = page.getByRole("region", { name: "采集计划" })
  const table = section.getByRole("table", { name: "每项数据的来源" })
  await expect(table.getByRole("row").filter({ hasText: "温度" })).toContainText("SNMP")
  await expect(table.getByRole("row").filter({ hasText: "温度" })).toContainText("Redfish")
  await expect(table.getByRole("row").filter({ hasText: "硬盘与阵列" })).toContainText("每 10 个周期")
  await expect(table.getByRole("row").filter({ hasText: "内存容量等清单" })).toContainText("没有协议读到")
  await expect(section).toContainText("IPMI 不可用（每小时重试）")
  await expect(section.getByRole("list", { name: "双来源核对" })).toContainText("该品牌 MIB 对应可能有误")

  await section.getByRole("button", { name: "重新探测" }).click()
  await expect.poll(() => reprobed).toBe(1)
  await expect(page.getByText("已安排重新探测", { exact: false })).toBeVisible()
  await expect(section.getByRole("button", { name: "重新探测" })).toBeDisabled()

  // 窄屏：计划表在自己的区域里横向滚动，页面本身不出现横向滚动
  await page.setViewportSize({ width: 375, height: 812 })
  await expect(table).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375)
})
