"""Authentication: password hashing (PBKDF2-HMAC-SHA256), sessions, tokens."""
import hashlib
import hmac
import logging
import os
import secrets
import time

from .config import (
    AUTH_ENABLED,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USER,
    SESSION_TTL_SECONDS,
)
from .db import Database

logger = logging.getLogger(__name__)

_PBKDF2_ITERATIONS = 260_000


def hash_password(password: str, salt: str | None = None) -> str:
    """Return string 'pbkdf2$iterations$salt$hash'."""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS
    )
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, hex_hash = stored.split("$")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        )
        return hmac.compare_digest(dk.hex(), hex_hash)
    except (ValueError, TypeError):
        return False


class AuthManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def ensure_default_admin(self) -> None:
        if not AUTH_ENABLED:
            return
        user = await self.db.get_user_by_username(DEFAULT_ADMIN_USER)
        if user is None:
            await self.db.create_user(
                DEFAULT_ADMIN_USER,
                hash_password(DEFAULT_ADMIN_PASSWORD),
                role="admin",
            )
            logger.info("Created default admin user '%s'", DEFAULT_ADMIN_USER)

    async def authenticate(self, username: str, password: str) -> dict | None:
        user = await self.db.get_user_by_username(username)
        if user and verify_password(password, user["password_hash"]):
            return user
        return None

    async def create_session(self, user: dict) -> str:
        token = secrets.token_urlsafe(48)
        await self.db.create_session(token, user["id"], time.time() + SESSION_TTL_SECONDS)
        return token

    async def get_user_from_token(self, token: str) -> dict | None:
        if not token:
            return None
        session = await self.db.get_session(token)
        if session is None:
            return None
        return await self.db.get_user_by_id(session["user_id"])

    async def logout(self, token: str) -> None:
        if token:
            await self.db.delete_session(token)

    async def change_password(
        self, user_id: int, old_password: str, new_password: str
    ) -> tuple[bool, str]:
        user = await self.db.get_user_by_id(user_id)
        if not user:
            return False, "user not found"
        if not verify_password(old_password, user["password_hash"]):
            return False, "old password is incorrect"
        await self.db.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (hash_password(new_password), user_id),
        )
        await self.db.commit()
        return True, "password changed"

    async def create_user(self, username: str, password: str, role: str = "user") -> tuple[bool, str]:
        if await self.db.get_user_by_username(username):
            return False, "username already exists"
        await self.db.create_user(username, hash_password(password), role)
        return True, "user created"
