// 4.3 P18e：漏洞 AI 影响分析（伪造模型）、预览发送内容、通知设置、仪表盘漏洞小组件。设备与模型都是伪造的，不访问外网。
import { expect, test, type Page } from "@playwright/test"
import { fileURLToPath } from "node:url"

const password = "fake-e2e-password-123"

async function csrf(page: Page) { return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value }
async function login(page: Page) {
  const r = await page.request.post("/api/auth/login", { data: { username: "admin", password } })
  if (!r.ok()) {
    expect((await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })).ok()).toBeTruthy()
    expect((await page.request.post("/api/auth/password", { data: { old_password: "admin123", new_password: password }, headers: { "X-CSRF-Token": await csrf(page) } })).ok()).toBeTruthy()
  }
}

test("AI 影响分析、通知设置与仪表盘", async ({ page }) => {
  await login(page)
  const headers = { "X-CSRF-Token": await csrf(page) }
  const asset = await (await page.request.post("/api/assets", { headers, data: { name: "fake-idrac-p18e", form: "physical", role: "server", vendor: "Dell",
    model: "PowerEdge R740", lifecycle: "active", endpoints: [{ address: "192.0.2.43", port: 161, protocol: "snmp" }] } })).json()
  expect((await page.request.post(`/api/assets/${asset.id}/collect`, { headers })).ok()).toBeTruthy()
  await expect.poll(async () => (await (await page.request.get(`/api/assets/${asset.id}/software`)).json()).items.length, { timeout: 30000 }).toBe(1)
  const bundle = await page.request.post("/api/vuln/bundle", { headers, multipart: { file: { name: "b.zip", mimeType: "application/zip",
    buffer: Buffer.from(await (await import("node:fs/promises")).readFile(fileURLToPath(new URL("./fixtures/vulndb-mini.zip", import.meta.url)))) } } })
  expect(bundle.ok()).toBeTruthy()
  await expect.poll(async () => (await (await page.request.get(`/api/vuln/matches?asset_id=${asset.id}`)).json()).length, { timeout: 30000 }).toBe(1)

  // 配置伪造的模型接口并开启“漏洞影响分析”
  const p = await (await page.request.post("/api/ai/providers", { headers, data: { name: "fake-p18e", protocol: "openai", base_url: "https://llm.example.invalid/v1",
    model: "fake-deepseek-chat", api_key: "fake-ai-key", quirks: ["json_object_only"] } })).json()
  const s = await (await page.request.get("/api/ai/settings")).json()
  expect((await page.request.put("/api/ai/settings", { headers, data: { ...s, enabled: true, default_provider_id: p.id,
    features: { ...Object.fromEntries(Object.entries(s.features).map(([k, f]: [string, any]) => [k, { enabled: f.enabled, provider_id: f.provider_id }])), cve_analysis: { enabled: true, provider_id: null } } } })).ok()).toBeTruthy()

  await page.goto("/#/vulns?id=CVE-2018-1243")
  const ai = page.getByRole("region", { name: "AI 影响分析" })
  await expect(ai).toContainText("AI 生成，仅供参考")
  await ai.getByRole("button", { name: /AI 分析|重新分析/ }).click()
  await expect(ai).toContainText("fake-idrac-p18e 的 iDRAC 固件在受影响范围内")  // 代号已换回资产名
  await expect(ai).toContainText("尽快处理")
  await ai.getByRole("button", { name: "预览将发送的内容" }).click()
  await expect(ai.locator("pre")).toContainText("ASSET_")
  await page.getByRole("dialog").getByRole("button", { name: "关闭", exact: true }).last().click()

  await page.getByRole("tab", { name: "通知" }).click()
  await expect(page.getByRole("checkbox", { name: "发送漏洞通知" })).toBeChecked()
  await expect(page.getByLabel("汇总窗口（分钟）")).toHaveValue("5")
  await expect(page.getByRole("checkbox", { name: /订阅命中预警/ })).toBeChecked()

  await page.goto("/#/overview")
  await expect(page.getByRole("region", { name: "漏洞" })).toContainText("CVE-2018-1243")
})
