"""File-based job state store.

Each job lives at `JOBS_DIR/<job_id>/` containing:
    state.json    - progress + status (read by the API)
    meta.json     - input snapshot (filenames, title override)
    uploads/      - original uploaded files
    result.json   - the produced data.json (only when status == completed)
    error.log     - exception traceback (only when status == failed)

Writes are atomic via tempfile + os.replace. The arq worker is single-process
so per-job state has no concurrent writer.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config


log = logging.getLogger("api.job_store")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job_dir(job_id: str) -> Path:
    return config.JOBS_DIR / job_id


def uploads_dir(job_id: str) -> Path:
    return job_dir(job_id) / "uploads"


def state_path(job_id: str) -> Path:
    return job_dir(job_id) / "state.json"


def meta_path(job_id: str) -> Path:
    return job_dir(job_id) / "meta.json"


def result_path(job_id: str) -> Path:
    return job_dir(job_id) / "result.json"


def error_path(job_id: str) -> Path:
    return job_dir(job_id) / "error.log"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def create(
    job_id: str,
    *,
    questionnaire_filename: str,
    reference_filenames: list[str],
    questionnaire_title: str | None,
) -> Path:
    """Create JOBS_DIR/<job_id>/ with initial state.json + meta.json + uploads/."""
    d = job_dir(job_id)
    (d / "uploads").mkdir(parents=True, exist_ok=True)

    _atomic_write_json(meta_path(job_id), {
        "job_id": job_id,
        "questionnaire_filename": questionnaire_filename,
        "reference_filenames": reference_filenames,
        "questionnaire_title": questionnaire_title,
    })

    _atomic_write_json(state_path(job_id), {
        "job_id": job_id,
        "status": "queued",
        "percent": 0,
        "stage": "queued",
        "stage_text": "Queued, waiting for worker",
        "submitted_at": _now_iso(),
        "started_at": None,
        "completed_at": None,
        "error": None,
    })
    return d


def get_state(job_id: str) -> dict[str, Any] | None:
    p = state_path(job_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def get_meta(job_id: str) -> dict[str, Any]:
    return json.loads(meta_path(job_id).read_text())


def update_state(job_id: str, **kwargs: Any) -> None:
    """Merge kwargs into state.json atomically."""
    p = state_path(job_id)
    state = json.loads(p.read_text()) if p.exists() else {"job_id": job_id}
    state.update(kwargs)
    _atomic_write_json(p, state)


def write_result(job_id: str, data: dict[str, Any]) -> None:
    _atomic_write_json(result_path(job_id), data)


def mark_failed(job_id: str, exc: BaseException) -> None:
    error_path(job_id).write_text("".join(traceback.format_exception(exc)))
    update_state(
        job_id,
        status="failed",
        stage="failed",
        stage_text=f"Failed: {type(exc).__name__}",
        completed_at=_now_iso(),
        error=f"{type(exc).__name__}: {exc}",
    )


def cleanup_expired() -> int:
    """Delete job directories older than JOB_TTL_HOURS. Returns count removed."""
    if not config.JOBS_DIR.exists():
        return 0
    cutoff = time.time() - config.JOB_TTL_HOURS * 3600
    removed = 0
    for child in config.JOBS_DIR.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = max(p.stat().st_mtime for p in child.rglob("*") if p.is_file())
        except ValueError:
            mtime = child.stat().st_mtime
        if mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
            log.info("cleanup_expired: removed %s", child.name)
    return removed
