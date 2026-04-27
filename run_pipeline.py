"""
Smart pipeline runner — auto-detects data format and dispatches the right
stages.

Data format detection (in priority order):

  1. FLAT       — data is a dict of primitives: { "first_name": "Bao", ... }
                  No adapter needed; keys must match canonical_keys.

  2. FLATLIST   — data is { items: [ { question, answer/extracted_answer, ... } ] }
                  or a top-level list of such objects.
                  Uses flatlist_adapter.py + AcroForm native fill.

  3. NESTED     — data has nested structure with deep { id, question }
                  nodes (e.g. sections -> categories -> topics -> questions).
                  Uses questionnaire_adapter.py + AcroForm native fill.

Usage:
    python3 run_pipeline.py source.pdf data.json -o filled.pdf
    python3 run_pipeline.py source.pdf data.json --workdir output/test1 \\
                            [--answers answers.json] [--format flatlist]

The --format flag forces a specific mode if auto-detect gets it wrong.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from field_detector import detect_fields_to_json
from field_normalizer import enrich_json
from form_filler import fill_form


# --------------------------------------------------------------------------- #
# Format detection
# --------------------------------------------------------------------------- #

# Common keys used in flat-list questionnaire schemas
FLATLIST_ITEM_KEYS = {"items", "questions", "entries", "fields", "data"}
QUESTION_KEYS = {"question", "label", "prompt", "title", "text",
                 "contextualized_question"}
ANSWER_KEYS = {"answer", "extracted_answer", "value", "response", "reply"}


def _looks_like_flat_dict(d: Any) -> bool:
    """A flat {key: scalar} map — values are strings/numbers/bools."""
    if not isinstance(d, dict):
        return False
    # Skip meta keys used by template generator
    real = {k: v for k, v in d.items() if not k.startswith("_")}
    if not real:
        return False
    scalar_count = 0
    for v in real.values():
        if isinstance(v, (str, int, float, bool, type(None))):
            scalar_count += 1
    # If >= 70% of top-level values are scalar, it's flat
    return scalar_count / len(real) >= 0.7


def _looks_like_flatlist(d: Any) -> tuple[bool, str | None, list | None]:
    """
    Detect { <key>: [ { question, answer } ] } schema.
    Returns (is_flatlist, items_key, items_list).
    """
    # Top-level list of question objects
    if isinstance(d, list):
        if d and isinstance(d[0], dict) and \
                any(k in d[0] for k in QUESTION_KEYS):
            return True, None, d
        return False, None, None

    if not isinstance(d, dict):
        return False, None, None

    # Find a key whose value is a list of question-like dicts
    for key in FLATLIST_ITEM_KEYS:
        if key in d and isinstance(d[key], list) and d[key]:
            first = d[key][0]
            if isinstance(first, dict) and \
                    any(k in first for k in QUESTION_KEYS):
                return True, key, d[key]

    # Fallback: any list value that contains question-like dicts
    for key, v in d.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and \
                any(k in v[0] for k in QUESTION_KEYS):
            return True, key, v

    return False, None, None


def _looks_like_nested(d: Any) -> bool:
    """Deep nested structure with {id, question} leaves at any depth."""
    def has_question_leaf(node, depth=0):
        if depth > 15:
            return False
        if isinstance(node, dict):
            if "question" in node and ("id" in node or "qid" in node):
                return True
            return any(has_question_leaf(v, depth + 1) for v in node.values())
        if isinstance(node, list):
            return any(has_question_leaf(v, depth + 1) for v in node)
        return False

    return has_question_leaf(d)


def detect_format(data: Any) -> str:
    """Return one of: 'flat', 'flatlist', 'nested'."""
    # Flatlist check first — often dicts also pass flat-dict if items is one
    # of few keys, but the presence of a question list is a strong signal.
    is_fl, _, _ = _looks_like_flatlist(data)
    if is_fl:
        return "flatlist"
    if _looks_like_nested(data):
        return "nested"
    if _looks_like_flat_dict(data):
        return "flat"
    # Default: try flat
    return "flat"


def detect_question_answer_keys(items: list[dict]) -> tuple[str, str | None, str]:
    """
    Inspect the first item to find the best question/contextualized/answer keys.
    """
    if not items:
        return "question", None, "answer"
    first = items[0]

    q_key = next((k for k in ("question", "label", "prompt", "title", "text")
                  if k in first), "question")
    cq_key = "contextualized_question" if "contextualized_question" in first else None
    a_key = next((k for k in ("extracted_answer", "answer", "value", "response")
                  if k in first), "answer")

    return q_key, cq_key, a_key


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

def _is_docx(path: str) -> bool:
    return Path(path).suffix.lower() == ".docx"


def _run_docx(
    source_docx: str,
    user_data_json: str,
    output_docx: str,
    fields_enriched: Path,
    flat_data_path: Path,
    diagnostics_path: Path,
    format_override: str | None = None,
) -> dict:
    """Docx variant: structural detection + reuse of flatlist matching."""
    from docx_detector import detect_docx_fields_to_json
    from docx_filler import fill_docx
    from flatlist_adapter import build_flat_user_data

    if Path(output_docx).suffix.lower() != ".docx":
        output_docx = str(Path(output_docx).with_suffix(".docx"))

    print(f"[1/3] Detecting fields in {source_docx} (docx) ...")
    det = detect_docx_fields_to_json(source_docx, fields_enriched)
    print(f"       -> {det['num_fields']} fields detected")

    if det["num_fields"] == 0:
        print("       ! No empty cells found in any table.")
        return {"num_filled": 0, "num_missing": 0, "output_pdf": ""}

    # Detection already produced an enriched fields list (label, left_label,
    # question_text, question_number, canonical_key, section, docx_locator).
    # No separate PDF normalization step.
    print(f"[2/3] Loading user data ...")
    user_data_raw = json.loads(Path(user_data_json).read_text())
    fmt = format_override or detect_format(user_data_raw)
    print(f"       -> data format: {fmt}"
          + (" (override)" if format_override else " (auto-detected)"))

    if fmt == "flat":
        filler_data = {k: v for k, v in user_data_raw.items()
                       if not k.startswith("_")}
    elif fmt == "flatlist":
        is_fl, items_key, items = _looks_like_flatlist(user_data_raw)
        q_key, cq_key, a_key = detect_question_answer_keys(items)
        print(f"       -> using keys: question='{q_key}', answer='{a_key}'"
              + (f", contextualized='{cq_key}'" if cq_key else ""))
        flat, diag = build_flat_user_data(
            det["fields"], items,
            question_key=q_key, contextualized_key=cq_key, answer_key=a_key,
        )
        flat_data_path.write_text(json.dumps(flat, indent=2, ensure_ascii=False))
        diagnostics_path.write_text(json.dumps(diag, indent=2, ensure_ascii=False))
        matched = len(diag)
        with_ans = sum(1 for d in diag.values() if d["has_answer"])
        print(f"       -> matched {matched} fields, {with_ans} with answers")
        filler_data = flat
    else:
        raise ValueError(
            f"docx pipeline does not support format '{fmt}' yet "
            "(use flat or flatlist)"
        )

    print("[3/3] Writing filled docx ...")
    report = fill_docx(source_docx, fields_enriched, filler_data, output_docx)
    total = report["num_filled"] + report["num_missing"]
    print(f"       -> {report['num_filled']}/{total} fields filled")
    print(f"       -> {output_docx}")
    if fmt != "flat":
        print(f"       -> diagnostics: {diagnostics_path}")
    return report


def run(
    source_pdf: str,
    user_data_json: str,
    output_pdf: str = "filled.pdf",
    workdir: str = ".",
    format_override: str | None = None,
    answers_json: str | None = None,
) -> dict:
    workdir_path = Path(workdir)
    workdir_path.mkdir(parents=True, exist_ok=True)

    fields_raw = workdir_path / "fields.json"
    fields_norm = workdir_path / "fields_normalized.json"
    fields_enriched = workdir_path / "fields_enriched.json"
    flat_data_path = workdir_path / "flat_data.json"
    diagnostics_path = workdir_path / "diagnostics.json"

    # ---- DOCX path: structural detection, no PDF stages -------------------
    if _is_docx(source_pdf):
        return _run_docx(
            source_docx=source_pdf,
            user_data_json=user_data_json,
            output_docx=output_pdf,
            fields_enriched=fields_enriched,
            flat_data_path=flat_data_path,
            diagnostics_path=diagnostics_path,
            format_override=format_override,
        )

    # ---- Stage 1: detection ------------------------------------------------
    print(f"[1/3] Detecting fields in {source_pdf} ...")
    det = detect_fields_to_json(source_pdf, fields_raw)
    print(f"       -> {det['num_fields']} fields detected")
    print(f"       -> by strategy: {det['fields_by_strategy']}")

    if det["num_fields"] == 0:
        print("       ! No fields detected.")
        print("         - Check if PDF has text layer (not scanned)")
        print("         - Run: python3 form_utils.py visualize <pdf> fields.json")
        return {"num_filled": 0, "num_missing": 0, "output_pdf": ""}

    # ---- Stage 2: normalization -------------------------------------------
    print("[2/3] Normalizing field labels ...")
    norm = enrich_json(fields_raw, fields_norm)
    print(f"       -> {norm['num_fields']} fields normalized")

    # ---- Detect data format -----------------------------------------------
    user_data_raw = json.loads(Path(user_data_json).read_text())
    fmt = format_override or detect_format(user_data_raw)
    print(f"[2b]  Data format: {fmt}"
          + (f" (override)" if format_override else " (auto-detected)"))

    # ---- Dispatch based on format -----------------------------------------
    fields_for_fill: Path = fields_norm
    filler_data: dict
    use_acroform_native = False

    if fmt == "flat":
        # Strip template meta keys and use directly
        filler_data = {k: v for k, v in user_data_raw.items()
                       if not k.startswith("_")}

    elif fmt == "flatlist":
        print("[2c]  Running flat-list adapter (left-side labels)")
        from flatlist_adapter import (
            enrich_labels_from_left, build_flat_user_data,
        )
        enrich_labels_from_left(source_pdf, fields_norm, fields_enriched)
        # Re-load enriched fields
        enriched = json.loads(Path(fields_enriched).read_text())

        is_fl, items_key, items = _looks_like_flatlist(user_data_raw)
        q_key, cq_key, a_key = detect_question_answer_keys(items)
        print(f"       -> using keys: question='{q_key}', answer='{a_key}'"
              + (f", contextualized='{cq_key}'" if cq_key else ""))

        flat, diag = build_flat_user_data(
            enriched["fields"], items,
            question_key=q_key, contextualized_key=cq_key,
            answer_key=a_key,
        )
        flat_data_path.write_text(json.dumps(flat, indent=2, ensure_ascii=False))
        diagnostics_path.write_text(json.dumps(diag, indent=2, ensure_ascii=False))

        matched = len(diag)
        with_ans = sum(1 for d in diag.values() if d["has_answer"])
        print(f"       -> matched {matched} fields, {with_ans} with answers")

        fields_for_fill = fields_enriched
        filler_data = flat
        use_acroform_native = True

    elif fmt == "nested":
        print("[2c]  Running nested-JSON adapter (question text above fields)")
        from questionnaire_adapter import (
            enrich_labels_with_questions, build_flat_user_data,
        )
        enrich_labels_with_questions(source_pdf, fields_norm, fields_enriched)
        enriched = json.loads(Path(fields_enriched).read_text())

        overrides = None
        if answers_json and Path(answers_json).exists():
            overrides = json.loads(Path(answers_json).read_text())

        flat, diag = build_flat_user_data(
            enriched["fields"], user_data_raw,
            question_to_answer=overrides,
        )
        flat_data_path.write_text(json.dumps(flat, indent=2, ensure_ascii=False))
        diagnostics_path.write_text(json.dumps(diag, indent=2, ensure_ascii=False))

        matched = len(diag)
        with_ans = sum(1 for d in diag.values() if d["has_answer"])
        print(f"       -> matched {matched} fields, {with_ans} with answers")

        fields_for_fill = fields_enriched
        filler_data = flat
        use_acroform_native = True

    else:
        raise ValueError(f"Unknown format: {fmt}")

    # ---- Stage 3: fill -----------------------------------------------------
    print(f"[3/3] Filling form (acroform_native={use_acroform_native}) ...")
    report = fill_form(
        source_pdf, fields_for_fill, filler_data, output_pdf,
        acroform_native=use_acroform_native,
        missing_behaviour="skip",
    )
    total = report['num_filled'] + report['num_missing']
    print(f"       -> {report['num_filled']}/{total} fields filled")
    print(f"       -> {output_pdf}")
    if fmt != "flat":
        print(f"       -> diagnostics: {diagnostics_path}")

    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("source_pdf")
    ap.add_argument("user_data_json")
    ap.add_argument("-o", "--output", default="filled.pdf")
    ap.add_argument("--workdir", default=".")
    ap.add_argument("--format", choices=["flat", "flatlist", "nested"],
                    default=None, help="Force a specific data format")
    ap.add_argument("--answers",
                    help="Optional flat {question_id: answer} JSON (nested mode)")
    args = ap.parse_args()

    run(
        args.source_pdf,
        args.user_data_json,
        output_pdf=args.output,
        workdir=args.workdir,
        format_override=args.format,
        answers_json=args.answers,
    )
