"""Shared FastAPI dependencies: auth guard, CSRF guard, rate limiting."""
from typing import Optional

from fastapi import Depends, HTTPException, Request

from .config import (
    AUTH_ENABLED,
    RATE_LIMIT_MAX,
    RATE_LIMIT_MAX_DANGEROUS,
    RATE_LIMIT_WINDOW,
)
from .auth import AuthManager
from .db import Database
from .security import RateLimiter, csrf_ok

_general_limiter = RateLimiter(RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)
_dangerous_limiter = RateLimiter(RATE_LIMIT_MAX_DANGEROUS, RATE_LIMIT_WINDOW)

_auth: Optional[AuthManager] = None


def get_auth() -> AuthManager:
    global _auth
    if _auth is None:
        _auth = AuthManager(Database.get())
    return _auth


def _extract_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get("session", "")


async def current_user(
    request: Request, auth: AuthManager = Depends(get_auth)
) -> dict:
    """Require authenticated user."""
    token = _extract_token(request)
    user = await auth.get_user_from_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def require_user(request: Request, auth: AuthManager = Depends(get_auth)) -> dict:
    """Optional auth."""
    if not AUTH_ENABLED:
        return {"username": "local", "role": "admin", "id": 0}
    token = _extract_token(request)
    user = await auth.get_user_from_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit_general(request: Request) -> None:
    if not AUTH_ENABLED:
        return
    ok, retry = _general_limiter.allow(f"general:{client_ip(request)}")
    if not ok:
        raise HTTPException(status_code=429, detail=f"rate limit exceeded, retry in {retry}s")


async def rate_limit_dangerous(request: Request) -> None:
    if not AUTH_ENABLED:
        return
    ok, retry = _dangerous_limiter.allow(f"danger:{client_ip(request)}")
    if not ok:
        raise HTTPException(status_code=429, detail=f"rate limit exceeded, retry in {retry}s")


def csrf_guard(request: Request) -> None:
    """CSRF check for state-changing requests when using cookie sessions."""
    if not AUTH_ENABLED:
        return
    if request.headers.get("Authorization", "").lower().startswith("bearer "):
        return
    cookie_token = request.cookies.get("csrf")
    header_token = request.headers.get("X-CSRF-Token", "")
    if not csrf_ok(header_token, cookie_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")