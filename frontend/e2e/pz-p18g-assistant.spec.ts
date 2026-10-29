// 4.3 P18g：问答助手“问云枢”——开启后顶栏出现入口，提问后显示查询步骤与回答（伪造模型，代号已换回资产名）。
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

test("问云枢：只读问答", async ({ page }) => {
  await login(page)
  const headers = { "X-CSRF-Token": await csrf(page) }
  let providers = await (await page.request.get("/api/ai/providers")).json()
  if (!providers.length) {
    await page.request.post("/api/ai/providers", { headers, data: { name: "fake-p18g", protocol: "openai", base_url: "https://llm.example.invalid/v1",
      model: "fake-deepseek-chat", api_key: "fake-ai-key", quirks: ["json_object_only"] } })
    providers = await (await page.request.get("/api/ai/providers")).json()
  }
  const s = await (await page.request.get("/api/ai/settings")).json()
  const features = Object.fromEntries(Object.entries(s.features).map(([k, f]: [string, any]) => [k, { enabled: f.enabled, provider_id: f.provider_id }]))
  expect((await page.request.put("/api/ai/settings", { headers, data: { ...s, enabled: true, default_provider_id: providers[0].id,
    features: { ...features, assistant: { enabled: true, provider_id: null } } } })).ok()).toBeTruthy()

  await page.goto("/#/assets")
  await page.getByRole("button", { name: "问云枢（AI 问答助手）" }).click()
  const panel = page.getByRole("dialog", { name: "问云枢" })
  await panel.getByLabel("问题").fill("哪台 CPU 使用率最高？")
  await panel.getByRole("button", { name: "发送" }).click()
  const chat = panel.getByLabel("对话")
  await expect(chat).toContainText("查询了：按指标排序")
  await expect(chat).toContainText("伪造回答：CPU 使用率最高的是")
  await expect(chat).not.toContainText("ASSET_1")
  await expect(chat.getByRole("link", { name: "打开" })).toHaveAttribute("href", "#/monitoring")
})
