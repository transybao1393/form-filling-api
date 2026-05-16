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
    webhook_url: str | None = None,
    team_id: int | None = None,
) -> Path:
    """Create JOBS_DIR/<job_id>/ with initial state.json + meta.json + uploads/.

    `team_id` scopes the job to a Team (Phase 2 onwards). None means the job
    was created by an unauthenticated client (legacy / AUTH_REQUIRED=0) and
    is only visible to other unauthenticated callers.
    """
    d = job_dir(job_id)
    (d / "uploads").mkdir(parents=True, exist_ok=True)

    _atomic_write_json(meta_path(job_id), {
        "job_id": job_id,
        "questionnaire_filename": questionnaire_filename,
        "reference_filenames": reference_filenames,
        "questionnaire_title": questionnaire_title,
        "webhook_url": webhook_url,
        "team_id": team_id,
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


def get_meta(job_id: str) -> dict[str, Any] | None:
    p = meta_path(job_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def update_state(job_id: str, **kwargs: Any) -> None:
    """Merge kwargs into state.json atomically.

    No-op if the job dir was deleted concurrently (DELETE /jobs/{id} mid-run);
    we don't want to resurrect a state.json the user just asked us to remove.
    """
    if not job_dir(job_id).exists():
        log.info("update_state: job_id=%s deleted, skipping write", job_id)
        return
    p = state_path(job_id)
    state = json.loads(p.read_text()) if p.exists() else {"job_id": job_id}
    state.update(kwargs)
    _atomic_write_json(p, state)


def write_result(job_id: str, data: dict[str, Any]) -> None:
    if not job_dir(job_id).exists():
        log.info("write_result: job_id=%s deleted, skipping write", job_id)
        return
    _atomic_write_json(result_path(job_id), data)


def mark_failed(job_id: str, exc: BaseException) -> None:
    if not job_dir(job_id).exists():
        log.info("mark_failed: job_id=%s deleted, skipping write", job_id)
        return
    error_path(job_id).write_text("".join(traceback.format_exception(exc)))
    update_state(
        job_id,
        status="failed",
        stage="failed",
        stage_text=f"Failed: {type(exc).__name__}",
        completed_at=_now_iso(),
        error=f"{type(exc).__name__}: {exc}",
    )


def delete(job_id: str) -> bool:
    """Remove JOBS_DIR/<job_id>/ recursively. Returns True if removed,
    False if it didn't exist (caller should map that to HTTP 404)."""
    d = job_dir(job_id)
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=False)
    log.info("delete: removed job_id=%s", job_id)
    return True


_TEAM_FILTER_ANONYMOUS = object()  # sentinel: filter to jobs with team_id == None


def list_jobs(
    *,
    statuses: set[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    team_id: int | None | object = None,
) -> list[dict[str, Any]]:
    """Return one record per job dir (state ∪ a select subset of meta),
    filtered by status / submitted_at window, sorted newest-first.

    `team_id` filter semantics:
        - None (default): no team filter — returns ALL jobs (admin view).
        - int: returns only jobs whose meta.team_id matches.
        - _TEAM_FILTER_ANONYMOUS: returns only jobs with meta.team_id is None
          (i.e. created before auth was enabled).

    Skips dirs missing state.json (a create() that hasn't completed its
    atomic write yet) and dirs whose state.json is corrupt — listing should
    never throw on a single bad job.

    Cost is O(N) state.json + meta.json reads. Fine up to ~10k jobs;
    beyond that, swap for an index. Result.json is intentionally never
    read here (large; not needed for list views).
    """
    out: list[dict[str, Any]] = []
    if not config.JOBS_DIR.exists():
        return out
    for child in config.JOBS_DIR.iterdir():
        if not child.is_dir():
            continue
        sp = child / "state.json"
        if not sp.exists():
            continue
        try:
            state = json.loads(sp.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if statuses and state.get("status") not in statuses:
            continue
        if since or until:
            sub = state.get("submitted_at")
            try:
                t = datetime.fromisoformat(sub) if sub else None
            except ValueError:
                t = None
            if since is not None and (t is None or t < since):
                continue
            if until is not None and (t is None or t >= until):
                continue
        meta: dict[str, Any] = {}
        mp = child / "meta.json"
        if mp.exists():
            try:
                meta = json.loads(mp.read_text())
            except (json.JSONDecodeError, OSError):
                meta = {}
        meta_team = meta.get("team_id")
        if team_id is _TEAM_FILTER_ANONYMOUS:
            if meta_team is not None:
                continue
        elif isinstance(team_id, int):
            if meta_team != team_id:
                continue
        out.append({
            **state,
            "questionnaire_filename": meta.get("questionnaire_filename"),
            "reference_filenames": meta.get("reference_filenames") or [],
            "questionnaire_title": meta.get("questionnaire_title"),
            "has_webhook": bool(meta.get("webhook_url")),
            "team_id": meta_team,
        })
    out.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
    return out


def team_owns(job_id: str, team_id: int | None) -> bool | None:
    """Return True if `team_id` matches the job's stored team_id.

    Returns False on mismatch and None if the job does not exist. Used by
    the API layer to map (no access | not found) → HTTP 404.
    """
    meta = get_meta(job_id)
    if meta is None:
        return None
    return meta.get("team_id") == team_id


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
