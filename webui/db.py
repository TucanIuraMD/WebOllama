"""SQLite database layer — settings, jobs, metrics, users, audit_log."""
import asyncio
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

import aiosqlite

from .config import DB_PATH, HISTORY_RETENTION, JOB_KEEP_DAYS

logger = logging.getLogger(__name__)

# ----- Schema ---------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    operation   TEXT NOT NULL,
    model       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'running',
    progress    REAL NOT NULL DEFAULT 0,
    current     INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    speed       REAL NOT NULL DEFAULT 0,
    started_at  REAL NOT NULL,
    finished_at REAL,
    duration    REAL NOT NULL DEFAULT 0,
    output      TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS metrics (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    kind     TEXT NOT NULL,
    payload  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);
CREATE INDEX IF NOT EXISTS idx_metrics_kind ON metrics(kind);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin',
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    action   TEXT NOT NULL,
    model    TEXT NOT NULL DEFAULT '',
    result   TEXT NOT NULL DEFAULT 'success',
    error    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
""";


# ----- Connection pool ------------------------------------------------------
class Database:
    _instance: Optional["Database"] = None

    def __init__(self, path: str = DB_PATH) -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        Database._instance = self

    @classmethod
    def get(cls) -> "Database":
        assert cls._instance is not None, "Database not initialized"
        return cls._instance

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()
        logger.info("Database ready at %s", self.path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params=()) -> aiosqlite.Cursor:
        async with self._lock:
            return await self._conn.execute(sql, params)

    async def execute_many(self, sql: str, params=()) -> aiosqlite.Cursor:
        async with self._lock:
            return await self._conn.execute(sql, params)

    async def executemany(self, sql: str, params_seq) -> None:
        async with self._lock:
            await self._conn.executemany(sql, params_seq)
            await self._conn.commit()

    async def commit(self) -> None:
        await self._conn.commit()

    async def fetchone(self, sql: str, params=()) -> Optional[sqlite3.Row]:
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            return await cur.fetchone()

    async def fetchall(self, sql: str, params=()) -> list:
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            return await cur.fetchall()

    # ---- Settings ----------------------------------------------------------
    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value)
        )
        await self.commit()

    async def get_all_settings(self) -> dict:
        rows = await self.fetchall("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in rows}

    # ---- Jobs --------------------------------------------------------------
    async def upsert_job(self, job: dict) -> None:
        await self.execute(
            """INSERT OR REPLACE INTO jobs
            (id, operation, model, status, progress, current, total, speed,
             started_at, finished_at, duration, output, error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job["id"], job["operation"], job["model"], job["status"],
                job.get("progress", 0), job.get("current", 0), job.get("total", 0),
                job.get("speed", 0), job["started_at"],
                job.get("finished_at"), job.get("duration", 0),
                job.get("output", ""), job.get("error", ""),
            ),
        )
        await self.commit()

    async def get_job(self, job_id: str) -> Optional[dict]:
        row = await self.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row:
            return dict(row)
        return None

    async def list_jobs(self, limit: int = 50) -> list:
        rows = await self.fetchall(
            "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    async def prune_old_jobs(self) -> int:
        cutoff = time.time() - JOB_KEEP_DAYS * 86400
        await self.execute(
            "DELETE FROM jobs WHERE finished_at IS NOT NULL AND finished_at < ?",
            (cutoff,),
        )
        deleted = self._conn.total_changes if self._conn else 0
        await self.commit()
        return deleted

    # ---- Metrics -----------------------------------------------------------
    async def insert_metric(self, ts: float, kind: str, payload: dict) -> None:
        await self.execute(
            "INSERT INTO metrics (ts, kind, payload) VALUES (?,?,?)",
            (ts, kind, json.dumps(payload, default=str)),
        )
        await self.commit()

    async def get_metrics(
        self, kind: str, since: float, limit: int = 5000
    ) -> list:
        rows = await self.fetchall(
            "SELECT ts, payload FROM metrics WHERE kind=? AND ts>=? ORDER BY ts ASC LIMIT ?",
            (kind, since, limit),
        )
        result = []
        for row in rows:
            try:
                result.append({"ts": row["ts"], "data": json.loads(row["payload"])})
            except json.JSONDecodeError:
                continue
        return result

    async def prune_old_metrics(self) -> int:
        cutoff = time.time() - HISTORY_RETENTION
        await self.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
        deleted = self._conn.total_changes if self._conn else 0
        await self.commit()
        return deleted

    # ---- Users ---------------------------------------------------------------
    async def get_user_by_username(self, username: str) -> Optional[dict]:
        row = await self.fetchone(
            "SELECT * FROM users WHERE username=?", (username,)
        )
        return dict(row) if row else None

    async def get_user_by_id(self, user_id: int) -> Optional[dict]:
        row = await self.fetchone(
            "SELECT * FROM users WHERE id=?", (user_id,)
        )
        return dict(row) if row else None

    async def create_user(self, username: str, password_hash: str, role: str = "admin") -> int:
        cur = await self.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
            (username, password_hash, role, time.time()),
        )
        await self.commit()
        return cur.lastrowid

    async def list_users(self) -> list:
        rows = await self.fetchall("SELECT id, username, role, created_at FROM users")
        return [dict(r) for r in rows]

    # ---- Sessions ------------------------------------------------------------
    async def create_session(self, token: str, user_id: int, expires_at: float) -> None:
        await self.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token, user_id, time.time(), expires_at),
        )
        await self.commit()

    async def get_session(self, token: str) -> Optional[dict]:
        row = await self.fetchone(
            "SELECT * FROM sessions WHERE token=? AND expires_at>?",
            (token, time.time()),
        )
        return dict(row) if row else None

    async def delete_session(self, token: str) -> None:
        await self.execute("DELETE FROM sessions WHERE token=?", (token,))
        await self.commit()

    async def delete_expired_sessions(self) -> None:
        await self.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
        await self.commit()

    # ---- Audit log -----------------------------------------------------------
    async def add_audit(
        self, username: str, action: str, model: str = "",
        result: str = "success", error: str = "",
    ) -> None:
        await self.execute(
            "INSERT INTO audit_log (ts, username, action, model, result, error) VALUES (?,?,?,?,?,?)",
            (time.time(), username, action, model, result, error),
        )
        await self.commit()

    async def list_audit(
        self, limit: int = 100, offset: int = 0, username: str = "",
        action: str = "",
    ) -> list:
        parts = ["SELECT * FROM audit_log"]
        args = []
        conds = []
        if username:
            conds.append("username=?")
            args.append(username)
        if action:
            conds.append("action=?")
            args.append(action)
        if conds:
            parts.append("WHERE " + " AND ".join(conds))
        parts.append("ORDER BY ts DESC LIMIT ? OFFSET ?")
        args.extend([limit, offset])
        rows = await self.fetchall(" ".join(parts), args)
        return [dict(r) for r in rows]