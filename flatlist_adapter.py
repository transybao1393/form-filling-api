"""
Flat-list questionnaire adapter.

For forms where:
  - Fields are AcroForm widgets with generic names (e.g. "Champ de texte 1")
  - The human-readable label sits to the LEFT of the field on the same line
    (typical for French insurance forms, tax forms, application forms)
  - User data is a flat list: { items: [ { question, extracted_answer } ] }

This adapter:
  1. For each field, extracts the label text immediately to its left.
  2. Flattens the items[] list into a searchable catalog.
  3. Fuzzy-matches each field's label to an item's question.
  4. Writes a flat { canonical_key: answer } compatible with form_filler.py.

Usage:
    python3 flatlist_adapter.py \\
        --pdf form.pdf \\
        --fields fields_normalized.json \\
        --data flat_list.json \\
        --items-key items \\
        --question-key question \\
        --answer-key extracted_answer \\
        --out flat_user_data.json

The --items-key, --question-key, --answer-key flags let the adapter work
with any flat-list schema, not just the specific one we saw first.
"""

from __future__ import annotations

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

import pdfplumber

from questionnaire_adapter import _question_text_above


# Question-number prefix used by ESG-style forms (R1, G3, E2, S5, EN7, …).
# Captured at the start of the text-above region.
_QNUM_RE = re.compile(r"^\s*([A-Z]+[0-9]+)\b")

# Strip Q-numbers (one or more letters + digits) to decide if a left-label
# is generic (i.e. carries no question text by itself).
_QNUM_TOKEN_RE = re.compile(r"\b[A-Z]+[0-9]+\b")

# Words that act as table-header chrome around the input cell — they tell
# us nothing about the question itself.
_LABEL_BOILERPLATE = {"answer", "q", "no", "yes", "oui", "non", "n"}


# --------------------------------------------------------------------------- #
# Label extraction: text to the LEFT of a field on the same line
# --------------------------------------------------------------------------- #

def _label_left_of(page, bbox: tuple, max_look_back: float = 200.0) -> str:
    """
    Collect text tokens on the same horizontal line, to the left of the field,
    within `max_look_back` points. Stops at a big horizontal gap that indicates
    a different column.
    """
    x0, top, x1, bottom = bbox
    line_mid_y = (top + bottom) / 2
    line_h = bottom - top

    words = page.extract_words(x_tolerance=2, y_tolerance=2)
    left_candidates = [
        w for w in words
        if abs(((w["top"] + w["bottom"]) / 2) - line_mid_y) < max(6, line_h)
        and w["x1"] <= x0 + 2
        and (x0 - w["x0"]) < max_look_back
    ]
    if not left_candidates:
        return ""

    # Sort left-to-right, then walk backward from the field collecting tokens
    # until we hit a big horizontal gap (new column / new label).
    left_candidates.sort(key=lambda w: w["x0"])
    tail = []
    for w in reversed(left_candidates):
        if not tail:
            tail.append(w)
            continue
        gap = tail[-1]["x0"] - w["x1"]
        if gap < 20:
            tail.append(w)
        else:
            break
    tail.reverse()
    text = " ".join(w["text"] for w in tail)
    # Strip trailing colon/punctuation
    text = re.sub(r"[:\.\s]+$", "", text).strip()
    return text


def _is_generic_left_label(label: str) -> bool:
    """
    True when the left-of-field text carries no real question content —
    e.g. just "Answer", "Answer G1 G2", "Q E10 No", "Answer Q EN7 No".
    These all show up when the table detector picks up an "Answer" cell
    in a questionnaire whose question text actually sits ABOVE the field.

    Strategy: drop Q-number tokens and table-header boilerplate words.
    If nothing substantive remains, it's generic.
    """
    if not label:
        return True
    stripped = _QNUM_TOKEN_RE.sub(" ", label)
    tokens = [t for t in re.split(r"[^A-Za-z]+", stripped) if t]
    residue = [t for t in tokens if t.lower() not in _LABEL_BOILERPLATE]
    return not residue


