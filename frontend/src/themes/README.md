# 云枢主题

界面的颜色、圆角、阴影、背景都来自一组 CSS 变量。主题就是给这些变量换一套值。

外观分两个维度，分开选：
- **风格**：`simple`（简洁）、`glass`（玻璃），或者一个主题包；
- **深浅**：跟随系统、浅色、深色。

每个主题都要同时提供浅色和深色两套值。

## 两种方式提供主题

| 方式 | 适合 | 怎么做 |
| --- | --- | --- |
| 管理员导入主题包 | 不改代码、不重新发版 | 平台设置 → 外观 → 导入主题包，上传一个 JSON 文件 |
| 随版本内置 | 想让所有部署都自带 | 把同一个 JSON 放进本目录，在 `index.ts` 的 `PACKAGES` 里登记 |

两种方式用的是同一种文件格式。后端在 `backend/app/appearance.py` 校验。

## 主题包格式

完整示例见同目录的 [example-theme.json](example-theme.json)。

```json
{
  "format": "yunshu-theme",
  "version": 1,
  "id": "ocean",
  "name": "海洋",
  "base": "glass",
  "author": "example.invalid",
  "description": "一句话说明",
  "theme_version": "1.0.0",
  "light": { "primary": "#00668a" },
  "dark": { "primary": "#00668a" }
}
```

字段说明：
- **id**：小写字母开头，只能有小写字母、数字和 `-`，2–32 个字符。不能是 `simple` 或 `glass`。同一 id 再导入会覆盖原来的。
- **name**：显示名称，最多 40 个字符。
- **base**：基于哪个风格，二选一：
  - `simple`：不透明表面；
  - `glass`：毛玻璃表面、悬浮的顶栏与侧栏、渐变壁纸。

  主题包没写的变量，沿用基础风格的值。
- **light / dark**：要改的变量。变量名写不写前导 `--` 都可以。两个都不写会被拒绝。

## 值的规则

主题包只能改变量的值，不能写样式规则。

允许：
- 颜色：`#rrggbb`、`rgb()`、`rgba()`、`hsl()`、`oklch()`、`color-mix()`；
- 长度：`1rem`、`24px`；
- 阴影；
- 渐变：`linear-gradient()`、`radial-gradient()`、`conic-gradient()`；
- `calc()`。

不允许：
- 引号、分号、花括号、反斜杠；
- `url()`、`var()`、`image-set()` 这类函数。

每个值最多 600 个字符，整个文件最大 64 KB，最多导入 20 个主题。

前端用 `style.setProperty` 把值写到 `<html>` 上。所以即使后端校验漏了，也不能注入新的样式规则。

## 变量清单

默认值见 `src/index.css`（简洁）和 `glass.css`（玻璃）。这份清单与后端的 `TOKENS` 必须一致，改的时候两边一起改。

**页面与表面**

| 变量 | 用途 |
| --- | --- |
| `background` / `foreground` | 页面底色 / 正文颜色 |
| `card` / `card-foreground` | 卡片、表格外框、输入框底色 / 文字 |
| `popover` / `popover-foreground` | 弹窗、下拉、菜单 |
| `sidebar` / `titlebar` | 侧栏 / 顶栏（带背景模糊，可以半透明） |
| `border` / `input` | 分隔线 / 输入框边框 |
| `radius` | 卡片圆角；按钮、输入框按它减 2–4px |
| `card-shadow` / `card-shadow-hover` / `modal-shadow` | 卡片、悬停卡片、弹窗的阴影 |

**按钮与强调**

| 变量 | 用途 |
| --- | --- |
| `primary` / `primary-foreground` / `primary-hover` / `primary-active` | 主按钮、侧栏当前项 |
| `secondary` / `secondary-foreground` / `secondary-hover` | 次要按钮、表头 |
| `muted` / `muted-foreground` | 弱化底色 / 次要文字 |
| `accent` / `accent-foreground` / `accent-strong` | 悬停、按下 |
| `destructive` / `destructive-solid` / `destructive-solid-hover` | 危险操作的文字 / 按钮底色 |
| `disabled` / `disabled-foreground` | 禁用控件 |
| `ring` / `link` | 键盘焦点描边 / 链接 |

**状态色**

| 变量 | 用途 |
| --- | --- |
| `status-ok` / `-warn` / `-bad` / `-info` / `-manual` / `-neutral` | 正常、警告、严重、信息、需人工、无数据的文字和图标 |
| 同名加 `-bg` | 对应的浅底色 |

**玻璃（只在 base 为 glass 时起作用）**

| 变量 | 用途 |
| --- | --- |
| `app-backdrop` | 页面背后的壁纸，一般写几层 `radial-gradient` 加一层 `linear-gradient`。管理员上传了自定义壁纸时，图片盖在它上面 |
| `glass-blur` / `glass-saturate` | 背景模糊半径（如 `24px`）/ 饱和度（如 `180%`） |
| `glass-edge` | 玻璃片的边框，一般是半透明白 |
| `glass-highlight` | 上沿高光（`card-shadow` 里的 `inset` 阴影可以用同一个颜色） |

## 做主题时请检查

- **对比度**：正文对背景至少 4.5:1。玻璃主题要按背后最亮或最暗的壁纸区域算。白字放在 `primary` 上也要达到 4.5:1。
- **深浅都要试**：深色模式下状态色要换成亮一些的版本（参考 `index.css` 的深色块）。
- **降级**：玻璃风格在系统开了“降低透明度”、或浏览器不支持背景模糊时，会把表面改成不透明（见 `glass.css` 末尾）。主题包不用另外处理。

## 开发者加 CSS 的主题（进阶）

如果一个主题需要的不只是换变量，比如改布局：
1. 在本目录加一个 CSS 文件，选择器写 `:root[data-skin="<id>"]`；
2. 在 `src/main.tsx` 里引入这个 CSS 文件；
3. 同时在 `index.ts` 登记主题包。

`<html>` 上的属性：
- `data-style`：基础风格；
- `data-skin`：主题 id；
- `data-theme`：手动选的深浅（跟随系统时没有这个属性）；
- `data-wallpaper`：有自定义壁纸。

外观相关的区域都带 `data-shell` 属性：`titlebar`（顶栏）、`sidebar`（侧栏）。

管理员导入的主题包不能带 CSS，只能用变量。
