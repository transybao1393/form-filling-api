"""arq worker entrypoint.

Run with:

    .venv/bin/arq api.worker.WorkerSettings

The worker pulls one job at a time off the Redis queue and executes
`api.jobs.run_generation`. A cron job runs cleanup_expired() every 6 hours
to GC job directories older than JOB_TTL_HOURS.
"""

from __future__ import annotations

import logging
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from . import config, job_store, jobs


log = logging.getLogger("api.worker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def cleanup_job(ctx: dict[str, Any]) -> int:
    removed = job_store.cleanup_expired()
    log.info("cleanup_expired: removed %d expired job directories", removed)
    return removed


async def on_startup(ctx: dict[str, Any]) -> None:
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "worker startup: JOBS_DIR=%s OLLAMA_URL=%s OLLAMA_MODEL=%s",
        config.JOBS_DIR, config.OLLAMA_URL, config.OLLAMA_MODEL,
    )
    await cleanup_job(ctx)


class WorkerSettings:
    functions = [jobs.run_generation]
    cron_jobs = [
        cron(cleanup_job, hour=set(range(0, 24, 6)), minute=0, run_at_startup=False),
    ]
    on_startup = on_startup
    redis_settings = RedisSettings(
        host=config.REDIS_HOST,
        port=config.REDIS_PORT,
        database=config.REDIS_DATABASE,
    )
    # Keep results in Redis for 1 hour as a debug aid; the source of truth
    # for a job is the on-disk state.json.
    keep_result = 3600
    # Don't auto-retry — caller re-POSTs (per plan).
    max_tries = 1
    # Per-job hard timeout: typical Ollama call is < 90s, but big inputs can
    # legitimately need a few minutes. Match OLLAMA_TIMEOUT + buffer.
    job_timeout = config.OLLAMA_TIMEOUT + 60
