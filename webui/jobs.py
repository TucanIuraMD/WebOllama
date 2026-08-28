"""Job manager — long-running operations (pull/push/create/delete/copy/stop).

Jobs run as asyncio tasks, persist state to SQLite on every progress tick and
broadcast updates over WebSocket. They never block an HTTP request.
"""
import asyncio
import logging
import time
import uuid
from typing import Awaitable, Callable, Optional

from .db import Database
from .ollama_client import OllamaClient, OllamaError, ProgressEvent

logger = logging.getLogger(__name__)

JOB_STATUSES = {"pending", "running", "complete", "error", "cancelled"}


class Job:
    def __init__(self, operation: str, model: str = "") -> None:
        self.id = uuid.uuid4().hex[:12]
        self.operation = operation
        self.model = model
        self.status = "pending"
        self.progress = 0.0
        self.current = 0
        self.total = 0
        self.speed = 0.0
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.duration = 0.0
        self.output: list[str] = []
        self.error = ""
        self._cancel_event = asyncio.Event()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "operation": self.operation,
            "model": self.model,
            "status": self.status,
            "progress": round(self.progress, 2),
            "current": self.current,
            "total": self.total,
            "speed": self.speed,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": round(self.duration, 2),
            "output": "\n".join(self.output[-500:]),
            "error": self.error,
        }

    def cancel(self) -> None:
        self._cancel_event.set()


