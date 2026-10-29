# frontend（4.0 前端）

React + TypeScript + Vite + Tailwind CSS v4 + shadcn/ui（new-york 风格，neutral 基色）。

- **已接真实接口（P1）**：登录、首次登录强制改密、资产管理（`#/assets`：列表、筛选、四类入口、详情、管理端点、关联、编辑、新增、删除）。
  接口客户端在 `src/lib/api.ts`（会话 Cookie + `X-CSRF-Token`），登录状态在 `src/lib/auth.tsx`。
  修改资产带修订号，别人先改过时提示冲突并可载入最新内容，不静默覆盖。只读角色不显示编辑按钮（后端同样拒绝）。
  新增资产默认 SSH，可同时填写协议、设备地址、端口及设备用户名 / 密码 / 多行私钥，也可选择已有凭据、暂不关联或仅建档；更多资产信息可展开填写。资产、端点和新凭据一次保存，冲突会整体回滚，保留表单供修改后重试，关闭后清除秘密字段。
  填好连接后可在创建前“获取设备信息”，不必先保存；凭据别名可留空自动命名。编辑页可按已有端点重新读取名称 / 平台 / 品牌 / 型号 / 序列号，对比当前值和采集值，单选、多选、全选或只选空字段；替换只写入表单，点击保存才生效。未采到的项不清空旧值，定时指标采集不自动改写资产资料。
- 批量导入（`#/import`）：下载模板、上传预览、确认提交、整批撤销、导入记录与逐行结果导出。
- 平台设置（`#/settings`）：凭据（只能整体替换，看不到原文）、场所、用户与角色（管理员）、备份（管理员，提示备份不含主密钥）、我的账户。
- **监控中心（P2，`#/monitoring`）**：资产最新指标、六种质量、按指标查询原始/分钟/小时历史、端点采集状态、降级与冷却、任务队列、9 个 A/B 批模板、IPv4 TCP / SNMP 网段发现。
  资产详情“指标与采集”复用同一组件，支持立即采集与确认后重新信任指纹；编辑资产可选择模板，端点可保存或更换凭据关联。
  未采到、失败或过期显示空值和状态，上次有效值单独标明；页面断连时缓存数据也会随时间标为过期。
  网段发现先预览授权范围，再显式开始，结果逐项确认；同 IP 仅提示候选，不自动合并。所有模板均为“未实机验证”。
- **拓扑（P3，`#/topology`）**：接真实接口（`src/lib/topology.ts`），@xyflow/react 12.12.0 + elkjs 0.12.0。家用 / 机房视图切换；
  候选（细虚线）、已确认（实线）、人工（粗实线带箭头）、资产关系（点线）用线型 + 文字 + 图例区分，不只靠颜色；连线多于 12 条时标签只在悬停或选中时显示。
  重新扫描先显示差异预览（新增 / 更新 / 移除，以及不受影响的已确认、人工、已忽略数量和未匹配的邻居），确认后才合并；候选可确认或忽略，人工连线可拖动或用表单新增、拖端点改接、删除。
  锁定位置、移出视图（不删资产，可放回）、自动布局都进撤销历史（Ctrl/⌘+Z、Shift+Ctrl/⌘+Z），撤销 / 重做后在画布上标出受影响的节点；
  连线改动立即保存，不进撤销历史。保存布局带修订号，别人先保存时提示冲突，可“载入最新”或“用我的布局覆盖”。只读角色只能查看。
  ELK 在 Web Worker 里计算（`elkjs/lib/elk-worker.min.js` 以 `?url` 原样复制）。
- **实时推送（P3）**：`src/lib/live.ts` 的 `useLiveEvents(handlers, { fallbackMs })` 订阅 `GET /api/events`。
  一个标签页只开一条 EventSource，所有组件共用；页面在后台时断开，回到前台按上次事件 id 续传；连不上时按 2–30 秒退避重连，
  未连上期间每 `fallbackMs` 调用 `handlers.onRefetch` 定时重新读取接口。状态用 `src/components/LiveIndicator.tsx` 显示
  “实时” / “已断开，正在重连”（图标 + 文字）。推送只更新显示，不写进正在编辑的表单。
  资产列表的“采集”列、监控中心与资产详情的指标和端点状态、采集任务列表已接入；仪表盘和拓扑可直接复用这个钩子。
- 资产表格：列头可点击排序（`aria-sort`，键盘可操作，后端分页排序），也可在“排序”下拉中按资产编号、最近更新排序；
  列头右侧分隔线可拖动或用方向键调整列宽，按浏览器记住（存储不可用时用默认列宽），“恢复默认列宽”一键还原。
