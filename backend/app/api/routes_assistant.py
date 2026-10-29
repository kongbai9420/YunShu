"""问答助手“问云枢”（4.3 P18g）：三种角色都可以用，查询结果按账号的资产范围过滤；助手只能查询。"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import ai_runtime, assistant
from app.ai.protocols import AiError
from app.api.deps import get_db, reader
from app.models import User
from app.scope import scope_for

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatIn(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=30)


@router.get("/status")
def status(_: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    return {"enabled": ai_runtime.feature_enabled(ai_runtime.settings_row(db), assistant.FEATURE)}


@router.post("/chat")
def chat(body: ChatIn, request: Request, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict:
    scope = scope_for(db, user)
    visible = None if scope.all else set(db.scalars(scope.assets()))
    try:
        return assistant.chat(db, request.app.state.session_factory, request.app.state.cipher, user_name=user.username, visible=visible,
                              history=[m.model_dump() for m in body.messages], client=getattr(request.app.state, "ai_http_client", None))
    except AiError as exc:
        db.commit()  # 保留用量记录
        raise HTTPException(422 if exc.kind in ("disabled", "quota", "unsupported", "bad_request") else 502,
                            {"kind": exc.kind, "message": str(exc)}) from None
