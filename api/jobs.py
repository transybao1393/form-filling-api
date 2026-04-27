"""Worker-side job logic for /generate-data-json.

`run_generation(ctx, job_id)` is the function the arq worker invokes. It
reads inputs from JOBS_DIR/<job_id>/, drives the same pipeline the old sync
endpoint used (extract → prompt → Ollama → validate → normalize), and
writes the result alongside the state. Progress is reported between stages
via job_store.update_state().
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import job_store, ollama_client, prompts
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

    except Exception as e:
        log.exception("run_generation: job_id=%s failed", job_id)
        job_store.mark_failed(job_id, e)
        raise
