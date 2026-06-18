"""Worker-side job logic for /generate-data-json.

`run_generation(ctx, job_id)` is the function the arq worker invokes. It
reads inputs from JOBS_DIR/<job_id>/, extracts text from the questionnaire
and references, hands off to the host-native llm_service for the actual LLM
work, and writes the returned data.json alongside the state. Progress is
reported between stages via job_store.update_state().
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from arq.worker import Retry

from . import config, db as app_db, job_store, llm_service_client, models, template_task_store, usage
from .extractors import UnsupportedFileType, extract_text
from .schemas import DataJson
from .template_helpers import items_to_field_schema


log = logging.getLogger("api.jobs")


# --------------------------------------------------------------------------- #
# Worker entry point
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def run_generation(ctx: dict[str, Any], job_id: str) -> None:
    """The arq worker function. ctx is provided by arq."""
    log.info("run_generation: starting job_id=%s", job_id)
    update = lambda **kw: job_store.update_state(job_id, **kw)

    try:
        meta = job_store.get_meta(job_id)
        if meta is None:
            log.info("run_generation: job_id=%s deleted before start, skipping", job_id)
            return
        uploads = job_store.uploads_dir(job_id)

        update(
            status="running",
            started_at=_now_iso(),
            percent=10,
            stage="extracting_questionnaire",
            stage_text="Reading the questionnaire",
        )
        q_path: Path = uploads / meta["questionnaire_filename"]
        try:
            q_text = extract_text(q_path)
        except UnsupportedFileType as e:
            raise RuntimeError(f"unsupported questionnaire type: {e}") from e
        if not q_text:
            raise RuntimeError(
                f"could not extract any text from {meta['questionnaire_filename']!r}"
            )

        update(
            percent=25,
            stage="extracting_references",
            stage_text="Reading reference documents",
        )
        refs: list[tuple[str, str]] = []
        for name in meta.get("reference_filenames", []):
            text = extract_text(uploads / name)
            if text:
                refs.append((name, text))

        update(
            percent=40,
            stage="calling_llm_service",
            stage_text="Generating answers",
        )
        data_dict = await llm_service_client.generate(
            q_text, refs, meta.get("questionnaire_title")
        )
        # llm_service has already parsed, repaired, normalized, and
        # renumbered. We re-parse here only so the downstream review-check
        # logic below can introspect Item objects.
        data = DataJson.model_validate(data_dict)

        update(percent=99, stage="saving", stage_text="Writing data.json")
        job_store.write_result(job_id, data.model_dump())

        # Phase 3: surface low-confidence answers for human review instead of
        # silently shipping "-" for fields the model couldn't find. Any item
        # whose confidence is NONE — including filler answers the llm_service
        # already coerced to NONE — blocks auto-completion. POST
        # /jobs/{id}/approve transitions review → completed.
        needs_review = any(item.confidence == "NONE" for item in data.items)
        terminal_status = "review" if needs_review else "completed"
        terminal_stage = "review" if needs_review else "completed"
        terminal_text = (
            "Awaiting reviewer — open items need a human"
            if needs_review else "Done"
        )

        update(
            percent=100,
            status=terminal_status,
            stage=terminal_stage,
            stage_text=terminal_text,
            completed_at=_now_iso(),
        )
        log.info(
            "run_generation: %s job_id=%s items=%d none=%d",
            terminal_status, job_id, len(data.items),
            sum(1 for i in data.items if i.confidence == "NONE"),
        )
        await usage.increment(meta.get("team_id"), jobs_count=1)
        await _maybe_enqueue_webhook(ctx, job_id)

    except Exception as e:
        log.exception("run_generation: job_id=%s failed", job_id)
        job_store.mark_failed(job_id, e)
        await _maybe_enqueue_webhook(ctx, job_id)
        raise


# --------------------------------------------------------------------------- #
# Webhook delivery — opt-in callback when a job reaches a terminal state
# --------------------------------------------------------------------------- #

async def _maybe_enqueue_webhook(ctx: dict[str, Any], job_id: str) -> None:
    """Queue a webhook delivery job if the caller registered one. Runs as a
    separate arq job so retries/backoff are isolated from the main worker
    and a slow receiver can't pin a generation slot."""
    meta = job_store.get_meta(job_id)
    if meta is None or not meta.get("webhook_url"):
        return
    pool = ctx.get("redis")
    if pool is None:
        log.warning("_maybe_enqueue_webhook: no arq pool in ctx, skipping")
        return
    # max_tries=4 (set on the worker registration) → arq attempts at ~0s,
    # 2s, 4s, 8s before giving up.
    await pool.enqueue_job("deliver_webhook", job_id)
    log.info("_maybe_enqueue_webhook: queued for job_id=%s", job_id)


