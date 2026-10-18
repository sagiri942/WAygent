"""用户注册 / 登录接口。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from app.core.auth import create_token, hash_password, verify_password
from app.core.identity import SQLiteAccounts, DuplicateUsername

router = APIRouter(prefix="/api/auth", tags=["auth"])
_accounts = SQLiteAccounts()


def configure_auth_backend(database) -> None:
    global _accounts
    if database.engine.dialect.name == "mysql":
        from app.core.identity_storage import CoreAccounts
        _accounts = CoreAccounts(database)
    else:
        raise RuntimeError("Production authentication requires MySQL")


class AuthRequest(BaseModel):
    username: str
    password: str


def get_accounts():
    return _accounts


@router.post("/register")
def register(req: AuthRequest, accounts=Depends(get_accounts)):
    if not req.username.strip() or not req.password:
        raise HTTPException(400, "用户名和密码不能为空")
    user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        accounts.create(user_id, req.username.strip(), hash_password(req.password), now)
    except DuplicateUsername as exc:
        raise HTTPException(409, "\u7528\u6237\u540d\u5df2\u5b58\u5728") from exc
    except Exception as exc:
        raise HTTPException(500, "\u6ce8\u518c\u5931\u8d25") from exc
    return {"user_id": user_id, "token": create_token(user_id), "username": req.username.strip()}


@router.post("/login")
def login(req: AuthRequest, accounts=Depends(get_accounts)):
    row = accounts.by_username(req.username.strip())
    if not row or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    return {"user_id": row["id"], "token": create_token(row["id"]), "username": req.username.strip()}
