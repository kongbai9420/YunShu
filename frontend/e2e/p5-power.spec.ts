import { expect, test, type Page } from "@playwright/test"

// 设备全部是 e2e/server.py 里的伪造 BMC，不连接任何真实设备
test.describe.configure({ mode: "serial" })
const password = "fake-e2e-password-123"
async function csrf(page: Page) {
  return (await page.context().cookies()).find((c) => c.name === "yunshu_csrf")!.value
}
async function login(page: Page) {
  let response = await page.request.post("/api/auth/login", { data: { username: "admin", password } })
  if (!response.ok()) {
    response = await page.request.post("/api/auth/login", { data: { username: "admin", password: "admin123" } })
    expect(response.ok()).toBeTruthy()
    response = await page.request.post("/api/auth/password", {
      data: { old_password: "admin123", new_password: password },
      headers: { "X-CSRF-Token": await csrf(page) },
    })
    expect(response.ok()).toBeTruthy()
  }
}
async function post(page: Page, path: string, body: unknown) {
  const response = await page.request.post(path, { data: body, headers: { "X-CSRF-Token": await csrf(page) } })
  expect(response.ok(), await response.text()).toBeTruthy()
  return response.json()
}
async function bmcAsset(page: Page, name: string, address: string) {
  const cred = await post(page, "/api/credentials", { alias: `fake-${name}`, kind: "password", username: "root", secret: "fake-bmc-e2e" })
  return post(page, "/api/assets", {
    name,
    form: "physical",
    role: "server",
    platform: "dell-idrac",
    lifecycle: "active",
    endpoints: [{ address, protocol: "ipmi", port: 623, credential_id: cred.id }],
  })
}

test("电源控制：高风险要输入确认、结果分开显示、超时为结果不确定且需确认解除", async ({ page }) => {
  await login(page)
  await bmcAsset(page, "电源样本甲", "192.0.2.250")
  await bmcAsset(page, "电源样本乙", "192.0.2.251")
  await page.goto("/#/operations")
  await expect(page.getByRole("heading", { name: "运维操作", exact: true })).toBeVisible()
  await expect(page.getByText("控制仍由旧控制器负责")).toHaveCount(0)

  await page.getByLabel("搜索资产").fill("电源样本")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await page.getByLabel("电源样本甲", { exact: true }).check()
  await page.getByRole("button", { name: "强制断电", exact: true }).click()
  const dialog = page.getByRole("dialog")
  await expect(dialog).toContainText("高风险操作")
  const confirm = dialog.getByRole("button", { name: "确认强制断电" })
  await expect(confirm).toBeDisabled()
  await dialog.getByLabel("请输入“电源样本甲”确认").fill("电源样本甲")
  // 双击只会执行一次
  await confirm.dblclick()
  const result = page.locator("section").filter({ hasText: "本次操作结果" })
  await expect(result.getByText("已查询确认")).toBeVisible()
  await expect(result.getByRole("row")).toHaveCount(2)

  // 超时：结果不确定，不重试，暂停该设备的控制直到确认解除
  await page.getByLabel("电源样本甲", { exact: true }).uncheck()
  await page.getByLabel("电源样本乙", { exact: true }).check()
  await page.getByRole("button", { name: "开机", exact: true }).click()
  await page.getByRole("dialog").getByRole("button", { name: "确认开机" }).click()
  await expect(page.locator("section").filter({ hasText: "本次操作结果" }).getByText("结果不确定")).toBeVisible()
  const notice = page.getByRole("alert").filter({ hasText: "控制结果不确定" })
  await expect(notice).toBeVisible({ timeout: 15000 })
  await notice.getByLabel("核对结果").fill("已登录 BMC 确认已开机")
  await notice.getByRole("button", { name: "确认解除" }).click()
  await expect(page.getByRole("alert").filter({ hasText: "控制结果不确定" })).toHaveCount(0)

  // 能力说明：只读查看每台设备能做什么
  await page.getByRole("listitem").filter({ hasText: "电源样本甲" }).getByRole("button", { name: "查看能力" }).click()
  await expect(page.getByText("电源样本甲 的电源控制能力")).toBeVisible()

  await page.getByRole("button", { name: "操作记录", exact: true }).click()
  await expect(page.getByRole("table", { name: "控制操作记录" })).toContainText("强制断电")
})