async def _record_delivery(
    *,
    team_id: int | None,
    job_id: str,
    event: str,
    url: str,
    attempt: int,
    http_status: int | None,
    response_excerpt: str | None,
    error: str | None,
) -> None:
    """Persist one webhook-delivery attempt to the DB. Best-effort — a DB
    failure here must not block the worker, so we swallow exceptions."""
    try:
        sm = app_db.get_sessionmaker()
        async with sm() as session:
            session.add(
                models.WebhookDelivery(
                    team_id=team_id,
                    job_id=job_id,
                    event=event,
                    url=url,
                    http_status=http_status,
                    attempt=attempt,
                    delivered_at=datetime.now(timezone.utc),
                    response_excerpt=(response_excerpt[:500] if response_excerpt else None),
                    error=(error[:500] if error else None),
                )
            )
            await session.commit()
    except Exception as e:
        log.warning("_record_delivery: failed to persist row: %s", e)


async def deliver_webhook(ctx: dict[str, Any], job_id: str) -> None:
    """POST the terminal-state payload to the caller's webhook_url. Raises on
    network errors / 5xx so arq retries with exponential backoff; 4xx is
    swallowed (caller misconfigured — no point retrying)."""
    meta = job_store.get_meta(job_id)
    state = job_store.get_state(job_id)
    if meta is None or state is None:
        log.info("deliver_webhook: job_id=%s no longer exists, dropping", job_id)
        return
    webhook_url = meta.get("webhook_url")
    if not webhook_url:
        return

    team_id = meta.get("team_id")
    status = state.get("status")
    event = f"job.{status}"
    result_available = status in ("review", "completed")
    download_url = f"/jobs/{job_id}/data.json" if result_available else None
    result_data: dict[str, Any] | None = None
    if result_available:
        rp = job_store.result_path(job_id)
        if rp.exists():
            result_data = json.loads(rp.read_text())

    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "stage": state.get("stage"),
        "submitted_at": state.get("submitted_at"),
        "completed_at": state.get("completed_at"),
        "error": state.get("error"),
        "status_url": f"/jobs/{job_id}",
        "download_url": download_url,
        "result": result_data,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "form-pipeline-webhook/1",
        "X-Form-Pipeline-Job-Id": job_id,
        "X-Form-Pipeline-Event": event,
    }
    if config.WEBHOOK_SECRET:
        sig = hmac.new(
            config.WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256,
        ).hexdigest()
        headers["X-Form-Pipeline-Signature"] = f"sha256={sig}"

    # Compute the next-attempt backoff up front: 2s, 4s, 8s after tries
    # 1, 2, 3 (arq gives up on try == max_tries=4 anyway, so the 4th defer
    # is never observed).
    job_try = int(ctx.get("job_try", 1) or 1)
    backoff_s = float(2 ** job_try)

    try:
        async with httpx.AsyncClient(timeout=config.WEBHOOK_TIMEOUT) as client:
            resp = await client.post(webhook_url, content=body, headers=headers)
    except httpx.HTTPError as e:
        log.warning(
            "deliver_webhook: job_id=%s url=%s try=%d network error %s — retry in %.0fs",
            job_id, webhook_url, job_try, e, backoff_s,
        )
        await _record_delivery(
            team_id=team_id, job_id=job_id, event=event, url=webhook_url,
            attempt=job_try, http_status=None,
            response_excerpt=None, error=f"{type(e).__name__}: {e}",
        )
        # arq retries only on Retry / CancelledError / RetryJob — a plain
        # exception is treated as a permanent failure.
        raise Retry(defer=backoff_s) from e

    response_excerpt = (resp.text or "")[:500]

    if 200 <= resp.status_code < 300:
        log.info(
            "deliver_webhook: job_id=%s url=%s try=%d → %d OK",
            job_id, webhook_url, job_try, resp.status_code,
        )
        await _record_delivery(
            team_id=team_id, job_id=job_id, event=event, url=webhook_url,
            attempt=job_try, http_status=resp.status_code,
            response_excerpt=response_excerpt, error=None,
        )
        return
    if 400 <= resp.status_code < 500:
        log.warning(
            "deliver_webhook: job_id=%s url=%s try=%d → %d, dropping (no retry)",
            job_id, webhook_url, job_try, resp.status_code,
        )
        await _record_delivery(
            team_id=team_id, job_id=job_id, event=event, url=webhook_url,
            attempt=job_try, http_status=resp.status_code,
            response_excerpt=response_excerpt, error="4xx; no retry",
        )
        return
    log.warning(
        "deliver_webhook: job_id=%s url=%s try=%d → %d, retry in %.0fs",
        job_id, webhook_url, job_try, resp.status_code, backoff_s,
    )
    await _record_delivery(
        team_id=team_id, job_id=job_id, event=event, url=webhook_url,
        attempt=job_try, http_status=resp.status_code,
        response_excerpt=response_excerpt, error=f"5xx; will retry in {backoff_s:.0f}s",
    )
    raise Retry(defer=backoff_s)


