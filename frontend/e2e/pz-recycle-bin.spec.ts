// 资产回收站（P22）：删除移入回收站、从回收站恢复、管理员彻底删除。不连接任何设备。
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
async function asset(page: Page, name: string) {
  const r = await page.request.post("/api/assets", { data: { name, form: "physical", role: "server", lifecycle: "active" }, headers: { "X-CSRF-Token": await csrf(page) } })
  expect(r.ok()).toBeTruthy()
  return r.json()
}

test("删除的资产进回收站，可恢复，也可彻底删除", async ({ page }) => {
  await login(page)
  const keep = await asset(page, `fake-bin-keep-${Date.now()}`)
  const gone = await asset(page, `fake-bin-gone-${Date.now()}`)

  for (const a of [keep, gone]) {
    await page.goto(`/#/assets?asset=${a.id}`)
    const dialog = page.getByRole("dialog")
    await expect(dialog).toContainText(a.name)
    await dialog.getByRole("tab", { name: "编辑", exact: true }).click()
    await dialog.getByRole("button", { name: "删除资产" }).click()
    await expect(dialog).toContainText("移入回收站，保留 30 天")
    await dialog.getByRole("button", { name: "确认删除" }).click()
    await expect(dialog).toBeHidden()
  }
  await page.getByLabel("搜索").fill("fake-bin-")
  await expect(page.getByText("没有符合条件的资产")).toBeVisible()

  await page.getByRole("button", { name: "回收站" }).click()
  const keepRow = page.getByRole("row", { name: new RegExp(keep.name) })
  const goneRow = page.getByRole("row", { name: new RegExp(gone.name) })
  await expect(keepRow).toContainText("30 天")
  await keepRow.getByRole("button", { name: "恢复" }).click()
  await expect(page.getByText(`已恢复“${keep.name}”`)).toBeVisible()
  await expect(keepRow).toHaveCount(0)

  await goneRow.getByRole("button", { name: "彻底删除" }).click()
  await goneRow.getByRole("button", { name: "确认彻底删除" }).click()
  await expect(page.getByText(`已彻底删除“${gone.name}”`)).toBeVisible()

  await page.getByRole("button", { name: "返回资产列表" }).click()
  await expect(page.getByRole("button", { name: keep.name })).toBeVisible()
})