test("风扇温控：低于安全下限被拒绝不自动调整，未登记机型零写，恢复自动显示无法确认", async ({ page }) => {
  await login(page)
  await bmcAsset(page, "温控样本甲", "192.0.2.253")
  await bmcAsset(page, "温控样本乙", "192.0.2.252")
  await page.goto("/#/operations")
  await page.getByRole("button", { name: "风扇与温控", exact: true }).click()
  await page.getByLabel("搜索资产").fill("温控样本")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await page.getByLabel("温控样本甲", { exact: true }).check()

  await page.getByRole("button", { name: "全局固定转速", exact: true }).click()
  let dialog = page.getByRole("dialog")
  await dialog.getByLabel(/目标转速/).fill("20")
  await dialog.getByLabel("请输入“温控样本甲”确认").fill("温控样本甲")
  await dialog.getByRole("button", { name: "确认全局固定转速" }).click()
  const result = page.locator("section").filter({ hasText: "本次操作结果" })
  await expect(result).toContainText("安全下限 30%")
  await expect(result.getByText("已拒绝（未发送）")).toBeVisible()

  await page.getByRole("button", { name: "全局固定转速", exact: true }).click()
  dialog = page.getByRole("dialog")
  await dialog.getByLabel(/目标转速/).fill("45")
  await dialog.getByLabel("请输入“温控样本甲”确认").fill("温控样本甲")
  await dialog.getByRole("button", { name: "确认全局固定转速" }).click()
  await expect(page.locator("section").filter({ hasText: "本次操作结果" }).getByText("已接受，无法确认")).toBeVisible()
  await page.getByRole("listitem").filter({ hasText: "温控样本甲" }).getByRole("button", { name: "查看能力" }).click()
  await expect(page.getByText("手动固定转速（已下发） · 45%")).toBeVisible()

  await page.getByLabel("温控样本甲", { exact: true }).uncheck()
  await page.getByLabel("温控样本乙", { exact: true }).check()
  await page.getByRole("button", { name: "恢复原厂自动温控", exact: true }).click()
  await page.getByRole("dialog").getByRole("button", { name: "确认恢复原厂自动温控" }).click()
  await expect(page.locator("section").filter({ hasText: "本次操作结果" })).toContainText("不会尝试其他厂商")
})

test("温控策略：编辑曲线带修订号，为服务器添加并启用策略，没有温度读数时不调速", async ({ page }) => {
  await login(page)
  await page.goto("/#/operations")
  await page.getByRole("button", { name: "温控策略", exact: true }).click()
  const curve = page.locator("section").filter({ hasText: "温控曲线（全局）" })
  for (const [t, s] of [
    ["40", "30"],
    ["60", "50"],
    ["82", "100"],
  ]) {
    await curve.getByRole("button", { name: "添加节点" }).click()
    const i = await curve.getByLabel(/个节点温度$/).count()
    await curve.getByLabel(`第 ${i} 个节点温度`).fill(t)
    await curve.getByLabel(`第 ${i} 个节点转速`).fill(s)
  }
  await curve.getByRole("button", { name: "保存曲线" }).click()
  await expect(curve.getByText("已保存为修订 1，熔断温度 82℃")).toBeVisible()

  await page.getByLabel("为服务器添加策略").selectOption({ label: "温控样本甲" })
  await page.getByRole("button", { name: "添加（先为原厂自动、停用）" }).click()
  const row = page.getByRole("listitem", { name: "温控样本甲 的温控策略" })
  await expect(row.getByText("已停用")).toBeVisible()
  await row.getByLabel("模式").selectOption("curve")
  await row.getByLabel("启用").check()
  await row.getByRole("button", { name: "保存策略" }).click()
  await expect(page.getByRole("listitem", { name: "温控样本甲 的温控策略" }).getByText("已启用")).toBeVisible()
  // 伪造设备没有 CPU 温度：策略引擎只记录判断，不下发
  await expect(page.getByRole("listitem", { name: "温控样本甲 的温控策略" })).toContainText("没有可用的 CPU 温度", { timeout: 30000 })
})

