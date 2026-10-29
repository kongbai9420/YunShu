// 4.3 P18a AI 设置：默认关闭、厂商预设、获取模型列表、连接自检得出能力表、按功能开启。模型接口是伪造的（e2e/server.py），不访问外网。
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

test("AI 设置：预设、自检与按功能开启", async ({ page }) => {
  await login(page)
  await page.goto("/#/settings")
  await page.getByRole("tab", { name: "AI" }).click()
  await expect(page.getByRole("checkbox", { name: "启用 AI 功能" })).not.toBeChecked()
  await expect(page.getByLabel("隐私模式")).toHaveValue("redact")

  await page.getByRole("button", { name: "新增模型接口" }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByLabel("厂商").selectOption("deepseek")
  await expect(dialog.getByLabel("接口地址")).toHaveValue("https://api.deepseek.com")
  await expect(dialog.getByRole("checkbox", { name: "只支持 json_object" })).toBeChecked()
  await dialog.getByLabel("名称").fill("fake-deepseek")
  await dialog.getByLabel("接口密钥").fill("fake-ai-key")
  await dialog.getByRole("button", { name: "获取模型列表" }).click()
  await expect(dialog.getByText("可选模型 2 个")).toBeVisible()
  await dialog.locator("#ai-model").fill("fake-deepseek-chat")
  await dialog.getByRole("button", { name: "保存" }).click()

  const card = page.getByRole("article", { name: "模型接口 fake-deepseek" })
  await expect(card.getByText("尚未自检")).toBeVisible()
  await expect(card.getByText("该厂商按文档适配，未实测")).toBeVisible()
  await expect(card).not.toContainText("fake-ai-key")
  await card.getByRole("button", { name: "连接自检" }).click()
  await expect(card.getByText("✓ 结构化输出（json_object）", { exact: true })).toBeVisible()
  await expect(card.getByText("✓ 工具调用", { exact: true })).toBeVisible()
  await expect(card.getByRole("list", { name: "自检详情" })).toContainText("普通回复（地址与密钥）")

  await page.getByRole("checkbox", { name: "启用 AI 功能" }).check()
  await page.getByLabel("默认模型接口").selectOption({ label: "fake-deepseek（fake-deepseek-chat）" })
  await page.getByRole("checkbox", { name: "漏洞影响分析" }).check()
  await page.getByRole("button", { name: "保存设置" }).click()
  await expect(page.getByText("已保存")).toBeVisible()
  await page.reload()
  await page.getByRole("tab", { name: "AI" }).click()
  await expect(page.getByRole("checkbox", { name: "漏洞影响分析" })).toBeChecked()
})