def enrich_labels_from_left(pdf_path: str | Path,
                            fields_json: str | Path,
                            out_path: str | Path) -> dict:
    data = json.loads(Path(fields_json).read_text())

    with pdfplumber.open(pdf_path) as pdf:
        for f in data["fields"]:
            page = pdf.pages[f["page"] - 1]
            bbox = tuple(f["bbox"])

            label = _label_left_of(page, bbox)
            if label:
                f["left_label"] = label
                # Replace generic placeholders like "Champ de texte 1",
                # "Case à cocher 75", "Answer Field N" with the real label.
                generic = re.match(
                    r"^(champ de texte|case .*cocher|answer field|field|"
                    r"text\s*\d+|checkbox)",
                    f["label"], re.I,
                )
                if generic:
                    f["label"] = label

            # Also pull the question text from above the field, for
            # hybrid-layout forms where label-to-the-left is generic
            # ("Answer") and the real question sits above the input cell.
            question_text = _question_text_above(page, bbox)
            if question_text:
                f["question_text"] = question_text
                m = _QNUM_RE.match(question_text)
                if m:
                    f["question_number"] = m.group(1).upper()
                # If the left-label was useless ("Answer", "Answer G1 G2"),
                # promote the above-text into f["label"] so display + the
                # left-label-based fuzzy path can still find a hit.
                if _is_generic_left_label(f.get("left_label", "")):
                    f["label"] = question_text[:80]

    Path(out_path).write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data


# --------------------------------------------------------------------------- #
# Flat-list data helpers
# --------------------------------------------------------------------------- #

