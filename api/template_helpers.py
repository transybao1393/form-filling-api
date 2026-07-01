"""Shared helpers for template field_schema ↔ LLM questionnaire text."""

from __future__ import annotations

from typing import Any


def items_to_field_schema(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map data.json items to Template.field_schema rows."""
    schema: list[dict[str, Any]] = []
    for i, it in enumerate(items):
        qnum = (it.get("question_number") or "").strip() or f"F{i + 1}"
        label = (it.get("question") or "").strip()
        ctx_q = (it.get("contextualized_question") or "").strip() or None
        schema.append({
            "id": qnum,
            "label": label,
            "type": "text",
            "required": it.get("confidence") != "NONE",
            "contextualized_question": ctx_q,
        })
    return schema


def field_schema_to_questionnaire_text(schema: list[dict[str, Any]]) -> str:
    """Build QUESTIONNAIRE block text from a saved field_schema snapshot."""
    lines: list[str] = []
    for i, field in enumerate(schema, start=1):
        qnum = (field.get("id") or "").strip() or f"F{i}"
        label = (field.get("label") or "").strip()
        ctx = (field.get("contextualized_question") or "").strip()
        if label:
            lines.append(f"{qnum}. {label}")
        else:
            lines.append(qnum)
        if ctx:
            lines.append(ctx)
    return "\n".join(lines) if lines else "Untitled form"