class JobManager:
    def __init__(self, db: Database, client: OllamaClient) -> None:
        self.db = db
        self.client = client
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._broadcast: Optional[Callable[[dict], Awaitable[None]]] = None

    def set_broadcast(self, fn: Callable[[dict], Awaitable[None]]) -> None:
        self._broadcast = fn

    async def start(self) -> None:
        """Restore state: mark any non-terminal jobs from a previous run as interrupted."""
        for job in await self.db.list_jobs(limit=1000):
            if job["status"] in ("running", "pending"):
                job["status"] = "error"
                job["error"] = (job["error"] or "") + "\n[interrupted by restart]"
                job["finished_at"] = time.time()
                job["duration"] = (job["finished_at"] - job["started_at"]) if job["finished_at"] else 0
                await self.db.upsert_job(job)
        await self.db.prune_old_jobs()

    async def _notify(self, job: Job) -> None:
        await self.db.upsert_job(job.to_dict())
        if self._broadcast:
            try:
                await self._broadcast({"type": "job_update", "job": job.to_dict()})
            except Exception:
                pass

    async def _finish(self, job: Job, status: str, error: str = "") -> Job:
        job.status = status
        job.error = error
        job.finished_at = time.time()
        job.duration = job.finished_at - job.started_at
        if status != "complete":
            job.progress = job.progress  # keep last
        await self._notify(job)
        return job

    async def submit(
        self,
        operation: str,
        model: str,
        runner: Callable[[Job], Awaitable[None]],
    ) -> Job:
        job = Job(operation, model)
        async with self._lock:
            self._jobs[job.id] = job
        await self.db.upsert_job(job.to_dict())
        task = asyncio.create_task(self._run(job, runner))
        self._tasks[job.id] = task

        def _on_done(_t: asyncio.Task) -> None:
            self._tasks.pop(job.id, None)
            # Safety net: if the task was cancelled before _run could run its
            # CancelledError handler, mark the job cancelled from here.
            if _t.cancelled() and job.status not in ("complete", "error", "cancelled"):
                asyncio.get_event_loop().create_task(
                    self._finish(job, "cancelled", "job cancelled")
                )

        task.add_done_callback(_on_done)
        return job

    async def _run(self, job: Job, runner: Callable[[Job], Awaitable[None]]) -> None:
        try:
            await runner(job)
            if job.status == "pending":
                # runner didn't manage the lifecycle itself — mark complete
                await self._finish(job, "complete")
        except asyncio.CancelledError:
            await self._finish(job, "cancelled", "job cancelled")
        except OllamaError as exc:
            await self._finish(job, "error", exc.message)
        except Exception as exc:  # pragma: no cover
            logger.exception("Job %s failed", job.id)
            await self._finish(job, "error", str(exc))

    # ---- concrete jobs -----------------------------------------------------------
    async def run_pull(self, job: Job, name: str) -> None:
        job.status = "running"
        job.progress = 0
        await self._notify(job)
        last_tick = time.monotonic()
        last_bytes = 0

        def on_progress(evt: ProgressEvent) -> None:
            nonlocal last_tick, last_bytes
            # only meaningful byte-progress events carry a total
            if evt.total > 0:
                job.progress = evt.percent
                job.current = evt.completed
                job.total = evt.total
                now = time.monotonic()
                dt = now - last_tick
                if evt.completed >= last_bytes and dt >= 1.0:
                    job.speed = (evt.completed - last_bytes) / dt if dt > 0 else 0
                    last_tick = now
                    last_bytes = evt.completed
            job.output.append(evt.status or evt.digest or "")
            asyncio.create_task(self._notify(job))

        try:
            await self.client.pull(name, on_progress, job._cancel_event)
            job.progress = 100
            job.current = job.total
            await self._finish(job, "complete")
        except OllamaError as exc:
            if job._cancel_event.is_set():
                await self._finish(job, "cancelled", "cancelled")
            else:
                await self._finish(job, "error", exc.message)

    async def run_push(self, job: Job, name: str) -> None:
        job.status = "running"
        await self._notify(job)
        last_tick = time.monotonic()
        last_bytes = 0

        def on_progress(evt: ProgressEvent) -> None:
            nonlocal last_tick, last_bytes
            if evt.total > 0:
                job.progress = evt.percent
                job.current = evt.completed
                job.total = evt.total
                now = time.monotonic()
                if evt.completed >= last_bytes and now - last_tick >= 1.0:
                    job.speed = (evt.completed - last_bytes) / (now - last_tick) if now > last_tick else 0
                    last_tick = now
                    last_bytes = evt.completed
            job.output.append(evt.status or evt.digest or "")
            asyncio.create_task(self._notify(job))

        try:
            await self.client.push(name, on_progress, job._cancel_event)
            job.progress = 100
            await self._finish(job, "complete")
        except OllamaError as exc:
            await self._finish(job, "error", exc.message)

    async def run_create(self, job: Job, name: str, modelfile: str) -> None:
        job.status = "running"
        await self._notify(job)

        def on_progress(evt: ProgressEvent) -> None:
            job.progress = evt.percent
            job.output.append(evt.status or evt.digest or "")
            asyncio.create_task(self._notify(job))

        try:
            await self.client.create(name, modelfile, on_progress, job._cancel_event)
            job.progress = 100
            await self._finish(job, "complete")
        except OllamaError as exc:
            await self._finish(job, "error", exc.message)

    async def run_delete(self, job: Job, name: str) -> None:
        job.status = "running"
        job.progress = 10
        await self._notify(job)
        try:
            await asyncio.wait_for(self.client.delete(name), timeout=300)
            job.progress = 100
            await self._finish(job, "complete")
        except OllamaError as exc:
            await self._finish(job, "error", exc.message)
        except asyncio.TimeoutError:
            await self._finish(job, "error", "delete timed out")

    async def run_copy(self, job: Job, source: str, destination: str) -> None:
        job.status = "running"
        job.progress = 10
        await self._notify(job)
        try:
            await asyncio.wait_for(self.client.copy(source, destination), timeout=300)
            job.progress = 100
            await self._finish(job, "complete")
        except OllamaError as exc:
            await self._finish(job, "error", exc.message)
        except asyncio.TimeoutError:
            await self._finish(job, "error", "copy timed out")

    async def run_stop(self, job: Job, name: str) -> None:
        job.status = "running"
        job.progress = 10
        await self._notify(job)
        try:
            await self.client.stop(name)
            job.progress = 100
            await self._finish(job, "complete")
        except OllamaError as exc:
            await self._finish(job, "error", exc.message)

    # ---- querying ------------------------------------------------------------------
    async def get(self, job_id: str) -> dict | None:
        job = self._jobs.get(job_id)
        if job:
            return job.to_dict()
        return await self.db.get_job(job_id)

    async def list(self, limit: int = 50) -> list[dict]:
        jobs = [j.to_dict() for j in self._jobs.values()]
        # merge in DB-only jobs (persisted from previous runs)
        db_jobs = await self.db.list_jobs(limit=limit)
        ids = {j["id"] for j in jobs}
        jobs.extend(j for j in db_jobs if j["id"] not in ids)
        jobs.sort(key=lambda x: x.get("started_at", 0), reverse=True)
        return jobs[:limit]

    async def cancel(self, job_id: str) -> dict | None:
        job = self._jobs.get(job_id)
        if not job:
            return await self.db.get_job(job_id)
        job.cancel()  # sets the stream cancel event
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        await self._notify(job)
        return job.to_dict()


_manager: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    assert _manager is not None, "JobManager not initialized"
    return _manager


def set_job_manager(mgr: JobManager) -> None:
    global _manager
    _manager = mgr
