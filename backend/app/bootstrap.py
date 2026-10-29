"""首次启动：库里没有任何用户时创建初始管理员。

沿用旧版做法：用户名 admin、初始密码 admin123，
首次登录后必须先修改密码才能进行任何操作。导入旧版 users.json 时，
未改过密码的初始管理员会被旧账号替换。
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.auth import hash_password
from app.models import User

log = logging.getLogger(__name__)
INITIAL_USERNAME = "admin"
INITIAL_PASSWORD = "admin123"


def ensure_initial_admin(db: Session) -> User | None:
    if db.scalar(select(func.count()).select_from(User)):
        return None
    user = User(
        username=INITIAL_USERNAME,
        display_name="系统管理员",
        role="admin",
        password_hash=hash_password(INITIAL_PASSWORD),
        require_password_change=True,
    )
    db.add(user)
    db.flush()
    audit.record(db, None, "user.bootstrap", target_type="user", target_id=user.id, summary="首次启动创建初始管理员")
    db.commit()
    log.warning("已创建初始管理员 admin（初始密码与旧版相同），首次登录后必须修改密码")
    return user