async def run_template_generation(ctx: dict[str, Any], task_id: str) -> None:
    """Extract field schema from a form and persist a Template row."""
    log.info("run_template_generation: starting task_id=%s", task_id)
    update = lambda **kw: template_task_store.update_state(task_id, **kw)

    try:
        meta = template_task_store.get_meta(task_id)
        if meta is None:
            log.info("run_template_generation: task_id=%s deleted, skipping", task_id)
            return

        update(
            status="running",
            started_at=_now_iso(),
            percent=10,
            stage="extracting_form",
            stage_text="Reading the form document",
        )

        form_path: Path | None = None
        if meta.get("form_filename"):
            form_path = template_task_store.uploads_dir(task_id) / meta["form_filename"]
        elif meta.get("document_storage_path"):
            form_path = Path(meta["document_storage_path"])

        if form_path is None or not form_path.exists():
            raise RuntimeError("form document not found for template generation")

        try:
            q_text = extract_text(form_path)
        except UnsupportedFileType as e:
            raise RuntimeError(f"unsupported form type: {e}") from e
        if not q_text:
            raise RuntimeError(f"could not extract any text from {form_path.name!r}")

        update(
            percent=40,
            stage="calling_llm_service",
            stage_text="Extracting field list",
        )
        data_dict = await llm_service_client.extract_fields(
            q_text, meta.get("name")
        )
        schema = items_to_field_schema(data_dict.get("items") or [])
        if not schema:
            raise RuntimeError("LLM returned no fields for this form")

        update(percent=90, stage="saving", stage_text="Saving template")

        sm = app_db.get_sessionmaker()
        async with sm() as session:
            tpl = models.Template(
                team_id=meta["team_id"],
                created_by_user_id=meta.get("user_id"),
                name=meta["name"],
                field_schema=schema,
                source_job_id=None,
                source_document_id=meta.get("document_id"),
                uses=0,
                created_at=datetime.now(timezone.utc),
            )
            session.add(tpl)
            await session.commit()
            await session.refresh(tpl)
            template_id = tpl.id

        update(
            percent=100,
            status="completed",
            stage="completed",
            stage_text="Template saved",
            completed_at=_now_iso(),
            template_id=template_id,
        )
        log.info(
            "run_template_generation: completed task_id=%s template_id=%d fields=%d",
            task_id, template_id, len(schema),
        )

    except Exception as e:
        log.exception("run_template_generation: task_id=%s failed", task_id)
        template_task_store.update_state(
            task_id,
            status="failed",
            stage="failed",
            stage_text=f"Failed: {type(e).__name__}",
            completed_at=_now_iso(),
            error=f"{type(e).__name__}: {e}",
        )
        raise
