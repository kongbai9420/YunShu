// 4.3 P18c 漏洞情报：外网同步默认关闭；开启推送接收 → 两个来源推送同一 CVE → 列表合并显示、详情列出来源；订阅。数据全部伪造，不访问外网。
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

test("漏洞情报：推送接收、多来源合并、订阅与数据源", async ({ page }) => {
  await login(page)
  await page.goto("/#/vulns")
  await expect(page.getByRole("heading", { name: "漏洞情报", exact: true })).toBeVisible()

  await page.getByRole("tab", { name: "数据源" }).click()
  await expect(page.getByRole("checkbox", { name: "允许平台访问外网同步漏洞数据" })).not.toBeChecked()
  await expect(page.getByText("外网同步已关闭")).toBeVisible()
  await expect(page.getByRole("list", { name: "数据源" })).toContainText("长亭漏洞库")

  await page.getByRole("tab", { name: "离线包与推送" }).click()
  await page.getByRole("button", { name: "开启并生成接收地址" }).click()
  const url = (await page.locator("code").first().textContent())!.trim()
  expect(url).toContain("/api/vuln/webhook/ysw_")
  const path = new URL(url).pathname
  const push = (content: Record<string, unknown>) => page.request.post(path, { data: { type: "watchvuln-vulninfo", content } })
  expect((await push({ unique_key: "CT-2026-9001", title: "Fake 设备远程命令执行漏洞", severity: "严重", cve: "CVE-2026-49001", from: "https://stack.chaitin.com/vuldb/detail/fake" })).ok()).toBeTruthy()
  expect((await push({ unique_key: "AVD-2026-49001", title: "Fake device RCE", severity: "高危", cve: "CVE-2026-49001", from: "https://avd.aliyun.com/detail?id=AVD-2026-49001" })).ok()).toBeTruthy()

  await page.getByRole("tab", { name: "漏洞", exact: true }).click()
  const list = page.getByRole("list", { name: "漏洞列表" })
  await expect(list.getByText("CVE-2026-49001")).toBeVisible()
  await expect(list).toContainText("来源：长亭漏洞库、阿里云漏洞库")
  await list.getByRole("button", { name: /CVE-2026-49001/ }).click()
  const dialog = page.getByRole("dialog")
  await expect(dialog.getByRole("heading", { name: /Fake 设备远程命令执行漏洞/ })).toBeVisible()
  await expect(dialog.getByRole("region", { name: "发布来源" })).toContainText("发布来源（2）")
  await dialog.getByRole("button", { name: "关闭", exact: true }).last().click()

  await page.getByRole("tab", { name: "订阅" }).click()
  await page.getByLabel("产品").fill("cpe:2.3:a:openbsd:openssh")
  await page.getByRole("button", { name: "添加订阅" }).click()
  await expect(page.getByRole("list", { name: "订阅列表" })).toContainText("openbsd:openssh")
})
