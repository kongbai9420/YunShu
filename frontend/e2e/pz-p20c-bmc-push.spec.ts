// 4.4 P20c 告警推送的界面流程：在“开启 SNMP”里配置 Trap 目标、订阅 Redfish 事件，查看能力里显示推送状态。
// 浏览器测试后端没有打开推送接收，这里拦截控制接口模拟“已开启”，只验证界面；真实的门禁、BMC 修改与接收端
// 由 backend/tests/test_bmc_push.py 覆盖。不连接任何设备。
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
async function post(page: Page, path: string, data: unknown) {
  const r = await page.request.post(path, { data, headers: { "X-CSRF-Token": await csrf(page) } })
  expect(r.ok(), await r.text()).toBeTruthy()
  return r.json()
}

const allowed = (key: string, label: string) => ({ key, label, high_risk: false, allowed: true, reason: "" })

test("开启 SNMP → 配置 Trap 目标要选团体名凭据，订阅 Redfish 事件不用；查看能力显示推送状态", async ({ page }) => {
  await login(page)
  const server = await post(page, "/api/assets", { name: "fake-push-r740", form: "physical", role: "server", lifecycle: "active" })
  const cred = await post(page, "/api/credentials", { alias: `fake-trap-v2c-${Date.now()}`, kind: "snmp_community", username: "", secret: "fake-community" })

  const bodies: Record<string, unknown>[] = []
  await page.route("**/api/control/status", async (r) => {
    const res = await r.fetch()
    await r.fulfill({ response: res, json: { ...(await res.json()), owner: "new" } })
  })
  await page.route(`**/api/control/assets/${server.id}/bmc-snmp`, (r) => r.fulfill({ json: {
    asset_id: server.id, asset_name: server.name, enabled: true, endpoint: { id: "e1", address: "192.0.2.242", purpose: "management" },
    snmp_endpoints: [], note: "",
    actions: [allowed("enable", "开启 BMC 的 SNMP"), allowed("trap_on", "在 BMC 上配置 Trap 目标"), allowed("events_on", "订阅 BMC 的 Redfish 事件")],
    push: {
      trap: { enabled: true, target: "192.0.2.10", port: 1162, subscription: null },
      events: { url: "https://yunshu.example.test", subscription: { destination: "https://yunshu.example.test/api/bmc-events/", created_at: "2026-10-27T03:00:00+00:00",
                                                                   created_by: "admin", last_event_at: null, events_received: 0 } },
    } } }))
  await page.route("**/api/control/bmc-snmp", async (r) => { bodies.push(r.request().postDataJSON()); await r.fulfill({ status: 202, json: { batch_id: "fake-push-batch" } }) })
  await page.route("**/api/control/operations?batch_id=*", (r) => r.fulfill({ json: [] }))

  await page.goto("/#/operations")
  await page.getByRole("group", { name: "运维操作内容" }).getByRole("button", { name: "开启 SNMP", exact: true }).click()
  await page.getByLabel("搜索资产").fill("fake-push-r740")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await page.getByLabel("fake-push-r740", { exact: true }).check()
  await page.getByRole("button", { name: "查看能力" }).first().click()
  const push = page.getByLabel("告警推送")
  await expect(push).toContainText("平台在 192.0.2.10 接收")
  await expect(push).toContainText("这台 BMC 上还没有配置")
  await expect(push).toContainText("还没收到过推送")

  const trap = page.getByRole("button", { name: "在 BMC 上配置 Trap 目标", exact: true })
  await expect(trap).toBeDisabled() // 要先选团体名凭据
  const events = page.getByRole("button", { name: "订阅 BMC 的 Redfish 事件", exact: true })
  await events.click()
  await page.getByRole("dialog", { name: "订阅 BMC 的 Redfish 事件：1 台" }).getByRole("button", { name: "确认订阅 BMC 的 Redfish 事件" }).click()
  await expect.poll(() => bodies.length).toBe(1)
  expect(bodies[0]).toMatchObject({ asset_ids: [server.id], action: "events_on" })
  expect(bodies[0].credential_id).toBeUndefined()

  await page.getByLabel("SNMP 凭据（开启时用于采集，配置 Trap 目标时用其团体名）").selectOption(cred.id)
  await trap.click()
  await page.getByRole("dialog", { name: "在 BMC 上配置 Trap 目标：1 台" }).getByRole("button", { name: "确认在 BMC 上配置 Trap 目标" }).click()
  await expect.poll(() => bodies.length).toBe(2)
  expect(bodies[1]).toMatchObject({ action: "trap_on", credential_id: cred.id })

  await page.setViewportSize({ width: 375, height: 812 })
  await expect(push).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375)
})
