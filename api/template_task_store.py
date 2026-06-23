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

from . import config, event_bus


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

    state = {
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
        "revision": 1,
    }
    _atomic_write_json(state_path(task_id), state)
    event_bus.publish_template_task_update(task_id, state, team_id)
    return d


def get_state(task_id: str) -> dict[str, Any] | None:
    p = state_path(task_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def list_tasks(
    *,
    team_id: int,
    statuses: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return in-flight template generation tasks for a team, newest first."""
    root = _root()
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        sp = child / "state.json"
        mp = child / "meta.json"
        if not sp.exists() or not mp.exists():
            continue
        try:
            state = json.loads(sp.read_text())
            meta = json.loads(mp.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("team_id") != team_id:
            continue
        st = state.get("status", "queued")
        if statuses and st not in statuses:
            continue
        out.append({**state, **meta})
    out.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
    return out


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
    state["revision"] = int(state.get("revision", 0)) + 1
    _atomic_write_json(p, state)
    meta = get_meta(task_id)
    team_id = meta.get("team_id") if meta else None
    event_bus.publish_template_task_update(task_id, state, team_id)
