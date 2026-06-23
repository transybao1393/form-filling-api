"""Read current status snapshots from disk for WS subscribe."""

from __future__ import annotations

from typing import Any

from api import event_bus, job_store, template_task_store


def snapshot_for_channel(channel: str) -> dict[str, Any] | None:
    """Return event envelope for channel, or None if not found."""
    if channel.startswith("team:"):
        return None

    if channel.startswith("job:"):
        job_id = channel[4:]
        state = job_store.get_state(job_id)
        if state is None:
            return None
        meta = job_store.get_meta(job_id)
        team_id = meta.get("team_id") if meta else None
        return event_bus.build_envelope(
            kind="job", resource_id=job_id, team_id=team_id, state=state,
        )

    if channel.startswith("template_task:"):
        task_id = channel[14:]
        state = template_task_store.get_state(task_id)
        if state is None:
            return None
        meta = template_task_store.get_meta(task_id)
        team_id = meta.get("team_id") if meta else None
        return event_bus.build_envelope(
            kind="template_task",
            resource_id=task_id,
            team_id=team_id,
            state=state,
        )

    return None
