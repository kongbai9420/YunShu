import { expect, test, type Page } from "@playwright/test"

// 与 p2.spec.ts 同一个测试后端；单独运行本文件时先完成初始管理员改密
const password = "fake-e2e-password-123"
test.describe.configure({ mode: "serial" })

async function csrf(page: Page) {
  const cookies = await page.context().cookies()
  return cookies.find((c) => c.name === "yunshu_csrf")!.value
}

async function api(page: Page, method: "post" | "patch", path: string, body: unknown) {
  const response = await page.request[method](path, { data: body, headers: { "X-CSRF-Token": await csrf(page) } })
  const text = await response.text()
  expect(response.ok(), text).toBeTruthy()
  return text ? JSON.parse(text) : null
}

async function signIn(page: Page) {
  let res = await page.request.post("/api/auth/login", { data: { username: "admin", password } })
  if (!res.ok()) {
    res = await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })
    expect(res.ok()).toBeTruthy()
    await api(page, "post", "/api/auth/password", { old_password: "admin123", new_password: password })
  }
}

const node = (page: Page, name: string) => page.locator(".react-flow__node").filter({ hasText: name })

test("拓扑：重新扫描预览与合并不覆盖人工连线，锁定位置保存后刷新不丢，布局冲突提示", async ({ page }) => {
  await signIn(page)
  const sw = await api(page, "post", "/api/assets", {
    name: "拓扑交换机", asset_no: "FAKE-TOPO-SW", form: "physical", role: "switch", collection_profile: "snmp-switch",
    endpoints: [{ address: "192.0.2.60", port: 161, protocol: "snmp" }],
  })
  await api(page, "post", "/api/assets", { name: "fake-topo-srv", asset_no: "FAKE-TOPO-SRV", form: "physical", role: "server" })
  await api(page, "post", "/api/assets", { name: "拓扑存储", asset_no: "FAKE-TOPO-NAS", form: "physical", role: "nas" })
  await api(page, "post", `/api/assets/${sw.id}/collect`, {})
  await expect
    .poll(async () => JSON.stringify(await (await page.request.get(`/api/assets/${sw.id}/collection`)).json()), { timeout: 30000 })
    .toContain("lldp_neighbors")

  await page.goto("/#/topology")
  await expect(page.getByRole("heading", { name: "拓扑", exact: true })).toBeVisible()
  await expect(page.getByText(/演示数据/)).toHaveCount(0)
  await expect(node(page, "拓扑交换机")).toBeVisible()

  // 扫描差异先预览，再合并成候选
  await page.getByRole("button", { name: "重新扫描", exact: true }).click()
  const dialog = page.getByRole("dialog", { name: "重新扫描：差异预览" })
  await expect(dialog.getByText("新增：拓扑交换机 → fake-topo-srv")).toBeVisible()
  await dialog.getByRole("button", { name: "合并到候选" }).click()
  const list = page.getByRole("region", { name: "拓扑详情" }).or(page.getByLabel("拓扑详情"))
  await expect(list.getByRole("button", { name: /拓扑交换机 → fake-topo-srv\s*候选/ })).toBeVisible()

  // 键盘可用的表单新增人工连线
  await page.getByLabel("起点设备", { exact: true }).selectOption({ label: "fake-topo-srv" })
  await page.getByLabel("终点设备", { exact: true }).selectOption({ label: "拓扑存储" })
  await page.getByRole("button", { name: "新增连线" }).click()
  await expect(list.getByRole("button", { name: /fake-topo-srv → 拓扑存储\s*人工/ })).toBeVisible()

  // 再扫一次：没有差异，人工连线不受影响
  await page.getByRole("button", { name: "重新扫描", exact: true }).click()
  await expect(dialog.getByText("没有差异", { exact: false })).toBeVisible()
  await expect(dialog).toContainText("1 条已确认或人工链路")
  await dialog.getByRole("button", { name: "关闭" }).click()
  await expect(list.getByRole("button", { name: /fake-topo-srv → 拓扑存储\s*人工/ })).toBeVisible()

  // 锁定位置并保存，刷新后仍在
  await node(page, "拓扑存储").click()
  await page.getByRole("button", { name: "锁定位置" }).click()
  await expect(node(page, "拓扑存储")).toContainText("已锁定")
  await page.getByRole("button", { name: "保存布局" }).click()
  await expect(page.getByText(/修订号 2/).first()).toBeVisible()
  await page.reload()
  await expect(node(page, "拓扑存储")).toContainText("已锁定")

  // 移出视图后撤销：画布上标出被撤销的节点
  await node(page, "fake-topo-srv").click()
  await page.getByRole("button", { name: "移出视图" }).click()
  await expect(node(page, "fake-topo-srv")).toHaveCount(0)
  await page.getByRole("button", { name: "撤销", exact: true }).click()
  await expect(node(page, "fake-topo-srv")).toContainText("已撤销")

  // 另一个窗口先保存：本页保存时明确提示冲突，可载入最新
  const views = await (await page.request.get("/api/topology/views")).json()
  const home = views[0]
  await api(page, "patch", `/api/topology/views/${home.id}`, { revision: home.revision, hidden: [] })
  await node(page, "fake-topo-srv").click()
  await page.getByRole("button", { name: "移出视图" }).click()
  await page.getByRole("button", { name: "保存布局" }).click()
  await expect(page.getByRole("alert").filter({ hasText: "已被他人修改" })).toBeVisible()
  await page.getByRole("button", { name: /载入最新/ }).click()
  await expect(node(page, "fake-topo-srv")).toBeVisible()
  await expect(page.getByText(/修订号 3/).first()).toBeVisible()
  await page.screenshot({ path: "test-results/p3-topology.png", fullPage: true })
})
