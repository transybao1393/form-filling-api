"""Redis Pub/Sub event bus for real-time status updates.

Workers and the API publish after every state.json write. The ws_service
subscribes and fans out to WebSocket clients. Redis Pub/Sub is at-most-once;
disk/REST remain the source of truth.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from . import config

log = logging.getLogger("api.event_bus")

_redis_client: Any | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_redis():
    global _redis_client
    if _redis_client is None:
        import redis

        _redis_client = redis.Redis(
            host=config.REDIS_HOST,
            port=config.REDIS_PORT,
            db=config.REDIS_DATABASE,
            decode_responses=True,
        )
    return _redis_client


def team_channel(team_id: int) -> str:
    return f"fp:team:{team_id}"


def resource_channel(kind: str, resource_id: str) -> str:
    return f"fp:resource:{kind}:{resource_id}"


def build_envelope(
    *,
    kind: str,
    resource_id: str,
    team_id: int | None,
    state: dict[str, Any],
) -> dict[str, Any]:
    return {
        "v": 1,
        "kind": kind,
        "id": resource_id,
        "team_id": team_id,
        "revision": state.get("revision", 0),
        "status": state.get("status"),
        "percent": state.get("percent", 0),
        "stage": state.get("stage"),
        "stage_text": state.get("stage_text"),
        "error": state.get("error"),
        "template_id": state.get("template_id"),
        "completed_at": state.get("completed_at"),
        "submitted_at": state.get("submitted_at"),
        "started_at": state.get("started_at"),
        "emitted_at": _now_iso(),
    }


def publish_status(
    *,
    kind: str,
    resource_id: str,
    team_id: int | None,
    state: dict[str, Any],
) -> None:
    """Publish to team + resource Redis channels. Never raises."""
    envelope = build_envelope(
        kind=kind,
        resource_id=resource_id,
        team_id=team_id,
        state=state,
    )
    payload = json.dumps(envelope, ensure_ascii=False)
    try:
        r = _get_redis()
        if team_id is not None:
            r.publish(team_channel(team_id), payload)
        r.publish(resource_channel(kind, resource_id), payload)
    except Exception:
        log.warning(
            "event_bus publish failed kind=%s id=%s",
            kind,
            resource_id,
            exc_info=True,
        )


def publish_job_update(job_id: str, state: dict[str, Any], team_id: int | None) -> None:
    publish_status(kind="job", resource_id=job_id, team_id=team_id, state=state)


def publish_template_task_update(
    task_id: str, state: dict[str, Any], team_id: int | None,
) -> None:
    publish_status(kind="template_task", resource_id=task_id, team_id=team_id, state=state)