test("情景方案：按风扇设定转速，策略选用同一方案后不再重复下发", async ({ page }) => {
  await login(page)
  await page.goto("/#/operations")
  await page.getByRole("button", { name: "风扇与温控", exact: true }).click()
  await page.getByLabel("搜索资产").fill("温控样本甲")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await page.getByLabel("温控样本甲", { exact: true }).check()
  await page.getByRole("button", { name: "按风扇设定转速", exact: true }).click()
  const dialog = page.getByRole("dialog")
  // 自定义：填错时不能提交
  await dialog.getByLabel("每个风扇的转速 %").fill("30, abc")
  await dialog.getByLabel("请输入“温控样本甲”确认").fill("温控样本甲")
  await expect(dialog.getByRole("button", { name: "确认按风扇设定转速" })).toBeDisabled()
  await dialog.getByLabel("情景方案").selectOption({ label: "满载散热（80 / 80 / 80 / 80 / 80 / 80%）" })
  await dialog.getByRole("button", { name: "确认按风扇设定转速" }).click()
  await expect(page.locator("section").filter({ hasText: "本次操作结果" }).getByText("已接受，无法确认")).toBeVisible()
  await page.getByRole("listitem").filter({ hasText: "温控样本甲" }).getByRole("button", { name: "查看能力" }).click()
  await expect(page.getByText("按风扇设定（已下发） · 80 / 80 / 80 / 80 / 80 / 80%")).toBeVisible()

  await page.getByRole("button", { name: "温控策略", exact: true }).click()
  const row = page.getByRole("listitem", { name: "温控样本甲 的温控策略" })
  await row.getByLabel("模式").selectOption("preset")
  await row.getByLabel("情景方案").selectOption("turbo")
  await row.getByRole("button", { name: "保存策略" }).click()
  await expect(page.getByRole("listitem", { name: "温控样本甲 的温控策略" })).toContainText("已是情景方案“满载散热”", { timeout: 30000 })
})

test("风扇命令：管理员添加自定义命令后，原先匹配不到的设备可以下发", async ({ page }) => {
  await login(page)
  await page.goto("/#/operations")
  await page.getByRole("button", { name: "风扇命令", exact: true }).click()
  await expect(page.getByRole("listitem", { name: /^内置风扇命令/ }).first()).toBeVisible()
  await page.getByRole("button", { name: "新增自定义命令" }).click()
  const form = page.getByRole("group", { name: "新增自定义风扇命令" })
  await form.getByLabel("名称").fill("其他厂商（自测）")
  await form.getByLabel("厂商").fill("Other Vendor")
  await form.getByLabel("恢复原厂温控的命令").fill("0x00 0x02 0x00")
  await form.getByLabel("设定转速的命令", { exact: true }).fill("0x3a 0x07 0xff {pwm} 0x01")
  await form.getByRole("button", { name: "保存" }).click()
  await expect(form.getByRole("alert")).toContainText("OEM 段")
  await form.getByLabel("恢复原厂温控的命令").fill("0x3a 0x07 0xff 0x00 0x00")
  await form.getByRole("button", { name: "保存" }).click()
  const item = page.getByRole("listitem", { name: "自定义风扇命令 其他厂商（自测）" })
  await expect(item).toContainText("0x3a 0x07 0xff {pwm} 0x01")

  // 192.0.2.252 报告的厂商是 Other Vendor：之前被拒绝，现在用自定义命令下发
  await page.getByRole("button", { name: "风扇与温控", exact: true }).click()
  await page.getByLabel("搜索资产").fill("温控样本乙")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await page.getByLabel("温控样本乙", { exact: true }).check()
  await page.getByRole("button", { name: "恢复原厂自动温控", exact: true }).click()
  await page.getByRole("dialog").getByRole("button", { name: "确认恢复原厂自动温控" }).click()
  const result = page.locator("section").filter({ hasText: "本次操作结果" })
  await expect(result.getByText("已接受，无法确认")).toBeVisible()
  await expect(result).toContainText("管理员自定义")

  await page.getByRole("button", { name: "风扇命令", exact: true }).click()
  page.once("dialog", (d) => void d.accept())
  await page.getByRole("listitem", { name: "自定义风扇命令 其他厂商（自测）" }).getByRole("button", { name: "删除" }).click()
  await expect(page.getByText("还没有自定义命令。")).toBeVisible()
})

