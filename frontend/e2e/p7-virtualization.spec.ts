import { expect, test, type Page } from "@playwright/test"

test.describe.configure({ mode: "serial" })
const password = "fake-e2e-password-123"
const memberPassword = "fake-virt-member-123"
let platformId = "", credentialId = "", sourceId = "", vmwareSourceId = "", hostA = "", hostB = "", vm = ""

async function csrf(page: Page) { return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value }
async function api(page: Page, method: "post" | "patch", path: string, data: unknown) {
  const r = await page.request[method](path, { data, headers: { "X-CSRF-Token": await csrf(page) } })
  const body = await r.text()
  expect(r.ok(), body).toBeTruthy()
  return body ? JSON.parse(body) : null
}
async function login(page: Page, role = "admin") {
  const r = await page.request.post("/api/auth/login", { data: { username: role === "admin" ? "admin" : `fake-virt-${role}`, password: role === "admin" ? password : memberPassword } })
  if (!r.ok() && role === "admin") {
    expect((await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })).ok()).toBeTruthy()
    await api(page, "post", "/api/auth/password", { old_password: "admin123", new_password: password })
  } else expect(r.ok(), await r.text()).toBeTruthy()
}
async function collect(page: Page, id: string, expected = "succeeded") {
  const job = await api(page, "post", `/api/assets/${id}/collect`, {})
  await expect.poll(async () => {
    const data = await (await page.request.get(`/api/tasks?asset_id=${id}`)).json()
    return data.items.find((t: { id: string }) => t.id === job.task_id)?.status
  }, { timeout: 25000 }).toBe(expected)
}
async function open(page: Page) {
  await page.goto("/#/overview?view=resources")
  await page.getByRole("button", { name: "虚拟资源发现与关联", exact: true }).click()
  await expect(page.getByLabel("虚拟化实例范围", { exact: true }).getByRole("option", { name: /fake-virt-management/ })).toHaveCount(1)
  await page.getByLabel("虚拟化实例范围", { exact: true }).selectOption(sourceId)
}
const native = (page: Page, external: string) => page.getByRole("article", { name: new RegExp(`^原生资源 .* ${external}$`) })
async function link(page: Page, external: string, assetId: string, name: string) {
  await native(page, external).getByRole("button", { name: "处理候选", exact: true }).click()
  const dialog = page.getByRole("dialog", { name: "预览虚拟资源变更" })
  await dialog.getByLabel("查询已有服务器资产", { exact: true }).fill(name)
  await dialog.getByRole("button", { name: "查询资产", exact: true }).click()
  await expect(dialog.getByLabel("目标资产", { exact: true }).locator(`option[value="${assetId}"]`)).toHaveCount(1)
  await dialog.getByLabel("目标资产", { exact: true }).selectOption(assetId)
  await dialog.getByRole("button", { name: "预览变更", exact: true }).click()
  await expect(dialog.getByRole("region", { name: "虚拟资源变更预览" })).toBeVisible()
  await dialog.getByRole("button", { name: "确认本次变更", exact: true }).click()
  await expect(dialog).toHaveCount(0)
}

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext()
  const p = await context.newPage()
  await login(p)
  for (const role of ["operator", "viewer"]) {
    await api(p, "post", "/api/users", { username: `fake-virt-${role}`, role, password: "fake-virt-initial-123" })
    const ctx = await browser.newContext()
    const m = await ctx.newPage()
    expect((await m.request.post("/api/auth/login", { data: { username: `fake-virt-${role}`, password: "fake-virt-initial-123" } })).ok()).toBeTruthy()
    await api(m, "post", "/api/auth/password", { old_password: "fake-virt-initial-123", new_password: memberPassword })
    await ctx.close()
  }
  const credential = await api(p, "post", "/api/credentials", { alias: "fake-virt-token", kind: "token", username: "fake-reader", secret: "fake-virt-initial" })
  credentialId = credential.id
  const platform = await api(p, "post", "/api/assets", { name: "fake-virt-management", form: "physical", role: "other", lifecycle: "planned", collection_profile: "pve", endpoints: [{ address: "192.0.2.180", protocol: "pve-api", credential_id: credential.id }] })
  platformId = platform.id
  for (const name of ["fake-virt-host-a", "fake-virt-host-b", "fake-virt-existing-vm"]) {
    const a = await api(p, "post", "/api/assets", { name, form: name.endsWith("vm") ? "vm" : "physical", role: "server", lifecycle: "planned" })
    if (name.endsWith("-a")) hostA = a.id
    else if (name.endsWith("-b")) hostB = a.id
    else vm = a.id
  }
  await api(p, "post", "/api/relations", { source_asset_id: hostA, target_asset_id: vm, kind: "hosts" })
  const vmware = await api(p, "post", "/api/assets", { name: "fake-virt-vmware", form: "physical", role: "other", lifecycle: "planned", collection_profile: "esxi", endpoints: [{ address: "192.0.2.181", protocol: "esxi-api", credential_id: credential.id }] })
  await collect(p, platform.id)
  await collect(p, vmware.id)
  const sources = await (await p.request.get("/api/virtualization/sources")).json()
  sourceId = sources.items.find((s: { asset_id: string }) => s.asset_id === platform.id).id
  vmwareSourceId = sources.items.find((s: { asset_id: string }) => s.asset_id === vmware.id).id
  await context.close()
})

