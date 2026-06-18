"""File-based state store for async template generation tasks.

Each task lives at `JOBS_DIR/_template_tasks/<task_id>/` containing:
    state.json    - progress + status
    meta.json     - input snapshot (name, document_id, filenames, team_id)
    uploads/      - optional uploaded form file
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _root() -> Path:
    return config.JOBS_DIR / "_template_tasks"


def task_dir(task_id: str) -> Path:
    return _root() / task_id


def uploads_dir(task_id: str) -> Path:
    return task_dir(task_id) / "uploads"


def state_path(task_id: str) -> Path:
    return task_dir(task_id) / "state.json"


def meta_path(task_id: str) -> Path:
    return task_dir(task_id) / "meta.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def create(
    task_id: str,
    *,
    name: str,
    team_id: int,
    user_id: int | None,
    document_id: int | None = None,
    form_filename: str | None = None,
    document_storage_path: str | None = None,
) -> Path:
    d = task_dir(task_id)
    (d / "uploads").mkdir(parents=True, exist_ok=True)

    _atomic_write_json(meta_path(task_id), {
        "task_id": task_id,
        "name": name,
        "team_id": team_id,
        "user_id": user_id,
        "document_id": document_id,
        "form_filename": form_filename,
        "document_storage_path": document_storage_path,
    })

    _atomic_write_json(state_path(task_id), {
        "task_id": task_id,
        "status": "queued",
        "percent": 0,
        "stage": "queued",
        "stage_text": "Queued, waiting for worker",
        "submitted_at": _now_iso(),
        "started_at": None,
        "completed_at": None,
        "error": None,
        "template_id": None,
    })
    return d


def get_state(task_id: str) -> dict[str, Any] | None:
    p = state_path(task_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def get_meta(task_id: str) -> dict[str, Any] | None:
    p = meta_path(task_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def update_state(task_id: str, **kwargs: Any) -> None:
    if not task_dir(task_id).exists():
        return
    p = state_path(task_id)
    state = json.loads(p.read_text()) if p.exists() else {"task_id": task_id}
    state.update(kwargs)
    _atomic_write_json(p, state)
