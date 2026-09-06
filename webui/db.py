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

-- ---- Agents tab (practical model × agent knowledge base) -------------------
-- Agents are environments/hosts where a model can be used (Hermes, OpenCode,
-- Claude, OpenWebUI, ...). They are regular DB rows: new agents can be added
-- at runtime without code changes. Capabilities are the same kind of entity.
CREATE TABLE IF NOT EXISTS agents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    slug        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS capabilities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    slug        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- One assessment per (model name, agent). The model is referenced by its exact
-- Ollama model name (as returned by GET /api/tags) — variants like
-- deepseek-coder-v2 and deepseek-coder-v2-tools-16k stay separate rows.
-- No row for a pair == status "untested"; untested is never stored as failed.
CREATE TABLE IF NOT EXISTS model_agent_assessments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    model      TEXT NOT NULL,
    agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'untested'
               CHECK (status IN ('untested','failed','works','good')),
    note       TEXT NOT NULL DEFAULT '',
    tested_at  REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (model, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_maa_model ON model_agent_assessments(model);
CREATE INDEX IF NOT EXISTS idx_maa_agent ON model_agent_assessments(agent_id);

CREATE TABLE IF NOT EXISTS assessment_capabilities (
    assessment_id INTEGER NOT NULL REFERENCES model_agent_assessments(id) ON DELETE CASCADE,
    capability_id INTEGER NOT NULL REFERENCES capabilities(id) ON DELETE CASCADE,
    PRIMARY KEY (assessment_id, capability_id)
);
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
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()
        await self.seed_agents_defaults()
        logger.info("Database ready at %s", self.path)

    # ---- Agents tab ---------------------------------------------------------
    AGENTS_SEED_AGENTS = [
        ("Hermes", "hermes", "Hermes agent / CLI environment"),
        ("OpenCode", "opencode", "OpenCode coding agent"),
        ("Claude", "claude", "Claude Code agent"),
        ("OpenWebUI", "openwebui", "OpenWebUI chat frontend"),
    ]
    AGENTS_SEED_CAPABILITIES = [
        ("Coding", "coding", "Writing and editing code"),
        ("Chat", "chat", "General conversation"),
        ("Analysis", "analysis", "Data and code analysis"),
        ("Vision", "vision", "Image understanding"),
        ("Data", "data", "Data processing / pipelines"),
        ("RAG", "rag", "Retrieval-augmented generation"),
        ("Tools", "tools", "Tool / function calling"),
        ("Research", "research", "Long research / summarization"),
    ]

    async def seed_agents_defaults(self) -> None:
        """Idempotent seed of the default agents/capabilities.

        Runs on every connect; INSERT OR IGNORE keeps user-created rows and
        additions safe. Extend these lists (or insert rows via API) to add
        new agents like Automation / Documents / OCR — no schema change needed.
        """
        now = time.time()
        for i, (name, slug, desc) in enumerate(self.AGENTS_SEED_AGENTS):
            await self.execute(
                "INSERT OR IGNORE INTO agents (name, slug, description, sort_order, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (name, slug, desc, i, now, now),
            )
        for i, (name, slug, desc) in enumerate(self.AGENTS_SEED_CAPABILITIES):
            await self.execute(
                "INSERT OR IGNORE INTO capabilities (name, slug, description, sort_order) VALUES (?,?,?,?)",
                (name, slug, desc, i),
            )
        await self.commit()

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

    # ---- Agents tab: CRUD ----------------------------------------------------
    VALID_ASSESSMENT_STATUSES = ("untested", "failed", "works", "good")

    async def list_agents(self, include_disabled: bool = True) -> list:
        sql = "SELECT * FROM agents"
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY sort_order, id"
        return [dict(r) for r in await self.fetchall(sql)]

    async def get_agent(self, agent_id: int) -> Optional[dict]:
        row = await self.fetchone("SELECT * FROM agents WHERE id=?", (agent_id,))
        return dict(row) if row else None

    async def get_agent_by_slug(self, slug: str) -> Optional[dict]:
        row = await self.fetchone("SELECT * FROM agents WHERE slug=?", (slug,))
        return dict(row) if row else None

    async def create_agent(self, name: str, slug: str, description: str = "", enabled: bool = True) -> dict:
        now = time.time()
        cur = await self.execute(
            "INSERT INTO agents (name, slug, description, enabled, sort_order, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (name, slug, description, 1 if enabled else 0, 999, now, now),
        )
        await self.commit()
        return await self.get_agent(cur.lastrowid)

    async def update_agent(self, agent_id: int, fields: dict) -> Optional[dict]:
        allowed = {"name", "slug", "description", "enabled", "sort_order"}
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k}=?")
                args.append(v)
        if not sets:
            return await self.get_agent(agent_id)
        sets.append("updated_at=?")
        args.extend([time.time(), agent_id])
        await self.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id=?", args)
        await self.commit()
        return await self.get_agent(agent_id)

    async def delete_agent(self, agent_id: int) -> bool:
        cur = await self.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        await self.commit()
        return cur.rowcount > 0

    async def list_capabilities(self) -> list:
        rows = await self.fetchall(
            "SELECT * FROM capabilities ORDER BY sort_order, id"
        )
        return [dict(r) for r in rows]

    async def get_capability(self, cap_id: int) -> Optional[dict]:
        row = await self.fetchone("SELECT * FROM capabilities WHERE id=?", (cap_id,))
        return dict(row) if row else None

    async def create_capability(self, name: str, slug: str, description: str = "") -> dict:
        cur = await self.execute(
            "INSERT INTO capabilities (name, slug, description, sort_order) VALUES (?,?,?,999)",
            (name, slug, description),
        )
        await self.commit()
        return await self.get_capability(cur.lastrowid)

    # ---- Assessments -----------------------------------------------------------
    async def get_assessment(self, model: str, agent_id: int) -> Optional[dict]:
        row = await self.fetchone(
            "SELECT * FROM model_agent_assessments WHERE model=? AND agent_id=?",
            (model, agent_id),
        )
        return dict(row) if row else None

    async def upsert_assessment(
        self, model: str, agent_id: int, status: str, note: str = "",
        capabilities: Optional[list[int]] = None, tested_at: Optional[float] = None,
    ) -> dict:
        """Insert or update one model×agent assessment.

        tested_at is set automatically (now) unless the caller pins it.
        status must be one of VALID_ASSESSMENT_STATUSES; stored untested rows
        clear tested_at — untested is not a test result.
        """
        if status not in self.VALID_ASSESSMENT_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        now = time.time()
        if tested_at is None:
            tested_at = None if status == "untested" else now
        await self.execute(
            """INSERT INTO model_agent_assessments (model, agent_id, status, note, tested_at, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT (model, agent_id)
            DO UPDATE SET status=excluded.status, note=excluded.note,
                          tested_at=excluded.tested_at, updated_at=excluded.updated_at""",
            (model, agent_id, status, note, tested_at, now, now),
        )
        row = await self.fetchone(
            "SELECT id FROM model_agent_assessments WHERE model=? AND agent_id=?",
            (model, agent_id),
        )
        aid = row["id"]
        await self.execute(
            "DELETE FROM assessment_capabilities WHERE assessment_id=?", (aid,)
        )
        if capabilities:
            await self.executemany(
                "INSERT OR IGNORE INTO assessment_capabilities (assessment_id, capability_id) VALUES (?,?)",
                [(aid, int(c)) for c in capabilities],
            )
        await self.commit()
        return await self.get_assessment(model, agent_id)

    async def delete_assessment(self, assessment_id: int) -> bool:
        cur = await self.execute(
            "DELETE FROM model_agent_assessments WHERE id=?", (assessment_id,)
        )
        await self.commit()
        return cur.rowcount > 0

    async def list_assessments(
        self, model: Optional[str] = None, agent_id: Optional[int] = None
    ) -> list:
        sql = """SELECT a.*, ag.name AS agent_name, ag.slug AS agent_slug
                 FROM model_agent_assessments a
                 JOIN agents ag ON ag.id = a.agent_id"""
        conds, args = [], []
        if model is not None:
            conds.append("a.model=?")
            args.append(model)
        if agent_id is not None:
            conds.append("a.agent_id=?")
            args.append(agent_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY a.model, ag.sort_order, ag.id"
        rows = await self.fetchall(sql, args)
        out = []
        for r in rows:
            d = dict(r)
            caps = await self.fetchall(
                """SELECT c.id, c.name, c.slug FROM assessment_capabilities ac
                   JOIN capabilities c ON c.id = ac.capability_id
                   WHERE ac.assessment_id=? ORDER BY c.sort_order, c.id""",
                (d["id"],),
            )
            d["capabilities"] = [dict(c) for c in caps]
            out.append(d)
        return out