// 4.4 P19 一键开启 BMC 的 SNMP 的界面流程。浏览器测试后端没有开启 YUNSHU_BMC_SNMP，这里拦截控制接口模拟“已开启”，
// 只验证界面提交了正确的请求；真实的门禁、Redfish 修改与自动添加端点由 backend/tests/test_bmc_snmp.py 覆盖。不连接任何设备。
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

test("运维操作 → 开启 SNMP：选凭据后对所选服务器开启", async ({ page }) => {
  await login(page)
  const server = await post(page, "/api/assets", { name: "fake-snmp-r740", form: "physical", role: "server", lifecycle: "active" })
  const cred = await post(page, "/api/credentials", { alias: `fake-snmp-v2c-${Date.now()}`, kind: "snmp_community", username: "", secret: "fake-community" })

  const bodies: Record<string, unknown>[] = []
  await page.route("**/api/control/status", async (r) => {
    const res = await r.fetch()
    await r.fulfill({ response: res, json: { ...(await res.json()), owner: "new" } })
  })
  await page.route(`**/api/control/assets/${server.id}/bmc-snmp`, (r) => r.fulfill({ json: {
    asset_id: server.id, asset_name: server.name, enabled: true, endpoint: { id: "e1", address: "192.0.2.241", purpose: "management" },
    snmp_endpoints: [], note: "执行时先只读识别 BMC 的 SNMP 设置位置。",
    actions: [{ key: "enable", label: "开启 BMC 的 SNMP", high_risk: false, allowed: true, reason: "" },
              { key: "disable", label: "关闭 BMC 的 SNMP", high_risk: true, allowed: true, reason: "" }] } }))
  await page.route("**/api/control/bmc-snmp", async (r) => { bodies.push(r.request().postDataJSON()); await r.fulfill({ status: 202, json: { batch_id: "fake-snmp-batch" } }) })
  await page.route("**/api/control/operations?batch_id=*", (r) => r.fulfill({ json: [] }))

  await page.goto("/#/operations")
  await page.getByRole("group", { name: "运维操作内容" }).getByRole("button", { name: "开启 SNMP", exact: true }).click()
  await page.getByLabel("搜索资产").fill("fake-snmp-r740")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await page.getByLabel("fake-snmp-r740", { exact: true }).check()
  await page.getByRole("button", { name: "查看能力" }).first().click()
  await expect(page.getByText("Redfish 端点：192.0.2.241（管理）")).toBeVisible()

  // 没选凭据时“开启”不可点
  const enable = page.getByRole("button", { name: "开启 BMC 的 SNMP", exact: true })
  await expect(enable).toBeDisabled()
  await page.getByLabel("SNMP 凭据（开启时用于采集，配置 Trap 目标时用其团体名）").selectOption(cred.id)
  await enable.click()
  const dialog = page.getByRole("dialog", { name: "开启 BMC 的 SNMP：1 台" })
  await expect(dialog).toContainText("不支持的机型直接拒绝")
  await dialog.getByRole("button", { name: "确认开启 BMC 的 SNMP" }).click()
  await expect.poll(() => bodies.length).toBe(1)
  expect(bodies[0]).toMatchObject({ asset_ids: [server.id], action: "enable", credential_id: cred.id })

  // 关闭是高风险：要输入资产名称
  await page.getByRole("button", { name: "关闭 BMC 的 SNMP", exact: true }).click()
  const off = page.getByRole("dialog", { name: "关闭 BMC 的 SNMP：1 台" })
  await expect(off.getByRole("button", { name: "确认关闭 BMC 的 SNMP" })).toBeDisabled()
  await off.getByLabel(/请输入/).fill("fake-snmp-r740")
  await off.getByRole("button", { name: "确认关闭 BMC 的 SNMP" }).click()
  await expect.poll(() => bodies.length).toBe(2)
  expect(bodies[1]).toMatchObject({ action: "disable" })
  expect(bodies[1].credential_id).toBeUndefined()
})
