"""Output normalization + one-shot repair logic.

Coerces the model's raw JSON toward the canonical schema and, when the model
emits filler answers or cites an unknown source file, collapses the triple
(extracted_answer, source_file, confidence) to ("-", "N/A", "NONE") so the
caller can flag the item for human review.
"""

from __future__ import annotations

import json
import logging
import re

from pydantic import ValidationError

from . import ollama_client, prompts
from .schemas import DataJson


log = logging.getLogger("llm_service.normalize")


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


def normalize(data: DataJson, known_filenames: set[str]) -> None:
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


async def parse_and_validate(
    raw: str, original_messages: list[dict[str, str]]
) -> DataJson:
    """Parse JSON + validate; on failure, ask Ollama to fix it once."""
    try:
        return DataJson.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        repair_msgs = prompts.build_repair_messages(original_messages, raw, str(e))
        second = await ollama_client.chat(repair_msgs)
        return DataJson.model_validate(json.loads(second))
