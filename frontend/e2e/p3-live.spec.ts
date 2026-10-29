import { expect, test, type Page } from "@playwright/test"

const password = "fake-e2e-password-123"
test.describe.configure({ mode: "serial" })

/** 单独运行本文件时 admin 还是初始密码，先改密；和 p2 一起跑时已经改过 */
async function login(page: Page) {
  await page.goto("/#/assets")
  await page.getByRole("textbox", { name: "用户名", exact: true }).fill("admin")
  await page.getByLabel(/^密码/).fill(password)
  await page.getByRole("button", { name: "登录", exact: true }).click()
  const changed = await page
    .getByRole("heading", { name: "登录云枢运维平台", exact: true })
    .waitFor({ state: "detached", timeout: 3000 })
    .then(() => true)
    .catch(() => false)
  if (!changed) {
    await page.getByLabel(/^密码/).fill("admin123")
    await page.getByRole("button", { name: "登录", exact: true }).click()
    await page.getByLabel("当前密码").fill("admin123")
    await page.getByLabel(/^新密码/).fill(password)
    await page.getByLabel("再次输入新密码").fill(password)
    await page.getByRole("button", { name: "保存新密码" }).click()
    await expect(page.getByLabel("当前密码")).toHaveCount(0)
  }
  await expect(page.getByRole("heading", { name: "登录云枢运维平台", exact: true })).toHaveCount(0)
}

async function post(page: Page, path: string, body?: unknown) {
  const csrf = (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value
  const response = await page.request.post(path, { data: body, headers: { "X-CSRF-Token": csrf } })
  expect(response.ok(), await response.text()).toBeTruthy()
  return response.json()
}

test("资产列表实时显示采集状态，不需要手动刷新", async ({ page }) => {
  await login(page)
  const asset = await post(page, "/api/assets", {
    name: "实时推送样本",
    asset_no: "FAKE-LIVE-1",
    form: "physical",
    role: "server",
    platform: "ubuntu",
    collection_profile: "linux",
    endpoints: [{ address: "192.0.2.40", protocol: "ssh", port: 22 }],
  })
  // 登录后已停在 #/assets，goto 同一地址只改 hash 不重新加载；列表可能在资产创建前就读完，必须重新加载
  await page.reload()
  await expect(page.getByRole("status").filter({ hasText: "实时" }).first()).toBeVisible()
  const row = page.getByRole("row").filter({ hasText: "实时推送样本" })
  await expect(row).toBeVisible()
  // 页面晚于连接挂载时也要立即拿到快照（补发缓存），不能等到服务器下次重连
  await expect(row.getByText("读取中…")).toHaveCount(0, { timeout: 5000 })
  // 页面打开后不再刷新：采集状态只来自实时推送（定时采集可能先到，所以先等任一状态出现）
  await expect(row.getByText(/尚未采集|采集正常/)).toBeVisible()
  await post(page, `/api/assets/${asset.id}/collect`)
  await expect(row.getByText("采集正常", { exact: true })).toBeVisible()
})

test("从其他页面切到资产列表：实时连接已建立时仍能拿到采集状态，不停在读取中", async ({ page }) => {
  await login(page)
  await page.goto("/#/monitoring")
  await expect(page.getByRole("status").filter({ hasText: "实时" }).first()).toBeVisible()
  // 顶栏一直保持连接，全量快照早已发过；应用内切换页面不重新加载
  await page.waitForTimeout(1500)
  await page.getByRole("navigation", { name: "主导航" }).getByRole("link", { name: "资产列表", exact: true }).click()
  const row = page.getByRole("row").filter({ hasText: "实时推送样本" })
  await expect(row).toBeVisible()
  await expect(row.getByText("读取中…")).toHaveCount(0, { timeout: 5000 })
  await expect(row.getByText(/尚未采集|采集正常/)).toBeVisible()
})

test("监控中心指标随推送更新，筛选输入不被覆盖", async ({ page }) => {
  await login(page)
  await page.goto("/#/monitoring")
  await expect(page.getByRole("status").filter({ hasText: "实时" }).first()).toBeVisible()
  await page.getByLabel("当前资产").selectOption({ label: "实时推送样本（FAKE-LIVE-1）" })
  const filter = page.getByLabel("筛选指标")
  await filter.fill("cpu")
  await expect(page.getByText("实时推送更新", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "立即采集", exact: true }).click()
  const table = page.getByRole("table", { name: "最新指标，空值用横线和质量状态表示" })
  await expect(table.getByText("23.5 %", { exact: true })).toBeVisible()
  await expect(filter).toHaveValue("cpu")
  await expect(table.getByRole("row").filter({ hasText: "power.watts" })).toHaveCount(0)
})

test("资产表格列头排序与列宽调整（键盘可用、按浏览器记住）", async ({ page }) => {
  await login(page)
  for (const [name, no] of [
    ["排序样本甲", "FAKE-SORT-2"],
    ["排序样本乙", "FAKE-SORT-1"],
  ])
    await post(page, "/api/assets", { name, asset_no: no, form: "physical", role: "server" })
  await page.goto("/#/assets")
  await page.getByLabel("搜索", { exact: true }).fill("排序样本")
  const header = page.getByRole("columnheader", { name: /名称/ })
  await expect(header).toHaveAttribute("aria-sort", "ascending")
  const names = page.locator("tbody th button")
  await expect(names).toHaveText(["排序样本乙", "排序样本甲"].sort())
  await header.getByRole("button").focus()
  await page.keyboard.press("Enter")
  await expect(header).toHaveAttribute("aria-sort", "descending")
  await expect(names).toHaveText(["排序样本乙", "排序样本甲"].sort().reverse())
  await page.getByLabel("排序", { exact: true }).selectOption({ label: "资产编号" })
  await expect(names).toHaveText(["排序样本乙", "排序样本甲"])

  const handle = page.getByRole("separator", { name: "调整“名称”列宽" })
  await handle.focus()
  await page.keyboard.press("ArrowRight")
  await page.keyboard.press("ArrowRight")
  await expect(handle).toHaveAttribute("aria-valuenow", "252")
  await expect(page.getByRole("button", { name: "恢复默认列宽" })).toBeVisible()
  await page.waitForTimeout(400)
  await page.reload()
  await expect(page.getByRole("separator", { name: "调整“名称”列宽" })).toHaveAttribute("aria-valuenow", "252")
  await page.getByRole("button", { name: "恢复默认列宽" }).click()
  await expect(page.getByRole("separator", { name: "调整“名称”列宽" })).toHaveAttribute("aria-valuenow", "220")
})

test("后端短暂不可用时不会被当成退出登录，恢复后自动回到页面", async ({ page }) => {
  await login(page)
  await expect(page.getByRole("heading", { name: "资产管理", exact: true })).toBeVisible()
  // 模拟后端重启：反向代理返回 502
  await page.route("**/api/auth/me", (route) => route.fulfill({ status: 502, body: "Bad Gateway" }))
  await page.reload()
  await expect(page.getByRole("alert").filter({ hasText: "无法连接后端服务" })).toBeVisible()
  await page.unroute("**/api/auth/me")
  await expect(page.getByRole("heading", { name: "资产管理", exact: true })).toBeVisible({ timeout: 10_000 })
})
