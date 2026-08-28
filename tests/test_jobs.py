"""Tests for the job manager."""
import asyncio

import pytest

from webui.jobs import JobManager


@pytest.mark.asyncio
async def test_pull_job_completes(job_manager, mock_ollama):
    jm = job_manager
    job = await jm.submit("pull", "test:model", lambda j: jm.run_pull(j, "test:model"))
    for _ in range(100):
        if job.status in ("complete", "error"):
            break
        await asyncio.sleep(0.05)
    assert job.status == "complete"
    assert job.progress == 100


@pytest.mark.asyncio
async def test_delete_job_completes(job_manager, mock_ollama):
    jm = job_manager
    job = await jm.submit("delete", "qwen3:8b", lambda j: jm.run_delete(j, "qwen3:8b"))
    for _ in range(100):
        if job.status in ("complete", "error"):
            break
        await asyncio.sleep(0.05)
    assert job.status == "complete"


@pytest.mark.asyncio
async def test_copy_job_completes(job_manager, mock_ollama):
    jm = job_manager
    job = await jm.submit("copy", "qwen3:8b -> copy", lambda j: jm.run_copy(j, "qwen3:8b", "qwen3:8b-t"))
    for _ in range(100):
        if job.status in ("complete", "error"):
            break
        await asyncio.sleep(0.05)
    assert job.status == "complete"


@pytest.mark.asyncio
async def test_job_persisted_to_db(job_manager):
    jm = job_manager
    job = await jm.submit("pull", "x:y", lambda j: asyncio.sleep(0.01))
    for _ in range(100):
        if job.status in ("complete", "error"):
            break
        await asyncio.sleep(0.05)
    saved = await jm.get(job.id)
    assert saved is not None
    assert saved["operation"] == "pull"
    assert saved["status"] in ("complete", "running")


@pytest.mark.asyncio
async def test_cancel_job(job_manager, mock_ollama):
    jm = job_manager
    job = await jm.submit("pull", "slow:model", lambda j: asyncio.sleep(5))
    await jm.cancel(job.id)
    await asyncio.sleep(0.2)
    assert job.status == "cancelled"


@pytest.mark.asyncio
async def test_restart_marks_stale_jobs(db, mock_ollama):
    # simulate a running job left by a previous process
    await db.upsert_job({
        "id": "stale1", "operation": "pull", "model": "x", "status": "running",
        "progress": 50, "current": 0, "total": 0, "speed": 0,
        "started_at": __import__("time").time() - 10, "finished_at": None,
        "duration": 0, "output": "", "error": "",
    })
    jm = JobManager(db, mock_ollama)
    await jm.start()
    stale = await db.get_job("stale1")
    assert stale["status"] == "error"
    assert "interrupted by restart" in stale["error"]