def _normalize_for_match(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return s.strip()


_STOPWORDS = {
    "the", "a", "an", "is", "are", "of", "to", "in", "for", "on", "at",
    "and", "or", "if", "so", "as", "by", "be", "do", "does", "have", "has",
    "any", "your", "please", "provide", "details",
    # French stopwords
    "le", "la", "les", "de", "du", "des", "un", "une", "et", "ou", "a",
    "au", "aux", "ce", "ces", "votre", "vos", "mon", "ma", "mes", "son",
    "sa", "ses", "en", "dans", "sur", "par", "pour", "avec", "sans",
    "que", "qui", "quoi", "n", "o",
}


def _tokens(s: str) -> set[str]:
    return {w for w in _normalize_for_match(s).split()
            if len(w) > 2 and w not in _STOPWORDS}


def _similarity(a: str, b: str) -> float:
    """Token containment with fallback to character ratio."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return SequenceMatcher(None, _normalize_for_match(a),
                               _normalize_for_match(b)).ratio()
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(shorter & longer) / len(shorter)


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def match_fields_to_items(
    fields: list[dict],
    items: list[dict],
    question_key: str = "question",
    contextualized_key: str | None = "contextualized_question",
    number_key: str = "question_number",
    min_similarity: float = 0.5,
) -> dict:
    """
    Returns { canonical_key: item_index } mapping.

    Two-pass strategy:
      1. Exact match on `question_number` (e.g. "G3") when both the field
         and the items carry that key — high precision for ESG-style forms.
      2. Greedy fuzzy match on the remaining fields, using the best of
         `left_label`, `question_text`, and `label` against item question
         and contextualized_question.
    """
    mapping: dict[str, int] = {}
    used_fields: set[int] = set()
    used_items: set[int] = set()

    # --- Pass 1: question_number exact match ------------------------------
    if items and isinstance(items[0], dict) and number_key in items[0]:
        by_number: dict[str, int] = {}
        for ii, item in enumerate(items):
            qn = str(item.get(number_key, "") or "").upper()
            if qn:
                by_number.setdefault(qn, ii)
        for fi, f in enumerate(fields):
            qn = str(f.get("question_number", "") or "").upper()
            if not qn:
                continue
            ii = by_number.get(qn)
            if ii is None or ii in used_items:
                continue
            mapping[f["canonical_key"]] = ii
            used_fields.add(fi)
            used_items.add(ii)

    # --- Pass 2: fuzzy match on remaining fields --------------------------
    # Use left_label, question_text, and label as candidates and take the
    # best similarity per (field, item) pair. left_label still wins for
    # forms with clean left-of-field labels (insurance/tax); question_text
    # carries the load for hybrid forms where left_label is generic.
    pairs: list[tuple[float, int, int]] = []
    for fi, f in enumerate(fields):
        if fi in used_fields:
            continue
        candidates = [
            s for s in (
                f.get("left_label"),
                f.get("question_text"),
                f.get("label"),
            ) if s
        ]
        if not candidates:
            continue
        for ii, item in enumerate(items):
            if ii in used_items:
                continue
            q = str(item.get(question_key, ""))
            cq = str(item.get(contextualized_key, "")) if contextualized_key else ""
            best = 0.0
            for cand in candidates:
                if q:
                    best = max(best, _similarity(cand, q))
                if cq:
                    best = max(best, _similarity(cand, cq))
            if best >= min_similarity:
                pairs.append((best, fi, ii))

    pairs.sort(key=lambda p: -p[0])
    for score, fi, ii in pairs:
        if fi in used_fields or ii in used_items:
            continue
        mapping[fields[fi]["canonical_key"]] = ii
        used_fields.add(fi)
        used_items.add(ii)

    return mapping


def build_flat_user_data(
    fields: list[dict],
    items: list[dict],
    question_key: str = "question",
    contextualized_key: str | None = "contextualized_question",
    answer_key: str = "extracted_answer",
    empty_sentinels: tuple = ("", "-", None, "N/A", "n/a"),
) -> tuple[dict, dict]:
    """Returns (flat_user_data, diagnostics)."""
    mapping = match_fields_to_items(
        fields, items, question_key=question_key,
        contextualized_key=contextualized_key,
    )

    flat: dict = {}
    diag: dict = {}
    for f in fields:
        key = f["canonical_key"]
        idx = mapping.get(key)
        if idx is None:
            continue
        item = items[idx]
        answer = item.get(answer_key, "")
        has_answer = answer not in empty_sentinels

        diag[key] = {
            "item_index": idx,
            "question": str(item.get(question_key, ""))[:100],
            "matched_label": (
                f.get("left_label")
                or f.get("question_text")
                or f.get("label", "")
            ),
            "question_number": f.get("question_number"),
            "has_answer": has_answer,
            "field_type": f.get("field_type", "text"),
        }

        if has_answer:
            # Clean up checkbox-like answers (e.g. "☑ non — ...")
            ans = str(answer)
            if f.get("field_type") == "checkbox":
                # Truthy if starts with ☑, ✓, "oui", "yes", "true"
                yes_prefix = re.match(r"\s*[☑✓]|\s*(oui|yes|true|x)\b",
                                      ans, re.I)
                flat[key] = True if yes_prefix else False
            else:
                flat[key] = ans

    return flat, diag


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--fields", required=True,
                    help="fields_normalized.json from the normalizer")
    ap.add_argument("--data", required=True,
                    help="Flat-list JSON (e.g. { items: [...] })")
    ap.add_argument("--items-key", default="items",
                    help="Top-level key holding the list (default: items)")
    ap.add_argument("--question-key", default="question")
    ap.add_argument("--contextualized-key", default="contextualized_question")
    ap.add_argument("--answer-key", default="extracted_answer")
    ap.add_argument("--out", default="flat_user_data.json")
    ap.add_argument("--enriched-fields", default=None)
    ap.add_argument("--diagnostics", default=None)
    ap.add_argument("--min-similarity", type=float, default=0.5)
    args = ap.parse_args()

    # Step 1: enrich labels using left-neighbour text
    enriched_out = args.enriched_fields or args.fields.replace(
        ".json", "_enriched.json"
    )
    enriched = enrich_labels_from_left(args.pdf, args.fields, enriched_out)

    # Step 2: load items
    data = json.loads(Path(args.data).read_text())
    items = data.get(args.items_key, data)
    if not isinstance(items, list):
        raise SystemExit(
            f"Expected a list at key '{args.items_key}' in {args.data}, "
            f"got {type(items).__name__}"
        )

    # Step 3: match + build flat user data
    flat, diag = build_flat_user_data(
        enriched["fields"], items,
        question_key=args.question_key,
        contextualized_key=args.contextualized_key,
        answer_key=args.answer_key,
    )

    Path(args.out).write_text(json.dumps(flat, indent=2, ensure_ascii=False))
    if args.diagnostics:
        Path(args.diagnostics).write_text(
            json.dumps(diag, indent=2, ensure_ascii=False)
        )

    total = len(enriched["fields"])
    matched = len(diag)
    with_ans = sum(1 for d in diag.values() if d["has_answer"])
    print(f"Fields: {total}  matched: {matched}  "
          f"with answers: {with_ans}  -> {args.out}")
    print(f"Enriched fields: {enriched_out}")
    if args.diagnostics:
        print(f"Diagnostics: {args.diagnostics}")


if __name__ == "__main__":
    main()
