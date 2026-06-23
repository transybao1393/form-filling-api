"""Redis Pub/Sub event bus for real-time status updates.

Workers and the API publish after every state.json write. The ws_service
subscribes and fans out to WebSocket clients. Redis Pub/Sub is at-most-once;
disk/REST remain the source of truth.

WS / Redis channel keys: user, job, template (no team-scoped feeds).
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


def user_channel(user_id: int) -> str:
    return f"fp:user:{user_id}"


def job_channel(job_id: str) -> str:
    return f"fp:job:{job_id}"


def template_channel(task_id: str) -> str:
    return f"fp:template:{task_id}"


def build_envelope(
    *,
    kind: str,
    resource_id: str,
    user_id: int | None,
    state: dict[str, Any],
) -> dict[str, Any]:
    return {
        "v": 1,
        "kind": kind,
        "id": resource_id,
        "user_id": user_id,
        "revision": state.get("revision", 0),
        "status": state.get("status"),
        "percent": state.get("percent", 0),
        "stage": state.get("stage"),
        "stage_text": state.get("stage_text"),
        "error": state.get("error"),
        "template_id": state.get("template_id"),
        "name": state.get("name"),
        "completed_at": state.get("completed_at"),
        "submitted_at": state.get("submitted_at"),
        "started_at": state.get("started_at"),
        "emitted_at": _now_iso(),
    }


def publish_job_update(
    job_id: str,
    state: dict[str, Any],
    user_id: int | None,
) -> None:
    """Publish to per-job channel. Never raises."""
    envelope = build_envelope(
        kind="job",
        resource_id=job_id,
        user_id=user_id,
        state=state,
    )
    payload = json.dumps(envelope, ensure_ascii=False)
    try:
        r = _get_redis()
        r.publish(job_channel(job_id), payload)
    except Exception:
        log.warning(
            "event_bus publish failed kind=job id=%s",
            job_id,
            exc_info=True,
        )


def publish_template_update(
    task_id: str,
    state: dict[str, Any],
    user_id: int | None,
) -> None:
    """Publish to per-user feed + per-template channel. Never raises."""
    envelope = build_envelope(
        kind="template",
        resource_id=task_id,
        user_id=user_id,
        state=state,
    )
    payload = json.dumps(envelope, ensure_ascii=False)
    try:
        r = _get_redis()
        if user_id is not None:
            r.publish(user_channel(user_id), payload)
        r.publish(template_channel(task_id), payload)
    except Exception:
        log.warning(
            "event_bus publish failed kind=template id=%s",
            task_id,
            exc_info=True,
        )


# Back-compat alias for callers that still use the old name.
publish_template_task_update = publish_template_update
