// 4.4 P20b / P20d：资产“指标与采集”显示临时提频与本轮顺延的原因。浏览器测试环境没有真实 BMC，
// 这里拦截采集状态接口，只验证界面；提频登记、入队与顺延由 backend/tests/test_collection_boost.py、test_bmc_queue.py 覆盖。不连接任何设备。
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

test("资产详情 → 指标与采集：显示临时提频与本轮顺延原因", async ({ page }) => {
  await login(page)
  const r = await page.request.post("/api/assets", { data: { name: "fake-boost-r750", form: "physical", role: "server", platform: "iDRAC9", lifecycle: "active" },
                                                    headers: { "X-CSRF-Token": await csrf(page) } })
  const asset = await r.json()
  await page.route(`**/api/assets/${asset.id}/collection`, (route) => route.fulfill({ json: {
    profile: { name: "dell-server", label: "Dell 服务器 / iDRAC9", verified: false, inferred: true }, endpoints: [], plan: null,
    boost: { interval: 15, until: "2026-10-27T03:10:00+00:00", since: "2026-10-27T03:00:00+00:00", reason: "接近阈值：硬件 CPU 高温（74°C，阈值 80）" },
    last_task: { status: "succeeded", error: "", finished_at: "2026-10-27T03:00:05+00:00",
                 result: { ok: ["snmp"], failed: [], unconfigured: [], deferred: ["redfish", "redfish"], deferred_reason: "" } },
  } }))
  await page.goto(`/#/assets?asset=${asset.id}`)
  await page.getByRole("tab", { name: "指标与采集" }).click()
  await expect(page.getByText(/临时提频：每 15 秒采集一次/)).toContainText("接近阈值：硬件 CPU 高温")
  await expect(page.getByText(/最近任务/)).toContainText("本轮顺延：Redfish（这台 BMC 一分钟内的访问已达上限，不算失败）")
})
