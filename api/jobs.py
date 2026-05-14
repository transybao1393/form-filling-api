"""Worker-side job logic for /generate-data-json.

`run_generation(ctx, job_id)` is the function the arq worker invokes. It
reads inputs from JOBS_DIR/<job_id>/, drives the same pipeline the old sync
endpoint used (extract → prompt → Ollama → validate → normalize), and
writes the result alongside the state. Progress is reported between stages
via job_store.update_state().
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from arq.worker import Retry
from pydantic import ValidationError

from . import config, job_store, ollama_client, prompts
from .extractors import UnsupportedFileType, extract_text
from .schemas import DataJson


log = logging.getLogger("api.jobs")


# --------------------------------------------------------------------------- #
# Normalization helpers (moved from main.py — now job logic, not endpoint)
# --------------------------------------------------------------------------- #

# Filler strings the model emits when it can't find an answer; we coerce
# these into the sentinel triple ("-", "N/A", "NONE").
_FILLER_ANSWER_RE = re.compile(
    r"^\s*(?:-|n/?a|none|null|nil|unknown|tbd|tba|not\s+specified|"
    r"not\s+mentioned|not\s+available|not\s+provided|not\s+applicable)\s*\.?\s*$",
    re.IGNORECASE,
)
# Leading section-code prefix the model often pastes into `question`
# (e.g. "F1 ", "ENV-3 ", "S.10 ").
_SECTION_CODE_RE = re.compile(r"^[A-Za-z]{1,4}[\s.\-]?\d{1,3}[\s.\-:)]+")
_QUESTION_MAX_LEN = 60


def _clean_question(question: str, contextualized: str) -> str:
    q = question.strip()
    q = _SECTION_CODE_RE.sub("", q).strip()
    ctx = contextualized.strip()
    if ctx and ctx in q:
        q = q.split(ctx, 1)[0].strip(" /-—–|·.,")
    if len(q) > _QUESTION_MAX_LEN:
        cut = q[:_QUESTION_MAX_LEN].rsplit(" ", 1)[0] or q[:_QUESTION_MAX_LEN]
        q = cut.rstrip(" /-—–|·.,")
    return q or question.strip()


def _normalize(data: DataJson, known_filenames: set[str]) -> None:
    """Coerce model output toward the canonical schema. Mutates in place."""
    for item in data.items:
        item.question = _clean_question(item.question, item.contextualized_question)

        ans = (item.extracted_answer or "").strip()
        is_filler = not ans or _FILLER_ANSWER_RE.match(ans) is not None
        is_unknown_source = (
            item.source_file not in known_filenames
            and item.source_file != "N/A"
        )

        if (
            item.confidence == "NONE"
            or is_filler
            or (item.confidence != "NONE" and is_unknown_source)
        ):
            if is_unknown_source and not is_filler and item.confidence != "NONE":
                log.warning(
                    "answer cited unknown source_file=%r; coercing to NONE",
                    item.source_file,
                )
            item.extracted_answer = "-"
            item.source_file = "N/A"
            item.confidence = "NONE"


# --------------------------------------------------------------------------- #
# Validation with one-shot repair
# --------------------------------------------------------------------------- #

async def _parse_and_validate(
    raw: str, original_messages: list[dict[str, str]]
) -> DataJson:
    """Parse JSON + validate; on failure, ask Ollama to fix it once."""
    try:
        return DataJson.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        repair_msgs = prompts.build_repair_messages(original_messages, raw, str(e))
        second = await ollama_client.chat(repair_msgs)
        return DataJson.model_validate(json.loads(second))


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
            percent=35,
            stage="building_prompt",
            stage_text="Building prompt for Qwen3:8b",
        )
        messages = prompts.build_messages(
            q_text, refs, meta.get("questionnaire_title")
        )

        update(
            percent=40,
            stage="calling_llm",
            stage_text="Generating answers (Qwen3:8b)",
        )
        raw = await ollama_client.chat(messages)

        update(
            percent=90,
            stage="normalizing",
            stage_text="Validating and normalizing output",
        )
        data = await _parse_and_validate(raw, messages)
        if meta.get("questionnaire_title"):
            data.questionnaire_title = meta["questionnaire_title"]

        known = set(meta.get("reference_filenames", []))
        _normalize(data, known)

        # Defensive: renumber to guarantee F1..Fn even if the model drifted.
        for i, item in enumerate(data.items, start=1):
            item.question_number = f"F{i}"

        update(percent=99, stage="saving", stage_text="Writing data.json")
        job_store.write_result(job_id, data.model_dump())

        update(
            percent=100,
            status="completed",
            stage="completed",
            stage_text="Done",
            completed_at=_now_iso(),
        )
        log.info("run_generation: completed job_id=%s items=%d", job_id, len(data.items))
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

    status = state.get("status")
    download_url = f"/jobs/{job_id}/data.json" if status == "completed" else None
    result_data: dict[str, Any] | None = None
    if status == "completed":
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
        "X-Form-Pipeline-Event": f"job.{status}",
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
        # arq retries only on Retry / CancelledError / RetryJob — a plain
        # exception is treated as a permanent failure.
        raise Retry(defer=backoff_s) from e

    if 200 <= resp.status_code < 300:
        log.info(
            "deliver_webhook: job_id=%s url=%s try=%d → %d OK",
            job_id, webhook_url, job_try, resp.status_code,
        )
        return
    if 400 <= resp.status_code < 500:
        log.warning(
            "deliver_webhook: job_id=%s url=%s try=%d → %d, dropping (no retry)",
            job_id, webhook_url, job_try, resp.status_code,
        )
        return
    log.warning(
        "deliver_webhook: job_id=%s url=%s try=%d → %d, retry in %.0fs",
        job_id, webhook_url, job_try, resp.status_code, backoff_s,
    )
    raise Retry(defer=backoff_s)
