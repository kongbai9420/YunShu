// 4.2 P16 巡检报告与只读令牌。文件名以 pz- 开头，排在 p2 首次登录用例之后（见 pz-p11-scope.spec.ts 说明）
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

test("巡检报告：立即巡检、导出与打印入口；只读令牌抓取指标", async ({ page }) => {
  await login(page)
  await page.goto("/#/reports")
  await expect(page.getByRole("heading", { name: "巡检报告", exact: true })).toBeVisible()
  await page.getByRole("button", { name: "立即巡检" }).click()
  const result = page.getByRole("region", { name: "巡检结果" })
  await expect(result.getByText(/检查了 \d+ 台在用资产/)).toBeVisible()
  await expect(result.getByRole("link", { name: "导出 CSV" })).toHaveAttribute("href", /\/api\/inspections\/.+\/export\.csv$/)
  await expect(result.getByRole("button", { name: "打印 / 另存 PDF" })).toBeVisible()
  const href = await result.getByRole("link", { name: "导出 CSV" }).getAttribute("href")
  const csv = await page.request.get(href!)
  expect(csv.headers()["content-type"]).toContain("text/csv")
  expect((await csv.text()).startsWith("﻿级别,类别,资产")).toBe(true)

  await page.goto("/#/settings")
  await page.getByRole("tab", { name: "API 令牌" }).click()
  await page.getByLabel("令牌名称").fill("fake-prometheus")
  await page.getByRole("button", { name: "创建令牌" }).click()
  const token = (await page.getByRole("region", { name: "新令牌" }).locator("code").textContent())!.trim()
  expect(token.startsWith("ys_")).toBe(true)
  await page.getByRole("button", { name: "我已保存" }).click()
  await expect(page.getByRole("list", { name: "我的令牌" }).getByText("fake-prometheus")).toBeVisible()
  const metrics = await page.request.get("/metrics", { headers: { Authorization: `Bearer ${token}` } })
  expect(metrics.status()).toBe(200)
  expect(metrics.headers()["content-type"]).toContain("text/plain")
  expect((await page.request.get("/metrics", { headers: { Authorization: "Bearer ys_wrong" } })).status()).toBe(401)

  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto("/#/reports")
  await expect(page.getByRole("region", { name: "巡检结果" })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})
