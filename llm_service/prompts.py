"""Prompt builders for Qwen3:8b structured extraction."""

from __future__ import annotations

from . import config


SYSTEM_PROMPT = """/no_think
You are an expert form-analysis assistant. Read the QUESTIONNAIRE block and \
produce a JSON object listing every question it contains. When REFERENCE \
blocks are provided, fill in answers using ONLY those references.

Return JSON that matches this exact schema:

{
  "questionnaire_title": "string",
  "items": [
    {
      "contextualized_question": "string",
      "source_file": "string",
      "question": "string",
      "question_number": "F1",
      "extracted_answer": "string",
      "confidence": "HIGH" | "MEDIUM" | "NONE"
    }
  ]
}

# Hard rules

1. **Questions come ONLY from the QUESTIONNAIRE block.** Never invent questions.
2. **Numbering**: sequential `F1`, `F2`, `F3`, ... in order of appearance. \
Do not reuse the form's own section codes (R1, G1, ENV, S, E, etc.).
3. **`question` is the SHORT TOPIC LABEL** — the noun-phrase header as it \
would appear on the form. Maximum 8 words / ~50 characters. Examples of \
good values: `"Organization Name"`, `"Tax ID"`, `"GHG emissions intensity"`, \
`"Headquarters address"`. NEVER include the section code. NEVER restate the \
contextualized sentence. NEVER concatenate the section header with the \
question text.
4. **`contextualized_question` is the FULL self-contained sentence**, ending \
with `?`. Expand acronyms, add an implicit subject (e.g. "the organization"), \
make it readable without seeing the form.
5. **Answers come ONLY from a REFERENCE block.** Never use world knowledge. \
Before writing an answer you must be able to point at a literal sentence in a \
REFERENCE block that supports it.
   - If a reference clearly states the answer (verbatim or near-verbatim): \
`confidence="HIGH"`, `source_file=<exact filename of that reference>`, \
`extracted_answer=<the value, kept tight>`.
   - If you have to paraphrase or combine 2+ sentences from references: \
`confidence="MEDIUM"`, `source_file=<filename>`, `extracted_answer=<paraphrase>`.
   - If no reference contains the answer, output the **exact sentinel triple**: \
`extracted_answer="-"`, `source_file="N/A"`, `confidence="NONE"`. NEVER write \
"Not specified", "Not mentioned", "N/A", "Unknown", "TBD", or any other filler.
6. **No answer reuse.** Each `extracted_answer` must come from text actually \
addressing that specific question. Do not paste the same answer across \
multiple questions unless the references genuinely give the same answer.
7. **`questionnaire_title`**: if the user supplied one in the user message, \
use it verbatim. Otherwise propose one based on the questionnaire heading.
8. Output VALID JSON only — no prose, no markdown fences, no commentary.

# Few-shot examples

Example A — answer found in a reference:
```json
{
  "contextualized_question": "What is the legal name of the reporting organization?",
  "source_file": "annual_report_2024.pdf",
  "question": "Organization Name",
  "question_number": "F1",
  "extracted_answer": "Acme Reinsurance Holdings Ltd.",
  "confidence": "HIGH"
}
```

Example B — references silent on the question (note the sentinel triple):
```json
{
  "contextualized_question": "What is the average annual training hours per employee?",
  "source_file": "N/A",
  "question": "Average training hours",
  "question_number": "F23",
  "extracted_answer": "-",
  "confidence": "NONE"
}
```"""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def build_user_message(
    questionnaire_text: str,
    references: list[tuple[str, str]],
    questionnaire_title: str | None,
) -> str:
    """
    Assemble the user-turn message. References are blocks delimited by
    `=== REFERENCE: <filename> ===` so the model can cite filenames exactly.
    """
    parts: list[str] = []
    if questionnaire_title:
        parts.append(f"questionnaire_title (use verbatim): {questionnaire_title}")
        parts.append("")

    parts.append("=== QUESTIONNAIRE ===")
    parts.append(_truncate(questionnaire_text, config.MAX_CHARS_PER_QUESTIONNAIRE))

    for name, text in references:
        parts.append("")
        parts.append(f"=== REFERENCE: {name} ===")
        parts.append(_truncate(text, config.MAX_CHARS_PER_DOC))

    return "\n".join(parts)


def build_messages(
    questionnaire_text: str,
    references: list[tuple[str, str]],
    questionnaire_title: str | None,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_user_message(
                questionnaire_text, references, questionnaire_title
            ),
        },
    ]


EXTRACT_FIELDS_SYSTEM_PROMPT = """/no_think
You are an expert form-analysis assistant. Read the QUESTIONNAIRE block and \
produce a JSON object listing every fillable field / question it contains. \
Do NOT invent answers — every item must use the sentinel triple: \
`extracted_answer="-"`, `source_file="N/A"`, `confidence="NONE"`.

Return JSON that matches this exact schema:

{
  "questionnaire_title": "string",
  "items": [
    {
      "contextualized_question": "string",
      "source_file": "N/A",
      "question": "string",
      "question_number": "F1",
      "extracted_answer": "-",
      "confidence": "NONE"
    }
  ]
}

# Hard rules

1. **Questions come ONLY from the QUESTIONNAIRE block.** Never invent questions.
2. **Numbering**: sequential `F1`, `F2`, `F3`, ... in order of appearance.
3. **`question` is the SHORT TOPIC LABEL** — maximum 8 words / ~50 characters.
4. **`contextualized_question` is the FULL self-contained sentence**, ending with `?`.
5. **Never fill answers** — always `extracted_answer="-"`, `source_file="N/A"`, \
`confidence="NONE"`.
6. **`questionnaire_title`**: if the user supplied one, use it verbatim; otherwise \
propose one from the form heading.
7. Output VALID JSON only — no prose, no markdown fences."""


def build_extract_fields_messages(
    questionnaire_text: str,
    questionnaire_title: str | None,
) -> list[dict[str, str]]:
    parts: list[str] = []
    if questionnaire_title:
        parts.append(f"questionnaire_title (use verbatim): {questionnaire_title}")
        parts.append("")
    parts.append("=== QUESTIONNAIRE ===")
    parts.append(_truncate(questionnaire_text, config.MAX_CHARS_PER_QUESTIONNAIRE))
    return [
        {"role": "system", "content": EXTRACT_FIELDS_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def build_repair_messages(
    original: list[dict[str, str]],
    bad_output: str,
    error: str,
) -> list[dict[str, str]]:
    """Re-prompt with the validation error so the model can self-correct."""
    return original + [
        {"role": "assistant", "content": bad_output},
        {
            "role": "user",
            "content": (
                "Your previous response failed schema validation with this "
                f"error:\n\n{error}\n\nReturn corrected JSON only — same schema, "
                "no prose."
            ),
        },
    ]