// 必须在“控制归属”用例之前：那个用例会切回旧控制器，之后所有控制都会被拒绝
test("资源全景电源快捷入口复用 P5 门禁：核对对象、承载影响、确认后才提交", async ({ page }) => {
  const submits: string[] = []
  page.on("request", (r) => { if (r.method() === "POST" && r.url().includes("/api/control/power")) submits.push(r.url()) })
  await login(page)
  const host = await bmcAsset(page, "快捷电源宿主", "192.0.2.252")
  const vm = await post(page, "/api/assets", { name: "快捷电源虚拟机", form: "vm", role: "server", lifecycle: "active" })
  await post(page, "/api/relations", { source_asset_id: host.id, target_asset_id: vm.id, kind: "hosts" })
  await page.goto("/#/overview?view=resources")
  await page.getByLabel("搜索名称、编号、平台或地址", { exact: true }).fill("快捷电源")
  await page.getByRole("button", { name: "应用筛选", exact: true }).click()
  const vmCard = page.getByRole("article", { name: "资源 快捷电源虚拟机", exact: true })
  // 4.2 P17：虚拟机也有电源入口，走虚拟化平台；浏览器测试环境没有开启 YUNSHU_GUEST_POWER，对话框说明默认关闭且不能操作
  await expect(vmCard).toContainText("经发现它的 PVE / vSphere 平台控制电源")
  await vmCard.getByRole("button", { name: "电源操作 快捷电源虚拟机", exact: true }).click()
  const vmDialog = page.getByRole("dialog", { name: "电源操作：快捷电源虚拟机" })
  await expect(vmDialog).toContainText("虚拟机电源控制默认关闭")
  await expect(vmDialog.getByRole("button", { name: "开机", exact: true })).toBeDisabled()
  await page.keyboard.press("Escape")

  await page.getByRole("button", { name: "电源操作 快捷电源宿主", exact: true }).click()
  const dialog = page.getByRole("dialog", { name: "电源操作：快捷电源宿主" })
  const facts = dialog.getByRole("definition")
  await expect(facts.filter({ hasText: host.id })).toHaveCount(1)
  await expect(facts.filter({ hasText: "192.0.2.252" })).toHaveCount(1)
  await expect(dialog).toContainText("该资源已确认承载 1 项资源")
  await dialog.getByRole("button", { name: /更多操作/ }).click()
  await dialog.getByRole("button", { name: "强制断电", exact: true }).click()
  const risky = page.getByRole("dialog", { name: "强制断电：1 台" })
  await expect(risky.getByRole("button", { name: "确认强制断电" })).toBeDisabled()
  await risky.getByRole("button", { name: "取消", exact: true }).click()
  expect(submits).toEqual([])  // 打开、展开与取消都不提交

  await dialog.getByRole("button", { name: "开机", exact: true }).click()
  await page.getByRole("dialog", { name: "开机：1 台" }).getByRole("button", { name: "确认开机" }).click()
  await expect(dialog.getByLabel("本次操作结果").getByText("已查询确认")).toBeVisible()
  expect(submits).toHaveLength(1)
})

test("控制归属：部署开关关闭时不能切到新平台；切回旧控制器后所有控制被拒绝", async ({ page }) => {
  await login(page)
  await page.goto("/#/operations")
  await page.getByRole("button", { name: "控制归属", exact: true }).click()
  await expect(page.getByText("新平台负责控制")).toBeVisible()
  await expect(page.getByText("控制切换开关未关闭（YUNSHU_CONTROL_CUTOVER 未设为 0）")).toBeVisible()
  const rollback = page.locator("section").filter({ hasText: "切回旧控制器（回滚）" })
  await expect(rollback.getByRole("button", { name: "切回旧控制器" })).toBeDisabled()
  await rollback.getByLabel("请输入“切回旧控制器”确认").fill("切回旧控制器")
  await rollback.getByRole("button", { name: "切回旧控制器" }).click()
  await expect(page.getByText("控制仍由旧控制器负责").first()).toBeVisible()
  // 开关关闭：不能再切到新平台
  await expect(page.getByText("这套部署关闭了控制切换开关")).toBeVisible()
  await expect(page.locator("section").filter({ hasText: "逐项确认" }).getByRole("button", { name: "切换到新平台" })).toBeDisabled()
  // 旧控制器负责时操作按钮不可用；375 宽时卡片不超出页面
  await page.setViewportSize({ width: 375, height: 812 })
  await page.getByRole("button", { name: "电源控制", exact: true }).click()
  await expect(page.getByText("控制仍由旧控制器负责，操作按钮暂不可用。")).toBeVisible()
  await expect(page.getByRole("button", { name: "强制断电", exact: true })).toBeDisabled()
  for (const box of await page.locator("main section").evaluateAll((els) => els.map((e) => e.getBoundingClientRect().right)))
    expect(box).toBeLessThanOrEqual(375)
})