- 总览仪表盘（`#/overview`，react-grid-layout 2.3.0，P3）：布局存后端带修订号，冲突时让用户选择；小组件每 30 秒从 `/api/dashboard/summary` 取真实数据；未保存的编辑暂存在 sessionStorage 可恢复。
- **告警中心（P4，`#/alerts`）**：当前告警、历史事件分页与当时快照、规则、通知渠道与投递、维护窗口。支持确认 / 静默、修订冲突、资产 / 标签 / 严重程度路由；资产详情有“告警”页签，总览同时显示活跃告警与采集问题。
  SMTP 与七种 webhook 的秘密加密且不回显，渠道默认停用。测试按钮向已保存的目标排队；“服务已接受”仅表示 SMTP / 推送服务端接受，不代表收件人收到。结果不确定时须确认可能重复才手动重试。
  配置从后续事件生效，持续告警可在规则中设置重复提醒；默认聚合 30 秒。历史、处置和投递每 5 秒刷新，编辑中的输入不被刷新覆盖。

## 本地运行

需要 Node 20 LTS（>=20.19，见 `.nvmrc`）。

```bash
cd frontend
npm ci
npm run dev        # 开发服务器
npm run typecheck  # tsc -b --noEmit
npm run build      # 类型检查 + 生产构建，输出 dist/
npm run test:e2e   # Playwright：真实 HTTP/数据库/Worker，设备传输使用伪造响应
```

前后端一起预览（含 /api 代理、PR 预览）：双击 `scripts\preview.bat`。
预览使用独立的数据目录和主密钥（`backend/.preview/`，不进 git），首次登录 admin / admin123 并按提示改密码。

依赖全部精确版本（`.npmrc` 设 `save-exact=true`），以 `package-lock.json` 为准。
添加 shadcn 组件：`npx shadcn@4.21.5 add <组件>`，生成后确认 `cn` 从 `@/lib/utils` 导入。

## 浏览器测试

- `npm run test:e2e` 自动启动独立的临时库后端（127.0.0.1:8015，内置伪造设备与通知渠道）和前端开发服务器（127.0.0.1:5185），不读取任何本机配置、不连接真实设备。
- 覆盖首次改密、采集质量与历史、发现与关联、拓扑、告警与维护窗口、运维操作（伪造 BMC）、三种角色的权限、外观主题、窄屏与深色、200% 字号等。
- Windows 上优先使用已安装的 Chrome；其他环境先 `npx playwright install chromium`。CI 也运行这些测试。
- README 截图：`README_SCREENS=1 npx playwright test zz-readme-screens`，只用伪造数据；输出目录用 `README_SCREENS_DIR` 指定，默认 `test-results/screenshots/readme/`。
- 截图与失败追踪在 `test-results/`，不提交。

## 直接依赖许可证

| 包 | 版本 | 许可证 |
| --- | --- | --- |
| react / react-dom | 19.3.1 | MIT |
| radix-ui | 1.8.1 | MIT |
| class-variance-authority | 0.7.1 | Apache-2.0 |
| clsx | 2.1.1 | MIT |
| tailwind-merge | 3.7.0 | MIT |
| lucide-react | 1.57.0 | ISC |
| vite | 8.3.4 | MIT |
| @vitejs/plugin-react | 6.1.2 | MIT |
| tailwindcss / @tailwindcss/vite | 4.3.3 | MIT |
| tw-animate-css | 1.4.0 | MIT |
| typescript | 6.0.3 | Apache-2.0 |
| @playwright/test（仅开发测试） | 1.63.0 | Apache-2.0 |
| @types/node / @types/react / @types/react-dom | 20.19.43 / 19.3.0 / 19.3.0 | MIT |
| react-grid-layout | 2.3.0 | MIT |
| @xyflow/react | 12.12.0 | MIT |
| elkjs | 0.12.0 | EPL-2.0 OR GPL-3.0-or-later（按 EPL-2.0 使用） |
| @dnd-kit/core / @dnd-kit/sortable / @dnd-kit/utilities | 6.3.1 / 10.0.0 / 3.2.2 | MIT |

锁文件内传递依赖为 MIT / Apache-2.0 / ISC / BSD-3-Clause / 0BSD；例外是构建期的 lightningcss
（MPL-2.0，弱 copyleft，由 Tailwind/Vite 引入，只在构建时运行、不修改源码、不进入产物）。没有 GPL/AGPL 依赖。

P0 原型新增的传递依赖：react-draggable、react-resizable、prop-types、fast-equals、resize-observer-polyfill、
@xyflow/system、zustand、classcat、use-sync-external-store、@dnd-kit/accessibility 等为 MIT；d3-color、d3-dispatch、
d3-drag、d3-interpolate、d3-selection、d3-timer、d3-transition、d3-zoom 为 ISC；d3-ease 为 BSD-3-Clause。
elkjs 是双许可（EPL-2.0 或 GPL-3.0-or-later），我们选 EPL-2.0：不修改其源码、原样打包，EPL-2.0 的弱 copyleft
只约束对 elkjs 本身的修改，不影响本项目代码；分发时需附带 elkjs 的许可证与源码获取方式。
