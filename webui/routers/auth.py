"""Authentication endpoints: login, logout, me, change password, users."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..audit import get_audit
from ..config import AUTH_ENABLED, SESSION_TTL_SECONDS
from ..db import Database
from ..deps import current_user, get_auth, rate_limit_general, require_user
from ..security import make_csrf_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _session_cookie(token: str) -> dict:
    return {
        "key": "session",
        "value": token,
        "httponly": True,
        "samesite": "lax",
        "max_age": SESSION_TTL_SECONDS,
        "path": "/",
    }


def _csrf_cookie(token: str) -> dict:
    return {"key": "csrf", "value": token, "httponly": False, "samesite": "lax", "path": "/"}


@router.post("/login")
async def login(
    payload: dict,
    request: Request,
    response: Response,
    auth=Depends(get_auth),
    db: Database = Depends(lambda: Database.get()),
):
    if not AUTH_ENABLED:
        return {"ok": True, "auth_enabled": False, "message": "auth disabled"}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not username or not password:
        raise HTTPException(400, "username and password required")
    user = await auth.authenticate(username, password)
    if user is None:
        await db.add_audit(username, "login", "", "error", "invalid credentials")
        raise HTTPException(401, "invalid credentials")
    token = await auth.create_session(user)
    csrf = make_csrf_token()
    response.set_cookie(**_session_cookie(token))
    response.set_cookie(**_csrf_cookie(csrf))
    await db.add_audit(username, "login", "", "success", "")
    return {"ok": True, "user": {"username": user["username"], "role": user["role"]}}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    auth=Depends(get_auth),
    user: dict = Depends(require_user),
):
    token = request.cookies.get("session") or ""
    if request.headers.get("Authorization", "").lower().startswith("bearer "):
        token = request.headers["Authorization"][7:].strip()
    await auth.logout(token)
    response.delete_cookie("session", path="/")
    response.delete_cookie("csrf", path="/")
    await get_audit().log(user["username"], "logout")
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return {"username": user["username"], "role": user["role"], "id": user["id"]}


@router.get("/enabled")
async def enabled():
    return {"auth_enabled": AUTH_ENABLED}


@router.post("/password")
async def change_password(
    payload: dict,
    user: dict = Depends(current_user),
    auth=Depends(get_auth),
):
    old = str(payload.get("old_password", ""))
    new = str(payload.get("new_password", ""))
    if len(new) < 8:
        raise HTTPException(400, "new password too short (min 8 chars)")
    ok, msg = await auth.change_password(user["id"], old, new)
    if not ok:
        raise HTTPException(400, msg)
    await get_audit().log(user["username"], "change_password")
    return {"ok": True}


@router.get("/users", dependencies=[Depends(rate_limit_general)])
async def list_users(user: dict = Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "admin required")
    return await Database.get().list_users()


@router.post("/users", dependencies=[Depends(rate_limit_general)])
async def create_user(
    payload: dict,
    user: dict = Depends(current_user),
    auth=Depends(get_auth),
):
    if user["role"] != "admin":
        raise HTTPException(403, "admin required")
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    role = str(payload.get("role", "user"))
    if not username or len(password) < 8:
        raise HTTPException(400, "username required and password >= 8 chars")
    if role not in ("admin", "user"):
        raise HTTPException(400, "invalid role")
    ok, msg = await auth.create_user(username, password, role)
    if not ok:
        raise HTTPException(400, msg)
    await get_audit().log(user["username"], "create_user", username)
    return {"ok": True}
