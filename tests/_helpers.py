"""Importable helpers for the test suite.

Lives outside conftest.py because pytest collects conftest.py for fixtures
but does NOT add it to sys.path for `from conftest import ...`. Plain
modules under tests/ (with the package's __init__.py) are importable as
`from _helpers import ...`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest


BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
REPO_ROOT = Path(__file__).resolve().parent.parent
LLM_TESTS_ENABLED = os.getenv("LLM_TESTS", "0") == "1"
# Two separate budgets — conflating them was masking real LLM regressions
# behind queue-backlog noise from earlier fire-and-forget submission tests.
#   QUEUE_TIMEOUT — wall-clock budget for status to advance past `queued`.
#                   Sized to handle ~15 fire-and-forget jobs ahead in the queue
#                   (single-worker arq). Pure test-env artifact.
#   LLM_TIMEOUT_S — once the worker is on the job (status==running), how long
#                   we'll wait for completion. The real production budget.
QUEUE_TIMEOUT_S = float(os.getenv("QUEUE_TIMEOUT", "1500"))
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT", "600"))


def upload(name: str, path: Path, mime: str = "application/octet-stream"):
    """Build a multipart entry for httpx files= argument."""
    return (name, (path.name, path.read_bytes(), mime))


def upload_bytes(name: str, filename: str, data: bytes,
                 mime: str = "application/octet-stream"):
    return (name, (filename, data, mime))


def submit_job(http_client: httpx.Client,
               questionnaire: Path,
               references: list[Path] | None = None,
               title: str | None = None) -> dict:
    """Submit a /generate-data-json job; assert 202 and return the body."""
    files = [("questionnaire_file",
              (questionnaire.name, questionnaire.read_bytes(), "application/pdf"))]
    for ref in references or []:
        files.append(("reference_files",
                      (ref.name, ref.read_bytes(), "application/octet-stream")))
    data = {"questionnaire_title": title} if title else {}
    r = http_client.post("/generate-data-json", files=files, data=data)
    assert r.status_code == 202, f"submit failed: {r.status_code} {r.text[:200]}"
    body = r.json()
    assert {"job_id", "status", "status_url", "download_url"} <= body.keys()
    return body


def poll_until_terminal(http_client: httpx.Client,
                        job_id: str,
                        queue_timeout_s: float = QUEUE_TIMEOUT_S,
                        run_timeout_s: float = LLM_TIMEOUT_S,
                        interval_s: float = 1.0) -> dict:
    """Poll /jobs/{id} until terminal. Two-phase budget:

      queue_timeout_s — max time spent in `queued` (waiting for a worker)
      run_timeout_s   — once `running`, max time for the LLM to finish
    """
    queue_deadline = time.monotonic() + queue_timeout_s
    run_deadline: float | None = None
    last: dict = {}
    while True:
        r = http_client.get(f"/jobs/{job_id}")
        assert r.status_code == 200, f"unexpected status: {r.status_code}"
        last = r.json()
        status = last.get("status")
        if status in {"completed", "failed"}:
            return last
        # First time we see status leave queued, switch to the run-phase budget.
        if status == "running" and run_deadline is None:
            run_deadline = time.monotonic() + run_timeout_s
        deadline = run_deadline if run_deadline is not None else queue_deadline
        if time.monotonic() >= deadline:
            phase = "running" if run_deadline is not None else "queued"
            budget = run_timeout_s if run_deadline is not None else queue_timeout_s
            pytest.fail(
                f"job {job_id} stuck in `{phase}` past {budget}s budget; last={last}"
            )
        time.sleep(interval_s)