test("人工关联宿主与重名 VM、预览取消、新建、迁移保留人工关系", async ({ page }) => {
  await login(page)
  await open(page)
  await expect(native(page, "100")).toContainText("尚未关联")
  await expect(native(page, "101")).toContainText("尚未关联")
  await link(page, "node-a", hostA, "fake-virt-host-a")
  await link(page, "node-b", hostB, "fake-virt-host-b")
  await link(page, "100", vm, "fake-virt-existing-vm")
  await expect(native(page, "100")).toContainText("已关联")
  await native(page, "101").getByRole("button", { name: "处理候选", exact: true }).click()
  const dialog = page.getByRole("dialog", { name: "预览虚拟资源变更" })
  await dialog.getByLabel("操作", { exact: true }).selectOption("create")
  await dialog.getByLabel("新资产名称", { exact: true }).fill("fake-virt-created-vm")
  await dialog.getByRole("button", { name: "预览变更", exact: true }).click()
  await expect(dialog.getByRole("region", { name: "虚拟资源变更预览" })).toContainText("fake-virt-created-vm")
  await dialog.getByRole("button", { name: "取消", exact: true }).click()
  expect((await (await page.request.get("/api/assets?q=fake-virt-created-vm")).json()).total).toBe(0)
  await native(page, "101").getByRole("button", { name: "处理候选", exact: true }).click()
  await dialog.getByLabel("操作", { exact: true }).selectOption("create")
  await dialog.getByLabel("新资产名称", { exact: true }).fill("fake-virt-created-vm")
  await dialog.getByRole("button", { name: "预览变更", exact: true }).click()
  await expect(dialog.getByRole("button", { name: "确认本次变更", exact: true })).toBeEnabled()
  await dialog.getByRole("button", { name: "确认本次变更", exact: true }).click()
  await expect(dialog).toHaveCount(0)
  const created = await (await page.request.get("/api/assets?q=fake-virt-created-vm")).json()
  expect(created.total).toBe(1)
  expect(created.items[0].endpoints).toEqual([])
  expect(created.items[0].lifecycle).toBe("planned")
  await api(page, "patch", `/api/credentials/${credentialId}`, { secret: "fake-virt-moved" })
  await collect(page, platformId)
  await page.getByRole("button", { name: "刷新候选", exact: true }).click()
  await expect(native(page, "100")).toContainText("需要复核")
  await native(page, "100").getByRole("button", { name: "复核关联", exact: true }).click()
  await dialog.getByRole("button", { name: "预览变更", exact: true }).click()
  const preview = dialog.getByRole("region", { name: "虚拟资源变更预览" })
  await expect(preview).toContainText("fake-virt-host-b")
  await expect(preview).toContainText("fake-virt-host-a · 人工关系")
  await dialog.getByRole("button", { name: "确认本次变更", exact: true }).click()
  await expect(dialog).toHaveCount(0)
  const asset = await (await page.request.get(`/api/assets/${vm}`)).json()
  expect(asset.relations.filter((r: { kind: string }) => r.kind === "hosts")).toHaveLength(2)
  await page.getByLabel("虚拟化实例范围", { exact: true }).selectOption(vmwareSourceId)
  await expect(native(page, "100")).toContainText("尚未关联")
})

test("预览后来源失联拒绝确认，资产与关系不变", async ({ page }) => {
  await login(page)
  await open(page)
  await native(page, "200").getByRole("button", { name: "处理候选", exact: true }).click()
  const dialog = page.getByRole("dialog", { name: "预览虚拟资源变更" })
  await dialog.getByLabel("操作", { exact: true }).selectOption("create")
  await dialog.getByLabel("新资产名称", { exact: true }).fill("fake-virt-must-not-create")
  await dialog.getByRole("button", { name: "预览变更", exact: true }).click()
  await expect(dialog.getByRole("button", { name: "确认本次变更", exact: true })).toBeEnabled()
  await api(page, "patch", `/api/credentials/${credentialId}`, { secret: "fake-virt-failed" })
  // 采集失败在任务结果中记录，读任务本身仍完成。
  await collect(page, platformId)
  await dialog.getByRole("button", { name: "确认本次变更", exact: true }).click()
  await expect(dialog.getByRole("alert")).toContainText("来源失联")
  expect((await (await page.request.get("/api/assets?q=fake-virt-must-not-create")).json()).total).toBe(0)
  await dialog.getByRole("button", { name: "取消", exact: true }).click()
  await page.getByRole("button", { name: "刷新候选", exact: true }).click()
  await expect(native(page, "100")).toContainText("平台电源观察：未知")
})

for (const role of ["operator", "viewer"]) {
  test(`${role} 候选权限与375深色200%字号`, async ({ page }) => {
    await login(page, role)
    await page.setViewportSize({ width: 375, height: 900 })
    await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" })
    await open(page)
    await page.addStyleTag({ content: "html { font-size:200% !important }" })
    await expect(native(page, "100")).toBeVisible()
    await expect(page.getByRole("button", { name: "处理候选", exact: true }).first()).toHaveCount(role === "operator" ? 1 : 0)
    expect(await page.getByRole("region", { name: "总览内容" }).evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBeTruthy()
    if (role === "operator") {
      await native(page, "100").getByRole("button", { name: "复核关联", exact: true }).click()
      const dialog = page.getByRole("dialog")
      expect(await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBeTruthy()
    }
  })
}
