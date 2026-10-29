"""外观主题接口（P23）。

- 所有登录账号：读外观设置、改自己的选择（只读账号也可以，跟账号走）。
- 管理员：改平台默认、导入 / 删除主题包、上传 / 删除玻璃壁纸；都写审计。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import appearance, audit
from app.api.deps import admin_only, current_user, get_db
from app.models import UiWallpaper, User

router = APIRouter(prefix="/api/appearance", tags=["appearance"])


class Choice(BaseModel):
    style: str | None = None
    mode: str | None = None


class DefaultIn(BaseModel):
    style: str
    mode: str


def _who(user: User) -> str:
    return user.display_name or user.username


def _bad(exc: appearance.ThemeError) -> HTTPException:
    return HTTPException(422, str(exc))


def _state(db: Session, user: User) -> dict:
    return {
        "default": appearance.default(db),
        "mine": appearance.mine(user),
        "themes": appearance.themes(db),
        "wallpaper": appearance.wallpaper_info(db),
    }


@router.get("")
def get_appearance(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return _state(db, user)


@router.put("/mine")
def update_mine(body: Choice, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    try:
        appearance.set_mine(db, user, body.style, body.mode)
    except appearance.ThemeError as exc:
        raise _bad(exc) from exc
    return _state(db, user)


@router.put("/default")
def update_default(body: DefaultIn, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    before = appearance.default(db)
    try:
        appearance.set_default(db, body.style, body.mode, _who(actor))
    except appearance.ThemeError as exc:
        raise _bad(exc) from exc
    after = {"style": body.style, "mode": body.mode}
    audit.record(db, actor, "appearance.default", target_type="platform_setting", target_id=appearance.DEFAULT_KEY,
                 summary=f"平台默认外观 {before['style']}/{before['mode']} → {body.style}/{body.mode}",
                 detail={"before": before, "after": after})
    return _state(db, actor)


@router.post("/themes", status_code=status.HTTP_201_CREATED)
async def import_theme(file: UploadFile = File(...), actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    raw = await file.read(appearance.MAX_PACKAGE_BYTES + 1)
    if len(raw) > appearance.MAX_PACKAGE_BYTES:
        raise HTTPException(413, "主题包太大（最多 64 KB）")
    try:
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise appearance.ThemeError("主题包不是有效的 UTF-8 JSON 文件") from exc
        package = appearance.parse_package(data)
        created = appearance.save_theme(db, package, _who(actor))
    except appearance.ThemeError as exc:
        raise _bad(exc) from exc
    audit.record(db, actor, "appearance.theme_import", target_type="ui_theme", target_id=package["id"],
                 summary=f"{'导入' if created else '更新'}主题 {package['name']}（{package['id']}，基于{'玻璃' if package['base'] == 'glass' else '简洁'}）")
    return {"theme": package, "created": created}


@router.delete("/themes/{theme_id}")
def remove_theme(theme_id: str, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    try:
        affected = appearance.delete_theme(db, theme_id, _who(actor))
    except appearance.ThemeError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    audit.record(db, actor, "appearance.theme_delete", target_type="ui_theme", target_id=theme_id,
                 summary=f"删除主题 {theme_id}" + (f"；{affected} 个账号改回平台默认风格" if affected else ""))
    return {"affected_users": affected}


@router.get("/wallpaper")
def get_wallpaper(_: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    row = db.get(UiWallpaper, appearance.WALLPAPER_KEY)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有自定义壁纸")
    # 地址里带内容哈希，可以长期缓存；nosniff 防止浏览器把它当别的类型解析
    return Response(row.content, media_type=row.media_type, headers={
        "Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})


@router.post("/wallpaper")
async def upload_wallpaper(file: UploadFile = File(...), actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict:
    content = await file.read(appearance.MAX_WALLPAPER_BYTES + 1)
    try:
        row = appearance.save_wallpaper(db, content, _who(actor))
    except appearance.ThemeError as exc:
        raise _bad(exc) from exc
    audit.record(db, actor, "appearance.wallpaper", target_type="ui_wallpaper", target_id=row.key,
                 summary=f"更换玻璃主题壁纸（{row.media_type}，{len(content) // 1024} KB）")
    db.flush()
    return {"wallpaper": appearance.wallpaper_info(db)}


@router.delete("/wallpaper", status_code=status.HTTP_204_NO_CONTENT)
def remove_wallpaper(actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> None:
    row = db.get(UiWallpaper, appearance.WALLPAPER_KEY)
    if row is None:
        return
    db.delete(row)
    audit.record(db, actor, "appearance.wallpaper", target_type="ui_wallpaper", target_id=appearance.WALLPAPER_KEY,
                 summary="删除玻璃主题壁纸，改回内置渐变")
