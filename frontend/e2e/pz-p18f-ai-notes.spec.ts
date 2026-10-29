// 4.3 P18f：AI 巡检摘要与告警解读（伪造模型）。依赖 pz-p18e 已配置好的伪造模型接口；单独运行时本用例自己配置。
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

test("AI 巡检摘要与告警解读", async ({ page }) => {
  await login(page)
  const headers = { "X-CSRF-Token": await csrf(page) }
  let providers = await (await page.request.get("/api/ai/providers")).json()
  if (!providers.length) {
    await page.request.post("/api/ai/providers", { headers, data: { name: "fake-p18f", protocol: "openai", base_url: "https://llm.example.invalid/v1",
      model: "fake-deepseek-chat", api_key: "fake-ai-key", quirks: ["json_object_only"] } })
    providers = await (await page.request.get("/api/ai/providers")).json()
  }
  const s = await (await page.request.get("/api/ai/settings")).json()
  const features = Object.fromEntries(Object.entries(s.features).map(([k, f]: [string, any]) => [k, { enabled: f.enabled, provider_id: f.provider_id }]))
  expect((await page.request.put("/api/ai/settings", { headers, data: { ...s, enabled: true, default_provider_id: providers[0].id,
    features: { ...features, alert_explain: { enabled: true, provider_id: null }, inspection_summary: { enabled: true, provider_id: null } } } })).ok()).toBeTruthy()

  await page.goto("/#/reports")
  await page.getByRole("button", { name: "立即巡检" }).click()
  const summary = page.getByRole("region", { name: "AI 摘要" })
  await summary.getByRole("button", { name: "生成摘要" }).click()
  await expect(summary).toContainText("伪造的巡检摘要")
  await expect(summary).toContainText("需关注")
})
