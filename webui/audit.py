"""Audit log helpers."""
import logging

from .db import Database

logger = logging.getLogger(__name__)


class Audit:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def log(
        self, username: str, action: str, model: str = "",
        result: str = "success", error: str = "",
    ) -> None:
        try:
            await self.db.add_audit(username, action, model, result, error)
        except Exception as exc:  # pragma: no cover
            logger.warning("audit write failed: %s", exc)

    async def list(self, limit: int = 100, offset: int = 0, username: str = "", action: str = "") -> list:
        return await self.db.list_audit(limit=limit, offset=offset, username=username, action=action)


_audit: Audit | None = None


def get_audit() -> Audit:
    global _audit
    if _audit is None:
        _audit = Audit(Database.get())
    return _audit
