"""外观主题（P23）：风格 × 深浅、平台默认与账号选择、导入的主题包、玻璃壁纸。

风格（style）：内置 simple（简洁）、glass（玻璃），或管理员导入的主题包 id。
深浅（mode）：system（跟随系统）/ light / dark，与风格分开选。
账号的选择存在 users.appearance，缺的键用平台默认（platform_settings 的 appearance.default）。

主题包只能改界面变量的值（见 TOKENS），不能带样式规则、脚本或外部地址：
- 变量名必须在 TOKENS 里；
- 值只允许颜色、长度、阴影、渐变这类字符，函数只认 FUNCTIONS 里的，不能有 url()、引号、分号、花括号。
前端用 style.setProperty 写到 <html> 上，所以即使校验漏了也不能注入新的样式规则；校验是第一道关。
变量契约（含义、默认值）写在 frontend/src/themes/README.md，两边要一起改。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PlatformSetting, UiTheme, UiWallpaper, User

BUILTIN_STYLES = ("simple", "glass")
MODES = ("system", "light", "dark")
DEFAULT = {"style": "simple", "mode": "system"}
DEFAULT_KEY = "appearance.default"

# 主题可以改的变量（不带前导 --）。与 frontend/src/index.css、src/themes/*.css 里的变量一致
TOKENS = frozenset({
    "background", "foreground", "card", "card-foreground", "popover", "popover-foreground",
    "primary", "primary-foreground", "primary-hover", "primary-active",
    "destructive", "destructive-solid", "destructive-solid-hover",
    "secondary", "secondary-foreground", "secondary-hover", "muted", "muted-foreground",
    "accent", "accent-foreground", "accent-strong", "disabled", "disabled-foreground",
    "border", "input", "ring", "sidebar", "titlebar", "link",
    "status-ok", "status-warn", "status-bad", "status-info", "status-manual", "status-neutral",
    "status-ok-bg", "status-warn-bg", "status-bad-bg", "status-info-bg", "status-manual-bg", "status-neutral-bg",
    "card-shadow", "card-shadow-hover", "modal-shadow", "radius",
    "app-backdrop", "glass-blur", "glass-saturate", "glass-edge", "glass-highlight",
})
FUNCTIONS = frozenset({
    "rgb", "rgba", "hsl", "hsla", "oklch", "oklab", "lab", "lch", "color-mix", "calc",
    "linear-gradient", "radial-gradient", "conic-gradient", "repeating-linear-gradient", "repeating-radial-gradient",
})
_VALUE = re.compile(r"^[A-Za-z0-9#%().,\-\s/+*]*$")
_FUNC = re.compile(r"([A-Za-z-]+)\s*\(")
_ID = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
MAX_VALUE = 600
MAX_THEMES = 20
MAX_PACKAGE_BYTES = 64 * 1024

WALLPAPER_KEY = "glass"
MAX_WALLPAPER_BYTES = 4 * 1024 * 1024
# 按文件头判断类型，不信任文件名和浏览器给的类型
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))


class ThemeError(ValueError):
    """主题包或外观设置不合法；消息直接给用户看。"""


def check_value(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ThemeError(f"变量 {name} 的值必须是文字")
    v = value.strip()
    if not v:
        raise ThemeError(f"变量 {name} 的值为空")
    if len(v) > MAX_VALUE:
        raise ThemeError(f"变量 {name} 的值太长（最多 {MAX_VALUE} 个字符）")
    if not _VALUE.match(v):
        raise ThemeError(f"变量 {name} 的值含有不允许的字符（不能有引号、分号、花括号、反斜杠等）")
    for fn in _FUNC.findall(v):
        if fn.lower() not in FUNCTIONS:
            raise ThemeError(f"变量 {name} 用了不允许的函数 {fn}()；只允许颜色、渐变和 calc")
    return v


def check_tokens(tokens: object, where: str) -> dict[str, str]:
    if tokens is None:
        return {}
    if not isinstance(tokens, dict):
        raise ThemeError(f"{where} 必须是“变量名: 值”的对象")
    out: dict[str, str] = {}
    for raw, value in tokens.items():
        name = str(raw).removeprefix("--")
        if name not in TOKENS:
            raise ThemeError(f"{where} 里的变量 {raw} 不在可用变量列表里")
        out[name] = check_value(name, value)
    return out


def parse_package(data: object) -> dict[str, Any]:
    """校验主题包并返回规范化后的内容（变量名去掉前导 --）。"""
    if not isinstance(data, dict):
        raise ThemeError("主题包必须是 JSON 对象")
    if data.get("format") != "yunshu-theme" or data.get("version") != 1:
        raise ThemeError("不是云枢主题包：需要 \"format\": \"yunshu-theme\" 和 \"version\": 1")
    theme_id = data.get("id")
    if not isinstance(theme_id, str) or not _ID.match(theme_id):
        raise ThemeError("id 只能用小写字母、数字和连字符，以字母开头，2–32 个字符")
    if theme_id in BUILTIN_STYLES:
        raise ThemeError(f"id 不能用内置主题的名字 {theme_id}")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
        raise ThemeError("name（显示名称）必填，最多 40 个字符")
    base = data.get("base", "simple")
    if base not in BUILTIN_STYLES:
        raise ThemeError("base 只能是 simple（简洁）或 glass（玻璃）")
    light = check_tokens(data.get("light"), "light")
    dark = check_tokens(data.get("dark"), "dark")
    if not light and not dark:
        raise ThemeError("light 和 dark 至少要有一个变量")
    meta = {k: str(data.get(k) or "").strip()[:200] for k in ("author", "description", "theme_version")}
    return {"format": "yunshu-theme", "version": 1, "id": theme_id, "name": name.strip(), "base": base,
            "light": light, "dark": dark, **meta}


def default(db: Session) -> dict[str, str]:
    row = db.get(PlatformSetting, DEFAULT_KEY)
    value = row.value if row and isinstance(row.value, dict) else {}
    return {"style": value.get("style") or DEFAULT["style"], "mode": value.get("mode") or DEFAULT["mode"]}


def styles(db: Session) -> set[str]:
    return set(BUILTIN_STYLES) | set(db.scalars(select(UiTheme.id)))


def check_choice(db: Session, style: str | None, mode: str | None) -> None:
    if style is not None and style not in styles(db):
        raise ThemeError(f"没有主题 {style}")
    if mode is not None and mode not in MODES:
        raise ThemeError("深浅只能是 system、light 或 dark")


def set_default(db: Session, style: str, mode: str, by: str) -> None:
    check_choice(db, style, mode)
    row = db.get(PlatformSetting, DEFAULT_KEY)
    if row is None:
        db.add(PlatformSetting(key=DEFAULT_KEY, value={"style": style, "mode": mode}, updated_by=by))
    else:
        row.value = {"style": style, "mode": mode}
        row.updated_by = by


def mine(user: User) -> dict[str, str | None]:
    value = user.appearance if isinstance(user.appearance, dict) else {}
    return {"style": value.get("style"), "mode": value.get("mode")}


def set_mine(db: Session, user: User, style: str | None, mode: str | None) -> None:
    check_choice(db, style, mode)
    user.appearance = {k: v for k, v in (("style", style), ("mode", mode)) if v is not None} or None


def themes(db: Session) -> list[dict[str, Any]]:
    return [dict(t.package, updated_by=t.updated_by) for t in db.scalars(select(UiTheme).order_by(UiTheme.name))]


def save_theme(db: Session, package: dict[str, Any], by: str) -> bool:
    """保存（同 id 覆盖）主题包，返回是否新建。"""
    row = db.get(UiTheme, package["id"])
    if row is None:
        if len(styles(db)) - len(BUILTIN_STYLES) >= MAX_THEMES:
            raise ThemeError(f"最多导入 {MAX_THEMES} 个主题，请先删除不用的")
        db.add(UiTheme(id=package["id"], name=package["name"], base=package["base"], package=package, updated_by=by))
        return True
    row.name, row.base, row.package, row.updated_by = package["name"], package["base"], package, by
    return False


def delete_theme(db: Session, theme_id: str, by: str) -> int:
    """删除主题包；平台默认用它时退回简洁，账号选了它的清掉风格选择。返回受影响的账号数。"""
    row = db.get(UiTheme, theme_id)
    if row is None:
        raise ThemeError(f"没有主题 {theme_id}")
    db.delete(row)
    db.flush()
    current = default(db)
    if current["style"] == theme_id:
        set_default(db, DEFAULT["style"], current["mode"], by)
    affected = 0
    for user in db.scalars(select(User).where(User.appearance.is_not(None))):
        choice = mine(user)
        if choice["style"] == theme_id:
            user.appearance = {"mode": choice["mode"]} if choice["mode"] else None
            affected += 1
    return affected


def sniff(content: bytes) -> str:
    for magic, media_type in _MAGIC:
        if content.startswith(magic):
            return media_type
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    raise ThemeError("壁纸只支持 PNG、JPEG、WebP 图片")


def save_wallpaper(db: Session, content: bytes, by: str) -> UiWallpaper:
    if not content:
        raise ThemeError("图片是空的")
    if len(content) > MAX_WALLPAPER_BYTES:
        raise ThemeError(f"图片太大（最多 {MAX_WALLPAPER_BYTES // 1024 // 1024} MB）")
    media_type = sniff(content)
    sha = hashlib.sha256(content).hexdigest()
    row = db.get(UiWallpaper, WALLPAPER_KEY)
    if row is None:
        row = UiWallpaper(key=WALLPAPER_KEY, media_type=media_type, content=content, sha256=sha, updated_by=by)
        db.add(row)
    else:
        row.media_type, row.content, row.sha256, row.updated_by = media_type, content, sha, by
    return row


def wallpaper_info(db: Session) -> dict[str, str] | None:
    row = db.execute(select(UiWallpaper.sha256).where(UiWallpaper.key == WALLPAPER_KEY)).first()
    # 地址带内容哈希：换图后浏览器不会继续用缓存里的旧图
    return {"url": f"/api/appearance/wallpaper?v={row[0][:16]}"} if row else None
