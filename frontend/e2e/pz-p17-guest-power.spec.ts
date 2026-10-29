// 4.2 P17 虚拟机开关机与宿主联动的界面流程。浏览器测试后端没有开启 YUNSHU_GUEST_POWER，这里拦截控制接口模拟“已开启”，
// 只验证界面提交了正确的请求；真实的门禁、任务执行与联动由 backend/tests/test_guest_power.py 覆盖。不连接任何设备。
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

const allowed = (keys: [string, string, boolean][]) => keys.map(([key, label, high_risk]) => ({ key, label, high_risk, allowed: true, reason: "" }))

test("宿主关机前先关其上虚拟机；虚拟机经虚拟化平台关机", async ({ page }) => {
  await login(page)
  const host = await post(page, "/api/assets", { name: "fake-联动宿主", form: "physical", role: "server", lifecycle: "active" })
  const vm = await post(page, "/api/assets", { name: "fake-联动虚拟机", form: "vm", role: "server", lifecycle: "active" })
  await post(page, "/api/relations", { source_asset_id: host.id, target_asset_id: vm.id, kind: "hosts" })

  const bodies: Record<string, unknown>[] = []
  await page.route(`**/api/control/assets/${host.id}/power*`, (r) => r.fulfill({ json: {
    asset_id: host.id, asset_name: host.name, endpoint: { id: "e1", address: "192.0.2.240", purpose: "control" }, target: null,
    actions: allowed([["on", "开机", false], ["soft", "正常软关机", true], ["reset", "硬件重启", true], ["off", "强制断电", true]]) } }))
  await page.route(`**/api/control/assets/${host.id}/hosted-guests`, (r) => r.fulfill({ json: { enabled: true, guests: [{ id: vm.id, name: vm.name, power: "on", lifecycle: "active" }] } }))
  await page.route(`**/api/control/assets/${vm.id}/guest-power`, (r) => r.fulfill({ json: {
    enabled: true, source: { platform: "pve", target: "192.0.2.80:8006", power: "on" },
    actions: allowed([["on", "开机", false], ["shutdown", "正常关机", true], ["off", "强制断电", true], ["reboot", "重启", true]]) } }))
  await page.route("**/api/control/power", async (r) => { bodies.push(r.request().postDataJSON()); await r.fulfill({ status: 202, json: { batch_id: "fake-host-batch", guest_batch_id: "fake-guest-batch" } }) })
  await page.route("**/api/control/guest-power", async (r) => { bodies.push(r.request().postDataJSON()); await r.fulfill({ status: 202, json: { batch_id: "fake-guest-only" } }) })
  await page.route("**/api/control/operations?batch_id=*", (r) => r.fulfill({ json: [] }))

  await page.goto("/#/overview?view=resources")
  await page.getByLabel("搜索名称、编号、平台或地址", { exact: true }).fill("fake-联动")
  await page.getByRole("button", { name: "应用筛选", exact: true }).click()

  // 宿主：正常软关机 → 确认框里有联动选项（默认勾选），提交 guests_first
  await page.getByRole("button", { name: "电源操作 fake-联动宿主", exact: true }).click()
  const hostDialog = page.getByRole("dialog", { name: "电源操作：fake-联动宿主" })
  await hostDialog.getByRole("button", { name: "正常软关机", exact: true }).click()
  const confirm = page.getByRole("dialog", { name: "正常软关机：1 台" })
  const linked = confirm.getByRole("region", { name: "宿主联动" })
  await expect(linked.getByRole("checkbox", { name: /先正常关闭其上 1 台虚拟机/ })).toBeChecked()
  await expect(linked).toContainText("fake-联动虚拟机（运行中）")
  await linked.getByLabel("最长等待（秒）").fill("600")
  await confirm.getByLabel(/请输入/).fill("fake-联动宿主")
  await confirm.getByRole("button", { name: "确认正常软关机" }).click()
  await expect.poll(() => bodies.length).toBe(1)
  expect(bodies[0]).toMatchObject({ asset_ids: [host.id], action: "soft", guests_first: true, guest_timeout: 600 })
  await expect(hostDialog.getByText("其上虚拟机（先执行）")).toBeVisible()
  await page.keyboard.press("Escape")

  // 开机不需要联动选项
  await page.getByRole("button", { name: "电源操作 fake-联动宿主", exact: true }).click()
  await hostDialog.getByRole("button", { name: "开机", exact: true }).click()
  await expect(page.getByRole("dialog", { name: "开机：1 台" }).getByRole("region", { name: "宿主联动" })).toHaveCount(0)
  await page.keyboard.press("Escape")
  await page.keyboard.press("Escape")

  // 虚拟机：经虚拟化平台，提交到 guest-power
  await page.getByRole("button", { name: "电源操作 fake-联动虚拟机", exact: true }).click()
  const vmDialog = page.getByRole("dialog", { name: "电源操作：fake-联动虚拟机" })
  await expect(vmDialog).toContainText("PVE · 192.0.2.80:8006")
  await vmDialog.getByRole("button", { name: "正常关机", exact: true }).click()
  const vmConfirm = page.getByRole("dialog", { name: "正常关机：1 台" })
  await expect(vmConfirm).toContainText("不使用宿主的 IPMI")
  await vmConfirm.getByLabel(/请输入/).fill("fake-联动虚拟机")
  await vmConfirm.getByRole("button", { name: "确认正常关机" }).click()
  await expect.poll(() => bodies.length).toBe(2)
  expect(bodies[1]).toMatchObject({ asset_ids: [vm.id], action: "shutdown", timeout: 300 })
})
