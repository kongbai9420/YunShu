/**
 * 内置主题登记表（P23）。新增一个主题：
 *   1. 在本目录放一个主题包 JSON（格式与管理员在界面导入的主题包相同，见 README.md 与 example-theme.json）；
 *   2. 在下面 import 并加进 PACKAGES。
 * 主题包只改变量值；需要玻璃效果时把 base 设为 "glass"。simple / glass 两个基础风格的变量写在 CSS 里
 * （src/index.css、glass.css），不在这里。
 */

export type Base = "simple" | "glass"
export type Tokens = Record<string, string>

export interface ThemePackage {
  id: string
  name: string
  base: Base
  light: Tokens
  dark: Tokens
  author?: string
  description?: string
  theme_version?: string
}

export interface ThemeInfo extends ThemePackage {
  /** builtin：随版本内置；imported：管理员导入 */
  source: "builtin" | "imported"
}

/** 两个基础风格：变量在 CSS 里，这里只登记名称和说明 */
export const BASE_THEMES: ThemeInfo[] = [
  {
    id: "simple",
    name: "简洁",
    base: "simple",
    light: {},
    dark: {},
    description: "不透明的浅灰 / 深灰界面，信息密度高，适合长时间值守",
    source: "builtin",
  },
  {
    id: "glass",
    name: "玻璃",
    base: "glass",
    light: {},
    dark: {},
    description: "苹果风格：半透明毛玻璃卡片、悬浮的顶栏与侧栏、彩色渐变壁纸",
    source: "builtin",
  },
]

/** 随版本内置的主题包（开发者按 README 添加）。目前没有，留作扩展点 */
const PACKAGES: ThemePackage[] = []

export const BUILTIN_THEMES: ThemeInfo[] = [
  ...BASE_THEMES,
  ...PACKAGES.map((p) => ({ ...p, source: "builtin" as const })),
]
